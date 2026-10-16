"""BrowserAuthRepository 端口（Port，Cloud-0.1.1-C）。

只描述 Browser Auth 需要的持久化能力，不含实现，不 import SQLAlchemy / persistence。

与业务仓储端口的差异（刻意）：
- 它操作的是 **auth 表**（user_credentials / browser_sessions），不是领域实体，
  因此返回的是 `app.core.browser_auth` 的读取投影（CredentialRecord / SessionRecord），
  而不是 Domain Entity（Gate §9：Domain 全树冻结，凭据/会话不进 Domain）。
- **不按 tenant 过滤**：登录 / 会话解析都发生在 tenant 解析**之前**（bootstrap），
  按全局唯一的 `login` / `session_id` 定位；跨租户数据隔离由后续 tenant-scoped
  UoW（Cloud-0.1.1-B）承担，不在本端口。

实现见 app/persistence/repositories/browser_auth.py（SqlAlchemyBrowserAuthRepository）。
"""

from typing import Protocol, runtime_checkable

from app.core.browser_auth import CredentialRecord, SessionRecord


@runtime_checkable
class BrowserAuthRepository(Protocol):
    """Browser Auth 凭据 / 会话的持久化端口。"""

    def create_credential(
        self, *, user_id: int, tenant_id: int, login: str, verifier: str
    ) -> CredentialRecord:
        """写入一条登录凭据（`login` 全局唯一；由 DB 唯一约束兜底）。"""
        ...

    def find_credential_by_login(self, login: str) -> CredentialRecord | None:
        """按 `login` 取凭据；不存在返回 None。"""
        ...

    def create_session(
        self, *, session_id: str, user_id: int, tenant_id: int
    ) -> SessionRecord:
        """写入一条 opaque 浏览器会话。"""
        ...

    def find_session(self, session_id: str) -> SessionRecord | None:
        """按 `session_id` 取会话；不存在返回 None。"""
        ...

    def delete_session(self, session_id: str) -> None:
        """删除会话（logout）；不存在为 no-op。"""
        ...
