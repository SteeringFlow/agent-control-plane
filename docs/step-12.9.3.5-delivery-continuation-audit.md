# Step 12.9.3.5 — Delivery Continuation Audit

> 只读审计（承接 12.9.1~12.9.3.4）。问题：ACK 与 Agent execution 分离后，**Worker 能否
> 仅凭 Interaction 的持久化事实完成 Delivery？** 重点是 `response_url` 在 Worker 重建时
> 是否丢失，以及三个冻结状态语义在异步下是否仍成立。
>
> 证据基线：
> - 入站承载：`adapters/channels/wecom.py:378-390`（机器人 inbound 把 `response_url`
>   置入 `inbound.metadata`）、`:234-248`（`_response_url_from` 读 `OutboundResponse.metadata`）
> - 引擎合并：`application/coordinator.py:142`（metadata 合并）、`:149`（delivery.execute）
> - Delivery：`application/delivery.py:64`（adapter.deliver）
> - 持久化请求快照：`application/interaction_recording.py:162-173`（`_start` 的 `request_payload`）
> - 状态映射：`application/interaction_recording.py:98-108`（`STAGE_FAILURE_MAP`）、`:240`（retryable 传播）
> - 契约：`domain/inbound_interaction.py:61-66`、`domain/outbound_response.py:70-73`

---

## 0. 同步链路（现状）Delivery 数据流还原

```
WeComAdapter.parse_message
  └─ _inbound_from_robot_json (wecom.py:383-390)
       → InboundInteraction(metadata={"response_url": <url>})   # response_url 在 inbound.metadata
            ↓ ChannelInboundService.handle_message
            ↓ RuntimeCoordinator.execute
                 metadata = {**response.metadata, **inbound.metadata}   # coordinator.py:142
                 outbound = OutboundResponse(..., metadata=metadata)
                 self._delivery.execute(outbound)                        # coordinator.py:149
                      ↓ DeliveryExecutor.execute (delivery.py:64)
                      channel_adapter.deliver(outbound)
                           ↓ WeComAdapter.deliver (wecom.py:551)
                           response_url = _response_url_from(outbound)   # :234-248 读 metadata["response_url"]
                           ├─ 有 → _deliver_via_response_url (POST response_url)   # :552-554
                           └─ 无 → message/send（corpsecret/agentid 路径）         # 错误回退！见下
```

关键事实：
- `response_url` **只**经由 `inbound.metadata` 进入系统（`wecom.py:389`），并经
  `coordinator.py:142` 的合并进入 `OutboundResponse.metadata`，再被
  `WecomAdapter.deliver` 通过 `_response_url_from` 取出（`:551`）。
- 模块纪律（`:350`、**Fail Closed**）：机器人 `response_url` 是智能机器人**唯一**合法
  回复通道，**严禁**静默回退到自建应用 `message/send`（混用会静默错投）。
- 入站若缺 `response_url`，机器人路径在 `parse_message` 阶段即 `InboundChannelConfigError`
  Fail Closed（`:378-382`）。

---

## A. 关键发现：当前 `request_payload` 未持久化 `inbound.metadata` → `response_url` 必丢

`PersistingCoordinator._start` 构造的 `request_payload`（`interaction_recording.py:162-173`）：

```python
request_payload={
    "channel_id": inbound.channel_id,
    "external_interaction_id": inbound.external_interaction_id,
    "external_conversation_id": inbound.external_conversation_id,
    "external_identity_id": inbound.external_identity_id,
    "content": inbound.content,
    # ← 没有 "metadata": inbound.metadata ！！！
},
```

- `InboundInteraction` 的全部字段（`inbound_interaction.py:61-66`）：`channel_id` /
  `external_interaction_id` / `external_conversation_id` / `external_identity_id` /
  `content` / **`metadata`**。前 5 个都在 `request_payload` 里，**唯独 `metadata` 缺失**。
- 因此：**异步 Worker 若从持久化 `request_payload` 重建 `InboundInteraction`，其
  `metadata` 为 `None` → `response_url` 丢失**。

