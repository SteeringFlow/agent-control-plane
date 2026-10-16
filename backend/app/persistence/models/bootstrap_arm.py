"""bootstrap_arms 表 ORM（M6.6-C3 / ADR-12 —— Bootstrap Arm 持久化）。

字段面严格按 `docs/M6_6_C3_CP3_F_BOOTSTRAP_ARM_IMPLEMENTATION_DESIGN.md` §2。

**关键约束：部分唯一索引**（O-1 的权威层）

    UNIQUE(tenant_id, channel_id, connection_id) WHERE status = 'armed'

同一 scope **至多一条 ARMED** 由 **数据库**保证；应用层的 revoke-then-insert 只负责
把语义塑成 supersede，**不承载**该不变量（PG READ COMMITTED 下并发 arm 会各插一条，
无 DB 约束即产生两条 ARMED ⇒ one-shot 破裂）。

它在 SQLAlchemy 中是 `Index(unique=True, postgresql_where=…, sqlite_where=…)`，
**不是** `UniqueConstraint` —— 因此既有「UNIQUE 必须 tenant 前导」的守卫看不见它，
需由 G-I 守卫单独覆盖（见 CP3-E Guard Manifest §7）。

**复合 FK** 与 `connection_conversations` 同形：一个约束同时锁死「跨租户」与
「同租户跨 channel」两种 scope 错配。

`initiator_user_id` **NOT NULL**：Arm 的合法性组成部分（CP-1 P4），无 sentinel。
"""

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base


class BootstrapArmModel(Base):
    __tablename__ = "bootstrap_arms"
    __table_args__ = (
        # O-1：同一 (tenant, channel, connection) 至多一条 ARMED —— **DB 为权威**。
        Index(
            "uq_bootstrap_arms_scope_armed",
            "tenant_id",
            "channel_id",
            "connection_id",
            unique=True,
            postgresql_where=text("status = 'armed'"),
            sqlite_where=text("status = 'armed'"),
        ),
        ForeignKeyConstraint(
            ["tenant_id", "channel_id", "connection_id"],
            ["connections.tenant_id", "connections.channel_id", "connections.id"],
            name="fk_bootstrap_arms_tenant_channel_connection",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id"), nullable=False, index=True,
        default=1, server_default="1",
    )
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id"), nullable=False
    )
    connection_id: Mapped[int] = mapped_column(nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="armed"
    )
    # NOT NULL：Arm 只在存在真实已认证操作者时被创建（O-4）。
    initiator_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
        nullable=False,
    )
    # ---- 消费追溯（无 FK：避免与 Interaction / Grant 的生命周期互锁）----
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    consumed_by_interaction_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    consumed_external_identity_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    consumed_external_conversation_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
