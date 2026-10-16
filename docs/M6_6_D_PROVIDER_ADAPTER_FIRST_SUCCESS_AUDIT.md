# M6.6-D — Provider Adapter & Real First Success Audit

> 前序：
> - `M6.6-C3 Bootstrap / First Message` — PASS / CLOSED
> - `M6.6-C4 First Success` — PASS / CLOSED
> - `M6.6-C4 / CP4-2 Cross-Boundary Audit` — PASS
>
> 本轮：**Provider Adapter（WeCom / WeCom Bot / WorkBuddy）** 与 **Flow UX（Flow Home → Connect → Verify → First Success）** 的只读审计。
> 目标：验证当前用户可见 Flow 是否已能通过 **Provider-native Adapter** 真正完成 Connect → Verify → First Success。
> **只读纪律**：唯一新增物是本文件；未改动 backend/app、frontend、migration、测试、ADR、nginx、生产；未创建真实 Provider 配置 / Agent / Channel；未 commit / tag。
> 本轮**不重新审计 ACP Core（Layer 1）**——该层已由前序轮次 PASS/CLOSED，本轮仅做边界确认。

---

## 1. Scope

本轮三层职责（见 §24 三层次区分）：

- **Layer 1 — Core correctness**（Trust / Bootstrap / Identity / Grant / Session / Interaction / Worker / Delivery）：前序已 PASS，本轮仅做边界确认，不重开。
- **Layer 2 — Adapter correctness**（WeCom Adapter / WeCom Bot Adapter / WorkBuddy Adapter）：本轮重点。核心原则——外部平台异构、ACP 内部统一；Adapter 必须做 **External Language → ACP Language** 翻译，而非把 ACP 内部模型暴露给用户。
- **Layer 3 — User journey**（Flow Home → Connect → Verify → First Success）：本轮重点。检查 UX 是否泄漏 `connection_id / channel_id / runtime_id / acpLink / external_identity_id / external_conversation_id` 等内部术语。

审计方法：静态代码阅读 + 已有测试 + 静态搜索 + 只读 inspection。无真实 Provider 凭据，故真实 E2E 标为 `not verified`（见 §22 / §23）。

---

## 2. Current Provider Map

以代码事实为准（非假设）。受控类型集合：

- Channel：`backend/app/application/channels.py:44` → `ALLOWED_CHANNEL_TYPES = ("wecom", "local", "wecom_bot")`
- Agent：`backend/app/application/agents.py:30` → `ALLOWED_AGENT_TYPES = ("acp", "local")`

| Provider | 类别 | 实现文件 | 状态 | 备注 |
|---|---|---|---|---|
| local | Channel | `adapters/channels/local.py:55` `LocalChannelAdapter` | **implemented** | 无外部系统，开发用 First Success 路径 |
| wecom | Channel | `adapters/channels/wecom.py:452` `WeComAdapter` | **implemented** | HTTP webhook，自建应用 + 智能机器人双格式 |
| wecom_bot | Channel | `adapters/channels/wecom_bot.py:550` `WeComBotAdapter` + `:733` `WeComBotTransport` | **implemented** | WebSocket 长连接，当前推荐路径 |
| local / custom | Agent | `adapters/agents/local.py` `LocalAgentAdapter` | **implemented** | `type="local"`，确定性 echo |
| acp（= WorkBuddy） | Agent | `adapters/agents/acp.py:325` `ACPAdapter` | **implemented（但非 Provider-native）** | WorkBuddy 仅作为 `Agent(type="acp")` 的昵称；见 §12 |
| WorkBuddy（独立类） | Agent | — | **not implemented** | 架构守卫硬锁 `WorkBuddyAgent`/`WorkBuddyIntegration` 等专属类不存在（`tests/test_cloud_0_1_2_2_connect_agent_architecture_gate.py:42-46, 115-131`） |
| feishu / dingtalk / webhook / 其它 | Channel/Agent | — | **not implemented** | 全仓无对应适配器 |

adapter 分派层：`adapters/channels/registry.py:41` `MultiChannelAdapter`（按 `channel.type` 字典分派）；`adapters/agents/registry.py` `MultiAgentAdapter`（按 `Agent.type` 分派）。`main.py:114-121` 在 `create_app()` 工厂期构造固定字典。

> 注意：`adapters/channels/registry.py` 的 docstring（行 7-8）仍只写 "wecom → WeComAdapter / local → LocalChannelAdapter"，是**过期描述**；实际分派字典已含 `wecom_bot`。

---

## 3. Core Boundary Confirmation

Layer 1 本轮仅确认边界仍成立（不重开）：

- **CP-2 Trust Gate**：`application/channel_inbound.py:282-301` `accept_message` 顺序——`_load_channel`（仅定位）→ `_assert_route_trust`（段 1 路由信任）→ `adapter.parse_message`（段 2 验签）→ `_tenant_scope`（验签后才解析 TenantScope，绝不取自 URL）→ `create_pending`（PENDING+COMMIT 即 200 ACK）。
- **Bootstrap 段位置**：`application/worker.py:82-100` — `claim_processing`（CAS）→ `Bootstrap.ensure`（claim 后、execute 前）→ `RuntimeCoordinator.execute`。符合 CP3-F 冻结。
- **First Success 定义**：派生状态 `processing=succeeded AND delivery=delivered`（`M6_6_C4_CP4_2` §2），无持久化模型。本轮沿用，不重开。
- **Group 不消费 Arm**：`application/bootstrap.py:77-78` `_is_single_chat` 守卫。

