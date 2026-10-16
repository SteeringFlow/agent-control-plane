# M6.6-E3-C — WorkBuddy Official API Contract & Real E2E Verification

> 前序：
> - `M6.6-D Provider Adapter & Real First Success Audit` — CONDITIONAL PASS（F3/F4 = WorkBuddy GAP）
> - `M6.6-E3-A WorkBuddy Provider-native Feasibility Audit` — CONDITIONAL PASS
> - `M6.6-E3-B WorkBuddy Provider-native Adapter Implementation` — CONDITIONAL PASS（剩 `X-Source-App` 取值未确认 + Management API schema 待官方复核 + 无真实凭据）
>
> 本轮 **E3-C** 只做两件事（严格只读 + 文档）：
> 1. **官方契约核验**：以 WorkBuddy / CodeBuddy 官方公开文档复核 E3-B 实现的 Management API 契约；
> 2. **真实 E2E**（若真实凭据可用）。
>
> **本轮唯一新增物：本文件。** 未修改 backend / frontend / migration / tests / config；未创建真实 Runtime / Agent；未 commit / tag。发现任何官方与实现的差异均**只记录、不修正**（见 §22 纪律）。

---

## 1. Scope

验证对象（E3-B 实现的契约面，全部停在 Adapter 边界内）：

- `POST /v2/agentos/runtimes`（Management API 客户端：`backend/app/adapters/agents/workbuddy.py`）；
- `POST /api/connections/{id}/verify`（lazy Runtime provisioning 入口：`backend/app/api/management/connections.py`）；
- 既有 `tencent-acp-sse` 通信路径（`acp.py`）的复用。

**核验维度**（对应任务 5–13）：

| 维度 | 官方来源 | 仓库 wire evidence | 当前实现 |
|---|---|---|---|
| Management API 契约 | 公开官方文档 | `step-10.1.3` / `step-12.8.6` | `workbuddy.py` |
| `X-Source-App` | 公开官方文档（搜索） | `step-10.1.3`（提及但未给值） | `workbuddy.py`（可选、不猜） |
| Runtime Request Schema | 公开官方文档 | `step-10.1.3` | `workbuddy.py` |
| Credential Boundary | 公开官方文档 | E3-A §12 | `workbuddy.py` |
| Runtime Response Schema | 公开官方文档 | `step-10.1.3` | `workbuddy.py._parse_runtime_response` |
| Runtime Readiness | 公开官方文档 | `step-12.8.6`（RUNNING） | `workbuddy.py`（probe-based） |
| Runtime Lifecycle | 公开官方文档 | `step-12.8.6`（rebuild） | `workbuddy.py`（reuse） |
| ACP Link Contract | 公开官方文档 | `step-10.1.3` / `step-12.8.6` | `acp.py` |
| SSE Contract | 公开官方文档 | `step-12.8.6`（真实事件） | `acp.py` |

**本轮默认禁止**（已遵守，零代码改动）：修改 ACP Core / Domain Model / migration / Runtime Entity / Bootstrap / Session / Authorization / Worker / Delivery / 既有 E3-B 实现。

---

## 2. Evidence Sources

### 2.1 官方公开来源（本轮新取，按优先级）

| # | 来源 | 类型 | 与契约相关度 |
|---|---|---|---|
| O1 | `https://cloud.tencent.com/edu/learning/course-4659` —《Codebuddy CloudAgent：Harness 驱动的云端托管智能体》（腾讯云培训认证） | 架构 + 示例级 API 说明 | **高**：明确给出 `agentManifest`、`acpLink`、`ACP` 协议形态 |
| O2 | `https://cloud.tencent.com/product/workbuddy-enterprise` — WorkBuddy Enterprise 产品页 | 产品着陆页 | 低：仅指向 `document/product/1831`，无 API 细节 |
| O3 | `https://cloud.tencent.com/document/product/1831` — WorkBuddy Enterprise 文档中心 | 文档目录页 | 低：「API 文档」链接为 `javascript:void(0)`，无技术参数 |
| O4 | `https://blog.csdn.net/weixin_29531177/article/details/165200866` —《WorkBuddy 开放平台 Agent 开发实战：从 API 接入到应用落地》 | 第三方实战（部分付费墙） | 中：公开段给出开放平台 `AppKey`/`AppSecret` 凭证模型 |
| O5 | `https://open.workbuddy.cn/` — WorkBuddy 开放平台入口 | 平台着陆页 | 低：无深层 API 参考 |

