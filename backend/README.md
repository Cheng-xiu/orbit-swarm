# Backend

Orbit Swarm has two interchangeable local server entry points:

- `server_stdlib.py`: zero-dependency server based on Python's standard library.
- `main.py`: optional FastAPI server with WebSocket task snapshots.

Both serve the frontend from `../frontend` and expose the same main REST contract.

## Zero-dependency server

From the repository root:

```powershell
python backend/server_stdlib.py --port 8000
```

Open <http://127.0.0.1:8000>.

## FastAPI server

```powershell
python -m venv backend/.venv
backend/.venv/Scripts/Activate.ps1
python -m pip install -r backend/requirements.txt
cd backend
uvicorn main:app --host 127.0.0.1 --port 8000
```

## Configuration

The runtime reads process environment variables. `backend/.env.example` lists the supported names, but the zero-dependency server does not parse `.env` files automatically. The browser can also update the in-memory configuration through `POST /api/config`.

`ORBIT_MODE` selects the dynamic cluster profile: `0` starts one general assistant, `1` starts the five-slot mid profile, `2` starts the twenty-slot high profile, and `3` starts the one-hundred-slot extreme profile. `ORBIT_SWARM_MODE`, `SWARM_MODE`, and `MODE` are accepted aliases. Each role exposes its catalog maximum, while unavailable credentials, disabled model families, and optional `ORBIT_MAX_*` caps reduce the active count without taking down the rest of the cluster.

Simulation is enabled by default. Set `SWARM_SIMULATION=false` only after configuring a compatible provider and reviewing the executor permissions.

The default weighted concurrency policy is Low=1, Medium=2, High=5, Ultra=8, with a total limit of 30. `SWARM_WEIGHT_LOW`, `SWARM_WEIGHT_MEDIUM`, `SWARM_WEIGHT_HIGH`, `SWARM_WEIGHT_ULTRA`, and `SWARM_MAX_WEIGHT` override these values. A worker waits for enough weighted capacity before invoking an executor.

When both `A6_OPENAI_BASE_URL` and `A6_OPENAI_API_KEY` are absent, the legacy projection may reuse `models.providers.a6api` from `%USERPROFILE%\\.openclaw\\openclaw.json` (or `OPENCLAW_CONFIG_PATH`). The structured registry also imports the other OpenClaw providers, including `codekey`. Independently, Orbit always declares `codekey` at `https://codekey.ai/v1` with default model `claude-opus-5`; its credential is read from `CODEKEY_API_KEY` when OpenClaw does not already supply one. Values are held in memory only; they are not returned by `/api/system`, written to this repository, or sent to the browser.

The live adapters are deliberately opt-in:

- Direct model calls use OpenAI-compatible `/chat/completions` or Anthropic `/messages`, according to the selected provider protocol.
- Codex is invoked through its CLI with a read-only sandbox, the resolved provider/model override, and a task-scoped credential environment.
- OpenClaw is invoked through its CLI with a task-scoped session id and the resolved `provider/model` pair.
- Claude Code is invoked in non-interactive print mode and uses its own native Claude Code configuration; it is never selected by default for an OpenAI-compatible provider.

### Editable mode profiles

`GET` and `POST`/`PUT /api/agent-profiles` expose the per-mode role composition. Each row can select a catalog role and set `max_count`, `provider_id`, `model`, and one of `direct_model`, `codex`, `openclaw`, or `claude_code`. A missing mode profile uses the built-in contract; an explicit empty list keeps that mode empty and ordinary tasks receive a clear main-agent blocker instead of starting an invalid cluster.

The same edits can come through the only task input. `agent_profile_commands.py` deterministically recognizes explicit commands to keep, add, remove, clear, resize, re-route, or change a role's executor. It does not call a model. Each instantiated slot receives a stable friendly name, while internal dispatch and heartbeat transcripts remain persisted and searchable without being emitted into the user-facing chat feed.

### Provider and route registry

The additive registry supports OpenAI-compatible chat, Anthropic messages, and CLI providers. Configure named providers with `ORBIT_PROVIDERS_JSON`, `ORBIT_PROVIDER_<ID>_*`, or the built-in `OPENAI_*`, `ANTHROPIC_*`, and `DEEPSEEK_*` variables. Use `ORBIT_ROUTES_JSON` to assign a provider/model to a tier, pool, or role. The browser and integrations can inspect redacted metadata with `GET /api/providers` and `GET /api/routes`, update profiles with `PUT /api/providers/{id}`, and update or remove routes with `PUT`/`DELETE /api/routes/{scope}/{key}`. `POST /api/providers/{id}/test` performs a non-network simulation check by default; pass `{"live":true}` only when a live provider is intentionally enabled.

Tasks, events, configuration snapshots, and attachment metadata are persisted in `work/state/orbit-state-v1.json` (or the directory named by `ORBIT_SWARM_DATA_DIR`). Secrets remain in process memory/environment and are redacted before persistence, exports, or `/api/system`. On restart, completed task structure and searchable event history are restored; malformed state is preserved for recovery and the service continues with a clean snapshot.

