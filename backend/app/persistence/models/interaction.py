"""interactions 表 ORM（对应 4.2 冻结字段）。

processing_status 与 delivery_status 必须保持两个独立状态，不得合成 status：
「Agent 已成功（processing=SUCCEEDED）→ 渠道投递失败（delivery=FAILED）」
是完全合法的状态组合。

request_payload / response_payload 是渠道与 Agent 的载荷快照；
所有关系型标识（session/channel/agent/connection）一律走 FK，不入 JSON。

**10.4.2 Schema 修正案**：`session_id` nullable=False → True。实现驱动的
裁决：Identity/Routing 等 **Session 建立之前的失败** 也要按 10.4.2 验收被
记录（processing=FAILED, failure_stage=identity_resolution），此时不存在
可挂的 Session——NOT NULL 会阻止记录真实失败。语义：成功路径与到达
Dispatch 的失败路径由 Persistence Wrapper 回填，前段失败保持 NULL。
这是 5.3 预言的「第一个需要演进已有库 schema 的真实变更」——Alembic 触发
条件达成，记录在案（MVP 仍用 create_all，迁移工具链留真实部署 Step）。

**10.5.2 Interaction Identity 物化（第二次 schema 演进）**：

- `channel_id` 升为一等持久化列（FK → channels.id，NOT NULL）——
  此前它只存在于 `request_payload` JSON 快照里，无法参与约束与查询；
- 新增 `UNIQUE(channel_id, external_interaction_id)`（uq_interactions_channel_external_id）：
  同一 Channel 内同一外部交互 ID 只能产生一条 Interaction——
  第二次 create 会在 DB 层 conflict（IntegrityError）。**本约束只物化身份，
  不做任何幂等/去重决策**（命中 conflict 后怎么办属 10.5.3+ 裁决，
  见 docs/step-10.5.1 §Q10 的 Idempotency ≠ Retry 记录）；
- `external_interaction_id` 保持可空：无外部 ID 的行不参与去重
  （SQL 唯一约束对 NULL 不去重，语义恰好吻合）；
- 索引裁决：**不再单独为 channel_id 建索引**——复合唯一索引以 channel_id
  为前导列，天然服务 channel 作用域访问（identities / connections 同纪律）。
"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.sqlite import DATETIME as SQLiteDateTime
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base


class InteractionModel(Base):
    __tablename__ = "interactions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "channel_id", "external_interaction_id",
            name="uq_interactions_tenant_channel_external_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id"), nullable=False  # Interaction Identity scope
    )
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("sessions.id"), nullable=True, index=True
    )
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    external_interaction_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    request_payload: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    response_payload: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    processing_status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="pending"
    )
    delivery_status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="pending"
    )
    failure_stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 12.9.4.12 Attempt Fence：CAS 抢占那一刻由 DB 写入的时间戳（与
    # PENDING→PROCESSING 同一条条件化 UPDATE，func.now()）。record_outcome 以
    # 「列 == 抢占时读回值」作等式围栏——旧 attempt 被回收后其 outcome 落不进
    # 新 attempt（不写穿）。回收只写 NULL（12.9.4.11 D.2：无 onupdate）。
    # SQLite 变体 truncate_microseconds=True：CURRENT_TIMESTAMP 是秒级文本
    # （'YYYY-MM-DD HH:MM:SS'），默认 bind 处理器会给 datetime 补 '.000000'
    # 导致「列 vs 读回值」文本不等——围栏恒 REJECTED。截断后等式两侧同为秒级。
    processing_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True).with_variant(
            SQLiteDateTime(timezone=True, truncate_microseconds=True), "sqlite"),
        nullable=True,
    )
    # P2 Delivery Lifecycle（三列，均为**附加式**，不改既有列语义）：
    # - delivery_attempts：投递尝试次数的**单调计数** = 投递写入围栏身份
    #   （**不用时间戳等式**：SQLite 秒级精度会碰撞，见上）；
    # - delivery_started_at：投递尝试的**持有起点**（DB 时钟写入，过期回收写 NULL）；
    # - delivery_protocol：**协议世代**（0 = 历史/未知协议 ⇒ 禁止自动重投；
    #   1 = 新协议状态机）。0 语义让历史行的 `attempts=0` **不能**被误读为
    #   「从未发送」。
    delivery_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0")
    delivery_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True).with_variant(
            SQLiteDateTime(timezone=True, truncate_microseconds=True), "sqlite"),
        nullable=True,
    )
    delivery_protocol: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id"), nullable=False, index=True,
        default=1, server_default="1",
    )
