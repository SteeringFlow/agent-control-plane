"""MultiChannelAdapter —— channel.type → Channel Adapter 分派（Step 15.2.3）。

位置：`app/adapters/channels/registry.py`。同时实现两个 Port：

    ChannelInboundService ─┐
    DeliveryExecutor      ─┴→ (Port) → MultiChannelAdapter
                                          ├── "wecom" → WeComAdapter
                                          └── "local" → LocalChannelAdapter

**Runtime 不认识 wecom / local 的存在**：`ChannelInboundService` 与
`DeliveryExecutor` 各自只看见一个 `InboundChannelAdapter` / `ChannelAdapter`；
渠道类型集合完全收口在组合根（`main.py`）。新增第三种渠道只需在此登记映射，
不改 Runtime / Executor / Domain / Persistence。

分派依据：
- 入站 `verify_handshake` / `parse_message`：**channel 参数在手**，直接读渠道
  类型字段分派（无需查库）；
- 出站 `deliver(outbound)`：只有 `channel_id`，经 UoW 读回渠道类型后分派
  （与 `WeComAdapter._load_channel_for_send` 同一模式）。

命名纪律（6.x / 8.x 守卫）：本模块不是 Router / Resolver / Dispatcher ——
不选渠道、不做规则匹配；只在已确定的渠道上选择协议实现。

未知类型 → 入站 `InboundChannelConfigError`（HTTP 500）、出站
`ChannelDeliveryFailedError`——都是既有错误码，不新造。
"""

from collections.abc import Callable

from app.application.errors import ChannelDeliveryFailedError
from app.domain.channel import Channel
from app.domain.inbound_interaction import InboundInteraction
from app.domain.outbound_response import OutboundResponse
from app.repositories.channel_inbound import (
    InboundChannelAdapter,
    InboundChannelConfigError,
)
from app.repositories.unit_of_work import UnitOfWork


class MultiChannelAdapter:
    """按渠道类型把入站 / 出站交给对应 Adapter（组合根装配的唯一渠道出口）。"""

    def __init__(
        self,
        adapters: dict[str, InboundChannelAdapter],
        *,
        uow_factory: Callable[[], UnitOfWork],
    ) -> None:
        self._adapters = dict(adapters)
        self._uow_factory = uow_factory

    def _for_channel_type(self, channel_type: str | None):
        return self._adapters.get(channel_type)

    # ================================================================ 入站

    def verify_handshake(
        self,
        channel: Channel,
        *,
        signature: str,
        timestamp: str,
        nonce: str,
        echostr: str,
    ) -> str:
        adapter = self._for_channel_type(channel.type)
        if adapter is None:
            raise InboundChannelConfigError(
                f"channel_id={channel.id} 无可用入站 Adapter：type={channel.type!r}")
        return adapter.verify_handshake(
            channel, signature=signature, timestamp=timestamp,
            nonce=nonce, echostr=echostr)

    def parse_message(
        self,
        channel: Channel,
        *,
        signature: str,
        timestamp: str,
        nonce: str,
        body: str,
    ) -> InboundInteraction | None:
        adapter = self._for_channel_type(channel.type)
        if adapter is None:
            raise InboundChannelConfigError(
                f"channel_id={channel.id} 无可用入站 Adapter：type={channel.type!r}")
        return adapter.parse_message(
            channel, signature=signature, timestamp=timestamp,
            nonce=nonce, body=body)

    # ================================================================ 出站

    def deliver(self, outbound: OutboundResponse) -> None:
        channel_type = self._load_channel_type(outbound.channel_id)
        adapter = self._for_channel_type(channel_type)
        if adapter is None:
            raise ChannelDeliveryFailedError(
                channel_id=outbound.channel_id,
                external_conversation_id=outbound.external_conversation_id)
        return adapter.deliver(outbound)

    def _load_channel_type(self, channel_id: int) -> str | None:
        """按 channel_id 读回渠道类型（只读，用于选择交付实现）。"""
        if not isinstance(channel_id, int) or channel_id <= 0:
            raise ChannelDeliveryFailedError()
        if self._uow_factory is None:
            raise ChannelDeliveryFailedError(channel_id=channel_id)
        try:
            with self._uow_factory() as uow:
                channel = uow.channels.get_by_id(channel_id)
        except ChannelDeliveryFailedError:
            raise
        except Exception as exc:  # noqa: BLE001  读库失败 → 交付失败（Fail Closed）
            raise ChannelDeliveryFailedError(channel_id=channel_id) from exc
        if channel is None:
            raise ChannelDeliveryFailedError(channel_id=channel_id)
        return channel.type