结论：Layer 1 边界在 adapter 接入后**仍然成立**，无回归证据。

---

## 4. Flow Home Audit

检查：`frontend/home.html`、`frontend/home.js`、`frontend/connect.html`、`frontend/connect.js` 及对应管理面 API（`api/management/channels.py`、`agents.py`、`connections.py`）。

- **Flow Home 模型**：后端**无 SteeringFlow 实体**（`home.html:43-45` 仅把中间 hub 标为品牌名 "SteeringFlow"）。真实数据模型是 **Channel → Connection → Agent**；UI 呈现为 Channel → SteeringFlow(hub) → Agent，与品牌一致，**非泄漏**。
- **Connect 路径事实**：`connect.js:8-12` 始终是三件——Channel 已建 / Agent 已建 / Connection 已建。
- **五态处理**（`home.js:112-121` `computeState` + `render`）：

| 用户语义态 | 实际映射 | 渲染位置 |
|---|---|---|
| Empty（无 channel 无 agent） | `empty` | `home.js:135-143` |
| Agent Only / Channel Only / Both Unconnected | **三者全部并入 `partial`（待接通）** | `home.js:147-161`（用名字 vs "尚未连接" 区分，不区分三态） |
| Connected | `connected` | `home.js:183-199` |
| 存在 inactive 连接 | `error`（需要处理） | `home.js:164-181` |
| Connecting（瞬时读取） | `connecting` | `home.js:99-110` |

> **偏差 1**：`home.js` **不区分** "Agent Only / Channel Only / Both Unconnected"——三者折叠为单一 `partial`，仅节点显示名字区分。
> **偏差 2（核心）**：见 §5，Connected 以 tenant-global 判定。

---

## 5. Flow-scoped Connection Audit（M6.6-C0 复检，仍存活）

**结论：当前 Connected 判定是 tenant-global，不是 flow-scoped。**

`frontend/home.js:112-121`：

```js
function computeState(agents, channels, connections) {
  const hasChannel = channels.length > 0;
  const hasAnyAgent = agents.length > 0;
  const activeConn = connections.find((c) => c.status === "active");
  const inactiveConn = connections.find((c) => c.status !== "active");
  if (!hasChannel && !hasAnyAgent) return "empty";
  if (activeConn) return "connected";   // ← 只要本租户有任意 active Connection 即判定 connected
  if (inactiveConn) return "error";
  return "partial";
}
```

它**只检查"是否存在任意 status=active 的 Connection"**，不验证该 Connection 的 `channel_id`/`agent_id` 是否对应当前渠道/代理，也不解析"当前 channel + 当前 agent + 它们专属 connection"。`render()`（`home.js:185-186`）用 `channelById.get(activeConn.channel_id)` 取名字，但若引用到的 channel/agent 不在本租户列表里只显示 "—"，**不改变已判定的 `connected`**。

反例（M6.6-C0 场景）：

```
Tenant
 ├── Channel A
 ├── Agent A ── Connection A (active)
 ├── Channel B
 └── Agent B ── Connection B (active)
```

用户正在查看 `Channel A → SteeringFlow → Agent A`，若恰取到 `Connection B` 为 `activeConn`，则 hub 显示 "已接通"——但这是 Connection B 的接通，与当前 flow 无关。

后端事实：

- 管理面**无 "flow connected" 计算端点**，无 SteeringFlow/Flow 实体（`api/management/connections.py` 仅 `list_connections`/`health`）。
- **真正的 flow-scoped 解析只存在于运行时路由层**：`persistence/repositories/connection.py:73-82` `find_active_by_channel(channel_id)` 按 `channel_id + status='active'` 解析当前渠道的活跃访问路径（9.4 路由读路径）。该能力存在，但 **Flow Home 前端未使用**。

> **记录为 UX leakage / Adapter boundary violation（核心语义层）**：Connected 的 flow-scoped 语义在后端可达，但前端未采用，导致 tenant-global 误判。本轮**只记录、不修改**（禁止扩张范围）。

---

## 6. WeCom Audit（Legacy Webhook）

端点（`api/runtime/channels.py`）：

- `GET /api/channels/{id}/wecom/webhook` → `wecom_verify`（`:120-141`）
- `POST /api/channels/{id}/wecom/webhook` → `wecom_message`（`:143-178`）

**签名 / 加解密**（`adapters/channels/wecom.py`）：

- 签名 `compute_signature`（`:134`）：SHA1(排序拼接 `token+timestamp+nonce+encrypt`)。
- 加解密 `decrypt_message`（`:159`）：AES-CBC（PKCS7/32 块），IV=AESKey 前 16B；校验 `receiveid` 必须等于期望值（自建应用=`corp_id`，智能机器人=空串，由 `_expected_receiveid` `:481` 裁决），否则 `InboundSignatureError`。

**可信入站 / 租户解析**（`application/channel_inbound.py:282-301`）顺序写死：段 1 路由信任 → 段 2 验签 → 验签后才解析 TenantScope（绝不取自 URL）→ PENDING+COMMIT。

**Interaction 创建**：`intake.create_pending(inbound)`（`:299-300`）；`interaction_id` 返回后经 `background_tasks.add_task` 异步执行（`:175-177`）。

**失败行为**（`api/runtime/channels.py:110-118`）：`InboundChannelConfigError→500`、`InboundSignatureError→403`、`InboundFormatError→400`；失败时**不产生 Interaction**（异常在 `create_pending` 前已抛出或被路由信任拦截）。

