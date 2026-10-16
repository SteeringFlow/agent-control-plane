"""SqlAlchemyBrowserAuthRepository（Adapter，实现 app/repositories/browser_auth.py 端口）。

Cloud-0.1.1-C —— Browser Auth 凭据 / 会话的持久化实现。

边界（刻意写死）：

- 构造只接收 `session`（**无 tenant_id**）：登录 / 会话解析都是 bootstrap
  （tenant 解析之前）访问，按全局唯一的 `login` / `session_id` 定位——
  跨租户数据隔离由后续 tenant-scoped UoW 承担，不在此过滤；
- 本层只 flush（拿自增 id / 可见性），**不 commit**——事务边界属 UoW；
- ORM 行 → `CredentialRecord` / `SessionRecord` 的翻译内联在此（auth 记录不是
  Domain Entity，无需 mapper，Gate §9）。
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.browser_auth import CredentialRecord, SessionRecord
from app.persistence.models.browser_session import BrowserSessionModel
from app.persistence.models.credential import UserCredentialModel


class SqlAlchemyBrowserAuthRepository:
    """实现 BrowserAuthRepository 协议。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    # ------------------------------------------------------------ credentials

    def create_credential(
        self, *, user_id: int, tenant_id: int, login: str, verifier: str
    ) -> CredentialRecord:
        row = UserCredentialModel(
            user_id=user_id, tenant_id=tenant_id, login=login, verifier=verifier
        )
        self._session.add(row)
        self._session.flush()
        return _to_credential(row)

    def find_credential_by_login(self, login: str) -> CredentialRecord | None:
        row = self._session.execute(
            select(UserCredentialModel).where(UserCredentialModel.login == login)
        ).scalar_one_or_none()
        return _to_credential(row) if row is not None else None

    # ------------------------------------------------------------ sessions

    def create_session(
        self, *, session_id: str, user_id: int, tenant_id: int
    ) -> SessionRecord:
        row = BrowserSessionModel(
            session_id=session_id, user_id=user_id, tenant_id=tenant_id
        )
        self._session.add(row)
        self._session.flush()
        return _to_session(row)

    def find_session(self, session_id: str) -> SessionRecord | None:
        row = self._session.execute(
            select(BrowserSessionModel).where(
                BrowserSessionModel.session_id == session_id
            )
        ).scalar_one_or_none()
        return _to_session(row) if row is not None else None

    def delete_session(self, session_id: str) -> None:
        row = self._session.execute(
            select(BrowserSessionModel).where(
                BrowserSessionModel.session_id == session_id
            )
        ).scalar_one_or_none()
        if row is not None:
            self._session.delete(row)
            self._session.flush()


def _to_credential(row: UserCredentialModel) -> CredentialRecord:
    return CredentialRecord(
        user_id=row.user_id, tenant_id=row.tenant_id,
        login=row.login, verifier=row.verifier,
    )


def _to_session(row: BrowserSessionModel) -> SessionRecord:
    return SessionRecord(
        session_id=row.session_id, user_id=row.user_id, tenant_id=row.tenant_id,
    )
