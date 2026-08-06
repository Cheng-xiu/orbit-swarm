"""Dynamic multi-mode agent cluster primitives.

The original Orbit Swarm runtime contains a task-oriented, approval-gated
workflow.  This module supplies the broader MODE 0-3 contract independently of
that workflow so both HTTP entry points can expose the same cluster state.  It
is deliberately standard-library only: a broker, health monitor, context
budget helper and deterministic dispute process can later be backed by Redis,
an external model router, or a durable event store without changing callers.
"""

from __future__ import annotations

from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import os
import random
import threading
import time
from typing import Any, Callable, Iterable, Mapping
from uuid import uuid4


MODE_LABELS = {
    0: "单Agent模式",
    1: "中档模式",
    2: "高档模式",
    3: "极限模式",
}

# Stable machine keys for the browser.  Human-readable Chinese role names are
# still returned in ``role_status`` so persisted task data remains legible.
ROLE_KEYS = {
    "通用助理": "general",
    "总管理（GM）": "gm",
    "全栈开发": "fullstack",
    "后端/数据库": "backend",
    "测试工程师": "testing",
    "文档/运维": "ops",
    "系统架构师": "architect",
    "前端TL": "frontend_tl",
    "后端TL": "backend_tl",
    "数据TL": "data_tl",
    "前端开发组": "frontend",
    "后端开发组": "backend",
    "数据库/缓存组": "data",
    "测试开发组": "testing",
    "安全审计员": "security",
    "文档编写组": "docs",
    "运维实施组": "ops",
    "人力资源（HR）": "hr",
    "超级网关": "gateway",
    "辩论主持人": "debate_host",
    "HR": "hr",
    "观察员": "observer",
    "编码池长": "coding_lead",
    "编码执行组": "coding",
    "测试池长": "testing_lead",
    "测试执行组": "testing",
    "安全池长": "security_lead",
    "安全执行组": "security",
    "文档池长": "docs_lead",
    "文档执行组": "docs",
    "性能池长": "performance_lead",
    "性能执行组": "performance",
    "辩论储备组（辩手）": "debaters",
}

# Routing needs a one-to-one key even when several roles share the same
# presentation group (for example three different testing roles).  ROLE_KEYS
# above intentionally remains compact for colors and aggregate counters.
ROLE_ROUTE_KEYS = {
    "通用助理": "general_assistant",
    "总管理（GM）": "general_manager",
    "全栈开发": "fullstack_developer",
    "后端/数据库": "backend_database",
    "测试工程师": "test_engineer",
    "文档/运维": "docs_operations",
    "系统架构师": "system_architect",
    "前端TL": "frontend_tl",
    "后端TL": "backend_tl",
    "数据TL": "data_tl",
    "前端开发组": "frontend_developers",
    "后端开发组": "backend_developers",
    "数据库/缓存组": "database_cache",
    "测试开发组": "test_developers",
    "安全审计员": "security_auditor",
    "文档编写组": "documentation_writers",
    "运维实施组": "operations_implementers",
    "人力资源（HR）": "human_resources",
    "超级网关": "super_gateway",
    "辩论主持人": "debate_host",
    "HR": "cluster_hr",
    "观察员": "observers",
    "编码池长": "coding_lead",
    "编码执行组": "coding_workers",
    "测试池长": "testing_lead",
    "测试执行组": "testing_workers",
    "安全池长": "security_lead",
    "安全执行组": "security_workers",
    "文档池长": "documentation_lead",
    "文档执行组": "documentation_workers",
    "性能池长": "performance_lead",
    "性能执行组": "performance_workers",
    "辩论储备组（辩手）": "debate_reserve",
}


@dataclass(frozen=True)
class RoleSpec:
    """Maximum slot definition for one role in a mode."""

    role: str
    max_count: int
    model: str
    duty: str
    pool: str = "general"
    # These optional route fields make a role self-describing while preserving
    # the legacy catalog, whose entries only supplied a display model.
    provider_id: str | None = None
    executor: str = "direct_model"
    configured: bool = False


