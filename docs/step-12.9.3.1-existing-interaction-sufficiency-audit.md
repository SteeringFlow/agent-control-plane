# Step 12.9.3.1 — Existing Interaction Sufficiency Audit

> 只读审计（承接 12.9.1 ACK 边界、12.9.2 duplicate ACK）。**不改生产代码。**
> 只回答一个非常具体的问题：
>
> **「如果 HTTP 200 在 Interaction COMMIT 后立即返回，现有这条 Interaction 记录，
> 是否已经携带了异步 Worker 完成后续 execution + delivery 所需的全部事实？」**

引用事实面：`interaction_recording.py:152`（`_start` 持久化投影）、
`interaction_recording.py:183`（`_record_success`）、
`interaction.py`（ORM 列）、`interaction_record.py`（Port）、
`persistence/repositories/interaction_record.py`（读写面）、
`inbound_interaction.py`（值对象字段）、`coordinator.py:142`（metadata 合并）、
`wecom.py:389`（`response_url` 入 metadata）/`:234`/`wecom.py:575`（机器人投递）。

---

## 0. 结论（一句话）

**近乎是，但缺一处硬缺口**：现有 PENDING 行携带了重建 `InboundInteraction` 所需的 5 个
核心字段，**但 `InboundInteraction.metadata` 没有被持久化**（`_start` 的
`request_payload` 投影只写了 5 个键，见 §2）。对携带 channel-specific delivery
context 的渠道（WeCom 智能机器人的 `response_url`），Worker 重建出的
`InboundInteraction` 会**丢失投递回执地址** → robot 投递路径无法正确执行
（`coordinator.py:142` 的 metadata 合并退化为 None → `wecom.py:_response_url_from`
返回 None → 错走到 message/send）。**其余全部充分。**

---

## 1. 现状：200 在 `execute` 之后才返回（12.9.1 已确认）

`ChannelInboundService.handle_message`（`channel_inbound.py:94`）同步调用
`runtime.execute(inbound)`，而 `PersistingCoordinator.execute`
（`interaction_recording.py:134`）内部顺序：

```
_start(inbound)        ← create PENDING + received（事务①）
  └─ mark_processing   ← PROCESSING（事务②）      ← ★ 若此处之后立刻 return 200，即本步前提
inner.execute(inbound) ← 六段同步执行（Identity→…→Delivery）
_record_success/failure ← 终态
```

本步假想：**在事务② commit 后、调用 `inner.execute` 之前**就返回 HTTP 200，把
`inner.execute` 连同 delivery 整体交给 Worker。问题于是变成：此刻已落库的 PENDING 行，
Worker 是否能仅凭它（+ DB 其余表）跑完剩余链路？

---

## 2. PENDING 行实际携带了什么

ORM 列（`interaction.py`）：`id` / `channel_id`(FK) / `session_id`(NULL) /
`direction` / `external_interaction_id`(含于 UNIQUE) / `request_payload`(JSON) /
`processing_status=pending` / `delivery_status=pending` / `failure_*`(NULL) /
`retryable=False` / `created_at`。

其中**唯一承载重建载荷的是 `request_payload`**（由 `_start` 注入，`interaction_recording.py:162`）：

```python
request_payload={
    "channel_id":               inbound.channel_id,
    "external_interaction_id":  inbound.external_interaction_id,
    "external_conversation_id": inbound.external_conversation_id,
    "external_identity_id":     inbound.external_identity_id,
    "content":                  inbound.content,
    # ← inbound.metadata 不在此投影中
}
```

对照 `InboundInteraction` 值对象（`inbound_interaction.py:57-66`）的 6 个字段：

| InboundInteraction 字段 | 在 record 中的来源 | 是否充分 |
| --- | --- | --- |
| `channel_id` | 列 + `request_payload` | ✅ |
| `external_interaction_id` | 列 + `request_payload` | ✅ |
| `external_conversation_id` | `request_payload` | ✅ |
| `external_identity_id` | `request_payload` | ✅ |
| `content` | `request_payload` | ✅ |
| `metadata` | **无** | ❌ **缺口** |

→ 重建代码形态（Worker 视角）：

```python
rec = repo.get_by_id(interaction_id)        # 或 find_by_channel_and_external_id 做去重
inbound = InboundInteraction(
    channel_id=rec.channel_id,
    external_interaction_id=rec.external_interaction_id,
    external_conversation_id=rec.request_payload["external_conversation_id"],
    external_identity_id=rec.request_payload["external_identity_id"],
    content=rec.request_payload["content"],
    metadata= ???,   # ← rec.request_payload 没有这个键；InboundInteraction.metadata 永远重建为 None
)
```