**CP-2 Trust Gate 确认**：HTTP 路由对 `wecom/webhook` 显式声明 `expected_channel_type="wecom"`（`:137,167`）。`_assert_route_trust`（`channel_inbound.py:172-193`）：`if channel.type != expected_channel_type: raise InboundSignatureError`（403）。故 `channel.type != "wecom"`（含 `wecom_bot`/`local`）的请求在**段 1 即 403 拒绝**，**绝不进入** `parse_message → Identity → Bootstrap`。✅ 代码事实确认段 1 失败直接 `raise`，段 2 未被调用。

---

## 7. WeCom Bot Audit（Long Connection）

**用户需要提供的连接信息**（`connection_spec_from_channel` `wecom_bot.py:217-243` + 常量 `:182-184`）：

- `bot_id`（必填）、`bot_secret`（必填）、`ws_url`（可选，缺省 `wss://openws.work.weixin.qq.com`）。
- **不是** URL / Token / EncodingAESKey / CorpID / Webhook——这些属于 `wecom` webhook 适配器，Bot 长连接不消费。

> **结论：first-use UI 只要求 Bot ID + Bot Secret**，符合 Provider-native 形态，**无 UX leakage**（主路径 `connect.html` 不预填这些内部术语）。

**适配器激活路径（核心复检，M6.6-C3 关注点）**：

- 注册表 `main.py:114-121` 在 `create_app()` 工厂期构造固定字典（含 `wecom_bot`）。
- 真正的连接由 `WeComBotTransport` 经 FastAPI **lifespan 启动时** `bot_transport.start()` 拉起（`main.py:226-227`）。
- `start()`（`wecom_bot.py:787-804`）**只调用一次** `self._binding_source()`，枚举 `discover_bot_connections`（`main.py:429-447`，读 `uow.channels.list_channels()`，筛选 `type=="wecom_bot" and status=="active"`）。
- `_run` 重连循环（`:840-853`）只重连**已有** binding，**永不重新调用 `binding_source`**，全代码库无 `reload`/`rediscover`/重新发现端点。

**答案**：`POST create channel → wecom_bot → adapter discovery` = **restart required**（进程重启后 lifespan 重新 `start()` 才重新枚举）。

> **分类**：这是 WeCom Bot 首次真实 First Success 的 **First Success blocker（无重启则无法入站）**，同时归类为 **operational limitation**（适配器本身正确，仅发现机制在启动期一次性枚举）。结合真实激活路径判断：用户连好 WeCom Bot 后，若进程不重启，新渠道**不会**收到任何 inbound，故无法完成真实 First Success，直到显式重启。属有限后续实现批次需修复项（热发现 / 显式 reload 信号）。

---

## 8. WeCom Trust Boundary（Bot）

- **逐帧无签名**：WS 帧无 per-request 签名材料；`_on_bot_frame`（`main.py:449-455`）以 `signature="",timestamp="",nonce=""` 调 `accept_message`，`expected_channel_type=None` → 段 1 路由信任对 WS 是 no-op（`channel_inbound.py:187-188`）。
- **真正信任来自订阅连接**：`WeComBotTransport._subscribe`（`:866-883`）发送 `aibot_subscribe{bot_id, secret}`，**仅 `errcode==0` 才 `self._registry.register(channel_id, connection.send)`**；否则抛 `WeComBotSubscribeError` → supervisor 退避重连（Fail Closed，不进入 connected）。
- 之后所有入站帧只可能来自这条**已认证 live 连接**。
- **纵深防御（归属校验，非信任来源）**：`WeComBotAdapter.parse_message`（`:606-617`）比对帧 `aibotid` 与渠道 `bot_id`，不符则 `InboundFormatError`（只是 route/attribution，信任仍是订阅连接）。
- `verify_handshake` 对 wecom_bot **显式 fail-closed**（`:569-585`，抛 `InboundChannelConfigError`："无 URL 握手语义"）。

> 一句话：真实 Bot 消息可信，因它从一条用 `bot_secret` 订阅成功（`errcode==0`）的活连接到达，且该连接在连接期已完成认证。信任边界正确。✅

---

## 9. WeCom Bootstrap & First Success（Bot）

链路：`Worker.process`（`worker.py:70`）→ `claim_processing`（G1 CAS）→ `Bootstrap.ensure`（claim 后、execute 前）→ `RuntimeCoordinator.execute`（Identity→Auth→Routing→Session→Dispatch→Delivery）→ `WeComBotAdapter.deliver`（经 registry 活连接发 `aibot_respond_msg` `wecom_bot.py:623`）→ `delivery=DELIVERED`（帧写入 socket + 服务端 `errcode==0` ACK，`:629-708`）。

**首条消息 = Bootstrap 触发器（非认证来源）**：Arm 由已认证用户经 `build_bootstrap_arms_api` 创建（`main.py:310-321` `authenticated_user_provider`，未认证→401）。消息本身只**触发**授权事实建立，不作凭证（`bootstrap.py:5-14`）。

**"一条真实消息完成 First Success"**：支持。存在有效 Arm 且为单聊时，一次 `Worker.process` 内完成 Arm 消费 + Identity + Grant + Session + Agent + 交付（DELIVERED）。`wecom_bot` 与 `local` 共用同一路径。✅

> 本轮**不重新提出**"是否需要第二条消息"旧问题——当前语义已明确为 C 方案（首条=触发），且代码已达成。

---

## 10. WeCom First Success（完整路径可达性）

