"""Shared HTTP-facing helpers for provider and role routing.

The FastAPI and standard-library servers intentionally share this small layer
so their CRUD responses, validation, and redaction rules cannot drift.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

try:  # Package imports for tests/tools; script imports for server_stdlib.py.
    from .executors import RuntimeConfig, provider_registry, test_provider, updated_config
    from .providers import normalize_id, normalize_url
except ImportError:  # pragma: no cover - stdlib server script path.
    from executors import RuntimeConfig, provider_registry, test_provider, updated_config
    from providers import normalize_id, normalize_url


def provider_catalog(config: RuntimeConfig) -> dict[str, Any]:
    registry = provider_registry(config)
    return {
        "version": registry.version,
        "default_provider_id": registry.default_provider_id or "",
        "providers": registry.providers_public(simulation_override=config.simulation),
    }


def route_catalog(config: RuntimeConfig) -> dict[str, Any]:
    registry = provider_registry(config)
    routes = registry.routes_public()
    result: dict[str, Any] = {
        "version": registry.version,
        "default_provider_id": registry.default_provider_id or "",
        "default": {},
        "tiers": {},
        "pools": {},
        "roles": {},
        "routes": routes,
    }
    for route in routes:
        scope = str(route.get("scope") or "tier")
        key = str(route.get("key") or "default")
        if scope == "tier":
            result["tiers"][key] = route
        elif scope == "pool":
            result["pools"][key] = route
        elif scope == "role":
            # The browser uses mode-2/architect, while the registry normalizes
            # slash separators to colons. Publish both spellings.
            result["roles"][key.replace(":", "/")] = route
            result["roles"][key] = route
        elif scope == "default":
            result["default"] = route
    if not result["default"] and registry.default_provider_id:
        provider = registry.providers.get(registry.default_provider_id)
        if provider:
            result["default"] = {
                "provider_id": provider.id,
                "model_id": provider.models[0] if provider.models else "",
                "executor": "direct_model",
                "route_version": registry.version,
            }
    return result


def _route_payload(config: RuntimeConfig, scope: str, key: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise TypeError("route payload must be an object")
    provider_id = str(payload.get("provider_id", payload.get("provider", ""))).strip()
    model_id = str(payload.get("model_id", payload.get("model", ""))).strip()
    if not provider_id:
        raise ValueError("provider_id is required")
    if not model_id:
        raise ValueError("model_id is required")
    registry = provider_registry(config)
    if normalize_id(provider_id) not in registry.providers:
        raise ValueError(f"unknown provider: {normalize_id(provider_id)}")
    return {
        "id": f"{normalize_id(scope)}:{normalize_id(key)}",
        "scope": scope,
        "key": key,
        "provider_id": provider_id,
        "model_id": model_id,
        "executor": str(payload.get("executor") or "direct_model")[:80],
        "fallbacks": payload.get("fallbacks", payload.get("fallback_chain", [])),
        "timeout_seconds": payload.get("timeout_seconds", payload.get("timeout")),
        "capabilities": payload.get("capabilities", []),
        "enabled": payload.get("enabled", True),
    }


def update_provider(config: RuntimeConfig, provider_id: str, payload: Mapping[str, Any]) -> RuntimeConfig:
    if not isinstance(payload, Mapping):
        raise TypeError("provider payload must be an object")
    raw = dict(payload)
    raw["id"] = provider_id or raw.get("id") or raw.get("provider_id")
    if not raw.get("id"):
        raise ValueError("provider id is required")
    if "base_url" in raw or "baseUrl" in raw:
        supplied_url = raw.get("base_url", raw.get("baseUrl"))
        if supplied_url and normalize_url(supplied_url) is None:
            raise ValueError("base_url must be an http(s) URL without credentials, query parameters, or fragments")
    # update_from_payload preserves an existing in-memory key when api_key is
    # omitted. A key supplied here is intentionally never persisted/public.
    update_payload: dict[str, Any] = {"provider_registry": {"providers": [raw]}}
    registry = provider_registry(config)
    default_id = registry.default_provider_id or normalize_id(config.provider_name or "")
    if normalize_id(str(raw["id"])) == default_id:
        # Keep the v1 single-provider fields as a compatibility projection of
        # the structured default provider.  Older clients continue to read
        # these fields while new tasks execute from the registry snapshot.
        if "base_url" in raw or "baseUrl" in raw:
            update_payload["base_url"] = raw.get("base_url", raw.get("baseUrl")) or ""
        if raw.get("api_key"):
            update_payload["api_key"] = raw.get("api_key")
        if raw.get("clear_api_key"):
            update_payload["clear_api_key"] = True
        tier_models = raw.get("tier_models")
        if isinstance(tier_models, Mapping):
            for tier, model in tier_models.items():
                update_payload[f"model_{tier}"] = model
    return updated_config(config, update_payload)


def disable_provider(config: RuntimeConfig, provider_id: str) -> RuntimeConfig:
    return update_provider(config, provider_id, {"enabled": False})


def update_route(config: RuntimeConfig, scope: str, key: str, payload: Mapping[str, Any]) -> RuntimeConfig:
    scope = normalize_id(scope, "tier")
    if scope not in {"task", "role", "pool", "tier", "default"}:
        raise ValueError("scope must be task, role, pool, tier, or default")
    key = str(key or "default").strip()
    return updated_config(config, {"provider_registry": {"routes": [_route_payload(config, scope, key, payload)]}})


def remove_route(config: RuntimeConfig, scope: str, key: str) -> RuntimeConfig:
    registry = provider_registry(config)
    target = f"{normalize_id(scope)}:{normalize_id(key)}"
    return replace(config, provider_registry=registry.without_route(target))


def provider_test(config: RuntimeConfig, provider_id: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    payload = payload if isinstance(payload, Mapping) else {}
    live = bool(payload.get("live", False))
    model_id = str(payload.get("model_id", payload.get("model", ""))).strip() or None
    try:
        timeout = max(1.0, min(30.0, float(payload.get("timeout_seconds", 10))))
    except (TypeError, ValueError):
        timeout = 10.0
    return test_provider(config, provider_id, live=live, model_id=model_id, timeout_seconds=timeout)


__all__ = [
    "provider_catalog",
    "route_catalog",
    "update_provider",
    "disable_provider",
    "update_route",
    "remove_route",
    "provider_test",
]
