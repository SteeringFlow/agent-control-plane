# Step 15.2 — Local First Success（canonical procedure）

> **Provenance（本文档的来源，务必先读）**
>
> 本文档**不是**一份被"恢复"的历史 Step 留档——它是 M1 Release Fix 期间，依据
> **当前实现**与**当前测试证据**重写的 **canonical First Success procedure**：
>
> ```
> implementation  →  test evidence  →  current documentation
> ```
>
> 依据：`backend/app/adapters/channels/local.py`、`local.py::parse_message`、
> `backend/app/api/runtime/channels.py`（`POST /api/channels/{id}/local/message`）、
> Runtime 执行链（`application/runtime.py`）、Package 2 授权
> （`application/access_authorization.py`），以及
> `backend/tests/test_step15_2_3_local_first_success.py`。
>
> 目的：让"零外部依赖 Local First Success"与当前 **Identity + Conversation Grant**
> 的真实语义一致——修正此前 README / walkthrough 中 **"建 Connection 后直接发消息"**
> 的缺步（漏掉 Grant）。

---

## 1. 一句话定义

**Local First Success = 在不接任何外部系统（无 Tencent ACP / 无企业微信 / 无公网
HTTPS / 无凭据）时，经完整、未改动的 Runtime 链跑通一次真实交互，且该 Interaction
终态为 `processing_status=succeeded` 且 `delivery_status=delivered`。**

链路（与 WeCom 完全相同的六段 + 授权）：

```
POST /api/channels/{id}/local/message {"external_id","text"}
   ↓ LocalChannelAdapter.parse_message → InboundInteraction（8.1 契约）
   ↓ ChannelInboundService → PENDING + COMMIT → HTTP 200（ACK ≠ 执行）
   ↓（响应之后）Worker → RuntimeCoordinator：
       identity_resolution → authorization → routing
         → session_resolution → [session_creation?] → agent_dispatch → delivery
   ↓ interactions.response_payload（Observation 读取出口）
```

---

## 2. 什么算 / 不算 First Success

| | 判据 |
|---|---|
| ✅ **First Success** | `processing_status=succeeded` **且** `delivery_status=delivered`（原生双状态，见 `GET /api/interactions/{id}`） |
| ❌ 不算 | HTTP 200 / `status=accepted`（仅"已持久化接收"） |
| ❌ 不算 | Agent / Channel / Connection / Identity / Grant **配置成功** |
| ⚠️ 合法但非成功 | `succeeded × failed`（Agent 成功、投递失败）——是双状态事实，不是 First Success |

---

## 3. 前置事实（5 实体 + 1 授权，缺一不可）

| # | 事实 | 键 / 说明 |
|---|---|---|
| 1 | User | `user_id` |
| 2 | Agent（`type=local`） | 内置确定性 echo，不接外部系统 |
| 3 | Channel（`type=local`） | 本地 JSON 入站 + 本地 outbox 交付；`channel_id` |
| 4 | **Identity** | `(channel_id, external_id)` → User；Runtime 未命中即 Fail Closed |
| 5 | Connection（Access Path） | `(channel_id, agent_id)`，`status=active` |
| 6 | **ConnectionConversation Grant** | `(channel_id, external_conversation_id, connection_id)`；Access Authorization 默认 deny |

**Local 单聊键定义**：`external_conversation_id = external_id`（`local.py:126`，
conversation == identity，与 WeCom 单聊裁决同形）。

> **第 6 项是 M1 修复点**：缺 Grant 时消息会在 `authorization` 阶段被拒
> （见 §5），不会到达 Agent。

---

## 4. Canonical 全流程（curl）

