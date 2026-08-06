/* Orbit Swarm workbench. The UI accepts one task stream and adapts to both
 * the current REST contract and newer cluster telemetry fields when present. */
(() => {
  "use strict";

  const MODE_DEFINITIONS = {
    0: {
      name: "模式 0 · 单 Agent", description: "由一个通用助理直接回答，不启用内部协作。", maxSlots: 1,
      roles: [{ key: "general", label: "通用助理", max: 1, model: "Claude Opus 5", color: "blue" }],
    },
    1: {
      name: "模式 1 · 中档集群", description: "总管理拆解任务，开发、数据、测试与运维接力完成。", maxSlots: 5,
      roles: [
        { key: "gm", label: "总管理", max: 1, model: "Claude Opus 5", color: "violet" },
        { key: "fullstack", label: "全栈开发", max: 1, model: "GPT-5.6 Terra", color: "blue" },
        { key: "backend", label: "后端 / 数据库", max: 1, model: "GPT-5.6 Terra", color: "teal" },
        { key: "testing", label: "测试工程师", max: 1, model: "DeepSeek V4 Flash", color: "green" },
        { key: "ops", label: "文档 / 运维", max: 1, model: "DeepSeek V4 Flash", color: "amber" },
      ],
    },
    2: {
      name: "模式 2 · 高档集群", description: "架构师统筹三条技术线，支持弹性岗位、审计、测试与交付。", maxSlots: 20,
      roles: [
        { key: "architect", label: "系统架构师", max: 1, model: "Claude Opus 5", color: "violet" },
        { key: "frontend_tl", label: "前端 TL", max: 1, model: "GPT-5.6 Sol", color: "blue" },
        { key: "backend_tl", label: "后端 TL", max: 1, model: "GPT-5.6 Sol", color: "teal" },
        { key: "data_tl", label: "数据 TL", max: 1, model: "GPT-5.6 Sol", color: "teal" },
        { key: "frontend", label: "前端开发组", max: 3, model: "GPT-5.6 Terra", color: "blue" },
        { key: "backend", label: "后端开发组", max: 3, model: "GPT-5.6 Terra", color: "teal" },
        { key: "data", label: "数据库 / 缓存组", max: 2, model: "GPT-5.6 Terra", color: "teal" },
        { key: "testing", label: "测试开发组", max: 3, model: "DeepSeek V4 Flash", color: "green" },
        { key: "security", label: "安全审计员", max: 1, model: "GPT-5.6 Sol", color: "red" },
        { key: "docs", label: "文档编写组", max: 2, model: "DeepSeek V4 Flash", color: "amber" },
        { key: "ops", label: "运维实施组", max: 1, model: "DeepSeek V4 Flash", color: "amber" },
        { key: "hr", label: "人力资源 HR", max: 1, model: "GPT-5.6 Luna", color: "violet" },
      ],
    },
    3: {
      name: "模式 3 · 极限集群", description: "超级网关、辩论主持、专业池与储备辩手按需扩缩。", maxSlots: 100,
      roles: [
        { key: "gateway", label: "超级网关", max: 1, model: "GPT-5.6 Luna", color: "violet" },
        { key: "debate_host", label: "辩论主持人", max: 1, model: "Claude Opus 5", color: "violet" },
        { key: "hr", label: "HR", max: 1, model: "GPT-5.6 Sol", color: "red" },
        { key: "observer", label: "观察员", max: 2, model: "DeepSeek V4 Flash", color: "amber" },
        { key: "coding_lead", label: "编码池长", max: 1, model: "Claude Opus 5", color: "blue" },
        { key: "coding", label: "编码执行组", max: 20, model: "GPT-5.6 Terra", color: "blue" },
        { key: "testing_lead", label: "测试池长", max: 1, model: "GPT-5.6 Sol", color: "green" },
        { key: "testing", label: "测试执行组", max: 15, model: "DeepSeek V4 Flash", color: "green" },
        { key: "security_lead", label: "安全池长", max: 1, model: "GPT-5.6 Sol", color: "red" },
        { key: "security", label: "安全执行组", max: 10, model: "GPT-5.6 Sol", color: "red" },
        { key: "docs_lead", label: "文档池长", max: 1, model: "Claude Opus 5", color: "amber" },
        { key: "docs", label: "文档执行组", max: 10, model: "DeepSeek V4 Flash", color: "amber" },
        { key: "performance_lead", label: "性能池长", max: 1, model: "GPT-5.6 Terra", color: "teal" },
        { key: "performance", label: "性能执行组", max: 5, model: "GPT-5.6 Terra", color: "teal" },
        { key: "debaters", label: "辩论储备组", max: 30, model: "GPT-5.6 Terra", color: "violet" },
      ],
    },
  };

  const MODE_NAMES = Object.fromEntries(Object.entries(MODE_DEFINITIONS).map(([key, value]) => [key, value.name]));
  const TERMINAL = new Set(["complete", "completed", "cancelled", "canceled", "failed", "interrupted"]);
  // The task payload deliberately retains the full agent transcript for
  // recovery and log search.  The chat timeline is a separate, user-facing
  // view: it shows deliverables, leadership decisions and operational events,
  // rather than every dispatch and worker heartbeat.
  const LEADERSHIP_ROLE_KEYS = new Set([
    "general", "gm", "architect", "frontend_tl", "backend_tl", "data_tl",
    "gateway", "debate_host", "hr", "coding_lead", "testing_lead",
    "security_lead", "docs_lead", "performance_lead",
  ]);
  const KEY_STATUS_EVENTS = new Set([
    "task_started", "started", "cluster_started", "planning", "fan_out", "synthesis",
    "complete", "completed", "task_completed", "blocked", "cancelled", "failed",
    "interrupted", "model_switch", "model_switched", "context_compressed",
    "agent_failed", "agent_offline", "agent_disabled", "agent_restarted", "dispute_resolved",
  ]);
  const LEADERSHIP_ONLY_EVENTS = new Set(["planning", "fan_out", "synthesis", "complete", "completed", "task_completed", "dispute_resolved"]);
  const WORKER_PROGRESS_EVENTS = new Set(["dispatch", "agent_started", "agent_progress", "working", "heartbeat", "agent_complete", "ack", "acknowledged"]);
  const $ = (selector) => document.querySelector(selector);
  const safe = (value) => String(value == null ? "" : value).replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" })[char]);
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const nowTime = () => new Date().toLocaleTimeString("zh-CN", { hour12: false });

  const state = {
    mode: Number(localStorage.getItem("orbit-mode") || 2), system: null, tasks: [], active: null,
    serviceOnline: false, socket: null, poller: null, refreshing: false, submitting: false,
    autoAdvanceBusy: false, autoClarified: new Set(), seenEvents: new Set(), noticeKeys: new Set(), composerIntent: "task",
    notices: [], stream: [], startedAt: 0, logRequest: 0, providers: [], routes: null, roleCatalog: [], agentProfiles: {},
  };
  if (!MODE_DEFINITIONS[state.mode]) state.mode = 2;

  async function api(path, options = {}) {
    const headers = { Accept: "application/json", ...(options.headers || {}) };
    const request = { ...options, headers };
    if (request.body && typeof request.body !== "string") {
      request.body = JSON.stringify(request.body);
      headers["Content-Type"] = "application/json";
    }
    const response = await fetch(path, request);
    const text = await response.text();
    let payload = {};
    try { payload = text ? JSON.parse(text) : {}; } catch (_) { payload = text; }
    if (!response.ok) {
      const detail = payload && typeof payload === "object" ? payload.detail || payload.message : payload;
      throw new Error(String(detail || `${response.status} ${response.statusText}`));
    }
    return payload;
  }

  function modeFromPayload(payload) {
    const value = payload && (payload.mode ?? payload.runtime_mode ?? payload.agent_mode ?? payload.cluster_mode);
    if (value == null) return state.mode;
    const match = String(value).match(/[0-3]/);
    return match && MODE_DEFINITIONS[Number(match[0])] ? Number(match[0]) : state.mode;
  }

  function setConnection(online, message) {
    state.serviceOnline = Boolean(online);
    const pill = $("#connectionPill");
    const dot = $("#railLiveDot");
    const text = message || (online ? "服务在线" : "服务离线");
    if (pill) { pill.className = `connection-pill ${online ? "online" : "offline"}`; pill.querySelector("span").textContent = online ? "本地服务在线" : "本地服务离线"; }
    if (dot) dot.className = `live-dot ${online ? "online" : "offline"}`;
    if ($("#railConnectionText")) $("#railConnectionText").textContent = text;
    const health = $("#healthBadge");
    if (health) { health.className = `health-badge ${online ? "online" : "offline"}`; health.textContent = online ? ((state.system?.simulation_mode || state.system?.simulation) ? "模拟模式" : "运行中") : "离线"; }
  }

  function pushNotice(message, kind = "info", key = message) {
    const normalized = String(message || "").trim();
    if (!normalized || state.noticeKeys.has(String(key))) return;
    state.noticeKeys.add(String(key));
    state.notices.unshift({ id: `${Date.now()}-${Math.random()}`, message: normalized, kind });
    state.notices = state.notices.slice(0, 5);
    renderNotices();
  }

  function renderNotices() {
    const node = $("#alertStack");
    if (!node) return;
    node.innerHTML = state.notices.map((notice) => `<div class="alert ${safe(notice.kind)}" data-notice="${safe(notice.id)}"><span>${safe(notice.message)}</span><button type="button" title="关闭" aria-label="关闭">×</button></div>`).join("");
    node.querySelectorAll("[data-notice]").forEach((item) => item.querySelector("button").addEventListener("click", () => {
      state.notices = state.notices.filter((notice) => notice.id !== item.dataset.notice);
      renderNotices();
    }));
  }

  function statusLabel(status) {
    return ({ queued: "排队中", planning: "规划中", running: "执行中", chatting: "协调中", awaiting_confirmation: "准备执行", paused: "已暂停", complete: "已完成", completed: "已完成", cancelled: "已取消", canceled: "已取消", failed: "失败", interrupted: "已中断" })[String(status || "").toLowerCase()] || (status ? String(status) : "空闲");
  }

  function isOnlineAgent(agent) {
    const status = String(agent?.status || agent?.state || "").toLowerCase();
    return !["offline", "disabled", "inactive", "failed", "error", "stopped", "unavailable", "complete", "completed", "cancelled", "canceled"].includes(status);
  }

  function roleText(agent) {
    return String(agent?.role || agent?.job || agent?.position || agent?.pool || agent?.name || "").trim();
  }

  function roleMatch(agent, definition) {
    if (!agent) return false;
    const text = `${roleText(agent)} ${agent.id || ""}`.toLowerCase();
    const aliases = {
      gm: ["gm", "总管理", "coordinator", "swarm coordinator", "orion", "lead"], architect: ["architect", "架构"], frontend_tl: ["frontend tl", "前端tl", "前端 tl", "前端组管理"], backend_tl: ["backend tl", "后端tl", "后端 tl", "后端组管理"], data_tl: ["data tl", "数据tl", "数据 tl", "数据组管理"], frontend: ["frontend", "前端开发"], backend: ["backend", "后端开发"], data: ["database", "数据库", "缓存", "data"], testing: ["test", "测试"], security: ["security", "安全"], docs: ["doc", "文档"], ops: ["ops", "运维"], hr: ["hr", "人力"], gateway: ["gateway", "网关"], debate_host: ["debate host", "辩论主持"], observer: ["observer", "观察员"], coding: ["coding", "编码"], coding_lead: ["编码池长", "coding lead"], testing_lead: ["测试池长", "testing lead"], security_lead: ["安全池长", "security lead"], docs_lead: ["文档池长", "docs lead"], performance: ["performance", "性能"], performance_lead: ["性能池长", "performance lead"], debaters: ["debater", "辩手", "储备"], general: ["general", "通用", "assistant"], fullstack: ["fullstack", "全栈"],
    };
    const values = [...(aliases[definition.key] || [definition.key]), String(definition.label || "").toLowerCase()];
    return values.some((alias) => text.includes(alias));
  }

  function taskAgents(task) {
    const values = task?.agents || task?.agent_status || task?.active_agents || [];
    if (Array.isArray(values)) return values;
    if (values && typeof values === "object") return Object.values(values).flatMap((value) => Array.isArray(value) ? value : [value]);
    return [];
  }

  function telemetryAgents() {
    const values = state.system?.agents || state.system?.agent_slots || state.system?.agent_status || state.system?.active_agents || state.system?.cluster?.agents || state.system?.cluster?.agent_slots || [];
    if (Array.isArray(values)) return values;
    if (values && typeof values === "object") return Object.values(values).flatMap((value) => Array.isArray(value) ? value : [value]);
    return [];
  }

  function agentInventory() {
    const active = taskAgents(state.active);
    const system = telemetryAgents();
    const taskRunning = state.active && !TERMINAL.has(String(state.active.status || "").toLowerCase());
    return taskRunning && active.length ? active : system;
  }

  function profileForMode(mode) {
    const profiles = state.agentProfiles;
    if (!profiles || typeof profiles !== "object") return { present: false, entries: null };
    const key = String(mode);
    if (Object.prototype.hasOwnProperty.call(profiles, key)) return { present: true, entries: profiles[key] };
    if (Object.prototype.hasOwnProperty.call(profiles, Number(mode))) return { present: true, entries: profiles[Number(mode)] };
    return { present: false, entries: null };
  }

  function recommendedExecutor(label, model) {
    const name = String(label || "").replace(/\s+/g, "");
    if (["全栈开发", "前端TL", "后端TL", "前端开发组", "后端开发组", "编码执行组"].includes(name)) return "codex";
    if (["文档/运维", "文档编写组", "运维实施组", "观察员", "文档池长", "文档执行组"].includes(name)) return "openclaw";
    return "direct_model";
  }

  function defaultRoleEntries(mode) {
    const fallback = (MODE_DEFINITIONS[mode] || MODE_DEFINITIONS[0]).roles;
    const catalog = state.roleCatalog.filter((item) => Number(item.default_mode) === Number(mode));
    if (!catalog.length) return fallback.map((role) => ({ ...role, executor: role.executor || recommendedExecutor(role.label, role.model) }));
    return catalog.map((item) => {
      const base = fallback.find((role) => role.key === item.group_key || role.key === item.role_key || role.label === item.role) || {};
      return {
        key: item.role_key || base.key || `role-${item.role}`,
        label: item.role || base.label,
        max: Number(item.max_count || base.max || 1),
        model: item.model || base.model || "Unknown model",
        executor: item.executor || recommendedExecutor(item.role, item.model),
        providerId: item.provider_id || item.recommended_provider_id || "",
        color: base.color || "blue",
      };
    });
  }

  function configuredRoles(mode) {
    const fallback = defaultRoleEntries(mode);
    const profile = profileForMode(mode);
    if (!profile.present || !Array.isArray(profile.entries)) return fallback;
    const entries = profile.entries;
    return entries.map((entry, index) => {
      const catalog = state.roleCatalog.find((item) => String(item.role) === String(entry.role || entry.name));
      const label = entry.role || entry.name || catalog?.role || `Role ${index + 1}`;
      const fallbackRole = fallback.find((item) => item.label === label || item.key === entry.role_key);
      return {
        key: entry.role_key || catalog?.role_key || fallbackRole?.key || `role-${index}`,
        label,
        max: Math.max(1, Number(entry.max_count ?? entry.slots ?? catalog?.max_count ?? fallbackRole?.max ?? 1) || 1),
        model: entry.model || entry.model_id || catalog?.model || fallbackRole?.model || "Unknown model",
        executor: entry.executor || catalog?.executor || fallbackRole?.executor || recommendedExecutor(label, entry.model || catalog?.model),
        providerId: entry.provider_id || entry.provider || catalog?.provider_id || catalog?.recommended_provider_id || fallbackRole?.providerId || "",
        agentName: entry.agent_name || entry.display_name || entry.nickname || "",
        color: fallbackRole?.color || "blue",
      };
    });
  }

  function executorLabel(value) {
    return ({ direct_model: "direct model", openclaw: "OpenClaw", codex: "Codex", claude_code: "Claude Code" })[String(value || "direct_model").toLowerCase()] || String(value || "direct model");
  }

  function roleCounts() {
    const agents = agentInventory();
    return configuredRoles(state.mode).map((role) => {
      const matches = agents.filter((agent) => roleMatch(agent, role));
      let online = matches.filter(isOnlineAgent).length;
      const externalRole = state.system?.roles?.[role.key] || state.system?.roles?.[role.label] || state.system?.cluster?.roles?.[role.key] || state.system?.cluster?.roles?.[role.label];
      const external = state.system?.online_counts?.[role.key] ?? state.system?.online_counts?.[role.label] ?? state.system?.active_counts?.[role.key] ?? state.system?.active_counts?.[role.label] ?? externalRole?.online;
      if (!matches.length && external != null && Number.isFinite(Number(external))) online = Math.max(0, Number(external));
      return { ...role, online: Math.min(role.max, online), matched: matches };
    });
  }

  function renderMode() {
    const definition = MODE_DEFINITIONS[state.mode];
    const serverLabel = state.system?.mode_label || state.system?.cluster?.mode_label;
    const serverSlots = Number(state.system?.expected_slots ?? state.system?.cluster?.expected_slots ?? state.system?.agent_slots?.length ?? definition.maxSlots);
    const select = $("#modeSelect");
    if (select) select.value = String(state.mode);
    $("#modeDescription").textContent = definition.description;
    $("#fleetModeName").textContent = serverLabel || definition.name;
    $("#fleetModeCount").textContent = `0 / ${serverSlots}`;
    $("#appTitle").textContent = state.active ? (state.active.title || "任务执行中") : "准备接收任务";
  }

  function renderRoster() {
    const definition = MODE_DEFINITIONS[state.mode];
    const counts = roleCounts();
    const inventory = agentInventory();
    const inventoryOnline = inventory.filter(isOnlineAgent).length;
    const online = Math.max(counts.reduce((total, item) => total + item.online, 0), inventoryOnline);
    const max = Number(state.system?.expected_slots ?? state.system?.cluster?.expected_slots ?? state.system?.agent_slots?.length ?? definition.maxSlots);
    const percentage = max ? Math.min(100, Math.round((online / max) * 100)) : 0;
    $("#railOnlineCount").textContent = String(online);
    $("#railSlotText").textContent = `${online} / ${max} 槽位`;
    $("#railCapacityBar").style.width = `${percentage}%`;
    $("#fleetTrackBar").style.width = `${percentage}%`;
    $("#fleetModeCount").textContent = `${online} / ${max}`;
    $("#fleetOnlineText").textContent = `${online} 个岗位在线`;
    $("#fleetUpdatedAt").textContent = nowTime();
    $("#rolePoolSummary").textContent = `${counts.length} 个岗位池`;
    const hasGap = counts.some((item) => item.online < item.max);
    $("#railHealthText").textContent = state.serviceOnline ? (hasGap ? "弹性运行" : "岗位齐备") : "等待连接";
    $("#roleBreakdown").innerHTML = counts.map((item) => {
      const countClass = item.online === 0 && item.max > 0 ? "offline" : item.online < item.max ? "gap" : "";
      const names = item.matched.map((agent) => agent?.agent_name || agent?.display_name || agent?.name).filter(Boolean);
      const agentName = names.length > 2 ? `${names.slice(0, 2).join(", ")} +${names.length - 2}` : names.join(", ") || item.agentName;
      const executor = executorLabel(item.matched[0]?.executor || item.executor);
      const provider = item.matched[0]?.provider_id || item.matched[0]?.provider || item.providerId || "自动路由";
      const model = item.matched[0]?.model_name || item.matched[0]?.model || item.model;
      const title = names.length ? `title="${safe(names.join(", "))}"` : "";
      return `<div class="role-row role-${safe(item.color)}" ${title}><i class="role-dot"></i><div class="role-label"><strong>${safe(item.label)}${agentName ? ` · ${safe(agentName)}` : ""}</strong><small>${safe(provider)} · ${safe(model)} · ${safe(executor)}</small></div><span class="role-count ${countClass}">${item.online} / ${item.max}</span></div>`;
    }).join("");
    const inactive = Number(state.system?.inactive_slots ?? state.system?.cluster?.inactive_slots ?? counts.reduce((total, item) => total + Math.max(0, item.max - item.online), 0));
    const health = String(state.system?.health || state.system?.cluster?.health || "").toLowerCase();
    if (state.serviceOnline && (health === "degraded" || inactive > 0)) {
      pushNotice(`集群弹性运行：${inactive} 个槽位未激活，其余岗位继续工作。`, "warning", `capacity-${state.mode}-${inactive}`);
    }
  }

  function taskStatusClass(status) {
    const normalized = String(status || "").toLowerCase();
    if (["running", "planning", "queued", "chatting", "awaiting_confirmation"].includes(normalized)) return "working";
    if (["complete", "completed"].includes(normalized)) return "complete";
    if (["failed", "interrupted"].includes(normalized)) return "failed";
    if (normalized === "paused") return "paused";
    return "";
  }

  function taskDate(task) {
    const raw = task?.updated_at || task?.updated_at_iso || task?.created_at_iso || task?.created_at;
    if (!raw) return "刚刚";
    const date = new Date(raw);
    return Number.isNaN(date.getTime()) ? String(raw).slice(0, 8) : date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
  }

  function renderTaskList() {
    const node = $("#taskList");
    const tasks = state.tasks.slice(0, 35);
    node.innerHTML = tasks.length ? tasks.map((task) => `<button type="button" class="task-item ${state.active?.id === task.id ? "active" : ""}" data-task-id="${safe(task.id)}"><span class="task-item-title">${safe(task.title || task.prompt || "未命名任务")}</span><span class="task-item-meta"><span>${safe(statusLabel(task.status))}</span><span>${safe(taskDate(task))}</span></span></button>`).join("") : `<p class="muted">还没有任务记录。</p>`;
    node.querySelectorAll("[data-task-id]").forEach((button) => button.addEventListener("click", () => selectTask(button.dataset.taskId)));
  }

  function sourceFor(agent, id, role) {
    const name = agent?.agent_name || agent?.display_name || agent?.name || agent?.label;
    const roleName = agent?.role || agent?.job || role;
    if (id === "lead" || role === "lead") {
      const leadRole = state.mode === 0 ? "通用助理" : (roleName && roleName !== "lead" ? roleName : MODE_DEFINITIONS[state.mode].roles[0].label);
      return name && name !== leadRole ? `${leadRole} · ${name}` : leadRole;
    }
    if (name && roleName && name !== roleName) return `${roleName} · ${name}`;
    return roleName || name || "Agent";
  }

  function agentFor(task, id) {
    return taskAgents(task).find((agent) => String(agent.id || agent.agent_id) === String(id)) || null;
  }

  function eventTime(item) {
    const raw = item?.timestamp || item?.created_at || item?.time;
    if (!raw) return "";
    const date = new Date(raw);
    return Number.isNaN(date.getTime()) ? String(raw).slice(0, 8) : date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  function colorClass(agent, source) {
    const text = `${roleText(agent)} ${source || ""}`.toLowerCase();
    if (text.includes("测试") || text.includes("test") || text.includes("文档") || text.includes("docs")) return "agent-green";
    if (text.includes("数据") || text.includes("后端") || text.includes("backend") || text.includes("安全") || text.includes("security")) return "agent-teal";
    if (text.includes("架构") || text.includes("总管理") || text.includes("hr") || text.includes("辩论")) return "agent-violet";
    return "agent-blue";
  }

  function initials(source) {
    const text = String(source || "Agent").replace(/[【】\[\]·\s]/g, "");
    return Array.from(text).slice(0, 2).join("").toUpperCase();
  }

  function normalizedRoleKey(agent, item = {}) {
    const explicit = item.role_key || item.roleKey || agent?.role_key || agent?.roleKey;
    if (explicit) return String(explicit).trim().toLowerCase();
    const text = `${agent?.role || item.role || item.source || ""}`.toLowerCase();
    if (/\b(gm|architect|gateway|hr)\b/.test(text)) return text.match(/\b(gm|architect|gateway|hr)\b/)?.[1] || "";
    if (/\b(frontend|backend|data)[ _-]?tl\b/.test(text)) return `${text.match(/\b(frontend|backend|data)[ _-]?tl\b/)?.[1]}_tl`;
    if (/\b(coding|testing|security|docs|performance)[ _-]?lead\b/.test(text)) return `${text.match(/\b(coding|testing|security|docs|performance)[ _-]?lead\b/)?.[1]}_lead`;
    if (/\blead\b|coordinator|manager/.test(text)) return "lead";
    return "";
  }

  function isLeadershipAgent(agent, item = {}) {
    if (String(item.agent_id || item.agentId || agent?.id || "") === "lead") return true;
    const key = normalizedRoleKey(agent, item);
    return LEADERSHIP_ROLE_KEYS.has(key) || key === "lead";
  }

  function isPublicStatusEvent(event, agent) {
    const type = String(event?.type || "event").toLowerCase();
    if (WORKER_PROGRESS_EVENTS.has(type)) return false;
    if (!KEY_STATUS_EVENTS.has(type)) return false;
    return !LEADERSHIP_ONLY_EVENTS.has(type) || isLeadershipAgent(agent, event);
  }

  function isPublicConversation(item, origin, agent = null) {
    const role = String(item?.role || item?.speaker || "assistant").toLowerCase();
    if (origin === "conversation") {
      if (role === "user") return true;
      if (!["assistant", "lead"].includes(role)) return false;
      if (item?.public === true || item?.user_visible === true) return true;
      if (!item?.agent_id && !item?.agentId) return true;
      return isLeadershipAgent(agent, item);
    }
    // Agent-to-agent transcripts are retained by the backend but are not
    // chat messages. Leadership milestones arrive as typed events instead.
    return false;
  }

  function collectFeed(task) {
    if (!task) return [];
    const rows = [];
    const conversation = Array.isArray(task.conversation) ? task.conversation : (Array.isArray(task.messages) ? task.messages : []);
    conversation.forEach((item, index) => {
      const role = String(item.role || item.speaker || "assistant").toLowerCase();
      const agent = agentFor(task, item.agent_id || item.agentId);
      if (!isPublicConversation(item, "conversation", agent)) return;
      const source = role === "user" ? "你" : role === "system" ? "系统" : sourceFor(agent, item.agent_id || item.agentId, role === "lead" ? "lead" : role);
      rows.push({ key: item.id || `conversation-${index}`, timestamp: item.timestamp || item.time || index, source, content: item.content || item.message || "", kind: role === "user" ? "user" : role === "system" ? "system" : "agent", agent, time: eventTime(item) });
    });
    const agentConversations = task.agent_conversations || task.agentConversations || {};
    Object.entries(agentConversations || {}).forEach(([agentId, values]) => {
      if (!Array.isArray(values)) return;
      values.forEach((item, index) => {
        const content = item.content || item.message || "";
        if (!content || values.length === conversation.length) return;
        const role = String(item.role || item.speaker || "assistant").toLowerCase();
        if (role === "user" && conversation.some((message) => message.content === content)) return;
        const agent = agentFor(task, agentId);
        if (!isPublicConversation(item, "agent_conversation", agent)) return;
        rows.push({ key: `agent-${agentId}-${item.id || index}`, timestamp: item.timestamp || item.time || index, source: sourceFor(agent, agentId, agentId === "lead" ? "lead" : role), content, kind: role === "system" ? "system" : "agent", agent, time: eventTime(item) });
      });
    });
    (Array.isArray(task.events) ? task.events : []).forEach((event, index) => {
      const content = event.message || event.content || event.detail || "";
      if (!content) return;
      const type = String(event.type || "event").toLowerCase();
      const agent = agentFor(task, event.agent_id || event.agentId);
      const source = event.agent_id ? sourceFor(agent, event.agent_id, event.agent_id === "lead" ? "lead" : "agent") : "系统状态";
      if (["user_message", "assistant_message"].includes(type) && rows.some((row) => row.content === content)) return;
      if (!isPublicStatusEvent(event, agent)) return;
      rows.push({ key: event.id || `event-${index}`, timestamp: event.timestamp || event.time || index, source, content, kind: event.agent_id ? "agent" : "system", agent, time: eventTime(event), eventType: type });
    });
    const unique = [];
    const seen = new Set();
    rows.sort((a, b) => String(a.timestamp).localeCompare(String(b.timestamp)));
    rows.forEach((row) => {
      const key = `${row.kind}|${row.source}|${String(row.content).slice(0, 240)}`;
      if (!row.content || seen.has(key)) return;
      seen.add(key); unique.push(row);
    });
    if (task.result && !unique.some((row) => row.content === task.result)) unique.push({ key: "result", source: "集群汇总", content: task.result, kind: "result", time: nowTime() });
    return unique;
  }

  function renderFeed() {
    const feed = $("#feed");
    const empty = $("#emptyState");
    if (!state.active) {
      empty.classList.remove("hidden");
      feed.querySelectorAll(".feed-message").forEach((node) => node.remove());
      return;
    }
    empty.classList.add("hidden");
    const rows = collectFeed(state.active);
    feed.querySelectorAll(".feed-message").forEach((node) => node.remove());
    rows.forEach((row) => {
      const classes = ["feed-message", row.kind];
      if (row.kind === "agent") classes.push(colorClass(row.agent, row.source));
      const source = row.kind === "agent" ? `【${row.source}】` : row.source;
      const role = row.agent?.model_name || row.agent?.model || (row.kind === "system" ? "运行时状态" : row.kind === "user" ? "任务请求" : "协作消息");
      const node = document.createElement("article");
      node.className = classes.join(" ");
      node.innerHTML = `<div class="message-avatar" aria-hidden="true">${safe(initials(row.source))}</div><div><div class="message-head"><strong class="message-source">${safe(source)}</strong><span class="message-role">${safe(role)}</span><time class="message-time">${safe(row.time || "")}</time></div><div class="message-body">${safe(row.content)}</div></div>`;
      feed.appendChild(node);
    });
    feed.scrollTop = feed.scrollHeight;
  }

  function renderRun() {
    const task = state.active;
    const status = task?.status || "idle";
    const stateNode = $("#runState");
    stateNode.textContent = statusLabel(status);
    stateNode.className = `run-state ${taskStatusClass(status)}`;
    $("#runTitle").textContent = task ? (task.title || task.prompt || "未命名任务") : "等待你的第一个任务";
    $("#runAgentCount").textContent = task ? String(taskAgents(task).length || (task.cluster_started ? MODE_DEFINITIONS[state.mode].maxSlots : 1)) : "0";
    $("#runEventCount").textContent = task ? String((task.events || []).length) : "0";
    if (!state.startedAt && task) state.startedAt = Date.now();
    $("#appTitle").textContent = task ? (task.title || "任务执行中") : "准备接收任务";
    if (task && ["running", "planning", "queued", "chatting", "awaiting_confirmation"].includes(String(status).toLowerCase())) {
      const seconds = Math.max(0, Math.floor((Date.now() - state.startedAt) / 1000));
      $("#runElapsed").textContent = `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
    } else if (!task) $("#runElapsed").textContent = "00:00";
  }

  function eventKind(event) {
    const type = String(event?.type || "").toLowerCase();
    if (type.includes("fail") || type.includes("error") || type.includes("offline") || type.includes("blocked")) return "error";
    if (type.includes("switch") || type.includes("compress") || type.includes("pause") || type.includes("warn")) return "warning";
    if (type.includes("complete") || type.includes("success") || type.includes("started")) return "success";
    return "info";
  }

  function registerTaskEvents(task) {
    (task?.events || []).forEach((event) => {
      const key = event.id || `${event.type}|${event.timestamp}|${event.message}`;
      if (state.seenEvents.has(key)) return;
      state.seenEvents.add(key);
      if (!isPublicStatusEvent(event, agentFor(task, event.agent_id || event.agentId))) return;
      const message = event.message || event.content || event.detail || "集群状态更新";
      const kind = eventKind(event);
      state.stream.unshift({ key, message, kind, time: eventTime(event) || nowTime() });
      const lower = String(message).toLowerCase();
      if (lower.includes("compress") || lower.includes("上下文") || lower.includes("context")) pushNotice(`上下文已压缩：${message}`, "warning", `context-${key}`);
      else if (lower.includes("switch") || lower.includes("替换") || lower.includes("fallback") || lower.includes("model")) pushNotice(`模型状态变化：${message}`, "warning", `model-${key}`);
      else if (lower.includes("offline") || lower.includes("下线") || lower.includes("failed") || lower.includes("失败")) pushNotice(`岗位异常：${message}`, "error", `agent-${key}`);
    });
    if (["failed", "interrupted"].includes(String(task?.status || "").toLowerCase())) pushNotice(task.interruption_reason || "任务执行被中断，请检查状态流。", "error", `task-${task.id}-failed`);
    state.stream = state.stream.slice(0, 35);
    renderStatusStream();
  }

  function renderStatusStream() {
    const node = $("#statusStream");
    $("#statusStreamCount").textContent = String(state.stream.length);
    node.innerHTML = state.stream.length ? state.stream.slice(0, 18).map((item) => `<div class="stream-item ${safe(item.kind)}"><strong>${safe(item.message)}</strong><small>${safe(item.time)}</small></div>`).join("") : `<p class="muted">等待集群事件……</p>`;
  }

  function mergeTask(task) {
    if (!task?.id) return;
    state.tasks = [task, ...state.tasks.filter((item) => item.id !== task.id)].sort((a, b) => String(b.updated_at_iso || b.created_at_iso || b.created_at || "").localeCompare(String(a.updated_at_iso || a.created_at_iso || a.created_at || "")));
    if (state.active?.id === task.id || !state.active) state.active = task;
    registerTaskEvents(task);
    renderTaskList(); renderRun(); renderFeed(); renderRoster();
  }

  function selectTask(id) {
    const task = state.tasks.find((item) => item.id === id);
    if (!task) return;
    state.active = task; state.startedAt = Date.now(); state.stream = []; state.seenEvents.clear();
    mergeTask(task); connectSocket(task.id);
    api(`/api/tasks/${encodeURIComponent(task.id)}`).then((fresh) => mergeTask(fresh)).catch(() => {});
  }

  function connectSocket(taskId) {
    if (state.socket) { try { state.socket.close(); } catch (_) {} state.socket = null; }
    if (!window.WebSocket || location.protocol === "file:") return;
    const protocol = location.protocol === "https:" ? "wss" : "ws";
    try {
      const socket = new WebSocket(`${protocol}://${location.host}/ws/tasks/${encodeURIComponent(taskId)}`);
      state.socket = socket;
      socket.onmessage = (event) => {
        try {
          const payload = JSON.parse(event.data);
          if (payload.task) mergeTask(payload.task);
          if (payload.event && state.active) { state.active = { ...state.active, events: [...(state.active.events || []), payload.event] }; registerTaskEvents(state.active); renderFeed(); }
        } catch (_) { /* polling remains authoritative */ }
      };
      socket.onclose = () => { if (state.socket === socket) state.socket = null; };
      socket.onerror = () => { try { socket.close(); } catch (_) {} };
    } catch (_) { /* stdlib server has no websocket; polling is enough */ }
  }

  async function refreshAll() {
    if (state.refreshing) return;
    state.refreshing = true;
    try {
      const [system, tasks] = await Promise.all([api("/api/system"), api("/api/tasks")]);
      state.system = system || {};
      state.agentProfiles = state.system.agent_profiles || state.system.mode_roles || state.agentProfiles || {};
      state.roleCatalog = Array.isArray(state.system.role_catalog) ? state.system.role_catalog : state.roleCatalog;
      const serverMode = modeFromPayload(state.system);
      if (state.system?.mode != null || state.system?.runtime_mode != null || state.system?.agent_mode != null) state.mode = serverMode;
      state.tasks = Array.isArray(tasks) ? tasks : (Array.isArray(tasks?.tasks) ? tasks.tasks : []);
      if (state.active?.id) {
        const current = state.tasks.find((task) => task.id === state.active.id);
        if (current) state.active = current;
        try { state.active = await api(`/api/tasks/${encodeURIComponent(state.active.id)}`); } catch (_) { /* keep previous snapshot */ }
      } else if (state.tasks.length) {
        const candidate = state.tasks.find((task) => !TERMINAL.has(String(task.status || "").toLowerCase())) || state.tasks[0];
        state.active = candidate; state.startedAt = Date.now(); connectSocket(candidate.id);
      }
      setConnection(true);
      renderMode(); renderTaskList(); renderRoster(); renderRun(); renderFeed();
      if (state.active) registerTaskEvents(state.active);
      if (state.active && !state.active.cluster_started && state.active.mode_managed !== true && !TERMINAL.has(String(state.active.status || "").toLowerCase())) autoAdvance(state.active);
    } catch (error) {
      setConnection(false);
      if (!state.system) pushNotice(`无法连接本地服务：${error.message}`, "error", "service-offline");
    } finally { state.refreshing = false; }
  }

  async function controlTask(taskId, action, extra = {}) {
    return api(`/api/tasks/${encodeURIComponent(taskId)}/control`, { method: "POST", body: { action, ...extra } });
  }

  async function autoAdvance(initialTask) {
    if (state.autoAdvanceBusy || !initialTask?.id || state.mode === 0 || initialTask.mode_managed === true) return;
    state.autoAdvanceBusy = true;
    const taskId = initialTask.id;
    try {
      for (let round = 0; round < 32; round += 1) {
        let task = state.active?.id === taskId ? state.active : null;
        try { task = await api(`/api/tasks/${encodeURIComponent(taskId)}`); mergeTask(task); } catch (_) { await sleep(450); continue; }
        if (!task || task.cluster_started || TERMINAL.has(String(task.status || "").toLowerCase())) break;
        if (!task.cluster_available) { await sleep(450); continue; }
        if (task.needs_clarification || Number(task.task_turns || 0) < 2) {
          if (!state.autoClarified.has(taskId) && !task.cluster_started) {
            state.autoClarified.add(taskId);
            try {
              task = await api(`/api/tasks/${encodeURIComponent(taskId)}/messages`, { method: "POST", body: { content: "目标已明确，请直接按原始要求执行；如需取舍，以可验证、可交付为准。", cluster_enabled: true } });
              mergeTask(task); pushNotice("总管理已完成需求澄清，集群继续推进。", "info", `clarified-${taskId}`);
            } catch (error) { pushNotice(`自动推进暂时受阻：${error.message}`, "warning", `clarify-error-${taskId}`); }
          }
          await sleep(650); continue;
        }
        if (task.last_answered_token != null && task.reply_token != null && task.last_answered_token !== task.reply_token) { await sleep(550); continue; }
        if (!task.workflow_confirmed && (task.workflow_ready || task.needs_clarification === false)) {
          try { task = await controlTask(taskId, "confirm_workflow"); mergeTask(task); pushNotice("工作流已自动确认，正在准备岗位协作。", "info", `workflow-${taskId}`); } catch (error) { if (!String(error.message).includes("before confirming")) pushNotice(`工作流确认等待中：${error.message}`, "warning", `workflow-error-${taskId}`); }
          await sleep(500); continue;
        }
        if (!task.reasoning_approved && task.workflow_confirmed) {
          const level = task.reasoning_recommendation?.level || task.cluster_reasoning || "medium";
          try { task = await controlTask(taskId, "approve_reasoning", { level, reasoning_level: level }); mergeTask(task); pushNotice(`推理强度已自动批准（${level}），准备启动集群。`, "info", `reasoning-${taskId}`); } catch (error) { pushNotice(`推理配置等待中：${error.message}`, "warning", `reasoning-error-${taskId}`); }
          await sleep(500); continue;
        }
        if (!task.cluster_started && task.reasoning_approved) {
          try { task = await controlTask(taskId, "start_cluster"); mergeTask(task); pushNotice("集群已启动，岗位正在并行执行。", "success", `cluster-${taskId}`); } catch (error) { pushNotice(`集群启动等待中：${error.message}`, "warning", `cluster-error-${taskId}`); }
          break;
        }
        await sleep(500);
      }
    } finally { state.autoAdvanceBusy = false; }
  }

  async function submitPrompt(prompt) {
    const content = String(prompt || "").trim();
    if (!content || state.submitting) return;
    state.submitting = true; $("#sendButton").disabled = true; $("#composerStatus").textContent = "提交中……";
    try {
      const task = await api("/api/tasks", { method: "POST", body: { prompt: content, cluster_enabled: state.mode !== 0, mode: state.mode } });
      state.active = task; state.startedAt = Date.now(); state.stream = []; state.seenEvents.clear();
      mergeTask(task); connectSocket(task.id); $("#promptInput").value = "";
      pushNotice(`${MODE_NAMES[state.mode]} 已接收任务，正在识别意图并分配岗位。`, "success", `created-${task.id}`);
      autoAdvance(task);
    } catch (error) {
      pushNotice(`任务提交失败：${error.message}`, "error", `submit-${Date.now()}`);
    } finally { state.submitting = false; $("#sendButton").disabled = false; $("#composerStatus").textContent = ""; }
  }

  function resetComposerIntent() {
    state.composerIntent = "task";
    const input = $("#promptInput");
    input.placeholder = "描述你要完成的事情，集群会自动协作……";
    $("#composerStatus").textContent = "";
  }

  function activateLogSearch() {
    state.composerIntent = "search";
    const input = $("#promptInput");
    input.placeholder = "输入关键词或岗位，发送后检索日志……";
    $("#composerStatus").textContent = "日志检索";
    input.focus();
    pushNotice("已切换到日志检索：在底部输入框输入关键词并发送。", "info", `log-mode-${Date.now()}`);
  }

  function localSearch(query) {
    const needle = query.toLowerCase();
    const hits = [];
    state.tasks.forEach((task) => {
      const rows = collectFeed(task);
      rows.forEach((row) => {
        if (`${row.source} ${row.content}`.toLowerCase().includes(needle)) hits.push({ task, source: row.source, content: row.content, time: row.time });
      });
    });
    return hits.slice(0, 60);
  }

  function normalizeSearchPayload(payload) {
    const values = Array.isArray(payload) ? payload : payload?.results || payload?.logs || payload?.matches || payload?.items || [];
    return Array.isArray(values) ? values.map((item) => ({ taskId: item.task_id || item.taskId || item.task?.id, source: item.source || item.role || item.agent || item.agent_role || item.agent_id || "系统", content: item.content || item.message || item.detail || "", time: item.timestamp || item.time || "" })).filter((item) => item.content) : [];
  }

  function parseLogQuery(value) {
    const source = String(value || "").trim();
    const field = (pattern) => source.match(pattern)?.[1]?.trim() || "";
    const role = field(/(?:^|\s)(?:role|岗位)\s*[:：]\s*([^\s]+)/i);
    const from = field(/(?:^|\s)(?:from|开始|起始)\s*[:：]\s*([^\s]+)/i);
    const to = field(/(?:^|\s)(?:to|结束|截至)\s*[:：]\s*([^\s]+)/i);
    const keyword = source
      .replace(/(?:^|\s)(?:role|岗位|from|开始|起始|to|结束|截至)\s*[:：]\s*[^\s]+/gi, " ")
      .trim();
    return { keyword, role, from, to };
  }

  async function searchLogs(query) {
    const requestId = ++state.logRequest;
    const value = String(query || "").trim();
    const node = $("#logResults");
    if (!value) { node.innerHTML = `<p class="muted">输入关键词检索集群历史记录。</p>`; return; }
    node.innerHTML = `<p class="muted">搜索中……</p>`;
    const filters = parseLogQuery(value);
    const params = new URLSearchParams();
    params.set("keyword", filters.keyword);
    if (filters.role) params.set("role", filters.role);
    if (filters.from) params.set("from_time", filters.from);
    if (filters.to) params.set("to_time", filters.to);
    const encoded = params.toString();
    let hits = [];
    let foundEndpoint = false;
    for (const path of [`/api/logs/search?${encoded}`, `/api/logs?${encoded}`, `/api/search/logs?q=${encodeURIComponent(value)}`, `/api/tasks/search?q=${encodeURIComponent(value)}`]) {
      try { hits = normalizeSearchPayload(await api(path)); foundEndpoint = true; break; } catch (_) { /* try a legacy endpoint before local history */ }
    }
    if (!foundEndpoint) {
      hits = localSearch(value).map((item) => ({ taskId: item.task.id, source: item.source, content: item.content, time: item.time }));
    }
    if (requestId !== state.logRequest) return;
    node.innerHTML = hits.length ? hits.slice(0, 40).map((hit) => {
      const task = state.tasks.find((item) => item.id === hit.taskId);
      return `<button type="button" class="log-hit" data-task-id="${safe(hit.taskId || "")}"><strong>【${safe(hit.source)}】 ${safe(hit.content)}</strong><small>${safe(hit.time || task?.title || "历史记录")}</small></button>`;
    }).join("") : `<p class="muted">没有匹配记录。</p>`;
    node.querySelectorAll("[data-task-id]").forEach((button) => button.addEventListener("click", () => selectTask(button.dataset.taskId)));
  }

  function settingsMessage(message, error = false) {
    const node = $("#settingsNotice");
    if (!node) return;
    node.textContent = String(message || "");
    node.className = `settings-notice${error ? " error" : ""}`;
  }

  function providerRows() {
    return Array.isArray(state.providers) ? state.providers : [];
  }

  function providerById(id) {
    return providerRows().find((item) => String(item.id) === String(id)) || null;
  }

  function renderProviderList() {
    const node = $("#providerList");
    if (!node) return;
    const rows = providerRows();
    node.innerHTML = rows.length ? rows.map((provider) => {
      const configured = provider.secret?.configured ?? provider.configured;
      const health = String(provider.health || (configured ? "configured" : "offline")).toLowerCase();
      const label = provider.display_name || provider.name || provider.id;
      const models = Array.isArray(provider.models) ? provider.models.map((model) => typeof model === "string" ? model : model.id).filter(Boolean).join(", ") : "";
      return `<button type="button" class="provider-item ${provider.enabled === false ? "disabled" : ""}" data-provider-id="${safe(provider.id)}"><span><strong>${safe(label)} <small>${safe(provider.id)}</small></strong><small>${safe(provider.base_url || "")} · ${safe(models || "未声明模型")}</small></span><span class="provider-health ${health === "offline" || provider.enabled === false ? "offline" : ""}">${provider.enabled === false ? "已停用" : (configured ? "已配置" : "缺少密钥")}</span></button>`;
    }).join("") : `<p class="muted">还没有 Provider。点击“新增”添加一个接口。</p>`;
    node.querySelectorAll("[data-provider-id]").forEach((button) => button.addEventListener("click", () => fillProviderForm(providerById(button.dataset.providerId))));
  }

  function fillProviderForm(provider) {
    const value = provider || {};
    $("#providerId").value = value.id || "";
    $("#providerName").value = value.id || "";
    $("#providerDisplayName").value = value.display_name || value.name || "";
    $("#providerBaseUrl").value = value.base_url || "";
    $("#providerProtocol").value = value.protocol || "openai_chat";
    const models = Array.isArray(value.models) ? value.models.map((model) => typeof model === "string" ? model : model.id).filter(Boolean) : [];
    $("#providerModels").value = models.join(", ");
    $("#providerApiKey").value = "";
    $("#providerApiKey").placeholder = value.secret?.configured || value.configured ? "已配置；留空保持不变" : "输入新密钥（不会回显）";
    $("#providerKeyRef").value = value.secret?.ref || value.api_key_ref || value.api_key_env || value.env_key || "";
    document.querySelectorAll(".provider-item").forEach((item) => item.classList.toggle("selected", item.dataset.providerId === String(value.id || "")));
  }

  function clearProviderForm() {
    fillProviderForm({});
    $("#providerProtocol").value = "openai_chat";
    $("#providerApiKey").placeholder = "输入新密钥（不会回显）";
  }

  function routeKey(mode, role) {
    return `mode-${mode}/${role}`;
  }

  function routeFor(mode, role) {
    const roles = state.routes?.roles || {};
    return roles[routeKey(mode, role)] || roles[`${mode}/${role}`] || roles[role] || state.routes?.default || {};
  }

  function routeModels(providerId) {
    const provider = providerById(providerId);
    return Array.isArray(provider?.models) ? provider.models.map((model) => typeof model === "string" ? model : model.id).filter(Boolean) : [];
  }

  function profileEntries(mode) {
    const profile = profileForMode(mode);
    if (profile.present && Array.isArray(profile.entries)) return profile.entries.map((entry) => ({ ...entry }));
    return defaultRoleEntries(mode).map((role) => ({ role: role.label, role_key: role.key, max_count: role.max, model: role.model, provider_id: "", executor: role.executor || "direct_model" }));
  }

  function catalogOptions(selected) {
    const catalog = state.roleCatalog.length ? state.roleCatalog : Object.values(MODE_DEFINITIONS).flatMap((mode) => mode.roles.map((role) => ({ role: role.label, role_key: role.key, max_count: role.max, model: role.model })));
    return catalog.map((item) => `<option value="${safe(item.role)}" ${String(item.role) === String(selected) ? "selected" : ""}>${safe(item.role)}</option>`).join("");
  }

  function renderRoleProfiles() {
    const node = $("#roleProfileList");
    if (!node) return;
    const mode = Number($("#routeMode")?.value || state.mode);
    const entries = profileEntries(mode);
    node.innerHTML = entries.map((entry, index) => {
      const catalog = state.roleCatalog.find((item) => item.role === (entry.role || entry.name));
      const executor = entry.executor || catalog?.executor || recommendedExecutor(entry.role || entry.name, entry.model || catalog?.model);
      return `<div class="role-profile-row" data-profile-index="${index}"><select class="profile-role" aria-label="岗位">${catalogOptions(entry.role || entry.name)}</select><input class="profile-slots" type="number" min="1" max="100" value="${safe(entry.max_count ?? entry.slots ?? 1)}" aria-label="岗位人数" /><select class="profile-executor" aria-label="执行方式"><option value="direct_model" ${executor === "direct_model" ? "selected" : ""}>直接模型</option><option value="openclaw" ${executor === "openclaw" ? "selected" : ""}>OpenClaw</option><option value="codex" ${executor === "codex" ? "selected" : ""}>Codex</option><option value="claude_code" ${executor === "claude_code" ? "selected" : ""}>Claude Code</option></select><button type="button" class="profile-remove icon-button" title="移除岗位" aria-label="移除岗位">×</button></div>`;
    }).join("") || `<p class="muted">尚未配置岗位。</p>`;
    node.querySelectorAll(".profile-role").forEach((select) => select.addEventListener("change", (event) => {
      const entry = state.roleCatalog.find((item) => item.role === event.target.value);
      const row = event.target.closest(".role-profile-row");
      if (entry && row) {
        row.querySelector(".profile-slots").value = entry.max_count || 1;
        row.querySelector(".profile-executor").value = entry.executor || recommendedExecutor(entry.role, entry.model);
      }
    }));
    node.querySelectorAll(".profile-remove").forEach((button) => button.addEventListener("click", () => { button.closest(".role-profile-row")?.remove(); }));
  }

  function collectRoleProfiles() {
    const entries = {};
    for (let mode = 0; mode <= 3; mode += 1) entries[String(mode)] = profileEntries(mode);
    const activeMode = String(Number($("#routeMode")?.value || state.mode));
    const routeRows = new Map([...document.querySelectorAll("#routeList .route-row")].map((row) => [row.dataset.routeRole, row]));
    entries[activeMode] = [...document.querySelectorAll("#roleProfileList .role-profile-row")].map((row) => {
      const role = row.querySelector(".profile-role")?.value;
      const catalog = state.roleCatalog.find((item) => item.role === role);
      const routeRow = routeRows.get(catalog?.role_key);
      return {
        role,
        role_key: catalog?.role_key,
        max_count: Number(row.querySelector(".profile-slots")?.value || 1),
        provider_id: routeRow?.querySelector(".route-provider")?.value || "",
        model: routeRow?.querySelector(".route-model")?.value || catalog?.model || "",
        executor: row.querySelector(".profile-executor")?.value || catalog?.executor || "direct_model",
      };
    });
    return entries;
  }

  function renderRouteList() {
    const node = $("#routeList");
    if (!node) return;
    const mode = Number($("#routeMode")?.value || state.mode);
    const definition = { roles: configuredRoles(mode) };
    const providers = providerRows().filter((provider) => provider.enabled !== false);
    if (!providers.length) { node.innerHTML = `<p class="muted">没有可用 Provider，请先添加接口。</p>`; return; }
    if (!definition.roles.length) { node.innerHTML = `<p class="muted">当前模式未配置岗位。</p>`; return; }
    const providerOptions = (selected) => providers.map((provider) => `<option value="${safe(provider.id)}" ${String(provider.id) === String(selected) ? "selected" : ""}>${safe(provider.display_name || provider.id)}</option>`).join("");
    node.innerHTML = definition.roles.map((role) => {
      const route = routeFor(mode, role.key);
      const selectedProvider = route.provider_id || route.provider || role.providerId || (role.model.toLowerCase().includes("claude") ? "codekey" : state.system?.provider_name) || providers[0].id;
      const models = routeModels(selectedProvider);
      const selectedModel = route.model_id || route.model || role.model;
      const modelOptions = models.length ? models.map((model) => `<option value="${safe(model)}" ${String(model) === String(selectedModel) ? "selected" : ""}>${safe(model)}</option>`).join("") : `<option value="${safe(selectedModel)}" selected>${safe(selectedModel || "未指定")}</option>`;
      return `<div class="route-row" data-route-role="${safe(role.key)}"><div class="route-role"><strong>${safe(role.label)}</strong><small>${safe(role.model)} · ${safe(role.max)} 槽位</small></div><select class="route-select route-provider" aria-label="${safe(role.label)} provider">${providerOptions(selectedProvider)}</select><select class="route-select route-model" aria-label="${safe(role.label)} model">${modelOptions}</select></div>`;
    }).join("");
    node.querySelectorAll(".route-provider").forEach((select) => select.addEventListener("change", (event) => {
      const row = event.target.closest(".route-row");
      const model = row?.querySelector(".route-model");
      if (!model) return;
      const values = routeModels(event.target.value);
      model.innerHTML = values.length ? values.map((value) => `<option value="${safe(value)}">${safe(value)}</option>`).join("") : `<option value="">未声明模型</option>`;
    }));
  }

  async function loadSettingsData() {
    try {
      const [providers, routes, config] = await Promise.all([api("/api/providers"), api("/api/routes"), api("/api/agent-profiles").catch(() => ({}))]);
      state.providers = Array.isArray(providers) ? providers : (providers?.providers || []);
      state.routes = routes || {};
      state.agentProfiles = config.agent_profiles || config.mode_roles || state.system?.agent_profiles || state.agentProfiles || {};
      state.roleCatalog = Array.isArray(config.role_catalog) ? config.role_catalog : (state.system?.role_catalog || state.roleCatalog);
      renderProviderList(); renderRoleProfiles(); renderRouteList();
      settingsMessage(`已加载 ${state.providers.length} 个 Provider`);
    } catch (error) {
      settingsMessage(`设置加载失败：${error.message}`, true);
    }
  }

  async function saveProvider(event) {
    event.preventDefault();
    const id = $("#providerId").value.trim() || $("#providerName").value.trim();
    if (!id) { settingsMessage("Provider 标识不能为空", true); return; }
    const payload = {
      id,
      name: $("#providerDisplayName").value.trim(),
      display_name: $("#providerDisplayName").value.trim(),
      base_url: $("#providerBaseUrl").value.trim(),
      protocol: $("#providerProtocol").value,
      models: $("#providerModels").value.split(",").map((value) => value.trim()).filter(Boolean),
      api_key_ref: $("#providerKeyRef").value.trim(),
    };
    const key = $("#providerApiKey").value.trim();
    if (key) payload.api_key = key;
    try {
      const response = await api(`/api/providers/${encodeURIComponent(id)}`, { method: "PUT", body: payload });
      state.providers = response.providers || state.providers;
      renderProviderList(); fillProviderForm((response.providers || []).find((item) => item.id === id) || response.provider || providerById(id));
      settingsMessage(`Provider ${id} 已保存。密钥只保留在服务内存或指定环境变量中。`);
      await refreshAll();
    } catch (error) { settingsMessage(`保存失败：${error.message}`, true); }
  }

  async function disableProvider() {
    const id = $("#providerId").value.trim();
    if (!id) { settingsMessage("先选择一个 Provider", true); return; }
    try {
      const response = await api(`/api/providers/${encodeURIComponent(id)}`, { method: "DELETE", body: { disable: true } });
      state.providers = response.providers || state.providers;
      renderProviderList(); fillProviderForm(providerById(id)); settingsMessage(`Provider ${id} 已停用`); await refreshAll();
    } catch (error) { settingsMessage(`停用失败：${error.message}`, true); }
  }

  async function saveRoutes() {
    const mode = Number($("#routeMode").value || state.mode);
    const rows = [...document.querySelectorAll("#routeList .route-row")];
    try {
      const profiles = collectRoleProfiles();
      const config = await api("/api/agent-profiles", { method: "POST", body: { agent_profiles: profiles } });
      state.agentProfiles = config.agent_profiles || config.mode_roles || profiles;
      for (const row of rows) {
        const role = row.dataset.routeRole;
        const providerId = row.querySelector(".route-provider")?.value;
        const modelId = row.querySelector(".route-model")?.value;
        const configured = configuredRoles(mode).find((item) => item.key === role);
        await api(`/api/routes/roles/${mode}/${encodeURIComponent(role)}`, { method: "PUT", body: { provider_id: providerId, model_id: modelId, executor: configured?.executor || "direct_model" } });
      }
      settingsMessage(`模式 ${mode} 的职位路由已保存；在途任务继续使用原快照。`);
      const routes = await api("/api/routes"); state.routes = routes || state.routes; renderRouteList(); await refreshAll();
    } catch (error) { settingsMessage(`路由保存失败：${error.message}`, true); }
  }

  function openSettings() {
    const overlay = $("#settingsOverlay");
    if (!overlay) return;
    overlay.hidden = false; $("#routeMode").value = String(state.mode); clearProviderForm(); loadSettingsData();
  }

  function closeSettings() { const overlay = $("#settingsOverlay"); if (overlay) overlay.hidden = true; }

  function bind() {
    $("#modeSelect").value = String(state.mode);
    $("#modeSelect").addEventListener("change", async (event) => {
      state.mode = Number(event.target.value); localStorage.setItem("orbit-mode", String(state.mode)); renderMode(); renderRoster();
      pushNotice(`${MODE_NAMES[state.mode]} 已切换，后续任务将按新模式运行。`, "info", `mode-${state.mode}-${Date.now()}`);
      let changed = false;
      for (const path of ["/api/cluster/mode", "/api/mode", "/api/config"]) {
        try { state.system = await api(path, { method: "POST", body: { mode: state.mode, cluster_mode: state.mode, runtime_mode: state.mode } }); changed = true; break; } catch (_) { /* try the next compatible contract */ }
      }
      if (changed) { setConnection(true); renderMode(); renderRoster(); }
    });
    $("#refreshButton").addEventListener("click", () => refreshAll());
    $("#composer").addEventListener("submit", (event) => {
      event.preventDefault();
      const value = $("#promptInput").value;
      if (state.composerIntent === "search") { searchLogs(value); $("#promptInput").value = ""; resetComposerIntent(); return; }
      submitPrompt(value);
    });
    $("#promptInput").addEventListener("keydown", (event) => { if ((event.ctrlKey || event.metaKey) && event.key === "Enter") { event.preventDefault(); $("#composer").requestSubmit(); } });
    $("#promptInput").addEventListener("input", (event) => { event.currentTarget.style.height = "auto"; event.currentTarget.style.height = `${Math.min(150, event.currentTarget.scrollHeight)}px`; });
    document.querySelectorAll("[data-prompt]").forEach((button) => button.addEventListener("click", () => { resetComposerIntent(); $("#promptInput").value = button.dataset.prompt; $("#promptInput").focus(); }));
    $("#logSearchToggle").addEventListener("click", activateLogSearch);
    $("#settingsButton")?.addEventListener("click", openSettings);
    $("#settingsClose")?.addEventListener("click", closeSettings);
    $("#settingsOverlay")?.addEventListener("click", (event) => { if (event.target.id === "settingsOverlay") closeSettings(); });
    $("#providerNewButton")?.addEventListener("click", clearProviderForm);
    $("#providerForm")?.addEventListener("submit", saveProvider);
    $("#providerDisableButton")?.addEventListener("click", disableProvider);
    $("#routeMode")?.addEventListener("change", () => { renderRoleProfiles(); renderRouteList(); });
    $("#roleAddButton")?.addEventListener("click", () => {
      const node = $("#roleProfileList");
      if (!node) return;
      if (node.querySelector(".muted")) node.innerHTML = "";
      const template = document.createElement("div");
      template.className = "role-profile-row";
      template.innerHTML = `<select class="profile-role" aria-label="岗位">${catalogOptions("")}</select><input class="profile-slots" type="number" min="1" max="100" value="1" aria-label="岗位人数" /><select class="profile-executor" aria-label="执行方式"><option value="direct_model">直接模型</option><option value="openclaw">OpenClaw</option><option value="codex">Codex</option><option value="claude_code">Claude Code</option></select><button type="button" class="profile-remove icon-button" title="移除岗位" aria-label="移除岗位">×</button>`;
      template.querySelector(".profile-remove").addEventListener("click", () => template.remove());
      template.querySelector(".profile-role").addEventListener("change", (event) => {
        const entry = state.roleCatalog.find((item) => item.role === event.target.value);
        if (entry) {
          template.querySelector(".profile-slots").value = entry.max_count || 1;
          template.querySelector(".profile-executor").value = entry.executor || recommendedExecutor(entry.role, entry.model);
        }
      });
      node.appendChild(template);
    });
    $("#routeResetButton")?.addEventListener("click", () => { state.routes = { roles: {} }; renderRouteList(); settingsMessage("已清除当前模式的自定义路由，保存后恢复默认解析。"); });
    $("#routeSaveButton")?.addEventListener("click", saveRoutes);
    $("#languageButton").addEventListener("click", () => pushNotice("当前工作台以中文显示；岗位来源、状态和日志均保持原始记录。", "info", `language-${Date.now()}`));
  }

  function start() {
    bind(); renderMode(); renderRoster(); renderRun(); renderStatusStream(); renderNotices();
    refreshAll();
    state.poller = window.setInterval(refreshAll, 2200);
    window.setInterval(renderRun, 1000);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})();
