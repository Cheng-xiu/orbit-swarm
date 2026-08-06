"""Safe executor selection for the local swarm control plane."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

try:  # Support both ``backend.executors`` and the existing script imports.
    from .providers import (
        PROTOCOL_ANTHROPIC,
        PROTOCOL_CLI,
        PROTOCOL_OPENAI,
        ProviderRegistry,
        ProviderProfile,
        ProviderSpec,
        ResolvedRoute,
        RouteSpec,
        load_registry,
        registry_from_runtime,
    )
except ImportError:  # pragma: no cover - exercised by the stdlib server.
    from providers import (
        PROTOCOL_ANTHROPIC,
        PROTOCOL_CLI,
        PROTOCOL_OPENAI,
        ProviderRegistry,
        ProviderProfile,
        ProviderSpec,
        ResolvedRoute,
        RouteSpec,
        load_registry,
        registry_from_runtime,
    )

MAX_ATTACHMENTS = 5
MAX_ATTACHMENT_CONTENT = 250_000
MAX_TOTAL_ATTACHMENT_CONTENT = 800_000

# The coordinator follows this contract before it is allowed to fan out. It is
# kept as data on each task as well as in this module so a real model-backed
# coordinator can use the same instructions later.
COORDINATOR_INSTRUCTIONS = (
    "You are Orion, the swarm coordinator. First understand the request and ask "
    "direct questions about the goal, scope, deliverable, constraints, acceptance "
    "criteria, and priority whenever anything is unclear. After the user replies, "
    "restate the agreed task and proposed workflow, then wait for explicit workflow "
    "confirmation. Only after that confirmation, assess task difficulty and propose "
    "one total reasoning intensity with a short rationale, then wait for explicit "
    "reasoning approval. Never dispatch agents because a cluster toggle is enabled. "
    "Never fan out for a greeting, simple chat, a task that does not benefit from "
    "parallel work, or before the clarification, workflow, and reasoning reviews are "
    "complete. Ask the user promptly whenever a material uncertainty remains."
)

# Keep the tier vocabulary in one place so both HTTP entry points expose and
# execute the same configuration contract. ``ultra`` is the compatibility name
# for the highest model tier. Its default deliberately reuses the configured
# high-tier model, so the highest reasoning level never invents an unknown
# provider model.
MODEL_TIERS = ("low", "medium", "high", "ultra")
REASONING_LEVELS = ("minimal", "low", "medium", "high", "xhigh")
TIER_RANK = {tier: index for index, tier in enumerate(MODEL_TIERS)}
REASONING_RANK = {level: index for index, level in enumerate(REASONING_LEVELS)}
DEFAULT_MODELS = {
    "low": "gpt-5.6-luna",
    "medium": "gpt-5.6-terra",
    "high": "gpt-5.6-sol",
    "ultra": "gpt-5.6-sol",
}
DEFAULT_REASONING = {"low": "low", "medium": "medium", "high": "high", "ultra": "xhigh"}
DEFAULT_TIER_WEIGHTS = {"low": 1, "medium": 2, "high": 5, "ultra": 8}


def _coerce_reasoning(value, fallback: str = "high") -> str:
    normalized = str(value or fallback).strip().lower()
    return normalized if normalized in REASONING_LEVELS else fallback


def _mapping_value(mapping: dict | None, key: str, fallback):
    value = (mapping or {}).get(key, fallback)
    return value if value not in (None, "") else fallback


def model_for_tier(config: "RuntimeConfig", tier: str) -> str:
    """Resolve a model while tolerating configs created by older callers."""
    normalized = tier if tier in MODEL_TIERS else "high"
    return str(_mapping_value(config.models, normalized, DEFAULT_MODELS[normalized]))


def provider_registry(config: "RuntimeConfig") -> ProviderRegistry:
    """Return the routing snapshot for a config, including legacy configs."""
    return registry_from_runtime(config)


def resolve_route(
    config: "RuntimeConfig",
    tier: str = "high",
    *,
    role: str | None = None,
    pool: str | None = None,
    model_id: str | None = None,
    task_override: dict | None = None,
    executor: str | None = None,
) -> ResolvedRoute | None:
    """Resolve a task route without changing the legacy tier projection."""
    registry = provider_registry(config)
    return registry.resolve(
        tier,
        role=role,
        pool=pool,
        model_id=model_id,
        task_override=task_override,
        executor=executor,
        simulation_override=getattr(config, "simulation", None),
    )


def route_public(route: ResolvedRoute | None) -> dict:
    """Return a redacted route snapshot suitable for task JSON."""
    return route.public() if route else {}


def safe_registry(config: "RuntimeConfig") -> dict:
    """Return redacted provider/route metadata for HTTP responses."""
    return provider_registry(config).public(simulation_override=config.simulation)


def secret_values(config: "RuntimeConfig") -> list[str]:
    """Collect configured provider keys for storage and export redaction."""
    registry = provider_registry(config)
    values = [str(getattr(config, "api_key", "") or "")]
    values.extend(str(provider.effective_key(registry.env) or "") for provider in registry.providers.values())
    return list(dict.fromkeys(value for value in values if value))


def reasoning_for_tier(config: "RuntimeConfig", tier: str, cluster_reasoning: str | None = None) -> str:
    """Return the effective effort for an agent in a task snapshot."""
    normalized = tier if tier in MODEL_TIERS else "high"
    configured = _coerce_reasoning(_mapping_value(config.reasoning, normalized, DEFAULT_REASONING[normalized]), DEFAULT_REASONING[normalized])
    if cluster_reasoning is None:
        return configured
    # A reviewed cluster profile is an explicit user decision and therefore
    # applies uniformly to the lead and every worker, even when it is lower
    # than that tier's default profile.
    return _coerce_reasoning(cluster_reasoning, configured)


class ExecutorKind(StrEnum):
    DIRECT = "direct_model"
    CODEX = "codex"
    OPENCLAW = "openclaw"
    CLAUDE_CODE = "claude_code"


def normalize_attachments(values) -> list[dict]:
    """Validate attachment JSON and cap text retained in server memory."""
    if not isinstance(values, list):
        return []
    attachments = []
    total_content = 0
    for raw in values[:MAX_ATTACHMENTS]:
        if not isinstance(raw, dict):
            continue
        name = Path(str(raw.get("name", "attachment"))).name.strip()[:180] or "attachment"
        media_type = str(raw.get("type", "application/octet-stream")).strip()[:120]
        try:
            size = max(0, min(100_000_000, int(raw.get("size", 0))))
        except (TypeError, ValueError):
            size = 0
        content = raw.get("content", "")
        content = content if isinstance(content, str) else ""
        remaining = max(0, MAX_TOTAL_ATTACHMENT_CONTENT - total_content)
        content = content[:min(MAX_ATTACHMENT_CONTENT, remaining)]
        total_content += len(content)
        attachments.append({"name": name, "type": media_type, "size": size, "content": content})
    return attachments


def public_attachments(values: list[dict]) -> list[dict]:
    return [{"name": item["name"], "type": item["type"], "size": item["size"], "has_content": bool(item["content"])} for item in values]


def attachment_prompt(values: list[dict]) -> str:
    if not values:
        return ""
    blocks = []
    for item in values:
        header = f"Attachment: {item['name']} ({item['type']}, {item['size']} bytes)"
        blocks.append(header + ("\n" + item["content"] if item["content"] else "\n[Binary file metadata only]"))
    return "\n\nAttached files:\n\n" + "\n\n---\n\n".join(blocks)


@dataclass(frozen=True)
class RuntimeConfig:
    provider_name: str | None
    base_url: str | None
    api_key: str | None
    models: dict[str, str]
    reasoning: dict[str, str]
    tier_weights: dict[str, int]
    max_weight: int
    simulation: bool
    # A cluster-wide reasoning profile.  Per-tier profiles remain available
    # for agents that need a deliberate override, while this value is the
    # user-approved default for the whole fan-out.
    cluster_reasoning: str = "high"
    input_price_per_million: float | None = None
    output_price_per_million: float | None = None
    pricing_currency: str = "USD"
    # Persisted external MODE selector.  It remains last so older positional
    # RuntimeConfig construction keeps working.
    mode: int = 0
    # Additive provider/route snapshot.  This field is last on purpose: older
    # callers may still construct RuntimeConfig positionally with the v1
    # fields above.  Credentials remain in memory and are never serialized by
    # persistable_config().
    provider_registry: ProviderRegistry | None = None
    # Per-mode role composition and explicit role routes.  ``None`` means use
    # the historical built-in catalog, which preserves old state documents.
    agent_profiles: dict | None = None

    @property
    def max_tier(self) -> str:
        return MODEL_TIERS[-1]

    @classmethod
    def from_environment(cls) -> "RuntimeConfig":
        base_url = os.getenv("A6_OPENAI_BASE_URL") or os.getenv("OPENAI_BASE_URL")
        api_key = os.getenv("A6_OPENAI_API_KEY") or os.getenv("A6API_API_KEY") or os.getenv("OPENAI_API_KEY")

        # The user explicitly opted into reusing the local OpenClaw A6 provider.
        # Values stay in memory and are never returned by the API or written here.
        if not (base_url and api_key):
            config_path = Path(os.getenv("OPENCLAW_CONFIG_PATH", Path.home() / ".openclaw" / "openclaw.json"))
            try:
                document = json.loads(config_path.read_text(encoding="utf-8"))
                provider = document.get("models", {}).get("providers", {}).get("a6api", {})
                base_url = base_url or provider.get("baseUrl")
                api_key = api_key or provider.get("apiKey")
            except (OSError, ValueError, TypeError):
                pass

        def env_text(*names: str, default: str) -> str:
            for name in names:
                value = os.getenv(name)
                if value is not None and str(value).strip():
                    return str(value).strip()
            return default

        def positive_int(name: str, default: int, maximum: int = 1000) -> int:
            try:
                return max(1, min(maximum, int(os.getenv(name, str(default)))))
            except (TypeError, ValueError):
                return default

        def optional_price(name: str) -> float | None:
            raw = os.getenv(name)
            if raw is None or not raw.strip():
                return None
            try:
                return max(0.0, min(1_000_000.0, float(raw)))
            except (TypeError, ValueError):
                return None

        legacy_max_weight = positive_int("SWARM_MAX_CONCURRENCY", 30, 1000)
        try:
            raw_mode = next((os.getenv(name) for name in ("ORBIT_MODE", "ORBIT_SWARM_MODE", "SWARM_MODE", "MODE") if os.getenv(name) is not None), "0")
            if isinstance(raw_mode, str):
                normalized_mode = raw_mode.strip().lower()
                if normalized_mode.startswith("mode"):
                    normalized_mode = normalized_mode[4:].strip(" _-")
                raw_mode = {"single": 0, "mid": 1, "medium": 1, "high": 2, "extreme": 3, "max": 3}.get(normalized_mode, normalized_mode)
            mode = max(0, min(3, int(raw_mode)))
        except (TypeError, ValueError):
            mode = 0
        config = cls(
            provider_name=env_text("A6_PROVIDER_NAME", default="a6api"),
            base_url=base_url.rstrip("/") if base_url else None,
            api_key=api_key,
            models={tier: env_text(f"A6_MODEL_{tier.upper()}", default=DEFAULT_MODELS[tier]) for tier in MODEL_TIERS},
            reasoning={
                tier: _coerce_reasoning(env_text(f"A6_REASONING_{tier.upper()}", default=DEFAULT_REASONING[tier]), DEFAULT_REASONING[tier])
                for tier in MODEL_TIERS
            },
            tier_weights={tier: positive_int(f"SWARM_WEIGHT_{tier.upper()}", DEFAULT_TIER_WEIGHTS[tier], 1000) for tier in MODEL_TIERS},
            max_weight=positive_int("SWARM_MAX_WEIGHT", legacy_max_weight, 1000),
            simulation=os.getenv("SWARM_SIMULATION", "true").lower() not in {"0", "false", "no"},
            cluster_reasoning=_coerce_reasoning(
                env_text("SWARM_CLUSTER_REASONING", "SWARM_TOTAL_REASONING", "A6_CLUSTER_REASONING", default="high"),
                "high",
            ),
            input_price_per_million=optional_price("A6_INPUT_PRICE_PER_MILLION"),
            output_price_per_million=optional_price("A6_OUTPUT_PRICE_PER_MILLION"),
            pricing_currency=env_text("A6_PRICING_CURRENCY", default="USD")[:12].upper(),
            mode=mode,
        )
        # Construct the registry after the legacy projection exists.  Named
        # providers are opt-in and malformed declarations are ignored by the
        # registry, preserving the old startup behavior.
        return replace(
            config,
            provider_registry=ProviderRegistry.from_environment(
                legacy={
                    "provider_name": config.provider_name,
                    "base_url": config.base_url,
                    "api_key": config.api_key,
                    "models": config.models,
                    "simulation": config.simulation,
                }
            ),
        )


def public_config(config: RuntimeConfig) -> dict:
    """Return settings safe to send to the browser; never include the API key."""
    models = {tier: model_for_tier(config, tier) for tier in MODEL_TIERS}
    reasoning = {
        tier: _coerce_reasoning(_mapping_value(config.reasoning, tier, DEFAULT_REASONING[tier]), DEFAULT_REASONING[tier])
        for tier in MODEL_TIERS
    }
    tier_weights = {
        tier: max(1, int(_mapping_value(config.tier_weights, tier, DEFAULT_TIER_WEIGHTS[tier])))
        for tier in MODEL_TIERS
    }
    cluster_reasoning = _coerce_reasoning(getattr(config, "cluster_reasoning", "high"), "high")
    registry = provider_registry(config)
    registry_public = registry.public(simulation_override=config.simulation)
    try:
        from cluster import SUPPORTED_EXECUTORS, role_catalog
        selectable_roles = role_catalog()
    except ImportError:  # pragma: no cover - package import fallback
        from .cluster import SUPPORTED_EXECUTORS, role_catalog
        selectable_roles = role_catalog()
    return {
        "provider_name": config.provider_name or "",
        "base_url": config.base_url or "",
        "api_key_configured": bool(config.api_key),
        "api_key_hint": ("..." + config.api_key[-4:]) if config.api_key else "",
        "models": models,
        "reasoning": reasoning,
        "tier_weights": tier_weights,
        "model_tiers": list(MODEL_TIERS),
        "reasoning_levels": list(REASONING_LEVELS),
        "max_tier": config.max_tier,
        "highest_tier": config.max_tier,
        "highest_model": model_for_tier(config, config.max_tier),
        "cluster_reasoning": cluster_reasoning,
        "cluster_reasoning_effort": cluster_reasoning,
        "total_reasoning_effort": cluster_reasoning,
        "max_weight": config.max_weight,
        "simulation": config.simulation,
        "input_price_per_million": config.input_price_per_million,
        "output_price_per_million": config.output_price_per_million,
        "pricing_currency": config.pricing_currency,
        "pricing_configured": config.input_price_per_million is not None and config.output_price_per_million is not None,
        "mode": max(0, min(3, int(getattr(config, "mode", 0)))),
        "runtime_mode": max(0, min(3, int(getattr(config, "mode", 0)))),
        # Additive structured routing contract.  ``models`` above remains the
        # legacy tier -> string map expected by existing browser clients.
        "provider_registry_version": registry.version,
        "default_provider_id": registry.default_provider_id or "",
        "providers": registry_public["providers"],
        "routes": registry_public["routes"],
        "provider_registry": registry_public,
        "agent_profiles": getattr(config, "agent_profiles", None) or {},
        "mode_roles": getattr(config, "agent_profiles", None) or {},
        "role_catalog": selectable_roles,
        "executors": list(SUPPORTED_EXECUTORS),
    }


def _normalize_agent_profiles(raw, current: RuntimeConfig, payload: dict) -> dict:
    """Validate the user-editable mode -> role composition contract.

    A compact command may target one mode with ``roles``/``mode``; clients
    that already have the full document can send ``agent_profiles`` directly.
    Only roles exposed by the catalog are accepted.
    """
    try:
        from cluster import SUPPORTED_EXECUTORS, role_catalog
    except ImportError:  # pragma: no cover - package import fallback
        from .cluster import SUPPORTED_EXECUTORS, role_catalog
    existing = getattr(current, "agent_profiles", None) or {}
    source = raw if isinstance(raw, dict) else existing
    if isinstance(payload.get("roles"), list):
        mode = str(payload.get("profile_mode", payload.get("mode", getattr(current, "mode", 0))))
        source = {**existing, mode: payload["roles"]}
    catalog = {str(item["role"]): item for item in role_catalog()}
    normalized: dict[str, list[dict]] = {}
    for raw_mode, values in source.items():
        try:
            mode = str(max(0, min(3, int(raw_mode))))
        except (TypeError, ValueError):
            continue
        if not isinstance(values, list):
            continue
        roles: list[dict] = []
        seen_roles: set[str] = set()
        for entry in values:
            if not isinstance(entry, dict):
                continue
            role = str(entry.get("role") or entry.get("name") or "").strip()
            base = catalog.get(role)
            if not base:
                raise ValueError(f"unknown role: {role}")
            if role in seen_roles:
                # A repeated row cannot produce distinct stable slot ids;
                # keep the first declaration so a malformed browser payload
                # never overwrites another agent at runtime.
                continue
            seen_roles.add(role)
            try:
                max_count = max(1, min(100, int(entry.get("max_count", entry.get("slots", base["max_count"])))))
            except (TypeError, ValueError) as error:
                raise ValueError(f"max_count for {role} must be an integer") from error
            executor = str(entry.get("executor") or base.get("executor") or "direct_model").strip().lower()
            if executor not in SUPPORTED_EXECUTORS:
                raise ValueError("executor must be direct_model, openclaw, codex, or claude_code")
            roles.append({
                "role": role,
                "max_count": max_count,
                # An omitted provider means automatic model-capability
                # matching.  Keep it empty so role/pool routes remain
                # overrideable; an explicitly supplied id is authoritative.
                "provider_id": str(entry.get("provider_id") or entry.get("provider") or "").strip(),
                "model": str(entry.get("model") or entry.get("model_id") or base["model"]).strip(),
                "executor": executor,
                "pool": str(entry.get("pool") or base["pool"]).strip(),
            })
        # An explicit empty list is meaningful: it disables all optional slots
        # for that mode.  Runtime still has legacy behavior when no profile is
        # supplied at all.
        normalized[mode] = roles
    return normalized


def updated_config(current: RuntimeConfig, payload: dict) -> RuntimeConfig:
    """Validate browser-provided settings and create a new in-memory config."""
    if not isinstance(payload, dict):
        raise TypeError("configuration payload must be an object")

    def text(name: str, fallback: str, limit: int = 200) -> str:
        value = payload.get(name, fallback)
        return str(value).strip()[:limit] or fallback

    def positive(name: str, fallback: int, maximum: int = 1000) -> int:
        try:
            return max(1, min(maximum, int(payload.get(name, fallback))))
        except (TypeError, ValueError):
            return fallback

    def boolean(name: str, fallback: bool) -> bool:
        value = payload.get(name, fallback)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() not in {"0", "false", "no", "off"}

    def optional_price(name: str, fallback: float | None) -> float | None:
        if name not in payload:
            return fallback
        value = payload.get(name)
        if value is None or str(value).strip() == "":
            return None
        try:
            return max(0.0, min(1_000_000.0, float(value)))
        except (TypeError, ValueError) as error:
            raise ValueError(f"{name} must be empty or a non-negative number") from error

    provider_name = text("provider_name", current.provider_name or "a6api")
    base_url = text("base_url", current.base_url or "", 500).rstrip("/") or None
    if base_url and not base_url.lower().startswith(("http://", "https://")):
        raise ValueError("base_url must start with http:// or https://")
    if base_url:
        parsed_base_url = urlsplit(base_url)
        if parsed_base_url.username or parsed_base_url.password or parsed_base_url.query or parsed_base_url.fragment:
            raise ValueError("base_url must not include credentials, query parameters, or fragments")
    supplied_key = str(payload.get("api_key", "")).strip()
    api_key = supplied_key or current.api_key
    if payload.get("clear_api_key"):
        api_key = None

    models = {}
    reasoning = {}
    weights = {}
    for tier in MODEL_TIERS:
        model_key = f"model_{tier}"
        # ``model_max``/``model_top`` are accepted as ergonomic aliases for
        # clients that refer to the highest tier without naming it.
        model_fallback = model_for_tier(current, tier)
        if tier == "ultra" and model_key not in payload:
            for alias in ("model_max", "model_top", "max_model"):
                if alias in payload:
                    model_key = alias
                    break
        models[tier] = text(model_key, model_fallback, 200)

        reasoning_key = f"reasoning_{tier}"
        reasoning_fallback = _coerce_reasoning(_mapping_value(current.reasoning, tier, DEFAULT_REASONING[tier]), DEFAULT_REASONING[tier])
        if tier == "ultra" and reasoning_key not in payload:
            for alias in ("reasoning_max", "reasoning_top", "max_reasoning"):
                if alias in payload:
                    reasoning_key = alias
                    break
        reasoning_value = text(reasoning_key, reasoning_fallback, 20).lower()
        if reasoning_value not in REASONING_LEVELS:
            raise ValueError("reasoning must be minimal, low, medium, high, or xhigh")
        reasoning[tier] = reasoning_value
        weights[tier] = positive(f"weight_{tier}", int(_mapping_value(current.tier_weights, tier, DEFAULT_TIER_WEIGHTS[tier])), 1000)

    max_weight = positive("max_weight", current.max_weight, 10000)
    if max(weights.values()) > max_weight:
        raise ValueError("max_weight must be at least the largest tier weight")
    cluster_reasoning = payload.get("cluster_reasoning", payload.get("cluster_reasoning_effort", payload.get("total_reasoning_effort", getattr(current, "cluster_reasoning", "high"))))
    cluster_reasoning = str(cluster_reasoning).strip().lower()
    if cluster_reasoning not in REASONING_LEVELS:
        raise ValueError("cluster_reasoning must be minimal, low, medium, high, or xhigh")
    raw_mode = payload.get("mode", payload.get("cluster_mode", payload.get("runtime_mode", getattr(current, "mode", 0))))
    try:
        if isinstance(raw_mode, str):
            normalized_mode = raw_mode.strip().lower()
            if normalized_mode.startswith("mode"):
                normalized_mode = normalized_mode[4:].strip(" _-")
            raw_mode = {"single": 0, "mid": 1, "medium": 1, "high": 2, "extreme": 3, "max": 3}.get(normalized_mode, normalized_mode)
        mode = max(0, min(3, int(raw_mode)))
    except (TypeError, ValueError) as error:
        raise ValueError("mode must be 0, 1, 2, or 3") from error
    agent_profiles = _normalize_agent_profiles(
        payload.get("agent_profiles", payload.get("mode_roles", payload.get("roles_by_mode", getattr(current, "agent_profiles", None)))),
        current,
        payload,
    )
    candidate = RuntimeConfig(
        provider_name,
        base_url,
        api_key,
        models,
        reasoning,
        weights,
        max_weight,
        boolean("simulation", current.simulation),
        cluster_reasoning,
        optional_price("input_price_per_million", current.input_price_per_million),
        optional_price("output_price_per_million", current.output_price_per_million),
        text("pricing_currency", current.pricing_currency or "USD", 12).upper(),
        mode,
        None,
        agent_profiles,
    )
    # Keep the old projection authoritative for the default provider, then
    # merge any additive provider/route declarations.  A malformed optional
    # registry payload is rejected rather than silently changing the legacy
    # settings in an ambiguous way.
    previous_registry = provider_registry(current)
    registry = ProviderRegistry.from_legacy(
        candidate.provider_name,
        candidate.base_url,
        candidate.api_key,
        candidate.models,
        simulation=candidate.simulation,
        existing=previous_registry,
    )
    # Rebuilding the legacy tier projection must not erase an operator's
    # explicit tier route during an unrelated provider/pool/role update.  A
    # route that still matches the previous legacy projection may be safely
    # refreshed; any other tier route remains authoritative until explicitly
    # updated or removed through the route API.
    merged_routes = dict(registry.routes)
    for tier in MODEL_TIERS:
        route_id = f"tier:{tier}"
        previous_route = previous_registry.routes.get(route_id)
        if not previous_route:
            continue
        was_legacy_projection = (
            previous_route.provider_id == previous_registry.default_provider_id
            and previous_route.model_id == model_for_tier(current, tier)
            and previous_route.executor == ExecutorKind.DIRECT.value
            and not previous_route.fallbacks
            and not previous_route.capabilities
        )
        if not was_legacy_projection:
            merged_routes[route_id] = previous_route
    if merged_routes != dict(registry.routes):
        registry = replace(registry, routes=merged_routes)
    registry_payload = payload.get("provider_registry") if isinstance(payload.get("provider_registry"), dict) else payload
    if any(key in registry_payload for key in ("providers", "routes", "default_provider_id", "default_provider")):
        registry = registry.update_from_payload(registry_payload)
    return replace(candidate, provider_registry=registry)


def persistable_config(config: RuntimeConfig) -> dict:
    """Return the non-secret runtime settings stored on disk."""
    public = public_config(config)
    return {
        "version": 1,
        "provider_name": public["provider_name"],
        "base_url": public["base_url"],
        "models": public["models"],
        "reasoning": public["reasoning"],
        "tier_weights": public["tier_weights"],
        "cluster_reasoning": public["cluster_reasoning"],
        "max_weight": public["max_weight"],
        "simulation": public["simulation"],
        "input_price_per_million": public["input_price_per_million"],
        "output_price_per_million": public["output_price_per_million"],
        "pricing_currency": public["pricing_currency"],
        "mode": public["mode"],
        "agent_profiles": public["agent_profiles"],
        "mode_roles": public["agent_profiles"],
        # Provider metadata is additive and redacted.  API keys are retained
        # only in the process/environment, so restoring this document cannot
        # leak or overwrite credentials.
        "provider_registry_version": public["provider_registry_version"],
        "default_provider_id": public["default_provider_id"],
        "providers": [
            {
                key: value
                for key, value in provider.items()
                if key not in {"api_key_hint", "api_key_configured"}
            }
            for provider in public.get("providers", [])
        ],
        "routes": public.get("routes", []),
    }


def restore_persisted_config(environment_config: RuntimeConfig, saved: dict | None) -> RuntimeConfig:
    """Overlay safe persisted settings while retaining the secure API key source."""
    if not isinstance(saved, dict):
        return environment_config
    payload = {
        "provider_name": saved.get("provider_name", environment_config.provider_name or "a6api"),
        "base_url": saved.get("base_url", environment_config.base_url or ""),
        "cluster_reasoning": saved.get("cluster_reasoning", environment_config.cluster_reasoning),
        "max_weight": saved.get("max_weight", environment_config.max_weight),
        "simulation": saved.get("simulation", environment_config.simulation),
        "input_price_per_million": saved.get("input_price_per_million", environment_config.input_price_per_million),
        "output_price_per_million": saved.get("output_price_per_million", environment_config.output_price_per_million),
        "pricing_currency": saved.get("pricing_currency", environment_config.pricing_currency),
        "mode": saved.get("mode", getattr(environment_config, "mode", 0)),
    }
    if isinstance(saved.get("providers"), (list, dict)):
        payload["providers"] = saved.get("providers")
    if isinstance(saved.get("routes"), (list, dict)):
        payload["routes"] = saved.get("routes")
    if saved.get("default_provider_id"):
        payload["default_provider_id"] = saved.get("default_provider_id")
    if isinstance(saved.get("agent_profiles", saved.get("mode_roles")), dict):
        payload["agent_profiles"] = saved.get("agent_profiles", saved.get("mode_roles"))
    for tier in MODEL_TIERS:
        payload[f"model_{tier}"] = (saved.get("models") or {}).get(tier, model_for_tier(environment_config, tier))
        payload[f"reasoning_{tier}"] = (saved.get("reasoning") or {}).get(tier, environment_config.reasoning.get(tier, DEFAULT_REASONING[tier]))
        payload[f"weight_{tier}"] = (saved.get("tier_weights") or {}).get(tier, environment_config.tier_weights.get(tier, DEFAULT_TIER_WEIGHTS[tier]))
    return updated_config(environment_config, payload)


def is_greeting(prompt: str) -> bool:
    normalized = " ".join(str(prompt or "").strip().lower().split())
    normalized = normalized.strip(" .,!?:;\u3002\uff01\uff1f\u3001")
    return normalized in {
        "hello", "hi", "hey", "good morning", "good afternoon", "good evening",
        "\u4f60\u597d", "\u60a8\u597d", "\u55e8", "\u54c8\u5587", "\u5728\u5417",
    }


def _has_any(text: str, words: tuple[str, ...]) -> bool:
    return any(word in text for word in words)


def assess_task_difficulty(prompt: str, attachments: list[dict] | None = None, workspace: bool = False) -> dict:
    """Estimate task complexity locally before any fan-out.

    This is deliberately deterministic and cheap: it gives the coordinator a
    useful proposal even in simulation/offline mode, and it can be replaced by
    a model-backed assessor later without changing the task API.
    """
    text = str(prompt or "").strip()
    normalized = text.lower()
    attachments = attachments or []
    score = 8
    signals: list[str] = []

    if len(text) >= 240:
        score += 12
        signals.append("long_context")
    if len(text) >= 900:
        score += 10
        signals.append("large_context")
    if text.count("\n") >= 3:
        score += 7
        signals.append("multi_section_request")
    if len(attachments) >= 1:
        score += min(18, 6 + len(attachments) * 3)
        signals.append("attachments")
    if workspace:
        score += 10
        signals.append("workspace_or_code_access")

    keyword_groups = (
        (("code", "repo", "repository", "bug", "refactor", "implement", "python", "typescript", "api", "database", "代码", "仓库", "修复"), 18, "implementation"),
        (("research", "compare", "evaluate", "sources", "literature", "调研", "比较", "评估", "资料"), 14, "evidence_review"),
        (("design", "architecture", "migration", "deploy", "production", "架构", "迁移", "部署", "生产"), 18, "system_design"),
        (("security", "privacy", "compliance", "legal", "medical", "financial", "安全", "隐私", "合规", "法律", "医疗", "金融"), 20, "high_stakes"),
        (("step by step", "multi-step", "end to end", "workflow", "multiple", "分步骤", "多步骤", "端到端", "流程"), 12, "multi_step"),
    )
    for words, increment, signal in keyword_groups:
        if _has_any(normalized, words):
            score += increment
            signals.append(signal)

    # Multiple explicit deliverables usually require independent passes.
    delimiters = sum(normalized.count(token) for token in (" and ", ",", ";", "并且", "以及", "、"))
    if delimiters >= 2:
        score += 8
        signals.append("multiple_deliverables")
    score = min(100, score)

    if score < 28:
        complexity, tier, level, agent_count = "low", "low", "low", 1
    elif score < 52:
        complexity, tier, level, agent_count = "medium", "medium", "medium", 2
    elif score < 76:
        complexity, tier, level, agent_count = "high", "high", "high", 3
    else:
        complexity, tier, level, agent_count = "critical", "ultra", "xhigh", 4

    questions: list[str] = []
    if not text or len(text) < 80:
        questions.append("What outcome or decision should the finished work enable?")
    if not _has_any(normalized, ("scope", "only", "exclude", "范围", "不包括")):
        questions.append("What is in scope, and what should we explicitly leave out?")
    if not _has_any(normalized, ("format", "deliverable", "output", "报告", "格式", "交付")):
        questions.append("What deliverable format and level of detail do you prefer?")
    if not questions:
        questions.append("Are there deadlines, constraints, or acceptance criteria I should apply?")
    questions = questions[:3]

    signal_text = ", ".join(signals) if signals else "a focused request"
    rationale = f"Estimated {complexity} complexity (score {score}/100) from {signal_text}; recommend {tier} tier with {level} reasoning and {agent_count} worker pass{'es' if agent_count != 1 else ''}."
    return {
        "score": score,
        "complexity": complexity,
        "signals": signals,
        "recommended_tier": tier,
        "recommended_reasoning": level,
        "recommended_agents": agent_count,
        "rationale": rationale,
        "clarifying_questions": questions,
    }


# Short alias for callers/tests that use the noun phrase from the product
# requirement.
assess_task = assess_task_difficulty


def _round_token_estimate(value: float) -> int:
    return max(100, int(math.ceil(value / 100.0) * 100))


def build_resource_estimate(
    prompt: str,
    attachments: list[dict] | None,
    workspace: bool,
    config: RuntimeConfig,
    assessment: dict,
) -> dict:
    """Estimate local workload with a deterministic, reviewable rule set."""
    text = str(prompt or "")
    attachments = attachments or []
    attachment_characters = sum(len(str(item.get("content") or "")) for item in attachments)
    metadata_tokens = sum(80 + min(500, int(item.get("size") or 0) // 4000) for item in attachments)
    context_tokens = max(300, math.ceil((len(text) + attachment_characters) / 4) + metadata_tokens)
    score = max(0, min(100, int(assessment.get("score", 0))))
    recommended_agents = max(1, min(4, int(assessment.get("recommended_agents", 1))))
    level = _coerce_reasoning(assessment.get("recommended_reasoning"), "medium")
    reasoning_factor = {"minimal": 0.7, "low": 0.85, "medium": 1.0, "high": 1.35, "xhigh": 1.7}[level]
    context_factor = 1.0 + score / 250.0 + (0.12 if workspace else 0.0)

    input_min = _round_token_estimate(context_tokens * (1.0 + recommended_agents * 0.55) * context_factor * reasoning_factor)
    input_max = _round_token_estimate(input_min * (1.55 + score / 180.0))
    output_seed = 350 + score * 18 + recommended_agents * 220
    output_min = _round_token_estimate(output_seed * reasoning_factor)
    output_max = _round_token_estimate(output_min * (1.7 + score / 220.0))

    if config.simulation:
        duration_min = max(3, int(math.ceil(2.0 + recommended_agents * 0.9)))
        duration_max = max(duration_min + 2, int(math.ceil(duration_min * 2.1)))
    else:
        duration_min = max(20, int(math.ceil(18 + score * 0.45 + recommended_agents * 6 + reasoning_factor * 10)))
        duration_max = max(duration_min + 15, int(math.ceil(duration_min * (1.65 + score / 130.0))))

    pricing_configured = config.input_price_per_million is not None and config.output_price_per_million is not None
    cost_min = cost_max = None
    if pricing_configured:
        cost_min = round(
            input_min / 1_000_000 * float(config.input_price_per_million)
            + output_min / 1_000_000 * float(config.output_price_per_million),
            6,
        )
        cost_max = round(
            input_max / 1_000_000 * float(config.input_price_per_million)
            + output_max / 1_000_000 * float(config.output_price_per_million),
            6,
        )

    confidence = "low" if workspace or any(not item.get("content") and item.get("size") for item in attachments) else "medium"
    mode = "simulation mode" if config.simulation else "configured executor mode"
    basis = f"Local deterministic heuristic ({mode}); score, context size, attachments, workspace access, reasoning level, and parallel passes."
    basis_localized = (
        "本地确定性启发式规则（模拟模式）；依据难度评分、上下文规模、附件、工作区访问、推理档位和并行工作单元。"
        if config.simulation
        else "本地确定性启发式规则（已配置执行器模式）；依据难度评分、上下文规模、附件、工作区访问、推理档位和并行工作单元。"
    )
    uncertainty = "Approximate workload range; actual duration and token usage may vary by at least 50% with executor and response shape."
    return {
        "version": 1,
        "duration_seconds_min": duration_min,
        "duration_seconds_max": duration_max,
        "input_tokens_min": input_min,
        "input_tokens_max": input_max,
        "output_tokens_min": output_min,
        "output_tokens_max": output_max,
        "cost_currency": config.pricing_currency or "USD",
        "cost_min": cost_min,
        "cost_max": cost_max,
        "pricing_configured": pricing_configured,
        "recommended_agents": recommended_agents,
        "confidence": confidence,
        "basis": basis,
        "basis_localized": basis_localized,
        "uncertainty": uncertainty,
        "uncertainty_localized": "这是近似工作量范围；实际耗时和 token 用量会随执行器与响应结构变化，误差可能达到或超过 50%。",
        "simulation": bool(config.simulation),
        "reasoning_level": level,
        "model": model_for_tier(config, assessment.get("recommended_tier", "medium")),
    }


def build_reasoning_recommendation(prompt: str, attachments: list[dict] | None = None, workspace: bool = False, config: RuntimeConfig | None = None) -> tuple[dict, dict]:
    """Return the assessment and the user-reviewable reasoning proposal."""
    assessment = assess_task_difficulty(prompt, attachments, workspace)
    config = config or RuntimeConfig.from_environment()
    tier = assessment["recommended_tier"]
    level = assessment["recommended_reasoning"]
    chinese = any("\u4e00" <= char <= "\u9fff" for char in str(prompt or ""))
    complexity_names = {"low": "低", "medium": "中", "high": "高", "critical": "关键"}
    level_names = {"minimal": "最低", "low": "低", "medium": "中", "high": "高", "xhigh": "最高"}
    localized_rationale = (
        f"预计任务复杂度为{complexity_names.get(assessment['complexity'], assessment['complexity'])}（评分 {assessment['score']} / 100）；"
        f"建议使用{level_names.get(level, level)}档推理强度，并安排 {assessment['recommended_agents']} 个工作单元。"
        if chinese else assessment["rationale"]
    )
    proposal_id = str(uuid4())
    estimate = build_resource_estimate(prompt, attachments, workspace, config, assessment)
    estimate["proposal_id"] = proposal_id
    assessment["estimate"] = estimate
    recommendation = {
        "level": level,
        "recommended_reasoning": level,
        "total_reasoning": level,
        "reasoning_effort": level,
        "rationale": assessment["rationale"],
        "rationale_localized": localized_rationale,
        "complexity": assessment["complexity"],
        "approved": False,
        "recommended_tier": tier,
        "model_tier": tier,
        "model": model_for_tier(config, tier),
        "recommended_agents": assessment["recommended_agents"],
        "score": assessment["score"],
        "signals": list(assessment["signals"]),
        "proposal_id": proposal_id,
        "estimate": estimate,
    }
    return assessment, recommendation


def coordinator_reply(
    prompt: str,
    questions: list[str] | None = None,
    recommendation: dict | None = None,
    cluster_available: bool = False,
    workflow_ready: bool = False,
) -> str:
    """Compose the coordinator's clarification/review message.

    The optional arguments preserve compatibility with the old two-argument
    helper while allowing both HTTP servers to render the same message.
    """
    # Backwards compatibility: the previous signature was
    # ``coordinator_reply(prompt, cluster_available)``.
    if isinstance(questions, bool):
        cluster_available = questions
        questions = None
    questions = list(questions or [])
    recommendation = recommendation or {}
    short_prompt = str(prompt or "").replace("\n", " ").strip()
    if len(short_prompt) > 180:
        short_prompt = short_prompt[:177] + "..."
    chinese = any("\u4e00" <= char <= "\u9fff" for char in short_prompt)
    if chinese:
        if workflow_ready and cluster_available:
            intro = (
                f"根据你补充的信息，我将任务复述为：“{short_prompt}”。\n"
                "建议工作流程：\n"
                "1. Orion 固化目标、范围、约束与验收标准；\n"
                "2. Orion 拆分可并行的工作，并把边界清楚地分派给子 Agent；\n"
                "3. Orion 检查各子 Agent 的过程与结果，处理冲突和遗漏；\n"
                "4. Orion 汇总为你要求的交付物并回到主对话。\n"
                "请确认这个工作流程。确认后，我会单独提出集群总推理强度建议，仍不会自动启动集群。"
            )
        elif is_greeting(short_prompt):
            intro = "你好，我是 Orion。请告诉我你想完成什么；我会先直接与你讨论清楚，再判断是否需要子 Agent。"
        else:
            intro = f"我理解你的任务是：“{short_prompt}”。在分派任何子 Agent 之前，我需要先把细节确认清楚。"
            question_map = {
                "What outcome or decision should the finished work enable?": "这项工作最终要支持什么结果或决策？",
                "What is in scope, and what should we explicitly leave out?": "本次范围包括什么？哪些内容需要明确排除？",
                "What deliverable format and level of detail do you prefer?": "你希望交付什么格式，以及详细到什么程度？",
                "Are there deadlines, constraints, or acceptance criteria I should apply?": "是否有截止时间、约束条件或验收标准？",
            }
            question_block = "\n".join(f"{index}. {question_map.get(question, question)}" for index, question in enumerate(questions, 1))
            if question_block:
                intro += "\n需要你确认的问题：\n" + question_block
            if cluster_available:
                intro += "\n请先补充上述信息；收到补充后，我会复述任务和工作流程。在你确认前不会启动集群。"
            else:
                intro += "\n当前未启用集群，我会只在本对话中回复，不会自动启动任何子 Agent。"
    else:
        if workflow_ready and cluster_available:
            intro = (
                f'I restate the agreed task as: "{short_prompt}".\n'
                "Proposed workflow:\n"
                "1. Orion fixes the goal, scope, constraints, and acceptance criteria.\n"
                "2. Orion separates work that benefits from parallel passes and dispatches clear boundaries.\n"
                "3. Orion reviews sub-agent progress and results, resolving conflicts and gaps.\n"
                "4. Orion returns the requested deliverable in the main conversation.\n"
                "Please confirm this workflow. After confirmation, I will propose the total reasoning intensity separately; the cluster will still not start automatically."
            )
        elif is_greeting(short_prompt):
            intro = "Hello, I am Orion. Tell me what you want to accomplish; I will discuss it with you before deciding whether any sub-agents are useful."
        else:
            intro = f"I understand the task as: \"{short_prompt}\". Before I dispatch agents, I want to clarify a few details."
            question_block = "\n".join(f"{index}. {question}" for index, question in enumerate(questions, 1))
            if question_block:
                intro += "\nQuestions:\n" + question_block
            if cluster_available:
                intro += "\nPlease answer these questions. I will then restate the task and workflow for confirmation; no agents will start before that review."
            else:
                intro += "\nThe cluster is off; I will keep this as a direct conversation and will not start any agents automatically."
    return intro


def reasoning_proposal_reply(prompt: str, recommendation: dict) -> str:
    """Publish the coordinator's reasoning proposal after workflow approval."""
    chinese = any("\u4e00" <= char <= "\u9fff" for char in str(prompt or ""))
    level = str(recommendation.get("level", "medium"))
    estimate = recommendation.get("estimate") or {}
    agents = estimate.get("recommended_agents", recommendation.get("recommended_agents", "-"))
    duration = f"{estimate.get('duration_seconds_min', '-')}-{estimate.get('duration_seconds_max', '-')}s"
    input_tokens = f"{estimate.get('input_tokens_min', '-')}-{estimate.get('input_tokens_max', '-')}"
    output_tokens = f"{estimate.get('output_tokens_min', '-')}-{estimate.get('output_tokens_max', '-')}"
    if estimate.get("pricing_configured"):
        cost = f"{estimate.get('cost_currency', 'USD')} {estimate.get('cost_min', '-')}-{estimate.get('cost_max', '-')}"
    else:
        cost = "未配置模型价格" if chinese else "model pricing is not configured"
    if chinese:
        complexity_names = {"low": "低", "medium": "中", "high": "高", "critical": "关键"}
        level_names = {"minimal": "最低", "low": "低", "medium": "中", "high": "高", "xhigh": "最高"}
        confidence_names = {"low": "低", "medium": "中", "high": "高"}
        complexity = recommendation.get("complexity", "")
        return (
            "工作流程已确认。现在给出集群总推理强度建议："
            f"任务复杂度为{complexity_names.get(complexity, complexity)}（评分 {recommendation.get('score', '-')} / 100），"
            f"建议选择“{level_names.get(level, level)}”，预计并行 {agents} 个 Agent，耗时 {duration}，"
            f"输入 token 约 {input_tokens}，输出 token 约 {output_tokens}，预计成本 {cost}。"
            f"估算依据：{estimate.get('basis_localized') or estimate.get('basis', '本地确定性启发式规则')}；"
            f"置信度：{confidence_names.get(estimate.get('confidence', 'medium'), estimate.get('confidence', 'medium'))}。"
            "请审核并批准或调整该强度；批准后仍需你显式点击“启动集群”。"
        )
    return (
        "The workflow is confirmed. Here is the total reasoning proposal: "
        + str(recommendation.get("rationale", ""))
        + f" Estimated parallel agents: {agents}; duration: {duration}; input tokens: {input_tokens}; output tokens: {output_tokens}; cost: {cost}."
        + f" Basis: {estimate.get('basis', 'local deterministic heuristic')}; confidence: {estimate.get('confidence', 'medium')}."
        + " Review and approve or adjust it. Even after approval, the cluster starts only when you explicitly click Start cluster."
    )


