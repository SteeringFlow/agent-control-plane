"""user_credentials 表 ORM（Cloud-0.1.1-C —— Browser Auth 凭据）。

职责边界（Gate §4/§7 写死）：

- 凭据**只落本表**，绝不进入 `users` 表或 `User` 领域实体（红线 D）；
- `login` 是全局唯一登录标识（bootstrap 登录在 tenant 解析**之前**按它定位用户，
  故不能按 tenant 分区唯一）；`user_id` 唯一 = 一个浏览器用户一条凭据；
- `tenant_id` 冗余存放：登录早于 tenant 解析，且 `users` 受 RLS 保护（bootstrap
  时不可读），故 tenant 由凭据行直接给出；
- `verifier` 是 `app.core.passwords` 生成的验证器（非明文、非 JWT）。

本表**不启用** per-tenant RLS：登录/会话解析都是 bootstrap（`app.tenant_id` 尚未
设置）访问，per-tenant policy 会 fail-closed 挡住登录。第二层隔离的真正对象仍是
8 张 Core Table（Cloud-0.1.1-B，零重写）。

本表只声明 Schema，不写 relationship()，与其余表同纪律。
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base


class UserCredentialModel(Base):
    __tablename__ = "user_credentials"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False, unique=True, index=True
    )
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id"), nullable=False, index=True
    )
    login: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True, index=True
    )
    verifier: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
        nullable=False,
    )
