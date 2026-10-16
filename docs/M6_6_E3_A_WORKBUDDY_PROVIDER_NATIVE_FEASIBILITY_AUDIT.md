# M6.6-E3-A — WorkBuddy Provider-native Adapter Feasibility Audit

> 前序：
> - `M6.6-D Provider Adapter & Real First Success Audit` — **CONDITIONAL PASS**
>   - Layer 1 ACP Core：PASS；Layer 2 WeCom / WeCom Bot：PASS；WorkBuddy：CONDITIONAL/GAP（F3/F4）
>   - Layer 3：F1 Flow-scoped Connected、F2 WeCom Bot Hot Discovery、F3/F4 WorkBuddy Provider-native UX、F5/F6 Verify semantics
>
> 本轮：仅处理 **F3/F4 — WorkBuddy Provider-native Adapter** 的可行性。
> **只读纪律**：唯一新增物是本文件；未改动 backend / frontend / migration / tests / ADR / config / 生产；未创建真实 Runtime / Agent；未 commit / tag。
> 本轮**不写代码、不实现**（见 §24 不提前实现）。

---

## 1. Scope

判断当前 SteeringFlow 能否以**最小范围**实现：

```
WorkBuddy
  ↓ Agent
  ↓ Agent ID
  ↓ API Key
  ↓ SteeringFlow WorkBuddy Adapter
  ↓ WorkBuddy Management API
  ↓ Runtime
  ↓ Agent execution
  ↓ Response
```

用户目标体验：Connect → Select/enter Agent → Agent ID → API Key → Connect → Connected → Verify/Test；**不应理解或填写** `runtime_id` / `acpLink` / ACP session / `bearer_token` / ACP endpoint / Connection internals。

同时审计两个边界：
- **A. SteeringFlow 当前代码**（Agent / AgentAdapter / Connection / Connection.config / Worker / Runtime execution / API / Frontend）。
- **B. WorkBuddy 当前实际 API 能力**（以仓库已保存文档为准，优先 `docs/step-10.1.3-tencent-acp-wire.md`）。

---

## 2. Current WorkBuddy Implementation

**事实还原（非理想设计）**：WorkBuddy 当前**不是** Provider-native 集成，而是 `Agent(type="acp")` + 手动粘贴 `Connection.config` 三值。

真实链路（CURRENT）：

```
frontend/connect-agent.js (填入 ACP 模板)
  → POST /api/agents   {name:"workbuddy", type:"acp"}   （application/agents.py:157）
  → POST /api/connections
       {channel_id, agent_id, config:{
          protocol:"tencent-acp-sse",
          endpoint:"<acpLink.url>",        ← 用户手填
          bearer_token:"<acpLink.token>",  ← 用户手填
          runtime_id:"<runtime.id>"}}       ← 用户手填
  → backend 存 Connection.config（JSON 列，明文；读时脱敏）
  → 运行时 Worker → AgentDispatchExecutor → AgentAdapter.send(connection)
       → adapters/agents/acp.py
            endpoint = config["endpoint"]   (acp.py:469)
            token   = config["bearer_token"] (acp.py:474)
            runtime_id = config["runtime_id"] (acp.py:478)
       → GET {url} 持久 SSE + POST jsonrpc session/prompt (acp.py:460-653)
       → agent_message_chunk 累积 → AgentResponse(content, external_session_id, metadata)
  → RuntimeCoordinator → Delivery → Channel
```

- **WorkBuddy 专有概念仅存在于**：`Connection.config`（自由 JSON）+ `ACPAdapter` 内部（`acp.py`）。全仓搜 `runtime_id|acpLink|bearer_token` 在 `domain`/`persistence` 均零命中（M6.6-D §16 已确认，本轮不推翻）。
- **Runtime 创建完全在 SteeringFlow 之外**（用户在 WorkBuddy 控制台手动创建，粘贴三值）。`acp.py` 仅**读取** `Connection.config`，从不调用任何 Runtime 创建端点（M6.6-D PART B 确认）。

---

## 3. Current ACP Boundary

冻结的 Runtime 链（`docs/step-10.1.4-runtime-vertical-slice-closure.md` §1）已闭合并冻结。相关边界：

