"""activities 表 ORM（对应 4.2 冻结字段）。

Append-only（Evidence / Execution Record，不是 Audit Platform）：

- 只有 created_at，没有 updated_at——历史事实不允许被改写；
- ORM 层不提供任何"更新 Activity"的业务机制，也不建唯一约束；
- interaction_id 允许 NULL：4.2 未冻结 nullability，允许会话级活动
  （如 session 开始/结束）不挂到具体 interaction。

status 为 NOT NULL 且无默认值：活动是显式记录的事实，
由写入方明确声明（不发明 "recorded" 之类的语义默认）。

**10.4.2 Schema 修正案**：`session_id` nullable=False → True（与
interactions.session_id 同因：Session 建立之前的阶段失败 Evidence 也要被
记录，见 interaction.py docstring）。
"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base


class ActivityModel(Base):
    __tablename__ = "activities"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    interaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("interactions.id"), nullable=True, index=True
    )
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("sessions.id"), nullable=True, index=True
    )
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra_metadata: Mapped[dict | None] = mapped_column("metadata", JSON(none_as_null=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id"), nullable=False, index=True,
        default=1, server_default="1",
    )
