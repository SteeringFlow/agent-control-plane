"""LocalChannelAdapter —— external-system-free Channel Adapter（Step 15.2.3）。

位置：`app/adapters/channels/local.py`。同时实现两个 Port：
- 入站 `InboundChannelAdapter`（Port 在 `app/repositories/channel_inbound.py`）；
- 出站 `ChannelAdapter`（Port 在 `app/repositories/channel_adapter.py`）。

用途：`type="local"` 渠道 —— 让 OSS V0.x 在**无企业微信、无公网 HTTPS、无回调
验签**的前提下拥有一个真实入站入口，并走完与 WeCom 完全相同的 Runtime 链：

    POST /api/channels/{id}/local/message  {"external_id": "...", "text": "..."}
        ↓ parse_message（纯 JSON：无 SHA1 / AES / XML）
        ↓ InboundInteraction（8.1 冻结契约，与 WeCom 同一形状）
        ↓ ChannelInboundService → PENDING + COMMIT → HTTP 200 ACK
        ↓ （响应之后）Worker → RuntimeCoordinator（完整六段）
        ↓ LocalChannelAdapter.deliver(outbound)

**入站 wire（本地开发契约，非外部协议）**：
    外层  application/json（请求体原文即 message body）
    字段  external_id  必填 → external_identity_id / external_conversation_id
                             （本地单聊：conversation == identity，与 WeCom 单聊裁决同形）
          text         必填 → content
          message_id   可选 → external_interaction_id；缺省生成 uuid4
                             （幂等/去重依据：显式携带可做状态感知重复接纳）
    失败：非 JSON / 非对象 / 缺字段 / 空白 → `InboundFormatError`（HTTP 400）。

**出站 delivery sink**：`deliver(outbound)` 把响应追加写入**本地 outbox**
（append-only JSONL）。这是 local channel 的交付副作用——与 WeCom 的 HTTP POST
在 Adapter 层对称。**durable 的读取出口仍是既有 Observation**
（Interaction 的响应快照 + `delivery_status=DELIVERED`，由
`PersistingCoordinator.record_success` 在 `deliver()` 成功返回后落库）。

本 Adapter **不修改持久化 schema、不读 ORM、不认识 Runtime**：
- outbox 只是 Adapter 层的本地文件 sink，不进数据库、不进 8.6 契约字段面；
- 交付成功/失败语义由既有 `ChannelDeliveryFailedError` 承载（不新造错误码）。
"""

import json
import uuid
from pathlib import Path

from app.application.errors import ChannelDeliveryFailedError
from app.domain.artifact_reference import artifact_reference_records
from app.domain.channel import Channel
from app.domain.inbound_interaction import (
    CHAT_TYPE_METADATA_KEY,
    CHAT_TYPE_SINGLE,
    InboundInteraction,
)
from app.domain.outbound_response import OutboundResponse
from app.repositories.channel_inbound import InboundFormatError

#: 缺省 outbox 路径（相对 CWD = backend/，与 DATABASE_URL 的 ./data/ 约定一致）。
DEFAULT_OUTBOX_PATH = Path("data") / "local_outbox.jsonl"


