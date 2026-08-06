"""Single-task export and continuation helpers for both HTTP entry points."""

from __future__ import annotations

from datetime import datetime
import json
import re
from typing import Any, Iterable
from urllib.parse import quote

from storage import redact_secrets, utc_timestamp

EXPORT_FORMAT = "orbit-swarm-task-export"
EXPORT_VERSION = 1


def _dict_records(values: Any) -> list[dict]:
    return [item for item in values if isinstance(item, dict)] if isinstance(values, list) else []


def _dict_or_empty(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _record(value: Any, allowed: tuple[str, ...]) -> dict:
    return {key: value.get(key) for key in allowed if isinstance(value, dict) and key in value}


def _messages_export(values: Any) -> list[dict]:
    return [_record(item, ("id", "role", "speaker", "content", "agent_id", "time", "timestamp")) for item in _dict_records(values)]


def _events_export(values: Any) -> list[dict]:
    return [_record(item, ("id", "type", "message", "agent_id", "time", "timestamp")) for item in _dict_records(values)]


def _work_log_export(values: Any) -> list[dict]:
    return [_record(item, ("id", "type", "message", "status", "time", "timestamp")) for item in _dict_records(values)]


def _safe_text(value: Any, fallback: str = "") -> str:
    """Avoid serializing arbitrary nested data into attachment/context text."""
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, (int, float, bool)):
        return redact_secrets(str(value))
    return fallback


def _messages(task: dict, role: str | None = None) -> list[dict]:
    values = _dict_records(task.get("conversation") if isinstance(task, dict) else [])
    return [item for item in values if role is None or item.get("role") == role]


