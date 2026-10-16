"""Step 5.5：Persistence 事务边界 —— SqlAlchemyUnitOfWork。

谁负责一次业务操作的事务边界？—— UoW。

MVP 的 UoW 只负责三件事：
1. 提供一个共享的 Session（所有 Repository 共用，见 tests/test_unit_of_work.py::A）
2. 暴露 Repository（users / channels / agents / identities / connections /
   sessions —— 只暴露已实现的 Port；sessions 自 9.3 起只提供读路径 find_active）
3. commit / rollback / 退出时 close

边界（刻意写死，便于日后检查）：

可以：
- 持有 Session，并把它交给 Repository（Repository 只是 Session 的使用者）
- 暴露已存在的 Repository Port 实例
- commit() / rollback() / close()，以及 with 上下文（异常 → rollback）

不可以：
- 是 Service：绝不出现 uow.create_user() / uow.resolve_identity() /
  uow.route_agent() 之类的业务方法——业务操作在未来的 application 层；
- 知道 Domain Entity / 业务逻辑：本模块不 import app.domain；
- 替 Repository commit：Repository 只 flush，commit/rollback 永远属于 UoW；
- 引入 TransactionManager / TransactionService / 分布式事务 / Saga /
  Outbox / Event Sourcing / CQRS / Async / Retry 框架——
  现在就是「SQLAlchemy Session + commit + rollback」，这已经足够。

生命周期：__enter__ 创建 Session + Repository → 业务操作 →
显式 commit()（或异常自动 rollback）→ __exit__/close 关闭 Session。
"""

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.persistence.database import SessionLocal
from app.persistence.repositories.activity_record import (
    SqlAlchemyActivityRecordRepository,
)
from app.persistence.repositories.agent import SqlAlchemyAgentRepository
from app.persistence.repositories.bootstrap_arm import (
    SqlAlchemyBootstrapArmRepository,
)
from app.persistence.repositories.browser_auth import SqlAlchemyBrowserAuthRepository
from app.persistence.repositories.channel import SqlAlchemyChannelRepository
from app.persistence.repositories.connection import SqlAlchemyConnectionRepository
from app.persistence.repositories.connection_conversation import (
    SqlAlchemyConnectionConversationRepository,
)
from app.persistence.repositories.identity import SqlAlchemyIdentityRepository
from app.persistence.repositories.interaction_record import (
    SqlAlchemyInteractionRecordRepository,
)
from app.persistence.repositories.session import SqlAlchemySessionRepository
from app.persistence.repositories.tenant import SqlAlchemyTenantRepository
from app.persistence.repositories.user import SqlAlchemyUserRepository


class SqlAlchemyUnitOfWork:
    """一个事务边界：共享 Session + 一组 Repository + commit/rollback。

    session_factory 默认取持久化基础设施的 SessionLocal；
    测试可注入绑定内存库的 factory。
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session] = SessionLocal,
        tenant_id: int | None = None,
    ) -> None:
        self._session_factory = session_factory
        # Cloud-0.1.1-B：租户作用域（由 API 入口经 TenantScope 显式绑定）。
        # None = 本地/测试单租户 legacy 路径（行为 == 隔离前）。
        self._tenant_id = tenant_id
        self.session: Session | None = None
        self.users: SqlAlchemyUserRepository | None = None
        self.channels: SqlAlchemyChannelRepository | None = None
        self.agents: SqlAlchemyAgentRepository | None = None
        self.identities: SqlAlchemyIdentityRepository | None = None
        self.connections: SqlAlchemyConnectionRepository | None = None
        # Implementation Package 2 / Step 1：授权状态仓储（Conversation → Connection）。
        self.connection_conversations: (
            SqlAlchemyConnectionConversationRepository | None
        ) = None
        # M6.6-C3 / ADR-12：Bootstrap 窗口（不是授权状态存储）。
        self.bootstrap_arms: SqlAlchemyBootstrapArmRepository | None = None
        self.sessions: SqlAlchemySessionRepository | None = None
        self.interactions: SqlAlchemyInteractionRecordRepository | None = None
        self.activities: SqlAlchemyActivityRecordRepository | None = None
        # Cloud-0.1.1-C：Browser Auth 凭据 / 会话（bootstrap 访问，不按 tenant 过滤）。
        self.browser_auth: SqlAlchemyBrowserAuthRepository | None = None
        # Cloud-0.1.2.1：tenants root catalog 写入（唯一写路径 = TenantProvisioner）。
        self.tenants: SqlAlchemyTenantRepository | None = None

    def __enter__(self) -> "SqlAlchemyUnitOfWork":
        session = self._session_factory()
        self.session = session
        # 第二层防线：PostgreSQL RLS 的 session 上下文——事务级注入
        # （COMMIT/ROLLBACK 自动清除，连接池复用无残留）。仅 PG 生效；
        # 本地 SQLite 无 RLS（走第一层 Repository 显式过滤）。
        #
        # 用 `set_config(name, value, is_local=true)` 而非 `SET LOCAL ... = :param`：
        # PG 的 SET 是 utility 语句，**不接受绑定参数**（Closure Evidence Run 捕获
        # `syntax error at or near "$1"`）；set_config 的 is_local=true 与 SET LOCAL
        # 语义等价（事务级），且可参数化。
        if self._tenant_id is not None and session.get_bind().dialect.name == "postgresql":
            session.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"),
                {"tid": str(self._tenant_id)},
            )
        # 第一层防线：向每个 repo 注入 tenant_id（显式过滤的承载点）。
        self.users = SqlAlchemyUserRepository(session, self._tenant_id)
        self.channels = SqlAlchemyChannelRepository(session, self._tenant_id)
        self.agents = SqlAlchemyAgentRepository(session, self._tenant_id)
        self.identities = SqlAlchemyIdentityRepository(session, self._tenant_id)
        self.connections = SqlAlchemyConnectionRepository(session, self._tenant_id)
        self.connection_conversations = SqlAlchemyConnectionConversationRepository(
            session, self._tenant_id)
        self.bootstrap_arms = SqlAlchemyBootstrapArmRepository(
            session, self._tenant_id)
        self.sessions = SqlAlchemySessionRepository(session, self._tenant_id)
        self.interactions = SqlAlchemyInteractionRecordRepository(session, self._tenant_id)
        self.activities = SqlAlchemyActivityRecordRepository(session, self._tenant_id)
        # Browser Auth：构造不带 tenant（bootstrap 按 login / session_id 定位）。
        self.browser_auth = SqlAlchemyBrowserAuthRepository(session)
        # tenants：root catalog，构造不带 tenant（bootstrap；唯一写路径 = TenantProvisioner）。
        self.tenants = SqlAlchemyTenantRepository(session)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is not None:
            self.rollback()
        self.close()

    def commit(self) -> None:
        """显式提交整个事务边界内的全部变更。"""
        self.session.commit()

    def rollback(self) -> None:
        """显式回滚整个事务边界内的全部变更。"""
        self.session.rollback()

    def close(self) -> None:
        """关闭 Session 并释放 Repository 引用（Session 不泄漏）。"""
        session, self.session = self.session, None
        self.users = None
        self.channels = None
        self.agents = None
        self.identities = None
        self.connections = None
        self.connection_conversations = None
        self.bootstrap_arms = None
        self.sessions = None
        self.interactions = None
        self.activities = None
        self.browser_auth = None
        self.tenants = None
        if session is not None:
            session.close()
