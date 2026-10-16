"""ChannelAdapter 端口（Port，Step 9.6 Delivery Execution）。

这是 Control Plane → Channel 的**出站适配器边界**（8.6 的反方向）：描述「Runtime
需要把一个已确定的 OutboundResponse 交还给 Channel」的能力。只描述形状，不含实现：

    app/application/delivery.py   ← 依赖本端口（不依赖具体 Adapter）
    app/adapters/channels/…       ← 未来实现（9.6 阶段用 Test Adapter 注入）

`deliver(outbound)`：
- `outbound`：8.6 冻结的 OutboundResponse（channel_id / external_conversation_id /
  content / metadata?）。Delivery **只消费** 它——不重新找 User / Identity /
  Connection / Agent，不读取 Connection 来决定目的地；
- Adapter 自己负责 channel protocol authentication / serialization / transport /
  protocol-specific delivery。

失败语义：本端口不引入新的底层错误（无 ChannelNotFound / ChannelAuthFailed /
ChannelTimeout / ChannelRateLimited / ChannelTransportFailed）——底层失败由
Executor 统一映射到既有的 CHANNEL_DELIVERY_FAILED。
"""

from typing import Protocol, runtime_checkable

from app.domain.outbound_response import OutboundResponse


@runtime_checkable
class ChannelAdapter(Protocol):
    """Control Plane → Channel 的出站适配器（交还 OutboundResponse）。"""

    def deliver(self, outbound: OutboundResponse) -> None:
        """把已确定的 OutboundResponse 交还给对应 Channel 的 conversation。

        成功 → 返回（投递完成）；
        失败（transport / protocol / auth）→ 抛异常，由 Executor 映射为
        `CHANNEL_DELIVERY_FAILED`（不吞掉失败）。
        """
        ...
