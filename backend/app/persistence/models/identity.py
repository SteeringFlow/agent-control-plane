"""identities 表 ORM（对应 4.2 冻结字段）。

核心唯一约束 UNIQUE(tenant_id, channel_id, external_id)
（Cloud-0.1.1-B 在 4.2 的 UNIQUE(channel_id, external_id) 前导了 tenant_id）：
同一租户、同一 Channel 中，一个外部身份只能对应一个 Control Plane Identity。

**user_id 可空（Implementation Package 1 / Gate 2.6 D1/D2）**：
ACP User 关联是**可选**的——`user_id IS NULL` 表示该外部身份**未与 ACP Account
建立管理绑定**（**不是**匿名 / 访客 / 系统用户，也不表示「任何人可用」）。
Identity 自身即一等运行时身份。

**FK 语义（Gate 3 A-2 冻结）**：user_id → users.id **无 ondelete 子句**
= NO ACTION（阻断式）——删除被引用的 User 会失败，**不是** SET NULL / CASCADE。
「Identity 在 User 消失后保留」靠**阻断删除**实现，不靠隐式解绑。

注意：SQLAlchemy Declarative 保留属性名 metadata，
故 ORM 属性为 extra_metadata，数据库列名仍是冻结 schema 的 metadata。
"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base


class IdentityModel(Base):
    __tablename__ = "identities"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "channel_id", "external_id",
            name="uq_identities_tenant_channel_external_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id"), nullable=False, index=True
    )
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="active")
    extra_metadata: Mapped[dict | None] = mapped_column("metadata", JSON, nullable=True)
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
