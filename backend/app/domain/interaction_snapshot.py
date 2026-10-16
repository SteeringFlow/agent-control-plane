"""Interaction Snapshot —— InboundInteraction ⇄ Interaction.request_payload（12.9.4.3）。

为什么需要这一层：

    现在（同步）   InboundInteraction ──内存直传──→ RuntimeCoordinator
    将来（Worker） InboundInteraction
                        │ persist（create_pending）
                        ▼
                   Interaction.request_payload
                        │ reconstruct（本模块）
                        ▼
                   InboundInteraction ──→ RuntimeCoordinator

Worker 拿到的**不是** InboundInteraction 对象，而是库里的 `request_payload` 快照。
因此「落库存什么」与「重建取什么」必须是一对严格互逆、且**在同一处定义**的函数——
否则执行所依赖的事实会在持久化边界上静默丢失。

已发生的事实缺口（12.9.3.5）：原先 `create_pending` 把 `metadata` 漏在快照之外 →
重建后 `metadata=None` → 机器人入站的 `response_url` 丢失 → `WeComAdapter.deliver`
静默回退到 `message/send`（绕过入站 Fail Closed，且不报错）。本模块把快照形状收口
为单一事实源，杜绝这类漂移。

设计裁决：

1. **显式列出全部字段**：不依赖 `dataclasses.asdict` 隐式展开——若契约字段隐式
   跟随 dataclass 变化，已落库的持久化数据会静默改形状；
2. **`metadata` 恒存（可为 None）**：空值也写键，使「该渠道入站未携带 metadata」
   与「快照缺键」可区分；
3. **重建 Fail Closed**：必需字段缺失 / `metadata` 类型非法 → `ValueError`，
   绝不静默补全（静默补全 = 伪造入站输入）；
4. **旧数据兼容**：12.9.4.3 之前的行没有 `metadata` 键 → 重建取 `None`，
   等价于自建应用 XML 入站路径，不抛错、不补齐、不猜测。

边界：本模块是**纯 domain**（只依赖 InboundInteraction 值对象），不 import
SQLAlchemy / persistence / application——重建因此可在任何进程（Worker）中复用。
"""

from app.domain.inbound_interaction import InboundInteraction

#: 运行时执行所需的事实面（与 InboundInteraction 的必需字段一一对应）。
#: 重建必须完整拿到——缺任何一项，执行语义都会漂移。
RUNTIME_REQUIRED_FIELDS = (
    "channel_id",
    "external_interaction_id",
    "external_conversation_id",
    "external_identity_id",
    "content",
)

#: 快照可选字段：允许缺失（旧数据兼容），缺失时取 None。
SNAPSHOT_OPTIONAL_FIELDS = ("metadata",)


def inbound_to_snapshot(inbound: InboundInteraction) -> dict:
    """InboundInteraction → Interaction.request_payload（落库快照）。

    保存**完整**入站事实：五个运行时必需字段 + `metadata`（可 None）。
    `metadata` 不可省略——它是渠道侧 delivery context（如智能机器人
    `response_url`）的唯一载体，Worker 重建后要靠它把回复投回正确通道。
    """
    return {
        "channel_id": inbound.channel_id,
        "external_interaction_id": inbound.external_interaction_id,
        "external_conversation_id": inbound.external_conversation_id,
        "external_identity_id": inbound.external_identity_id,
        "content": inbound.content,
        "metadata": inbound.metadata,
    }


def inbound_from_snapshot(payload: dict | None) -> InboundInteraction:
    """Interaction.request_payload → InboundInteraction（重建边界）。

    与 `inbound_to_snapshot` 互为逆运算：落库时存什么，重建时就取什么——
    不多取、不猜测、不回退。

    旧数据兼容：12.9.4.3 之前落库的行没有 `metadata` 键 → 取 `None`
    （等价于「该渠道入站未携带 metadata」，与自建应用 XML 路径一致）。

    Fail Closed：快照不是对象 / 缺必需字段 / `metadata` 既非 dict 也非 null
    → `ValueError`。快照不完整意味着无法忠实重建运行时所需事实，静默补全等于
    伪造入站输入（12.x Evidence = 事实纪律）。
    """
    if not isinstance(payload, dict):
        raise ValueError("request_payload 不是对象，无法重建 InboundInteraction")
    missing = [key for key in RUNTIME_REQUIRED_FIELDS if key not in payload]
    if missing:
        raise ValueError(f"request_payload 缺少运行时必需字段：{missing}")
    metadata = payload.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise ValueError("request_payload.metadata 必须是 JSON 对象（dict）或 null")
    return InboundInteraction(
        channel_id=payload["channel_id"],
        external_interaction_id=payload["external_interaction_id"],
        external_conversation_id=payload["external_conversation_id"],
        external_identity_id=payload["external_identity_id"],
        content=payload["content"],
        metadata=metadata,
    )
