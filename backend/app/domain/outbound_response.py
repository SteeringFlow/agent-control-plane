"""OutboundResponse —— Runtime 出口契约（Step 8.6 Delivery Contract & Boundary）。

一句话定位：**OutboundResponse = Delivery 把 AgentResponse 归一化后交还给 Channel
的交还消息**（Runtime 出口的标准 Contract，与 8.1 InboundInteraction 对称）：

    8.1 Inbound : External Channel → ChannelAdapter → InboundInteraction → Runtime
    8.6 Outbound : AgentResponse → Delivery → OutboundResponse → ChannelAdapter
                  → External Channel

**Delivery is transport, not routing**：
    Routing  = 决定去哪（8.4 已完成：select connection_id / agent_id）
    Dispatch = 发送给 Agent（8.5 已完成：execution not selection）
    Delivery = 把 Agent 的结果交还给 Channel——**不重新 Routing**、不重新
    Identity Resolution、不重新 Session Resolution。

字段语义（告诉 Channel Adapter：把这段内容发送到这个 Channel 的这个 Conversation）：
    - `channel_id`：目标 Channel.id（Routing/Connection 已确定，Delivery 只消费）；
    - `external_conversation_id`：Channel-side conversation（回给谁）；
    - `content`：要发送的文本；
    - `metadata`：可选结构化扩展（与 InboundInteraction.metadata 同纪律——不承载
      凭据 / 策略 / 路由信息）；
    - `artifacts`：**ACP V0.3 Phase 1 加性字段**——Agent 产出的工作成果**引用**
      （`list[app.domain.artifact.Artifact]`）。它是**引用**，不是字节：Control Plane
      **不取回、不存储、不代理**其指向的资源（契约见
      `docs/step-acp-v0.3-artifact-outbound-implementation-gate.md` §6.4）。
      默认 `None` ⇒ 旧调用方与旧出站消息**逐字节不变**。

**不携带**（Delivery 不需要知道这些）：
    user_id / identity_id / session_id / connection_id / agent_id / routing_rule
    ——归属决策已在 8.2~8.4 完成，Outbound 不再复述，也不允许把某具体 IM 协议
    硬编码进 Control Plane。

与 AgentResponse 的边界：
    - AgentResponse  = Agent → Control Plane 的归一化响应（external_session_id =
      Agent-side reference）；
    - OutboundResponse = Control Plane → Channel 的交还消息（channel-scoped，
      external_conversation_id = Channel-side conversation）。
    两者不合并：Agent 内部会话与 Channel 会话是两个世界的 id。

**Delivery failure semantics**：
    AgentResponse
        ↓ Delivery
        ↓ Channel Adapter
        ├── success
        └── failure → CHANNEL_DELIVERY_FAILED

    processing_status ≠ delivery_status（4.2/5.3 已在 interactions 表冻结为两个
    独立状态，不得合成 status）：
        Agent 已成功（processing=SUCCEEDED）→ 渠道投递失败（delivery=FAILED）
    是完全合法的状态组合。

Fail Closed：
    - 拿不到确定 Channel / conversation → **不投递** → CHANNEL_DELIVERY_FAILED；
    - 绝不「猜一个 conversation 发出去」或「重选一条 Connection 重投」。
"""

from dataclasses import dataclass

# Runtime failure boundary：渠道投递失败（与 Agent Dispatch 失败码同属 Runtime
# failure boundary，但 stage 不同——Agent Dispatch 在前，Channel Delivery 在后）。
CHANNEL_DELIVERY_FAILED = "CHANNEL_DELIVERY_FAILED"


@dataclass(frozen=True)
class OutboundResponse:
    """Delivery 交还给 Channel 的消息（值对象，不落库）。

    字段语义：
    - `channel_id`：目标 Channel.id（已被 Routing/Connection 确定，Delivery 只消费）；
    - `external_conversation_id`：Channel-side conversation（回给谁）；
    - `content`：要发送的文本；
    - `metadata`：可选结构化扩展；
    - `artifacts`：可选的 Agent 产出的**成果引用**列表（默认 None）。

    ACP V0.3 Phase 1：本模块**刻意不 import** `app.domain.artifact`——元素类型在
    上文 docstring 声明为 `list[Artifact]`，以维持 8.6「本模块仅 stdlib」的
    导入纪律（只依赖 dataclasses）。
    """

    channel_id: int
    external_conversation_id: str
    content: str
    metadata: dict | None = None
    artifacts: list | None = None