> **官方来源局限声明**：WorkBuddy / CodeBuddy 的 **AgentOS Management API 级参考文档（含 `X-Source-App` 取值、`POST /v2/agentos/runtimes` 的精确参数表）在公开网络未检索到**。最接近的是 O1（CloudAgent 培训课程，描述**通用** CloudAgent 控制面 `POST /runtimes`，并非 WorkBuddy 管理后台的精确路由），以及开放平台的 `AppKey/AppSecret` 模型（O4）。因此本核验对 `X-Source-App` 与精确路由**如实标记为 unresolved / 有差异**，不臆造官方保证。

### 2.2 仓库 wire evidence（实现侧事实）

| # | 文件 | 内容 |
|---|---|---|
| W1 | `docs/step-10.1.3-tencent-acp-wire.md` | 转录自「腾讯管理后台 API 集成说明」：`POST https://www.codebuddy.cn/v2/agentos/runtimes`、header、`agentManifest`、`acpLink`、ACP 协议 |
| W2 | `docs/step-12.8.6-real-acp-wire-classification-smoke.md` | **真实 wire**：经管理面 `POST /v2/agentos/runtimes` 重建 Runtime；无效 body 探测返回 **400 而非 401**；真实 SSE 事件形状 |
| W3 | `backend/app/adapters/agents/workbuddy.py` | E3-B Management API 客户端 + lazy provisioner（实现） |
| W4 | `backend/app/api/management/connections.py` | `POST /{id}/verify` 端点（实现） |
| W5 | `backend/tests/test_m6_6_e3_b_workbuddy_provider_native.py` | E3-B T1–T12 契约/行为测试（**本轮复跑：18 passed**） |

### 2.3 实现侧快照（复跑确认）

```
cd backend && python -m pytest tests/test_m6_6_e3_b_workbuddy_provider_native.py -q --basetemp=%TEMP%\wb-e3c-pytest
=> 18 passed, 0 failed
```

---

## 3. Official Management API Contract

### 3.1 端点（`POST /v2/agentos/runtimes`）

| 项 | 官方说法 | 仓库 wire（W1/W2） | 当前实现（W3） | 判定 |
|---|---|---|---|---|
| base URL | 未公开精确值 | `https://www.codebuddy.cn`（W1） | `DEFAULT_MANAGEMENT_BASE_URL="https://www.codebuddy.cn"` | PASS（wire 一致） |
| path | O1 通用 CloudAgent 写 `POST /runtimes`；**WorkBuddy 管理后台精确路由未在公开官方文档出现** | `POST /v2/agentos/runtimes`（W1/W2 实测调用成功） | `RUNTIME_CREATE_PATH="/v2/agentos/runtimes"` | **Gap（路由差异，非冲突）**：实现采用真实 WorkBuddy 管理后台路由（W2 实测有效）；O1 的 `/runtimes` 是通用 CloudAgent 控制面表述，二者非同一产品精确契约 |
| method | 创建 = POST | POST | POST | PASS |
| `Authorization` | 公开文档**未给出** HTTP 头规范 | W1 写 `Authorization: Bearer <AgentOS token>`；W2 实测 无效 body→400（说明鉴权边界存在） | `headers["Authorization"]=f"Bearer {api_key}"` | PASS（wire 一致；官方未公开但实现与 wire 一致） |
| `X-Source-App` | **未在任何公开官方来源出现**（见 §4） | W1 列出该头但**未给取值** | 可选：仅 `config["source_app"]` / `WORKBUDDY_SOURCE_APP` 提供时发送；缺省不发送（`test_t3b`） | **Gap（unresolved external contract）**：实现不猜生产值 |
| `Content-Type` | 未公开 | 隐含 `application/json` | `application/json` | PASS |
| `Accept` | 未公开 | 隐含 | `application/json` | PASS（额外约束，无害） |

### 3.2 request body

```json
{
  "runtimeName": "<string>",
  "agentManifest": {
    "id": "<Agent ID>",
    "name": "<runtimeName>",
    "manifestVersion": "1.0"
  }
}
```

- 实现**不含 `secrets`**（T3：`test_t3_api_key_never_in_agent_manifest_secrets`）。
- 字段顺序与 W1 一致（`runtimeName` + `agentManifest{id,name,manifestVersion}`）。

### 3.3 response body

```json
{ "data": { "id": "<runtime.id>", "links": { "acpLink": { "url": "<acp url>", "token": "<acp token>" } } } }
```

- 实现解析 `data.id` / `data.links.acpLink.url` / `data.links.acpLink.token`（`_parse_runtime_response`）。
- **官方 O1 额外字段 `tokenExpiresAt`（Unix 时间戳）**：实现**未解析/未使用** → 见 §7 / §20 Finding F2。

### 3.4 error response