For long conversations, `ORBIT_CONTEXT_LIMIT`, `ORBIT_CONTEXT_THRESHOLD`, and `ORBIT_CONTEXT_FALLBACK_MODEL` control token estimation, automatic compression, and large-context model handoff. `ORBIT_HR_ERROR_THRESHOLD` and `ORBIT_HR_MIN_TASKS` tune high/extreme-mode health intervention.

## 后端模块地图

| 文件 | 责任 | 什么时候需要修改 |
| --- | --- | --- |
| `server_stdlib.py` | 标准库 HTTP 服务、静态文件、REST 路由和任务后台线程 | 修改零依赖服务行为或增加 HTTP 端点 |
| `main.py` | FastAPI/Uvicorn 入口、WebSocket 任务流和同一套业务契约 | 需要 WebSocket、异步部署或 FastAPI 集成 |
| `executors.py` | 运行配置、模型档位、难度评估、直接模型/Codex/OpenClaw/Claude Code 适配 | 增加模型协议、执行器或任务前置流程 |
| `providers.py` | Provider 和 Route 的数据结构、环境变量导入、优先级解析和脱敏公开数据 | 增加 API 接口或路由匹配规则 |
| `routing_api.py` | 两个服务入口共用的 Provider/Route CRUD 辅助函数 | 调整 HTTP 层的配置更新语义 |
| `cluster.py` | 模式、岗位目录、槽位实例、稳定命名、发布/订阅、心跳、上下文和争议裁决 | 修改集群协作规则或新增岗位行为 |
| `agent_profile_commands.py` | 唯一输入框中的中文岗位配置命令解析 | 增加自然语言配置句式 |
| `storage.py` | `orbit-state-v1.json` 的原子保存、加载、恢复和密钥脱敏 | 修改状态格式或恢复策略 |
| `exports.py` | JSON/Markdown 导出、文件名安全处理和导出脱敏 | 增加导出格式或字段 |
| `test_*.py` | 岗位、路由、命名、自然语言命令和 HTTP 回归测试 | 修改对应契约时同步补测试 |

建议先在与需求最接近的模块中修改，再通过 `server_stdlib.py` 和 `main.py` 的共同契约验证两个入口。不要在前端复制 Provider 解析逻辑；前端只负责编辑和提交表单，最终校验由后端完成。

## 请求到结果的生命周期

一次普通任务的后端路径如下：

1. `POST /api/tasks` 校验 `prompt`、附件和工作区标志，并为任务保存配置快照。
2. `executors.assess_task_difficulty()` 估算任务复杂度，协调 Agent 生成澄清、推理建议和资源估算。
3. 用户确认工作流和资源建议后，任务进入集群阶段；模式和岗位配置决定期望槽位数。
4. `ClusterRuntime` 根据 Provider 可用性、环境变量和每个岗位的最大数量实例化 Agent。某一模型不可用只会让对应槽位变为未激活，其他槽位继续运行。
5. 工作项通过 `PubSubBroker` 发布到 `global`、`architecture`、`development`、`testing`、`debate`、`hr`、`status` 等主题。订阅者不会直接硬编码调用其他 Agent。
6. 执行器返回结果后，任务写入事件、岗位会话和对外对话；基础派发与心跳记录只保存在内部记录，前端过滤后不进入主聊天流。
7. 协调 Agent 生成最终汇总，任务和配置快照以脱敏形式写入状态文件。

心跳监控默认 30 秒。超时后由单模式自身、中档模式 GM、高档/极限模式 HR 发出状态并尝试重启。极限模式的争议流程包含主持人、正反方辩手、多轮陈述、激活 Agent 投票和 HR 平局裁决；这些关键状态会进入外部事件流。

## REST 接口详解

所有接口默认由同一个本机服务提供，JSON 使用 UTF-8。标准库版本没有 CORS 和认证层，建议只绑定 `127.0.0.1`。

### 系统、集群和日志

| 方法 | 路径 | 返回内容 |
| --- | --- | --- |
| `GET` | `/api/system` 或 `/api/config` | 脱敏运行配置、Provider、Route、执行器、模式和 Agent 槽位 |
| `GET` | `/api/cluster` 或 `/api/cluster/status` | 集群在线数、岗位计数、心跳阈值和主题 |
| `POST` | `/api/cluster/mode` 或 `/api/mode` | 只提交 `{ "mode": 0..3 }` 切换当前模式 |
| `GET` | `/api/logs?keyword=oauth&role=后端开发组` | 搜索持久化内部/外部事件，可附 `from`、`to` 时间条件 |

`/api/system` 中的 `providers`、`routes`、`agents` 和 `role_status` 是前端状态面板的主要来源。Provider 的密钥只以 `api_key_configured` 和末尾提示出现；不要把原始配置对象直接写入新的日志。

### Provider 和 Route

新增一个 OpenAI 兼容 Provider：