class LocalChannelAdapter:
    """`type="local"` 双向 Adapter（入站 JSON / 出站本地 outbox）。

    `outbox_path`：本地交付 sink 的目标文件；缺省 `data/local_outbox.jsonl`。
    测试可注入 tmp 路径，避免污染工作区。
    """

    def __init__(self, *, outbox_path: str | Path | None = None) -> None:
        self._outbox_path = (
            Path(outbox_path) if outbox_path is not None else DEFAULT_OUTBOX_PATH
        )

    # ================================================================ 入站（InboundChannelAdapter）

    def verify_handshake(
        self,
        channel: Channel,
        *,
        signature: str,
        timestamp: str,
        nonce: str,
        echostr: str,
    ) -> str:
        """本地渠道无验签语义：原样回显 echostr（与 WeCom 握手对称的可用占位）。

        不存在「不可信请求」概念（本地开发入口），因此不抛 InboundSignatureError。
        """
        return echostr

    def parse_message(
        self,
        channel: Channel,
        *,
        signature: str,
        timestamp: str,
        nonce: str,
        body: str,
    ) -> InboundInteraction | None:
        """本地 JSON 消息 → InboundInteraction（Fail Closed）。

        不产生 CP 交互的消息类型在本 wire 上不存在（无 image/event 概念）——
        永远返回 InboundInteraction 或抛 InboundFormatError，绝不返回 None。
        """
        if channel.id is None:
            raise InboundFormatError(
                "未持久化的 Channel（无 id）不能承接入站消息")
        if not isinstance(body, str) or not body.strip():
            raise InboundFormatError(f"channel_id={channel.id} local 消息体为空")

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise InboundFormatError(
                f"channel_id={channel.id} local 消息不是合法 JSON") from exc
        if not isinstance(payload, dict):
            raise InboundFormatError(
                f"channel_id={channel.id} local 消息必须是 JSON 对象")

        external_id = payload.get("external_id")
        text = payload.get("text")
        if not isinstance(external_id, str) or not external_id.strip():
            raise InboundFormatError(
                f"channel_id={channel.id} local 消息缺少 external_id")
        if not isinstance(text, str) or not text.strip():
            raise InboundFormatError(
                f"channel_id={channel.id} local 消息缺少 text")

        message_id = payload.get("message_id")
        if not isinstance(message_id, str) or not message_id.strip():
            message_id = uuid.uuid4().hex

        sender = external_id.strip()
        # M6.6-C3 / ADR-12 §13：local 是**单会话**形态（conversation == identity），
        # 由 Adapter 显式声明（Core 不做「两键不等 ⇒ 群聊」的推断）。
        return InboundInteraction(
            channel_id=channel.id,
            external_interaction_id=message_id.strip(),
            external_conversation_id=sender,
            external_identity_id=sender,
            content=text,
            metadata={CHAT_TYPE_METADATA_KEY: CHAT_TYPE_SINGLE},
        )

    # ================================================================ 出站（ChannelAdapter）

    def deliver(self, outbound: OutboundResponse) -> None:
        """把 OutboundResponse 追加写入本地 outbox（delivery sink）。

        Fail Closed：输入非法 / outbox 写入失败 → `ChannelDeliveryFailedError`
        （出站 Port 契约：不新造错误码）。
        语义：**交付**，返回 None；processing SUCCESS ≠ delivery SUCCESS 由
        Executor 层保证（9.6）。
        """
        if not isinstance(outbound, OutboundResponse):
            raise ChannelDeliveryFailedError()
        if not isinstance(outbound.channel_id, int) or outbound.channel_id <= 0:
            raise ChannelDeliveryFailedError()
        if not isinstance(outbound.external_conversation_id, str) \
                or not outbound.external_conversation_id.strip():
            raise ChannelDeliveryFailedError(channel_id=outbound.channel_id)
        if not isinstance(outbound.content, str) or not outbound.content.strip():
            raise ChannelDeliveryFailedError(channel_id=outbound.channel_id)

        # ACP V0.3 Phase 1：outbox 是**结构化 sink**——`content` **原样**（不渲染
        # 引用块），引用以**完整原值**的 `artifacts` 键随行透传（L4：展示预算
        # 不作用于此）。**无 artifact 时完全不写该键** ⇒ 旧输出逐字节不变。
        record = {
            "channel_id": outbound.channel_id,
            "external_conversation_id": outbound.external_conversation_id,
            "content": outbound.content,
            "metadata": outbound.metadata,
        }
        if outbound.artifacts:
            record["artifacts"] = artifact_reference_records(outbound.artifacts)
        line = json.dumps(record, ensure_ascii=False)
        try:
            self._outbox_path.parent.mkdir(parents=True, exist_ok=True)
            with self._outbox_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError as exc:
            raise ChannelDeliveryFailedError(
                channel_id=outbound.channel_id,
                external_conversation_id=outbound.external_conversation_id,
            ) from exc
