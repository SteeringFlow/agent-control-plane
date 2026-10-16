"""browser_sessions 表 ORM（Cloud-0.1.1-C —— opaque 浏览器会话）。

职责边界（Gate §4/§7 写死）：

- `session_id` 是**不透明**随机令牌（`secrets.token_urlsafe`），非 JWT、非签名
  声明链（Gate §3 JWT 禁区）。Cookie 只携带它；
- `user_id` 记录会话归属用户；`tenant_id` 冗余存放（会话解析早于 tenant 解析，
  且 `users` 受 RLS 保护），使 "session → user → tenant" 在 bootstrap 下可完成；
- **无** refresh_token / JWKS / 设备管理 / Remember Me / 过期轮换——会话管理属
  登录体验优化，C.1 明确不做（logout 即删除本行）。

本表**不启用** per-tenant RLS（同 user_credentials 的 bootstrap 理由）。

本表只声明 Schema，不写 relationship()。
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base


class BrowserSessionModel(Base):
    __tablename__ = "browser_sessions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True, index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