```
Verify Connection（URL 握手 / 订阅连接建立）
  ↓
Arm（已认证用户 build）
  ↓
user sends real WeCom message（单聊）
  ↓
trusted inbound（订阅连接）
  ↓
Identity / Grant Bootstrap（一次 Worker.process 内）
  ↓
Session
  ↓
Agent（ACPAdapter / LocalAgentAdapter）
  ↓
WeCom response（aibot_respond_msg）
  ↓
delivery=delivered
```

真实 E2E 可达条件：需有效 `bot_id`/`bot_secret` + 进程已 restart（见 §7）+ 有效 Arm。**无真实凭据时不声称 PASS**（见 §22 / §23）。

---

## 11. WeCom Group Chat（chat_type=group）

适配器层（`inbound_from_callback` `wecom_bot.py:346-424`）：

- `chattype=="group"` → `external_conversation_id = body.chatid`（群会话键），与单聊（`from.userid`）明确区分（`:396-403`）。
- 未知 `chattype` → `return None`（acknowledge-ignore，不猜、不误判，`:382-383`）。
- 元数据 `metadata = {BOT_REPLY_METADATA_KEY:{_HEADER_REQ_ID:req_id}, CHAT_TYPE_METADATA_KEY:chattype}`（`:420-423`）。

Bootstrap 层（`bootstrap.py:77-78`）：

```python
def _ensure(self, inbound, interaction_id):
    if not self._is_single_chat(inbound):
        return NOT_APPLICABLE   # 群聊/未知 ⇒ 不消费
```

`_is_single_chat`（`:159-165`）仅当 `metadata[CHAT_TYPE_METADATA_KEY]=="single"` 为真；group ⇒ False ⇒ `NOT_APPLICABLE`（不触碰 Arm、不建 Identity/Grant）。

**不绕过授权的事实链**：群聊不建 Grant → `AccessAuthorizationExecutor.execute`（`access_authorization.py:42-70`）在 `find_granted_connection_ids` 为空时 `raise AccessAuthorizationDeniedError` → STOP（DENY，不路由/不建会话/不投递）。除非群会话事先已有手动 Grant，否则群聊消息被拒。

**元数据 flow 适配器→core**：适配器置 `chat_type`+`bot_reply` → `interaction_snapshot` 恒存 metadata（`inbound_interaction.py:56-61`）→ Worker 重建 → `coordinator.py:164` 合并（channel-originated 覆盖同名 key）→ `WeComBotAdapter.deliver` 取 `req_id` 发帧。Core 只读 `CHAT_TYPE_METADATA_KEY`，**绝不做** "conversation≠identity ⇒ 群聊" 启发式推断。✅

> 确认：群聊在适配器层与 Bootstrap 层均被显式排除——不消费 Arm、不建 Identity/Grant、不绕过授权、不误判为单聊。V0 Group Chat 冻结决策成立。

---

## 12. WorkBuddy Adapter Audit

**核心发现：WorkBuddy 当前不是 Provider-native Agent 集成，而是 `ACPAdapter`（type="acp"）的手动 endpoint / bearer_token / runtime_id 配置模型。**

- 无 `workbuddy.py` 适配器文件；"WorkBuddy" 在代码中仅为 `Agent(type="acp")` 的昵称（指向腾讯 CodeBuddy Enterprise ACP Runtime），由 `ACPAdapter` 实现（`adapters/agents/acp.py:325`）。
- 架构守卫（`tests/test_cloud_0_1_2_2_connect_agent_architecture_gate.py:42-46,115-131`）**硬锁** `WorkBuddyAgent`/`WorkBuddyIntegration` 等专属类不存在。

**目标形态 vs 当前形态**：

```
目标：User → WorkBuddy Agent → Agent ID → API Key → SteeringFlow WorkBuddy Adapter
      → WorkBuddy Management API → Runtime → ACP / Agent execution
当前：User → Agent(type=acp) → 用户手填 endpoint+bearer_token+runtime_id（在 WorkBuddy 控制台外创建 Runtime 后粘贴）
      → ACPAdapter → 直连 ACP Runtime SSE 端点
```

`ACPAdapter` 从 `Connection.config` 读自由键（`acp.py:468-494`）：`protocol`(`tencent-acp-sse`/`minimal`)、`endpoint`(=acpLink.url)、`bearer_token`(=acpLink.token)、`runtime_id`(=runtime.id)、`timeout_s`。

> **UX leakage / Adapter boundary violation（WorkBuddy）**：用户必须理解 `runtime_id` / `acpLink`（endpoint / bearer_token），这些 ACP 内部/平台概念**已泄露到用户 UX**（见 §13 / §15 / §19）。不符合 "User 不应需要理解 runtime_id / acpLink / ACP session / endpoint / bearer_token" 的设计目标。

---

## 13. WorkBuddy Management API

代码中**不存在** "API Key → Management API → Agent 发现 → Runtime 创建" 流程。逐步骤判定：

| 步骤 | 状态 | 证据 |
|---|---|---|
| API Key 输入 | **缺失** | 无 `api_key` 字段；凭据是用户粘贴的 `bearer_token`。全仓无 WorkBuddy 管理端点 HTTP 客户端 |
| Management API 调用 | **缺失** | backend 无任何 `codebuddy.cn`/管理端点的客户端（仅 `api/version.py` 无关命中） |
| Agent 发现 / 选择 | **缺失** | Agent 仅用 `name`+`type` 创建（`application/agents.py:157-188`），无平台侧发现 |
| Runtime 创建 | **缺失** | 无 `createRuntime` 调用；`runtime_id` 由用户手填 |
| runtime.id 进入 ACP | **手动粘贴** | `examples/wecom-workbuddy/README.md:49-57`、前端 `connect-agent.js:30-35` 模板 |
| Runtime 执行 | **implemented** | `ACPAdapter.send` 对预置 endpoint 发 SSE+POST（`acp.py:460-653`） |

