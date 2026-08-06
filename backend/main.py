"""FastAPI entry point for the Orbit Swarm control plane.

The standard-library entry point implements the same task state machine.  Keep
the state fields and approval gates in sync when changing either module.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from executors import (
    MODEL_TIERS,
    REASONING_LEVELS,
    RuntimeConfig,
    assess_task_difficulty,
    attachment_prompt,
    available_executors,
    build_reasoning_recommendation,
    COORDINATOR_INSTRUCTIONS,
    choose_executor,
    coordinator_reply as shared_coordinator_reply,
    execute_agent,
    model_for_tier,
    normalize_attachments,
    public_attachments,
    public_config,
    persistable_config,
    reasoning_proposal_reply,
    reasoning_for_tier,
    resolve_route,
    route_public,
    secret_values,
    restore_persisted_config,
    synthesis_reply,
    updated_config,
    is_greeting,
)
from routing_api import (
    disable_provider,
    provider_catalog,
    provider_test,
    remove_route,
    route_catalog,
    update_provider,
    update_route,
)
from exports import (
    attachment_metadata,
    build_export_payload,
    content_disposition,
    continuation_context,
    continuation_context_text,
    continuation_welcome,
    render_markdown,
    safe_export_filename,
)
from storage import (
    AtomicJsonStateStore,
    build_state_document,
    default_state_path,
    normalize_loaded_tasks,
    restore_attachments,
    utc_timestamp,
)
from cluster import (
    MODE_LABELS,
    ROLE_KEYS,
    ClusterRuntime,
    configured_mode,
    get_cluster_runtime,
    parse_mode,
    role_catalog,
    role_route_key,
)
from agent_profile_commands import parse_agent_profile_command

ROOT = Path(__file__).resolve().parent.parent
ENV_CONFIG = RuntimeConfig.from_environment()
CONFIG = ENV_CONFIG
CONFIG_SNAPSHOTS: dict[str, RuntimeConfig] = {}
TASK_ATTACHMENTS: dict[str, list[dict]] = {}
TASK_CLUSTERS: dict[str, ClusterRuntime] = {}
WEIGHT_CONDITION = asyncio.Condition()
WEIGHT_USED = 0
TASKS: dict[str, dict[str, Any]] = {}
SUBSCRIBERS: dict[str, set[WebSocket]] = {}
STATE_STORE = AtomicJsonStateStore(default_state_path(ROOT))
CLUSTER = get_cluster_runtime(config=CONFIG)


def _state_document() -> dict:
    return build_state_document(
        TASKS,
        persistable_config(CONFIG),
        {task_id: persistable_config(config) for task_id, config in CONFIG_SNAPSHOTS.items()},
        TASK_ATTACHMENTS,
    )


def persist_state() -> bool:
    secrets = secret_values(ENV_CONFIG) + secret_values(CONFIG)
    for snapshot in CONFIG_SNAPSHOTS.values():
        secrets.extend(secret_values(snapshot))
    return STATE_STORE.save(_state_document(), secrets)


class PersistenceUnavailable(HTTPException):
    def __init__(self) -> None:
        self.persistence = STATE_STORE.status()
        super().__init__(
            status_code=503,
            detail=STATE_STORE.last_error or "Could not persist local state",
        )


def _persistence_failure() -> PersistenceUnavailable:
    return PersistenceUnavailable()


def load_persisted_state() -> None:
    global CONFIG, CLUSTER
    document = STATE_STORE.load()
    recovery_warnings: list[str] = []
    saved_config = document.get("config")
    if saved_config not in (None, {}) and not isinstance(saved_config, dict):
        recovery_warnings.append("The persisted runtime configuration was not an object")
    try:
        CONFIG = restore_persisted_config(ENV_CONFIG, saved_config)
    except (AttributeError, TypeError, ValueError) as error:
        CONFIG = ENV_CONFIG
        recovery_warnings.append(f"The persisted runtime configuration was ignored: {error}")
    CLUSTER = get_cluster_runtime(refresh=True, mode=getattr(CONFIG, "mode", 0), config=CONFIG)
    tasks, changed, task_warnings = normalize_loaded_tasks(document.get("tasks"))
    recovery_warnings.extend(task_warnings)
    TASKS.clear()
    TASKS.update(tasks)
    TASK_ATTACHMENTS.clear()
    if document.get("task_attachments") not in (None, {}) and not isinstance(document.get("task_attachments"), dict):
        recovery_warnings.append("The persisted attachment index was not an object")
    TASK_ATTACHMENTS.update(restore_attachments(document.get("task_attachments")))
    CONFIG_SNAPSHOTS.clear()
    raw_snapshots = document.get("config_snapshots")
    if raw_snapshots not in (None, {}) and not isinstance(raw_snapshots, dict):
        recovery_warnings.append("The persisted configuration snapshots were not an object")
    saved_snapshots = raw_snapshots if isinstance(raw_snapshots, dict) else {}
    for task_id in TASKS:
        try:
            CONFIG_SNAPSHOTS[task_id] = restore_persisted_config(CONFIG, saved_snapshots.get(task_id))
        except (AttributeError, TypeError, ValueError) as error:
            CONFIG_SNAPSHOTS[task_id] = CONFIG
            recovery_warnings.append(f"Task {task_id} had an invalid configuration snapshot: {error}")
        TASK_ATTACHMENTS.setdefault(task_id, [])
        SUBSCRIBERS.setdefault(task_id, set())
    if recovery_warnings and STATE_STORE.load_state != "recovered":
        STATE_STORE.preserve_for_recovery("Persisted state required structural recovery: " + "; ".join(recovery_warnings[:4]))
    if changed or recovery_warnings or STATE_STORE.migration_required:
        persist_state()


load_persisted_state()

app = FastAPI(title="Orbit Swarm", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:8000", "http://localhost:8000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(PersistenceUnavailable)
async def persistence_failure_handler(_request: Request, error: PersistenceUnavailable) -> JSONResponse:
    return JSONResponse(
        status_code=error.status_code,
        content={"detail": error.detail, "persistence": error.persistence},
    )


class AttachmentInput(BaseModel):
    name: str = Field(min_length=1, max_length=180)
    type: str = Field(default="application/octet-stream", max_length=120)
    size: int = Field(default=0, ge=0, le=100_000_000)
    content: str = Field(default="", max_length=250_000)


class CreateTask(BaseModel):
    prompt: str = Field(min_length=1, max_length=8000)
    cluster_enabled: bool = False
    workspace: bool = False
    attachments: list[AttachmentInput] = Field(default_factory=list, max_length=5)
    # Optional per-task override. Runtime configuration remains the default.
    cluster_reasoning: str | None = Field(default=None, max_length=20)
    # MODE 0-3 is optional so all existing clients keep the legacy workflow.
    mode: int | str | None = Field(default=None)
    cluster_mode: int | str | None = Field(default=None)
    runtime_mode: int | str | None = Field(default=None)


class ControlTask(BaseModel):
    action: str = Field(pattern="^(pause|resume|cancel|retry|confirm_workflow|approve_reasoning|start_cluster)$")
    level: str | None = Field(default=None, max_length=20)
    reasoning_level: str | None = Field(default=None, max_length=20)


class MessageTask(BaseModel):
    content: str = Field(min_length=1, max_length=8000)
    cluster_enabled: bool | None = None


class ContinueTask(BaseModel):
    content: str = Field(default="", max_length=8000)


def clock() -> str:
    return datetime.now().strftime("%H:%M:%S")


def public(task: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in task.items() if not key.startswith("_")}


def add_message(task: dict[str, Any], role: str, content: str, agent_id: str | None = None) -> None:
    timestamp = utc_timestamp()
    estimated = max(1, (len(str(content)) + 3) // 4)
    task.setdefault("conversation", []).append(
        {"id": str(uuid4()), "time": timestamp[11:19], "timestamp": timestamp, "role": role, "content": content, "agent_id": agent_id, "token_estimate": estimated, "token_usage": {"input_tokens": estimated if role == "user" else 0, "output_tokens": estimated if role == "assistant" else 0}}
    )


def add_work_log(task: dict[str, Any], agent_id: str, kind: str, message: str) -> None:
    timestamp = utc_timestamp()
    event = {"id": str(uuid4()), "time": timestamp[11:19], "timestamp": timestamp, "type": kind, "message": message, "agent_id": agent_id}
    task.setdefault("events", []).append(event)
    agent = next((item for item in task.get("agents", []) if item["id"] == agent_id), None)
    if agent is not None:
        agent.setdefault("work_log", []).append(
            {"time": event["time"], "timestamp": timestamp, "type": kind, "message": message}
        )


def add_agent_conversation(task: dict[str, Any], agent_id: str, role: str, content: str) -> None:
    # Keep the transcript unambiguous: ``lead`` is Orion and ``agent`` is the
    # selected worker. ``assistant`` is accepted from older call sites and is
    # normalized according to the owning agent.
    normalized_role = role
    if role == "assistant":
        normalized_role = "lead" if agent_id == "lead" else "agent"
    timestamp = utc_timestamp()
    estimated = max(1, (len(str(content)) + 3) // 4)
    input_tokens = estimated if role in {"user", "lead"} else 0
    output_tokens = estimated if role in {"assistant", "agent"} else 0
    task.setdefault("agent_conversations", {}).setdefault(agent_id, []).append(
        {"role": normalized_role, "speaker": normalized_role, "content": str(content), "time": timestamp[11:19], "timestamp": timestamp, "agent_id": agent_id, "token_estimate": estimated, "token_usage": {"input_tokens": input_tokens, "output_tokens": output_tokens}}
    )


def add_dispatch_turns(task: dict[str, Any], agent_id: str, objective: str) -> None:
    add_agent_conversation(task, "lead", "assistant", f"Dispatching {agent_id}: {objective}")
    add_agent_conversation(task, agent_id, "lead", objective)


def coordinator_reply(
    prompt: str,
    cluster_available: bool,
    questions: list[str] | None = None,
    recommendation: dict | None = None,
    workflow_ready: bool = False,
) -> str:
    return shared_coordinator_reply(prompt, questions, recommendation, cluster_available, workflow_ready)


def system_payload() -> dict[str, Any]:
    cluster = CLUSTER.status()
    provider_state = provider_catalog(CONFIG)
    return {
        **public_config(CONFIG),
        "executors": available_executors(),
        "live_provider_configured": any(item.get("configured") and item.get("enabled", True) for item in provider_state.get("providers", [])),
        "max_concurrency": CONFIG.max_weight,
        "used_weight": WEIGHT_USED,
        "persistence": STATE_STORE.status(),
        "mode": cluster["mode"],
        "runtime_mode": cluster["mode"],
        "mode_label": cluster["mode_label"],
        "simulation_mode": cluster["simulation"],
        "agent_slots": cluster["agent_slots"],
        "agents": cluster["agent_slots"],
        "active_agents": [agent for agent in cluster["agent_slots"] if agent.get("active")],
        "role_counts": cluster["role_counts"],
        "active_counts": cluster["active_counts"],
        "online_counts": cluster["online_counts"],
        "roles": cluster["roles"],
        "role_status": cluster["role_status"],
        "health": cluster["health"],
        "cluster": cluster,
    }


async def acquire_weight(requested_weight: int, task_id: str) -> int:
    global WEIGHT_USED
    while await active(task_id):
        async with WEIGHT_CONDITION:
            # Use the task snapshot's limit semantics for workers while still
            # allowing a live config update to wake waiting tasks.
            weight = min(max(1, int(requested_weight)), max(1, CONFIG.max_weight))
            if WEIGHT_USED + weight <= CONFIG.max_weight:
                WEIGHT_USED += weight
                return weight
            try:
                await asyncio.wait_for(WEIGHT_CONDITION.wait(), timeout=0.15)
            except TimeoutError:
                pass
    return 0


async def release_weight(weight: int) -> None:
    global WEIGHT_USED
    if not weight:
        return
    async with WEIGHT_CONDITION:
        WEIGHT_USED = max(0, WEIGHT_USED - weight)
        WEIGHT_CONDITION.notify_all()


async def broadcast(
    task_id: str,
    kind: str,
    message: str,
    agent_id: str | None = None,
    rollback_task: dict[str, Any] | None = None,
) -> bool:
    task = TASKS.get(task_id)
    if not task:
        return False
    original_task = deepcopy(task) if rollback_task is None else rollback_task
    if agent_id:
        add_work_log(task, agent_id, kind, message)
    else:
        timestamp = utc_timestamp()
        task.setdefault("events", []).append(
            {"id": str(uuid4()), "time": timestamp[11:19], "timestamp": timestamp, "type": kind, "message": message, "agent_id": agent_id}
        )
    if not persist_state():
        restored_task = deepcopy(original_task)
        task.clear()
        task.update(restored_task)
        TASKS[task_id] = task
        return False
    payload = {"type": "snapshot", "task": public(task)}
    disconnected = []
    for socket in SUBSCRIBERS.get(task_id, set()).copy():
        try:
            await socket.send_json(payload)
        except Exception:
            disconnected.append(socket)
    for socket in disconnected:
        SUBSCRIBERS.setdefault(task_id, set()).discard(socket)
    return True


async def active(task_id: str) -> bool:
    while task_id in TASKS and TASKS[task_id].get("status") == "paused":
        await asyncio.sleep(0.2)
    return task_id in TASKS and TASKS[task_id].get("status") != "cancelled"


def _task_questions(task: dict[str, Any]) -> list[str]:
    return list(task.get("clarifying_questions") or task.get("difficulty", {}).get("clarifying_questions", []))


async def answer_message(task_id: str, content: str, reply_token: int | None = None) -> None:
    await asyncio.sleep(0.18)
    task = TASKS.get(task_id)
    if (
        not task
        or task["status"] in {"cancelled", "complete"}
        or task.get("cluster_started")
        or (reply_token is not None and task.get("reply_token") != reply_token)
    ):
        return
    original_task = deepcopy(task)

    # New information invalidates an earlier review. The coordinator must
    # ask again and receive fresh approval before a cluster can start.
    combined_prompt = task.get("prompt_context") or task.get("prompt", "")
    assessment, recommendation = build_reasoning_recommendation(
        combined_prompt,
        TASK_ATTACHMENTS.get(task_id, []),
        bool(task.get("workspace")),
        CONFIG_SNAPSHOTS.get(task_id, CONFIG),
    )
    cluster_available = bool(
        task.get("cluster_enabled")
        and not is_greeting(combined_prompt)
        and int(assessment.get("recommended_agents", 1)) > 1
    )
    task["cluster_available"] = cluster_available
    task["approval_required"] = cluster_available
    task["difficulty"] = assessment
    task["difficulty_assessment"] = assessment
    recommendation["approved"] = False
    task["reasoning_recommendation"] = recommendation
    task["reasoning_profile"] = recommendation
    task["clarifying_questions"] = list(assessment["clarifying_questions"])
    task["reasoning_proposed"] = False
    task["reasoning_approved"] = False
    task["workflow_confirmed"] = False
    task.setdefault("review", {}).update(status="pending", approved_at=None, approved_by=None)
    task["review"]["required"] = cluster_available
    task["workflow_ready"] = bool(cluster_available and task.get("task_turns", 1) >= 2)
    task["needs_clarification"] = bool(cluster_available and task["clarifying_questions"] and not task["workflow_ready"])

    reply = coordinator_reply(
        combined_prompt,
        bool(task.get("cluster_available")),
        task["clarifying_questions"],
        recommendation,
        task["workflow_ready"],
    )
    add_message(task, "assistant", reply, "lead")
    task["assistant_replied"] = True
    task["last_answered_token"] = reply_token if reply_token is not None else task.get("reply_token", 0)
    can_offer_cluster = (
        bool(task.get("cluster_available"))
        and task.get("task_turns", 1) >= 2
        and task.get("workflow_ready")
        and not task.get("needs_clarification")
        and not task.get("cluster_started")
    )
    task["status"] = "awaiting_confirmation" if can_offer_cluster else "chatting"
    task["conversation_state"] = "awaiting_confirmation" if can_offer_cluster else "chatting"
    task["agents"][0].update(status="ready", step="Waiting for your review" if can_offer_cluster else "Waiting for your next instruction")
    add_agent_conversation(task, "lead", "assistant", reply)
    if not await broadcast(
        task_id,
        "assistant_message",
        "Orion replied with clarification questions or an updated workflow",
        "lead",
        rollback_task=original_task,
    ):
        return


def _approval_error(task: dict[str, Any]) -> str:
    missing = []
    if not task.get("cluster_available"):
        missing.append("cluster enabled for this task")
    if not task.get("assistant_replied"):
        missing.append("coordinator reply")
    if task.get("task_turns", 1) < 2:
        missing.append("one clarification reply from the user")
    if not task.get("workflow_ready") or task.get("needs_clarification"):
        missing.append("coordinator workflow restatement")
    if not task.get("workflow_confirmed"):
        missing.append("workflow confirmation")
    if not task.get("reasoning_proposed"):
        missing.append("reasoning recommendation")
    if not task.get("reasoning_approved") or not task.get("reasoning_recommendation", {}).get("approved"):
        missing.append("reasoning approval")
    return "Cluster review is incomplete: " + ", ".join(missing)


def _prepare_cluster_start(task: dict[str, Any]) -> bool:
    if (
        not task.get("cluster_available")
        or task.get("cluster_started")
        or not task.get("assistant_replied")
        or task.get("task_turns", 1) < 2
        or not task.get("workflow_ready")
        or task.get("needs_clarification")
        or not task.get("workflow_confirmed")
        or not task.get("reasoning_proposed")
        or not task.get("reasoning_approved")
        or not task.get("reasoning_recommendation", {}).get("approved")
    ):
        return False
    task["cluster_started"] = True
    task["conversation_state"] = "cluster_starting"
    task["status"] = "queued"
    task["review"]["status"] = "approved"
    task["review"]["approved_at"] = utc_timestamp()
    task["cluster_started_at"] = task["review"]["approved_at"]
    add_agent_conversation(task, "lead", "system", "User approved the workflow and reasoning profile")
    return True


async def start_cluster(task_id: str) -> bool:
    task = TASKS.get(task_id)
    if not task:
        return False
    original_task = deepcopy(task)
    if not _prepare_cluster_start(task):
        return False
    if not await broadcast(
        task_id,
        "cluster_started",
        "The approved workflow is starting the agent cluster",
        "lead",
        rollback_task=original_task,
    ):
        if task_id not in TASKS:
            TASKS[task_id] = original_task
        raise _persistence_failure()
    asyncio.create_task(run_swarm(task_id))
    return True


def _worker_tiers(recommended: str, count: int) -> list[str]:
    profiles = {
        "low": ["low", "low", "low", "low"],
        "medium": ["medium", "medium", "low", "low"],
        "high": ["medium", "high", "medium", "low"],
        "ultra": ["high", "ultra", "high", "medium"],
    }
    return profiles.get(recommended, profiles["medium"])[:count]


async def run_worker(task_id: str, agent_id: str, delay: float) -> None:
    task = TASKS.get(task_id)
    if not task:
        return
    config = CONFIG_SNAPSHOTS.get(task_id, CONFIG)
    agent = next((item for item in task["agents"] if item["id"] == agent_id), None)
    if not agent or not await active(task_id):
        return
    await asyncio.sleep(delay)
    if not await active(task_id):
        agent["status"] = "cancelled"
        return
    agent["status"] = "working"
    agent["step"] = "Waiting for weighted capacity"
    await broadcast(task_id, "agent_started", f"{agent['name']} is waiting for weighted capacity", agent_id)
    acquired_weight = await acquire_weight(agent.get("weight", 1), task_id)
    if not acquired_weight:
        return
    try:
        agent["step"] = "Working through assigned evidence"
        add_agent_conversation(task, "lead", "assistant", f"Checking progress for {agent['name']}")
        add_agent_conversation(task, agent_id, "assistant", f"Started the {agent['role'].lower()} pass")
        await broadcast(task_id, "agent_progress", f"{agent['name']} started its assigned evidence pass", agent_id)
        prompt = (
            f"Overall task and clarified context: {task.get('prompt_context', task['prompt'])}\n\nYour role: {agent['role']}\n"
            f"Assignment: {agent['objective']}\nCluster reasoning profile: {task.get('cluster_reasoning', config.cluster_reasoning)}"
            + attachment_prompt(TASK_ATTACHMENTS.get(task_id, []))
        )
        workspace = str(ROOT) if task["workspace"] else None
        route = resolve_route(
            config,
            agent["model_tier"],
            role=agent.get("role"),
            pool=agent.get("pool"),
            model_id=agent.get("model_name") or model_for_tier(config, agent["model_tier"]),
            task_override=agent.get("route") or None,
        )
        loop = asyncio.get_running_loop()
        agent["result"] = await asyncio.to_thread(
            execute_agent,
            agent["executor"],
            prompt,
            agent["model_tier"],
            config,
            task_id,
            workspace,
            agent.get("reasoning_effort"),
            resolved_route=route,
            role=agent.get("role"),
            pool=agent.get("pool"),
            on_route_switch=lambda event, task_id=task_id, agent_id=agent_id: asyncio.run_coroutine_threadsafe(
                _publish_route_switch(task_id, agent_id, event), loop
            ),
        )
        agent["status"] = "complete"
        agent["step"] = "Returned a structured finding"
        add_agent_conversation(task, agent_id, "assistant", str(agent["result"]))
        add_agent_conversation(task, "lead", "assistant", f"Received findings from {agent['name']}")
    except Exception:
        agent["status"] = "failed"
        agent["step"] = "Executor failed safely"
        agent["result"] = "The executor failed without exposing credentials. Retry after checking its local configuration."
        add_agent_conversation(task, agent_id, "system", "The executor failed safely")
    finally:
        await release_weight(acquired_weight)
    await broadcast(task_id, "agent_complete", f"{agent['name']} returned a result", agent_id)


async def run_swarm(task_id: str) -> None:
    task = TASKS.get(task_id)
    if not task or not task.get("cluster_available") or task.get("cluster_started") is not True:
        return
    config = CONFIG_SNAPSHOTS.get(task_id, CONFIG)
    lead = task["agents"][0]
    task["status"] = "planning"
    task["conversation_state"] = "cluster_running"
    lead["status"] = "working"
    lead["step"] = "Assessing complexity and critical path"
    add_agent_conversation(task, "lead", "assistant", "Review approved; assessing complexity and critical path")
    await broadcast(task_id, "planning", "Coordinator is building a dynamic task graph", lead["id"])
    await asyncio.sleep(0.65)
    if not await active(task_id):
        return

    assessment = task.get("difficulty", {})
    count = max(1, min(4, int(assessment.get("recommended_agents", 4))))
    recommended = task.get("reasoning_recommendation", {}).get("recommended_tier", "medium")
    tiers = _worker_tiers(recommended, count)
    definitions = [
        ("Mira", "Evidence scout", "Collect independent sources and claims", 0.9),
        ("Ivo", "Analyst", "Identify constraints and compare alternatives", 1.2),
        ("Nora", "Verifier", "Check conflicts, assumptions, and gaps", 1.5),
        ("Kai", "Synthesizer", "Prepare an implementation-ready outline", 1.8),
    ][:count]
    task["status"] = "running"
    for index, (name, role, objective, _) in enumerate(definitions, 1):
        tier = tiers[index - 1] if index - 1 < len(tiers) else recommended
        effort = reasoning_for_tier(config, tier, task.get("cluster_reasoning"))
        executor = str(choose_executor(f"{task.get('prompt_context', task['prompt'])} {objective}", task["workspace"]))
        route = resolve_route(config, tier, role=role, model_id=model_for_tier(config, tier), executor=executor)
        agent = {
            "id": f"agent-{index}",
            "name": name,
            "role": role,
            "objective": objective,
            "status": "queued",
            "step": "Waiting for a dispatch slot",
            "parent_id": lead["id"],
            "model_tier": tier,
            "model_name": route.model_id if route else model_for_tier(config, tier),
            "reasoning_effort": effort,
            "configured_reasoning": config.reasoning.get(tier, effort),
            "cluster_reasoning": task.get("cluster_reasoning", config.cluster_reasoning),
            "weight": int(config.tier_weights.get(tier, 1)),
            "executor": route.executor if route else executor,
            "provider_id": route.provider_id if route else (config.provider_name or ""),
            "route": route_public(route),
            "route_version": route.route_version if route else 0,
            "result": None,
            "work_log": [],
        }
        task["agents"].append(agent)
        task.setdefault("agent_conversations", {}).setdefault(agent["id"], [])
        add_dispatch_turns(task, agent["id"], objective)
    await broadcast(task_id, "fan_out", f"Coordinator dispatched {len(definitions)} independent sub-agents", lead["id"])
    await asyncio.gather(
        *(run_worker(task_id, f"agent-{index}", delay) for index, (_, _, _, delay) in enumerate(definitions, 1))
    )
    if task.get("status") == "cancelled":
        return
    lead["step"] = "Reconciling results and drafting final answer"
    add_agent_conversation(task, "lead", "assistant", "Reconciling the independent findings")
    await broadcast(task_id, "synthesis", "Coordinator is merging verified findings", lead["id"])
    await asyncio.sleep(0.6)
    if not await active(task_id):
        return
    lead["status"] = "complete"
    lead["step"] = "Delivered final synthesis"
    task["status"] = "complete"
    completed = [agent for agent in task["agents"][1:] if agent.get("result")]
    task["result"] = synthesis_reply(task.get("prompt_context", task["prompt"]), len(completed))
    add_message(task, "assistant", task["result"], "lead")
    add_agent_conversation(task, "lead", "assistant", task["result"])
    task["conversation_state"] = "complete"
    task["assistant_replied"] = True
    task["last_answered_token"] = int(task.get("reply_token", 0))
    task["completed_at"] = utc_timestamp()
    await broadcast(task_id, "complete", "Task completed and final synthesis is ready", lead["id"])


def _cluster_model_tier(model: str) -> str:
    text = str(model or "").lower()
    if "opus" in text or text.endswith(" sol") or "deepseek" in text:
        return "high"
    if "luna" in text:
        return "low"
    return "medium"


def _mode_agents(runtime: ClusterRuntime, task_id: str, config: RuntimeConfig) -> list[dict[str, Any]]:
    records = runtime.task_agents(task_id)
    lead_index = next((index for index, row in enumerate(records) if row.get("active")), 0)
    if lead_index:
        records = [records[lead_index], *records[:lead_index], *records[lead_index + 1 :]]
    agents: list[dict[str, Any]] = []
    for index, row in enumerate(records):
        lead = index == 0
        agent_id = "lead" if lead else str(row["id"])
        tier = _cluster_model_tier(row["model"])
        role_key = role_route_key(str(row.get("role") or ""))
        route = resolve_route(
            config,
            tier,
            role=f"mode-{runtime.mode}/{role_key}" if role_key else None,
            pool=row.get("pool"),
            model_id=str(row.get("model") or ""),
            task_override={
                "provider_id": row.get("provider_id"),
                "model_id": row.get("model"),
                "executor": row.get("executor"),
            } if row.get("provider_id") else None,
            executor=str(row.get("executor") or "direct_model"),
        )
        agents.append(
            {
                "id": agent_id,
                "name": "Orion" if lead and runtime.mode == 0 else row["name"],
                "role": row["role"],
                "role_key": row.get("role_key"),
                "route_key": row.get("route_key"),
                "objective": row["objective"],
                "status": row["status"] if row["active"] else "inactive",
                "step": "Ready" if row["active"] else "Model unavailable",
                "parent_id": None if lead else "lead",
                "model_tier": tier,
                "model_name": route.model_id if route else row["model"],
                "reasoning_effort": reasoning_for_tier(config, tier),
                "configured_reasoning": config.reasoning.get(tier),
                "weight": int(config.tier_weights.get(tier, 1)),
                "executor": route.executor if route else str(row.get("executor") or "direct_model"),
                "provider_id": route.provider_id if route else (config.provider_name or ""),
                "route": route_public(route),
                "route_version": route.route_version if route else 0,
                "result": None,
                "work_log": [],
                "cluster_agent_id": row["id"],
                "pool": row.get("pool"),
                "active": bool(row["active"]),
                "simulated": bool(row.get("simulated")),
                "heartbeat_at": row.get("heartbeat_at"),
            }
        )
    return agents


def _release_mode_runtime(task_id: str, runtime: ClusterRuntime) -> None:
    TASK_CLUSTERS.pop(task_id, None)
    if runtime is not CLUSTER:
        runtime.stop()


def _mode_context(task_id: str, runtime: ClusterRuntime) -> tuple[str, str, bool, bool]:
    """Refresh the bounded model transcript while retaining durable history."""
    task = TASKS.get(task_id)
    if not task:
        return "", "", False, False
    transcript = list(task.get("model_context") or task.get("conversation", []))
    compacted = runtime.compress_context(transcript, task_id=task_id)
    task["model_context"] = list(compacted.get("messages") or transcript)
    task["context_usage"] = compacted.get("usage")
    if compacted.get("compressed"):
        task["context_summary"] = compacted.get("summary")
    replacement = str((task.get("model_switch") or {}).get("replacement_model") or "")
    switched = bool(compacted.get("model_switched"))
    if switched:
        replacement = str(compacted.get("replacement_model") or replacement)
        task["model_switch"] = {
            "reason": "context_limit",
            "replacement_model": replacement,
            "timestamp": utc_timestamp(),
        }
        task["selected_model"] = replacement
        task["context_model"] = replacement
    elif task.get("selected_model"):
        task["context_model"] = task.get("selected_model")
    return str(task.get("context_summary") or ""), replacement, bool(compacted.get("compressed")), switched


def _mode_event(task: dict[str, Any], kind: str, message: str, agent_id: str | None = None, **payload: Any) -> None:
    add_work_log(task, agent_id, kind, message) if agent_id else task.setdefault("events", []).append(
        {"id": str(uuid4()), "time": utc_timestamp()[11:19], "timestamp": utc_timestamp(), "type": kind, "message": message, "agent_id": None}
    )
    if payload and task.get("events"):
        task["events"][-1].update(payload)
    if agent_id and task.get("events"):
        agent = next((item for item in task.get("agents", []) if item.get("id") == agent_id), None)
        if agent:
            task["events"][-1].setdefault("role", agent.get("role"))
            task["events"][-1].setdefault("source", agent.get("role"))
            task["events"][-1].setdefault("role_key", ROLE_KEYS.get(str(agent.get("role") or ""), ""))


async def _publish_route_switch(task_id: str, agent_id: str, event: dict[str, Any]) -> None:
    task = TASKS.get(task_id)
    if not task:
        return
    target = event.get("to") or {}
    provider = target.get("provider_id") or "unknown provider"
    model = target.get("model_id") or "unknown model"
    _mode_event(
        task,
        "model_switched",
        f"Model route switched to {provider}/{model}",
        agent_id,
        route=target,
        reason=event.get("reason") or "provider_fallback",
    )
    await broadcast(task_id, "model_switched", f"Model route switched to {provider}/{model}", agent_id)


async def run_mode_task(task_id: str) -> None:
    """Run the fixed MODE 1-3 role pool and stream each status transition."""
    runtime = TASK_CLUSTERS.get(task_id, CLUSTER)
    runtime.start()
    task = TASKS.get(task_id)
    if not task or task.get("status") == "cancelled" or not task.get("mode_managed") or int(task.get("mode", 0)) == 0:
        _release_mode_runtime(task_id, runtime)
        return
    config = CONFIG_SNAPSHOTS.get(task_id, CONFIG)
    task["status"] = "planning"
    task["conversation_state"] = "cluster_running"
    task["cluster_started"] = True
    task["cluster_started_at"] = utc_timestamp()
    runtime.begin_work(str(task["agents"][0].get("cluster_agent_id") or "lead"))
    task["agents"][0].update(status="working", step="拆解需求并分发岗位任务")
    _mode_event(task, "planning", f"{task['mode_label']}主管开始拆解任务", "lead", topic="architecture")
    add_agent_conversation(task, "lead", "assistant", f"{task['mode_label']}正在根据岗位池分发任务")
    await broadcast(task_id, "planning", "主管开始拆解并分发任务", "lead")
    if not await active(task_id):
        _release_mode_runtime(task_id, runtime)
        return
    task["status"] = "running"
    workers = [agent for agent in task["agents"][1:] if agent.get("active")]
    _mode_event(task, "fan_out", f"按岗位池激活 {len(workers) + 1} 个槽位", "lead")
    await broadcast(task_id, "fan_out", f"按岗位池激活 {len(workers) + 1} 个槽位", "lead")

    for agent in workers:
        if not await active(task_id):
            _release_mode_runtime(task_id, runtime)
            return
        cluster_agent_id = str(agent.get("cluster_agent_id") or agent["id"])
        runtime.begin_work(cluster_agent_id)
        agent.update(status="working", step="执行岗位任务")
        add_agent_conversation(task, "lead", "assistant", f"已派发 {agent['role']}：{agent['objective']}")
        add_agent_conversation(task, agent["id"], "assistant", f"{agent['role']}开始执行")
        await broadcast(task_id, "agent_progress", f"{agent['role']}开始执行", agent["id"])
        if not await active(task_id):
            runtime.finish_work(cluster_agent_id, success=True)
            _release_mode_runtime(task_id, runtime)
            return
        context_summary, replacement_model, compressed, switched = _mode_context(task_id, runtime)
        if compressed:
            await broadcast(task_id, "context_compressed", "上下文已压缩，继续执行任务", "lead")
            if switched:
                await broadcast(task_id, "model_switched", f"已切换至 {replacement_model}", "lead")
        if not await active(task_id):
            runtime.finish_work(cluster_agent_id, success=True)
            _release_mode_runtime(task_id, runtime)
            return
        prompt = (
            f"Overall task: {task.get('prompt_context', task.get('prompt', ''))}\n"
            f"Role: {agent['role']}\nAssignment: {agent['objective']}\nMode: {task.get('mode_label')}"
            + (f"\nContext summary: {context_summary}" if context_summary else "")
        )
        try:
            selected_model = replacement_model or agent.get("model_name", model_for_tier(config, agent.get("model_tier", "medium")))
            agent_config = replace(config, models={**config.models, agent.get("model_tier", "medium"): selected_model})
            role_key = role_route_key(str(agent.get("role") or ""))
            route = resolve_route(
                agent_config,
                agent.get("model_tier", "medium"),
                role=f"mode-{task.get('mode', 0)}/{role_key}" if role_key else None,
                pool=agent.get("pool"),
                model_id=selected_model,
                task_override=agent.get("route") or None,
            )
            loop = asyncio.get_running_loop()
            result = await asyncio.to_thread(
                execute_agent,
                agent.get("executor", "direct_model"),
                prompt,
                agent.get("model_tier", "medium"),
                agent_config,
                task_id,
                str(ROOT) if task.get("workspace") else None,
                agent.get("reasoning_effort"),
                resolved_route=route,
                role=f"mode-{task.get('mode', 0)}/{role_key}" if role_key else None,
                pool=agent.get("pool"),
                on_route_switch=lambda event, task_id=task_id, agent_id=agent["id"]: asyncio.run_coroutine_threadsafe(
                    _publish_route_switch(task_id, agent_id, event), loop
                ),
            )
            agent.update(status="complete", step="已返回岗位结果", result=str(result))
            runtime.finish_work(cluster_agent_id, success=True)
        except Exception:
            result = "岗位执行失败，已隔离并继续其他岗位。"
            agent.update(status="failed", step="执行失败，已隔离", result=result)
            runtime.finish_work(cluster_agent_id, success=False)
        if not await active(task_id):
            _release_mode_runtime(task_id, runtime)
            return
        add_message(task, "assistant", str(result), agent["id"])
        add_agent_conversation(task, agent["id"], "assistant", str(result))
        task.setdefault("model_context", []).append({"role": "assistant", "content": str(result), "agent_id": agent["id"]})
        event_kind = "agent_failed" if agent.get("status") == "failed" else "agent_complete"
        event_message = f"{agent['role']}执行失败，已隔离" if event_kind == "agent_failed" else f"{agent['role']}已返回结果"
        await broadcast(task_id, event_kind, event_message, agent["id"])

    if not await active(task_id):
        _release_mode_runtime(task_id, runtime)
        return
    prompt_text = str(task.get("prompt_context") or task.get("prompt") or "")
    if int(task.get("mode", 0)) == 3 and any(token in prompt_text.lower() for token in ("争议", "方案", "debate", "dispute", "选择")):
        dispute = runtime.resolve_dispute("任务方案存在分歧", {"方案A": (), "方案B": ()}, task_id=task_id)
        task["dispute"] = dispute
        _mode_event(task, "dispute_resolved", f"辩论与公投完成，胜出：{dispute['winner']}", "lead")
        await broadcast(task_id, "dispute_resolved", f"辩论与公投完成，胜出：{dispute['winner']}", "lead")
        if not await active(task_id):
            _release_mode_runtime(task_id, runtime)
            return
    completed = sum(1 for agent in task["agents"][1:] if agent.get("status") == "complete")
    task["agents"][0].update(status="complete", step="已汇总岗位结果")
    task["status"] = "complete"
    task["result"] = synthesis_reply(prompt_text, completed)
    add_message(task, "assistant", task["result"], "lead")
    add_agent_conversation(task, "lead", "assistant", task["result"])
    task["conversation_state"] = "complete"
    task["assistant_replied"] = True
    task["last_answered_token"] = int(task.get("reply_token", 0))
    task["completed_at"] = utc_timestamp()
    runtime.finish_work(str(task["agents"][0].get("cluster_agent_id") or "lead"), success=True)
    await broadcast(task_id, "complete", "多模式集群已完成并生成汇总", "lead")
    _release_mode_runtime(task_id, runtime)


def _model_dump(value: Any) -> dict[str, Any]:
    return value.model_dump() if hasattr(value, "model_dump") else value.dict()


def build_task(payload: CreateTask, config: RuntimeConfig, attachments: list[dict]) -> dict[str, Any]:
    global CLUSTER
    task_id = str(uuid4())
    prompt = payload.prompt.strip()
    explicit_mode = payload.mode if payload.mode is not None else (payload.cluster_mode if payload.cluster_mode is not None else payload.runtime_mode)
    default_mode = getattr(config, "mode", configured_mode())
    mode = parse_mode(default_mode if explicit_mode is None else explicit_mode)
    profiles = getattr(config, "agent_profiles", None)
    profile_present = isinstance(profiles, dict) and (str(mode) in profiles or mode in profiles)
    mode_managed = explicit_mode is not None or default_mode != 0 or profile_present
    assessment, recommendation = build_reasoning_recommendation(prompt, attachments, payload.workspace, config)
    # The first proposal comes from the task assessment. A runtime value is
    # only the fallback when the assessor has no usable recommendation.
    cluster_reasoning = (payload.cluster_reasoning or recommendation.get("level") or config.cluster_reasoning).strip().lower()
    if cluster_reasoning == "ultra":
        cluster_reasoning = "xhigh"
    if cluster_reasoning not in REASONING_LEVELS:
        cluster_reasoning = config.cluster_reasoning
    cluster_enabled = bool(payload.cluster_enabled) or (mode_managed and mode > 0)
    cluster_available = (
        True
        if mode_managed and mode > 0
        else bool(cluster_enabled and not is_greeting(prompt) and int(assessment.get("recommended_agents", 1)) > 1)
    )
    lead_tier = config.max_tier
    lead_effort = reasoning_for_tier(config, lead_tier, cluster_reasoning)
    lead_model = "claude-opus-5" if mode == 0 else model_for_tier(config, lead_tier)
    lead_route = resolve_route(
        config,
        lead_tier,
        role=f"mode-{mode}/{role_route_key('通用助理')}" if mode == 0 else None,
        model_id=lead_model,
    )
    lead = {
        "id": "lead",
        "name": "Orion",
        "role": "Swarm coordinator",
        "objective": "Clarify, plan, dispatch, and synthesize the task",
        "status": "working",
        "step": "Preparing clarification questions",
        "parent_id": None,
        "model_tier": lead_tier,
        "model_name": lead_route.model_id if lead_route else lead_model,
        "reasoning_effort": lead_effort,
        "weight": int(config.tier_weights.get(lead_tier, 1)),
        "executor": lead_route.executor if lead_route else "direct_model",
        "provider_id": lead_route.provider_id if lead_route else (config.provider_name or ""),
        "route": route_public(lead_route),
        "route_version": lead_route.route_version if lead_route else 0,
        "system_prompt": COORDINATOR_INSTRUCTIONS,
        "result": None,
        "work_log": [],
    }
    created_at_iso = utc_timestamp()
    task = {
        "id": task_id,
        "title": prompt.replace("\n", " ")[:72],
        "prompt": prompt,
        "prompt_context": prompt,
        "attachments": public_attachments(attachments),
        "cluster_enabled": cluster_enabled,
        "cluster_available": cluster_available,
        "cluster_started": False,
        "cluster_reasoning": cluster_reasoning,
        "cluster_reasoning_effort": cluster_reasoning,
        "workspace": bool(payload.workspace),
        "created_at": created_at_iso[11:19],
        "created_at_iso": created_at_iso,
        "source_task_id": None,
        "parent_task_id": None,
        "root_task_id": task_id,
        "continued_task_ids": [],
        "inherited_at": None,
        "inherited_context_summary": None,
        "status": "chatting",
        "conversation_state": "responding",
        "assistant_replied": False,
        "user_turns": 1,
        "task_turns": 0 if is_greeting(prompt) else 1,
        "reply_token": 1,
        "last_answered_token": 0,
        "coordinator_instructions": COORDINATOR_INSTRUCTIONS,
        "workflow_ready": False,
        "workflow_confirmed": False,
        "reasoning_proposed": False,
        "reasoning_approved": False,
        "approval_required": cluster_available,
        "clarifying_questions": list(assessment["clarifying_questions"]),
        "needs_clarification": bool(assessment["clarifying_questions"]),
        "difficulty": assessment,
        "difficulty_assessment": assessment,
        "reasoning_recommendation": recommendation,
        "reasoning_profile": recommendation,
        "review": {"required": cluster_available, "status": "pending", "approved_at": None, "approved_by": None},
        "agents": [lead],
        "agent_conversations": {"lead": []},
        "conversation": [],
        "events": [],
        "result": None,
        "mode": mode,
        "mode_label": MODE_LABELS[mode],
        "mode_managed": mode_managed,
        "cluster_status": {},
    }
    if mode_managed:
        # A task owns its lifecycle and heartbeats. Reusing the display
        # runtime would make simultaneous tasks in the same mode share slots.
        runtime = ClusterRuntime(mode, config=config, simulation=config.simulation, autostart=True) if mode > 0 else CLUSTER
        if mode > 0:
            TASK_CLUSTERS[task_id] = runtime
        task["agents"] = _mode_agents(runtime, task_id, config)
        task["agent_conversations"] = {str(agent["id"]): [] for agent in task["agents"]}
        task["cluster_status"] = runtime.status()
        if task["agents"]:
            lead = task["agents"][0]
        else:
            task["unconfigured_mode"] = True
            lead.update(
                name="Orbit System",
                role="System / Main agent",
                status="inactive",
                step="No roles configured",
                active=False,
            )
            task["agents"] = [lead]
            task["agent_conversations"] = {"lead": []}
            task["cluster_available"] = False
            task["approval_required"] = False
    add_message(task, "user", prompt)
    add_agent_conversation(task, "lead", "user", prompt)
    timestamp = utc_timestamp()
    task["events"].append({"id": str(uuid4()), "time": timestamp[11:19], "timestamp": timestamp, "type": "user_message", "message": "You sent a message to Orion", "agent_id": "lead"})
    lead["work_log"].append({"time": task["events"][-1]["time"], "timestamp": timestamp, "type": "user_message", "message": "You sent a message to Orion"})
    return task


def build_continuation_task(source: dict[str, Any], content: str, config: RuntimeConfig) -> tuple[dict[str, Any], list[dict], bool]:
    context = continuation_context(source)
    inherited_summary = continuation_context_text(context)
    inherited_at = utc_timestamp()
    attachments = [
        {
            "name": str(item.get("name") or "attachment"),
            "type": str(item.get("type") or "application/octet-stream"),
            "size": int(item.get("size") or 0),
            "content": "",
        }
        for item in source.get("attachments", [])
        if isinstance(item, dict)
    ]
    initial_prompt = content or str(source.get("prompt") or source.get("title") or "Continue discussion")
    task = build_task(
        CreateTask(
            prompt=initial_prompt,
            cluster_enabled=False,
            workspace=bool(source.get("workspace")),
            attachments=[],
        ),
        config,
        attachments,
    )
    task.update(
        source_task_id=source["id"],
        parent_task_id=source["id"],
        root_task_id=source.get("root_task_id") or source["id"],
        source_task_title=source.get("title"),
        inherited_at=inherited_at,
        inherited_context=context,
        inherited_context_summary=inherited_summary,
        inherited_attachments=attachment_metadata(source.get("attachments")),
        continued_task_ids=[],
        cluster_started=False,
        workflow_ready=False,
        workflow_confirmed=False,
        reasoning_proposed=False,
        reasoning_approved=False,
    )
    task["attachments"] = attachment_metadata(source.get("attachments"))
    task["reasoning_recommendation"]["approved"] = False
    task["reasoning_profile"] = task["reasoning_recommendation"]
    task["review"].update(status="pending", approved_at=None, approved_by=None)

    if content:
        task["prompt_context"] = inherited_summary + "\n\nContinuation request:\n" + content
        return task, attachments, True

    task.update(
        title=("继续：" if any("\u4e00" <= char <= "\u9fff" for char in str(source.get("title") or "")) else "Continue: ") + str(source.get("title") or "")[:64],
        prompt="",
        prompt_context=inherited_summary,
        cluster_available=False,
        approval_required=False,
        status="chatting",
        conversation_state="chatting",
        assistant_replied=True,
        user_turns=0,
        task_turns=0,
        reply_token=0,
        last_answered_token=0,
        needs_clarification=True,
    )
    task["conversation"] = []
    task["events"] = []
    task["agent_conversations"] = {"lead": []}
    task["agents"][0].update(status="ready", step="Waiting for your next instruction", result=None, work_log=[])
    welcome = continuation_welcome(source)
    add_message(task, "assistant", welcome, "lead")
    add_agent_conversation(task, "lead", "assistant", welcome)
    timestamp = utc_timestamp()
    task["events"].append(
        {
            "id": str(uuid4()),
            "time": timestamp[11:19],
            "timestamp": timestamp,
            "type": "continuation_created",
            "message": "A new discussion task inherited context from the completed source task",
            "agent_id": "lead",
        }
    )
    task["agents"][0]["work_log"].append(
        {"time": timestamp[11:19], "timestamp": timestamp, "type": "continuation_created", "message": task["events"][-1]["message"]}
    )
    return task, attachments, False


@app.get("/api/system")
async def system_info() -> dict[str, Any]:
    return system_payload()


@app.get("/api/config")
async def get_config() -> dict[str, Any]:
    return system_payload()


def _routing_response() -> dict[str, Any]:
    providers = provider_catalog(CONFIG)
    routes = route_catalog(CONFIG)
    return {**providers, "providers": providers.get("providers", []), "routes": routes.get("routes", []), "route_catalog": routes}


def _commit_routing_config(candidate: RuntimeConfig) -> None:
    """Commit new provider metadata without changing in-flight snapshots."""
    global CONFIG, CLUSTER
    replacement = ClusterRuntime(
        getattr(candidate, "mode", 0),
        config=candidate,
        simulation=candidate.simulation,
        autostart=True,
    )
    previous, previous_cluster = CONFIG, CLUSTER
    CONFIG = candidate
    if not persist_state():
        CONFIG = previous
        replacement.stop()
        raise _persistence_failure()
    CLUSTER = replacement
    previous_cluster.stop()


def _complete_profile_command_task(task: dict[str, Any], confirmation: str) -> None:
    """Turn a chat-box configuration command into a single system response."""
    runtime = TASK_CLUSTERS.pop(task["id"], None)
    if runtime:
        runtime.stop()
    task["cluster_started"] = False
    task["cluster_available"] = False
    task["approval_required"] = False
    task["workflow_ready"] = False
    task["workflow_confirmed"] = False
    task["reasoning_proposed"] = False
    task["reasoning_approved"] = False
    task["status"] = "complete"
    task["conversation_state"] = "complete"
    task["assistant_replied"] = True
    task["result"] = confirmation
    task["configuration_updated"] = True
    task["configuration_confirmation"] = confirmation
    if task.get("agents"):
        task["agents"][0].update(
            name="Orbit System",
            role="System / Main agent",
            status="ready",
            step="Configuration updated",
            result=confirmation,
        )
    add_message(task, "assistant", confirmation, "lead")
    add_agent_conversation(task, "lead", "assistant", confirmation)
    add_work_log(task, "lead", "configuration_updated", confirmation)


def _complete_unconfigured_task(task: dict[str, Any]) -> None:
    """Return a clear main-agent response when a mode has zero role slots."""
    message = f"{task.get('mode_label', '当前模式')}尚未配置岗位。请在输入框中添加岗位，或在设置面板保存岗位清单后再提交任务。"
    task["status"] = "complete"
    task["conversation_state"] = "blocked"
    task["assistant_replied"] = True
    task["cluster_available"] = False
    task["approval_required"] = False
    task["result"] = message
    task["blocked_reason"] = "no_configured_roles"
    if task.get("agents"):
        task["agents"][0].update(status="inactive", step="No roles configured", result=message)
    add_message(task, "assistant", message, "lead")
    add_agent_conversation(task, "lead", "assistant", message)
    add_work_log(task, "lead", "blocked", message)


@app.get("/api/providers")
async def get_providers() -> dict[str, Any]:
    return provider_catalog(CONFIG)


@app.get("/api/providers/{provider_id}")
async def get_provider(provider_id: str) -> dict[str, Any]:
    provider = next((item for item in provider_catalog(CONFIG).get("providers", []) if item.get("id") == provider_id.lower()), None)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    return provider


@app.post("/api/providers")
async def create_provider(payload: dict[str, Any]) -> dict[str, Any]:
    provider_id = str(payload.get("id") or payload.get("provider_id") or payload.get("name") or "").strip()
    if not provider_id:
        raise HTTPException(status_code=422, detail="provider id is required")
    try:
        candidate = update_provider(CONFIG, provider_id, payload)
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    _commit_routing_config(candidate)
    return _routing_response()


@app.put("/api/providers/{provider_id}")
async def put_provider(provider_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        candidate = update_provider(CONFIG, provider_id, payload)
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    _commit_routing_config(candidate)
    return _routing_response()


@app.delete("/api/providers/{provider_id}")
async def delete_provider(provider_id: str) -> dict[str, Any]:
    try:
        candidate = disable_provider(CONFIG, provider_id)
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    _commit_routing_config(candidate)
    return _routing_response()


@app.post("/api/providers/{provider_id}/test")
async def check_provider(provider_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return provider_test(CONFIG, provider_id, payload or {})


@app.put("/api/providers/{provider_id}/test")
async def check_provider_put(provider_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return provider_test(CONFIG, provider_id, payload or {})


@app.get("/api/routes")
async def get_routes() -> dict[str, Any]:
    return route_catalog(CONFIG)


@app.post("/api/routes")
async def create_route(payload: dict[str, Any]) -> dict[str, Any]:
    scope = str(payload.get("scope") or "tier")
    key = str(payload.get("key") or payload.get("role") or payload.get("tier") or "default")
    try:
        candidate = update_route(CONFIG, scope, key, payload)
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    _commit_routing_config(candidate)
    return _routing_response()


@app.put("/api/routes/roles/{mode}/{role_key}")
async def put_role_route(mode: int, role_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        candidate = update_route(CONFIG, "role", f"mode-{parse_mode(mode)}/{role_key}", payload)
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    _commit_routing_config(candidate)
    return _routing_response()


@app.delete("/api/routes/roles/{mode}/{role_key}")
async def delete_role_route(mode: int, role_key: str) -> dict[str, Any]:
    candidate = remove_route(CONFIG, "role", f"mode-{parse_mode(mode)}/{role_key}")
    _commit_routing_config(candidate)
    return _routing_response()


@app.put("/api/routes/tiers/{tier}")
async def put_tier_route(tier: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        candidate = update_route(CONFIG, "tier", tier, payload)
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    _commit_routing_config(candidate)
    return _routing_response()


@app.delete("/api/routes/tiers/{tier}")
async def delete_tier_route(tier: str) -> dict[str, Any]:
    candidate = remove_route(CONFIG, "tier", tier)
    _commit_routing_config(candidate)
    return _routing_response()


@app.put("/api/routes/pools/{pool_key}")
async def put_pool_route(pool_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        candidate = update_route(CONFIG, "pool", pool_key, payload)
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    _commit_routing_config(candidate)
    return _routing_response()


@app.delete("/api/routes/pools/{pool_key}")
async def delete_pool_route(pool_key: str) -> dict[str, Any]:
    candidate = remove_route(CONFIG, "pool", pool_key)
    _commit_routing_config(candidate)
    return _routing_response()


@app.put("/api/routes/{scope}/{key}")
async def put_generic_route(scope: str, key: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        candidate = update_route(CONFIG, scope, key, payload)
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    _commit_routing_config(candidate)
    return _routing_response()


@app.delete("/api/routes/{scope}/{key}")
async def delete_generic_route(scope: str, key: str) -> dict[str, Any]:
    candidate = remove_route(CONFIG, scope, key)
    _commit_routing_config(candidate)
    return _routing_response()


@app.get("/api/cluster")
@app.get("/api/cluster/status")
async def cluster_status() -> dict[str, Any]:
    return CLUSTER.status()


def _search_logs(keyword: str = "", role: str = "", task_id: str | None = None, limit: int = 100, from_time: str | None = None, to_time: str | None = None) -> list[dict[str, Any]]:
    keyword = str(keyword or "").strip().lower()
    role = str(role or "").strip().lower()
    limit = max(1, min(500, int(limit or 100)))
    rows = CLUSTER.logs_search(keyword, role, from_time, to_time, task_id, limit)
    for current_id, task in TASKS.items():
        if task_id and current_id != task_id:
            continue
        for event in task.get("events", []):
            item = dict(event)
            item.setdefault("task_id", current_id)
            stamp = str(item.get("timestamp") or "")
            if from_time and stamp < from_time:
                continue
            if to_time and stamp > to_time:
                continue
            haystack = " ".join(str(item.get(key) or "") for key in ("message", "type", "agent_id", "role", "role_key", "source")).lower()
            if keyword and keyword not in haystack:
                continue
            if role and role not in haystack:
                continue
            rows.append(item)
        agents_by_id = {str(agent.get("id")): agent for agent in task.get("agents", []) if isinstance(agent, dict)}
        conversations = list(task.get("conversation", []))
        for agent_id, entries in (task.get("agent_conversations", {}) or {}).items():
            for entry in entries if isinstance(entries, list) else []:
                if isinstance(entry, dict):
                    conversations.append({**entry, "agent_id": entry.get("agent_id") or agent_id, "internal": True})
        for entry in conversations:
            if not isinstance(entry, dict):
                continue
            agent_id = str(entry.get("agent_id") or "")
            owner = agents_by_id.get(agent_id, {})
            item = {
                "id": entry.get("id") or str(uuid4()),
                "task_id": current_id,
                "type": "agent_conversation" if entry.get("internal") else "conversation",
                "message": str(entry.get("content") or entry.get("message") or ""),
                "content": str(entry.get("content") or entry.get("message") or ""),
                "agent_id": agent_id or None,
                "role": owner.get("role") or entry.get("role"),
                "role_key": ROLE_KEYS.get(str(owner.get("role") or ""), ""),
                "source": owner.get("role") or entry.get("speaker") or entry.get("role"),
                "timestamp": entry.get("timestamp") or entry.get("time"),
                "token_estimate": entry.get("token_estimate"),
                "token_usage": entry.get("token_usage"),
            }
            stamp = str(item.get("timestamp") or "")
            if from_time and stamp < from_time:
                continue
            if to_time and stamp > to_time:
                continue
            haystack = " ".join(str(item.get(key) or "") for key in ("message", "content", "agent_id", "role", "role_key", "source")).lower()
            if keyword and keyword not in haystack:
                continue
            if role and role not in haystack:
                continue
            rows.append(item)
    rows.sort(key=lambda item: str(item.get("timestamp") or ""), reverse=True)
    return rows[:limit]


@app.get("/api/logs")
@app.get("/api/logs/search")
@app.get("/api/search/logs")
@app.get("/api/tasks/search")
async def search_logs(request: Request, keyword: str = "", q: str = "", role: str = "", task_id: str | None = None, from_time: str | None = None, to_time: str | None = None, limit: int = 100) -> dict[str, Any]:
    # Runtime search handles time windows; task timelines are merged above for
    # compatibility with the durable v1 state document.
    keyword = q or keyword
    from_time = request.query_params.get("from") or from_time
    to_time = request.query_params.get("to") or to_time
    items = _search_logs(keyword, role, task_id, limit, from_time, to_time)
    if from_time:
        items = [item for item in items if str(item.get("timestamp") or "") >= from_time]
    if to_time:
        items = [item for item in items if str(item.get("timestamp") or "") <= to_time]
    return {"items": items[: max(1, min(500, int(limit or 100)))], "keyword": keyword, "role": role, "task_id": task_id}


@app.post("/api/config")
async def update_config(payload: dict[str, Any]) -> dict[str, Any]:
    global CONFIG, CLUSTER
    try:
        candidate = updated_config(CONFIG, payload)
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    previous, previous_cluster = CONFIG, CLUSTER
    replacement = None
    CONFIG = candidate
    if any(key in payload for key in ("mode", "cluster_mode", "runtime_mode", "providers", "routes", "default_provider_id", "provider_registry", "agent_profiles", "mode_roles", "roles_by_mode", "roles")):
        replacement = ClusterRuntime(getattr(candidate, "mode", 0), config=candidate, simulation=candidate.simulation, autostart=True)
    if not persist_state():
        CONFIG = previous
        if replacement:
            replacement.stop()
        raise _persistence_failure()
    if replacement:
        CLUSTER = replacement
        previous_cluster.stop()
    async with WEIGHT_CONDITION:
        WEIGHT_CONDITION.notify_all()
    return system_payload()


@app.get("/api/agent-profiles")
async def get_agent_profiles() -> dict[str, Any]:
    """Editable role composition plus the existing selectable role directory."""
    return {
        "agent_profiles": getattr(CONFIG, "agent_profiles", None) or {},
        "mode_roles": getattr(CONFIG, "agent_profiles", None) or {},
        "role_catalog": role_catalog(),
        "executors": ["direct_model", "openclaw", "codex", "claude_code"],
    }


@app.put("/api/agent-profiles")
@app.post("/api/agent-profiles")
async def put_agent_profiles(payload: dict[str, Any]) -> dict[str, Any]:
    """Persist a role composition and atomically rebuild the visible cluster."""
    global CONFIG, CLUSTER
    try:
        candidate = updated_config(CONFIG, payload)
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    replacement = ClusterRuntime(candidate.mode, config=candidate, simulation=candidate.simulation, autostart=True)
    previous, previous_cluster = CONFIG, CLUSTER
    CONFIG = candidate
    if not persist_state():
        CONFIG = previous
        replacement.stop()
        raise _persistence_failure()
    CLUSTER = replacement
    previous_cluster.stop()
    return await get_agent_profiles()


@app.post("/api/agent-profiles/command")
async def agent_profile_command(payload: dict[str, Any]) -> dict[str, Any]:
    """Command contract for a main agent translating natural language edits.

    Supported commands: ``set_mode_roles``/``replace_mode_roles`` with
    ``mode`` and ``roles``.  The main agent may use this endpoint after it has
    resolved a user's natural-language instruction into catalog role names.
    """
    command = str(payload.get("command") or "").strip().lower()
    if command not in {"set_mode_roles", "replace_mode_roles", "configure_mode"}:
        raise HTTPException(status_code=422, detail="command must be set_mode_roles, replace_mode_roles, or configure_mode")
    if "roles" not in payload:
        raise HTTPException(status_code=422, detail="roles is required")
    return await put_agent_profiles({"profile_mode": payload.get("mode", CONFIG.mode), "roles": payload.get("roles")})


@app.post("/api/cluster/mode")
@app.post("/api/mode")
async def update_cluster_mode(payload: dict[str, Any]) -> dict[str, Any]:
    global CONFIG, CLUSTER
    if "mode" not in payload and "cluster_mode" not in payload and "runtime_mode" not in payload:
        raise HTTPException(status_code=422, detail="mode is required")
    mode = parse_mode(payload.get("mode", payload.get("cluster_mode", payload.get("runtime_mode"))))
    candidate = updated_config(CONFIG, {"mode": mode})
    replacement = ClusterRuntime(mode, config=candidate, simulation=candidate.simulation, autostart=True)
    previous_config, previous_cluster = CONFIG, CLUSTER
    CONFIG = candidate
    if not persist_state():
        CONFIG = previous_config
        replacement.stop()
        raise _persistence_failure()
    CLUSTER = replacement
    previous_cluster.stop()
    return system_payload()


@app.get("/api/tasks")
async def list_tasks() -> list[dict[str, Any]]:
    return [public(task) for task in reversed(list(TASKS.values()))]


@app.post("/api/tasks", status_code=201)
async def create_task(payload: CreateTask) -> dict[str, Any]:
    command = parse_agent_profile_command(payload.prompt, CONFIG)
    if command:
        try:
            _commit_routing_config(updated_config(CONFIG, command.payload))
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        attachments = normalize_attachments([_model_dump(item) for item in payload.attachments])
        task = build_task(payload, CONFIG, attachments)
        _complete_profile_command_task(task, command.confirmation)
        TASKS[task["id"]] = task
        CONFIG_SNAPSHOTS[task["id"]] = CONFIG
        TASK_ATTACHMENTS[task["id"]] = attachments
        SUBSCRIBERS[task["id"]] = set()
        if not persist_state():
            TASKS.pop(task["id"], None)
            CONFIG_SNAPSHOTS.pop(task["id"], None)
            TASK_ATTACHMENTS.pop(task["id"], None)
            SUBSCRIBERS.pop(task["id"], None)
            raise _persistence_failure()
        return public(task)
    config = CONFIG
    attachments = normalize_attachments([_model_dump(item) for item in payload.attachments])
    task = build_task(payload, config, attachments)
    TASKS[task["id"]] = task
    CONFIG_SNAPSHOTS[task["id"]] = config
    TASK_ATTACHMENTS[task["id"]] = attachments
    SUBSCRIBERS[task["id"]] = set()
    if task.get("unconfigured_mode"):
        _complete_unconfigured_task(task)
    if not persist_state():
        TASKS.pop(task["id"], None)
        CONFIG_SNAPSHOTS.pop(task["id"], None)
        TASK_ATTACHMENTS.pop(task["id"], None)
        TASK_CLUSTERS.pop(task["id"], None)
        SUBSCRIBERS.pop(task["id"], None)
        raise _persistence_failure()
    if task.get("unconfigured_mode"):
        pass
    elif task.get("mode_managed") and int(task.get("mode", 0)) > 0:
        asyncio.create_task(run_mode_task(task["id"]))
    else:
        asyncio.create_task(answer_message(task["id"], task["prompt"], task["reply_token"]))
    return public(task)


@app.post("/api/tasks/{task_id}/messages", status_code=201)
async def add_task_message(task_id: str, payload: MessageTask) -> dict[str, Any]:
    if task_id not in TASKS:
        raise HTTPException(status_code=404, detail="Task not found")
    content = payload.content.strip()
    command = parse_agent_profile_command(content, CONFIG)
    if command:
        try:
            _commit_routing_config(updated_config(CONFIG, command.payload))
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        task = TASKS[task_id]
        original_task = deepcopy(task)
        add_message(task, "user", content)
        task.setdefault("agent_conversations", {}).setdefault("lead", [])
        add_agent_conversation(task, "lead", "user", content)
        add_message(task, "assistant", command.confirmation, "lead")
        add_agent_conversation(task, "lead", "assistant", command.confirmation)
        task["configuration_updated"] = True
        task["configuration_confirmation"] = command.confirmation
        if not await broadcast(
            task_id,
            "configuration_updated",
            command.confirmation,
            "lead",
            rollback_task=original_task,
        ):
            raise _persistence_failure()
        return public(task)
    task = TASKS[task_id]
    if task["status"] in {"cancelled", "complete"} or task.get("cluster_started"):
        raise HTTPException(status_code=409, detail="Task is no longer accepting messages")
    original_task = deepcopy(task)
    if payload.cluster_enabled is not None:
        task["cluster_enabled"] = payload.cluster_enabled
    add_message(task, "user", content)
    task.setdefault("agent_conversations", {}).setdefault("lead", [])
    task_turns = int(task.get("task_turns", 0 if is_greeting(task.get("prompt", "")) else 1))
    if task_turns == 0 and not is_greeting(content):
        task["prompt"] = content
        inherited = str(task.get("inherited_context_summary") or "").strip()
        task["prompt_context"] = (inherited + "\n\nContinuation request:\n" + content).strip() if inherited else content
        task["title"] = content.replace("\n", " ")[:72]
    else:
        task["prompt_context"] = (task.get("prompt_context") or task.get("prompt", "")) + "\n" + content
    task["user_turns"] = int(task.get("user_turns", 1)) + 1
    task["task_turns"] = task_turns
    if not is_greeting(content):
        task["task_turns"] += 1
    task["reply_token"] = int(task.get("reply_token", 0)) + 1
    task["status"] = "chatting"
    task["conversation_state"] = "chatting"
    task["agents"][0].update(status="working", step="Reading your message")
    # Any follow-up changes the proposal and therefore requires fresh review.
    task["workflow_ready"] = False
    task["workflow_confirmed"] = False
    task["reasoning_proposed"] = False
    task["reasoning_approved"] = False
    task["reasoning_recommendation"]["approved"] = False
    if isinstance(task["reasoning_recommendation"].get("estimate"), dict):
        task["reasoning_recommendation"]["estimate"]["stale"] = True
    task["reasoning_profile"] = task["reasoning_recommendation"]
    task["review"].update(status="pending", approved_at=None, approved_by=None)
    add_agent_conversation(task, "lead", "user", content)
    if not await broadcast(
        task_id,
        "user_message",
        "You sent a message to Orion",
        "lead",
        rollback_task=original_task,
    ):
        raise _persistence_failure()
    if task.get("mode_managed") and int(task.get("mode", 0)) > 0:
        asyncio.create_task(run_mode_task(task_id))
    else:
        asyncio.create_task(answer_message(task_id, content, task["reply_token"]))
    return public(task)


@app.post("/api/tasks/{task_id}/continue", status_code=201)
async def continue_task(task_id: str, payload: ContinueTask) -> dict[str, Any]:
    source = TASKS.get(task_id)
    if not source:
        raise HTTPException(status_code=404, detail="Task not found")
    if source.get("status") != "complete":
        raise HTTPException(status_code=409, detail="Only completed tasks can be continued")
    original_source = deepcopy(source)
    content = payload.content.strip()
    config = CONFIG
    task, attachments, start_reply = build_continuation_task(source, content, config)
    TASKS[task["id"]] = task
    CONFIG_SNAPSHOTS[task["id"]] = config
    TASK_ATTACHMENTS[task["id"]] = attachments
    SUBSCRIBERS[task["id"]] = set()
    source.setdefault("continued_task_ids", []).append(task["id"])
    timestamp = utc_timestamp()
    source.setdefault("events", []).append(
        {
            "id": str(uuid4()),
            "time": timestamp[11:19],
            "timestamp": timestamp,
            "type": "continued",
            "message": f"Discussion continued in task {task['id']}",
            "agent_id": None,
        }
    )
    if not persist_state():
        TASKS.pop(task["id"], None)
        CONFIG_SNAPSHOTS.pop(task["id"], None)
        TASK_ATTACHMENTS.pop(task["id"], None)
        TASK_CLUSTERS.pop(task["id"], None)
        SUBSCRIBERS.pop(task["id"], None)
        source.clear()
        source.update(original_source)
        TASKS[task_id] = source
        raise _persistence_failure()
    if start_reply:
        if task.get("mode_managed") and int(task.get("mode", 0)) > 0:
            asyncio.create_task(run_mode_task(task["id"]))
        else:
            asyncio.create_task(answer_message(task["id"], content, task["reply_token"]))
    return public(task)


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str) -> dict[str, Any]:
    if task_id not in TASKS:
        raise HTTPException(status_code=404, detail="Task not found")
    return public(TASKS[task_id])


@app.get("/api/tasks/{task_id}/export")
async def export_task(task_id: str, format: str = "json") -> Response:
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    export_format = format.strip().lower()
    if export_format not in {"json", "markdown", "md"}:
        raise HTTPException(status_code=422, detail="format must be json or markdown")
    snapshot = CONFIG_SNAPSHOTS.get(task_id, CONFIG)
    secrets = secret_values(ENV_CONFIG) + secret_values(CONFIG) + secret_values(snapshot)
    payload = build_export_payload(
        task,
        persistable_config(snapshot),
        secrets,
    )
    if export_format == "json":
        body = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        filename = safe_export_filename(task, "json", secrets)
        media_type = "application/json"
    else:
        body = render_markdown(payload).encode("utf-8")
        filename = safe_export_filename(task, "md", secrets)
        media_type = "text/markdown"
    return Response(content=body, media_type=media_type, headers={"Content-Disposition": content_disposition(filename)})


@app.get("/api/tasks/{task_id}/messages")
async def get_task_messages(task_id: str) -> dict[str, Any]:
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return {
        "conversation": list(task.get("conversation", [])),
        "agent_conversations": task.get("agent_conversations", {}),
    }


@app.post("/api/tasks/{task_id}/control")
async def control_task(task_id: str, payload: ControlTask) -> dict[str, Any]:
    global CONFIG_SNAPSHOTS
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    action = payload.action
    original_task = deepcopy(task)
    original_snapshot = CONFIG_SNAPSHOTS.get(task_id)
    launch_cluster = False
    cancel_runtime: ClusterRuntime | None = None
    cancel_agent_id = "lead"

    if action == "pause" and task["status"] in {"planning", "running"}:
        task["status"] = "paused"
    elif action == "resume" and task["status"] == "paused":
        task["status"] = "running"
    elif action == "cancel" and task["status"] not in {"complete", "cancelled"}:
        task["status"] = "cancelled"
        task["conversation_state"] = "cancelled"
        task["agents"][0].update(status="cancelled", step="任务已取消")
        cancel_runtime = TASK_CLUSTERS.get(task_id)
        cancel_agent_id = str(task["agents"][0].get("cluster_agent_id") or "lead")
        if cancel_runtime:
            _mode_event(task, "cancelled", "任务已取消，正在停止岗位运行时", "lead")
    elif action == "confirm_workflow":
        if (
            task.get("cluster_started")
            or not task.get("cluster_available")
            or not task.get("assistant_replied")
            or task.get("task_turns", 1) < 2
            or not task.get("workflow_ready")
            or task.get("needs_clarification")
            or task.get("last_answered_token") != task.get("reply_token")
        ):
            raise HTTPException(status_code=409, detail="Reply to the coordinator and wait for its updated workflow before confirming")
        task["workflow_confirmed"] = True
        task["workflow_confirmed_at"] = utc_timestamp()
        task["needs_clarification"] = False
        task["review"]["status"] = "workflow_confirmed"
        task["status"] = "awaiting_confirmation"
        task["conversation_state"] = "awaiting_confirmation"
        add_agent_conversation(task, "lead", "system", "User confirmed the proposed workflow")
        task["reasoning_proposed"] = True
        proposal = reasoning_proposal_reply(task.get("prompt_context", task["prompt"]), task["reasoning_recommendation"])
        task["workflow_summary"] = next(
            (message.get("content", "") for message in reversed(task.get("conversation", [])) if message.get("role") == "assistant"),
            "",
        )
        add_message(task, "assistant", proposal, "lead")
        add_agent_conversation(task, "lead", "assistant", proposal)
        task["agents"][0].update(status="ready", step="Waiting for reasoning approval")
    elif action == "approve_reasoning":
        if (
            task.get("cluster_started")
            or not task.get("cluster_available")
            or not task.get("workflow_confirmed")
            or not task.get("reasoning_proposed")
            or task.get("last_answered_token") != task.get("reply_token")
        ):
            raise HTTPException(status_code=409, detail="Confirm the updated workflow before approving reasoning")
        level = (payload.level or payload.reasoning_level or task.get("reasoning_recommendation", {}).get("level", "high")).strip().lower()
        # ``ultra`` is accepted as a friendly alias for the xhigh effort.
        if level == "ultra":
            level = "xhigh"
        if level not in REASONING_LEVELS:
            raise HTTPException(status_code=422, detail="level must be minimal, low, medium, high, or xhigh")
        recommendation = task["reasoning_recommendation"]
        recommendation.update(
            level=level,
            total_reasoning=level,
            reasoning_effort=level,
            approved=True,
            approved_at=utc_timestamp(),
        )
        task["reasoning_profile"] = task["reasoning_recommendation"]
        task["cluster_reasoning"] = level
        task["cluster_reasoning_effort"] = level
        task["agents"][0]["reasoning_effort"] = level
        task["reasoning_approved"] = True
        task["reasoning_approved_at"] = recommendation["approved_at"]
        task["review"].update(status="reasoning_approved", approved_level=level, approved_by="user")
        task["status"] = "awaiting_confirmation"
        task["conversation_state"] = "awaiting_confirmation"
        add_agent_conversation(task, "lead", "system", f"User approved {level} cluster reasoning")
    elif action == "start_cluster":
        if not _prepare_cluster_start(task):
            raise HTTPException(status_code=409, detail=_approval_error(task))
        add_work_log(task, "lead", "cluster_started", "The approved workflow is starting the agent cluster")
        launch_cluster = True
    elif action == "retry":
        if task["status"] not in {"cancelled", "interrupted"}:
            raise HTTPException(status_code=409, detail="Only cancelled or interrupted tasks can be retried; completed tasks remain read-only")
        if (
            not task.get("cluster_available")
            or task.get("task_turns", 1) < 2
            or not task.get("workflow_ready")
            or not task.get("workflow_confirmed")
            or not task.get("reasoning_proposed")
            or not task.get("reasoning_approved")
            or not task.get("reasoning_recommendation", {}).get("approved")
        ):
            raise HTTPException(status_code=409, detail=_approval_error(task))
        task.setdefault("prior_attempts", []).append(
            {
                "archived_at": utc_timestamp(),
                "status": task.get("status"),
                "result": task.get("result"),
                "agents": deepcopy(task.get("agents", [])),
                "agent_conversations": deepcopy(task.get("agent_conversations", {})),
                "interruption_reason": task.get("interruption_reason"),
            }
        )
        lead = deepcopy(task["agents"][0])
        task["status"] = "queued"
        task["result"] = None
        task["agents"] = [lead]
        task["agent_conversations"] = {"lead": []}
        CONFIG_SNAPSHOTS[task_id] = CONFIG
        task["cluster_started"] = False
        task["conversation_state"] = "cluster_starting"
        task["review"]["status"] = "approved"
        task.pop("interrupted_at", None)
        task.pop("interruption_reason", None)
        task.pop("retryable", None)
        task["agents"][0].update(
            status="queued",
            step="Awaiting task launch",
            model_name=model_for_tier(CONFIG, CONFIG.max_tier),
            reasoning_effort=reasoning_for_tier(CONFIG, CONFIG.max_tier, task.get("cluster_reasoning")),
            weight=int(CONFIG.tier_weights.get(CONFIG.max_tier, 1)),
            result=None,
            work_log=[],
        )
        if not _prepare_cluster_start(task):
            raise HTTPException(status_code=409, detail=_approval_error(task))
        add_work_log(task, "lead", "cluster_started", "The approved workflow is starting the agent cluster")
        launch_cluster = True
    else:
        raise HTTPException(status_code=422, detail="Unsupported task action")

    if not await broadcast(
        task_id,
        "control",
        f"User action: {action}",
        rollback_task=original_task,
    ):
        if task_id not in TASKS:
            TASKS[task_id] = original_task
        if original_snapshot is None:
            CONFIG_SNAPSHOTS.pop(task_id, None)
        else:
            CONFIG_SNAPSHOTS[task_id] = original_snapshot
        raise _persistence_failure()
    if cancel_runtime:
        # Cancellation is an orderly stop; do not turn it into an HR model
        # error before releasing the task-owned runtime.
        cancel_runtime.finish_work(cancel_agent_id, success=True)
        cancel_runtime.emit_status("cancelled", "任务已取消，集群运行时已停止", task_id=task_id, agent_id=cancel_agent_id)
        _release_mode_runtime(task_id, cancel_runtime)
    if launch_cluster:
        asyncio.create_task(run_swarm(task_id))
    return public(TASKS[task_id])


@app.websocket("/ws/tasks/{task_id}")
async def task_stream(socket: WebSocket, task_id: str) -> None:
    if task_id not in TASKS:
        await socket.close(code=4404)
        return
    await socket.accept()
    SUBSCRIBERS.setdefault(task_id, set()).add(socket)
    await socket.send_json({"type": "snapshot", "task": public(TASKS[task_id])})
    try:
        while True:
            await socket.receive_text()
    except WebSocketDisconnect:
        SUBSCRIBERS.setdefault(task_id, set()).discard(socket)


app.mount("/", StaticFiles(directory=ROOT / "frontend", html=True), name="frontend")
