"""InboundChannelAdapter 端口（Port，Step 10.2.1 WeCom Inbound Boundary）。

这是 Channel → Control Plane 的**入站适配器边界**（8.1 InboundInteraction 契约的
正向落地；与 9.6 出站 `ChannelAdapter` 完全对称）：

    WeCom Wire ──► InboundChannelAdapter（本端口，实现于 app/adapters/channels/）
                       │  SHA1 / AES / XML / WeCom 字段 —— 全部 Adapter 内部
                       ▼
              InboundInteraction（8.1 冻结契约）
                       ▼
              RuntimeCoordinator（Runtime Core，零改动）

两个能力（10.2.1 裁决：WeCom 的 GET echostr 与 POST message 是两种协议操作，
不塞进一个 receive()——否则 Port 很快开始出现 if handshake 分支并污染 wire 语义）：

- `verify_handshake(...)`  → 返回明文 echostr（WeCom URL 验证握手）；
- `parse_message(...)`     → InboundInteraction | None。None = 已确认接收、
  但该消息类型不产生 CP 交互（MVP 仅支持 text；image/voice/video/event 一律
  acknowledge-ignore，见 step-10.2.1 §MVP 边界）。

HTTP / Wire 语义不进端口：输入输出都是 plain Python values（str / domain 契约）。
FastAPI Request/Response 绝不出现在本端口签名中（test_import_boundaries 守卫）。

入站协议异常（10.2.1 正式裁决：**不进入 Runtime Failure Model**——Runtime Failure
Model 从 InboundInteraction 才开始；这三个异常由 HTTP 边界映射为状态码，不属于
application/errors.py 的 CP 错误码体系）：

- `InboundChannelConfigError` → HTTP 500：渠道不存在 / 未启用 / config 缺验签材料
  （服务端配置错误，不是请求的错）；
- `InboundSignatureError`          → HTTP 403：签名校验失败 / 密文无法解密 / ReceiveId
  不符（不可信请求，拒绝服务）；
- `InboundFormatError`        → HTTP 400：可信但格式非法（坏 XML / 缺必需字段 /
  text 消息内容为空）。

依赖边界：只依赖 app.domain（端口层纪律，test_import_boundaries 守卫）。
"""

from typing import Protocol, runtime_checkable

from app.domain.channel import Channel
from app.domain.inbound_interaction import InboundInteraction


class InboundChannelError(Exception):
    """入站协议错误基类（Adapter-world；不是 CP Runtime Failure Model 成员）。"""


class InboundChannelConfigError(InboundChannelError):
    """渠道不可用：不存在 / 未启用 / config 缺少验签材料（→ HTTP 500）。"""


class InboundSignatureError(InboundChannelError):
    """签名校验失败 / 密文无法解密 / ReceiveId 不符（→ HTTP 403）。"""


class InboundFormatError(InboundChannelError):
    """请求可信但格式非法：坏 XML / 缺必需字段 / 内容为空（→ HTTP 400）。"""


@runtime_checkable
class InboundChannelAdapter(Protocol):
    """Channel → Control Plane 的入站适配器（把外部渠道消息归一化为 CP 契约）。"""

    def verify_handshake(
        self,
        channel: Channel,
        *,
        signature: str,
        timestamp: str,
        nonce: str,
        echostr: str,
    ) -> str:
        """渠道 URL 验证握手（WeCom GET echostr）。

        校验 signature（token+timestamp+nonce+echostr）→ 解密 echostr →
        返回**明文** echostr（由 HTTP 边界原样回给渠道）。

        失败：signature 不符 / 解密失败 → InboundSignatureError；
        channel.config 缺验签材料 → InboundChannelConfigError。
        """
        ...

    def parse_message(
        self,
        channel: Channel,
        *,
        signature: str,
        timestamp: str,
        nonce: str,
        body: str,
    ) -> InboundInteraction | None:
        """渠道消息（WeCom POST）→ InboundInteraction（Runtime 入口契约）。

        验签 → 解密 → 解析 → 按 8.1 契约归一化。None = 已确认但不产生 CP 交互
        （不支持的 MsgType：image/voice/video/event → acknowledge-ignore）。

        失败：签名/解密 → InboundSignatureError；坏 XML/缺字段/空内容 →
        InboundFormatError；config 缺材料 / channel 未持久化 →
        InboundChannelConfigError。
        """
        ...