def attachment_metadata(values: Any) -> list[dict]:
    output: list[dict] = []
    for item in values if isinstance(values, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            size = max(0, int(item.get("size") or 0))
        except (TypeError, ValueError):
            size = 0
        output.append(
            {
                "name": _safe_text(item.get("name"), "attachment") or "attachment",
                "type": _safe_text(item.get("type"), "application/octet-stream") or "application/octet-stream",
                "size": size,
                "has_content": bool(
                    item.get("content")
                    or item.get("has_content")
                    or item.get("had_content")
                    or item.get("content_unavailable_after_restart")
                ),
            }
        )
    return redact_secrets(output)


def supplemental_context(task: dict) -> str:
    user_messages = _messages(task, "user")
    prompt = _safe_text(task.get("prompt")).strip()
    prompt_index = next(
        (
            index
            for index, item in enumerate(user_messages)
            if str(item.get("content") or "").strip() == prompt
        ),
        0,
    )
    return redact_secrets("\n\n".join(
        _safe_text(item.get("content"))
        for item in user_messages[prompt_index + 1 :]
        if item.get("content")
    ))


def workflow_text(task: dict) -> str:
    if task.get("workflow_summary"):
        return _safe_text(task["workflow_summary"])
    candidates = []
    for message in _messages(task, "assistant"):
        content = _safe_text(message.get("content"))
        lowered = content.lower()
        if "工作流程" in content or "proposed workflow" in lowered or "workflow:" in lowered:
            candidates.append(content)
    return redact_secrets(candidates[-1] if candidates else "")


def continuation_context(source: dict) -> dict:
    conversations = []
    for message in _messages(source)[-8:]:
        speaker = "Orion" if message.get("role") == "assistant" else "User"
        content = " ".join(_safe_text(message.get("content")).split())
        if content:
            conversations.append(f"{speaker}: {content[:700]}")
    return {
        "source_task_id": source.get("id"),
        "source_title": _safe_text(source.get("title")),
        "original_goal": _safe_text(source.get("prompt")),
        "supplemental_context": supplemental_context(source),
        "workflow": workflow_text(source),
        "final_summary": _safe_text(source.get("result")),
        "attachments": attachment_metadata(source.get("attachments")),
        "conversation_summary": "\n".join(conversations),
    }


def continuation_context_text(context: dict) -> str:
    context = redact_secrets(_dict_or_empty(context))
    sections = [
        ("Source task", context.get("source_title")),
        ("Original goal", context.get("original_goal")),
        ("Supplemental constraints and acceptance context", context.get("supplemental_context")),
        ("Confirmed workflow", context.get("workflow")),
        ("Final synthesis", context.get("final_summary")),
        ("Conversation summary", context.get("conversation_summary")),
    ]
    text = "\n\n".join(f"{label}:\n{value}" for label, value in sections if value)
    attachments = context.get("attachments") or []
    if attachments:
        names = ", ".join(str(item.get("name") or "attachment") for item in attachments if isinstance(item, dict))
        text += f"\n\nAttachment metadata:\n{names}"
    return text[:12_000]


def continuation_welcome(source: dict) -> str:
    title = _safe_text(source.get("title"))
    chinese = any("\u4e00" <= character <= "\u9fff" for character in _safe_text(source.get("prompt")) or title)
    if chinese:
        return f"已载入来源任务“{title}”的目标、补充约束、最终汇总和必要对话摘要。请告诉我接下来要继续讨论或深化什么；如需再次启动集群，仍需重新确认工作流程和推理强度。"
    return f"I loaded the goal, constraints, final synthesis, and necessary conversation summary from “{title}”. Tell me what to deepen next; any new cluster still requires a fresh workflow and reasoning review."


def _agent_export(agent: dict, conversations: dict) -> dict:
    agent_id = str(agent.get("id") or "")
    agent_conversations = _messages_export(conversations.get(agent_id))
    return {
        "id": _safe_text(agent_id),
        "name": _safe_text(agent.get("name")),
        "role": _safe_text(agent.get("role")),
        "objective": _safe_text(agent.get("objective")),
        "status": agent.get("status"),
        "step": _safe_text(agent.get("step")),
        "parent_id": agent.get("parent_id"),
        "model_name": _safe_text(agent.get("model_name")),
        "model_tier": agent.get("model_tier"),
        "reasoning_effort": agent.get("reasoning_effort"),
        "executor": agent.get("executor"),
        "weight": agent.get("weight"),
        "conversation_with_orion": agent_conversations,
        "work_log": _work_log_export(agent.get("work_log")),
        "result": _safe_text(agent.get("result")),
    }


def build_export_payload(task: dict, config_snapshot: dict | None = None, secret_values: Iterable[str | None] = ()) -> dict:
    task = _dict_or_empty(task)
    raw_conversations = task.get("agent_conversations")
    conversations = {
        str(agent_id): _messages_export(entries)
        for agent_id, entries in (raw_conversations.items() if isinstance(raw_conversations, dict) else ())
    }
    agents = _dict_records(task.get("agents"))
    lead = next((agent for agent in agents if not agent.get("parent_id")), None) or {}
    snapshot = _dict_or_empty(config_snapshot)
    snapshot_models = _dict_or_empty(snapshot.get("models"))
    snapshot_reasoning = _dict_or_empty(snapshot.get("reasoning"))
    recommendation = _dict_or_empty(task.get("reasoning_recommendation") or task.get("reasoning_profile"))
    difficulty = _dict_or_empty(task.get("difficulty") or task.get("difficulty_assessment"))
    review = _dict_or_empty(task.get("review"))
    payload = {
        "export_format": EXPORT_FORMAT,
        "export_version": EXPORT_VERSION,
        "exported_at": utc_timestamp(),
        "task": {
            "id": _safe_text(task.get("id")),
            "title": _safe_text(task.get("title")),
            "created_at": task.get("created_at"),
            "created_at_iso": task.get("created_at_iso"),
            "original_task": _safe_text(task.get("prompt")),
            "supplemental_context": supplemental_context(task),
            "full_task_context": _safe_text(task.get("prompt_context")),
            "attachments": attachment_metadata(task.get("attachments")),
            "source_task_id": task.get("source_task_id"),
            "parent_task_id": task.get("parent_task_id"),
            "root_task_id": task.get("root_task_id"),
            "continued_task_ids": list(task.get("continued_task_ids") or []) if isinstance(task.get("continued_task_ids"), list) else [],
            "inherited_at": task.get("inherited_at"),
            "inherited_context_summary": _safe_text(task.get("inherited_context_summary")),
            "status": task.get("status"),
            "main_conversation": _messages_export(task.get("conversation")),
            "workflow": {
                "text": workflow_text(task),
                "ready": bool(task.get("workflow_ready")),
                "confirmed": bool(task.get("workflow_confirmed")),
                "confirmed_at": task.get("workflow_confirmed_at"),
            },
            "difficulty_assessment": difficulty,
            "reasoning_review": {
                "recommendation": recommendation,
                "selected_level": task.get("cluster_reasoning"),
                "approved": bool(task.get("reasoning_approved")),
                "approved_at": task.get("reasoning_approved_at") or recommendation.get("approved_at"),
                "review": review,
            },
            "model": {
                "name": lead.get("model_name"),
                "tier": lead.get("model_tier"),
                "reasoning_effort": lead.get("reasoning_effort"),
                "configured_model": _safe_text(snapshot_models.get(str(lead.get("model_tier") or ""))),
                "configured_reasoning": _safe_text(snapshot_reasoning.get(str(lead.get("model_tier") or ""))),
            },
            "agents": [_agent_export(agent, conversations) for agent in agents],
            "orion_agent_conversations": conversations,
            "event_timeline": _events_export(task.get("events")),
            "final_synthesis": _safe_text(task.get("result")),
        },
    }
    return redact_secrets(payload, secret_values)


def _value(value: Any) -> str:
    if value is None or value == "":
        return "-"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2)
    return str(value)


