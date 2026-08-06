"""Deterministic natural-language edits for the role composition contract.

This module deliberately recognizes a small, explicit command vocabulary.  It
does not call a model, so a role-setting sentence from the only chat box can
be applied consistently by both HTTP servers even when all providers run in
simulation mode.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

try:  # Package imports for tests/tools; script imports for server_stdlib.py.
    from .cluster import configured_role_specs, parse_mode, role_catalog
except ImportError:  # pragma: no cover - stdlib server script path.
    from cluster import configured_role_specs, parse_mode, role_catalog


_MODE_RE = re.compile(r"(?:\u6a21\u5f0f|mode)\s*([0-3\u96f6\u4e00\u4e8c\u4e09])", re.IGNORECASE)
_COUNT_RE = re.compile(r"(?:\u6539\u6210|\u6539\u4e3a|\u8bbe\u4e3a|\u8bbe\u7f6e\u4e3a|\u8c03\u6574\u4e3a)?\s*(\d{1,3})\s*(?:\u4e2a|\u540d|\u4eba|\u4e2a\u69fd\u4f4d)")
_MODEL_RE = re.compile(
    r"(?:\u6a21\u578b|model)\s*(?:\u4e3a|\u662f|\u6539\u6210|\u6539\u4e3a|\u4f7f\u7528)?\s*[:\uff1a]?\s*([^\uff0c,\u3002\uff1b;\n]+)",
    re.IGNORECASE,
)
_PROVIDER_RE = re.compile(
    r"(?:\u4f9b\u5e94\u5546|\u63d0\u4f9b\u5546|API\s*\u63a5\u53e3|\u63a5\u53e3|provider)\s*(?:\u4e3a|\u662f|\u6539\u6210|\u6539\u4e3a|\u4f7f\u7528)?\s*[:\uff1a]?\s*([A-Za-z0-9_.-]+)",
    re.IGNORECASE,
)

_CHINESE_NUMBERS = {"\u96f6": 0, "\u4e00": 1, "\u4e8c": 2, "\u4e09": 3}
_EXECUTORS = (
    ("claude_code", ("claude code", "claudecode", "claude-code")),
    ("openclaw", ("openclaw", "open claw", "open-claw")),
    ("direct_model", ("direct model", "direct_model", "\u76f4\u63a5\u6a21\u578b", "\u76f4\u63a5\u8c03\u7528")),
    ("codex", ("codex",)),
)


@dataclass(frozen=True)
class AgentProfileCommand:
    """A fully normalized edit suitable for ``updated_config``."""

    mode: int
    payload: dict[str, Any]
    confirmation: str


def _mode_from_text(text: str) -> int | None:
    match = _MODE_RE.search(text)
    if not match:
        return None
    value = match.group(1)
    return _CHINESE_NUMBERS.get(value, parse_mode(value))


def _executor_from_text(text: str) -> str | None:
    lowered = text.casefold()
    for executor, aliases in _EXECUTORS:
        if any(alias in lowered for alias in aliases):
            return executor
    return None


def _model_from_text(text: str) -> str | None:
    match = _MODEL_RE.search(text)
    if not match:
        return None
    value = match.group(1).strip()
    # Avoid swallowing a later executor clause in sentences without commas.
    value = re.split(r"\s*(?:\u4f7f\u7528|\u6267\u884c\u5668|executor)\s*", value, maxsplit=1, flags=re.IGNORECASE)[0].strip()
    return value[:200] or None


def _profile_entries(config: Any, mode: int) -> list[dict[str, Any]]:
    """Project the current effective mode catalog into editable dictionaries."""
    entries: list[dict[str, Any]] = []
    for spec in configured_role_specs(mode, config):
        entries.append({
            "role": spec.role,
            "max_count": spec.max_count,
            "provider_id": spec.provider_id or "",
            "model": spec.model,
            "executor": spec.executor,
            "pool": spec.pool,
        })
    return entries


def _mentioned_roles(text: str) -> list[str]:
    """Keep catalog order stable while matching all explicit role names."""
    found: list[str] = []
    for item in role_catalog():
        role = str(item["role"])
        if role in text:
            found.append(role)
    return found


def _new_entry(item: dict[str, Any]) -> dict[str, Any]:
    """Create a role row using the product's researched safe defaults."""
    return {
        "role": str(item["role"]),
        "max_count": int(item["max_count"]),
        "provider_id": "",
        "model": str(item["model"]),
        "executor": str(item.get("executor") or "direct_model"),
        "pool": str(item["pool"]),
    }