- 实现：`status not in (200,201)` → `WorkBuddyManagementError`（只带状态码/类型名，绝不带 key/token/URL）。
- Wire（W2）：无效 body → **400**（非 401），证明鉴权头与 body 校验分离；401 对应无效 API Key（实现 `test_t10` 用 401 验证 fail-closed）。
- 官方公开文档**未给出** Management API 错误码表 → 错误契约以 **wire evidence** 为准，不写成官方公开保证。

---

## 4. X-Source-App

**结论：unresolved external contract（公开官方文档未确认取值）。**

### 4.1 问题

任务要求明确：官方要求的 `X-Source-App` 到底是什么？是否 required？格式？是否预注册？是否与 Open Platform Application Name / Client ID 相关？

### 4.2 证据

| 来源 | 是否提及 `X-Source-App` | 相关信息 |
|---|---|---|
| O1 CloudAgent 培训 | ❌ 全文未出现 | 仅抽象安全概念（权限中间件、Webhook 签名） |
| O2/O3 WorkBuddy 文档 | ❌ 未出现 | 无具体 HTTP Header 规范 |
| O4 WorkBuddy 开放平台实战 | ❌ 未出现 `X-Source-App` | 开放平台凭证模型为 **`AppKey`（应用唯一标识）+ `AppSecret`（签名密钥）**，环境变量 `WORKBUDDY_APP_KEY` / `WORKBUDDY_APP_SECRET` |
| O5 开放平台入口 | ❌ 未出现 | — |
| W1 仓库转录 | ⚠️ 列出 `X-Source-App` 头，但**未给取值** | 仅写「headers：`Authorization: Bearer <AgentOS token>` + `X-Source-App`」 |

### 4.3 研判（不臆造官方保证）

- 公开官方文档**没有任何一处**定义 `X-Source-App` 的取值规则 / 是否 required / 格式 / 是否预注册。
- O4 显示 WorkBuddy 开放平台使用 `AppKey` 作为「应用唯一标识」。这**可能**与 `X-Source-App`（source application 标识）语义相近，但：
  1. O4 的 `AppKey/AppSecret` 属于**开放平台会话 API** 凭证体系；
  2. `X-Source-App` 出现在 **AgentOS Management API**（W1）上下文；
  3. 二者是否同一概念**无公开官方文档佐证** → 不可下结论。
- **不把 Client ID / Application Name / Agent ID / API Key 混为 `X-Source-App`**（任务明确警告）。

### 4.4 实现纪律（正确，符合任务 §22）

`workbuddy.py` 仅在该头被显式提供（`Connection.config["source_app"]` 或 `WORKBUDDY_SOURCE_APP`）时发送，缺省**不发送**（`test_t3b_source_app_header_only_when_configured`）。即：

- 不猜生产值；
- 不阻断：若官方未来确认该头为 required 且固定值，仅需配置 `WORKBUDDY_SOURCE_APP`，**无需改代码**。

> **记录为 unresolved**：`X-Source-App` 取值待 WorkBuddy / CodeBuddy 官方 AgentOS API 参考确认。

---

## 5. Runtime Request Schema

逐字段（官方 O1 + wire W1 + 实现 W3）：

| Field | Required | Official Meaning（O1/W1） | Current Implementation | 判定 |
|---|---|---|---|---|
| `runtimeName` | 是 | Runtime 展示名（O1 示例 `code-reviewer`） | `runtimeName = f"steeringflow-conn{conn}-agent{agent_id}"`（稳定、不含密钥） | PASS |
| `agentManifest.id` | 是 | Agent 实例标识（O1 `reviewer-v2`） | `agentManifest["id"] = agent_id`（用户提供的 WorkBuddy Agent ID） | PASS |
| `agentManifest.name` | 是（W1 列出） | Agent 名称 | `agentManifest["name"] = runtime_name` | PASS（W1 列出；O1 亦列 `name`） |
| `agentManifest.manifestVersion` | 是（W1 列出） | manifest 版本 | 固定 `"1.0"` | PASS（W1 写 `manifestVersion:"1.0"`） |
| `agentManifest.secrets` | 可选 | **Agent 自身运行时密钥**（O1 示例 `CODEBUDDY_API_KEY` / `GITHUB_TOKEN`，且明确「凭证不落地，auth-proxy 动态注入，Agent 进程不可见」）；O1 显示为 **`[{key,value}]` 数组** | **不发送**（T3 断言请求体无 `secrets`、无 API Key） | PASS（见 §6） |

> **官方确认的重要性**：O1 明确 `secrets` 是 `{key,value}` 数组、承载 **Agent 侧** 密钥（如 `CODEBUDDY_API_KEY`），并强调「凭证不落地」。这**正面支持** E3-A §12 的设计规则：SteeringFlow **API Key（AgentOS token）绝不属于 `secrets`**。

