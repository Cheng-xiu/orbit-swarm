"""Versioned, atomic local persistence for Orbit Swarm."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
from threading import Lock
from typing import Any, Iterable
from uuid import uuid4

STATE_FORMAT = "orbit-swarm-state"
STATE_VERSION = 1
STATE_FILENAME = "orbit-state-v1.json"
INTERRUPTED_TASK_STATUSES = {"queued", "planning", "running", "paused"}
INTERRUPTED_AGENT_STATUSES = {"queued", "planning", "running", "working"}
SECRET_KEYS = {
    "apikey",
    "xapikey",
    "openaiapikey",
    "apikeyvalue",
    "apitoken",
    "apikeyconfigured",
    "apikeyhint",
    "authorization",
    "authorizationheader",
    "authtoken",
    "bearertoken",
    "bearer",
    "accesstoken",
    "refreshtoken",
    "idtoken",
    "tokenvalue",
    "token",
    "secret",
    "secretkey",
    "secretvalue",
    "clientsecret",
    "password",
    "passwd",
    "credential",
    "credentials",
    "cookie",
    "setcookie",
    "proxyauthorization",
    "requestheaders",
}
NON_SECRET_KEYS = {
    "replytoken",
    "lastansweredtoken",
}
_SECRET_VALUE = r'(?:"[^"]*"|\'[^\']*\'|[^\s,;}&]+)'
SECRET_TEXT_PATTERNS = (
    re.compile(
        rf"(?i)(?P<prefix>\bauthorization\b\s*[\"']?\s*[:=]\s*(?:(?:bearer|basic|token)\s+)?)(?P<value>{_SECRET_VALUE})"
    ),
    re.compile(rf"(?i)(?P<prefix>\bauthorization\s+(?:bearer|basic|token)\s+)(?P<value>{_SECRET_VALUE})"),
    re.compile(rf"(?i)(?P<prefix>\bbearer\s+)(?P<value>{_SECRET_VALUE})"),
    re.compile(
        rf"(?i)(?P<prefix>\b(?:api[ _-]?key|api[ _-]?token|access[ _-]?token|refresh[ _-]?token|id[ _-]?token|auth[ _-]?token|client[ _-]?secret|secret|token|password|passwd|credential(?:s)?|set[ _-]?cookie|cookie)\b[\"']?\s*[:=]\s*)(?P<value>{_SECRET_VALUE})"
    ),
)


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def default_state_path(root: Path, data_dir: str | Path | None = None) -> Path:
    configured = data_dir or os.getenv("ORBIT_SWARM_DATA_DIR")
    directory = Path(configured).expanduser() if configured else root / "work" / "state"
    return directory.resolve() / STATE_FILENAME


def _secret_values(values: Iterable[str | None]) -> tuple[str, ...]:
    return tuple(sorted({str(value) for value in values if value and len(str(value)) >= 4}, key=len, reverse=True))


def _normalized_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).strip().lower())


def _is_secret_key(value: Any) -> bool:
    normalized = _normalized_key(value)
    if normalized in NON_SECRET_KEYS:
        return False
    return normalized in SECRET_KEYS or normalized.endswith(
        ("token", "secret", "password", "passwd", "credential", "credentials", "authorization", "cookie")
    )


def _has_status(value: Any, statuses: set[str]) -> bool:
    return isinstance(value, str) and value in statuses


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _redact_text(value: str, secrets: tuple[str, ...]) -> str:
    result = value
    for secret in secrets:
        result = result.replace(secret, "[REDACTED]")
    for pattern in SECRET_TEXT_PATTERNS:
        result = pattern.sub(_redact_secret_match, result)
    return result


def redact_secrets(value: Any, secret_values: Iterable[str | None] = ()) -> Any:
    """Drop known secret fields and replace configured secret values defensively."""
    secrets = _secret_values(secret_values)
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            normalized_key = _normalized_key(key)
            if _is_secret_key(key):
                continue
            safe_key = _redact_text(str(key), secrets)
            if normalized_key in {"attachments", "inheritedattachments", "sourceattachments"}:
                cleaned[safe_key] = _task_attachment_metadata(item, secrets)
            else:
                cleaned[safe_key] = redact_secrets(item, secrets)
        return cleaned
    if isinstance(value, list):
        return [redact_secrets(item, secrets) for item in value]
    if isinstance(value, tuple):
        return [redact_secrets(item, secrets) for item in value]
    if isinstance(value, str):
        return _redact_text(value, secrets)
    return value


def _redact_secret_match(match: re.Match[str]) -> str:
    """Replace both the credential label and its value to avoid export hints."""
    return "[REDACTED]"


def _attachment_record(item: dict, content_key: str, secret_values: Iterable[str | None] = ()) -> dict:
    size = _nonnegative_int(item.get("size"))
    name = item.get("name") if isinstance(item.get("name"), str) else "attachment"
    media_type = item.get("type") if isinstance(item.get("type"), str) else "application/octet-stream"
    return {
        "name": redact_secrets(name, secret_values) or "attachment",
        "type": redact_secrets(media_type, secret_values) or "application/octet-stream",
        "size": size,
        content_key: bool(
            item.get("content")
            or item.get("has_content")
            or item.get("had_content")
            or item.get("content_unavailable_after_restart")
        ),
    }


def _task_attachment_metadata(values: Any, secret_values: Iterable[str | None] = ()) -> list[dict]:
    """Keep only restart-safe attachment metadata embedded in a task."""
    return [
        _attachment_record(item, "has_content", secret_values)
        for item in (values if isinstance(values, list) else [])
        if isinstance(item, dict)
    ]


def persistable_attachments(task_attachments: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """Persist attachment metadata without retaining uploaded file contents."""
    output: dict[str, list[dict]] = {}
    entries = task_attachments.items() if isinstance(task_attachments, dict) else ()
    for task_id, attachments in entries:
        if not isinstance(attachments, list):
            continue
        output[str(task_id)] = [
            _attachment_record(item, "had_content")
            for item in attachments
            if isinstance(item, dict)
        ]
    return output


def restore_attachments(saved: dict | None) -> dict[str, list[dict]]:
    output: dict[str, list[dict]] = {}
    entries = saved.items() if isinstance(saved, dict) else ()
    for task_id, attachments in entries:
        if not isinstance(attachments, list):
            continue
        output[str(task_id)] = []
        for item in attachments:
            if not isinstance(item, dict):
                continue
            metadata = _attachment_record(item, "had_content")
            output[str(task_id)].append(
                {
                    "name": metadata["name"],
                    "type": metadata["type"],
                    "size": metadata["size"],
                    "content": "",
                    "content_unavailable_after_restart": metadata["had_content"],
                }
            )
    return output


def build_state_document(
    tasks: dict[str, dict] | list[dict],
    config: dict,
    config_snapshots: dict[str, dict],
    task_attachments: dict[str, list[dict]],
) -> dict:
    source_tasks = list(tasks.values()) if isinstance(tasks, dict) else list(tasks) if isinstance(tasks, list) else []
    task_values = []
    for source_task in source_tasks:
        if not isinstance(source_task, dict):
            task_values.append(redact_secrets(source_task))
            continue
        task = redact_secrets(source_task)
        task["attachments"] = _task_attachment_metadata(task.get("attachments"))
        if "inherited_attachments" in task:
            task["inherited_attachments"] = _task_attachment_metadata(task.get("inherited_attachments"))
        inherited_context = task.get("inherited_context")
        if isinstance(inherited_context, dict) and "attachments" in inherited_context:
            inherited_context = dict(inherited_context)
            inherited_context["attachments"] = _task_attachment_metadata(inherited_context.get("attachments"))
            task["inherited_context"] = inherited_context
        task_values.append(task)
    return {
        "format": STATE_FORMAT,
        "version": STATE_VERSION,
        "saved_at": utc_timestamp(),
        "config": redact_secrets(config),
        "config_snapshots": redact_secrets(config_snapshots),
        "task_attachments": persistable_attachments(task_attachments),
        "tasks": task_values,
    }


def _minimal_lead(task: dict) -> dict:
    return {
        "id": "lead",
        "name": "Orion",
        "role": "Swarm coordinator",
        "objective": "Clarify, plan, dispatch, and synthesize the task",
        "status": "interrupted" if _has_status(task.get("status"), INTERRUPTED_TASK_STATUSES) else "ready",
        "step": "Recovered with incomplete agent record",
        "parent_id": None,
        "model_tier": "high",
        "model_name": "gpt-5.6-sol",
        "reasoning_effort": "high",
        "weight": 1,
        "executor": "direct_model",
        "result": None,
        "work_log": [],
    }


def _dict_records(value: Any) -> list[dict]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def normalize_loaded_tasks(values: Any) -> tuple[dict[str, dict], bool, list[str]]:
    """Normalize aliases and mark work that cannot survive a process restart."""
    warnings: list[str] = []
    changed = False
    if isinstance(values, dict):
        candidates = list(values.values())
    elif isinstance(values, list):
        candidates = values
    else:
        candidates = []
        if values not in (None, []):
            changed = True
            warnings.append("The persisted task collection was not an object or array.")
    tasks: dict[str, dict] = {}
    for raw in candidates:
        if not isinstance(raw, dict) or not raw.get("id"):
            changed = True
            warnings.append("A persisted task without a valid object ID was skipped.")
            continue
        task = redact_secrets(raw)
        task_id = str(task["id"])
        if task != raw:
            changed = True
            warnings.append(f"Task {task_id} contained sensitive fields or values and they were redacted.")
        if not isinstance(task.get("status"), str):
            # A persisted cluster with an unusable status must take the normal
            # restart-interruption path below instead of becoming actionable.
            task["status"] = "running" if task.get("cluster_started") else "ready"
            changed = True
            warnings.append(f"Task {task_id} had an invalid status and it was reset.")
        for key in ("conversation", "events"):
            raw_records = task.get(key)
            if not isinstance(raw_records, list):
                task[key] = []
                changed = True
                warnings.append(f"Task {task_id} had an invalid {key} collection and it was reset.")
                continue
            records = _dict_records(raw_records)
            task[key] = records
            if len(records) != len(raw_records):
                changed = True
                warnings.append(f"Task {task_id} contained invalid {key} records and they were skipped.")

        raw_continued_ids = task.get("continued_task_ids")
        if not isinstance(raw_continued_ids, list):
            task["continued_task_ids"] = []
            changed = True
            warnings.append(f"Task {task_id} had an invalid continued_task_ids collection and it was reset.")

        raw_attachments = task.get("attachments")
        attachments = _task_attachment_metadata(raw_attachments)
        task["attachments"] = attachments
        if not isinstance(raw_attachments, list) or attachments != raw_attachments:
            changed = True
            warnings.append(f"Task {task_id} had attachment data that was reduced to safe metadata.")

        raw_agent_conversations = task.get("agent_conversations")
        if not isinstance(raw_agent_conversations, dict):
            raw_agent_conversations = {}
            changed = True
            warnings.append(f"Task {task_id} had invalid agent conversations and they were reset.")
        agent_conversations: dict[str, list] = {}
        for agent_id, entries in raw_agent_conversations.items():
            if isinstance(entries, list):
                records = _dict_records(entries)
                agent_conversations[str(agent_id)] = records
                if len(records) != len(entries):
                    changed = True
                    warnings.append(f"Task {task_id} had invalid conversation records for agent {agent_id}.")
            else:
                agent_conversations[str(agent_id)] = []
                changed = True
                warnings.append(f"Task {task_id} had an invalid conversation group for agent {agent_id}.")
        agent_conversations.setdefault("lead", [])
        task["agent_conversations"] = agent_conversations

        raw_agents = task.get("agents")
        if not isinstance(raw_agents, list):
            raw_agents = []
            changed = True
            warnings.append(f"Task {task_id} had an invalid agent collection and it was rebuilt.")
        agents = [agent for agent in raw_agents if isinstance(agent, dict)]
        if len(agents) != len(raw_agents):
            changed = True
            warnings.append(f"Task {task_id} contained invalid agent records and they were skipped.")
        lead_index = next(
            (index for index, agent in enumerate(agents) if agent.get("id") == "lead" and not agent.get("parent_id")),
            None,
        )
        if lead_index is None:
            agents.insert(0, _minimal_lead(task))
            changed = True
            warnings.append(f"Task {task_id} was missing its Orion record and a minimal one was rebuilt.")
        elif lead_index:
            agents.insert(0, agents.pop(lead_index))
            changed = True
            warnings.append(f"Task {task_id} had a displaced Orion record and it was restored to the lead position.")
        for agent in agents:
            if not isinstance(agent.get("status"), str):
                agent["status"] = "interrupted" if task.get("cluster_started") else "ready"
                changed = True
                warnings.append(f"Task {task_id} had an agent with an invalid status and it was reset.")
            raw_work_log = agent.get("work_log")
            if not isinstance(raw_work_log, list):
                agent["work_log"] = []
                changed = True
                warnings.append(f"Task {task_id} had an invalid work log and it was reset.")
                continue
            work_log = _dict_records(raw_work_log)
            agent["work_log"] = work_log
            if len(work_log) != len(raw_work_log):
                changed = True
                warnings.append(f"Task {task_id} contained invalid work-log records and they were skipped.")
        task["agents"] = agents
        for key, default in (
            ("source_task_id", None),
            ("parent_task_id", None),
            ("root_task_id", task.get("source_task_id") or task.get("id")),
        ):
            if key not in task:
                task[key] = default
                changed = True
        # MODE was introduced after the v1 task format.  Old tasks remain
        # compatibility-mode single-agent records, while new mode metadata is
        # always present for the front-end status panel.
        raw_mode = task.get("mode", 0)
        try:
            normalized_mode = max(0, min(3, int(raw_mode)))
        except (TypeError, ValueError):
            normalized_mode = 0
        if task.get("mode") != normalized_mode:
            task["mode"] = normalized_mode
            changed = True
        labels = {0: "单Agent模式", 1: "中档模式", 2: "高档模式", 3: "极限模式"}
        if task.get("mode_label") != labels[normalized_mode]:
            task["mode_label"] = labels[normalized_mode]
            changed = True
        if "mode_managed" not in task:
            task["mode_managed"] = False
            changed = True
        recommendation = task.get("reasoning_recommendation") or task.get("reasoning_profile") or {}
        difficulty = task.get("difficulty") or task.get("difficulty_assessment") or {}
        if not isinstance(recommendation, dict):
            recommendation = {}
            changed = True
        if not isinstance(difficulty, dict):
            difficulty = {}
            changed = True
        task["reasoning_recommendation"] = recommendation
        task["reasoning_profile"] = recommendation
        task["difficulty"] = difficulty
        task["difficulty_assessment"] = difficulty
        if not isinstance(task.get("review"), dict):
            task["review"] = {"required": bool(task.get("cluster_available")), "status": "pending", "approved_at": None, "approved_by": None}
            changed = True
            warnings.append(f"Task {task_id} had an invalid reasoning review and it was reset.")

        # Older mode-managed tasks could write the final synthesis before
        # updating the reply cursor.  On the next boot that stale cursor looks
        # like an in-flight reply and the restart recovery path incorrectly
        # downgrades an already-complete task to ``interrupted``.  A durable
        # result plus a complete event is stronger evidence than that cursor;
        # repair the legacy snapshot before evaluating pending work.
        complete_event = any(
            isinstance(event, dict) and event.get("type") == "complete"
            for event in task.get("events", [])
        )
        completed_snapshot = bool(
            str(task.get("result") or "").strip()
            and task.get("completed_at")
            and complete_event
        )
        if completed_snapshot:
            was_interrupted = task.get("status") == "interrupted"
            task_changed = False
            if task.get("status") != "complete":
                task["status"] = "complete"
                task_changed = True
            if task.get("conversation_state") != "complete":
                task["conversation_state"] = "complete"
                task_changed = True
            if task.get("assistant_replied") is not True:
                task["assistant_replied"] = True
                task_changed = True
            reply_token = _nonnegative_int(task.get("reply_token"))
            if _nonnegative_int(task.get("last_answered_token")) < reply_token:
                task["last_answered_token"] = reply_token
                task_changed = True
            if task.get("retryable") is not False:
                task["retryable"] = False
                task_changed = True
            if was_interrupted:
                task.pop("interrupted_at", None)
                task.pop("interruption_reason", None)
                for agent in task["agents"]:
                    if isinstance(agent, dict) and agent.get("id") == "lead" and agent.get("status") == "interrupted":
                        agent.update(status="complete", step="Recovered completed synthesis")
                task_changed = True
            if task_changed:
                changed = True
                if was_interrupted:
                    warnings.append(f"Task {task_id} was restored from a stale restart interruption after completion.")

        interruption_recorded = task.get("status") == "interrupted" and task.get("conversation_state") == "interrupted"
        pending_reply = not interruption_recorded and bool(
            task.get("conversation_state") == "responding"
            or (
                task.get("assistant_replied") is False
                and _nonnegative_int(task.get("last_answered_token")) < _nonnegative_int(task.get("reply_token"))
            )
        )
        if _has_status(task.get("status"), INTERRUPTED_TASK_STATUSES) or pending_reply:
            changed = True
            interrupted_at = utc_timestamp()
            previous_status = str(task.get("status"))
            reason = (
                "Orion's reply was interrupted by a service restart. Send a new message to retry."
                if pending_reply and not task.get("cluster_started")
                else "Execution was interrupted by a service restart."
            )
            task.update(
                status="interrupted",
                conversation_state="interrupted",
                retryable=True,
                interrupted_at=interrupted_at,
                interruption_reason=reason,
            )
            task["events"].append(
                {
                    "id": str(uuid4()),
                    "time": interrupted_at[11:19],
                    "timestamp": interrupted_at,
                    "type": "execution_interrupted",
                    "message": f"Service restarted while task status was {previous_status}; no replies or agents were restarted automatically.",
                    "agent_id": "lead",
                }
            )
            for agent in task["agents"]:
                if isinstance(agent, dict) and (
                    _has_status(agent.get("status"), INTERRUPTED_AGENT_STATUSES)
                    or (pending_reply and agent.get("id") == "lead")
                ):
                    agent.update(status="interrupted", step="Execution interrupted by service restart")
        tasks[task_id] = task
    return tasks, changed, warnings


class AtomicJsonStateStore:
    """Small JSON store with same-directory temporary writes and recovery status."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self._write_lock = Lock()
        self.last_saved_at: str | None = None
        self.last_error: str | None = None
        self.recovery_warning: str | None = None
        self.recovered_backup: str | None = None
        self.load_state = "not_loaded"
        self.migration_required = False

    def status(self) -> dict:
        return {
            "enabled": True,
            "path": str(self.path),
            "load_state": self.load_state,
            "last_saved_at": self.last_saved_at,
            "error": self.last_error,
            "warning": self.recovery_warning,
            "recovered_backup": self.recovered_backup,
            "format": STATE_FORMAT,
            "version": STATE_VERSION,
            "migration_required": self.migration_required,
        }

    def _empty(self) -> dict:
        return build_state_document({}, {}, {}, {})

    def preserve_for_recovery(self, message: str) -> None:
        """Preserve the current file and expose a recoverable warning."""
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup = self.path.with_name(f"{self.path.stem}.corrupt-{stamp}{self.path.suffix}")
        try:
            if self.path.exists():
                os.replace(self.path, backup)
                self.recovered_backup = str(backup)
        except OSError:
            self.recovered_backup = None
        suffix = f" Preserved as {backup.name}." if self.recovered_backup else " The original file could not be moved."
        self.recovery_warning = f"{message}.{suffix}"
        self.load_state = "recovered"

    def load(self) -> dict:
        if not self.path.exists():
            self.load_state = "empty"
            return self._empty()
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(document, dict):
                raise ValueError("state root must be a JSON object")
            version = int(document.get("version", 0))
            if version not in {0, STATE_VERSION}:
                raise ValueError(f"unsupported state version {version}")
            self.migration_required = version != STATE_VERSION or document.get("format") != STATE_FORMAT
            document.setdefault("format", STATE_FORMAT)
            document["version"] = STATE_VERSION
            for key, default in (("config", {}), ("config_snapshots", {}), ("task_attachments", {}), ("tasks", [])):
                if key not in document:
                    document[key] = default
                    self.migration_required = True
            self.last_saved_at = document.get("saved_at")
            self.load_state = "loaded"
            return document
        except (OSError, TypeError, ValueError, OverflowError, RecursionError, json.JSONDecodeError) as error:
            self.preserve_for_recovery(f"Could not load persisted state: {error}")
            return self._empty()

    def save(self, document: dict, secret_values: Iterable[str | None] = ()) -> bool:
        temp_path: Path | None = None
        with self._write_lock:
            try:
                cleaned = redact_secrets(document, secret_values)
                if not isinstance(cleaned, dict):
                    raise TypeError("state document must be an object")
                cleaned["format"] = STATE_FORMAT
                cleaned["version"] = STATE_VERSION
                cleaned["saved_at"] = utc_timestamp()
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    newline="\n",
                    delete=False,
                    dir=self.path.parent,
                    prefix=self.path.name + ".",
                    suffix=".tmp",
                ) as handle:
                    json.dump(cleaned, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                    temp_path = Path(handle.name)
                os.replace(temp_path, self.path)
                self.last_saved_at = cleaned["saved_at"]
                self.last_error = None
                self.migration_required = False
                if self.load_state == "not_loaded":
                    self.load_state = "saved"
                return True
            except (OSError, TypeError, ValueError, OverflowError, RecursionError) as error:
                self.last_error = f"Could not persist state: {error}"
                return False
            finally:
                if temp_path and temp_path.exists():
                    try:
                        temp_path.unlink()
                    except OSError:
                        pass
