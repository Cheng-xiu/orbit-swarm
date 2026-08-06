"""Zero-dependency HTTP entry point for Orbit Swarm.

Its REST contract intentionally mirrors :mod:`main`, including the
coordinator review and reasoning-approval gates.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import Condition, Lock, Thread
import time
from urllib.parse import parse_qs, unquote, urlparse
from uuid import uuid4

from executors import (
    MODEL_TIERS,
    REASONING_LEVELS,
    RuntimeConfig,
    build_reasoning_recommendation,
    attachment_prompt,
    available_executors,
    COORDINATOR_INSTRUCTIONS,
    choose_executor,
    coordinator_reply as shared_coordinator_reply,
    execute_agent,
    is_greeting as shared_is_greeting,
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
    model_available,
    parse_mode,
    role_catalog,
    role_route_key,
)
from agent_profile_commands import parse_agent_profile_command

ROOT = Path(__file__).resolve().parent.parent
TASKS: dict[str, dict] = {}
CONFIG_SNAPSHOTS: dict[str, RuntimeConfig] = {}
TASK_ATTACHMENTS: dict[str, list[dict]] = {}
# A task owns its selected mode runtime.  The global CLUSTER only represents
# the default mode shown in /api/system, so concurrent mode 1/2/3 tasks never
# accidentally execute against whichever task was created last.
TASK_CLUSTERS: dict[str, ClusterRuntime] = {}
LOCK = Lock()
ENV_CONFIG = RuntimeConfig.from_environment()
CONFIG = ENV_CONFIG
STATE_STORE = AtomicJsonStateStore(default_state_path(ROOT))
CLUSTER = get_cluster_runtime(config=CONFIG)


class WeightedLimiter:
    def __init__(self, max_weight: int) -> None:
        self.max_weight = max(1, int(max_weight))
        self.used_weight = 0
        self.condition = Condition()

    def update_limit(self, max_weight: int) -> None:
        with self.condition:
            self.max_weight = max(1, int(max_weight))
            self.condition.notify_all()

    def acquire(self, requested_weight: int, task_id: str) -> int:
        while can_continue(task_id):
            with self.condition:
                weight = min(max(1, int(requested_weight)), self.max_weight)
                if self.used_weight + weight <= self.max_weight:
                    self.used_weight += weight
                    return weight
                self.condition.wait(timeout=0.15)
        return 0

    def release(self, weight: int) -> None:
        if not weight:
            return
        with self.condition:
            self.used_weight = max(0, self.used_weight - int(weight))
            self.condition.notify_all()

    def snapshot(self) -> dict[str, int]:
        with self.condition:
            return {"used_weight": self.used_weight, "max_weight": self.max_weight}


LIMITER = WeightedLimiter(CONFIG.max_weight)


def _state_document() -> dict:
    return build_state_document(
        TASKS,
        persistable_config(CONFIG),
        {task_id: persistable_config(config) for task_id, config in CONFIG_SNAPSHOTS.items()},
        TASK_ATTACHMENTS,
    )


def persist_locked() -> bool:
    secrets = secret_values(ENV_CONFIG) + secret_values(CONFIG)
    for snapshot in CONFIG_SNAPSHOTS.values():
        secrets.extend(secret_values(snapshot))
    return STATE_STORE.save(_state_document(), secrets)


def load_persisted_state(data_dir: str | Path | None = None) -> None:
    global CONFIG, STATE_STORE, CLUSTER
    STATE_STORE = AtomicJsonStateStore(default_state_path(ROOT, data_dir))
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
    LIMITER.update_limit(CONFIG.max_weight)
    if recovery_warnings and STATE_STORE.load_state != "recovered":
        STATE_STORE.preserve_for_recovery("Persisted state required structural recovery: " + "; ".join(recovery_warnings[:4]))
    if changed or recovery_warnings or STATE_STORE.migration_required:
        persist_locked()


def system_payload() -> dict:
    cluster = CLUSTER.status()
    provider_state = provider_catalog(CONFIG)
    return {
        **public_config(CONFIG),
        "executors": available_executors(),
        "live_provider_configured": any(item.get("configured") and item.get("enabled", True) for item in provider_state.get("providers", [])),
        "max_concurrency": CONFIG.max_weight,
        "persistence": STATE_STORE.status(),
        **LIMITER.snapshot(),
        # MODE 0-3 is additive to the legacy executor configuration.  Keep
        # the complete role/status view in the same response for polling UIs.
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


def _cluster_model_tier(model: str) -> str:
    """Map product model labels to the legacy executor tier vocabulary."""
    text = str(model or "").lower()
    if "opus" in text or text.endswith(" sol") or "deepseek" in text:
        return "high"
    if "luna" in text:
        return "low"
    return "medium"


def _mode_agents(runtime: ClusterRuntime, task_id: str, config: RuntimeConfig) -> list[dict]:
    """Convert cluster slots to the legacy task-agent record shape."""
    rows = runtime.task_agents(task_id)
    lead_index = next((index for index, row in enumerate(rows) if row.get("active")), 0)
    if lead_index:
        rows = [rows[lead_index], *rows[:lead_index], *rows[lead_index + 1 :]]
    agents: list[dict] = []
    for index, row in enumerate(rows):
        is_lead = index == 0
        agent_id = "lead" if is_lead else str(row["id"])
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
        route_snapshot = route_public(route)
        agents.append(
            {
                "id": agent_id,
                "name": "Orion" if is_lead and runtime.mode == 0 else row["name"],
                "role": row["role"],
                "role_key": row.get("role_key"),
                "route_key": row.get("route_key"),
                "objective": row["objective"],
                "status": row["status"] if row["active"] else "inactive",
                "step": "Ready" if row["active"] else "Model unavailable",
                "parent_id": None if is_lead else "lead",
                "model_tier": tier,
                "model_name": route.model_id if route else row["model"],
                "reasoning_effort": reasoning_for_tier(config, tier),
                "configured_reasoning": config.reasoning.get(tier),
                "weight": int(config.tier_weights.get(tier, 1)),
                "executor": route.executor if route else str(row.get("executor") or "direct_model"),
                "provider_id": route.provider_id if route else (config.provider_name or ""),
                "route": route_snapshot,
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


def _mode_context(task_id: str, runtime: ClusterRuntime) -> tuple[str, str]:
    """Refresh the bounded model transcript while retaining durable history."""
    with LOCK:
        task = TASKS.get(task_id)
        if not task:
            return "", ""
        transcript = list(task.get("model_context") or task.get("conversation", []))
    compacted = runtime.compress_context(transcript, task_id=task_id)
    with LOCK:
        task = TASKS.get(task_id)
        if not task:
            return str(compacted.get("summary") or ""), str(compacted.get("replacement_model") or "")
        task["model_context"] = list(compacted.get("messages") or transcript)
        task["context_usage"] = compacted.get("usage")
        if compacted.get("compressed"):
            task["context_summary"] = compacted.get("summary")
            _mode_event(task, "context_compressed", "上下文已压缩，继续执行任务", "lead")
            if compacted.get("model_switched"):
                task["model_switch"] = {
                    "reason": "context_limit",
                    "replacement_model": compacted.get("replacement_model"),
                    "timestamp": utc_timestamp(),
                }
                task["selected_model"] = compacted.get("replacement_model")
                task["context_model"] = compacted.get("replacement_model")
                _mode_event(task, "model_switched", f"已切换至 {compacted.get('replacement_model')}", "lead")
        persist_locked()
        return str(task.get("context_summary") or ""), str((task.get("model_switch") or {}).get("replacement_model") or "")


def _mode_event(task: dict, kind: str, message: str, agent_id: str | None = None, **payload: object) -> None:
    """Append a cluster event while preserving the old event schema."""
    add_event(task, kind, message, agent_id)
    if payload:
        event = task.get("events", [])[-1]
        event.update(payload)
    if agent_id and task.get("events"):
        agent = next((item for item in task.get("agents", []) if item.get("id") == agent_id), None)
        if agent:
            task["events"][-1].setdefault("role", agent.get("role"))
            task["events"][-1].setdefault("source", agent.get("role"))
            task["events"][-1].setdefault("role_key", ROLE_KEYS.get(str(agent.get("role") or ""), ""))


def _record_route_switch(task_id: str, agent_id: str, event: dict) -> None:
    """Persist a provider fallback/model switch in the user-visible stream."""
    with LOCK:
        task = TASKS.get(task_id)
        if not task:
            return
        target = (event.get("to") or {}) if isinstance(event, dict) else {}
        provider = target.get("provider_id") or "unknown provider"
        model = target.get("model_id") or "unknown model"
        _mode_event(
            task,
            "model_switched",
            f"Model route switched to {provider}/{model}",
            agent_id,
            route=target,
            reason=(event.get("reason") if isinstance(event, dict) else "provider_fallback"),
        )
        persist_locked()


def run_mode_task(task_id: str) -> None:
    """Execute a MODE 1-3 task using the fixed role pool.

    This is intentionally simulation-friendly.  In live mode each active slot
    delegates through the existing ``execute_agent`` abstraction, while missing
    provider credentials remain inactive and are simply skipped.
    """
    runtime = TASK_CLUSTERS.get(task_id, CLUSTER)
    runtime.start()
    with LOCK:
        task = TASKS.get(task_id)
        if not task or task.get("status") == "cancelled" or not task.get("mode_managed") or int(task.get("mode", 0)) == 0:
            _release_mode_runtime(task_id, runtime)
            return
        config = CONFIG_SNAPSHOTS.get(task_id, CONFIG)
        task["status"] = "planning"
        task["conversation_state"] = "cluster_running"
        task["cluster_started"] = True
        task["cluster_started_at"] = utc_timestamp()
        lead = task["agents"][0]
        lead_runtime_id = str(lead.get("cluster_agent_id") or "lead")
        runtime.begin_work(lead_runtime_id)
        lead.update(status="working", step="拆解需求并分发岗位任务")
        _mode_event(task, "planning", f"{task['mode_label']}主管开始拆解任务", "lead", topic="architecture")
        add_agent_conversation(task, "lead", "assistant", f"{task['mode_label']}正在根据岗位池分发任务")
        persist_locked()
    runtime.emit_status("planning", "主管开始拆解并分发任务", task_id=task_id, agent_id="lead", mode=runtime.mode)

    workers = []
    with LOCK:
        task = TASKS.get(task_id)
        if not task or task.get("status") == "cancelled":
            _release_mode_runtime(task_id, runtime)
            return
        task["status"] = "running"
        _mode_event(task, "fan_out", f"按岗位池激活 {sum(1 for a in task['agents'] if a.get('active'))} 个槽位", "lead")
        workers = [agent for agent in task["agents"][1:] if agent.get("active")]
        persist_locked()

    for agent in workers:
        if not can_continue(task_id):
            _release_mode_runtime(task_id, runtime)
            return
        agent_id = agent["id"]
        cluster_agent_id = str(agent.get("cluster_agent_id") or agent_id)
        runtime.begin_work(cluster_agent_id)
        with LOCK:
            task = TASKS.get(task_id)
            if not task or task.get("status") == "cancelled":
                runtime.finish_work(cluster_agent_id, success=True)
                _release_mode_runtime(task_id, runtime)
                return
            agent = next((item for item in task["agents"] if item["id"] == agent_id), agent)
            agent.update(status="working", step="执行岗位任务")
            add_agent_conversation(task, "lead", "assistant", f"已派发 {agent['role']}：{agent['objective']}")
            add_agent_conversation(task, agent_id, "assistant", f"{agent['role']}开始执行")
            _mode_event(task, "agent_progress", f"{agent['role']}开始执行", agent_id)
        context_summary, replacement_model = _mode_context(task_id, runtime)
        prompt = (
            f"Overall task: {task.get('prompt_context', task.get('prompt', ''))}\n"
            f"Role: {agent['role']}\nAssignment: {agent['objective']}\n"
            f"Mode: {task.get('mode_label')}"
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
            result = execute_agent(
                agent.get("executor", "direct_model"),
                prompt,
                agent.get("model_tier", "medium"),
                agent_config,
                task_id,
                str(ROOT) if task.get("workspace") else None,
                agent.get("reasoning_effort"),
                route,
                role=f"mode-{task.get('mode', 0)}/{role_key}" if role_key else None,
                pool=agent.get("pool"),
                on_route_switch=lambda event, task_id=task_id, agent_id=agent_id: _record_route_switch(task_id, agent_id, event),
            )
            status, step = "complete", "已返回岗位结果"
            runtime.finish_work(cluster_agent_id, success=True)
        except Exception:
            result, status, step = "岗位执行失败，已隔离并继续其他岗位。", "failed", "执行失败，已隔离"
            runtime.finish_work(cluster_agent_id, success=False)
        with LOCK:
            task = TASKS.get(task_id)
            if not task or task.get("status") == "cancelled":
                _release_mode_runtime(task_id, runtime)
                return
            agent = next((item for item in task["agents"] if item["id"] == agent_id), agent)
            agent.update(status=status, step=step, result=str(result))
            add_message(task, "assistant", str(result), agent_id)
            add_agent_conversation(task, agent_id, "assistant", str(result))
            task.setdefault("model_context", []).append({"role": "assistant", "content": str(result), "agent_id": agent_id})
            event_kind = "agent_failed" if status == "failed" else "agent_complete"
            event_message = f"{agent['role']}执行失败，已隔离" if event_kind == "agent_failed" else f"{agent['role']}已返回结果"
            _mode_event(task, event_kind, event_message, agent_id)
            runtime.emit_status(event_kind, event_message, task_id=task_id, agent_id=cluster_agent_id)
        # A single save per worker keeps restart recovery useful without doing
        # one full atomic JSON write for every status field in a 100-slot run.
        with LOCK:
            persist_locked()

    with LOCK:
        task = TASKS.get(task_id)
        if not task or task.get("status") == "cancelled":
            _release_mode_runtime(task_id, runtime)
            return
        prompt_text = str(task.get("prompt_context") or task.get("prompt") or "")
        if int(task.get("mode", 0)) == 3 and any(token in prompt_text.lower() for token in ("争议", "方案", "debate", "dispute", "选择")):
            dispute = runtime.resolve_dispute("任务方案存在分歧", {"方案A": (), "方案B": ()}, task_id=task_id)
            task["dispute"] = dispute
            _mode_event(task, "dispute_resolved", f"辩论与公投完成，胜出：{dispute['winner']}", "lead")
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
        _mode_event(task, "complete", "多模式集群已完成并生成汇总", "lead")
        persist_locked()
    runtime.finish_work(str(task.get("agents", [{}])[0].get("cluster_agent_id") or "lead"), success=True)
    runtime.emit_status("complete", "多模式集群已完成", task_id=task_id, agent_id="lead")
    _release_mode_runtime(task_id, runtime)


def search_cluster_logs(query: Mapping[str, Any]) -> list[dict]:
    """Search runtime events and task timelines using one REST contract."""
    keyword = str(query.get("keyword") or query.get("q") or "").strip().lower()
    role = str(query.get("role") or "").strip().lower()
    task_id = str(query.get("task_id") or "").strip() or None
    # Query strings are untyped in the zero-dependency handler. Treat an
    # invalid limit like an omitted one instead of turning a search into a
    # server error; the FastAPI surface performs the equivalent validation.
    try:
        requested_limit = int(query.get("limit") or 100)
    except (TypeError, ValueError):
        requested_limit = 100
    limit = max(1, min(500, requested_limit))
    from_time = query.get("from") or query.get("from_time")
    to_time = query.get("to") or query.get("to_time")
    rows = CLUSTER.logs_search(keyword, role, from_time, to_time, task_id, limit)
    with LOCK:
        for current_id, task in TASKS.items():
            if task_id and current_id != task_id:
                continue
            for event in task.get("events", []):
                item = dict(event)
                item.setdefault("task_id", current_id)
                stamp = str(item.get("timestamp") or "")
                if from_time and stamp < str(from_time):
                    continue
                if to_time and stamp > str(to_time):
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
                if from_time and stamp < str(from_time):
                    continue
                if to_time and stamp > str(to_time):
                    continue
                haystack = " ".join(str(item.get(key) or "") for key in ("message", "content", "agent_id", "role", "role_key", "source")).lower()
                if keyword and keyword not in haystack:
                    continue
                if role and role not in haystack:
                    continue
                rows.append(item)
    rows.sort(key=lambda item: str(item.get("timestamp") or ""), reverse=True)
    return rows[:limit]


def clock() -> str:
    return time.strftime("%H:%M:%S")


def add_event(task: dict, kind: str, message: str, agent_id: str | None = None) -> None:
    timestamp = utc_timestamp()
    estimated = max(1, (len(str(message)) + 3) // 4)
    event = {"id": str(uuid4()), "time": timestamp[11:19], "timestamp": timestamp, "type": kind, "message": message, "agent_id": agent_id, "token_estimate": estimated}
    task.setdefault("events", []).append(event)
    if agent_id:
        agent = next((item for item in task.get("agents", []) if item["id"] == agent_id), None)
        if agent is not None:
            agent.setdefault("work_log", []).append({"time": event["time"], "timestamp": timestamp, "type": kind, "message": message})


def add_message(task: dict, role: str, content: str, agent_id: str | None = None) -> None:
    timestamp = utc_timestamp()
    estimated = max(1, (len(str(content)) + 3) // 4)
    task.setdefault("conversation", []).append({"id": str(uuid4()), "time": timestamp[11:19], "timestamp": timestamp, "role": role, "content": content, "agent_id": agent_id, "token_estimate": estimated, "token_usage": {"input_tokens": estimated if role == "user" else 0, "output_tokens": estimated if role == "assistant" else 0}})


def add_agent_conversation(task: dict, agent_id: str, role: str, content: str) -> None:
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


def add_dispatch_turns(task: dict, agent_id: str, objective: str) -> None:
    add_agent_conversation(task, "lead", "assistant", f"Dispatching {agent_id}: {objective}")
    add_agent_conversation(task, agent_id, "lead", objective)


def is_greeting(prompt: str) -> bool:
    return shared_is_greeting(prompt)


def coordinator_reply(
    prompt: str,
    cluster_available: bool,
    questions: list[str] | None = None,
    recommendation: dict | None = None,
    workflow_ready: bool = False,
) -> str:
    return shared_coordinator_reply(prompt, questions, recommendation, cluster_available, workflow_ready)


def _approval_error(task: dict) -> str:
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


def can_continue(task_id: str) -> bool:
    while True:
        with LOCK:
            task = TASKS.get(task_id)
            if not task or task.get("status") == "cancelled":
                return False
            if task.get("status") != "paused":
                return True
        time.sleep(0.15)


def answer_message(task_id: str, content: str, reply_token: int | None = None) -> None:
    time.sleep(0.18)
    with LOCK:
        task = TASKS.get(task_id)
        if (
            not task
            or task.get("status") in {"cancelled", "complete"}
            or task.get("cluster_started")
            or (reply_token is not None and task.get("reply_token") != reply_token)
        ):
            return
        config = CONFIG_SNAPSHOTS.get(task_id, CONFIG)
        combined_prompt = task.get("prompt_context") or task.get("prompt", "")
        assessment, recommendation = build_reasoning_recommendation(
            combined_prompt,
            TASK_ATTACHMENTS.get(task_id, []),
            bool(task.get("workspace")),
            config,
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
        task["review"].update(status="pending", approved_at=None, approved_by=None)
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
        add_event(task, "assistant_message", "Orion replied with clarification questions or an updated workflow", "lead")
        persist_locked()


def start_cluster(task_id: str) -> bool:
    with LOCK:
        task = TASKS.get(task_id)
        if (
            not task
            or not task.get("cluster_available")
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
        add_event(task, "cluster_started", "The approved workflow is starting the agent cluster", "lead")
        persist_locked()
    Thread(target=run_swarm, args=(task_id,), daemon=True).start()
    return True


def finish_worker(task_id: str, agent_id: str, delay: float) -> None:
    time.sleep(delay)
    if not can_continue(task_id):
        return
    with LOCK:
        task = TASKS.get(task_id)
        if not task or task.get("status") == "cancelled":
            return
        agent = next((item for item in task["agents"] if item["id"] == agent_id), None)
        if not agent:
            return
        prompt = (
            f"Overall task and clarified context: {task.get('prompt_context', task['prompt'])}\n\nYour role: {agent['role']}\n"
            f"Assignment: {agent['objective']}\nCluster reasoning profile: {task.get('cluster_reasoning', CONFIG.cluster_reasoning)}"
            + attachment_prompt(TASK_ATTACHMENTS.get(task_id, []))
        )
        executor = agent["executor"]
        tier = agent["model_tier"]
        effort = agent.get("reasoning_effort")
        config = CONFIG_SNAPSHOTS.get(task_id, CONFIG)
        workspace = str(ROOT) if task.get("workspace") else None
        agent["step"] = "Waiting for weighted capacity"
        persist_locked()
    acquired_weight = LIMITER.acquire(agent.get("weight", config.tier_weights.get(tier, 1)), task_id)
    if not acquired_weight:
        return
    try:
        with LOCK:
            task = TASKS.get(task_id)
            if not task or task.get("status") == "cancelled":
                return
            agent = next(item for item in task["agents"] if item["id"] == agent_id)
            agent["step"] = "Working through assigned evidence"
            add_event(task, "agent_progress", f"{agent['name']} started its assigned evidence pass", agent_id)
            add_agent_conversation(task, "lead", "assistant", f"Checking progress for {agent['name']}")
            add_agent_conversation(task, agent_id, "assistant", f"Started the {agent['role'].lower()} pass")
            persist_locked()
        role_key = role_route_key(str(agent.get("role") or ""))
        route = resolve_route(
            config,
            tier,
            role=f"mode-{task.get('mode', 0)}/{role_key}" if role_key else agent.get("role"),
            pool=agent.get("pool"),
            model_id=agent.get("model_name") or model_for_tier(config, tier),
            task_override=agent.get("route") or None,
        )
        result = execute_agent(
            executor,
            prompt,
            tier,
            config,
            task_id,
            workspace,
            effort,
            route,
            role=f"mode-{task.get('mode', 0)}/{role_key}" if role_key else agent.get("role"),
            pool=agent.get("pool"),
            on_route_switch=lambda event, task_id=task_id, agent_id=agent_id: _record_route_switch(task_id, agent_id, event),
        )
        status, step = "complete", "Returned a structured finding"
        with LOCK:
            task = TASKS.get(task_id)
            if task:
                add_agent_conversation(task, agent_id, "assistant", result)
                add_agent_conversation(task, "lead", "assistant", f"Received findings from {agent['name']}")
                persist_locked()
    except Exception:
        result = "The executor failed without exposing credentials. Retry after checking its local configuration."
        status, step = "failed", "Executor failed safely"
        with LOCK:
            task = TASKS.get(task_id)
            if task:
                add_agent_conversation(task, agent_id, "system", "The executor failed safely")
                persist_locked()
    finally:
        LIMITER.release(acquired_weight)
    with LOCK:
        task = TASKS.get(task_id)
        if not task or task.get("status") == "cancelled":
            return
        agent = next(item for item in task["agents"] if item["id"] == agent_id)
        agent.update(status=status, step=step, result=result)
        add_event(task, "agent_complete", f"{agent['name']} returned a result", agent_id)
        persist_locked()


def _worker_tiers(recommended: str, count: int) -> list[str]:
    profiles = {
        "low": ["low", "low", "low", "low"],
        "medium": ["medium", "medium", "low", "low"],
        "high": ["medium", "high", "medium", "low"],
        "ultra": ["high", "ultra", "high", "medium"],
    }
    return profiles.get(recommended, profiles["medium"])[:count]


def run_swarm(task_id: str) -> None:
    config = CONFIG_SNAPSHOTS.get(task_id, CONFIG)
    with LOCK:
        task = TASKS.get(task_id)
        if not task or not task.get("cluster_available") or task.get("cluster_started") is not True:
            return
        task["status"] = "planning"
        task["conversation_state"] = "cluster_running"
        task["agents"][0].update(status="working", step="Assessing complexity and critical path")
        add_event(task, "planning", "Coordinator is building a dynamic task graph", "lead")
        add_agent_conversation(task, "lead", "assistant", "Review approved; assessing complexity and critical path")
        persist_locked()
    time.sleep(0.65)
    if not can_continue(task_id):
        return
    with LOCK:
        task = TASKS.get(task_id)
        if not task or task.get("status") == "cancelled":
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
                "id": f"agent-{index}", "name": name, "role": role, "objective": objective,
                "status": "working", "step": "Waiting for weighted capacity", "parent_id": "lead",
                "model_tier": tier, "model_name": route.model_id if route else model_for_tier(config, tier),
                "reasoning_effort": effort, "configured_reasoning": config.reasoning.get(tier, effort),
                "cluster_reasoning": task.get("cluster_reasoning", config.cluster_reasoning),
                "weight": int(config.tier_weights.get(tier, 1)),
                "executor": route.executor if route else executor,
                "provider_id": route.provider_id if route else (config.provider_name or ""),
                "route": route_public(route), "route_version": route.route_version if route else 0,
                "result": None, "work_log": [],
            }
            task["agents"].append(agent)
            task.setdefault("agent_conversations", {}).setdefault(agent["id"], [])
            add_dispatch_turns(task, agent["id"], objective)
        add_event(task, "fan_out", f"Coordinator dispatched {len(definitions)} independent sub-agents", "lead")
        persist_locked()
    workers = [Thread(target=finish_worker, args=(task_id, f"agent-{index}", delay), daemon=True) for index, (_, _, _, delay) in enumerate(definitions, 1)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    if not can_continue(task_id):
        return
    with LOCK:
        task = TASKS.get(task_id)
        if not task or task.get("status") == "cancelled":
            return
        task["agents"][0]["step"] = "Reconciling results and drafting final answer"
        add_event(task, "synthesis", "Coordinator is merging verified findings", "lead")
        add_agent_conversation(task, "lead", "assistant", "Reconciling the independent findings")
        persist_locked()
    time.sleep(0.6)
    if not can_continue(task_id):
        return
    with LOCK:
        task = TASKS.get(task_id)
        if not task or task.get("status") == "cancelled":
            return
        task["agents"][0].update(status="complete", step="Delivered final synthesis")
        task["status"] = "complete"
        completed = [agent for agent in task["agents"][1:] if agent.get("result")]
        task["result"] = synthesis_reply(task.get("prompt_context", task["prompt"]), len(completed))
        add_message(task, "assistant", task["result"], "lead")
        add_agent_conversation(task, "lead", "assistant", task["result"])
        task["conversation_state"] = "complete"
        task["assistant_replied"] = True
        task["last_answered_token"] = int(task.get("reply_token", 0))
        task["completed_at"] = utc_timestamp()
        add_event(task, "complete", "Task completed and final synthesis is ready", "lead")
        persist_locked()


def build_task(payload: dict, config: RuntimeConfig, attachments: list[dict]) -> dict:
    global CLUSTER
    prompt = str(payload.get("prompt", "")).strip()
    explicit_mode = payload.get("mode")
    if explicit_mode is None:
        explicit_mode = payload.get("cluster_mode", payload.get("runtime_mode"))
    default_mode = getattr(config, "mode", configured_mode())
    mode = parse_mode(default_mode if explicit_mode is None else explicit_mode)
    profiles = getattr(config, "agent_profiles", None)
    profile_present = isinstance(profiles, dict) and (str(mode) in profiles or mode in profiles)
    mode_managed = explicit_mode is not None or default_mode != 0 or profile_present
    assessment, recommendation = build_reasoning_recommendation(prompt, attachments, bool(payload.get("workspace")), config)
    cluster_reasoning = str(payload.get("cluster_reasoning") or recommendation.get("level") or config.cluster_reasoning).strip().lower()
    if cluster_reasoning == "ultra":
        cluster_reasoning = "xhigh"
    if cluster_reasoning not in REASONING_LEVELS:
        cluster_reasoning = config.cluster_reasoning
    cluster_enabled = bool(payload.get("cluster_enabled", False)) or (mode_managed and mode > 0)
    cluster_available = (
        True
        if mode_managed and mode > 0
        else bool(cluster_enabled and not is_greeting(prompt) and int(assessment.get("recommended_agents", 1)) > 1)
    )
    lead_tier = config.max_tier
    lead_model = "claude-opus-5" if mode == 0 else model_for_tier(config, lead_tier)
    lead_route = resolve_route(
        config,
        lead_tier,
        role=f"mode-{mode}/{role_route_key('通用助理')}" if mode == 0 else None,
        model_id=lead_model,
    )
    lead = {
        "id": "lead", "name": "Orion", "role": "Swarm coordinator",
        "objective": "Clarify, plan, dispatch, and synthesize the task", "status": "working",
        "step": "Preparing clarification questions", "parent_id": None, "model_tier": lead_tier,
        "model_name": lead_route.model_id if lead_route else lead_model, "reasoning_effort": reasoning_for_tier(config, lead_tier, cluster_reasoning),
        "weight": int(config.tier_weights.get(lead_tier, 1)), "executor": lead_route.executor if lead_route else "direct_model",
        "provider_id": lead_route.provider_id if lead_route else (config.provider_name or ""),
        "route": route_public(lead_route), "route_version": lead_route.route_version if lead_route else 0,
        "system_prompt": COORDINATOR_INSTRUCTIONS, "result": None, "work_log": [],
    }
    created_at_iso = utc_timestamp()
    task = {
        "id": str(uuid4()), "title": prompt.replace("\n", " ")[:72], "prompt": prompt, "prompt_context": prompt,
        "attachments": public_attachments(attachments), "cluster_enabled": cluster_enabled,
        "cluster_available": cluster_available, "cluster_started": False, "cluster_reasoning": cluster_reasoning,
        "cluster_reasoning_effort": cluster_reasoning, "workspace": bool(payload.get("workspace")), "created_at": created_at_iso[11:19],
        "created_at_iso": created_at_iso, "source_task_id": None, "parent_task_id": None,
        "root_task_id": None, "continued_task_ids": [], "inherited_at": None, "inherited_context_summary": None,
        "status": "chatting", "conversation_state": "responding", "assistant_replied": False,
        "user_turns": 1, "task_turns": 0 if is_greeting(prompt) else 1,
        "reply_token": 1, "last_answered_token": 0,
        "coordinator_instructions": COORDINATOR_INSTRUCTIONS,
        "workflow_ready": False,
        "workflow_confirmed": False, "reasoning_approved": False, "approval_required": cluster_available,
        "reasoning_proposed": False,
        "clarifying_questions": list(assessment["clarifying_questions"]), "needs_clarification": bool(assessment["clarifying_questions"]),
        "difficulty": assessment, "difficulty_assessment": assessment, "reasoning_recommendation": recommendation,
        "reasoning_profile": recommendation, "review": {"required": cluster_available, "status": "pending", "approved_at": None, "approved_by": None},
        "agents": [lead], "agent_conversations": {"lead": []}, "conversation": [], "events": [], "result": None,
        "mode": mode,
        "mode_label": MODE_LABELS[mode],
        "mode_managed": mode_managed,
        "cluster_status": {},
    }
    if mode_managed:
        # Keep every maximum slot visible, including inactive slots.  This is
        # what lets the UI show e.g. ``后端开发组 0/3`` after a bad key.
        # A task owns its lifecycle and heartbeats. Reusing the display
        # runtime would make simultaneous tasks in the same mode share slots.
        runtime = ClusterRuntime(mode, config=config, simulation=config.simulation, autostart=True) if mode > 0 else CLUSTER
        if mode > 0:
            TASK_CLUSTERS[task["id"]] = runtime
        task["agents"] = _mode_agents(runtime, task["id"], config)
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
    task["root_task_id"] = task["id"]
    add_message(task, "user", prompt)
    add_agent_conversation(task, "lead", "user", prompt)
    add_event(task, "user_message", "You sent a message to Orion", "lead")
    return task


def _complete_profile_command_task(task: dict, confirmation: str) -> None:
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
    add_event(task, "configuration_updated", confirmation, "lead")


def _complete_unconfigured_task(task: dict) -> None:
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
    add_event(task, "blocked", message, "lead")


def build_continuation_task(source: dict, content: str, config: RuntimeConfig) -> tuple[dict, list[dict], bool]:
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
        {
            "prompt": initial_prompt,
            "cluster_enabled": False,
            "workspace": bool(source.get("workspace")),
        },
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
    add_event(task, "continuation_created", "A new discussion task inherited context from the completed source task", "lead")
    return task, attachments, False


class Handler(BaseHTTPRequestHandler):
    server_version = "OrbitSwarm/0.2"

    def log_message(self, *_args) -> None:
        return

    def json_response(self, value, status: int = 200) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.bytes_response(body, "application/json; charset=utf-8", status)

    def persistence_failure(self) -> None:
        self.json_response(
            {
                "detail": STATE_STORE.last_error or "Could not persist local state",
                "persistence": STATE_STORE.status(),
            },
            503,
        )

    def bytes_response(self, body: bytes, content_type: str, status: int = 200, headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict:
        try:
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}"
            value = json.loads(raw)
            return value if isinstance(value, dict) else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/providers":
            with LOCK:
                self.json_response(provider_catalog(CONFIG))
            return
        if path.startswith("/api/providers/"):
            provider_id = unquote(path.rsplit("/", 1)[-1]).strip()
            if provider_id and provider_id != "test":
                with LOCK:
                    catalog = provider_catalog(CONFIG)
                provider = next((item for item in catalog.get("providers", []) if item.get("id") == provider_id.lower()), None)
                if provider:
                    self.json_response(provider)
                else:
                    self.json_response({"detail": "Provider not found"}, 404)
                return
        if path == "/api/routes":
            with LOCK:
                self.json_response(route_catalog(CONFIG))
            return
        if path in ("/api/system", "/api/config"):
            with LOCK:
                self.json_response(system_payload())
            return
        if path == "/api/agent-profiles":
            with LOCK:
                self.json_response({
                    "agent_profiles": getattr(CONFIG, "agent_profiles", None) or {},
                    "mode_roles": getattr(CONFIG, "agent_profiles", None) or {},
                    "role_catalog": role_catalog(),
                    "executors": ["direct_model", "openclaw", "codex", "claude_code"],
                })
            return
        if path in ("/api/cluster", "/api/cluster/status"):
            with LOCK:
                self.json_response(CLUSTER.status())
            return
        if path in ("/api/logs", "/api/logs/search", "/api/search/logs", "/api/tasks/search"):
            query = {key: (values[-1] if values else "") for key, values in parse_qs(parsed.query).items()}
            self.json_response({"items": search_cluster_logs(query), "query": query})
            return
        if path.startswith("/api/tasks/") and path.endswith("/export"):
            task_id = path.split("/")[-2]
            export_format = str((parse_qs(parsed.query).get("format") or ["json"])[0]).strip().lower()
            if export_format not in {"json", "markdown", "md"}:
                self.json_response({"detail": "format must be json or markdown"}, 422)
                return
            with LOCK:
                task = TASKS.get(task_id)
                if not task:
                    self.json_response({"detail": "Task not found"}, 404)
                    return
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
                content_type = "application/json; charset=utf-8"
            else:
                body = render_markdown(payload).encode("utf-8")
                filename = safe_export_filename(task, "md", secrets)
                content_type = "text/markdown; charset=utf-8"
            self.bytes_response(body, content_type, headers={"Content-Disposition": content_disposition(filename)})
            return
        with LOCK:
            if path == "/api/tasks":
                self.json_response(list(reversed(list(TASKS.values()))))
                return
            if path.startswith("/api/tasks/") and path.endswith("/messages"):
                task_id = path.split("/")[-2]
                task = TASKS.get(task_id)
                if task:
                    self.json_response({"conversation": list(task.get("conversation", [])), "agent_conversations": task.get("agent_conversations", {})})
                    return
                self.json_response({"detail": "Task not found"}, 404)
                return
            if path.startswith("/api/tasks/"):
                task = TASKS.get(path.rsplit("/", 1)[-1])
                if task:
                    self.json_response(task)
                    return
        relative = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
        target = (ROOT / "frontend" / relative).resolve()
        frontend_root = (ROOT / "frontend").resolve()
        if frontend_root not in target.parents or not target.is_file():
            self.send_error(404)
            return
        mime = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}.get(target.suffix, "application/octet-stream")
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _commit_routing_config(self, candidate: RuntimeConfig) -> bool:
        """Atomically swap provider/routes while preserving task snapshots."""
        global CONFIG, CLUSTER
        replacement = ClusterRuntime(
            getattr(candidate, "mode", 0),
            config=candidate,
            simulation=candidate.simulation,
            autostart=True,
        )
        with LOCK:
            previous, previous_cluster = CONFIG, CLUSTER
            CONFIG = candidate
            persisted = persist_locked()
            if not persisted:
                CONFIG = previous
                replacement.stop()
            else:
                CLUSTER = replacement
                previous_cluster.stop()
        if not persisted:
            self.persistence_failure()
            return False
        return True

    def _routing_response(self) -> dict:
        with LOCK:
            providers = provider_catalog(CONFIG)
            routes = route_catalog(CONFIG)
        return {**providers, "routes": routes.get("routes", []), "route_catalog": routes, "providers": providers.get("providers", [])}

    def do_PUT(self) -> None:
        global CONFIG
        path = urlparse(self.path).path
        payload = self.read_json()
        segments = [unquote(item) for item in path.split("/") if item]
        try:
            if path == "/api/agent-profiles":
                with LOCK:
                    candidate = updated_config(CONFIG, payload)
                if not self._commit_routing_config(candidate):
                    return
                with LOCK:
                    self.json_response({
                        "agent_profiles": getattr(CONFIG, "agent_profiles", None) or {},
                        "mode_roles": getattr(CONFIG, "agent_profiles", None) or {},
                        "role_catalog": role_catalog(),
                        "executors": ["direct_model", "openclaw", "codex", "claude_code"],
                    })
                return
            if len(segments) >= 3 and segments[1] == "providers":
                provider_id = segments[2]
                if len(segments) == 4 and segments[3] == "test":
                    with LOCK:
                        result = provider_test(CONFIG, provider_id, payload)
                    self.json_response(result)
                    return
                with LOCK:
                    candidate = update_provider(CONFIG, provider_id, payload)
                if not self._commit_routing_config(candidate):
                    return
                self.json_response(self._routing_response())
                return
            if len(segments) >= 4 and segments[1] == "routes":
                scope = segments[2]
                if scope in {"roles", "role"} and len(segments) >= 5:
                    mode, role = segments[3], segments[4]
                    route_key = f"mode-{parse_mode(mode)}/{role}"
                    with LOCK:
                        candidate = update_route(CONFIG, "role", route_key, payload)
                    if not self._commit_routing_config(candidate):
                        return
                    self.json_response(self._routing_response())
                    return
                if scope in {"tiers", "tier", "pools", "pool"} and len(segments) >= 4:
                    key = segments[3]
                    normalized_scope = "tier" if scope.startswith("tier") else "pool"
                    with LOCK:
                        candidate = update_route(CONFIG, normalized_scope, key, payload)
                    if not self._commit_routing_config(candidate):
                        return
                    self.json_response(self._routing_response())
                    return
            self.json_response({"detail": "Unsupported provider or route endpoint"}, 404)
        except (TypeError, ValueError) as error:
            self.json_response({"detail": str(error)}, 422)

    def do_DELETE(self) -> None:
        global CONFIG
        path = urlparse(self.path).path
        segments = [unquote(item) for item in path.split("/") if item]
        try:
            if len(segments) == 3 and segments[1] == "providers":
                provider_id = segments[2]
                with LOCK:
                    candidate = disable_provider(CONFIG, provider_id)
                if not self._commit_routing_config(candidate):
                    return
                self.json_response(self._routing_response())
                return
            if len(segments) >= 4 and segments[1] == "routes":
                scope = segments[2]
                if scope in {"roles", "role"}:
                    if len(segments) < 5:
                        self.json_response({"detail": "Role route requires mode and role"}, 422)
                        return
                    key = f"mode-{parse_mode(segments[3])}/{segments[4]}"
                    normalized_scope = "role"
                elif scope in {"tiers", "tier", "pools", "pool"}:
                    key = segments[3]
                    normalized_scope = "tier" if scope.startswith("tier") else "pool"
                else:
                    self.json_response({"detail": "Unsupported route endpoint"}, 404)
                    return
                with LOCK:
                    candidate = remove_route(CONFIG, normalized_scope, key)
                if not self._commit_routing_config(candidate):
                    return
                self.json_response(self._routing_response())
                return
            self.json_response({"detail": "Unsupported provider or route endpoint"}, 404)
        except (TypeError, ValueError) as error:
            self.json_response({"detail": str(error)}, 422)

    def do_POST(self) -> None:
        global CONFIG, CLUSTER
        path = urlparse(self.path).path
        segments = [unquote(item) for item in path.split("/") if item]
        if path in ("/api/agent-profiles", "/api/agent-profiles/command"):
            payload = self.read_json()
            if path.endswith("/command"):
                command = str(payload.get("command") or "").strip().lower()
                if command not in {"set_mode_roles", "replace_mode_roles", "configure_mode"}:
                    self.json_response({"detail": "command must be set_mode_roles, replace_mode_roles, or configure_mode"}, 422)
                    return
                if "roles" not in payload:
                    self.json_response({"detail": "roles is required"}, 422)
                    return
                payload = {"profile_mode": payload.get("mode", CONFIG.mode), "roles": payload["roles"]}
            try:
                with LOCK:
                    candidate = updated_config(CONFIG, payload)
                if not self._commit_routing_config(candidate):
                    return
                with LOCK:
                    self.json_response({
                        "agent_profiles": getattr(CONFIG, "agent_profiles", None) or {},
                        "mode_roles": getattr(CONFIG, "agent_profiles", None) or {},
                        "role_catalog": role_catalog(),
                        "executors": ["direct_model", "openclaw", "codex", "claude_code"],
                    })
            except (TypeError, ValueError) as error:
                self.json_response({"detail": str(error)}, 422)
            return
        if path == "/api/providers" or (len(segments) == 3 and segments[1] == "providers"):
            payload = self.read_json()
            provider_id = str((segments[2] if len(segments) == 3 else "") or payload.get("id") or payload.get("provider_id") or payload.get("name") or "").strip()
            if not provider_id:
                self.json_response({"detail": "provider id is required"}, 422)
                return
            try:
                with LOCK:
                    candidate = update_provider(CONFIG, provider_id, payload)
                if self._commit_routing_config(candidate):
                    self.json_response(self._routing_response())
            except (TypeError, ValueError) as error:
                self.json_response({"detail": str(error)}, 422)
            return
        if len(segments) == 4 and segments[1] == "providers" and segments[3] == "test":
            payload = self.read_json()
            with LOCK:
                result = provider_test(CONFIG, segments[2], payload)
            self.json_response(result)
            return
        if path == "/api/routes":
            payload = self.read_json()
            try:
                scope = str(payload.get("scope") or "tier")
                key = str(payload.get("key") or payload.get("role") or payload.get("tier") or "default")
                with LOCK:
                    candidate = update_route(CONFIG, scope, key, payload)
                if self._commit_routing_config(candidate):
                    self.json_response(self._routing_response())
            except (TypeError, ValueError) as error:
                self.json_response({"detail": str(error)}, 422)
            return
        if path in ("/api/cluster/mode", "/api/mode"):
            payload = self.read_json()
            if "mode" not in payload and "cluster_mode" not in payload and "runtime_mode" not in payload:
                self.json_response({"detail": "mode is required"}, 422)
                return
            mode = parse_mode(payload.get("mode", payload.get("cluster_mode", payload.get("runtime_mode"))))
            with LOCK:
                previous_config, previous_cluster = CONFIG, CLUSTER
                candidate = updated_config(CONFIG, {"mode": mode})
                replacement = ClusterRuntime(mode, config=candidate, simulation=candidate.simulation, autostart=True)
                CONFIG = candidate
                persisted = persist_locked()
                if persisted:
                    CLUSTER = replacement
                    previous_cluster.stop()
                    response = system_payload()
                else:
                    CONFIG = previous_config
                    replacement.stop()
                    response = system_payload()
            if not persisted:
                self.persistence_failure()
                return
            self.json_response(response)
            return
        if path == "/api/config":
            raw_config = self.read_json()
            try:
                candidate = updated_config(CONFIG, raw_config)
            except (TypeError, ValueError) as error:
                self.json_response({"detail": str(error)}, 422)
                return
            with LOCK:
                previous, previous_cluster = CONFIG, CLUSTER
                replacement = None
                CONFIG = candidate
                LIMITER.update_limit(CONFIG.max_weight)
                if any(key in raw_config for key in ("mode", "cluster_mode", "runtime_mode", "providers", "routes", "default_provider_id", "provider_registry", "agent_profiles", "mode_roles", "roles_by_mode", "roles")):
                    replacement = ClusterRuntime(getattr(candidate, "mode", 0), config=candidate, simulation=candidate.simulation, autostart=True)
                persisted = persist_locked()
                if not persisted:
                    CONFIG = previous
                    LIMITER.update_limit(CONFIG.max_weight)
                    if replacement:
                        replacement.stop()
                elif replacement:
                    CLUSTER = replacement
                    previous_cluster.stop()
                response = system_payload()
            if not persisted:
                self.persistence_failure()
                return
            self.json_response(response)
            return
        if path == "/api/tasks":
            payload = self.read_json()
            prompt = str(payload.get("prompt", "")).strip()
            if not prompt:
                self.json_response({"detail": "prompt is required"}, 422)
                return
            if len(prompt) > 8000:
                self.json_response({"detail": "prompt is too long"}, 422)
                return
            command = parse_agent_profile_command(prompt, CONFIG)
            if command:
                try:
                    with LOCK:
                        candidate = updated_config(CONFIG, command.payload)
                except (TypeError, ValueError) as error:
                    self.json_response({"detail": str(error)}, 422)
                    return
                if not self._commit_routing_config(candidate):
                    return
                attachments = normalize_attachments(payload.get("attachments", []))
                with LOCK:
                    task = build_task(payload, CONFIG, attachments)
                    _complete_profile_command_task(task, command.confirmation)
                    TASKS[task["id"]] = task
                    CONFIG_SNAPSHOTS[task["id"]] = CONFIG
                    TASK_ATTACHMENTS[task["id"]] = attachments
                    persisted = persist_locked()
                    if not persisted:
                        TASKS.pop(task["id"], None)
                        CONFIG_SNAPSHOTS.pop(task["id"], None)
                        TASK_ATTACHMENTS.pop(task["id"], None)
                if not persisted:
                    self.persistence_failure()
                    return
                self.json_response(task, 201)
                return
            config = CONFIG
            attachments = normalize_attachments(payload.get("attachments", []))
            task = build_task(payload, config, attachments)
            with LOCK:
                TASKS[task["id"]] = task
                CONFIG_SNAPSHOTS[task["id"]] = config
                TASK_ATTACHMENTS[task["id"]] = attachments
                if task.get("unconfigured_mode"):
                    _complete_unconfigured_task(task)
                persisted = persist_locked()
                if not persisted:
                    TASKS.pop(task["id"], None)
                    CONFIG_SNAPSHOTS.pop(task["id"], None)
                    TASK_ATTACHMENTS.pop(task["id"], None)
                    TASK_CLUSTERS.pop(task["id"], None)
            if not persisted:
                self.persistence_failure()
                return
            if task.get("unconfigured_mode"):
                pass
            elif task.get("mode_managed") and int(task.get("mode", 0)) > 0:
                # MODE tasks start automatically after the single user input;
                # the legacy approval gates remain available for old payloads.
                Thread(target=run_mode_task, args=(task["id"],), daemon=True).start()
            else:
                Thread(target=answer_message, args=(task["id"], task["prompt"], task["reply_token"]), daemon=True).start()
            self.json_response(task, 201)
            return
        if path.startswith("/api/tasks/") and path.endswith("/continue"):
            task_id = path.split("/")[-2]
            payload = self.read_json()
            content = str(payload.get("content", "")).strip()
            if len(content) > 8000:
                self.json_response({"detail": "content is too long"}, 422)
                return
            with LOCK:
                source = TASKS.get(task_id)
                if not source:
                    self.json_response({"detail": "Task not found"}, 404)
                    return
                if source.get("status") != "complete":
                    self.json_response({"detail": "Only completed tasks can be continued"}, 409)
                    return
                config = CONFIG
                task, attachments, start_reply = build_continuation_task(source, content, config)
                TASKS[task["id"]] = task
                CONFIG_SNAPSHOTS[task["id"]] = config
                TASK_ATTACHMENTS[task["id"]] = attachments
                source.setdefault("continued_task_ids", []).append(task["id"])
                add_event(source, "continued", f"Discussion continued in task {task['id']}")
                persisted = persist_locked()
                if not persisted:
                    TASKS.pop(task["id"], None)
                    CONFIG_SNAPSHOTS.pop(task["id"], None)
                    TASK_ATTACHMENTS.pop(task["id"], None)
                    TASK_CLUSTERS.pop(task["id"], None)
                    if source.get("continued_task_ids", [])[-1:] == [task["id"]]:
                        source["continued_task_ids"].pop()
                    if source.get("events") and source["events"][-1].get("type") == "continued":
                        source["events"].pop()
            if not persisted:
                self.persistence_failure()
                return
            if start_reply:
                target = run_mode_task if task.get("mode_managed") and int(task.get("mode", 0)) > 0 else answer_message
                args = (task["id"],) if target is run_mode_task else (task["id"], content, task["reply_token"])
                Thread(target=target, args=args, daemon=True).start()
            self.json_response(task, 201)
            return
        if path.startswith("/api/tasks/") and path.endswith("/messages"):
            task_id = path.split("/")[-2]
            payload = self.read_json()
            content = str(payload.get("content", "")).strip()
            if not content:
                self.json_response({"detail": "content is required"}, 422)
                return
            command = parse_agent_profile_command(content, CONFIG)
            if command:
                with LOCK:
                    task = TASKS.get(task_id)
                    if not task:
                        self.json_response({"detail": "Task not found"}, 404)
                        return
                    candidate = updated_config(CONFIG, command.payload)
                if not self._commit_routing_config(candidate):
                    return
                with LOCK:
                    task = TASKS.get(task_id)
                    if not task:
                        self.json_response({"detail": "Task not found"}, 404)
                        return
                    original_task = deepcopy(task)
                    add_message(task, "user", content)
                    task.setdefault("agent_conversations", {}).setdefault("lead", [])
                    add_agent_conversation(task, "lead", "user", content)
                    add_message(task, "assistant", command.confirmation, "lead")
                    add_agent_conversation(task, "lead", "assistant", command.confirmation)
                    task["configuration_updated"] = True
                    task["configuration_confirmation"] = command.confirmation
                    add_event(task, "configuration_updated", command.confirmation, "lead")
                    persisted = persist_locked()
                    if not persisted:
                        TASKS[task_id] = original_task
                if not persisted:
                    self.persistence_failure()
                    return
                with LOCK:
                    self.json_response(TASKS[task_id], 201)
                return
            with LOCK:
                task = TASKS.get(task_id)
                if not task:
                    self.json_response({"detail": "Task not found"}, 404)
                    return
                if task["status"] in ("cancelled", "complete") or task.get("cluster_started"):
                    self.json_response({"detail": "Task is no longer accepting messages"}, 409)
                    return
                original_task = deepcopy(task)
                if "cluster_enabled" in payload:
                    task["cluster_enabled"] = bool(payload.get("cluster_enabled"))
                add_message(task, "user", content)
                add_event(task, "user_message", "You sent a message to Orion", "lead")
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
                reply_token = task["reply_token"]
                persisted = persist_locked()
                if not persisted:
                    TASKS[task_id] = original_task
            if not persisted:
                self.persistence_failure()
                return
            if task.get("mode_managed") and int(task.get("mode", 0)) > 0:
                Thread(target=run_mode_task, args=(task_id,), daemon=True).start()
            else:
                Thread(target=answer_message, args=(task_id, content, reply_token), daemon=True).start()
            with LOCK:
                self.json_response(TASKS[task_id], 201)
            return
        if path.startswith("/api/tasks/") and path.endswith("/control"):
            task_id = path.split("/")[-2]
            payload = self.read_json()
            action = str(payload.get("action", ""))
            launch_cluster = False
            cancel_runtime: ClusterRuntime | None = None
            cancel_agent_id = "lead"
            with LOCK:
                task = TASKS.get(task_id)
                if not task:
                    self.json_response({"detail": "Task not found"}, 404)
                    return
                original_task = deepcopy(task)
                original_snapshot = CONFIG_SNAPSHOTS.get(task_id)
                if action == "pause" and task["status"] in ("planning", "running"):
                    task["status"] = "paused"
                elif action == "resume" and task["status"] == "paused":
                    task["status"] = "running"
                elif action == "cancel" and task["status"] not in ("complete", "cancelled"):
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
                        self.json_response({"detail": "Reply to the coordinator and wait for its updated workflow before confirming"}, 409)
                        return
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
                        self.json_response({"detail": "Confirm the updated workflow before approving reasoning"}, 409)
                        return
                    level = str(payload.get("level") or payload.get("reasoning_level") or task.get("reasoning_recommendation", {}).get("level", "high")).strip().lower()
                    if level == "ultra":
                        level = "xhigh"
                    if level not in REASONING_LEVELS:
                        self.json_response({"detail": "level must be minimal, low, medium, high, or xhigh"}, 422)
                        return
                    task["reasoning_recommendation"].update(
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
                    task["reasoning_approved_at"] = task["reasoning_recommendation"]["approved_at"]
                    task["review"].update(status="reasoning_approved", approved_level=level, approved_by="user")
                    task["status"] = "awaiting_confirmation"
                    task["conversation_state"] = "awaiting_confirmation"
                    add_agent_conversation(task, "lead", "system", f"User approved {level} cluster reasoning")
                elif action == "start_cluster":
                    if not (
                        task.get("cluster_available")
                        and not task.get("cluster_started")
                        and task.get("assistant_replied")
                        and task.get("task_turns", 1) >= 2
                        and task.get("workflow_ready")
                        and not task.get("needs_clarification")
                        and task.get("workflow_confirmed")
                        and task.get("reasoning_proposed")
                        and task.get("reasoning_approved")
                        and task.get("reasoning_recommendation", {}).get("approved")
                    ):
                        self.json_response({"detail": _approval_error(task)}, 409)
                        return
                    task["cluster_started"] = True
                    task["conversation_state"] = "cluster_starting"
                    task["status"] = "queued"
                    task["review"]["status"] = "approved"
                    task["review"]["approved_at"] = utc_timestamp()
                    task["cluster_started_at"] = task["review"]["approved_at"]
                    add_event(task, "cluster_started", "The approved workflow is starting the agent cluster", "lead")
                    add_agent_conversation(task, "lead", "system", "User approved the workflow and reasoning profile")
                    launch_cluster = True
                elif action == "retry":
                    if task["status"] not in ("cancelled", "interrupted"):
                        self.json_response({"detail": "Only cancelled or interrupted tasks can be retried; completed tasks remain read-only"}, 409)
                        return
                    if (
                        not task.get("cluster_available")
                        or task.get("task_turns", 1) < 2
                        or not task.get("workflow_ready")
                        or not task.get("workflow_confirmed")
                        or not task.get("reasoning_proposed")
                        or not task.get("reasoning_approved")
                        or not task.get("reasoning_recommendation", {}).get("approved")
                    ):
                        self.json_response({"detail": _approval_error(task)}, 409)
                        return
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
                    task["cluster_started"] = True
                    task["conversation_state"] = "cluster_starting"
                    task.pop("interrupted_at", None)
                    task.pop("interruption_reason", None)
                    task.pop("retryable", None)
                    CONFIG_SNAPSHOTS[task_id] = CONFIG
                    task["agents"][0].update(status="queued", step="Awaiting task launch", model_name=model_for_tier(CONFIG, CONFIG.max_tier), reasoning_effort=reasoning_for_tier(CONFIG, CONFIG.max_tier, task.get("cluster_reasoning")), weight=int(CONFIG.tier_weights.get(CONFIG.max_tier, 1)), result=None, work_log=[])
                    launch_cluster = True
                elif action:
                    self.json_response({"detail": "Unsupported task action"}, 422)
                    return
                else:
                    self.json_response({"detail": "action is required"}, 422)
                    return
                add_event(task, "control", f"User action: {action}")
                persisted = persist_locked()
                if not persisted:
                    TASKS[task_id] = original_task
                    if original_snapshot is None:
                        CONFIG_SNAPSHOTS.pop(task_id, None)
                    else:
                        CONFIG_SNAPSHOTS[task_id] = original_snapshot
                response = dict(task)
            if not persisted:
                self.persistence_failure()
                return
            if cancel_runtime:
                # Cancellation is an orderly stop, so release the lead slot
                # without counting it as a model failure before stopping the
                # task-owned monitor and removing its registry entry.
                cancel_runtime.finish_work(cancel_agent_id, success=True)
                cancel_runtime.emit_status("cancelled", "任务已取消，集群运行时已停止", task_id=task_id, agent_id=cancel_agent_id)
                _release_mode_runtime(task_id, cancel_runtime)
            if launch_cluster:
                Thread(target=run_swarm, args=(task_id,), daemon=True).start()
            self.json_response(response)
            return
        self.send_error(404)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data-dir", type=Path, default=None)
    args = parser.parse_args()
    load_persisted_state(args.data_dir)
    startup = CLUSTER.status()
    print(f"Orbit Swarm running at http://127.0.0.1:{args.port}")
    print(f"Mode: {startup['mode']} ({startup['mode_label']})")
    print(f"Slots: {startup['active_slots']}/{startup['expected_slots']} active")
    for role, counts in startup.get("role_status", {}).items():
        print(f"Role {role}: {counts['active']}/{counts['max']} active")
    print(f"Persistent state: {STATE_STORE.path}")
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