**"Agent ID" 仅是用户输入字符串**（`name`）+ DB 整数 id，由 `POST /api/agents` 落库（`schemas/agents.py:31-39`）。SteeringFlow **不会**用 API Key 在 WorkBuddy 平台建立 Runtime——Runtime 完全在 SteeringFlow 之外于 WorkBuddy 控制台创建，用户把 `acpLink.url/token` 与 `runtime.id` 复制进 `Connection.config`。

---

## 14. WorkBuddy Runtime

存在**两个互不相干的 "Runtime" 概念**：

1. **SteeringFlow Runtime** = 执行管线：`RuntimeExecutionCoordinator`（`application/runtime.py:89`）+ `RuntimeExecutionResult`（`domain/runtime_execution_result.py:60`，仅 3 字段 `session_mode`/`session_id`/`agent_response`）。无 `runtime_id`/`acpLink`。
2. **WorkBuddy 平台 Runtime** = 仅以 `Connection.config["runtime_id"]` + `config["bearer_token"]`（acpLink.token）表示；是**用户手填的明文自由文本**。

**如何进入 ACP**：用户手动粘贴进 `Connection.config` JSON（配方 `examples/wecom-workbuddy/README.md:54-56`；前端模板 `connect-agent.js:32-34`）。

**是否泄漏到 UX**：**是**。`connect-agent.html:89` 提示"访问 endpoint / bearer_token / 运行时标识 等落在此处"；`connect-channel.html:123` 同样。"运行时标识"(runtime_id) 与 `acpLink.token` 是用户可见、需手工填入的字段；`runtime_id` 在 `Connection` 响应中明文回显（`bearer_token` 被脱敏）。

**Runtime 状态机（CREATING→READY）**：SteeringFlow **无此状态机**。WorkBuddy Runtime 的创建/就绪生命周期发生在 WorkBuddy 平台，对 SteeringFlow 不透明；SteeringFlow 仅在 dispatch 时对该 endpoint 做 Fail-Closed 建连（`acp.py:472-494`）。

> 目标 `User → WorkBuddy Agent → SteeringFlow` 未达成；当前是 `User → Runtime → ACP Link → Session`（经手动粘贴）。属明确 Adapter boundary violation + UX leakage。

---

## 15. WorkBuddy Credential Boundary

`bearer_token`（= API Key 等价物）安全边界：

| 维度 | 结论 | 证据 |
|---|---|---|
| 输入 | `Connection.config` JSON 的 `bearer_token` 键 | 前端 `f-conn-config` textarea（`connect-agent.js:154`）、`POST /api/connections` |
| 保存 | DB `connections.config` JSON 列 | `persistence/models/connection.py:50` |
| 传递/使用 | `Authorization: Bearer {token}` 头 | `acp.py:498`（send）、`acp.py:399`（probe） |
| 日志 | **不写入日志** | `acp.py` 全文 `logging/print` 命中 0 |
| 响应回显 | **不回显** | `schemas/connections.py:51-54` `SENSITIVE_CONNECTION_CONFIG_KEYS` 含 `bearer_token`；`sanitize_connection_config`（`:59`）置 `********` |
| URL | 密钥不在 URL（在 header） | `acp.py:498`；`endpoint`(acpLink.url host) 在 URL 但非密钥 |
| 前端状态 | **不持久化**于前端 | `connect-agent.js` 的 `state` 仅 POST payload 内存暂存，不渲染 DOM / 不写 storage |
| Interaction/Activity | **不进入** | `domain/interaction_record.py:61` `response_payload` 仅承载响应内容；ACPAdapter 只写 `content`+`external_session_id`+`metadata` |

`credential_ref` 为独立 write-only 不透明引用（`schemas/connections.py:104,125`、`:157-160` 响应模型不含），与 `bearer_token` 分离。

> 凭据使用边界正确（不回显/不日志/不进 URL/不进 Interaction）。但其**输入仍需用户手工粘贴 ACP 内部术语**（bearer_token / endpoint / runtime_id），这是 UX 形态问题，非凭据泄露问题。

---

## 16. Agent Adapter Boundary

**协议翻译职责正确**：`ACPAdapter` 把 ACP wire（jsonrpc+SSE）↔ 归一化 `AgentRequest`/`AgentResponse` 互译，仅从 `Connection.credential_ref/config` 取连接信息（`repositories/agent_adapter.py:13-19`，`acp.py` docstring 声明"不把 ACP 专有概念塞进 AgentRequest/AgentResponse"）。

**泄漏评估（精确命中）**：全仓搜 `runtime_id|acpLink|bearer_token` 在 `backend/app/domain` 与 `backend/app/persistence` 均**零命中**。WorkBuddy 专有概念只存在于：
- `Connection.config`（自由 JSON 字典）+ `ACPAdapter` 内部（`acp.py`）
- `application/connections.py:99`、`application/agents.py:121`、`schemas/connections.py:26-29` 注释/脱敏清单

**唯一跨入域模型的点（设计内翻译点）**：WorkBuddy `runtime.id` 经 `AgentRequest.external_session_id` / `AgentResponse.external_session_id` / `Session.external_session_id` 流动，统一使用协议中性名 `external_session_id`（`domain/agent_request.py:15,41`）。`acp.py:489-494` 当 `external_session_id is None` 时令 `session_id = str(runtime_id)`——属预期翻译边界，非 WorkBuddy 专有类型泄漏。