**后果（同步链路不会触发、异步必触发）**：
- Worker 重建的 `InboundInteraction.metadata=None` → `coordinator.py:142` 合并后
  `OutboundResponse.metadata` 不含 `response_url` → `WeComAdapter._response_url_from`
  返回 `None`（`:234-248`）→ `deliver` 落入 `message/send` 分支（`:556` 起）。
- 对**智能机器人**消息，这是 `wecom.py:350` 明文禁止的「静默错投」：本应 POST
  `response_url`，却用 corpsecret/agentid 走自建应用通道——且因绕过了入站
  `parse_message` 的 Fail Closed（重建不经 wire 解析），**不会报错，只静默发错地方**。
- 这正是用户提出的假设：**「metadata 必须进入 Interaction recording，否则 response_url
  在 Worker reconstruction 时会丢失」——已被代码证实成立。**

→ **A 答：当前持久化快照不足以支撑异步 Delivery。缺口 = `request_payload` 未存
  `inbound.metadata`，导致 `response_url` 在 Worker 重建时丢失，机器人回复静默错投。**

---

## B. Worker 能否仅凭持久化事实完成 Delivery？

**答：能——但前提是补上 `inbound.metadata` 的持久化。**

Worker 在异步模型下（12.9.3.3 §9）执行 `claim → 重建 InboundInteraction →
RuntimeCoordinator.execute(inbound) → record_outcome`。Delivery 所需全部事实来自两处：

| Delivery 需要的 OutboundResponse 字段 | 来源 | 异步是否可得 |
| --- | --- | --- |
| `channel_id` | `inbound.channel_id` | ✓ `request_payload` 已存 |
| `external_conversation_id` | `inbound.external_conversation_id` | ✓ 已存 |
| `content` | `AgentResponse.content`（Dispatch **重新生成**） | ✓ 引擎重生，不依赖 DB |
| `metadata` | 合并 `AgentResponse.metadata`（重生）+ `inbound.metadata`（**须持久化**） | ✗ 当前 `inbound.metadata` 丢失 |

- `content` / `AgentResponse` 由 Worker 调 `RuntimeCoordinator.execute` 时**重新生成**
  （Agent 被引擎重新调用一次，符合 12.9.3.3「Worker = 唯一执行驱动者」），**不依赖**
  持久化的 `response_payload`（后者只是 Evidence，见 12.9.3.1）。所以 Agent 响应不是
  重建瓶颈。
- 唯一瓶颈是 `inbound.metadata`：它包含 `response_url`，必须来自持久化快照。当前
  `request_payload` 缺它 → Delivery 无法选对通道。
- **补齐后**：Worker 用 `request_payload["metadata"]` 重建
  `InboundInteraction(metadata=...)`，引擎合并 → `OutboundResponse.metadata` 含
  `response_url` → `WeComAdapter.deliver` 正确走 `_deliver_via_response_url`。**Worker
  即可仅凭持久化事实完成 Delivery。**

→ **B 答：能，但当前不行。阻断性缺口 = `inbound.metadata` 未持久化；补一行
  `"metadata": inbound.metadata` 即可让 Worker 完整重建 Delivery 上下文。**

---

## C. 冻结状态语义在异步 Worker 中是否仍完全成立？

**答：三个组合全部仍然成立。** 因为状态矩阵由**异常类型 → (processing, delivery) 映射**
驱动，与同步/异步无关——Worker 只是换了执行驱动者，引擎行为与映射表不变。

`STAGE_FAILURE_MAP`（`interaction_recording.py:98-108`）以 **typed exception 类型** 为
键；Worker 的 `record_outcome` 沿用同一张表（12.9.3.3 §7/§8：终态由 Worker 记录，逻辑
等同当前 `_record_success/_record_failure`）。因此：