---

## 6. Credential Contract

**结论：PASS（官方 O1 + wire W2 + 实现 W3 一致）。**

| 概念 | 语义边界 | 官方支持 |
|---|---|---|
| SteeringFlow **API Key**（AgentOS token） | 仅用于 Management API `Authorization: Bearer`（管理面） | W1 明示；W2 实测 400/401 边界 | ✅ |
| `acpLink.token` | 仅用于 ACP 通信面 `Authorization: Bearer`（GET/POST `/acp`） | W1、O1（`acpLink.token`） | ✅ |
| `agentManifest.secrets` | 仅承载 **Agent 自身** 运行时密钥 | O1 明示 `{key,value}` 数组 + 「凭证不落地」 | ✅ |

**核心判定（Credential Boundary 正式关闭）**：

1. 官方 O1 证实 `secrets` 是 Agent 侧密钥数组，与 SteeringFlow 管理面 API Key 是两回事；
2. 实现 `workbuddy.py` 把 API Key 仅用于 Management API auth（`create_runtime` 的 `Authorization` 头），**绝不写入 `agentManifest.secrets`**（T3）；
3. `acpLink.token` 缓存为 `Connection.config["bearer_token"]`（命中既有 `SENSITIVE_CONNECTION_CONFIG_KEYS`，读时 `********`）；
4. 二者均不写日志（仅失败类型名）、不进 URL、不进 Interaction/Activity、不进普通 GET 响应（T2/T8）。

> E3-B 的 Credential Contract 在官方文档层面获得**正向支持**，无冲突。

---

## 7. Runtime Response Schema

| Field | Guaranteed? | Official（O1） | Wire（W1） | Implementation（W3） | 判定 |
|---|---|---|---|---|---|
| `data.id` | 必需 | 隐含（runtime id） | W1 列 `data.id` | 解析为 `runtime_id`，缺失即 fail-closed | PASS |
| `data.links.acpLink.url` | 必需 | O1 列 `url` | W1 列 | 解析，非 http(s) 即 fail-closed | PASS |
| `data.links.acpLink.token` | 必需 | O1 列 `token` | W1 列 | 解析，空即 fail-closed | PASS |
| `data.links.acpLink.tokenExpiresAt` | **optional** | O1 **明确列出**（Unix 时间戳） | W1 **未列** | **未解析/未使用** | **Gap（F2，非冲突）** |

**重要纪律（任务 §9）**：不把 transcript 中出现过的字段自动当成 guaranteed contract。

- `url` / `token`：W1（转录）+ W2（真实调用）双源均出现 → 视为 wire-guaranteed；实现按必需处理，符合。
- `tokenExpiresAt`：**仅 O1 公开文档列出**，仓库 wire（W1/W2）未出现；实现未消费。
  - 这不是「实现错误」——wire 返回里可能不含该字段；
  - 但官方 O1 表明该字段**可能**存在 → 实现当前**不感知 token 过期**，属剩余条件（§21 R2）。

> 不要把 `tokenExpiresAt` 当作 guaranteed，但记录其官方存在性供后续（若官方确认则可在 Adapter 内加过期感知，**不在本轮修改**）。

---

## 8. Runtime Readiness

**结论：PASS（wire 一致；无官方异步 readiness 契约冲突）。**

- 官方 O1 未定义 `CREATING → READY` 轮询契约；O1 仅说「创建 Runtime 后响应返回 `acpLink`」。
- Wire W1：「Runtime 创建即自带 session」；W2 实测重建后 Runtime 为 `RUNNING`，且返回的 `acpLink` 立即可用于真实 SSE。
- 实现：Verify 调 `create_runtime` → 拿到 `acpLink` → 立即 `ACPAdapter.probe`（GET SSE 200 = ready）。即**假设创建即返回可用 link（lazy readiness by probe）**。
- 与官方无冲突：O1/W1/W2 均显示创建即返回 acpLink；若平台偶发 `CREATING`，实现当前无轮询重试（E3-A §9 已记录此边界，复用既有 Worker/BackgroundTasks，本轮不改）。
- 任务 §8 选项（polling/callback/wait/retry/lazy）：当前采用 **lazy readiness（probe-based）**，与 wire 实证一致。

---

## 9. Runtime Lifecycle

**结论：PASS（复用语义与 wire 一致）。**

