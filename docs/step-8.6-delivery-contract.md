# Step 8.6 — Delivery Contract & Boundary

> Runtime 出口契约：`AgentResponse → OutboundResponse → ChannelAdapter → External Channel`。
> 本步只冻结契约与边界，不实现 DeliveryService / ChannelAdapter / 任何协议 Adapter。

## 定位

8.1~8.5 描述 Control Plane 如何把 inbound interaction 处理到 AgentResponse；
8.6 解决反方向：AgentResponse 如何重新成为 Channel 可以理解的 Outbound Response。

至此核心价值链第一次完整闭合：

```
Channel → Control Plane → Agent → Control Plane → Channel
```

## 核心原则

**Delivery is transport, not routing.**

| 职责 | 归属 |
| --- | --- |
| Routing | 决定去哪（8.4 select connection_id / agent_id） |
| Dispatch | 发送给 Agent（8.5 execution not selection） |
| Delivery | 把 Agent 结果交还给 Channel（8.6，不重新选路） |

Delivery **不重新 Routing / 不重新 Identity Resolution / 不重新 Session Resolution**。

## 契约冻结

`app/domain/outbound_response.py`

```python
@dataclass(frozen=True)
class OutboundResponse:
    channel_id: int              # 目标 Channel.id（Routing 已确定，只消费）
    external_conversation_id: str  # Channel-side conversation（回给谁）
    content: str
    metadata: dict | None = None
```

不携带：`user_id / identity_id / session_id / connection_id / agent_id / routing_rule`
（Delivery 不需要；归属决策已完成，不允许把具体 IM 协议硬编码进 Runtime）。

## 边界剥离（对称性）

Inbound：`Channel → InboundInteraction`
Outbound：`AgentResponse → OutboundResponse → Channel`

```
External World
      │
      ▼
┌─────────────────┐
│ Control Plane   │
│  Inbound → Runtime → Outbound │
└─────────────────┘
```

- OutboundResponse = InboundInteraction − {external_interaction_id, external_identity_id}
  （ingress 专属 identity/trace 字段不回流出口）；
- AgentResponse（Agent → CP，external_session_id = Agent-side reference）与
  OutboundResponse（CP → Channel，external_conversation_id = Channel-side
  conversation）不合并。

## Failure semantics

```
AgentResponse → Delivery → Channel Adapter
    ├── success
    └── failure → CHANNEL_DELIVERY_FAILED
```

- `CHANNEL_DELIVERY_FAILED` 归 OutboundResponse（Channel Delivery 失败），
  与 dispatch_failure 的 `AGENT_*`（Agent Dispatch 失败）分属不同 stage；
- `processing_status ≠ delivery_status`：两独立状态（4.2/5.3 已冻结在 interactions
  表）。Agent 成功 + 渠道投递失败是完全合法的组合，8.6 在既有设计上闭环。

## Fail Closed

- 拿不到确定 Channel / conversation → 不投递 → `CHANNEL_DELIVERY_FAILED`；
- 不「猜一个 conversation 发出去」，不重选 Connection 重投。

## 不做（下一阶段）

DeliveryService / ChannelAdapter implementation / WeComAdapter / FeishuAdapter /
WeChatAdapter / SlackAdapter / HTTP channel / real webhook / real message sending /
retry system / queue / async worker。

## 验收

`tests/test_step8_6_delivery_contract.py`（18 项）：模块纯净 / 字段面冻结 /
不重新选路 / Inbound-Outbound 对称 / ChannelAdapter 边界 / Failure semantics /
双状态独立 / Fail Closed / mutation 探测（DeliveryService、WeComAdapter、
ChannelAdapter 偷渡必须报红）。全仓 `pytest -q` = 400 passed，lint 0。
