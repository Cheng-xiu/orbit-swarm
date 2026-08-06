"""Provider and model route registry for Orbit Swarm.

The registry is intentionally small and standard-library only.  It keeps
provider credentials in memory, exposes only redacted metadata, and resolves
routes using a deterministic precedence order:

task override -> role -> pool -> tier -> default provider.

The legacy single-provider configuration is represented as an ordinary
``ProviderSpec`` so callers can adopt routing incrementally without changing
the v1 state document or the old ``RuntimeConfig`` fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
import os
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urlsplit


PROTOCOL_OPENAI = "openai_chat"
PROTOCOL_ANTHROPIC = "anthropic_messages"
PROTOCOL_CLI = "cli"
SUPPORTED_PROTOCOLS = (PROTOCOL_OPENAI, PROTOCOL_ANTHROPIC, PROTOCOL_CLI)
DEFAULT_TIMEOUT_SECONDS = 180.0
_ID_RE = re.compile(r"[^a-z0-9_.:-]+")


def normalize_id(value: Any, fallback: str = "provider") -> str:
    """Return a stable, non-empty identifier suitable for route keys."""

    text = str(value or "").strip().lower().replace("/", ":")
    text = _ID_RE.sub("-", text).strip("-.:_")
    return text[:120] or fallback


def normalize_protocol(value: Any, fallback: str = PROTOCOL_OPENAI) -> str:
    text = str(value or fallback).strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "openai": PROTOCOL_OPENAI,
        "openai_compatible": PROTOCOL_OPENAI,
        "chat_completions": PROTOCOL_OPENAI,
        "anthropic": PROTOCOL_ANTHROPIC,
        "messages": PROTOCOL_ANTHROPIC,
        "command": PROTOCOL_CLI,
    }
    text = aliases.get(text, text)
    return text if text in SUPPORTED_PROTOCOLS else fallback


def normalize_url(value: Any) -> str | None:
    text = str(value or "").strip().rstrip("/")
    if not text:
        return None
    if not text.lower().startswith(("http://", "https://")):
        return None
    try:
        parsed = urlsplit(text)
    except ValueError:
        return None
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return None
    if not parsed.hostname:
        return None
    return text[:500]


def _as_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", "disabled"}


def _as_float(value: Any, default: float = DEFAULT_TIMEOUT_SECONDS) -> float:
    try:
        return max(1.0, min(900.0, float(value)))
    except (TypeError, ValueError):
        return default


def _as_list(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    else:
        values = []
    result: list[str] = []
    for item in values:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text[:200])
    return tuple(result)


def _disabled_tokens(env: Mapping[str, str] | None = None) -> set[str]:
    env = env or os.environ
    values = set()
    for name in ("ORBIT_UNAVAILABLE_MODELS", "SWARM_DISABLED_MODELS", "SWARM_UNAVAILABLE_MODELS"):
        for item in str(env.get(name, "")).split(","):
            item = item.strip().lower()
            if item:
                values.add(item)
    return values


def _family(provider_id: str, protocol: str, model_id: str = "") -> str:
    text = f"{provider_id} {model_id}".lower()
    if protocol == PROTOCOL_ANTHROPIC or any(token in text for token in ("anthropic", "claude")):
        return "anthropic"
    if "deepseek" in text:
        return "deepseek"
    if any(token in text for token in ("openai", "a6api", "gpt", "o1", "o3")):
        return "openai"
    return "generic"


@dataclass(frozen=True)
class ProviderSpec:
    """A configured model service.  ``api_key`` is deliberately not public."""

    id: str
    name: str | None = None
    protocol: str = PROTOCOL_OPENAI
    base_url: str | None = None
    api_key: str | None = None
    api_key_env: str | None = None
    models: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    enabled: bool = True
    simulation: bool = False
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    priority: int = 100
    metadata: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", normalize_id(self.id))
        object.__setattr__(self, "name", str(self.name or self.id)[:160])
        object.__setattr__(self, "protocol", normalize_protocol(self.protocol))
        object.__setattr__(self, "base_url", normalize_url(self.base_url))
        object.__setattr__(self, "models", _as_list(self.models))
        object.__setattr__(self, "capabilities", _as_list(self.capabilities))
        object.__setattr__(self, "enabled", bool(self.enabled))
        object.__setattr__(self, "simulation", bool(self.simulation))
        object.__setattr__(self, "timeout_seconds", _as_float(self.timeout_seconds))
        try:
            priority = max(0, min(10_000, int(self.priority)))
        except (TypeError, ValueError):
            priority = 100
        object.__setattr__(self, "priority", priority)
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata or {})))

    @property
    def family(self) -> str:
        # Provider family describes the transport/account.  Model-specific
        # family checks happen in availability(model_id=...), so a provider
        # that serves both Claude and GPT models is not disabled wholesale.
        return _family(self.id, self.protocol)

    def key_configured(self, env: Mapping[str, str] | None = None) -> bool:
        env = env or os.environ
        return bool(self.api_key or (self.api_key_env and str(env.get(self.api_key_env, "")).strip()))

    def effective_key(self, env: Mapping[str, str] | None = None) -> str | None:
        env = env or os.environ
        return self.api_key or (str(env.get(self.api_key_env, "")).strip() if self.api_key_env else None) or None

    def availability(self, *, env: Mapping[str, str] | None = None, simulation_override: bool | None = None, model_id: str | None = None) -> tuple[bool, str]:
        env = env or os.environ
        simulated = self.simulation or bool(simulation_override)
        model_text = str(model_id or "").strip().lower()
        disabled = _disabled_tokens(env)
        model_family = _family(self.id, self.protocol, model_text)
        family_disabled = (model_family in disabled) if model_text else (not self.models and self.family in disabled)
        if self.id.lower() in disabled or family_disabled or model_text in disabled:
            return False, "disabled_by_environment"
        if not self.enabled:
            return False, "disabled"
        if simulated:
            return True, "simulation"
        if self.protocol == PROTOCOL_CLI:
            return True, "cli"
        if not self.base_url:
            return False, "base_url_missing"
        if not self.key_configured(env):
            return False, "api_key_missing"
        key = (self.effective_key(env) or "").lower()
        if any(marker in key for marker in ("invalid", "expired", "revoked", "disabled", "bad-key", "test-key")):
            return False, "api_key_invalid"
        return True, "configured"

    def public(self, *, env: Mapping[str, str] | None = None, simulation_override: bool | None = None) -> dict[str, Any]:
        available, reason = self.availability(env=env, simulation_override=simulation_override)
        model_states = [
            self.availability(env=env, simulation_override=simulation_override, model_id=model)
            for model in self.models
        ]
        available_models = sum(1 for state, _reason in model_states if state)
        if available and model_states and available_models == 0:
            available, reason = False, "models_unavailable"
        return {
            "id": self.id,
            "name": self.name or self.id,
            "protocol": self.protocol,
            "family": self.family,
            "base_url": self.base_url or "",
            "models": list(self.models),
            "available_models": available_models,
            "model_count": len(self.models),
            "capabilities": list(self.capabilities),
            "enabled": self.enabled,
            "simulation": self.simulation or bool(simulation_override),
            "configured": self.key_configured(env) or self.simulation or self.protocol == PROTOCOL_CLI,
            "available": available,
            "status": "online" if available else "offline",
            "reason": reason,
            "timeout_seconds": self.timeout_seconds,
            "priority": self.priority,
            "api_key_configured": self.key_configured(env),
            "api_key_hint": ("..." + (self.effective_key(env) or "")[-4:]) if self.effective_key(env) else "",
            # An environment variable name is not a secret and lets a
            # persisted metadata snapshot reconnect the provider on restart.
            "api_key_env": self.api_key_env or "",
        }


@dataclass(frozen=True)
class RouteSpec:
    """A route binding a scope/key to a provider model and optional fallbacks."""

    id: str
    scope: str
    key: str
    provider_id: str
    model_id: str
    executor: str = "direct_model"
    fallbacks: tuple[str, ...] = ()
    timeout_seconds: float | None = None
    capabilities: tuple[str, ...] = ()
    enabled: bool = True
    version: int = 1

    def __post_init__(self) -> None:
        scope = normalize_id(self.scope, "tier")
        key = normalize_id(self.key, "default")
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "key", key)
        object.__setattr__(self, "id", normalize_id(self.id, f"{scope}:{key}"))
        object.__setattr__(self, "provider_id", normalize_id(self.provider_id))
        object.__setattr__(self, "model_id", str(self.model_id or "").strip()[:200])
        object.__setattr__(self, "executor", str(self.executor or "direct_model").strip()[:80])
        object.__setattr__(self, "fallbacks", _as_list(self.fallbacks))
        object.__setattr__(self, "timeout_seconds", None if self.timeout_seconds is None else _as_float(self.timeout_seconds))
        object.__setattr__(self, "capabilities", _as_list(self.capabilities))
        object.__setattr__(self, "enabled", bool(self.enabled))
        try:
            version = max(1, int(self.version))
        except (TypeError, ValueError):
            version = 1
        object.__setattr__(self, "version", version)

    @property
    def key_name(self) -> str:
        return f"{self.scope}:{self.key}"

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "scope": self.scope,
            "key": self.key,
            "key_name": self.key_name,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "executor": self.executor,
            "fallbacks": list(self.fallbacks),
            "timeout_seconds": self.timeout_seconds,
            "capabilities": list(self.capabilities),
            "enabled": self.enabled,
            "version": self.version,
        }


@dataclass(frozen=True)
class ResolvedRoute:
    """Immutable task snapshot returned by :meth:`ProviderRegistry.resolve`."""

    route_id: str
    provider_id: str
    model_id: str
    executor: str
    protocol: str
    base_url: str | None
    api_key: str | None
    fallbacks: tuple[str, ...] = ()
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    capabilities: tuple[str, ...] = ()
    simulation: bool = False
    route_version: int = 1
    scope: str = "tier"
    key: str = "default"

    @property
    def id(self) -> str:
        return self.route_id

    def public(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "executor": self.executor,
            "protocol": self.protocol,
            "base_url": self.base_url or "",
            "fallbacks": list(self.fallbacks),
            "timeout_seconds": self.timeout_seconds,
            "capabilities": list(self.capabilities),
            "simulation": self.simulation,
            "route_version": self.route_version,
            "scope": self.scope,
            "key": self.key,
        }


def _provider_from_mapping(raw: Mapping[str, Any], *, env: Mapping[str, str], fallback_id: str = "provider") -> ProviderSpec | None:
    if not isinstance(raw, Mapping):
        return None
    provider_id = normalize_id(raw.get("id", raw.get("provider_id", raw.get("name", fallback_id))), fallback_id)
    key_env = str(raw.get("api_key_env", raw.get("api_key_ref", raw.get("env_key", raw.get("key_env", "")))) or "").strip()[:120] or None
    key = raw.get("api_key")
    # A mapping may reference an environment variable, but never requires
    # callers to put secrets into persisted JSON.
    if not key and key_env:
        key = None
    protocol = normalize_protocol(raw.get("protocol", raw.get("adapter", PROTOCOL_OPENAI)))
    return ProviderSpec(
        id=provider_id,
        name=raw.get("name") or raw.get("display_name") or provider_id,
        protocol=protocol,
        base_url=raw.get("base_url", raw.get("baseUrl", raw.get("url"))),
        api_key=str(key).strip() if key else None,
        api_key_env=key_env,
        models=_as_list(raw.get("models", raw.get("model_ids", ()))),
        capabilities=_as_list(raw.get("capabilities", ())),
        enabled=_as_bool(raw.get("enabled"), True),
        simulation=_as_bool(raw.get("simulation"), False),
        timeout_seconds=_as_float(raw.get("timeout_seconds", raw.get("timeout", DEFAULT_TIMEOUT_SECONDS))),
        priority=raw.get("priority", 100),
        metadata=raw.get("metadata", {}),
    )


def _route_from_mapping(raw: Mapping[str, Any], *, fallback_id: str = "route") -> RouteSpec | None:
    if not isinstance(raw, Mapping):
        return None
    scope = str(raw.get("scope", "tier") or "tier")
    key = str(raw.get("key", raw.get("name", "default")) or "default")
    route_id = raw.get("id", raw.get("route_id", f"{normalize_id(scope)}:{normalize_id(key)}"))
    provider_id = raw.get("provider_id", raw.get("provider", ""))
    model_id = raw.get("model_id", raw.get("model", ""))
    if not provider_id or not model_id:
        return None
    return RouteSpec(
        id=str(route_id or fallback_id),
        scope=scope,
        key=key,
        provider_id=str(provider_id),
        model_id=str(model_id),
        executor=str(raw.get("executor", raw.get("kind", "direct_model"))),
        fallbacks=_as_list(raw.get("fallbacks", raw.get("fallback_chain", ()))),
        timeout_seconds=raw.get("timeout_seconds", raw.get("timeout")),
        capabilities=_as_list(raw.get("capabilities", ())),
        enabled=_as_bool(raw.get("enabled"), True),
        version=raw.get("version", 1),
    )


@dataclass(frozen=True)
class ProviderRegistry:
    """Versioned provider and route collection.

    Methods return new registries instead of mutating the current snapshot;
    in-flight tasks can therefore retain their selected route safely while new
    tasks observe configuration updates.
    """

    providers: Mapping[str, ProviderSpec] = field(default_factory=dict)
    routes: Mapping[str, RouteSpec] = field(default_factory=dict)
    default_provider_id: str | None = None
    version: int = 1
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ), repr=False, compare=False)

    def __post_init__(self) -> None:
        providers: dict[str, ProviderSpec] = {}
        for key, value in dict(self.providers or {}).items():
            provider = value if isinstance(value, ProviderSpec) else _provider_from_mapping(value, env=self.env, fallback_id=str(key))
            if provider:
                providers[provider.id] = provider
        routes: dict[str, RouteSpec] = {}
        for key, value in dict(self.routes or {}).items():
            route = value if isinstance(value, RouteSpec) else _route_from_mapping(value, fallback_id=str(key))
            if route:
                routes[route.id] = route
        default_id = normalize_id(self.default_provider_id, "") if self.default_provider_id else None
        if default_id not in providers:
            default_id = next(iter(providers), None)
        try:
            version = max(1, int(self.version))
        except (TypeError, ValueError):
            version = 1
        object.__setattr__(self, "providers", MappingProxyType(providers))
        object.__setattr__(self, "routes", MappingProxyType(routes))
        object.__setattr__(self, "default_provider_id", default_id)
        object.__setattr__(self, "version", version)
        object.__setattr__(self, "env", MappingProxyType(dict(self.env or os.environ)))

    @classmethod
    def empty(cls, *, env: Mapping[str, str] | None = None) -> "ProviderRegistry":
        return cls({}, {}, None, 1, env or dict(os.environ))

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any] | None,
        *,
        env: Mapping[str, str] | None = None,
        base: "ProviderRegistry | None" = None,
    ) -> "ProviderRegistry":
        """Create or extend a registry from safe JSON-compatible metadata."""
        current = base or cls.empty(env=env)
        if not payload:
            return current
        return current.update_from_payload(payload)

    @classmethod
    def from_legacy(
        cls,
        provider_name: str | None,
        base_url: str | None,
        api_key: str | None,
        models: Mapping[str, str] | None,
        *,
        simulation: bool = False,
        env: Mapping[str, str] | None = None,
        existing: "ProviderRegistry | None" = None,
    ) -> "ProviderRegistry":
        env_map = dict(env or os.environ)
        provider_id = normalize_id(provider_name or "a6api", "a6api")
        # Keep an environment reference when possible so a persisted config
        # never needs to contain a key.  An explicitly supplied in-memory key
        # remains usable for the current process only.
        key_env = None
        if not api_key:
            for candidate in ("A6_OPENAI_API_KEY", "A6API_API_KEY", "OPENAI_API_KEY"):
                if str(env_map.get(candidate, "")).strip():
                    key_env = candidate
                    break
        provider = ProviderSpec(
            id=provider_id,
            name=provider_name or provider_id,
            protocol=PROTOCOL_OPENAI,
            base_url=base_url,
            api_key=api_key,
            api_key_env=key_env,
            models=tuple(str(value) for value in (models or {}).values() if value),
            simulation=bool(simulation),
            enabled=True,
        )
        providers = dict(existing.providers) if existing else {}
        providers[provider.id] = provider
        routes = dict(existing.routes) if existing else {}
        for tier, model in (models or {}).items():
            if not model:
                continue
            route_id = f"tier:{normalize_id(tier)}"
            routes[route_id] = RouteSpec(route_id, "tier", str(tier), provider.id, str(model), "direct_model")
        return cls(providers, routes, provider.id, (existing.version + 1 if existing else 1), env_map)

    @classmethod
    def from_environment(cls, *, legacy: Mapping[str, Any] | None = None, env: Mapping[str, str] | None = None) -> "ProviderRegistry":
        # A supplied mapping is an explicit isolated environment (useful for
        # tests and per-task snapshots).  Only the process environment gets the
        # implicit OpenClaw catalog; callers can still opt in with an explicit
        # OPENCLAW_CONFIG_PATH entry.
        isolated_env = env is not None
        env_map = dict(env if env is not None else os.environ)
        legacy = legacy or {}
        registry = cls.from_legacy(
            legacy.get("provider_name") or env_map.get("A6_PROVIDER_NAME", "a6api"),
            legacy.get("base_url") or env_map.get("A6_OPENAI_BASE_URL") or env_map.get("OPENAI_BASE_URL"),
            legacy.get("api_key"),
            legacy.get("models") or {},
            simulation=bool(legacy.get("simulation", str(env_map.get("SWARM_SIMULATION", "true")).lower() not in {"0", "false", "no", "off"})),
            env=env_map,
        )
        # Named providers use either a compact JSON object or predictable env
        # variables.  Invalid entries are ignored so one bad key cannot take
        # the whole cluster down.
        raw_json = env_map.get("ORBIT_PROVIDERS_JSON", "")
        if raw_json.strip():
            try:
                document = json.loads(raw_json)
                entries = document.get("providers", document) if isinstance(document, Mapping) else {}
                if isinstance(entries, Mapping):
                    entries = [dict(value, id=key) if isinstance(value, Mapping) else {} for key, value in entries.items()]
                if isinstance(entries, list):
                    for raw in entries:
                        provider = _provider_from_mapping(raw, env=env_map)
                        if provider:
                            registry = registry.with_provider(provider)
            except (TypeError, ValueError, json.JSONDecodeError):
                pass

        # MODE 0 has a stable product default even when this host has no
        # OpenClaw catalog.  The credential remains external; a missing key
        # simply leaves the CodeKey slot inactive in live mode.
        if "codekey" not in registry.providers:
            registry = registry.with_provider(ProviderSpec(
                id="codekey",
                name="CodeKey",
                protocol=PROTOCOL_OPENAI,
                base_url="https://codekey.ai/v1",
                api_key_env="CODEKEY_API_KEY",
                models=("claude-opus-5",),
                capabilities=("chat",),
                enabled=True,
                simulation=False,
                priority=50,
                metadata={"source": "builtin"},
            ))

        # Common provider aliases are opt-in when their key/base URL exists.
        common = (
            ("openai", PROTOCOL_OPENAI, ("OPENAI_BASE_URL", "OPENAI_API_BASE"), "OPENAI_API_KEY"),
            ("anthropic", PROTOCOL_ANTHROPIC, ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_BASE"), "ANTHROPIC_API_KEY"),
            ("deepseek", PROTOCOL_OPENAI, ("DEEPSEEK_BASE_URL",), "DEEPSEEK_API_KEY"),
        )
        for provider_id, protocol, url_names, key_name in common:
            base = next((env_map.get(name) for name in url_names if env_map.get(name)), None)
            key = env_map.get(key_name)
            model_text = env_map.get(f"ORBIT_{provider_id.upper()}_MODELS", env_map.get(f"{provider_id.upper()}_MODELS", ""))
            if not (base or key or model_text):
                continue
            if provider_id in registry.providers:
                continue
            registry = registry.with_provider(ProviderSpec(provider_id, provider_id, protocol, base, None, key_name, _as_list(model_text), (), True, False))

        # OpenClaw is a local provider catalog, not a network dependency.  It
        # is a useful source for already-authorized interfaces such as
        # codekey.ai.  Keys remain in this process only; persisted metadata
        # keeps the provider id, endpoint, model list, and environment hint.
        config_path_value = env_map.get("OPENCLAW_CONFIG_PATH")
        config_path = Path(config_path_value) if config_path_value else (None if isolated_env else Path.home() / ".openclaw" / "openclaw.json")
        try:
            if config_path is None:
                raise FileNotFoundError
            document = json.loads(config_path.read_text(encoding="utf-8"))
            entries = document.get("models", {}).get("providers", {}) if isinstance(document, Mapping) else {}
            if isinstance(entries, Mapping):
                for provider_id, raw in entries.items():
                    if not isinstance(raw, Mapping):
                        continue
                    api_name = str(raw.get("api") or raw.get("protocol") or "").lower()
                    protocol = PROTOCOL_ANTHROPIC if "anthropic" in api_name or "messages" in api_name else PROTOCOL_OPENAI
                    models_raw = raw.get("models", ())
                    models: list[str] = []
                    if isinstance(models_raw, Mapping):
                        models_raw = list(models_raw.values())
                    for model in models_raw if isinstance(models_raw, (list, tuple)) else (models_raw,):
                        if isinstance(model, Mapping):
                            model = model.get("id") or model.get("name")
                        if model:
                            models.append(str(model))
                    key = str(raw.get("apiKey") or raw.get("api_key") or "").strip() or None
                    env_key = str(raw.get("apiKeyEnv") or raw.get("api_key_env") or "").strip() or None
                    if not key and env_key:
                        key = str(env_map.get(env_key, "")).strip() or None
                    provider = ProviderSpec(
                        id=str(provider_id),
                        name=str(raw.get("name") or provider_id),
                        protocol=protocol,
                        base_url=raw.get("baseUrl") or raw.get("base_url") or raw.get("url"),
                        api_key=key,
                        api_key_env=env_key,
                        models=tuple(models),
                        capabilities=("chat",),
                        enabled=_as_bool(raw.get("enabled"), True),
                        simulation=False,
                        metadata={"source": "openclaw"},
                    )
                    if provider.base_url:
                        registry = registry.with_provider(provider)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass

        # Per-provider env declarations: ORBIT_PROVIDER_FOO_BASE_URL, etc.
        prefix = "ORBIT_PROVIDER_"
        ids: set[str] = set()
        for name in env_map:
            if name.startswith(prefix):
                remainder = name[len(prefix):]
                ids.add(remainder.split("_", 1)[0].lower())
        for provider_id in sorted(ids):
            upper = provider_id.upper()
            base = env_map.get(f"{prefix}{upper}_BASE_URL")
            key_name = env_map.get(f"{prefix}{upper}_API_KEY_ENV", f"{prefix}{upper}_API_KEY")
            key = env_map.get(f"{prefix}{upper}_API_KEY") if key_name == f"{prefix}{upper}_API_KEY" else None
            protocol = env_map.get(f"{prefix}{upper}_PROTOCOL", PROTOCOL_OPENAI)
            models = env_map.get(f"{prefix}{upper}_MODELS", "")
            if provider_id in registry.providers and not (base or key or models):
                continue
            registry = registry.with_provider(ProviderSpec(provider_id, provider_id, protocol, base, key, key_name, _as_list(models), (), _as_bool(env_map.get(f"{prefix}{upper}_ENABLED"), True), _as_bool(env_map.get(f"{prefix}{upper}_SIMULATION"), False)))

        raw_routes = env_map.get("ORBIT_ROUTES_JSON", "")
        if raw_routes.strip():
            try:
                document = json.loads(raw_routes)
                entries = document.get("routes", document) if isinstance(document, Mapping) else document
                if isinstance(entries, Mapping):
                    entries = [dict(value, id=key) if isinstance(value, Mapping) else {} for key, value in entries.items()]
                if isinstance(entries, list):
                    for raw in entries:
                        route = _route_from_mapping(raw)
                        if route:
                            registry = registry.with_route(route)
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        return registry

    def with_provider(self, provider: ProviderSpec | Mapping[str, Any]) -> "ProviderRegistry":
        value = provider if isinstance(provider, ProviderSpec) else _provider_from_mapping(provider, env=self.env)
        if not value:
            raise ValueError("invalid provider")
        providers = dict(self.providers)
        providers[value.id] = value
        return replace(self, providers=providers, version=self.version + 1, default_provider_id=self.default_provider_id or value.id)

    def without_provider(self, provider_id: str) -> "ProviderRegistry":
        key = normalize_id(provider_id)
        providers = {name: value for name, value in self.providers.items() if name != key}
        routes = {name: value for name, value in self.routes.items() if value.provider_id != key}
        default = self.default_provider_id if self.default_provider_id != key else next(iter(providers), None)
        return replace(self, providers=providers, routes=routes, default_provider_id=default, version=self.version + 1)

    def with_route(self, route: RouteSpec | Mapping[str, Any]) -> "ProviderRegistry":
        value = route if isinstance(route, RouteSpec) else _route_from_mapping(route)
        if not value:
            raise ValueError("invalid route")
        if value.provider_id not in self.providers:
            raise ValueError(f"unknown provider: {value.provider_id}")
        routes = dict(self.routes)
        routes[value.id] = value
        return replace(self, routes=routes, version=self.version + 1)

    def without_route(self, route_id: str) -> "ProviderRegistry":
        key = normalize_id(route_id)
        routes = {name: value for name, value in self.routes.items() if name != key}
        return replace(self, routes=routes, version=self.version + 1)

    def _find_route(self, scope: str, key: str) -> RouteSpec | None:
        scope_key = f"{normalize_id(scope)}:{normalize_id(key)}"
        route = self.routes.get(scope_key)
        if route and route.enabled:
            return route
        for candidate in self.routes.values():
            if candidate.enabled and candidate.scope == normalize_id(scope) and candidate.key == normalize_id(key):
                return candidate
        return None

    def _route_from_override(self, override: Mapping[str, Any] | None, *, tier: str, role: str | None, pool: str | None) -> RouteSpec | None:
        if isinstance(override, str):
            override = {"route_id": override}
        if not isinstance(override, Mapping):
            return None
        route_id = override.get("route_id")
        if route_id and normalize_id(route_id) in self.routes:
            return self.routes[normalize_id(route_id)]
        provider_id = override.get("provider_id", override.get("provider"))
        model_id = override.get("model_id", override.get("model"))
        if not provider_id and not model_id and not override.get("executor"):
            return None
        provider_id = normalize_id(provider_id or self.default_provider_id or "")
        if provider_id not in self.providers:
            return None
        return RouteSpec(
            str(route_id or f"task:{normalize_id(role or tier)}"),
            "task",
            str(override.get("key", role or tier)),
            provider_id,
            str(model_id or tier),
            str(override.get("executor", "direct_model")),
            _as_list(override.get("fallbacks", override.get("fallback_chain", ()))),
            override.get("timeout_seconds", override.get("timeout")),
            _as_list(override.get("capabilities", ())),
            _as_bool(override.get("enabled"), True),
            self.version,
        )

    def resolve(
        self,
        tier: str = "high",
        *,
        role: str | None = None,
        pool: str | None = None,
        model_id: str | None = None,
        task_override: Mapping[str, Any] | None = None,
        executor: str | None = None,
        simulation_override: bool | None = None,
    ) -> ResolvedRoute | None:
        """Resolve and snapshot a route; return ``None`` if no provider exists."""

        override = self._route_from_override(task_override, tier=tier, role=role, pool=pool)
        route = override
        if route is None and role:
            route = self._find_route("role", role) or self._find_route("role", normalize_id(role))
        if route is None and pool:
            route = self._find_route("pool", pool)
        if route is None:
            route = self._find_route("tier", tier)
        if route is None:
            route = self._find_route("default", "default")
        provider_id = normalize_id(route.provider_id if route else self.default_provider_id or "")
        if route and route.scope in {"role", "pool", "task", "default"}:
            requested_model = str(route.model_id or model_id or "")
        else:
            requested_model = str(model_id or (route.model_id if route else "") or "")
        # MODE role catalogs pass their preferred model as an override while
        # the compatibility tier route still points at the legacy GPT model.
        # In that one case, select a provider that explicitly advertises the
        # requested model.  A route whose own model already matches is an
        # explicit operator decision and must never be overridden.
        route_model_key = normalize_id(route.model_id, "") if route else ""
        requested_model_key = normalize_id(requested_model, "")
        may_match_catalog = route is None or (route.scope == "tier" and model_id is not None and requested_model_key != route_model_key)
        if may_match_catalog and requested_model_key:
            matches = []
            for candidate in self.providers.values():
                canonical = next((item for item in candidate.models if normalize_id(item, "") == requested_model_key), None)
                if canonical:
                    candidate_available = candidate.availability(
                        env=self.env,
                        simulation_override=simulation_override,
                        model_id=canonical,
                    )[0]
                    claude_preference = 0 if ("claude" in requested_model_key and (candidate.id == "codekey" or candidate.family == "anthropic")) else 1
                    matches.append((0 if candidate_available else 1, claude_preference, candidate.priority, candidate.id, canonical))
            if matches:
                _offline_rank, _preference, _priority, provider_id, requested_model = min(matches)
        provider = self.providers.get(provider_id)
        if not provider:
            return None
        selected_model = str(requested_model or (route.model_id if route else "") or (provider.models[0] if provider.models else ""))
        # Catalog labels are user-facing (for example ``GPT-5.6 Terra``),
        # while providers usually require their exact wire id
        # (``gpt-5.6-terra``).  Preserve the provider's canonical spelling
        # whenever the normalized identifiers match.
        canonical_model = next(
            (item for item in provider.models if normalize_id(item, "") == normalize_id(selected_model, "")),
            None,
        )
        if canonical_model:
            selected_model = canonical_model
        selected_executor = str(executor or (route.executor if route else "direct_model"))
        fallback_ids = route.fallbacks if route else ()
        timeout = (route.timeout_seconds if route and route.timeout_seconds else provider.timeout_seconds)
        available, _reason = provider.availability(env=self.env, simulation_override=simulation_override, model_id=selected_model)
        if not available and route:
            # Keep the route snapshot even when offline; execute_agent can walk
            # its fallback chain and the UI can show the inactive provider.
            pass
        return ResolvedRoute(
            route_id=route.id if route else f"provider:{provider.id}:{normalize_id(tier)}",
            provider_id=provider.id,
            model_id=selected_model,
            executor=selected_executor,
            protocol=provider.protocol,
            base_url=provider.base_url,
            api_key=provider.effective_key(self.env),
            fallbacks=_as_list(fallback_ids),
            timeout_seconds=float(timeout or DEFAULT_TIMEOUT_SECONDS),
            capabilities=tuple(dict.fromkeys((*provider.capabilities, *(route.capabilities if route else ())))),
            simulation=bool(provider.simulation or simulation_override),
            route_version=max(self.version, route.version if route else 1),
            scope=route.scope if route else "tier",
            key=route.key if route else str(tier),
        )

    def resolve_fallbacks(self, selected: ResolvedRoute, *, simulation_override: bool | None = None) -> list[ResolvedRoute]:
        """Resolve fallback route IDs or provider IDs in declaration order."""

        result: list[ResolvedRoute] = []
        seen = {selected.route_id}
        for item in selected.fallbacks:
            key = normalize_id(item)
            # Accept ergonomic ``route:foo`` / ``provider:bar`` references in
            # addition to bare IDs.  The prefix is metadata, not part of the
            # canonical registry key.
            prefix, _, suffix = key.partition(":")
            if prefix in {"route", "provider"} and suffix:
                key = suffix
            route = self.routes.get(key)
            if route:
                candidate = self.resolve(route.key, role=route.key if route.scope == "role" else None, pool=route.key if route.scope == "pool" else None, model_id=route.model_id, simulation_override=simulation_override)
            elif key in self.providers:
                provider = self.providers[key]
                candidate = self.resolve(selected.key, model_id=selected.model_id, simulation_override=simulation_override)
                if candidate:
                    candidate = replace(
                        candidate,
                        route_id=f"provider:{provider.id}:{normalize_id(selected.key)}",
                        provider_id=provider.id,
                        protocol=provider.protocol,
                        base_url=provider.base_url,
                        api_key=provider.effective_key(self.env),
                        fallbacks=(),
                        simulation=bool(provider.simulation or simulation_override),
                    )
            else:
                candidate = None
            if candidate and candidate.route_id not in seen:
                seen.add(candidate.route_id)
                result.append(candidate)
        return result

    def providers_public(self, *, simulation_override: bool | None = None) -> list[dict[str, Any]]:
        return [provider.public(env=self.env, simulation_override=simulation_override) for provider in sorted(self.providers.values(), key=lambda item: (item.priority, item.id))]

    def routes_public(self) -> list[dict[str, Any]]:
        return [route.public() for route in sorted(self.routes.values(), key=lambda item: item.id)]

    def public(self, *, simulation_override: bool | None = None) -> dict[str, Any]:
        return {
            "version": self.version,
            "default_provider_id": self.default_provider_id or "",
            "providers": self.providers_public(simulation_override=simulation_override),
            "routes": self.routes_public(),
        }

    def health(self, provider_id: str | None = None, *, simulation_override: bool | None = None) -> dict[str, Any]:
        if provider_id:
            provider = self.providers.get(normalize_id(provider_id))
            if not provider:
                return {"provider_id": normalize_id(provider_id), "available": False, "reason": "unknown_provider"}
            return provider.public(env=self.env, simulation_override=simulation_override)
        return {"version": self.version, "providers": self.providers_public(simulation_override=simulation_override)}

    def update_from_payload(self, payload: Mapping[str, Any]) -> "ProviderRegistry":
        """Apply additive provider/route updates without accepting raw persisted secrets."""

        if not isinstance(payload, Mapping):
            raise TypeError("registry payload must be an object")
        current = self
        providers = payload.get("providers")
        if isinstance(providers, Mapping):
            providers = [dict(value, id=key) if isinstance(value, Mapping) else {} for key, value in providers.items()]
        if isinstance(providers, list):
            for raw in providers:
                if not isinstance(raw, Mapping):
                    continue
                # API updates may use ``api_key`` transiently.  It stays only
                # in the in-memory object and is never returned by ``public``.
                raw_value = dict(raw)
                raw_id = normalize_id(raw_value.get("id", raw_value.get("provider_id", raw_value.get("name", "provider"))))
                previous = current.providers.get(raw_id)
                if previous:
                    # Provider PATCHes commonly contain only ``enabled`` or
                    # a new endpoint.  Preserve every omitted field so a
                    # health toggle cannot accidentally erase the model list
                    # or base URL.  Explicit empty values still mean clear.
                    for field_name in (
                        "name", "protocol", "base_url", "models", "capabilities",
                        "enabled", "simulation", "timeout_seconds", "priority", "metadata",
                    ):
                        if field_name not in raw_value:
                            raw_value[field_name] = getattr(previous, field_name)
                    if not raw_value.get("api_key"):
                        raw_value["api_key"] = previous.api_key
                    if not raw_value.get("api_key_env"):
                        raw_value["api_key_env"] = previous.api_key_env
                value = _provider_from_mapping(raw_value, env=current.env)
                if value:
                    current = current.with_provider(value)
        routes = payload.get("routes")
        if isinstance(routes, Mapping):
            routes = [dict(value, id=key) if isinstance(value, Mapping) else {} for key, value in routes.items()]
        if isinstance(routes, list):
            for raw in routes:
                if not isinstance(raw, Mapping):
                    continue
                value = _route_from_mapping(raw)
                if value:
                    current = current.with_route(value)
        default = payload.get("default_provider_id", payload.get("default_provider"))
        if default:
            key = normalize_id(default)
            if key not in current.providers:
                raise ValueError(f"unknown provider: {key}")
            current = replace(current, default_provider_id=key, version=current.version + 1)
        return current


def registry_from_runtime(config: Any) -> ProviderRegistry:
    """Build a registry from either a new or legacy ``RuntimeConfig`` object."""

    existing = getattr(config, "provider_registry", None)
    if isinstance(existing, ProviderRegistry):
        return existing
    return ProviderRegistry.from_legacy(
        getattr(config, "provider_name", None),
        getattr(config, "base_url", None),
        getattr(config, "api_key", None),
        getattr(config, "models", {}) or {},
        simulation=bool(getattr(config, "simulation", False)),
    )


# Naming aliases keep the registry easy to adopt from API modules that use a
# provider-profile vocabulary.  The canonical dataclass remains
# ``ProviderSpec`` so existing integrations can be explicit about the wire
# shape.
ProviderProfile = ProviderSpec
load_registry = registry_from_runtime


__all__ = [
    "PROTOCOL_OPENAI",
    "PROTOCOL_ANTHROPIC",
    "PROTOCOL_CLI",
    "SUPPORTED_PROTOCOLS",
    "ProviderSpec",
    "ProviderProfile",
    "RouteSpec",
    "ResolvedRoute",
    "ProviderRegistry",
    "normalize_id",
    "normalize_protocol",
    "normalize_url",
    "registry_from_runtime",
    "load_registry",
]