**架构边界问题（记录）**：
1. `Connection.config` 实际充当"协议参数 + 密钥 + 平台 runtime 标识"混合 blob；域契约声明"不是万能配置容器"（`domain/connection.py:17-18`），但 `tencent-acp-sse` 专属键（`endpoint/bearer_token/runtime_id`）以自由字符串键泄漏进适配器（`acp.py` 的 `config.get(...)`），好在完全收口于 `ACPAdapter`。
2. 同名歧义：SteeringFlow Runtime（执行管线）与 WorkBuddy Runtime（平台运行时）语义不同，仅后者 id 以用户粘贴字符串进入 `Connection.config`。
3. `runtime.id → external_session_id` 绑定（`acp.py:489-494`）是 WorkBuddy 标识唯一进入域类型字段之处，属协议翻译，但耦合值得记录。

> 总体：provider-specific 协议被正确隔离在 `ACPAdapter` 内；WorkBuddy 专有命名**未污染** Session/Interaction/Router/Core 的类型化字段（仅经协议中性 `external_session_id` 与 `Connection.config` blob 出现）。**边界基本干净**，无架构冲突。主要"泄漏"是 `Connection.config` 兼作密钥+runtime 标识存储（脱敏已覆盖）及 runtime.id→external_session_id 映射。

---

## 17. Connection Semantics

`Connection` = Channel ↔ Agent 的受控访问路径（Access Path）。域模型 `domain/connection.py:36-48`：`Connection 1 ─ N ─ 1 Agent`；`UNIQUE(channel_id, agent_id)`；status 纯管理态 active/inactive；`config` 只存连接参数；`credential_ref` 只存引用。

**WorkBuddy / Runtime / ACP Connection 边界**：三者 distinct，未混淆。
- Agent（领域实体）= 逻辑身份。
- Connection（领域实体）= 绑定 + 访问路径。
- Runtime = `Connection.config` 里的 `runtime_id` 键（由 AgentAdapter dispatch 时 Fail-Closed 校验），**无独立治理实体**。

> Adapter 未把 runtime/credential/channel/agent 混成一个实体；Connection 语义保持 "Channel ↔ Agent 关系"。✅ 但 Runtime 仅为 config 键（见 §14），无独立生命周期治理。

---

## 18. Credential Storage

**存储位置（持久层 = 明文 JSON 列，非加密）**：
- WeCom Bot Secret → `Channel.config.corpsecret`/`bot_secret`，存于 `channels.config`（`persistence/models/channel.py:22`；ORM 注释"凭据明文进入 JSON"）。
- WorkBuddy API Key(bearer_token) → `Connection.config.bearer_token`，存于 `connections.config`（`persistence/models/connection.py:50`）。
- `cryptography` 依赖（`requirements.txt:16`）被文档锁定"全仓唯一归属 wecom.py"（`docs/step-10.2.1-wecom-inbound-boundary.md`），仅用于企业微信 wire 签名/AES 信封，**不用于密钥落库加密**。

**保护层**：(a) 读时脱敏；(b) DB 访问控制 + 租户 RLS。

**GET 配置 API 是否泄露**：**不会明文泄露**。`ChannelResponse` 经 `sanitize_channel_config()`（`schemas/channels.py:39-61`，敏感键 `{token, encoding_aes_key, corpsecret, bot_secret}`）；`ConnectionResponse` 经 `sanitize_connection_config()`（`schemas/connections.py:51-74`，敏感键 `{bearer_token, access_token, api_key, client_secret, token, secret, password, authorization}`）。均为 model 层 `@field_validator` 强制脱敏（`********`），写路径拒绝回写占位符（`_reject_redacted_sentinel`，`:77-92`→422）。探针 `GET /.../health` 不返回 config。

> 事实记录：当前**仅为明文 JSON 落库、无静态加密**，依赖读时脱敏 + DB/RLS 保护；GET 配置接口不泄露明文。本轮**不重构**凭据架构（禁止扩张范围）。

---

## 19. Error Mapping

Provider-specific 失败 → 用户可理解状态：

**入站（WeCom/WeCom Bot）**（`api/runtime/channels.py:110-118` `_map_protocol_errors`，唯一映射点）：
- `InboundChannelConfigError → 500 "channel inbound misconfigured"`
- `InboundSignatureError → 403 "inbound authentication failed"`
- `InboundFormatError → 400 "inbound message malformed"`
- 其它 → 500 "inbound error"

映射为**人类可读短语**，无内部错误码/内部 ID 泄露。✅

**WeCom Bot 订阅失败**：`WeComBotSubscribeError` → supervisor 退避重连（Fail Closed），不进入 connected、不向用户暴露内部名。✅

**Agent 执行失败（含 WorkBuddy）**：`AgentDispatchFailedError` 等 typed error 为**内部**类型，仅记服务端日志；用户侧仅表现为 `processing_status=failed` / `delivery` 未 delivered（即 First Success 未达成），**不向 UI 暴露** `ACP_SESSION_xxx` / `Connection ID 17` / `runtime_id=xxx`。✅

**Verify Connection 探针（WorkBuddy）**：`ACPAdapter.probe` 返回 `AgentProbeSnapshot(reachable, detail)`（`acp.py:404-408, 439-440`）。`detail` 可能含原始 HTTP 状态串（如 `"GET SSE status 401"`）或异常类型名（`f"{type(exc).__name__}: {exc}"`）。前端 `connect.js:486` 直接渲染 `验证未通过 ✗ ${snap.detail}`：