| 语义 | 官方/ wire | 实现（W3） | 判定 |
|---|---|---|---|
| 长期存在 | W2 重建模式（旧 Runtime 过期后重建新 Runtime）显示 Runtime 可长期存在、可重建 | 缓存入 `Connection.config`，不主动 delete | PASS |
| 可复用 | W2 rebuild（按需重建，非每次新建） | Verify 二次复用同一 Runtime（T5/T6：`transport.calls==1`） | PASS |
| 与 Agent 绑定 | `agentManifest.id` = Agent ID | `agentManifest["id"]=agent_id` | PASS |
| 与 Runtime Name 绑定 | `runtimeName` 稳定 | `f"steeringflow-conn{conn}-agent{agent_id}"` 稳定 | PASS |
| 可重新连接 | W2 重建 | 缓存失效时 Verify 重建（fail-closed 不盲目重建） | PASS |
| 可删除 | 仓库无 delete 契约（U） | 不主动 delete | PASS（V0 留平台侧） |
| 可重新创建 | W2 rebuild | `_create` 仅在无缓存时调用 | PASS |

**→ Runtime A（Verify #1） = R1；Runtime A（Verify #2） = R1**：实现 T5/T6 实测 `transport.calls == 1`，确认**不无意义创建第二 Runtime**（见 §14）。

---

## 10. ACP Link Contract

**结论：PASS（官方 O1 + wire W1/W2 + 实现 `acp.py` 一致）。**

| 问 | 官方（O1）/ wire（W1/W2） | 实现（`acp.py`） | 判定 |
|---|---|---|---|
| token 是否用于 SSE | O1：`acpLink.token` 作 ACP 认证；W1：`GET {url} Authorization: Bearer <acpLink.token>` | `acp.py` GET SSE 带 `Authorization: Bearer <bearer_token>` | PASS |
| token 是否用于 session creation | O1：`POST /acp` 带同头发 `session/new` / `session/prompt` | `acp.py` POST jsonrpc `session/prompt` 带同头 | PASS |
| URL 是否直接作为 ACP endpoint | O1：`url` 即 `/acp` 端点；W1 GET/POST `{url}` | `endpoint = config["endpoint"]` | PASS |
| 是否 expiration | O1 列 `tokenExpiresAt`（见 §7） | **未感知**（F2） | Gap（非冲突） |
| 是否需要额外 Authorization header | O1/W1：仅需 `Bearer <acpLink.token>`（+ `Acp-Connection-Id`） | 一致 | PASS |
| 是否与 Runtime ID 一一对应 | W1：默认 `sessionId = runtime.id` | `external_session_id` 缺省 = `runtime_id` | PASS |

> ACP Link 契约在官方 O1（GET/POST `/acp` + JSON-RPC 2.0 + `session/prompt` + SSE）与仓库 wire（W1/W2）双重印证；实现 `acp.py` 的 `tencent-acp-sse` binding 直接消费，未重实现。

---

## 11. SSE Contract

**结论：PASS（官方 O1 + 真实 wire W2 一致；真实 wire 作为 evidence，非官方公开保证）。**

| 阶段 | 官方（O1）/ wire（W1/W2） | 实现（`acp.py`） | 判定 |
|---|---|---|---|
| session creation | O1 `session/new` / `session/prompt` | `session/prompt`（`sessionId` + `prompt`） | PASS |
| prompt | O1 `POST /acp` jsonrpc `session/prompt` | 一致 | PASS |
| stream | W1/W2：`session/update` → `agent_message_chunk` → `content.text` | `acp.py` 递归收集 `type=="text"` | PASS |
| final stop | W1 `codebuddy.ai.isFinalStop`；W2 实测 `session/endTurn` + `_meta.codebuddy.ai.isFinalStop=true` | `acp.py` 递归检测 `isFinalStop` / `turn_finished` | PASS |
| error | W2 `jsonrpc error{code,message}`；`stopReason:"refusal"` | `acp.py` → `AgentDispatchFailedError` / `AgentResponseInvalidError` | PASS |

**纪律（任务 §11）**：官方 O1 公开了 ACP 协议形态（JSON-RPC 2.0 + HTTP/SSE），但未逐字公开 `agent_message_chunk` / `isFinalStop` 等事件名；这些事件名来自**真实 wire（W2）**。本核验把真实 wire 作为 evidence，不写成官方公开 API guarantee。

> 既有真实测试 `test_step12_8_6_acp_wire_reasoning_live_smoke.py` / `test_step10_1_3_tencent_acp_wire.py` 已用真实 acpLink 跑通（env 门控）。

---

## 12. Real Credential Availability

**结论：真实 WorkBuddy 凭据不可用 → Real E2E 无法执行。**

检查 `backend/.env`（仅做存在性检查，绝不输出 secret 内容）：