def synthesis_reply(prompt: str, completed_count: int) -> str:
    """Return a concise final message in the language used by the task."""
    chinese = any("\u4e00" <= char <= "\u9fff" for char in str(prompt or ""))
    if chinese:
        return f"集群已完成并行研究、验证与汇总。指挥 Agent 已审阅 {completed_count} 个子 Agent 的结果。"
    return (
        "The swarm completed its parallel research, verification, and synthesis pass. "
        + (f"The coordinator reviewed {completed_count} sub-agent findings." if completed_count else "No sub-agent findings were returned.")
    )


def available_executors() -> dict[str, bool]:
    return {
        ExecutorKind.DIRECT.value: True,
        ExecutorKind.CODEX.value: shutil.which("codex") is not None,
        ExecutorKind.OPENCLAW.value: shutil.which("openclaw") is not None,
        ExecutorKind.CLAUDE_CODE.value: shutil.which("claude") is not None,
    }


def choose_executor(task: str, has_workspace: bool = False) -> ExecutorKind:
    text = task.lower()
    coding_terms = ("code", "repo", "test", "bug", "python", "typescript", "代码", "仓库", "测试", "修复")
    browser_terms = ("search", "browse", "download", "website", "网页", "搜索", "下载", "浏览器")
    installed = available_executors()
    if has_workspace and installed[ExecutorKind.CODEX.value] and any(word in text for word in coding_terms):
        return ExecutorKind.CODEX
    if installed[ExecutorKind.OPENCLAW.value] and any(word in text for word in browser_terms):
        return ExecutorKind.OPENCLAW
    return ExecutorKind.DIRECT