> **有限 UX leakage（Error Mapping）**：WorkBuddy 验证失败时，`detail` 可能向用户暴露原始 HTTP 状态（`401`）或传输层异常类型名（如 `ConnectionRefusedError`）。非凭据泄露，但违反"用户不应看到 HTTP 401 from runtime endpoint"原则。属 minor finding。

**总体**：核心错误映射（入站信任、bot 订阅、agent 执行）边界合理；仅 WorkBuddy 探针 `detail` 存在有限原始 HTTP 状态暴露。

---

## 20. Connected vs First Success

- **Connected**（来自 `home.js:118`）：本租户存在任意 `status=active` 的 Connection（tenant-global，见 §5）。表示"ACP 已建立 Channel ↔ Agent 绑定"的管理态——但可能非 flow-scoped。
- **First Success**（派生）：同一 `interaction` 行 `processing=succeeded AND delivery=delivered`（`M6_6_C4_CP4_2` §2）。表示至少有一次真实 Interaction 成功处理并成功 Delivery。

两者**不可混淆**：`Connected` 是绑定态（管理面），`First Success` 是真实 E2E 派生态（运行时）。前端 `home.js:203-214` `checkFirstSuccess` 通过查 interactions 判定，未把 `Connected` 当 `First Success`。✅ 该区分在前端正确保持。

---

## 21. Verify Connection

- **统一流**：`connect.js:478-495` `probe(connectionId)` → `GET /api/connections/{id}/health`，`reachable=true → "验证通过 ✓ 你的 Agent 可达"`，否则 `验证未通过 ✗ ${detail}`；503 → "暂不可用（非连接结论）"。`createBinding`（`:497-528`）接通后自动 `probe`；409 冲突裁决同样 `probe`。
- **后端**：`GET /api/connections/{id}/health`（`api/management/connections.py:188-216`）注入 `ACPAdapter.probe`（只读 config、Fail-Closed、不写库、不改 status）。
- **WeCom 额外验证**：服务端驱动 URL 握手 `GET /api/channels/{id}/wecom/webhook`（企业微信回调验证 echostr），**非 UI 按钮**；故用户侧 verify 对 WeCom/WorkBuddy 是强制相同的。

> **Finding**：Verify Connection 被**强制同构**——WeCom 与 WorkBuddy 共用同一 `/health` 探针，**无按渠道类型分叉的 provider-differentiated 验证**。WeCom 的"真实验证"是服务端 URL 握手（用户无按钮）；WorkBuddy 的 `/health` 仅是**可达性**（SSE 建连），非真正的 Agent 可用性/授权验证。为统一 UI 把不同 Provider 验证机制压平，不符合 §21 "不强行相同"原则。

---

## 22. Real Provider E2E Readiness

| Provider | 真实 E2E 可达条件 | 结论 |
|---|---|---|
| local | 无外部系统，golden 测试覆盖 | **real E2E available（测试/开发）** |
| WeCom（legacy webhook） | 需有效 CorpID/Token/AESKey + 企业微信回调可达 | 代码路径完备；**real-provider-evidence unavailable**（无真实凭据） |
| WeCom Bot | 需有效 `bot_id`/`bot_secret` + **进程已 restart** + 有效 Arm | 代码路径完备；**real-provider-evidence unavailable**；且 restart 前置是 operational 阻塞（§7） |
| WorkBuddy | 需用户在 WorkBuddy 控制台外创建 Runtime + 粘贴 `acpLink.url/token`+`runtime.id` | 代码路径完备；**real-provider-evidence unavailable**；且 UX 非 Provider-native（§12-14） |
| other | 未实现 | N/A |

> 本轮只读判断：**无任何 Provider 拥有真实凭据 E2E PASS 证据**。所有 "real E2E" 标 `not verified`，不把静态代码审计写成真实 E2E PASS。

---

## 23. Evidence Matrix

| Provider | Connect | Adapter | Trust | Bootstrap | Agent Exec | Delivery | First Success | Evidence |
|---|---|---|---|---|---|---|---|---|
| local | ✅ impl | ✅ impl | n/a | n/a | ✅ impl | ✅ impl | ✅ golden tests | existing |
| WeCom | ✅ impl | ✅ impl | ✅ CP2 两段式 | ✅ (webhook 不消费 Arm，单聊 bootstrap 在 bot 路径) | ✅ | ✅ | ✅ 路径完备 | existing + code audit |
| WeCom Bot | ✅ impl (Bot ID+Secret) | ✅ impl | ✅ 订阅连接 | ✅ 单聊消费 Arm | ✅ | ✅ | ✅ 路径完备 | existing + code audit；**restart 前置** |
| WorkBuddy | ⚠️ 手动粘贴 | ⚠️ ACPAdapter 非 native | n/a | n/a | ✅ impl | ✅ impl | ✅ 路径完备 | implementation audit；**real-provider-evidence unavailable** |
| other | ❌ not impl | ❌ | — | — | — | — | — | existing（无） |

> Evidence ≠ implementation。无真实凭据时：`real E2E = not verified`。

---

## 24. 三层区分

- **Layer 1 — Core correctness**（Trust / Bootstrap / Identity / Grant / Session / Interaction / Worker / Delivery）：前序 PASS/CLOSED，本轮边界确认仍成立（§3），**不重开**。
- **Layer 2 — Adapter correctness**（WeCom / WeCom Bot / WorkBuddy Adapter）：本轮重点。WeCom / WeCom Bot 适配器正确；WorkBuddy 适配器**非 Provider-native**（手动 endpoint/bearer_token/runtime_id）。
- **Layer 3 — User journey**（Flow Home → Connect → Verify → First Success）：本轮重点。Flow-scoped Connection 仍 tenant-global（§5）；WorkBuddy Connect 泄漏 `runtime_id`/`acpLink`（§14）；Verify 强制同构（§21）。