def render_markdown(payload: dict) -> str:
    payload = redact_secrets(_dict_or_empty(payload))
    task = _dict_or_empty(payload.get("task"))
    export_format = payload.get("export_format") or EXPORT_FORMAT
    export_version = payload.get("export_version") or EXPORT_VERSION
    lines = [
        f"# {task.get('title') or 'Orbit Swarm task'}",
        "",
        f"- Export format: `{export_format}` v{export_version}",
        f"- Task ID: `{task.get('id')}`",
        f"- Created: {_value(task.get('created_at_iso') or task.get('created_at'))}",
        f"- Status: {_value(task.get('status'))}",
        f"- Source task ID: {_value(task.get('source_task_id'))}",
        f"- Parent task ID: {_value(task.get('parent_task_id'))}",
        f"- Continued task IDs: {_value(task.get('continued_task_ids'))}",
        "",
        "## Original Task",
        "",
        _value(task.get("original_task")),
        "",
        "## Supplemental Context",
        "",
        _value(task.get("supplemental_context")),
    ]
    if task.get("inherited_context_summary"):
        lines.extend(["", "## Inherited Context", "", _value(task.get("inherited_context_summary"))])

    lines.extend(["", "## Main Conversation", ""])
    for message in _dict_records(task.get("main_conversation")):
        speaker = "Orion" if message.get("role") == "assistant" else "User"
        lines.extend([f"### {speaker} · {_value(message.get('timestamp') or message.get('time'))}", "", _value(message.get("content")), ""])

    workflow = _dict_or_empty(task.get("workflow"))
    lines.extend(["## Confirmed Workflow", "", _value(workflow.get("text")), ""])
    reasoning = _dict_or_empty(task.get("reasoning_review"))
    recommendation = _dict_or_empty(reasoning.get("recommendation"))
    estimate = _dict_or_empty(recommendation.get("estimate"))
    lines.extend(
        [
            "## Reasoning Review",
            "",
            f"- Recommended level: {_value(recommendation.get('recommended_reasoning') or recommendation.get('level'))}",
            f"- User-selected level: {_value(reasoning.get('selected_level'))}",
            f"- Approved: {_value(reasoning.get('approved'))}",
            f"- Approved at: {_value(reasoning.get('approved_at'))}",
            f"- Recommended agents: {_value(estimate.get('recommended_agents') or recommendation.get('recommended_agents'))}",
            f"- Duration: {_value(estimate.get('duration_seconds_min'))}-{_value(estimate.get('duration_seconds_max'))} seconds",
            f"- Input tokens: {_value(estimate.get('input_tokens_min'))}-{_value(estimate.get('input_tokens_max'))}",
            f"- Output tokens: {_value(estimate.get('output_tokens_min'))}-{_value(estimate.get('output_tokens_max'))}",
            f"- Cost: {_value(estimate.get('cost_min'))}-{_value(estimate.get('cost_max'))} {_value(estimate.get('cost_currency'))}" if estimate.get("pricing_configured") else "- Cost: Model pricing is not configured",
            f"- Confidence: {_value(estimate.get('confidence'))}",
            f"- Basis: {_value(estimate.get('basis'))}",
            f"- Uncertainty: {_value(estimate.get('uncertainty'))}",
            "",
        ]
    )

    lines.extend(["## Agents", ""])
    for agent in _dict_records(task.get("agents")):
        lines.extend(
            [
                f"### {agent.get('name') or agent.get('id')}",
                "",
                f"- Role: {_value(agent.get('role'))}",
                f"- Objective: {_value(agent.get('objective'))}",
                f"- Status: {_value(agent.get('status'))}",
                f"- Model: {_value(agent.get('model_name'))} ({_value(agent.get('model_tier'))})",
                f"- Reasoning: {_value(agent.get('reasoning_effort'))}",
                "",
                "#### Conversation with Orion",
                "",
            ]
        )
        for entry in _dict_records(agent.get("conversation_with_orion")):
            lines.append(f"- **{_value(entry.get('speaker') or entry.get('role'))}** ({_value(entry.get('timestamp') or entry.get('time'))}): {_value(entry.get('content'))}")
        lines.extend(["", "#### Work Log", ""])
        for entry in _dict_records(agent.get("work_log")):
            lines.append(f"- {_value(entry.get('timestamp') or entry.get('time'))} · {_value(entry.get('type'))}: {_value(entry.get('message'))}")
        lines.extend(["", "#### Result", "", _value(agent.get("result")), ""])

    lines.extend(["## Event Timeline", ""])
    for event in _dict_records(task.get("event_timeline")):
        lines.append(f"- {_value(event.get('timestamp') or event.get('time'))} · {_value(event.get('type'))}: {_value(event.get('message'))}")
    lines.extend(["", "## Final Synthesis", "", _value(task.get("final_synthesis")), ""])
    return "\n".join(lines).rstrip() + "\n"


def safe_export_filename(
    task: dict,
    extension: str,
    secret_values: Iterable[str | None] = (),
) -> str:
    raw_title = task.get("title") if isinstance(task, dict) else None
    safe_title = redact_secrets(_safe_text(raw_title, "task"), secret_values) or "task"
    title = re.sub(r"[^\w.-]+", "-", safe_title, flags=re.UNICODE).strip("-._")[:64] or "task"
    created = str(task.get("created_at_iso") or task.get("created_at") or datetime.now().isoformat()) if isinstance(task, dict) else datetime.now().isoformat()
    stamp = re.sub(r"\D", "", created)[:14] or datetime.now().strftime("%Y%m%d%H%M%S")
    return f"{title}-{stamp}.{extension}"


def content_disposition(filename: str) -> str:
    ascii_fallback = re.sub(r"[^A-Za-z0-9._-]+", "-", filename).strip("-") or "orbit-task-export"
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(filename)}"
