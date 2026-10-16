"""Delivery Execution（Step 9.6）—— 8.6 语义的第一次真实执行（Runtime 闭环的最后一段）。

    AgentResponse
        ↓ OutboundResponse（channel_id / external_conversation_id / content / metadata?）
        ↓ ChannelAdapter.deliver(outbound)
        ↓ Channel

核心原则（8.6 延续）：**Delivery is transport, not routing.**

- 只消费 OutboundResponse——channel_id 来自它、external_conversation_id **原样**
  交给 Adapter、content **原样**交付；
- 不重新 Routing / 不重新 Identity Resolution / 不重新 Session Resolution /
  不重新寻找 Agent / 不读取 Connection 来决定目的地；
- Adapter transport/protocol 失败 → `ChannelDeliveryFailedError`
  （CHANNEL_DELIVERY_FAILED）——不新造 ChannelNotFound / ChannelAuthFailed /
  ChannelTimeout / ChannelRateLimited / ChannelTransportFailed；
- Delivery failure 是 **delivery** 失败，不变成 processing failure
  （processing_status ≠ delivery_status，见 4.2/5.3）；
- 不直接访问 ORM / SQLAlchemy；不创建 DeliveryService / ChannelManager；
  ChannelAdapter 实现（含 WeComAdapter）不在此落地——本阶段用 Test Adapter 注入。

依赖边界：只依赖 `ChannelAdapter` Port（app/repositories/channel_adapter.py），
**不依赖 UoW / Repository**——Delivery 不需要读库。
"""

from app.application.errors import ChannelDeliveryFailedError
from app.domain.outbound_response import OutboundResponse
from app.repositories.channel_adapter import ChannelAdapter


class DeliveryExecutor:
    """Delivery 执行组件：把 OutboundResponse 交还给 Channel。

    只消费已确定的 outbound，不做任何决策 / 选路 / 解析。
    """

    def __init__(self, channel_adapter: ChannelAdapter) -> None:
        self._channel_adapter = channel_adapter

    def execute(self, outbound: OutboundResponse) -> None:
        """把 outbound 交给 Channel Adapter；失败 → Fail Closed 抛错。"""
        # ---- 输入 Fail Closed（只消费 8.6 契约字段）----
        if not isinstance(outbound, OutboundResponse):
            raise ChannelDeliveryFailedError()
        if not isinstance(outbound.channel_id, int) or outbound.channel_id <= 0:
            raise ChannelDeliveryFailedError(
                external_conversation_id=outbound.external_conversation_id)
        if not isinstance(outbound.external_conversation_id, str) \
                or not outbound.external_conversation_id.strip():
            raise ChannelDeliveryFailedError(channel_id=outbound.channel_id)
        if not isinstance(outbound.content, str) or not outbound.content.strip():
            raise ChannelDeliveryFailedError(
                channel_id=outbound.channel_id,
                external_conversation_id=outbound.external_conversation_id,
            )
        if outbound.metadata is not None and not isinstance(outbound.metadata, dict):
            raise ChannelDeliveryFailedError(
                channel_id=outbound.channel_id,
                external_conversation_id=outbound.external_conversation_id,
            )

        # ---- 交还给 Channel Adapter（外部 I/O）----
        try:
            self._channel_adapter.deliver(outbound)
        except ChannelDeliveryFailedError:
            raise
        except Exception as exc:  # transport / protocol / auth → CHANNEL_DELIVERY_FAILED
            raise ChannelDeliveryFailedError(
                channel_id=outbound.channel_id,
                external_conversation_id=outbound.external_conversation_id,
            ) from exc