- **Agent Adapter Port**：`AgentAdapter.send(request, connection) -> AgentResponse`（`repositories/agent_adapter.py`）。协议 auth/序列化/transport/解析归 Adapter，Core 零传输依赖。
- **Connection = Channel ↔ Agent**（domain/connection.py:36-48），`UNIQUE(channel_id, agent_id)`，status 纯管理态 `{active, inactive}`。
- **Session Boundary（冻结）**：隔离键 = `Identity.id × Connection.id × external_conversation_id`（step-10.1.4 §4）。
- **Connection.config** 是自由 JSON 字典，已承载 `endpoint/bearer_token/runtime_id/timeout_s`。
- **SENSITIVE_CONNECTION_CONFIG_KEYS**（`schemas/connections.py:51-54`）已含 `bearer_token, access_token, api_key, client_secret, token, secret, password, authorization`——**`api_key` 已在脱敏清单内**（关键：WorkBuddy API Key 存为 `config["api_key"]` 即自动掩码）。

**结论**：当前 ACP Domain（Agent / Connection / Channel / Session / Interaction）**无需改变**即可支持 WorkBuddy Agent。WorkBuddy-specific 概念可完全收口在 Adapter / Connection.config。M6.6-D 的"无 Core 污染"结论本轮确认维持。

---

## 4. WorkBuddy Official Capability Matrix

以仓库保存文档为准（`docs/step-10.1.3-tencent-acp-wire.md` 转录自"腾讯管理后台 API 集成说明"）。分类：`O`=officially supported（文档写明契约）；`C`=observed in current code；`I`=inferred；`U`=unknown。

| Capability | WorkBuddy API | 当前 ACP | Gap | 分类 |
|---|---|---|---|---|
| API Key authentication | `POST /v2/agentos/runtimes` `Authorization: Bearer <AgentOS token>` + `X-Source-App` | `acp.py:498` 用 `Bearer <acpLink.token>` 通信 | 管理面鉴权客户端缺失 | Management API：O（文档）；ACP 通信面：C |
| Agent discovery (list) | 仓库无此契约 | 无平台侧发现 | 缺失（不可虚构） | U |
| Agent selection by ID | `agentManifest.id`（创建 Runtime 时传入） | 仅 `name+type` 建 Agent | 经 Runtime 创建隐含校验 | O（文档，作为 manifest.id）+ U（是否有独立 GET） |
| Runtime creation | `POST https://www.codebuddy.cn/v2/agentos/runtimes` body `runtimeName + agentManifest{id,name,manifestVersion,secrets}`，resp `data.id / data.links.acpLink.url / data.links.acpLink.token` | 无客户端（用户手填三值） | 缺失客户端 | O（文档）+ U（无代码） |
| Runtime status | 真实观测到 `RUNNING`（step-12.8.6:50） | 无状态机 | 平台侧，对 ACP 不透明 | I（观测 RUNNING）+ U（无 CREATING 枚举） |
| Runtime readiness | "Runtime 创建即自带 session"（step-10.1.3:12） | 无 | 平台侧 | I |
| ACP link retrieval | resp `data.links.acpLink.url/token` | `acp.py:469,474` 读取 | 客户端缺失 | O（文档）+ C（消费） |
| Prompt/session execution | `POST {url}` jsonrpc `session/prompt`（`sessionId + prompt`） | `acp.py:552-571` 已实现 | 无 | O（文档）+ C |
| Streaming response | SSE `session/update` → `agent_message_chunk` → `codebuddy.ai.isFinalStop` | `acp.py:238-281,586-653` 已实现并真实校准 | 无 | O（文档）+ C |
| Runtime cleanup | 仓库无任何 deleteRuntime | 无 | 缺失（未知是否需） | U |
| Error reporting | jsonrpc `error{code,message,data}`（`acp.py:293-306`）；管理面 400/401 | 已映射为 `AgentDispatchFailedError` | 管理面错误未单独建模 | O（文档）+ C（通信面） |

> **证据等级声明**：Management API 契约来自**单份转录文档**（`step-10.1.3`），非仓库内 OpenAPI/Swagger 规格。E3-B 实现前应以**官方 WorkBuddy/CodeBuddy 文档**复核 request/response 与 `X-Source-App` 取值（见 §23）。