def parse_agent_profile_command(text: str, config: Any) -> AgentProfileCommand | None:
    """Parse an explicit role-configuration sentence, or return ``None``.

    Supported forms intentionally cover the UI's natural-language workflow:
    keep-only a mode's roles; edit one role's slot count; and select its
    executor, model, or provider.  Ordinary user prompts are ignored.
    """
    original = str(text or "").strip()
    if not original:
        return None
    mode = _mode_from_text(original)
    if mode is None:
        return None
    roles = _mentioned_roles(original)
    lowered = original.casefold()
    keep_only = "\u53ea\u4fdd\u7559" in original or "\u4ec5\u4fdd\u7559" in original or "keep only" in lowered
    replace_roles = keep_only or any(token in original for token in ("\u5c97\u4f4d\u8bbe\u4e3a", "\u5c97\u4f4d\u8bbe\u7f6e\u4e3a", "\u5c97\u4f4d\u6539\u4e3a", "\u5c97\u4f4d\u6539\u6210"))
    clear_roles = any(token in original for token in ("\u6e05\u7a7a\u5c97\u4f4d", "\u4e0d\u4fdd\u7559\u4efb\u4f55\u5c97\u4f4d", "\u7981\u7528\u5168\u90e8\u5c97\u4f4d", "\u4e0d\u8981\u4efb\u4f55\u5c97\u4f4d")) or "clear roles" in lowered
    add_roles = any(token in original for token in ("\u6dfb\u52a0", "\u589e\u52a0", "\u52a0\u5165", "\u542f\u7528")) or "add role" in lowered
    remove_roles = any(token in original for token in ("\u79fb\u9664", "\u5220\u9664", "\u53bb\u6389", "\u505c\u7528")) or "remove role" in lowered
    executor = _executor_from_text(original)
    model = _model_from_text(original)
    provider_match = _PROVIDER_RE.search(original)
    provider_id = provider_match.group(1).strip() if provider_match else None
    count_match = _COUNT_RE.search(original)
    count = int(count_match.group(1)) if count_match else None

    if clear_roles:
        confirmation = f"\u6a21\u5f0f{mode}\u7684\u5c97\u4f4d\u5df2\u6e05\u7a7a\u3002\u65b0\u4efb\u52a1\u5c06\u6682\u505c\u542f\u52a8\u8be5\u6a21\u5f0f\u7684\u5b50\u667a\u80fd\u4f53\u3002"
        return AgentProfileCommand(mode, {"profile_mode": mode, "roles": []}, confirmation)

    if replace_roles:
        if not roles:
            return None
        catalog = {str(item["role"]): item for item in role_catalog()}
        selected = [_new_entry(catalog[role]) for role in roles]
        confirmation = f"模式{mode}已只保留：{'、'.join(roles)}。岗位集群已按新配置重建。"
        return AgentProfileCommand(mode, {"profile_mode": mode, "roles": selected}, confirmation)

    if (add_roles or remove_roles) and roles:
        entries = _profile_entries(config, mode)
        catalog = {str(item["role"]): item for item in role_catalog()}
        if remove_roles:
            removed = set(roles)
            entries = [item for item in entries if item["role"] not in removed]
            confirmation = f"\u6a21\u5f0f{mode}\u5df2\u79fb\u9664\uff1a{'\u3001'.join(roles)}\u3002\u5c97\u4f4d\u96c6\u7fa4\u5df2\u6309\u65b0\u914d\u7f6e\u91cd\u5efa\u3002"
            return AgentProfileCommand(mode, {"profile_mode": mode, "roles": entries}, confirmation)
        existing_roles = {item["role"] for item in entries}
        added: list[dict[str, Any]] = []
        for role in roles:
            if role not in existing_roles:
                entry = _new_entry(catalog[role])
                entries.append(entry)
                added.append(entry)
                existing_roles.add(role)
        if len(roles) == 1:
            target = next(item for item in entries if item["role"] == roles[0])
            if count is not None:
                target["max_count"] = max(1, min(100, count))
            if executor:
                target["executor"] = executor
            if model:
                target["model"] = model
            if provider_id:
                target["provider_id"] = provider_id
        confirmation = f"\u6a21\u5f0f{mode}\u5df2\u6dfb\u52a0\u6216\u66f4\u65b0\uff1a{'\u3001'.join(roles)}\u3002\u5c97\u4f4d\u96c6\u7fa4\u5df2\u6309\u65b0\u914d\u7f6e\u91cd\u5efa\u3002"
        return AgentProfileCommand(mode, {"profile_mode": mode, "roles": entries}, confirmation)

    # Editing needs exactly one explicitly named role and at least one setting.
    if len(roles) != 1 or not any((executor, model, provider_id, count is not None)):
        return None
    target = roles[0]
    entries = _profile_entries(config, mode)
    existing = next((item for item in entries if item["role"] == target), None)
    if existing is None:
        catalog = {str(item["role"]): item for item in role_catalog()}
        base = catalog[target]
        existing = _new_entry(base)
        entries.append(existing)
    changes: list[str] = []
    if count is not None:
        existing["max_count"] = max(1, min(100, count))
        changes.append(f"数量为{existing['max_count']}个")
    if executor:
        existing["executor"] = executor
        changes.append(f"执行器为{executor}")
    if model:
        existing["model"] = model
        changes.append(f"模型为{model}")
    if provider_id:
        existing["provider_id"] = provider_id
        changes.append(f"供应商为{provider_id}")
    confirmation = f"模式{mode}的{target}已更新：{'，'.join(changes)}。岗位集群已按新配置重建。"
    return AgentProfileCommand(mode, {"profile_mode": mode, "roles": entries}, confirmation)


__all__ = ["AgentProfileCommand", "parse_agent_profile_command"]