---

## 25. Findings

| # | 层 | 严重度 | 发现 | 分类 |
|---|---|---|---|---|
| F1 | L3 | **high** | Flow Home `Connected` 以 tenant-global 判定（`home.js:118`），非 flow-scoped（当前 channel+当前 agent+其专属 connection）。M6.6-C0 复检仍存活 | UX leakage / 核心语义边界违例 |
| F2 | L2/L3 | **high** | WeCom Bot 适配器发现仅在进程启动期枚举一次（`wecom_bot.py:787-804` + `main.py:429-447`）；新建 `wecom_bot` 渠道需**重启进程**才能入站 | First Success blocker（无重启）/ operational limitation |
| F3 | L2/L3 | **high** | WorkBuddy 非 Provider-native：无 API Key→Management API→Agent 发现→Runtime 创建；用户须手填 `endpoint`/`bearer_token`/`runtime_id`（=acpLink） | Adapter boundary violation + UX leakage |
| F4 | L3 | **medium** | `connect-agent.html/connect-agent.js` 经"填入 ACP 模板"向用户暴露 `acpLink`（`<acpLink.url>`/`<acpLink.token>`）与 `runtime_id`（`<runtime.id>`） | UX leakage |
| F5 | L3 | **medium** | Verify Connection 强制同构（WeCom/WorkBuddy 共用 `/health` 探针）；WorkBuddy 验证仅为可达性，非 Agent 可用性/授权 | Verify 机制未 provider-differentiated |
| F6 | L2 | **low** | WorkBuddy 探针 `detail` 可能向用户暴露原始 HTTP 状态（`401`）/ 传输层异常类型名（`connect.js:486` + `acp.py:406,440`） | Error Mapping minor leak |
| F7 | L2 | **low** | `Connection.config` 兼作"协议参数+密钥+平台 runtime 标识"混合 blob；`runtime.id→external_session_id` 耦合（仅翻译点，已收口 ACPAdapter） | 架构边界观察（非冲突） |
| F8 | L1 | **info** | 凭据明文 JSON 落库、无静态加密；GET 配置接口不泄露明文（脱敏+RLS） | 记录事实（不重构） |

> 非阻塞但需有限后续批次：F1/F2/F3/F4/F5。F6/F7/F8 为观察级。

---

## 26. Non-Goals

本轮**不**做（禁止扩张范围）：
- 重构 AgentAdapter / ChannelAdapter
- 引入新 Provider abstraction / 统一 Credential Vault / 新 Connection model
- 修改 Session / Bootstrap / Authorization / Worker / Delivery
- 增加新 Runtime stage / 复杂 Flow Graph / multi-agent UI / Group Bootstrap
- 修复 F1-F5（仅记录、分类、判定是否阻塞）

---

## 27. Final Gate

**CONDITIONAL PASS**

判定依据：

- **Layer 1（Core）**：Trust / Bootstrap / Identity / Grant / Session / Interaction / Worker / Delivery 边界经本轮确认仍成立，无回归。✅
- **Layer 2（Adapter）**：WeCom / WeCom Bot 适配器实现正确，CP-2 Trust Gate、Bootstrap 段位置、群聊冻结、首条消息=Bootstrap 触发均达成。✅ 但 **WorkBuddy 非 Provider-native**（F3）——仅 `ACPAdapter` 手动配置模型，无 Management API / Runtime 自动创建，且 `runtime_id`/`acpLink` 泄漏至 UX（F4）。
- **Layer 3（User journey）**：Flow Home `Connected` 仍 tenant-global（F1，C0 复检存活）；Verify Connection 强制同构（F5）；WeCom Bot 首次真实 First Success 需进程重启（F2，operational blocker）。

**非 BLOCKED 理由**：Provider Adapter 与 ACP Core 的边界**无架构冲突**（WorkBuddy 专有概念完全收口于 `ACPAdapter` 与 `Connection.config`，未污染 Core 类型化字段，§16）；真实 First Success 在当前设计下**可以成立**（WeCom 经正确路径 + 重启；WorkBuddy 经手动 Runtime 创建 + 粘贴配置）。

**剩余工作仅为有限后续实现批次**（不阻塞核心，但需在宣称"Provider-native First Success"前完成）：
1. WeCom Bot 热发现 / 显式 reload 信号（消除 F2 重启前置）。
2. WorkBuddy Provider-native Adapter：API Key → Management API → Agent 发现 → Runtime 创建，消除 `runtime_id`/`acpLink` UX 泄漏（F3/F4）。
3. Flow Home flow-scoped Connected 判定（复用 `find_active_by_channel`，消除 F1）。
4. Provider-differentiated Verify（F5）与探针 `detail` 净化（F6）。

> 在真实凭据到位前，全部 Provider 的 **real E2E = not verified**（§22/§23）；本 CONDITIONAL PASS 基于代码路径正确性 + 核心边界成立，剩余为正常真实凭据验证 + 上述有限 Adapter/UX 批次。

---

*本报告为只读审计，未修改任何代码 / migration / 测试 / ADR / nginx / 生产；未创建真实 Provider 配置 / Agent / Channel；未 commit / tag。唯一新增物：本文件。*