---

## 5. API Key → Agent

用户只需输入 **API Key + Agent ID**。两种可行情况：

- **Case A（API Key → Agent list → Agent ID）**：仓库**无 Agent list API 证据** → 不可实现 Agent 选择器，且**不得为 UX 虚构发现 API**。
- **Case B（API Key + Agent ID → direct API）**：可行。`agentManifest.id` 在 `POST /v2/agentos/runtimes` 时作为 manifest 传入；若 Agent ID 无效，平台创建应返回错误 → 自然校验 Agent 存在性。

**结论**：采用 **Case B**。用户输入 Agent ID（手动），API Key 用于管理面 `Bearer` 鉴权。Verify 阶段 createRuntime 失败即表明 Agent ID / API Key 无效。不实现 Case A 发现器（无 API 证据）。

---

## 6. Agent Discovery

仓库无 `GET /agents`（WorkBuddy 平台侧）或 list 契约。**结论**：不提供 Agent 选择器；要求用户填写 Agent ID（Case B）。把"无发现 API"如实记为 evidence gap（§23），不臆造。

---

## 7. Runtime Creation

- **谁创建**：SteeringFlow Adapter（新增 Management API HTTP 客户端），**不再由用户在控制台外手动创建**（消除 F3/F4 的手动粘贴）。
- **何时创建**：建议 **Verify 时 lazy 创建**（非 Connect）。Connect 仅建立 Channel ↔ Agent 绑定并存 API Key；Verify 才调用 Management API 真正创建 Runtime 并探测 acpLink。
- **是否长期存在**：Runtime 创建即自带 session（step-10.1.3:12），视为持久；创建后将 `runtime_id + acpLink` 缓存进 `Connection.config`（非 status 字段）。
- **每次 Verify 是否新建**：否——复用已缓存的 `runtime_id/acpLink`；仅当缺失/失效时重建（step-12.8.6:47-50 已演示 rebuild 模式）。
- **Connection 是否保存 runtime_id**：是，但作为 `Connection.config` 的缓存值（非 Domain 字段、非 status）。
- **Runtime 与 Agent / Connection 对应**：1 Runtime ↔ 1 Connection（按 `agentManifest.id`=Agent ID 创建），与 Connection 一一对应。
- **显式 cleanup**：仓库无 delete API 证据（U）。V0 不主动 cleanup，Runtime 生命周期留平台侧；必要时 rebuild。
- **禁止提升为 ACP Domain Entity**：Runtime **不是** Core 实体——冻结决策 ⑤（step-10.1.4 §7）明确"Connection 无 health/runtime_health 字段，Runtime health 是 derived not persisted"。Runtime 仅作 `Connection.config` 缓存值存在。

---

## 8. Runtime Lifecycle

平台侧观察到：`CREATING → RUNNING`（step-12.8.6:50），且"创建即自带 session"（step-10.1.3:12）暗示创建即返回就绪 link。

```
CREATING → READY/RUNNING → (持久, 可 rebuild)
```

SteeringFlow **不建模状态机**：
- 创建在 Adapter 内同步调用 `POST /v2/agentos/runtimes`，响应即含 `acpLink`（ready）。
- 健康为**派生**（probe），不持久化为 Connection 状态（冻结决策 ⑤）。

**Connect ≠ Runtime 创建**：Connect = `Channel ↔ Agent` 绑定（Connection 记录）；Runtime 延迟到 Verify / 首次执行（lazy）。这保持 Connect 轻量、Verify 有真实含义。

---

## 9. Runtime Readiness

若平台返回 `CREATING` 而非立即可用：

- **不新建异步系统**。复用现有 `Worker` / `BackgroundTasks`（`api/runtime/channels.py:175` 异步执行模式）+ Adapter 内 lazy 创建 + 重试。
- 策略：Verify 阶段 lazy create → 若 `acpLink` 已返回则直接 probe（GET SSE 200 = ready）；若平台确需 poll，可在 Adapter 内对 `POST /v2/agentos/runtimes` 做有限重试（不引入独立 RuntimeManager——step-10.1.3:67 明确范围外）。
- 现有 `ACPAdapter.probe`（`acp.py:374-414`）已能探测 acpLink 可达性，复用为 readiness 检查。