# Keep the catalog data-only so a deployment can inspect it without starting a
# model client.  Names mirror the product contract and are also stable values
# for the front-end status panel and log filters.
ROLE_CATALOG: dict[int, tuple[RoleSpec, ...]] = {
    0: (
        RoleSpec("通用助理", 1, "Claude Opus 5", "直接理解并回答用户请求", "general"),
    ),
    1: (
        RoleSpec("总管理（GM）", 1, "Claude Opus 5", "拆解需求、分配任务、一票否决争议", "management"),
        RoleSpec("全栈开发", 1, "GPT-5.6 Terra", "实现业务逻辑和 API", "engineering"),
        RoleSpec("后端/数据库", 1, "GPT-5.6 Terra", "设计数据存储并优化查询", "data"),
        RoleSpec("测试工程师", 1, "DeepSeek V4 Flash", "编写和执行测试用例", "quality"),
        RoleSpec("文档/运维", 1, "DeepSeek V4 Flash", "维护文档和部署脚本", "operations"),
    ),
    2: (
        RoleSpec("系统架构师", 1, "Claude Opus 5", "技术选型、拆解大任务、主持跨组评审", "management"),
        RoleSpec("前端TL", 1, "GPT-5.6 Sol", "前端组管理与审核", "engineering"),
        RoleSpec("后端TL", 1, "GPT-5.6 Sol", "后端组管理与审核", "engineering"),
        RoleSpec("数据TL", 1, "GPT-5.6 Sol", "数据组管理与审核", "data"),
        RoleSpec("前端开发组", 3, "GPT-5.6 Terra", "具体前端实现", "engineering"),
        RoleSpec("后端开发组", 3, "GPT-5.6 Terra", "具体后端实现", "engineering"),
        RoleSpec("数据库/缓存组", 2, "GPT-5.6 Terra", "数据存储与缓存策略", "data"),
        RoleSpec("测试开发组", 3, "DeepSeek V4 Flash", "自动化测试编写与执行", "quality"),
        RoleSpec("安全审计员", 1, "GPT-5.6 Sol", "安全漏洞审查", "security"),
        RoleSpec("文档编写组", 2, "DeepSeek V4 Flash", "技术文档撰写", "documentation"),
        RoleSpec("运维实施组", 1, "DeepSeek V4 Flash", "CI/CD 与环境配置", "operations"),
        RoleSpec("人力资源（HR）", 1, "GPT-5.6 Luna", "监控错误率，超阈值时停用并通知", "management"),
    ),
    3: (
        RoleSpec("超级网关", 1, "GPT-5.6 Luna", "意图识别与分发到各专业池", "management"),
        RoleSpec("辩论主持人", 1, "Claude Opus 5", "组织跨池辩论并控制流程", "debate"),
        RoleSpec("HR", 1, "GPT-5.6 Sol", "全集群健康监控并处理平局", "management"),
        RoleSpec("观察员", 2, "DeepSeek V4 Flash", "日志与资源消耗监控", "observability"),
        RoleSpec("编码池长", 1, "Claude Opus 5", "指导编码池并审核核心代码", "engineering"),
        RoleSpec("编码执行组", 20, "GPT-5.6 Terra", "执行常规编码任务", "engineering"),
        RoleSpec("测试池长", 1, "GPT-5.6 Sol", "制定测试策略并审核覆盖率", "quality"),
        RoleSpec("测试执行组", 15, "DeepSeek V4 Flash", "执行大规模自动化测试", "quality"),
        RoleSpec("安全池长", 1, "GPT-5.6 Sol", "制定安全标准并审核", "security"),
        RoleSpec("安全执行组", 10, "GPT-5.6 Sol", "挖掘并修复漏洞", "security"),
        RoleSpec("文档池长", 1, "Claude Opus 5", "审核规范与架构文档", "documentation"),
        RoleSpec("文档执行组", 10, "DeepSeek V4 Flash", "批量生成和维护文档", "documentation"),
        RoleSpec("性能池长", 1, "GPT-5.6 Terra", "制定性能指标与压测方案", "performance"),
        RoleSpec("性能执行组", 5, "GPT-5.6 Terra", "压测执行与瓶颈分析", "performance"),
        RoleSpec("辩论储备组（辩手）", 30, "GPT-5.6 Terra", "仅在辩论时抽取为正反方辩手", "debate"),
    ),
}


def parse_mode(value: Any = None, default: int = 0) -> int:
    """Normalize an explicit mode or MODE/ORBIT_MODE environment value."""

    value = default if value is None else value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized.startswith("mode"):
            normalized = normalized[4:].strip(" _-")
        aliases = {"single": 0, "mid": 1, "medium": 1, "high": 2, "extreme": 3, "max": 3}
        if normalized in aliases:
            return aliases[normalized]
        value = normalized
    try:
        return max(0, min(3, int(value)))
    except (TypeError, ValueError):
        return max(0, min(3, int(default or 0)))


def configured_mode(env: Mapping[str, str] | None = None) -> int:
    env = env or os.environ
    raw = next(
        (env.get(name) for name in ("ORBIT_MODE", "ORBIT_SWARM_MODE", "SWARM_MODE", "MODE") if env.get(name) is not None),
        0,
    )
    return parse_mode(raw)


def role_specs(mode: Any = None) -> tuple[RoleSpec, ...]:
    return tuple(_role_with_defaults(spec) for spec in ROLE_CATALOG[parse_mode(mode)])


SUPPORTED_EXECUTORS = ("direct_model", "openclaw", "codex", "claude_code")
AGENT_NAME_POOL = (
    "Aster", "Nova", "Milo", "Iris", "Theo", "Luna", "Kai", "Rin",
    "阿澈", "小满", "知夏", "星野", "安然", "子墨", "若溪", "言川",
)

# Defaults are deliberately conservative.  Direct HTTP remains the most
# portable path for coordination, data, testing, security, and Claude roles;
# coding roles use Codex; documentation/operations roles use OpenClaw.  Claude
# Code stays opt-in because it requires its own native Anthropic-compatible
# credentials and local CLI configuration.
DEFAULT_CODEX_ROLES = {
    "全栈开发",
    "前端TL",
    "后端TL",
    "前端开发组",
    "后端开发组",
    "编码执行组",
}
DEFAULT_OPENCLAW_ROLES = {
    "文档/运维",
    "文档编写组",
    "运维实施组",
    "观察员",
    "文档池长",
    "文档执行组",
}


def default_executor_for_role(role: str, pool: str = "", model: str = "") -> str:
    if role in DEFAULT_CODEX_ROLES:
        return "codex"
    if role in DEFAULT_OPENCLAW_ROLES:
        return "openclaw"
    return "direct_model"


def default_provider_for_model(model: str) -> str | None:
    family = _model_family(model)
    return {"anthropic": "codekey", "deepseek": "deepseek", "openai": "a6api"}.get(family)


def role_route_key(role: str) -> str:
    return ROLE_ROUTE_KEYS.get(str(role), _slug(str(role)))


def _role_with_defaults(spec: RoleSpec) -> RoleSpec:
    return replace(
        spec,
        # Provider selection is resolved against the live registry by model
        # capability.  Leaving this empty keeps an explicit role route (or a
        # custom provider added later) authoritative instead of hard-coding a
        # provider that may not exist in an older installation.
        provider_id=spec.provider_id,
        executor=(
            spec.executor
            if spec.executor and spec.executor != "direct_model"
            else default_executor_for_role(spec.role, spec.pool, spec.model)
        ),
    )