def _extract_openclaw_text(value) -> str | None:
    if isinstance(value, dict):
        for key in ("text", "content", "message", "reply"):
            result = value.get(key)
            if isinstance(result, str) and result.strip():
                return result.strip()
        for nested in value.values():
            result = _extract_openclaw_text(nested)
            if result:
                return result
    elif isinstance(value, list):
        for nested in value:
            result = _extract_openclaw_text(nested)
            if result:
                return result
    return None


def _provider_endpoint(base_url: str | None, suffix: str) -> str:
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        raise RuntimeError("Model provider base URL is not configured")
    normalized_suffix = "/" + suffix.strip("/")
    if base.lower().endswith(normalized_suffix.lower()):
        return base
    return base + normalized_suffix


def _decode_json_response(response) -> dict:
    try:
        value = json.loads(response.read().decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise RuntimeError("Model provider returned invalid JSON") from error
    if not isinstance(value, dict):
        raise RuntimeError("Model provider returned an invalid response object")
    return value


def _openai_response_text(result: dict) -> str:
    try:
        content = result["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise RuntimeError("OpenAI-compatible provider returned no message content") from error
    if isinstance(content, str) and content.strip():
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        text = "\n".join(parts).strip()
        if text:
            return text
    raise RuntimeError("OpenAI-compatible provider returned empty message content")


def _anthropic_response_text(result: dict) -> str:
    content = result.get("content")
    if not isinstance(content, list):
        raise RuntimeError("Anthropic provider returned no content blocks")
    parts = [
        str(item.get("text", "")).strip()
        for item in content
        if isinstance(item, dict) and item.get("type") in (None, "text") and str(item.get("text", "")).strip()
    ]
    if not parts:
        raise RuntimeError("Anthropic provider returned empty message content")
    return "\n".join(parts)


def _run_openai_route(prompt: str, route: ResolvedRoute, effort: str) -> str:
    if not route.api_key:
        raise RuntimeError(f"Provider {route.provider_id} API key is not configured")
    body = json.dumps({
        "model": route.model_id,
        "reasoning_effort": effort,
        "messages": [
            {"role": "system", "content": "You are one sub-agent in a larger swarm. Return a concise, evidence-oriented result for the coordinator."},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
    }).encode("utf-8")
    request = Request(
        _provider_endpoint(route.base_url, "chat/completions"),
        data=body,
        method="POST",
        headers={"Authorization": "Bearer " + route.api_key, "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=route.timeout_seconds) as response:
            return _openai_response_text(_decode_json_response(response))
    except (HTTPError, URLError, TimeoutError, OSError) as error:
        raise RuntimeError(f"Provider {route.provider_id} request failed") from error


def _run_anthropic_route(prompt: str, route: ResolvedRoute, effort: str) -> str:
    if not route.api_key:
        raise RuntimeError(f"Provider {route.provider_id} API key is not configured")
    # Anthropic's Messages API has no OpenAI-style reasoning_effort field.
    # Keep the reviewed effort as safe metadata in the system instruction so
    # the cross-provider route still honors the task's deliberation profile.
    body = json.dumps({
        "model": route.model_id,
        "max_tokens": 4096,
        "system": (
            "You are one sub-agent in a larger swarm. Return a concise, "
            f"evidence-oriented result for the coordinator. Reasoning profile: {effort}."
        ),
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    base_path = urlsplit(str(route.base_url or "")).path.rstrip("/").lower()
    anthropic_resource = "messages" if base_path.endswith("/v1") else "v1/messages"
    request = Request(
        _provider_endpoint(route.base_url, anthropic_resource),
        data=body,
        method="POST",
        headers={
            "x-api-key": route.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=route.timeout_seconds) as response:
            return _anthropic_response_text(_decode_json_response(response))
    except (HTTPError, URLError, TimeoutError, OSError) as error:
        raise RuntimeError(f"Provider {route.provider_id} request failed") from error


def run_direct_model(
    prompt: str,
    tier: str,
    config: RuntimeConfig,
    reasoning_effort: str | None = None,
    resolved_route: ResolvedRoute | None = None,
) -> str:
    """Execute one direct HTTP route while retaining the legacy call shape."""
    route = resolved_route or resolve_route(config, tier, model_id=model_for_tier(config, tier))
    if not route:
        raise RuntimeError("No model provider route is configured")
    if config.simulation or route.simulation:
        return f"Completed the assigned evidence pass in local simulation mode ({_coerce_reasoning(reasoning_effort or reasoning_for_tier(config, tier), 'high')} reasoning)."
    effort = _coerce_reasoning(reasoning_effort or reasoning_for_tier(config, tier), "high")
    if route.protocol == PROTOCOL_ANTHROPIC:
        return _run_anthropic_route(prompt, route, effort)
    if route.protocol == PROTOCOL_OPENAI:
        return _run_openai_route(prompt, route, effort)
    raise RuntimeError(f"Provider protocol {route.protocol} does not support direct HTTP execution")


def run_codex(prompt: str, tier: str, config: RuntimeConfig, workspace: str | None = None, reasoning_effort: str | None = None, resolved_route: ResolvedRoute | None = None) -> str:
    executable = shutil.which("codex")
    if not executable:
        raise RuntimeError("Codex CLI is unavailable")
    working_directory = Path(workspace).resolve() if workspace else Path.cwd()
    with tempfile.TemporaryDirectory(prefix="orbit-codex-") as directory:
        output = Path(directory) / "last-message.txt"
        effort = _coerce_reasoning(reasoning_effort or reasoning_for_tier(config, tier), "high")
        command = [executable, "--config", f'model_reasoning_effort="{effort}"']
        subprocess_env = None
        if resolved_route:
            # A role can deliberately select a provider that differs from the
            # Codex global default.  Define the provider for this invocation
            # as well, so a provider added in Orbit does not also require a
            # manual edit to ~/.codex/config.toml.  Codex uses the Responses
            # wire contract; chat-only endpoints remain available through the
            # direct_model executor.
            provider_id = str(resolved_route.provider_id)
            provider_key = provider_id if re.fullmatch(r"[A-Za-z0-9_-]+", provider_id) else json.dumps(provider_id)
            provider_path = f"model_providers.{provider_key}"
            registry = provider_registry(config)
            provider = registry.providers.get(provider_id)
            env_name = provider.api_key_env if provider else None
            if not env_name:
                env_name = "".join(char if char.isalnum() else "_" for char in provider_id.upper()) + "_API_KEY"
            command.extend([
                "--config", f"{provider_path}.name={json.dumps((provider.name if provider else provider_id) or provider_id)}",
                "--config", f"{provider_path}.base_url={json.dumps(resolved_route.base_url)}",
                "--config", f"{provider_path}.env_key={json.dumps(env_name)}",
                "--config", f'{provider_path}.wire_api="responses"',
                "--config", f"{provider_path}.requires_openai_auth=false",
                "--config", f"model_provider={json.dumps(provider_id)}",
            ])
            if resolved_route.api_key and env_name and all(char.isalnum() or char == "_" for char in env_name):
                subprocess_env = dict(os.environ)
                subprocess_env[env_name] = resolved_route.api_key
        command.extend([
            "exec",
            "--skip-git-repo-check",
            "--ephemeral",
            "--sandbox",
            "read-only",
            "--model",
            resolved_route.model_id if resolved_route else model_for_tier(config, tier),
            "--output-last-message",
            str(output),
            "--cd",
            str(working_directory),
            prompt,
        ])
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
            env=subprocess_env,
        )
        if completed.returncode != 0 or not output.exists():
            raise RuntimeError("Codex execution failed")
        return output.read_text(encoding="utf-8").strip()


def run_openclaw(prompt: str, tier: str, config: RuntimeConfig, session_id: str, reasoning_effort: str | None = None, resolved_route: ResolvedRoute | None = None) -> str:
    executable = shutil.which("openclaw")
    if not executable:
        raise RuntimeError("OpenClaw CLI is unavailable")
    effort = _coerce_reasoning(reasoning_effort or reasoning_for_tier(config, tier), "high")
    provider_name = resolved_route.provider_id if resolved_route else (config.provider_name or "a6api")
    model_name = resolved_route.model_id if resolved_route else model_for_tier(config, tier)
    command = [executable, "agent", "--session-id", session_id, "--message", prompt, "--model", provider_name + "/" + model_name, "--thinking", effort, "--timeout", "600", "--json"]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=620, check=False)
    if completed.returncode != 0:
        raise RuntimeError("OpenClaw execution failed")
    try:
        payload = json.loads(completed.stdout)
    except ValueError as error:
        raise RuntimeError("OpenClaw returned invalid JSON") from error
    result = _extract_openclaw_text(payload)
    if not result:
        raise RuntimeError("OpenClaw returned no text result")
    return result


def run_claude_code(prompt: str, tier: str, config: RuntimeConfig, workspace: str | None = None, reasoning_effort: str | None = None, resolved_route: ResolvedRoute | None = None) -> str:
    """Run Claude Code in its non-interactive print mode for an explicit role."""
    executable = shutil.which("claude")
    if not executable:
        raise RuntimeError("Claude Code CLI is unavailable")
    if resolved_route and resolved_route.protocol != PROTOCOL_ANTHROPIC:
        raise RuntimeError("Claude Code requires an Anthropic Messages provider route")
    command = [executable, "-p", prompt, "--output-format", "text"]
    if resolved_route and resolved_route.model_id:
        command.extend(["--model", resolved_route.model_id])
    subprocess_env = None
    if resolved_route and resolved_route.api_key:
        subprocess_env = dict(os.environ)
        subprocess_env["ANTHROPIC_BASE_URL"] = str(resolved_route.base_url or "")
        subprocess_env["ANTHROPIC_API_KEY"] = resolved_route.api_key
        subprocess_env["ANTHROPIC_AUTH_TOKEN"] = resolved_route.api_key
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
        cwd=str(Path(workspace).resolve()) if workspace else None,
        env=subprocess_env,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        raise RuntimeError("Claude Code execution failed")
    return completed.stdout.strip()


def execute_agent(
    kind: str,
    prompt: str,
    tier: str,
    config: RuntimeConfig,
    task_id: str,
    workspace: str | None = None,
    reasoning_effort: str | None = None,
    resolved_route: ResolvedRoute | None = None,
    role: str | None = None,
    pool: str | None = None,
    task_override: dict | None = None,
    on_route_switch=None,
) -> str:
    """Execute an agent with versioned provider fallback routing.

    Existing callers still receive a plain string.  New callers may pass a
    resolved route stored on the task/agent and an optional callback that is
    notified when a provider or model fallback occurs.
    """
    effort = _coerce_reasoning(reasoning_effort or reasoning_for_tier(config, tier), "high")
    if config.simulation:
        return f"Completed the assigned evidence pass in local simulation mode ({effort} reasoning)."

    registry = provider_registry(config)
    selected = resolved_route or resolve_route(
        config,
        tier,
        role=role,
        pool=pool,
        model_id=None if (resolved_route or task_override) else model_for_tier(config, tier),
        task_override=task_override,
    )
    attempts = [selected] if selected else []
    if selected:
        attempts.extend(registry.resolve_fallbacks(selected, simulation_override=config.simulation))
    if not attempts:
        # A hand-built old RuntimeConfig still gets the exact legacy direct
        # behavior through registry_from_runtime; reaching this branch means
        # its provider projection is genuinely absent.
        raise RuntimeError("No model provider route is configured")

    errors: list[Exception] = []
    for index, route in enumerate(attempts):
        # Auto-generated legacy tier routes use ``direct_model`` as their
        # neutral default.  Do not let that erase the existing Codex/OpenClaw
        # choice made for an old task.  Explicit role/pool/task routes and any
        # non-default executor remain authoritative.
        explicit_executor_route = route.scope in {"task", "role", "pool", "default"} or route.executor != ExecutorKind.DIRECT.value
        effective_kind = route.executor if explicit_executor_route else kind
        owner = registry.providers.get(route.provider_id)
        if owner:
            available, reason = owner.availability(
                env=registry.env,
                simulation_override=config.simulation,
                model_id=route.model_id,
            )
            if not available:
                errors.append(RuntimeError(f"Provider {route.provider_id} is unavailable: {reason}"))
                continue
        if index > 0 and callable(on_route_switch):
            try:
                on_route_switch({
                    "type": "model_switched",
                    "reason": "provider_fallback",
                    "from": attempts[index - 1].public(),
                    "to": route.public(),
                    "task_id": task_id,
                })
            except Exception:
                # Telemetry must never prevent the fallback itself.
                pass
        try:
            if effective_kind == ExecutorKind.CODEX.value:
                return run_codex(prompt, tier, config, workspace, effort, route)
            if effective_kind == ExecutorKind.OPENCLAW.value:
                return run_openclaw(prompt, tier, config, "orbit-" + task_id, effort, route)
            if effective_kind == ExecutorKind.CLAUDE_CODE.value:
                return run_claude_code(prompt, tier, config, workspace, effort, route)
            return run_direct_model(prompt, tier, config, effort, route)
        except (RuntimeError, OSError, subprocess.SubprocessError) as error:
            errors.append(error)
    if errors:
        raise RuntimeError(f"All configured model routes failed ({len(errors)} attempts)") from errors[-1]
    raise RuntimeError("No executable model route is available")


def provider_health(config: RuntimeConfig, provider_id: str | None = None) -> dict:
    """Return a no-quota configuration health check for API/status views."""
    return provider_registry(config).health(provider_id, simulation_override=config.simulation)


def test_provider(
    config: RuntimeConfig,
    provider_id: str,
    *,
    live: bool = False,
    model_id: str | None = None,
    timeout_seconds: float = 10.0,
) -> dict:
    """Explicit, bounded provider check.

    The default is metadata-only and cannot spend model quota.  A caller must
    deliberately set ``live=True`` (for example through the provider test
    endpoint) to perform one minimal request.
    """
    registry = provider_registry(config)
    health = registry.health(provider_id, simulation_override=config.simulation)
    health = {**health, "live_test": bool(live), "checked": True}
    if not live or not health.get("available") or config.simulation:
        return health
    provider = registry.providers.get(str(health.get("id") or provider_id).strip().lower())
    if not provider:
        return health
    chosen_model = str(model_id or (provider.models[0] if provider.models else model_for_tier(config, "low")))
    route = ResolvedRoute(
        route_id=f"health:{provider.id}",
        provider_id=provider.id,
        model_id=chosen_model,
        executor=ExecutorKind.DIRECT.value,
        protocol=provider.protocol,
        base_url=provider.base_url,
        api_key=provider.effective_key(registry.env),
        timeout_seconds=max(1.0, min(30.0, float(timeout_seconds))),
        simulation=False,
        route_version=registry.version,
        scope="health",
        key=provider.id,
    )
    try:
        result = run_direct_model("Reply with OK.", "low", replace(config, simulation=False), "minimal", route)
    except RuntimeError as error:
        return {**health, "available": False, "status": "offline", "reason": "live_test_failed", "error": str(error)[:200]}
    return {**health, "available": True, "status": "online", "reason": "live_test_passed", "response_received": bool(result.strip())}