---

## 10. Connection Semantics

当前 `Connection.config` 自由 JSON 已可容纳：

```json
{
  "provider": "workbuddy",
  "agent_id": "<Agent ID>",
  "api_key":  "<AgentOS token>",     // 已在 SENSITIVE 清单，读时掩码
  "runtime_id": "<runtime.id>",       // Verify 后缓存
  "acp_link_url": "<...>",            // Verify 后缓存
  "acp_link_token": "<...>",          // Verify 后缓存（敏感，掩码）
  "timeout_s": 60
}
```

- **保持现有 Connection 模型**（Channel ↔ Agent）。不新增 Domain Entity / abstraction。
- `api_key` / `acp_link_token` 命中 `SENSITIVE_CONNECTION_CONFIG_KEYS` → 自动脱敏（schemas/connections.py:51-54）。
- 若未来需更强约束，可在 Application 层做 config schema 归一化（§21），**不触碰 Core**。

---

## 11. Credential Boundary

API Key 完整流经：

```
frontend (密码框，不回显)
  → POST /api/connections (config.api_key)
  → backend Connection.config (JSON 列，明文落库；读时脱敏)
  → Adapter (Management API Bearer 鉴权；acpLink.token 用于通信面)
  → 绝不进入：GET 响应明文 / logs / URL / Interaction / Activity / error message / runtime env
```

- 当前 `bearer_token` 边界已被验证不泄露（M6.6-D §15）。`api_key` 同属 `SENSITIVE_CONNECTION_CONFIG_KEYS` → GET 掩码、`sanitize_connection_config` 置 `********`、写路径拒绝回写占位符（schemas/connections.py:77-92）。
- **不写入 URL**：Management API 鉴权走 `Authorization` 头（非 URL）；通信面 `acpLink.url` 仅 host 在 URL，token 在 header。
- **不进 Interaction/Activity**：`domain/interaction_record.py` 仅承载响应内容；Adapter 只写 `content/external_session_id/metadata`。
- **结论**：现有凭据边界**已覆盖** WorkBuddy API Key（存为 `config.api_key`），无需新存储模型。

---

## 12. agentManifest.secrets

Management API body 含 `agentManifest.secrets`（step-10.1.3:29）。其语义（结合 step-10.1.4 §8 实证）：该字段注入**Agent 自身的运行时密钥**（如 Agent 执行所需的 `CODEBUDDY_API_KEY`）——是 Agent 内部 concern，**非 SteeringFlow API Key**。

**核心判断（Credential Boundary）**：

- SteeringFlow **API Key（AgentOS token）** = 管理面 `Bearer` 鉴权 + 用于获取 `acpLink.token`（通信面）。**绝不应被复制到 `agentManifest.secrets`**。
- 若 Provider-native Adapter 把 API Key 注入 Runtime secrets，属**凭据重复暴露**（双份留存），违反 §11 边界。
- `agentManifest.secrets` 仅在用户**显式提供 Agent 侧密钥**时使用（V0 通常留空）。

> **这是设计规则，非当前 bug**（当前代码无任何 Runtime 创建，故无错误复制）。E3-B 必须显式遵守：API Key 只用于 Management API auth + 派生 acpLink.token，不进 secrets。

---

## 13. Execution Path

```
Interaction → AgentDispatcher → AgentAdapter → WorkBuddy Runtime → Prompt/Session → Response
```

- **Q1 当前 AgentAdapter 是否支持执行路径**：**是**——`tencent-acp-sse` binding（`acp.py:460-653`）已实现 GET SSE + POST session/prompt + 流式收尾，并经真实 wire 校准（step-12.8.6）。仅缺 Runtime 供给。
- **Q2 若 Runtime 不存在**：当前 `acp.py:472-485` 缺 `endpoint/runtime_id` 即 `AgentDispatchFailedError`（fail）。Provider-native 改为 **managed 模式**：缺 Runtime 时由 Adapter 用 API Key+Agent ID lazy 调 Management API 创建，再执行（而非直接 fail）。
- **Q3 runtime_id 硬编码/手动依赖**：当前是手动 `config["runtime_id"]` 依赖；managed 模式将其变为 **Adapter 内部惰性派生**（对用户透明）。

