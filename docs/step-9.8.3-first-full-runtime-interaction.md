# Step 9.8.3 — First Full Runtime Interaction

> 第一条完整的 **Inbound → Outbound** 闭环：一条消息真的从 Channel 进入，完整穿过
> 整个 Control Plane，再回到 Channel。这是 Agent Control Plane 第一个 Runtime
> Vertical Slice 真正跑起来的证明。

## 链路（Test Channel / Test Agent，不接真实集成）

```
Test Channel Input
    ↓ InboundInteraction
    ↓ RuntimeCoordinator.execute()
    Identity → Routing → Session → [Session Creation] → Dispatch
    ↓ AgentResponse → OutboundResponse
    ↓ Test Channel Output
```

不碰 ACP 协议 / WeCom API / HTTP / OAuth / Webhook / Streaming——重点测试整个
**Runtime Control Flow**。

## E2E ① REUSE_ACTIVE（预置 Active Session）

预置 User/Identity/Channel/Agent/Connection/Active Session → 完整跑一遍，证明：

- `input.external_conversation_id == output.external_conversation_id`（回显）；
- `AgentRequest.control_plane_session_id == existing_session.id`；
- `session_count_before == session_count_after`（REUSE 不创建）。

## E2E ② CREATE_NEW

- `session_count_after == session_count_before + 1`；
- `created_session.id == AgentRequest.control_plane_session_id`（DB 行验证）；
- `OutboundResponse.channel_id == InboundInteraction.channel_id`；
- `OutboundResponse.external_conversation_id == InboundInteraction.external_conversation_id`；
- `input="hello"` → 完整穿过 → 拿到 `"test response"`。

## 边界闭环（第一次在 E2E 验证）

- **Identity Isolation**：channel + wx_a → User A 的 Identity，绝不落到 wx_b；
- **Routing Isolation**：Channel A → Connection A → Agent A，绝不变成 Agent B；
- **Session Isolation**：Identity A + conversation-X ≠ Identity A + conversation-Y；
- **Agent Session Reference**：CP Session id ≠ Agent `external_session_id`（共存不混淆）；
- **Outbound Boundary**：出口只有 `channel_id / external_conversation_id / content /
  metadata`，不带 `user_id / identity_id / connection_id / agent_id /
  control_plane_session_id`（8.6 出口边界在 E2E 验证）。

## 失败链（真实执行器，不重造错误机制）

- Identity 失败 → STOP（整链不动）；
- Routing 失败 → STOP（Identity 已过、无 Session/Dispatch/Delivery）；
- Dispatch 失败 → NO Delivery；
- Delivery 失败 → Dispatch 已成功（请求已发）后抛 `ChannelDeliveryFailedError`——
  再次证明 processing ≠ delivery 语义。

## 范围边界

**不持久化 Interaction/Activity**：interactions / activities 表在 schema 中
「存在」≠ Runtime 现在必须使用它们——审计持久化由真实需求驱动。测试锁定：Runtime
组件（coordinator + 六 executor）不 import `app.persistence`、不引用
InteractionModel / ActivityModel / InteractionRepository / ActivityRepository。

## 验收

`tests/test_step9_8_3_first_full_runtime_interaction.py`（13 项）。全仓
`pytest -q` = 578 passed，lint 0。成功标准：`"hello"` → Test Channel → … →
Agent `"test response"` → Test Channel，全程无越层 / 无重路由 / 无重解析 / 无
Session 泄漏 / 无错误吞掉 / 无 Delivery 越权 / 无新增未来架构。
