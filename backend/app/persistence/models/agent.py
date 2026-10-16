"""agents 表 ORM（对应 4.2 冻结字段）。

credential_ref 只存"凭据引用"（指向外部 Secret 存储的标识）——凭据材料
本身不进该列（引用入、引用不出）。

M7-AUI-10.2 语义澄清（与既有事实对齐，不重构 ORM）：credential-like
**材料**（如 Agent.config["api_key"]，与 E3-B 起 Connection.config["api_key"]
同一先例）经 JSON 列入库；安全边界在 API 面——读路径一律脱敏
（`sanitize_agent_config`），写路径拒绝脱敏占位符（哨兵 422），
敏感材料绝不回显给任何客户端。
"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base


class AgentModel(Base):
    __tablename__ = "agents"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    endpoint: Mapped[str | None] = mapped_column(String(512), nullable=True)
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