| 所需凭据 | 是否存在 | 说明 |
|---|---|---|
| WorkBuddy **Agent ID** | ❌ 无 | 仓库无任何 `workbuddy_agent_id` / 真实 Agent ID 落库 |
| WorkBuddy **AgentOS API Key**（Management API `Bearer`） | ❌ 无 | `.env` 无 `WORKBUDDY_*` / `AGENTOS_*` API Key；全仓搜索仅见 `TENCENT_ACP_*`（通信面） |
| 真实 **Runtime acpLink**（url+token） | ⚠️ 陈旧/过期 | `.env` 含 `TENCENT_ACP_URL` + `TENCENT_ACP_TOKEN`（真实 acpLink 三值之二）；**但该 JWT `exp` 已早于当前日期（约 2026-09-10 过期）**，且**无 `TENCENT_RUNTIME_ID`** |
| `TENCENT_RUNTIME_ID` | ❌ 无 | `.env` 未设置；live 测试门控三值不全 |

**判定**：

- Management API 路径（Connect → Verify → `POST /v2/agentos/runtimes` → 创建 Runtime）**缺少 AgentOS API Key + Agent ID**，无法发起；
- 即便想做下游真实 SSE 验证，缓存 acpLink token 已过期且缺 `runtime_id`，亦无法独立执行；
- 因此 **Real E2E = NOT VERIFIED**（非代码缺口，是凭据缺口）。

> 不伪造真实 E2E、不以静态/桩测试声称 PASS（遵守 E3-B §15 纪律）。

---

## 13. Real E2E

**结论：NOT VERIFIED（无真实凭据）。**

因 §12 凭据缺口，E1–E7 均无法在真实环境执行：

| # | 验收项 | 状态 |
|---|---|---|
| E1 | Connect 成功 | 未执行（无 Agent ID / API Key 构造真实连接） |
| E2 | Connect 不创建 Runtime | 未执行（由 T4 桩测试间接证明，非真实） |
| E3 | 第一次 Verify 创建 Runtime | 未执行（无 API Key 调真实 Management API） |
| E4 | Verify 最终成功 | 未执行 |
| E5 | 第二次 Verify 不无意义创建第二 Runtime | 未执行（由 T5/T6 桩测试间接证明） |
| E6 | 真实 Agent execution 成功 | 未执行（无有效 acpLink） |
| E7 | response 正确回到 SteeringFlow | 未执行 |

> 真实 E2E 待凭据就绪后重跑（无需改代码；装配 `WORKBUDDY_API_KEY` + `workbuddy_agent_id` + 有效 acpLink 即可）。

---

## 14. Runtime Reuse

**结论：实现层复用已验证（桩测试 T5/T6）；真实复用证据 N/A。**

- 实现：`WorkBuddyProvisioner.verify` 在 `Connection.config` 已有完整 `endpoint+bearer_token+runtime_id` 时**只探针、不重建**（`_cached_acp` 命中即返回 `updated=None`）。
- 桩测试 `test_t5_and_t6_verify_creates_then_reuses_runtime`：Verify #1 → `transport.calls==1`；Verify #2 → `transport.calls==1`（**未创建第二 Runtime**）。
- Fail closed：`test_t7c_cached_unreachable_is_not_recreated`——已缓存但不可达 → 不重复创建。
- 真实证据：因 §12 无凭据，**未采集真实 Runtime 复用证据**（R1=R1 仅以桩断言体现）。

---

## 15. Failure Path

**结论：真实失败场景未执行（无凭据、且不应破坏共享资源）；实现 fail-closed 已由桩测试覆盖。**

- 真实失败测试（如无效 API Key → 401、无效 Agent ID → 4xx、body 非法 → 400）**未执行**（无凭据；且任务 §16 警告「不要为测试错误破坏真实生产 Agent」）。
- 实现 fail-closed 路径（桩测试覆盖）：
  - `test_t7_runtime_response_parsing_is_fail_closed`：残缺响应（非 JSON / 缺 id / 缺 acpLink / 缺 token）→ `WorkBuddyManagementError`；
  - `test_t7b_management_failure_is_fail_closed_no_write`：传输错误 → `reachable=False` + 无缓存写入；
  - `test_t7c_cached_unreachable_is_not_recreated`：已缓存不可达 → 不重建；
  - `test_t10_provider_failure_maps_to_existing_error_model`：401 → 快照不可达，无新错误码。
- Wire（W2）实测：无效 body → **400**（非 401），证明实现把 400/401 统一收口为 fail-closed 与 wire 一致。

---

## 16. Credential Leakage Verification

**结论：PASS（静态搜索 + 桩测试 T8 双重确认；未输出任何 secret 内容）。**