def role_catalog() -> list[dict[str, Any]]:
    """Return the selectable role directory without making it mode-specific."""
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for catalog_mode, specs in ROLE_CATALOG.items():
        for raw_spec in specs:
            spec = _role_with_defaults(raw_spec)
            if spec.role in seen:
                continue
            seen.add(spec.role)
            rows.append({
                "role": spec.role,
                "role_key": role_route_key(spec.role),
                "group_key": ROLE_KEYS.get(spec.role, _slug(spec.role)),
                "default_mode": catalog_mode,
                "max_count": spec.max_count,
                "model": spec.model,
                "provider_id": spec.provider_id or "",
                "recommended_provider_id": default_provider_for_model(spec.model) or "",
                "executor": spec.executor,
                "pool": spec.pool,
                "duty": spec.duty,
            })
    return rows


def configured_role_specs(mode: Any, config: Any | None = None) -> tuple[RoleSpec, ...]:
    """Overlay a persisted mode composition onto the built-in role catalog.

    ``agent_profiles`` is intentionally additive.  A missing or malformed
    profile keeps the exact historical mode catalog, so old state documents
    and old clients remain valid.
    """
    selected_mode = parse_mode(mode)
    profiles = getattr(config, "agent_profiles", {}) if config is not None else {}
    entries = profiles.get(str(selected_mode), profiles.get(selected_mode)) if isinstance(profiles, dict) else None
    if not isinstance(entries, list):
        return role_specs(selected_mode)
    lookup = {item["role"]: item for item in role_catalog()}
    result: list[RoleSpec] = []
    for raw in entries:
        if not isinstance(raw, dict):
            continue
        role = str(raw.get("role") or raw.get("name") or "").strip()
        base = lookup.get(role)
        if not base:
            continue
        try:
            count = max(1, min(100, int(raw.get("max_count", raw.get("slots", base["max_count"])))) )
        except (TypeError, ValueError):
            count = base["max_count"]
        executor = str(raw.get("executor") or base.get("executor") or "direct_model").strip().lower()
        executor = executor if executor in SUPPORTED_EXECUTORS else str(base.get("executor") or "direct_model")
        provider_id = str(raw.get("provider_id") or raw.get("provider") or base.get("provider_id") or "").strip() or None
        registry = getattr(config, "provider_registry", None) if config is not None else None
        if provider_id and registry is not None and provider_id not in getattr(registry, "providers", {}):
            provider_id = None
        result.append(RoleSpec(
            role, count, str(raw.get("model") or raw.get("model_id") or base["model"]),
            str(raw.get("duty") or base["duty"]), str(raw.get("pool") or base["pool"]), provider_id, executor, True,
        ))
    # A present empty list is an explicit user choice.  Only a missing or
    # malformed profile falls back to the historical built-in composition.
    return tuple(result)


def stable_agent_name(agent_id: str) -> str:
    """Choose a repeatable friendly name for a slot across runtime rebuilds."""
    return AGENT_NAME_POOL[random.Random("orbit-name:" + str(agent_id)).randrange(len(AGENT_NAME_POOL))]


def role_max_counts(mode: Any = None) -> dict[str, int]:
    return {spec.role: spec.max_count for spec in role_specs(mode)}


def expected_slot_count(mode: Any = None) -> int:
    return sum(spec.max_count for spec in role_specs(mode))


def _model_family(model: str) -> str:
    text = str(model or "").lower()
    if "claude" in text or "anthropic" in text:
        return "anthropic"
    if "deepseek" in text:
        return "deepseek"
    if "gpt" in text or "openai" in text:
        return "openai"
    return "generic"


def _model_tier(model: str) -> str:
    text = str(model or "").lower()
    if "opus" in text or text.endswith(" sol") or "deepseek" in text:
        return "high"
    if "luna" in text:
        return "low"
    return "medium"


def model_available(model: str, *, simulation: bool | None = None, env: Mapping[str, str] | None = None) -> bool:
    """Check a model without making a network call.

    Simulation is intentionally considered available so a local installation
    remains usable without credentials.  In live mode, the provider-specific
    key is required; ``ORBIT_UNAVAILABLE_MODELS`` can force individual model
    families off for deterministic quota/key-failure tests.
    """

    env = env or os.environ
    if simulation is None:
        simulation = str(env.get("SWARM_SIMULATION", "true")).strip().lower() not in {"0", "false", "no", "off"}
    disabled = {
        item.strip().lower()
        for raw in (
            env.get("ORBIT_UNAVAILABLE_MODELS", ""),
            env.get("SWARM_DISABLED_MODELS", ""),
            env.get("SWARM_UNAVAILABLE_MODELS", ""),
        )
        for item in str(raw).split(",")
        if item.strip()
    }
    model_text = str(model or "").lower()
    if model_text in disabled or _model_family(model) in disabled:
        return False
    if simulation:
        return True
    family = _model_family(model)
    key_names = {
        "anthropic": ("ANTHROPIC_API_KEY",),
        "openai": ("OPENAI_API_KEY", "A6_OPENAI_API_KEY", "A6API_API_KEY"),
        "deepseek": ("DEEPSEEK_API_KEY", "OPENAI_API_KEY"),
        "generic": ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"),
    }
    values = [str(env.get(name, "")).strip() for name in key_names[family]]
    values = [value for value in values if value]
    # A local health check cannot safely spend quota.  Treat the common
    # explicit invalid-key markers as unavailable so operators/tests can
    # exercise elastic activation deterministically.  An explicitly invalid
    # alias wins over a stale fallback alias (for example OPENAI_API_KEY over
    # an old A6_OPENAI_API_KEY value).
    markers = ("invalid", "expired", "revoked", "disabled", "bad-key", "test-key")
    if any(any(marker in value.lower() for marker in markers) for value in values):
        return False
    return bool(values)