| 组合 | 触发条件（引擎行为，异步不变） | 异步下是否成立 |
| --- | --- | --- |
| **SUCCEEDED × DELIVERED** | `RuntimeCoordinator.execute` 返回 result（Delivery 成功） | ✓ 成立（Worker 记 `processing=SUCCEEDED, delivery=DELIVERED`） |
| **SUCCEEDED × FAILED** | 引擎抛 `ChannelDeliveryFailedError`（Dispatch 已成功、Delivery 失败，4.2/5.3） | ✓ 成立（映射 `STAGE_DELIVERY` → `processing=SUCCEEDED, delivery=FAILED`，即 10.7.2 B2） |
| **FAILED × PENDING** | 引擎抛其它 typed error（Identity/Routing/Session/Dispatch 在 Delivery 前 STOP） | ✓ 成立（映射 → `processing=FAILED, delivery=PENDING`，Delivery 从未被调用） |

补充验证：
- **`retryable` 语义保留**：`response_url` 一次性（`:585` `retryable=False`）由
  typed error 实例携带，Worker 经 `getattr(exc,"retryable",...)` 传播
  （`interaction_recording.py:240`），异步下一致。
- **Delivery 失败 ≠ Processing 失败** 仍成立：`ChannelDeliveryFailedError` 在 Delivery
  阶段抛出，processing 仍 SUCCEEDED（outbound_response.py:42-45、delivery.py:17-18 纪律
  不变）。
- **Fail Closed 不变**：Delivery 缺 `response_url` 时，异步下若 `metadata` 已补齐则正确
  走 response_url；若未补齐（即本审计暴露的缺口）则静默错投——这是**持久化缺口导致的事实
  缺失**，不是状态矩阵的语义变化；矩阵本身仍正确。

→ **C 答：SUCCEEDED×DELIVERED、SUCCEEDED×FAILED、FAILED×PENDING 在异步 Worker 中
   全部仍然成立；矩阵由异常类型驱动，与执行驱动者无关。需警惕的是「未补齐 metadata」
   会让 SUCCEEDED×FAILED 在错误的 Delivery 通道上发生（静默错投），但这归 B 节的持久化
   缺口，不归状态语义。**

---

## D. 最小修复（仅记录，不实现）

在 `PersistingCoordinator._start`（`interaction_recording.py:162-173`）的 `request_payload`
中加入 `inbound.metadata`：

```python
request_payload={
    "channel_id": inbound.channel_id,
    "external_interaction_id": inbound.external_interaction_id,
    "external_conversation_id": inbound.external_conversation_id,
    "external_identity_id": inbound.external_identity_id,
    "content": inbound.content,
    "metadata": inbound.metadata,   # ← 新增：保活 response_url 等 channel context
},
```

- **零 schema 变更**：`request_payload` 是 JSON 列，`dict | None` 直接可序列化；
- **不改引擎/契约**：`InboundInteraction`、`OutboundResponse`、`WeComAdapter` 全部不变；
- **对称完整**：使 `request_payload` 与 `InboundInteraction` 字段对齐（仅差 `id/status`
  等运行态字段），Worker 可 1:1 重建。
- 归属：这是 12.9.3.3 §1「intake 边界负责 PENDING 创建」应写入的快照内容的一部分；
  与「构建 Worker」同属未来动作，现在**只记录、不预先实现**（12.8.6 纪律）。

---

## 速查表

| 问题 | 答案 |
| --- | --- |
| A. `request_payload` 是否存了 `metadata` | **否**（`interaction_recording.py:162-173` 缺 `metadata`）→ `response_url` 在 Worker 重建时丢失，机器人回复静默错投 |
| B. Worker 仅凭持久化事实能否完成 Delivery | 能，但当前不行；阻断缺口 = `inbound.metadata` 未持久化；补一行即完整重建 |
| C. 三状态语义异步下是否成立 | **全部成立**；矩阵由 typed-exception 映射驱动，与执行驱动者无关 |
| D. 最小修复 | `_start` 的 `request_payload` 加 `"metadata": inbound.metadata`；零 schema 变更、不动引擎 |