```powershell
$body = @{
  id = "my-provider"
  display_name = "My Provider"
  base_url = "https://api.example.com/v1"
  protocol = "openai_chat"
  models = @("model-a", "model-b")
  api_key_env = "MY_PROVIDER_API_KEY"
} | ConvertTo-Json
Invoke-WebRequest http://127.0.0.1:8000/api/providers -Method Post -ContentType "application/json" -Body $body
```

可用协议是 `openai_chat` 和 `anthropic_messages`；CLI 执行器通过 `executor` 字段选择，不把 CLI 当成 HTTP 协议。更新或停用 Provider：

```powershell
Invoke-WebRequest http://127.0.0.1:8000/api/providers/my-provider -Method Put -ContentType "application/json" -Body $body
Invoke-WebRequest http://127.0.0.1:8000/api/providers/my-provider -Method Delete
```

岗位路由使用稳定的 `mode-{mode}/{role_key}` 键。前端保存岗位清单时会调用 `/api/routes/roles/{mode}/{role_key}`；直接调用 API 时至少提供 `provider_id`、`model_id` 和可选 `executor`。路由优先级是岗位 > 池 > 档位 > 默认 Provider，任务启动时把最终解析结果固定到任务快照。

### 岗位配置

读取岗位目录和当前配置：

```powershell
$profiles = Invoke-RestMethod http://127.0.0.1:8000/api/agent-profiles
$profiles.role_catalog
$profiles.agent_profiles
```

保存模式 2 的岗位清单：

```powershell
$body = @{
  profile_mode = 2
  roles = @(
    @{ role = "系统架构师"; max_count = 1; executor = "direct_model" }
    @{ role = "后端开发组"; max_count = 2; executor = "codex"; provider_id = "a6api"; model = "GPT-5.6 Terra" }
  )
} | ConvertTo-Json -Depth 5
Invoke-WebRequest http://127.0.0.1:8000/api/agent-profiles -Method Put -ContentType "application/json" -Body $body
```

也可以把同一句话作为任务输入。解析器是确定性的，不会调用模型：

```text
模式2只保留系统架构师、后端开发组和测试开发组
模式2添加文档执行组2个，使用 OpenClaw，模型 DeepSeek V4 Flash，接口 deepseek
模式1全栈开发改成 Codex，模型 GPT-5.6 Terra
模式3清空岗位
```

显式清空的档位会保持空配置；普通任务不会崩溃，而是返回 `blocked_reason=no_configured_roles` 的主 Agent 提示。

### 任务和导出

创建任务的最小请求：

```json
{
  "prompt": "重构登录模块并补充 OAuth2 测试",
  "cluster_enabled": true,
  "workspace": false,
  "attachments": []
}
```

任务接口包括：

- `GET /api/tasks`：倒序任务列表。
- `POST /api/tasks`：创建任务，岗位配置命令也从这里进入。
- `GET /api/tasks/{id}`：读取完整任务快照。
- `GET /api/tasks/{id}/messages`：读取外部对话和内部岗位会话。
- `POST /api/tasks/{id}/messages`：继续当前任务讨论。
- `POST /api/tasks/{id}/control`：提交 `pause`、`resume`、`cancel` 或 `retry`。
- `GET /api/tasks/{id}/export?format=json`：导出脱敏 JSON。
- `GET /api/tasks/{id}/export?format=markdown`：导出 Markdown 报告。

## 测试和本地开发

零依赖环境下，在 `backend` 目录执行：

```powershell
python -m unittest discover -v -p "test_*.py"
python -m py_compile *.py
```

前端没有打包步骤，检查 JavaScript 语法：

```powershell
node --check ..\frontend\app.js
```

HTTP 回归测试会使用临时状态目录和随机端口，不会覆盖正式 `work/state/orbit-state-v1.json`。新增接口时至少补一条单元测试和一条 HTTP 测试，并同时验证标准库入口和 FastAPI 入口的响应字段。

## 常见问题

**启动后页面显示连接失败怎么办？** 确认服务进程仍在运行，浏览器访问的端口与启动参数一致；标准库服务默认是 `8000`，不要同时启动两个服务占用同一个端口。

**为什么岗位数量少于目录中的最大值？** 这是设计行为。Provider 密钥、模型可用性、`ORBIT_MAX_*` 上限和加权并发都会降低实际激活数，系统会在状态面板中显示 `active/maximum`。

**为什么真实模型没有被调用？** `SWARM_SIMULATION=true` 是默认值。只有明确设置为 `false`、Provider 可用且对应 CLI 已安装，才会启用真实执行。

**为什么修改路由后旧任务没有变化？** 任务创建时保存配置快照。新路由只影响新任务，旧任务继续使用原快照，避免中途换模造成结果不可复现。

**如何增加新的执行器？** 先在 `ExecutorKind` 增加稳定标识，再在 `available_executors()`、`execute_agent()` 和路由校验中注册，最后补模拟分支、不可用分支、密钥脱敏和 HTTP 回归测试。前端的执行器下拉框也要同步，但不要改变已有标识的含义。