---

## 3. 除 metadata 外，为什么其余「充分」

Worker 重建 `InboundInteraction` 后调用 `RuntimeCoordinator.execute(inbound)`
（`coordinator.py:85`），六段所需的**所有其余事实都在 DB（非 Interaction 行）中可达**，
由 `channel_id` + 三个 `external_*` id 索引：

- **IdentityResolution**（`identity_resolution.execute(channel_id, external_identity_id)`）
  ← 两个 id 都在 record。DB `identities` 提供内部 `identity.id`。
- **Routing**（`routing.execute(channel_id)`）← channel_id 在 record。
- **SessionResolution / SessionCreation** ← `identity_id`(上一步) + `connection_id`
  (Routing 产出，DB) + `external_conversation_id`(record)。
- **AgentDispatch**（`dispatch.execute(routing_result, session_id, inbound.content)`）
  ← `inbound.content` 在 record；`Connection`/`Agent` 配置在 DB。
- **Delivery**（`coordinator.py:143` 构造 `OutboundResponse`）←
  `channel_id` / `external_conversation_id`(record) / `content` 已在；**唯独
  `metadata` 来自 `inbound.metadata`（`coordinator.py:142` 合并），缺失则退化**。

即：**execution + delivery 的「输入事实」已由 record 提供 5/6，缺失项仅影响
`OutboundResponse.metadata`。** 对 message/send 渠道（自建应用，不走 response_url）
这个字段本来就为 None，无影响；对 response_url 渠道（机器人）它是**投递回执地址的
唯一载体**，缺失即断链。

---

## 4. 唯一缺口的展开：metadata 不落库 → 机器人投递断链

1. `WeComAdapter._inbound_from_robot_json`（`wecom.py:389`）把 `response_url` 装进
   `InboundInteraction.metadata={RESPONSE_URL_METADATA_KEY: response_url}`。
2. `coordinator.py:142`：`metadata = {**(response.metadata or {}), **(inbound.metadata or {})}`
   —— **channel-originated 优先**；若 `inbound.metadata` 为 None，robot 的回执地址丢失。
3. `WeComAdapter._response_url_from`（`wecom.py:234`）读不到 → 返回 None →
   `deliver` 错走到 `_send_text_message`（message/send，`wecom.py:556`）。
4. 智能机器人**没有** `corpsecret`/`agentid`（其凭据形态不同），且 response_url 才是
   官方唯一主动回复通道（101138）——错走 message/send 要么配置缺失 Fail Closed、
   要么静默错投到自建应用通道。

→ **对机器人渠道，Worker 模式下的 delivery 会失败或错投。** 这是 record 充分性的
唯一硬伤。

---

## 5. 与 12.9.2 / 运行模型的边界

- 本步只问「record 是否携带充分事实」，**不问** duplicate ACK（见 12.9.2）——
  那是 Worker 取行后按状态决策的事；也不问「Worker 如何被调度 / 如何保证单执行」
  （UNIQUE 已保证至多一次 create，见 12.9.2 §前置事实 1；Worker 复用同一行 id
  而非新建）。
- 本步也不要求把 projection 与 `InboundInteraction` 字段面**双向同步**——
  当前是单向「`_start` 显式挑选 5 键」，未来 `InboundInteraction` 增字段时此投影
  必须同步更新（否则无声遗漏，正是 metadata 今天的处境）。

---

## 6. 若后续真要切异步：最小补强（记录，不实现）

让 record 充分，只需一处投影补全（与 12.9.2 的 lookup-before-execute、12.9.1 的
async 边界同属「下一阶段运行模型」，不在本步落地）：

- `interaction_recording.py:162` 的 `request_payload` 增加 `"metadata": inbound.metadata`
  （None 也显式写下，避免重建时歧义） → Worker 即可重建完整 `InboundInteraction`。
- 同时把「`_start` 投影 == `InboundInteraction` 字段面」列为冻结不变式，加单测守护
  （字段增删必须同步）。

**结论复述**：现有 Interaction 记录在「HTTP 200 于 COMMIT 后立即返回」的假设下，
**已基本携带异步 Worker 完成 execution + delivery 所需的全部事实；唯一硬缺口是
`InboundInteraction.metadata` 未持久化，对 WeCom 机器人 response_url 投递路径会造成
断链，需补一行投影。** 其余输入事实（身份/路由/会话/连接/凭据）均由 record 的
`channel_id` + `external_*` id 经 DB 索引可达，无需额外落库。