- **静态检查**（仅搜索、不打印 secret）：
  - `backend/.env` 含真实 acpLink token，但**不含** Management API API Key / Agent ID；
  - `workbuddy.py` 中 `api_key` 仅出现在 `Authorization` 头构造；错误消息只含 `type(exc).__name__` / 状态码，**无 key/token/URL 拼接**（`_default_transport` 异常、`WorkBuddyManagementError`）；
  - `sanitize_connection_config` 对 `api_key` / `bearer_token` 返回 `********`（T2）。
- **HTTP 响应**（T8 `test_t8_verify_never_leaks_runtime_token_in_http_response`）：
  - Verify 响应与后续 GET 详情均**不含** `_API_TOKEN` / `_API_KEY`；
  - 返回 `config["bearer_token"]=="********"`、`config["api_key"]=="********"`。
- **Activity / Interaction / Connection GET / error output**：凭据仅存 `Connection.config`（读时脱敏），不进 Interaction content、不进 Activity、不进 error message（E3-B §5/§11）。

> 本报告正文未粘贴任何真实 token / API Key / `TENCENT_ACP_TOKEN` 值。

---

## 17. Contract Matrix

| Contract | Official | Wire Evidence | Current Implementation | Result |
|---|---|---|---|---|
| Auth（`Authorization: Bearer`） | 未公开头规范 | W1/W2 一致 | `Bearer <api_key>` | **PASS** |
| `X-Source-App` | **未公开** | W1 列出无值 | 可选、不猜 | **Gap（unresolved）** |
| Runtime endpoint | O1 `/runtimes`；**WorkBuddy 路由未公开** | W1/W2 `/v2/agentos/runtimes`（实测） | `/v2/agentos/runtimes` | **Gap（路由差异，非冲突）** |
| Request schema | O1 `agentManifest{id,name,manifestVersion,secrets[]}` | W1 一致（不含 secrets） | 一致（不含 secrets） | **PASS** |
| Response schema | O1 `data.id`/`acpLink.url`/`token`/`tokenExpiresAt` | W1 `id`/`url`/`token` | 解析 `id`/`url`/`token` | **PASS**（缺 `tokenExpiresAt` → 见 F2） |
| Runtime readiness | O1 创建即返回 acpLink | W1 创建即带 session；W2 RUNNING | probe-based lazy | **PASS** |
| ACP Link | O1 token/url/endpoint 一致 | W1/W2 一致 | `acp.py` 复用 | **PASS** |
| SSE | O1 JSON-RPC+SSE 形态 | W2 真实事件 | `acp.py` 复用 | **PASS** |
| Runtime reuse | W2 rebuild 模式 | W2 同一 Runtime 复用 | T5/T6 不重建 | **PASS** |
| Credential Boundary | O1 `secrets`=Agent 侧、凭证不落地 | W1/W2 一致 | API Key≠secrets；均脱敏 | **PASS** |

> 仅 `X-Source-App` 与 endpoint 精确路由为 **Gap**；其余全部 PASS。Gap 均为「公开官方文档缺失/表述差异」，**非实现与 wire 冲突**。

---

## 18. E2E Matrix

| # | 验收项 | 真实执行 | 桩/实现佐证 |
|---|---|---|---|
| E1 | Connect 成功 | NOT VERIFIED | T4（桩） |
| E2 | Connect 不创建 Runtime | NOT VERIFIED | `test_t4_connect_does_not_create_runtime` |
| E3 | 第一次 Verify 创建 Runtime | NOT VERIFIED | `test_t5_and_t6_verify_creates_then_reuses_runtime` |
| E4 | Verify 最终成功 | NOT VERIFIED | 同上 |
| E5 | 二次 Verify 不重复创建 | NOT VERIFIED | 同上（`calls==1`） |
| E6 | 真实 Agent execution | NOT VERIFIED | `test_step12_8_6`（需有效 acpLink，当前过期） |
| E7 | response 回 SteeringFlow | NOT VERIFIED | `acp.py` 复用既有路径 |

**Real E2E Gate = NOT VERIFIED**（凭据缺口，非代码缺口）。

---

## 19. Scope Integrity

**结论：本轮零代码改动，Scope 完整。**

- 唯一新增物：本文件 `docs/M6_6_E3_C_*.md`；
- 未修改：`application/` / `domain/` / `persistence/` / migration / ACP Core（`acp.py` 本体）/ Bootstrap / Session / Authorization / Worker / Delivery / 既有 E3-B 实现；
- 未创建 Domain Entity（`WorkBuddy*`）/ Runtime Entity / 新错误码；
- 未触网发起真实 Management API 调用（凭据缺失，亦未尝试）；
- 未创建临时测试数据（无需；桩测试沿用既有 harness）。

> 若后续真实 E2E 需临时数据，应在凭据就绪后单独记录并清理（本轮未触发）。

---

## 20. Findings