---

## 14. ACP Session vs Provider Session

- **ACP Session**（冻结）：`Identity.id × Connection.id × external_conversation_id`（step-10.1.4 §4）。Coordinator 全文件无 `user_id`。
- **WorkBuddy Runtime / session**：默认 `sessionId = runtime.id`（step-10.1.3:31）；`acp.py:488-494` 当 `request.external_session_id is None` 时 `session_id = str(runtime_id)`。
- **无混淆**：WorkBuddy session（`runtime.id`）经协议中性字段 `external_session_id` 进入域（`domain/agent_request.py:15,41`），不创建 `WorkBuddySession` 类型、不写 ACP Session 内部。Provider session 归属 Adapter，ACP 仅在需外部执行引用时借 `external_session_id`。✅

---

## 15. Streaming

- `AgentResponse` 当前返回**累积后的完整 content**（`acp.py:631-636`），流式块在 Adapter 内被消费、不对外暴露逐块。
- WorkBuddy `agent_message_chunk` 流式已由 Adapter 正确处理为 content。
- **对 V0 Provider-native 是否阻碍**：**否**。WeCom 等交付本就是单条响应消息；逐块 streaming 到终端用户属 optional/future，不加入 V0（符合"不为 UX 好看而加 streaming"）。

---

## 16. Response Mapping

`agent_message_chunk` / final / error → `ACPAdapter` → 通用 `AgentResponse(content, external_session_id, metadata)`。`reasoning` 通道（`agent_thought_chunk`）在 Adapter 内丢弃（step-12.8.6 结论），**不进 Session / Core domain**。✅

---

## 17. Error Mapping

现有错误模型**已足够**，不新增 taxonomy（遵循指令）：

| WorkBuddy failure | Adapter 结果 | ACP 结果 | UI |
|---|---|---|---|
| invalid API Key | Management API 401 → `AgentDispatchFailedError` | AGENT_DISPATCH_FAILED（STOP, No Delivery） | "验证未通过 / Agent 不可达" |
| Agent not found | createRuntime 4xx → `AgentDispatchFailedError` | 同上 | 同上 |
| Runtime creation failed | 同上 | 同上 | 同上 |
| Runtime not ready | lazy 重试（Worker/BackgroundTask） | 重试后成功或 fail | 同上 |
| execution timeout | `acp.py:545,640` 超时 → `AgentDispatchFailedError` | 同上 | 同上 |
| execution failed | `AgentResponseInvalidError`（content 空） | AGENT_RESPONSE_INVALID（STOP） | "Agent 未返回有效内容" |
| response stream failure | `acp.py:593,646` → `AgentDispatchFailedError` | 同上 | 同上 |

> 全部收敛为既有 `AgentDispatchFailedError` / `AgentResponseInvalidError`，符合 step-10.1.4 §7 Failure Boundary（不新增 Provider 错误码，具体 provider 错误留在 Adapter 内）。

---

## 18. Connect vs Verify

- **Connect**：建立 `Channel ↔ WorkBuddy Agent`（Agent + Connection），保存 `api_key`（凭据）。**不创建 Runtime**。
- **Verify**：用 `api_key + agent_id` 调 Management API 创建/复用 Runtime → 取 `acpLink` → probe 可达性 → **确认 Agent actually executable**。
- **职责分离依据**：Connect 仅管理绑定；Verify 才有真实执行验证意义（避免 Connect 即声称可用）。lazy 创建亦契合"Runtime 延迟到首次执行"。
- 若坚持 Connect 即创建 Runtime 亦可，但 Verify 创建更符合现有"验证=可执行性"语义且更轻量。建议 **Verify 创建**。

---

## 19. Existing API Sufficiency

