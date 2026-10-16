"""SQLAlchemy SessionRepository 实现（Adapter，实现 app/repositories 端口）。

Session 由调用方注入；本层不 commit（事务边界属 UoW）。

9.3 实现读路径 `find_active`（status == "active"）；9.7 补最小写路径 `create`
（把 CREATE_NEW 决策物化为真实 Session）。不做 update。

Cloud-0.1.1-B：可选 `tenant_id` → 显式租户过滤（第一层防线）。
"""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.domain.session import Session
from app.persistence.mappers.session import to_domain, to_orm
from app.persistence.models.session import SessionModel
from app.repositories.session import SessionRepository


class SqlAlchemySessionRepository:
    """实现 SessionRepository 协议。"""

    def __init__(self, session: Session, tenant_id: int | None = None) -> None:
        self._session = session
        self._tenant_id = tenant_id

    def find_active(
        self,
        identity_id: int,
        connection_id: int,
        external_conversation_id: str,
    ) -> Session | None:
        stmt = select(SessionModel).where(
            SessionModel.identity_id == identity_id,
            SessionModel.connection_id == connection_id,
            SessionModel.external_conversation_id == external_conversation_id,
            SessionModel.status == "active",
        )
        if self._tenant_id is not None:
            stmt = stmt.where(SessionModel.tenant_id == self._tenant_id)
        row = self._session.execute(stmt).scalar_one_or_none()
        return to_domain(row) if row is not None else None

    def create(self, session: Session) -> Session:
        row = to_orm(session)
        if self._tenant_id is not None:
            row.tenant_id = self._tenant_id
        self._session.add(row)
        self._session.flush()
        return to_domain(row)

    def find_or_create(self, session: Session) -> Session:
        """幂等确保存在：命中复用 / 未命中创建；写冲突由 SAVEPOINT 消化后回读。

        M6.6-C4 / CP4-1。并发 `CREATE_NEW` 下输家的 INSERT 会命中：

        - PostgreSQL：`IntegrityError`（`uq_sessions_tenant_identity_conn_conv`）；
        - SQLite：`IntegrityError`，或多线程写锁下的 `OperationalError`。

        恢复的成功判据**不是「捕获了异常」**，而是「回滚后按**精确四元组**回读确实存在」：
        回读到 ⇒ 复用同一 Session；回读不到 ⇒ 原样抛出（由 `SessionCreationExecutor`
        转 `SESSION_RESOLUTION_FAILED`，Fail Closed）。

        SAVEPOINT（`begin_nested`）而非整体 rollback 是硬要求：外层事务（UoW）里持有
        PostgreSQL 事务级 RLS 上下文 `set_config('app.tenant_id', …, true)`；整体回滚会
        令其丢失，回读将失去租户作用域。回读仍走 tenant 作用域的 `find_active`
        （显式过滤 + RLS 双层防线），绝不跨租户 / 跨 conversation 误复用。
        """
        existing = self.find_active(
            session.identity_id, session.connection_id,
            session.external_conversation_id)
        if existing is not None:
            return existing
        try:
            with self._session.begin_nested():
                return self.create(session)
        except (IntegrityError, OperationalError):
            # SAVEPOINT 已回滚：外层事务（含 tenant / RLS 上下文）完好。
            found = self.find_active(
                session.identity_id, session.connection_id,
                session.external_conversation_id)
            if found is not None:
                return found
            raise

    def bind_external_session(
        self,
        session_id: int,
        external_session_id: str,
    ) -> None:
        """10.8.4：Agent 侧会话引用 → CP Session 映射的唯一写入点。"""
        stmt = select(SessionModel).where(SessionModel.id == session_id)
        if self._tenant_id is not None:
            stmt = stmt.where(SessionModel.tenant_id == self._tenant_id)
        row = self._session.execute(stmt).scalar_one_or_none()
        if row is None:
            raise ValueError(f"session_id={session_id} 不存在")
        row.external_session_id = external_session_id
        self._session.flush()

    def find_active_by_identity_and_conversation(
        self,
        identity_id: int,
        external_conversation_id: str,
    ) -> Session | None:
        """10.4.2：Interaction Wrapper 的最小读路径（隔离键弱化为二键，
        见 Port docstring 的「有记录的近似」）。"""
        stmt = select(SessionModel).where(
            SessionModel.identity_id == identity_id,
            SessionModel.external_conversation_id == external_conversation_id,
            SessionModel.status == "active",
        )
        if self._tenant_id is not None:
            stmt = stmt.where(SessionModel.tenant_id == self._tenant_id)
        row = self._session.execute(stmt).scalar_one_or_none()
        return to_domain(row) if row is not None else None

    def list_known_conversation_ids(self, channel_id: int) -> list[str]:
        """B4.5：Discovery 投影（distinct、滤空、稳定排序）——纯读，零写入。"""
        stmt = (
            select(SessionModel.external_conversation_id)
            .where(
                SessionModel.channel_id == channel_id,
                SessionModel.external_conversation_id.is_not(None),
            )
            .distinct()
            .order_by(SessionModel.external_conversation_id)
        )
        if self._tenant_id is not None:
            stmt = stmt.where(SessionModel.tenant_id == self._tenant_id)
        return [
            value for value in self._session.execute(stmt).scalars().all()
            if value is not None and value.strip()
        ]