```bash
BASE=http://127.0.0.1:8000

# 1. User
curl -s -X POST $BASE/api/users -H 'Content-Type: application/json' \
  -d '{"name": "local-user"}'

# 2. Local Agent（type=local → 确定性 echo）
curl -s -X POST $BASE/api/agents -H 'Content-Type: application/json' \
  -d '{"name": "local-echo", "type": "local"}'

# 3. Local Channel（type=local → JSON 入站 / 本地 outbox 交付）
curl -s -X POST $BASE/api/channels -H 'Content-Type: application/json' \
  -d '{"name": "local-1", "type": "local"}'

# 4. Identity：绑定外部身份 local-user-1 → (user, channel)
curl -s -X POST $BASE/api/identities -H 'Content-Type: application/json' \
  -d '{"user_id": 1, "channel_id": 1, "external_id": "local-user-1"}'

# 5. Connection：Channel ↔ Agent（status 默认 active）
curl -s -X POST $BASE/api/connections -H 'Content-Type: application/json' \
  -d '{"channel_id": 1, "agent_id": 1, "name": "local-path"}'

# 6. ConnectionConversation Grant：授权该会话访问该 Connection（必须）
curl -s -X POST $BASE/api/connection-conversations \
  -H 'Content-Type: application/json' \
  -d '{"channel_id": 1, "external_conversation_id": "local-user-1", "connection_id": 1}'

# 7. 发消息（200 仅表示已持久化接收）
curl -s -X POST $BASE/api/channels/1/local/message \
  -H 'Content-Type: application/json' \
  -d '{"external_id": "local-user-1", "text": "hello"}'
# -> {"interaction_id": 1, "status": "accepted"}

# 8. 读结果（读取出口）
curl -s $BASE/api/interactions/1
# -> processing_status=succeeded、delivery_status=delivered、
#    response_payload.content == "Echo: hello"
```

- **本地交付 sink**：响应同时追加写入 `backend/data/local_outbox.jsonl`（本地文件，
  不入库）。durable 事实面以 Interaction 观察为准。
- **入站边界**：非 JSON / 非对象 / 缺字段 / 空白 → `InboundFormatError`（HTTP 400）。
  `message_id` 可选（缺省 uuid4），作为 `external_interaction_id`（幂等键，
  与 `channel_id` 组成 UNIQUE）。

---

## 5. Bootstrap 与失败语义（首条被拒可能是正常的）

若 provisioning **未完成**，首条消息仍会 ACK 200，但终态失败：

| 缺失前置 | 终态 | 语义 |
|---|---|---|
| Identity（步 4） | `failure_stage=identity_resolution / failure_code=IDENTITY_RESOLUTION_FAILED` | Fail Closed；**绝不**自动创建 Identity/User |
| Grant（步 6） | `failure_stage=authorization / failure_code=UNAUTHORIZED` | 默认 deny；**绝不**自动授权（Discovery ≠ Authorization） |

**正常 bootstrap**：这是设计上的 Fail Closed，不是系统故障。被拒 Interaction 会在
`request_payload` 中保留 `external_identity_id` 与 `external_conversation_id`
（intake 阶段即持久化，先于执行），据此补齐 Identity / Grant 后重发即成功。

> 只有 `succeeded × delivered` 才叫 First Success；`IDENTITY_RESOLUTION_FAILED` /
> `UNAUTHORIZED` 是 provisioning 未完成的正常结果，不是"回执失败"。

---

## 6. 证据映射（`test_step15_2_3_local_first_success.py`）

| 组 | 覆盖 |
|---|---|
| A | `LocalAgentAdapter`：确定性 echo / prefix 覆写 / 空白内容 Fail Closed |
| B | `MultiAgentAdapter`：按 `Agent.type` 路由；未知 type → `AGENT_DISPATCH_FAILED` |
| C | `LocalChannelAdapter`：JSON 归一化 / outbox sink / 非法输入 Fail Closed |
| D | `MultiChannelAdapter`：按渠道类型路由（入站 + 出站） |
| E | **全链路 First Success**：真实六段 + 真实 Local Adapter + Observation |
| F | Identity 不绕过：未知 `external_id` → `IDENTITY_RESOLUTION_FAILED`，无 Session、无投递 |
| G | P1 校验：非法 type（如 `console`）→ 422；`local` 合法 |

**关键证据（M1 根因）**：E 组之所以通过，是因为测试 `_seed` **同时建立了 Identity 与
Grant**（`uow.connection_conversations.create(ConnectionConversation(...))`）。
这证明"Identity + Grant"是 First Success 的**必需前置**——此前 README 快速开始缺的
正是 Grant 这一步。

---

## 7. 边界 / 非目标

```
✅ 本文档只描述既有、已冻结的行为（Identity 解析 + Access Authorization + Local Adapter）
✅ Local 是 OSS V0.x 的本地开发 / First Success 渠道，不是生产 Channel
❌ 不引入 auto-grant / "首次见到即授权"（ADR-6：Discovery ≠ Authorization）
❌ 不引入 bootstrap mode / 配置开关 / 新 API / 新迁移
❌ 不引入 Retry / Replay / Queue / Redis / Celery
```

Runtime / Authorization / Identity Resolution / Connection / Grant API **均零改动**——
本 Step 的产物是**文档对齐**，不是代码。