| ID | 发现 | 类型 | 是否代码冲突 |
|---|---|---|---|
| F1 | `X-Source-App` 取值在公开官方文档**完全缺失**；WorkBuddy 开放平台用 `AppKey/AppSecret`（O4），语义可能相近但**无官方佐证**用于 AgentOS Management API | Gap / unresolved | 否（实现可选、不猜） |
| F2 | 官方 O1 明确 `acpLink.tokenExpiresAt`（Unix 时间戳），实现未解析/未感知 token 过期 | Gap（观察） | 否（wire 可能不含该字段） |
| F3 | 官方 O1 通用 CloudAgent 写 `POST /runtimes`；真实 WorkBuddy 管理后台路由为 `POST /v2/agentos/runtimes`（W1/W2 实测） | 路由差异 | 否（实现=真实 WorkBuddy 路由，wire 实测有效） |
| F4 | 官方 O1 确认 `agentManifest.secrets` 为 `{key,value}` 数组、承载 **Agent 自身** 密钥、且「凭证不落地」 | 正向支持 | 否（证实 E3-A §12 设计正确） |
| F5 | Wire W2 实测：无效 body → **400**（非 401），证明鉴权与 body 校验分离 | 正向支持 | 否（实现 fail-closed 兼容） |
| F6 | `.env` 真实 acpLink token 已过期（约 2026-09-10）且无 `TENCENT_RUNTIME_ID`；全仓无 AgentOS API Key / Agent ID | 凭据缺口 | 否（E2E 阻塞原因） |

> 所有 Finding 均为**记录项**，未触发 E3-B 修改（遵守 §22 纪律）。F2/F3 若官方后续确认，可在 Adapter 内以最小改动闭环，**不在本轮**。

---

## 21. Remaining Conditions

1. **R1 — `X-Source-App` 官方取值**：待 WorkBuddy / CodeBuddy 官方 AgentOS API 参考确认是否为 required、取值规则、是否与开放平台 `AppKey` 同源。确认后仅需配置 `WORKBUDDY_SOURCE_APP`（无需改代码）。
2. **R2 — `acpLink.tokenExpiresAt` 处理**：若官方确认该字段稳定返回，建议在 `WorkBuddyManagementClient._parse_runtime_response` 内捕获并在 Provisioner 做过期感知（Adapter 内最小改动，非 Core）。当前不阻断。
3. **R3 — Management API 精确路由**：实现采用真实 WorkBuddy 管理后台 `/v2/agentos/runtimes`（wire 实测有效）；若官方发布精确参考与wire 冲突，再裁定（当前无冲突）。
4. **R4 — 真实凭据**：需合法 WorkBuddy AgentOS API Key + Agent ID（+ 有效 acpLink）方可执行 Real E2E。

---

## 22. 重要纪律（已遵守）

- 发现 F1/F2/F3（官方与实现/通用文档的差异）后，**只记录、未修改 E3-B**；
- 未因「官方 `secrets` 为数组」「官方多 `tokenExpiresAt`」等差异而擅自改动实现；
- 真实 E2E 因凭据缺失判 NOT VERIFIED，未伪造 PASS。

---

## 23. Final Gate

### Contract Gate：**CONDITIONAL**

- 除 `X-Source-App`（unresolved）与 endpoint 精确路由（与通用文档差异、与真实 wire 一致）外，其余契约全部 PASS；
- 官方 O1 对 `agentManifest.secrets` / credential boundary / ACP Link / SSE 的正面描述**支持** E3-B 设计，无实质冲突；
- `X-Source-App` 与路由差异为「公开官方文档缺失/表述差异」，**非实现与 wire 冲突** → 不升为 BLOCKED。

### Real E2E Gate：**NOT VERIFIED**

- 真实 WorkBuddy AgentOS API Key / Agent ID 缺失；缓存 acpLink token 已过期且无 `runtime_id`；
- 凭据缺口，非代码缺口。

### 总 Gate：**CONDITIONAL PASS**

```
Official Contract = CONDITIONAL   （X-Source-App unresolved + 路由差异；其余 PASS，无冲突）
Real E2E         = NOT VERIFIED   （无真实凭据）
=> CONDITIONAL PASS
```

> 进入真实 E2E 的唯二前置：① 合法 WorkBuddy 凭据（R4）；② `X-Source-App` 官方确认（R1，可选 — 确认前实现已安全缺省不发送）。两者均**无需改动 ACP Core / Domain / 既有 E3-B 实现**。

---

*本报告为 M6.6-E3-C 官方契约核验 + 真实 E2E 验证说明。唯一新增物：本文件。未修改任何代码 / migration / 测试 / config / 生产；未创建真实 Runtime / Agent；未 commit / tag。*