- `POST /api/agents`（type=acp）、`POST /api/connections`、`GET /api/agents`、`GET /api/connections`、`GET /api/connections/{id}/health` 均已存在。
- **Scenario A（现有 API 足够）**：成立。Management API 代理放在 **Adapter 内部**（新增 HTTP 客户端），**不暴露为新的 ACP 端点**——契合"Do not expose Provider internals through generic ACP APIs"。
- **Scenario C（Management API proxy）**：满足于"在 Adapter 内部完成"，无需 `POST /api/agents/{id}/verify` 之类 Provider-specific 端点（Verify 复用现有 `/health` probe，Adapter 内 lazy create）。
- **结论**：**不新增 API 端点**；Management API 代理内聚于 Adapter。

---

## 20. Frontend Target Contract

**User-visible**：
- Provider: WorkBuddy
- Agent：选择 / 输入 Agent ID
- API Key：`********`（密码框，不回显）
- Connected（绑定建立）
- Verify（验证 Agent 可执行）

**Internal-only（first-use 不可见；仅高级区可选）**：
- `runtime_id`
- `acpLink`（url/token）
- ACP session / `external_session_id`
- Connection ID
- Adapter endpoint
- `bearer_token` / `acp_link_token`

**UX 目标**：`connect-agent.html` 的"填入 ACP 模板"（暴露 `acpLink`/`runtime_id`）改为 WorkBuddy 专用表单（仅 Agent ID + API Key），消除 F4 泄漏。

---

## 21. Minimum Implementation Boundary

```
Frontend
  ↓ WorkBuddy 连接字段（Agent ID + API Key；隐藏 runtime_id/acpLink）
API
  ↓ 现有 /api/agents（type=acp）、/api/connections、/connections/{id}/health
Application
  ↓ provider config 归一化（ConnectionApplicationService 内，可选 schema 校验）
Adapter
  ↓ 新增 WorkBuddy Management API HTTP 客户端（POST /v2/agentos/runtimes）
  ↓ 扩展 ACPAdapter 为 managed binding：API Key+Agent ID → createRuntime → 缓存 acpLink → 复用 tencent-acp-sse 执行
Core
  ↓ NO CHANGE
```

- **Core 修改？** 否。冻结决策 ⑤ 已规定 Runtime health 派生不持久化；Freeze Rule（step-10.1.4 §10）明确"后续开发进入产品层（Channel / Agent / Management UI）"——Provider-native Adapter 恰属此层。
- **架构守卫合规**：保持 `Agent(type="acp")`（决策 ③），**不创建 `WorkBuddy*` 专属类**（gate 禁用 `WorkBuddyAgent/WorkBuddyIntegration/WorkBuddyConnection` 等）。最小路径 = **扩展 `ACPAdapter`**（新增 managed binding），不新增顶层 Agent 类，`test_workbuddy_lands_as_agent_type_acp` 仍通过。
- **若误建 `WorkBuddyAdapter` 类**：`WorkBuddyAdapter` 不在 `_FORBIDDEN_CLASSES` 精确匹配集，但为尊重决策 ③ 精神，优先**扩展 ACPAdapter** 而非另立类。

---

## 22. Core Impact

**NONE。** 具体：

| 冻结不变量 | 是否被触碰 | 说明 |
|---|---|---|
| Identity Boundary | 否 | 不变 |
| Routing Boundary | 否 | 不变 |
| Session Boundary（三元隔离键） | 否 | WorkBuddy session 仍经 `external_session_id` |
| Dispatch Boundary（AgentAdapter 不实现协议） | 否 | Management API 属 Adapter 内部，不进 Dispatch |
| Agent Adapter Boundary（协议归 Adapter） | 否 | Runtime 供给归入 Adapter，恰是其职责 |
| Delivery Boundary | 否 | 不变 |
| Failure Boundary（既有 typed error） | 否 | 复用 `AgentDispatchFailedError`/`AgentResponseInvalidError` |
| Connection Status 三轴分离（决策 ⑤） | 否 | Runtime 不持久化为 status，仅缓存 config |
| Credential 归属 Connection（决策 ①④） | 否 | `api_key` 存 Connection.config，已脱敏 |

> 无 frozen invariant 被触碰，故**无需重新打开 Runtime Baseline**。

---

## 23. Risks / Evidence Gaps

