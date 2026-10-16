"""connections 表 ORM（对应 4.2 冻结字段）。

MVP 唯一约束 UNIQUE(channel_id, agent_id)：
Channel A 可以接 Agent X / Agent Y，但同一个 Channel → 同一个 Agent
暂时只允许一条 Connection。真实需求出现后再调整。

credential_ref 同 agents：只存引用，不存明文。
"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base


class ConnectionModel(Base):
    __tablename__ = "connections"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "channel_id", "agent_id",
            name="uq_connections_tenant_channel_agent_id",
        ),
        # Implementation Package 2 / Step 1：**结构性 supporting constraint**
        # （**不是** Connection 的业务唯一性）。
        #
        # 唯一目的：让 connection_conversations 的复合 FK
        #     (tenant_id, channel_id, connection_id)
        #         → connections(tenant_id, channel_id, id)
        # 能成为合法 FK target —— 由 DB 结构性防止「同租户跨 channel 错配」
        # （RLS 对同租户不生效，单列 connection_id FK 也不携带 channel）。
        #
        # 不改变：Connection 业务唯一性（仍是上面那条）/ 生命周期 / Router 语义 / API。
        UniqueConstraint(
            "tenant_id", "channel_id", "id",
            name="uq_connections_tenant_channel_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id"), nullable=False, index=True
    )
    agent_id: Mapped[int] = mapped_column(
        ForeignKey("agents.id"), nullable=False, index=True
    )
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="active")
    config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    credential_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
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