def _role_capacity(spec: RoleSpec, env: Mapping[str, str]) -> int:
    """Return the resource-limited active count for a catalog role.

    Every slot remains visible in telemetry. These optional caps decide how
    many slots are actually activated on a constrained host or test setup.
    """
    slug = ROLE_KEYS.get(spec.role, _slug(spec.role)).replace("-", "_").upper()
    for name in (
        f"ORBIT_MAX_{slug}",
        f"SWARM_MAX_{slug}",
        f"ORBIT_MAX_ROLE_{slug}",
        f"SWARM_MAX_ROLE_{slug}",
    ):
        raw = env.get(name)
        if raw is None or str(raw).strip() == "":
            continue
        try:
            return max(0, min(spec.max_count, int(raw)))
        except (TypeError, ValueError):
            continue
    return spec.max_count


def utc_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass
class AgentRecord:
    id: str
    role: str
    role_index: int
    model: str
    duty: str
    pool: str
    agent_name: str = ""
    status: str = "inactive"
    active: bool = False
    simulated: bool = True
    heartbeat_at: str | None = None
    last_error: str | None = None
    restart_count: int = 0
    completed_tasks: int = 0
    failed_tasks: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def heartbeat(self) -> None:
        self.heartbeat_at = utc_now()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "role": self.role,
            "role_key": ROLE_KEYS.get(self.role, _slug(self.role)),
            "route_key": role_route_key(self.role),
            "role_index": self.role_index,
            "agent_name": self.agent_name or stable_agent_name(self.id),
            "display_name": self.agent_name or stable_agent_name(self.id),
            "model": self.model,
            "model_name": self.model,
            "duty": self.duty,
            "pool": self.pool,
            "status": self.status,
            "active": self.active,
            "simulated": self.simulated,
            "heartbeat_at": self.heartbeat_at,
            "last_error": self.last_error,
            "restart_count": self.restart_count,
            "completed_tasks": self.completed_tasks,
            "failed_tasks": self.failed_tasks,
            **self.metadata,
        }


@dataclass(frozen=True)
class ClusterEvent:
    topic: str
    kind: str
    message: str
    timestamp: str
    agent_id: str | None = None
    task_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result = {
            "topic": self.topic,
            "type": self.kind,
            "kind": self.kind,
            "message": self.message,
            "timestamp": self.timestamp,
            "agent_id": self.agent_id,
            "task_id": self.task_id,
        }
        result.update(self.payload)
        return result


