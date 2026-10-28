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
from app.domain.agent_response import AgentResponse
from app.domain.artifact import Artifact
from app.domain.inbound_interaction import InboundInteraction
from app.domain.outbound_response import OutboundResponse
from app.repositories.channel_adapter import ChannelAdapter


def build_outbound_response(
    inbound: InboundInteraction,
    agent_response: AgentResponse,
) -> OutboundResponse:
    """InboundInteraction + AgentResponse → OutboundResponse（**纯函数**，无副作用）。

    P1 / C1：外部投递从 Runtime 的同步链里移出后，「组装出站消息」这件事
    改由**应用层**在「Agent 成功结果已落库之后」发起。它必须**只有一份**
    实现——两份相似实现会随时间漂移（本仓库已有 12.9.3.5「metadata 漏进快照
    → 机器人回复静默错投」的前车之鉴）。故收口为本纯函数，由正常投递路径与
    （未来的）投递恢复路径共用。

    **metadata 合并规则（12.8.2，逐字沿用）**：

        {**agent.metadata, **inbound.metadata}

    即 **channel-originated 优先**：Channel 侧交付上下文（如智能机器人的
    `bot_reply.req_id`）**绝不会被** Agent 输出覆盖；Agent 侧只能补充新 key。
    两侧皆空 → `{} or None` → None（保持既有「无 metadata」语义）。

    纯函数纪律：不 import 任何 I/O / persistence / adapter；不做校验（校验归
    生产者的 Fail Closed 纪律）；不读时钟、不读环境、不写库。
    """
    metadata = {**(agent_response.metadata or {}), **(inbound.metadata or {})}
    return OutboundResponse(
        channel_id=inbound.channel_id,
        external_conversation_id=inbound.external_conversation_id,
        content=agent_response.content,
        metadata=metadata or None,
        artifacts=agent_response.artifacts,
    )


#: `response_payload.artifacts` 快照的**完整字段面**（与
#: `interaction_recording._snapshot_artifacts` 的显式字段映射逐字一致，
#: 也与 `artifact_reference.artifact_reference_records` 的形状一致）。
#: 形状纪律：**显式列出全部字段**——快照形状若与本集合不符 ⇒ Fail Closed，
#: 不静默忽略未知字段（那会让「引用悄悄消失」）。
_ARTIFACT_SNAPSHOT_FIELDS = (
    "artifact_id", "kind", "name", "uri", "mime_type", "size_bytes", "metadata",
)

#: 快照里**必填**的 artifact 字段（其余可空）。
_ARTIFACT_REQUIRED_FIELDS = ("artifact_id", "kind", "name")


def _artifacts_from_snapshot(raw: object) -> list[Artifact] | None:
    """`response_payload.artifacts` 快照 → `Artifact` 领域对象（**Fail Closed**）。

    **纯函数**（无 I/O / 不读时钟 / 不写库）：把已落库的 artifact 引用快照
    还原成领域对象，使**恢复投递**构建出的出站消息与首次投递**逐字段一致**
    （P2.1：此前本映射缺失 ⇒ 恢复重发会**静默丢失引用**）。

    复用既有领域对象与形状（`Artifact` 7 字段 + `_snapshot_artifacts` 的显式
    字段映射）——**不**重写 Artifact Phase 1/2 的任何语义、存储格式或接口。

    失败处理（**明确、可测试**，绝不静默降级为「无 artifact 的消息」）：
    - 快照不是列表（且非 None）→ ValueError；
    - 元素不是对象 → ValueError；
    - 含**未知字段**（形状不兼容）→ ValueError；
    - 缺 `artifact_id` / `kind` / `name` → ValueError；
    - 字段类型非法（uri/mime_type 非 str、size_bytes 非 int、metadata 非 dict）→
      ValueError。

    `None` → `None`（与 `AgentResponse.artifacts is None` 的快照纪律一致）。
    """
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise ValueError("response_payload.artifacts 不是列表，无法重建 Artifact")
    known = set(_ARTIFACT_SNAPSHOT_FIELDS)
    restored: list[Artifact] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(
                f"response_payload.artifacts[{index}] 不是对象，无法重建 Artifact")
        unknown = sorted(set(item) - known)
        if unknown:
            raise ValueError(
                f"response_payload.artifacts[{index}] 含未知字段 {unknown}"
                "（快照形状不兼容，拒绝静默丢弃引用）")
        missing = [f for f in _ARTIFACT_REQUIRED_FIELDS if f not in item]
        if missing:
            raise ValueError(
                f"response_payload.artifacts[{index}] 缺少必填字段 {missing}")
        artifact_id = item["artifact_id"]
        kind = item["kind"]
        name = item["name"]
        for field_name, value in (("artifact_id", artifact_id),
                                  ("kind", kind), ("name", name)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"response_payload.artifacts[{index}].{field_name} 非法")
        uri = item.get("uri")
        mime_type = item.get("mime_type")
        for field_name, value in (("uri", uri), ("mime_type", mime_type)):
            if value is not None and not isinstance(value, str):
                raise ValueError(
                    f"response_payload.artifacts[{index}].{field_name} 非法")
        size_bytes = item.get("size_bytes")
        if size_bytes is not None and (isinstance(size_bytes, bool)
                                       or not isinstance(size_bytes, int)):
            raise ValueError(
                f"response_payload.artifacts[{index}].size_bytes 非法")
        metadata = item.get("metadata")
        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError(
                f"response_payload.artifacts[{index}].metadata 非法")
        restored.append(Artifact(
            artifact_id=artifact_id, kind=kind, name=name, uri=uri,
            mime_type=mime_type, size_bytes=size_bytes, metadata=metadata,
        ))
    return restored


def agent_response_from_snapshot(payload: dict | None) -> AgentResponse:
    """已落库的 `response_payload` 快照 → `AgentResponse`（恢复路径重建用）。

    **纯函数**（无 I/O / 不读时钟 / 不写库）——恢复重投用它把「已持久化的结果」
    还原成出站信封的输入，**不重跑 Agent**。

    快照形状（10.8.4 / P1 落库时写入）：
        {content, external_session_id, metadata, artifacts}

    **P2.1**：`artifacts` 由 `_artifacts_from_snapshot` 还原成 `Artifact` 领域
    对象 ⇒ 恢复投递构建出的出站消息与首次投递**逐字段一致**（含引用块渲染）。
    快照缺失 / 非法 / 形状不兼容 ⇒ **Fail Closed（ValueError）**：绝不静默
    降级成「没有引用的消息」并继续投递。

    缺 `content` 或形状非法 → Fail Closed（抛 ValueError）：**不猜**。
    """
    if not isinstance(payload, dict):
        raise ValueError("response_payload 不是对象，无法重建 AgentResponse")
    content = payload.get("content")
    if not isinstance(content, str):
        raise ValueError("response_payload.content 缺失或非法，无法重建")
    metadata = payload.get("metadata")
    external_session_id = payload.get("external_session_id")
    return AgentResponse(
        content=content,
        external_session_id=(
            external_session_id if isinstance(external_session_id, str) else None),
        metadata=metadata if isinstance(metadata, dict) else None,
        artifacts=_artifacts_from_snapshot(payload.get("artifacts")),
    )


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