1. **API 契约证据等级**：Management API 仅来自单份转录文档（`step-10.1.3`）。E3-B 前应以**官方 WorkBuddy/CodeBuddy 文档**复核 `POST /v2/agentos/runtimes` 的 request/response schema 与 `X-Source-App` 取值（当前文档未给 `X-Source-App` 具体值——`U`）。
2. **agentManifest.secrets 语义**：确认其为 Agent 自身运行时密钥（非 SteeringFlow API Key）。设计规则：API Key 不进 secrets（§12）。
3. **Runtime readiness 契约**：仓库仅观测 `RUNNING` 与"创建即自带 session"，无 `CREATING` 枚举定义。若平台返回 CREATING，需 Adapter 内 lazy 重试（现有 Worker 足够，无新异步系统）。
4. **Agent 发现 API 缺失**：无 list/GET-by-ID 平台契约 → V0 要求用户手填 Agent ID（Case B），不虚构发现器。
5. **Runtime cleanup API 缺失**：无 deleteRuntime 证据 → V0 不主动 cleanup，按需 rebuild。
6. **凭据静态加密**：当前 `Connection.config` 明文 JSON 落库、无静态加密（M6.6-D §18）。Provider-native 沿用既有脱敏+RLS 保护，不引入 Vault（决策 ④）。

---

## 24. Non-Goals（不提前实现）

本轮**不**：
- 实现 WorkBuddy Adapter / Management API 客户端
- 修改 frontend / Agent model / Connection model
- 增加 Runtime Domain model / migration
- 增加 API 端点
- 新增 `WorkBuddy*` 专属类（gate 禁用）
- 把 Runtime 提升为 ACP Domain Entity
- 为 V0 加 streaming 到终端用户
- 保存 API Key 新字段（复用 `config.api_key` 既有的脱敏机制）

仅输出设计结论。

---

## 25. Final Gate

**CONDITIONAL PASS**

判定依据：

- **ACP Core + Provider Adapter boundary 足以支撑 Provider-native WorkBuddy integration**（Layer 1 PASS；M6.6-D 已确认无 Core 污染，本轮维持）。通信面（`tencent-acp-sse` binding）已实现并真实校准；缺失的仅是**管理面 Runtime 创建客户端**，纯属 Adapter 层。
- **Connection 模型足够**：`Connection.config` 自由 JSON + `SENSITIVE` 脱敏已覆盖 `api_key`/`acp_link_token`；Runtime 仅作 config 缓存值，不提升为 Domain Entity（尊重冻结决策 ⑤）。
- **架构守卫已对齐**：决策 ③（WorkBuddy=`Agent(type="acp")`）、④（凭据归 Connection.config）、⑤（Runtime health 派生不持久化）共同确保最小路径 = 扩展 `ACPAdapter` + 内聚 Management API 客户端，**Core NO CHANGE**。
- **现有 API 足够**：Management API 代理内聚于 Adapter，不新增 ACP 端点（Scenario A/C 成立）。

**明确架构前置条件（CONDITIONAL 来源）**，须在 E3-B 实现前/中裁定：

1. **Credential Boundary 规则**：SteeringFlow API Key 仅用于 Management API `Bearer` 鉴权 + 派生 `acpLink.token`，**绝不注入 `agentManifest.secrets`**（secrets 仅承载用户显式提供的 Agent 侧密钥）。
2. **Runtime lifecycle 归属**：lazy 创建于 Verify（非 Connect）；缓存入 `Connection.config`（非 status）；健康派生（probe）；无 Runtime Domain Entity、无新异步状态机。
3. **官方 API 复核**：以官方 WorkBuddy/CodeBuddy 文档确认 `POST /v2/agentos/runtimes` schema 与 `X-Source-App` 取值（当前仅单份转录文档）。

> 无结构性冲突（WorkBuddy API 能力与 ACP Core/Adapter boundary 可对齐），故**非 BLOCKED**。上述条件均为"实现层可裁定的设计规则"，不阻塞核心；完成后即进入有限实现批次（M6.6-E3-B）。

---

*本报告为只读可行性审计，未修改任何代码 / migration / 测试 / ADR / config / 生产；未创建真实 Runtime / Agent；未 commit / tag。唯一新增物：本文件。*
