"""sessions 表 ORM（对应 4.2 冻结字段）。

Control Plane Session ≠ Agent Conversation（红线）：

- external_session_id      Agent 一侧的 session 标识，不是本表的替代品；
- external_conversation_id Channel 一侧具体的 conversation/chat/group 上下文。

因此「张三 × WeCom 私聊A / WeCom 群聊B / WeChat 私聊C」是三条 Session，
不会因为 identity 相同而混在一起（tests/test_models.py::test_f_context_isolation
把它落实成数据库事实）。

connection_id / agent_id 允许 NULL：4.2 未冻结 nullability，MVP 取宽松——
会话可以先于路由决策创建，绑定由 5.4 的 Repository 用例决定。
"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base


class SessionModel(Base):
    __tablename__ = "sessions"
    # Cloud-0.1.1-B：把应用层强制的语义键
    # (identity_id, connection_id, external_conversation_id) 提升为
    # 租户前导的 DB 唯一约束——语义键不变，仅加隔离作用域。
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "identity_id", "connection_id", "external_conversation_id",
            name="uq_sessions_tenant_identity_conn_conv",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    identity_id: Mapped[int] = mapped_column(
        ForeignKey("identities.id"), nullable=False, index=True
    )
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id"), nullable=False, index=True
    )
    connection_id: Mapped[int | None] = mapped_column(
        ForeignKey("connections.id"), nullable=True, index=True
    )
    agent_id: Mapped[int | None] = mapped_column(
        ForeignKey("agents.id"), nullable=True, index=True
    )
    external_session_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    external_conversation_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="active")
    extra_metadata: Mapped[dict | None] = mapped_column("metadata", JSON(none_as_null=True), nullable=True)
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