class PubSubBroker:
    """Small in-process topic broker with wildcard subscriptions.

    Subscribers are callbacks receiving a dictionary.  Callback failures are
    isolated, which keeps a stale websocket or plugin from stopping the
    cluster.  A bounded history makes late status panels useful without a
    second storage dependency.
    """

    def __init__(self, history_size: int = 5000) -> None:
        self._subscriptions: dict[str, dict[str, Callable[[dict[str, Any]], Any]]] = defaultdict(dict)
        self._history: deque[dict[str, Any]] = deque(maxlen=max(1, int(history_size)))
        self._lock = threading.RLock()

    def subscribe(self, topic: str, callback: Callable[[dict[str, Any]], Any]) -> str:
        token = str(uuid4())
        with self._lock:
            self._subscriptions[str(topic)][token] = callback
        return token

    def unsubscribe(self, topic: str, token: str) -> bool:
        with self._lock:
            callbacks = self._subscriptions.get(str(topic), {})
            existed = token in callbacks
            callbacks.pop(token, None)
            if not callbacks:
                self._subscriptions.pop(str(topic), None)
            return existed

    def publish(self, topic: str, message: Mapping[str, Any]) -> dict[str, Any]:
        payload = dict(message)
        payload.setdefault("topic", str(topic))
        payload.setdefault("timestamp", utc_now())
        with self._lock:
            self._history.append(dict(payload))
            callbacks = [
                callback
                for key, entries in self._subscriptions.items()
                if key == topic or key == "*" or (key.endswith(".*") and str(topic).startswith(key[:-1]))
                for callback in entries.values()
            ]
        for callback in callbacks:
            try:
                callback(dict(payload))
            except Exception:
                continue
        return payload

    def history(self, topic: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            values = list(self._history)
        if topic:
            values = [item for item in values if item.get("topic") == topic]
        return values[-max(1, int(limit)) :]


class ContextManager:
    """Estimate token use and compact a transcript at a configurable ratio."""

    def __init__(self, context_limit: int = 128_000, compression_threshold: float = 0.85) -> None:
        self.context_limit = max(256, int(context_limit))
        self.compression_threshold = max(0.5, min(0.99, float(compression_threshold)))

    @staticmethod
    def estimate_tokens(value: Any) -> int:
        if isinstance(value, str):
            # Four characters per token is a conservative estimate for mixed
            # Chinese/English task text and costs no tokenizer dependency.
            return max(1, (len(value) + 3) // 4)
        if isinstance(value, Mapping):
            return sum(ContextManager.estimate_tokens(k) + ContextManager.estimate_tokens(v) for k, v in value.items())
        if isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray)):
            return sum(ContextManager.estimate_tokens(item) for item in value)
        return max(1, len(str(value)) // 4) if value is not None else 0

    def usage(self, transcript: Any) -> dict[str, Any]:
        tokens = self.estimate_tokens(transcript)
        ratio = tokens / self.context_limit
        return {
            "estimated_tokens": tokens,
            "context_limit": self.context_limit,
            "ratio": round(ratio, 4),
            "threshold": self.compression_threshold,
            "needs_compression": ratio >= self.compression_threshold,
        }

    def compress(self, transcript: list[Any], *, summary: str | None = None, keep: int = 8) -> tuple[list[Any], str, dict[str, Any]]:
        original = self.estimate_tokens(transcript)
        if summary is None:
            pieces = []
            for item in transcript[:-max(1, keep)]:
                if isinstance(item, Mapping):
                    role = item.get("role") or item.get("speaker") or "event"
                    content = str(item.get("content") or item.get("message") or "").replace("\n", " ")
                    if content:
                        pieces.append(f"{role}: {content[:240]}")
                elif str(item).strip():
                    pieces.append(str(item)[:240])
            summary = "Context summary: " + " | ".join(pieces[-24:])
        compacted = [{"role": "system", "type": "context_summary", "content": summary, "timestamp": utc_now()}]
        compacted.extend(transcript[-max(1, keep) :])
        compressed = self.estimate_tokens(compacted)
        return compacted, summary, {
            "original_tokens": original,
            "compressed_tokens": compressed,
            "saved_tokens": max(0, original - compressed),
            "ratio_after": round(compressed / self.context_limit, 4),
        }


class ClusterRuntime:
    """Lifecycle, health and dispute coordinator for one configured mode."""

    TOPICS = ("global", "architecture", "development", "testing", "debate", "hr", "status")

    def __init__(
        self,
        mode: Any = None,
        *,
        env: Mapping[str, str] | None = None,
        config: Any | None = None,
        simulation: bool | None = None,
        heartbeat_timeout: float = 30.0,
        context_limit: int = 128_000,
        compression_threshold: float = 0.85,
        fallback_model: str | None = None,
        broker: PubSubBroker | None = None,
        autostart: bool = False,
    ) -> None:
        self.env = dict(env or os.environ)
        self.config = config
        self.config_version = int(getattr(getattr(config, "provider_registry", None), "version", 0) or 0)
        self.mode = configured_mode(self.env) if mode is None else parse_mode(mode)
        self.mode_label = MODE_LABELS[self.mode]
        self.simulation = (
            str(self.env.get("SWARM_SIMULATION", "true")).lower() not in {"0", "false", "no", "off"}
            if simulation is None
            else bool(simulation)
        )
        self.heartbeat_timeout = max(1.0, float(heartbeat_timeout))
        try:
            self.hr_error_threshold = max(0.0, min(1.0, float(self.env.get("ORBIT_HR_ERROR_THRESHOLD", "0.75"))))
        except (TypeError, ValueError):
            self.hr_error_threshold = 0.75
        try:
            self.hr_min_tasks = max(1, int(self.env.get("ORBIT_HR_MIN_TASKS", "3")))
        except (TypeError, ValueError):
            self.hr_min_tasks = 3
        try:
            context_limit = int(self.env.get("ORBIT_CONTEXT_LIMIT", context_limit))
        except (TypeError, ValueError):
            pass
        try:
            compression_threshold = float(self.env.get("ORBIT_CONTEXT_THRESHOLD", compression_threshold))
        except (TypeError, ValueError):
            pass
        self.context = ContextManager(context_limit, compression_threshold)
        self.fallback_model = str(fallback_model or self.env.get("ORBIT_CONTEXT_FALLBACK_MODEL", "long-context-model"))
        self.broker = broker or PubSubBroker()
        self.agents: dict[str, AgentRecord] = {}
        self.logs: deque[dict[str, Any]] = deque(maxlen=20_000)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._monitor: threading.Thread | None = None
        self._instantiate()
        if autostart:
            self.start()

    def _instantiate(self) -> None:
        with self._lock:
            self.agents.clear()
            used_names: Counter[str] = Counter()
            for spec in configured_role_specs(self.mode, self.config):
                capacity = _role_capacity(spec, self.env)
                for index in range(1, spec.max_count + 1):
                    agent_id = f"mode{self.mode}-{index:02d}-{_slug(spec.role)}"
                    route = None
                    provider_available = None
                    provider_reason = "legacy_model_check"
                    if self.config is not None:
                        try:
                            from executors import provider_registry, resolve_route, route_public

                            role_key = role_route_key(spec.role)
                            route = resolve_route(
                                self.config,
                                _model_tier(spec.model),
                                role=f"mode-{self.mode}/{role_key}",
                                pool=spec.pool,
                                model_id=spec.model,
                                task_override={
                                    "provider_id": spec.provider_id,
                                    "model_id": spec.model,
                                    "executor": spec.executor,
                                } if spec.configured and spec.provider_id else None,
                                # A role/pool route explicitly chosen by the
                                # operator wins.  Built-in defaults are
                                # applied only when the resolver selected a
                                # neutral tier route.
                                executor=None,
                            )
                            if route and route.scope not in {"role", "pool", "task", "default"}:
                                route = replace(route, executor=spec.executor)
                            if route:
                                registry = provider_registry(self.config)
                                provider = registry.providers.get(route.provider_id)
                                if provider:
                                    provider_available, provider_reason = provider.availability(
                                        env=registry.env,
                                        simulation_override=self.simulation,
                                        model_id=route.model_id,
                                    )
                        except (ImportError, AttributeError, TypeError, ValueError):
                            route = None
                    available = bool(provider_available) if provider_available is not None else model_available(spec.model, simulation=self.simulation, env=self.env)
                    active = index <= capacity and available
                    metadata = {"executor": spec.executor}
                    model_name = spec.model
                    if route:
                        model_name = route.model_id
                        metadata = {
                            "provider_id": route.provider_id,
                            "executor": route.executor,
                            "route": route_public(route),
                            "route_version": route.route_version,
                        }
                    base_name = stable_agent_name(agent_id)
                    used_names[base_name] += 1
                    agent_name = base_name if used_names[base_name] == 1 else f"{base_name}-{used_names[base_name]:02d}"
                    record = AgentRecord(
                        id=agent_id,
                        role=spec.role,
                        role_index=index,
                        model=model_name,
                        duty=spec.duty,
                        pool=spec.pool,
                        agent_name=agent_name,
                        status="ready" if active else "inactive",
                        active=active,
                        simulated=self.simulation,
                        heartbeat_at=utc_now() if active else None,
                        metadata=metadata,
                    )
                    if not active:
                        record.last_error = f"资源上限或 Provider 不可用（{provider_reason}）"
                    self.agents[agent_id] = record
        self._emit("global", "cluster_started", f"{self.mode_label}已初始化", payload={"mode": self.mode})

    def start(self) -> None:
        with self._lock:
            if self._monitor and self._monitor.is_alive():
                return
            self._stop.clear()
            self._monitor = threading.Thread(target=self._monitor_loop, name="orbit-cluster-heartbeat", daemon=True)
            self._monitor.start()

    def stop(self) -> None:
        self._stop.set()
        monitor = self._monitor
        if monitor and monitor.is_alive():
            monitor.join(timeout=min(2.0, self.heartbeat_timeout))
        self._monitor = None

    def _monitor_loop(self) -> None:
        interval = min(5.0, max(0.5, self.heartbeat_timeout / 3))
        while not self._stop.wait(interval):
            # Ready slots are idle by design.  Refresh their liveness locally
            # so only a working call can be considered unresponsive.
            with self._lock:
                ready = [agent.id for agent in self.agents.values() if agent.active and agent.status == "ready"]
            for agent_id in ready:
                self.heartbeat(agent_id)
            self.check_heartbeats()

    def _resolve_agent_unlocked(self, agent_id: str | None) -> AgentRecord | None:
        """Resolve a catalog id or the task-facing ``lead`` alias.

        Task views promote the first available slot to ``lead`` when the
        catalog's first role is unavailable.  Looking up the first catalog
        entry would therefore mislabel runtime events in degraded mode; use
        the first active slot for that alias and retain a deterministic
        fallback when the whole pool is inactive.
        """
        if not agent_id:
            return None
        agent = self.agents.get(agent_id)
        if agent is None and agent_id == "lead":
            agent = next((candidate for candidate in self.agents.values() if candidate.active), None)
            if agent is None:
                agent = next(iter(self.agents.values()), None)
        return agent

    def _emit(
        self,
        topic: str,
        kind: str,
        message: str,
        *,
        agent_id: str | None = None,
        task_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        event_payload = dict(payload or {})
        if agent_id and "role" not in event_payload:
            with self._lock:
                owner = self._resolve_agent_unlocked(agent_id)
            if owner:
                event_payload["role"] = owner.role
                event_payload["source"] = owner.role
                event_payload["role_key"] = ROLE_KEYS.get(owner.role, _slug(owner.role))
        event = ClusterEvent(topic, kind, message, utc_now(), agent_id, task_id, event_payload).to_dict()
        with self._lock:
            self.logs.append(event)
        return self.broker.publish(topic, event)

    def heartbeat(self, agent_id: str) -> bool:
        with self._lock:
            agent = self._resolve_agent_unlocked(agent_id)
            if not agent or not agent.active:
                return False
            agent.heartbeat()
            if agent.status in {"inactive", "stalled"}:
                agent.status = "ready"
            return True

    def begin_work(self, agent_id: str) -> bool:
        """Mark a slot as executing so the heartbeat monitor can observe it."""
        with self._lock:
            agent = self._resolve_agent_unlocked(agent_id)
            if not agent or not agent.active:
                return False
            agent.status = "working"
            agent.heartbeat()
            return True

    def finish_work(self, agent_id: str, *, success: bool = True) -> bool:
        """Return a slot to the ready pool and record the task outcome."""
        disabled = False
        disabled_message = ""
        with self._lock:
            agent = self._resolve_agent_unlocked(agent_id)
            if not agent:
                return False
            if success:
                agent.completed_tasks += 1
            else:
                agent.failed_tasks += 1
            total = agent.completed_tasks + agent.failed_tasks
            error_rate = agent.failed_tasks / total if total else 0.0
            if total >= self.hr_min_tasks and error_rate >= self.hr_error_threshold:
                agent.active = False
                agent.status = "inactive"
                agent.last_error = f"HR 已停用：错误率 {error_rate:.0%} 超过阈值 {self.hr_error_threshold:.0%}"
                disabled = True
                disabled_message = f"{agent.role}-{agent.role_index:02d} 错误率超阈值，HR 已停用该槽位"
            else:
                agent.status = "ready" if agent.active else "inactive"
            agent.heartbeat()
        if disabled:
            self._emit("hr", "agent_disabled", disabled_message, agent_id=agent_id, payload={"error_rate": error_rate, "threshold": self.hr_error_threshold, "supervisor_role": self._supervisor_role()})
        return True

    def check_heartbeats(self, now: float | None = None) -> list[dict[str, Any]]:
        now = time.time() if now is None else float(now)
        stalled: list[dict[str, Any]] = []
        with self._lock:
            agents = list(self.agents.values())
        for agent in agents:
            if not agent.active or not agent.heartbeat_at or agent.status not in {"working", "busy", "stalled"}:
                continue
            try:
                stamp = datetime.fromisoformat(agent.heartbeat_at).timestamp()
            except (TypeError, ValueError, OverflowError):
                stamp = now
            if now - stamp <= self.heartbeat_timeout:
                continue
            with self._lock:
                agent.status = "stalled"
                agent.last_error = f"心跳超过 {int(self.heartbeat_timeout)} 秒未响应"
            supervisor = self._supervisor_role()
            alert = self._emit(
                "hr",
                "agent_stalled",
                f"{agent.role}-{agent.role_index:02d} 心跳超时，{supervisor}尝试重启",
                agent_id=agent.id,
                payload={"supervisor_role": supervisor},
            )
            # A heartbeat timeout is a failed attempt and must participate in
            # the same HR error-rate policy as an executor failure.  Once the
            # threshold disables a slot, do not immediately reactivate it.
            self.finish_work(agent.id, success=False)
            with self._lock:
                should_restart = agent.active
            restarted = self.restart_agent(agent.id) if should_restart else False
            stalled.append({"agent": agent.to_dict(), "alert": alert, "restarted": restarted})
        return stalled

    def _supervisor_role(self) -> str:
        if self.mode == 0:
            return "通用助理"
        if self.mode == 1:
            return "总管理（GM）"
        return "HR"

    def restart_agent(self, agent_id: str) -> bool:
        with self._lock:
            agent = self.agents.get(agent_id)
            available = bool(agent) and model_available(agent.model if agent else "", simulation=self.simulation, env=self.env)
            if agent and self.config is not None and agent.metadata.get("provider_id"):
                try:
                    from executors import provider_registry

                    registry = provider_registry(self.config)
                    provider = registry.providers.get(str(agent.metadata.get("provider_id")))
                    if provider:
                        available = provider.availability(
                            env=registry.env,
                            simulation_override=self.simulation,
                            model_id=agent.model,
                        )[0]
                except (ImportError, AttributeError, TypeError, ValueError):
                    pass
            if not agent or not available:
                return False
            agent.active = True
            agent.status = "ready"
            agent.restart_count += 1
            agent.last_error = None
            agent.heartbeat()
        self._emit("hr", "agent_restarted", f"{agent.role}-{agent.role_index:02d} 已重启", agent_id=agent.id)
        return True

    def publish(self, topic: str, message: Mapping[str, Any]) -> dict[str, Any]:
        return self.broker.publish(topic, dict(message))

    def status(self) -> dict[str, Any]:
        with self._lock:
            values = [agent.to_dict() for agent in self.agents.values()]
        by_role: dict[str, dict[str, int]] = {}
        for spec in configured_role_specs(self.mode, self.config):
            rows = [item for item in values if item["role"] == spec.role]
            by_role[spec.role] = {
                "max": spec.max_count,
                "active": sum(1 for item in rows if item["active"]),
                "online": sum(1 for item in rows if item["status"] in {"ready", "working", "busy"}),
                "inactive": sum(1 for item in rows if not item["active"]),
            }
        compact_roles: dict[str, dict[str, int]] = {}
        for human_role, data in by_role.items():
            key = ROLE_KEYS.get(human_role, _slug(human_role))
            target = compact_roles.setdefault(key, {"max": 0, "active": 0, "online": 0, "inactive": 0})
            for name, value in data.items():
                target[name] += value
        active = sum(1 for item in values if item["active"])
        return {
            "mode": self.mode,
            "runtime_mode": self.mode,
            "agent_mode": self.mode,
            "mode_label": self.mode_label,
            "simulation": self.simulation,
            "expected_slots": len(values),
            "max_slots": len(values),
            "active_slots": active,
            "online_slots": sum(1 for item in values if item["status"] in {"ready", "working", "busy"}),
            "inactive_slots": len(values) - active,
            "agent_slots": values,
            "agents": values,
            "role_counts": {role: data["max"] for role, data in compact_roles.items()},
            "active_counts": {role: data["active"] for role, data in compact_roles.items()},
            "online_counts": {role: data["online"] for role, data in compact_roles.items()},
            "roles": compact_roles,
            "role_status": by_role,
            "health": "degraded" if not values or active < len(values) else "healthy",
            "heartbeat_timeout_seconds": self.heartbeat_timeout,
            "hr_error_threshold": self.hr_error_threshold,
            "hr_min_tasks": self.hr_min_tasks,
            "topics": list(self.TOPICS),
        }

    def task_agents(self, task_id: str | None = None) -> list[dict[str, Any]]:
        """Return a stable task-facing view, retaining inactive slots."""
        output = []
        for record in self.agents.values():
            row = record.to_dict()
            row.update({"task_id": task_id, "name": record.agent_name or stable_agent_name(record.id), "objective": record.duty})
            output.append(row)
        return output

    def logs_search(
        self,
        keyword: str | None = None,
        role: str | None = None,
        from_time: str | None = None,
        to_time: str | None = None,
        task_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        keyword = str(keyword or "").strip().lower()
        role = str(role or "").strip().lower()
        with self._lock:
            values = list(self.logs)
        result = []
        for item in reversed(values):
            if task_id and str(item.get("task_id") or "") != task_id:
                continue
            haystack = " ".join(str(item.get(key) or "") for key in ("message", "kind", "type", "topic", "agent_id", "role", "role_key", "source")).lower()
            if keyword and keyword not in haystack:
                continue
            if role and role not in haystack:
                continue
            stamp = str(item.get("timestamp") or "")
            if from_time and stamp < str(from_time):
                continue
            if to_time and stamp > str(to_time):
                continue
            result.append(item)
            if len(result) >= max(1, int(limit)):
                break
        return result

    def compress_context(self, transcript: list[Any], *, task_id: str | None = None, keep: int = 8) -> dict[str, Any]:
        usage = self.context.usage(transcript)
        if not usage["needs_compression"]:
            return {"compressed": False, "messages": transcript, "usage": usage}
        messages, summary, details = self.context.compress(transcript, keep=keep)
        self._emit("status", "context_compressed", "上下文已压缩并继续任务", task_id=task_id, payload={"summary": summary, **details})
        switched = details["ratio_after"] >= self.context.compression_threshold
        if switched:
            self._emit(
                "status",
                "model_switched",
                "压缩后上下文仍接近上限，已切换长上下文模型",
                task_id=task_id,
                payload={"replacement_model": self.fallback_model, "reason": "context_limit"},
            )
        return {
            "compressed": True,
            "messages": messages,
            "summary": summary,
            "model_switched": switched,
            "replacement_model": self.fallback_model if switched else None,
            "usage": {**usage, **details},
        }

    def resolve_dispute(
        self,
        question: str,
        options: Mapping[str, Iterable[str]] | None = None,
        *,
        task_id: str | None = None,
        seed: int | None = None,
    ) -> dict[str, Any]:
        """Run the mode-appropriate deterministic dispute procedure."""
        options = options or {"方案A": (), "方案B": ()}
        names = list(options) or ["方案A", "方案B"]
        rng = random.Random(seed)
        with self._lock:
            active = [agent for agent in self.agents.values() if agent.active]
        if self.mode == 0:
            result = {"procedure": "single", "winner": names[0], "votes": {names[0]: 1}, "rounds": []}
        elif self.mode == 1:
            result = {"procedure": "gm_decision", "winner": names[0], "votes": {names[0]: 1}, "rounds": []}
        elif self.mode == 2:
            voters = [agent for agent in active if agent.role in {"系统架构师", "前端TL", "后端TL", "数据TL"}]
            weights = {"系统架构师": 3, "前端TL": 1, "后端TL": 1, "数据TL": 1}
            votes = Counter()
            ballots = []
            for voter in voters:
                choice = names[(voter.role_index + len(question)) % len(names)]
                votes[choice] += weights.get(voter.role, 1)
                ballots.append({"agent_id": voter.id, "role": voter.role, "choice": choice, "weight": weights.get(voter.role, 1)})
            winner = max(names, key=lambda name: (votes[name], -names.index(name))) if votes else names[0]
            result = {"procedure": "weighted_tl_vote", "winner": winner, "votes": dict(votes), "ballots": ballots, "rounds": []}
        else:
            reserve = [agent for agent in active if "辩论储备" in agent.role]
            rng.shuffle(reserve)
            affirmative = reserve[:3]
            negative = reserve[3:6]
            rounds = []
            for round_number in range(1, 4):
                rounds.append({
                    "round": round_number,
                    "affirmative": [{"agent_id": a.id, "role": a.role, "statement": f"支持{names[0]}：{question}"} for a in affirmative],
                    "negative": [{"agent_id": a.id, "role": a.role, "statement": f"支持{names[-1]}：需审查{question}"} for a in negative],
                })
            votes = Counter()
            ballots = []
            for voter in active:
                if voter.role == "辩论主持人":
                    continue
                choice = names[(voter.role_index + voter.completed_tasks + len(voter.role)) % len(names)]
                votes[choice] += 1
                ballots.append({"agent_id": voter.id, "role": voter.role, "choice": choice})
            max_votes = max(votes.values(), default=0)
            leaders = [name for name in names if votes[name] == max_votes]
            hr_scores: dict[str, float] = {}
            for name in leaders:
                references = {str(value).lower() for value in (options.get(name, ()) or ())}
                related = [
                    agent
                    for agent in active
                    if not references
                    or agent.id.lower() in references
                    or agent.role.lower() in references
                ]
                if not related:
                    related = active
                completed = sum(agent.completed_tasks for agent in related)
                attempted = completed + sum(agent.failed_tasks for agent in related)
                hr_scores[name] = completed / attempted if attempted else 0.0
            winner = leaders[0] if len(leaders) == 1 else max(leaders, key=lambda name: (hr_scores.get(name, 0.0), -names.index(name)))
            result = {
                "procedure": "debate_and_referendum",
                "winner": winner,
                "votes": dict(votes),
                "ballots": ballots,
                "rounds": rounds,
                "tie_broken_by": "HR" if len(leaders) > 1 else None,
                "hr_scores": hr_scores,
            }
        self._emit("debate" if self.mode == 3 else "architecture", "dispute_resolved", f"争议已裁决：{result['winner']}", task_id=task_id, payload={"question": question, **result})
        return result

    def emit_status(self, kind: str, message: str, *, task_id: str | None = None, agent_id: str | None = None, **payload: Any) -> dict[str, Any]:
        return self._emit("status", kind, message, task_id=task_id, agent_id=agent_id, payload=payload)


def _slug(value: str) -> str:
    chars = "".join(character if character.isalnum() else "-" for character in str(value).lower())
    return chars.strip("-")[:24] or "agent"


_RUNTIME: ClusterRuntime | None = None
_RUNTIME_LOCK = threading.Lock()


def get_cluster_runtime(*, refresh: bool = False, mode: Any = None, config: Any | None = None) -> ClusterRuntime:
    """Return the process runtime used by both HTTP servers."""

    global _RUNTIME
    with _RUNTIME_LOCK:
        selected = configured_mode() if mode is None else parse_mode(mode)
        version = int(getattr(getattr(config, "provider_registry", None), "version", 0) or 0)
        if refresh or _RUNTIME is None or _RUNTIME.mode != selected or (config is not None and _RUNTIME.config_version != version):
            if _RUNTIME is not None:
                _RUNTIME.stop()
            _RUNTIME = ClusterRuntime(selected, config=config, simulation=getattr(config, "simulation", None), autostart=True)
        return _RUNTIME


def cluster_system_payload(runtime: ClusterRuntime | None = None) -> dict[str, Any]:
    runtime = runtime or get_cluster_runtime()
    return runtime.status()


__all__ = [
    "AgentRecord",
    "AGENT_NAME_POOL",
    "ClusterEvent",
    "ClusterRuntime",
    "ContextManager",
    "MODE_LABELS",
    "PubSubBroker",
    "ROLE_CATALOG",
    "ROLE_ROUTE_KEYS",
    "RoleSpec",
    "SUPPORTED_EXECUTORS",
    "cluster_system_payload",
    "configured_mode",
    "configured_role_specs",
    "expected_slot_count",
    "get_cluster_runtime",
    "model_available",
    "parse_mode",
    "role_max_counts",
    "role_catalog",
    "role_route_key",
    "role_specs",
    "stable_agent_name",
]
