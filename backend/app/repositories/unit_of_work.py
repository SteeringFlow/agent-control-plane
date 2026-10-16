"""UnitOfWork Port（Step 6.1，Management Application Boundary）。

表达的是「Application 所需要的事务协作能力」——非常薄：

    users / channels / agents / identities / connections   仓库端口
    sessions                                          会话仓库端口（9.3 起只有读路径）
    interactions / activities                         记录仓库端口（10.4.2 Interaction 持久化）
    connection_conversations                          授权状态仓库端口
                                                      （Implementation Package 2 / Step 1）
    __enter__ / __exit__                              上下文
    commit / rollback                                 事务边界

关键边界（刻意写死）：

- 只是 Port，不是框架：不做 GenericUnitOfWork / TransactionManager /
  AbstractPersistenceService / RepositoryManager / ApplicationRepositoryFactory；
- 不 import SQLAlchemy / persistence——实现见
  app/persistence/unit_of_work.py::SqlAlchemyUnitOfWork（结构满足本协议）；
- Application 只依赖本端口，从而保证：

      Application → Port → Persistence Adapter
      而非       Application → SQLAlchemy

Port 上的属性类型引用仓库端口（同层 repositories/），不引用 Adapter。
"""

from typing import Protocol

from app.repositories.activity_record import ActivityRecordRepository
from app.repositories.agent import AgentRepository
from app.repositories.bootstrap_arm import BootstrapArmRepository
from app.repositories.browser_auth import BrowserAuthRepository
from app.repositories.channel import ChannelRepository
from app.repositories.connection import ConnectionRepository
from app.repositories.connection_conversation import ConnectionConversationRepository
from app.repositories.identity import IdentityRepository
from app.repositories.interaction_record import InteractionRecordRepository
from app.repositories.session import SessionRepository
from app.repositories.tenant import TenantRepository
from app.repositories.user import UserRepository


class UnitOfWork(Protocol):
    """事务协作能力端口。"""

    users: UserRepository
    channels: ChannelRepository
    agents: AgentRepository
    identities: IdentityRepository
    connections: ConnectionRepository
    # Implementation Package 2 / Step 1：授权状态（Conversation → Connection）。
    connection_conversations: ConnectionConversationRepository
    # M6.6-C3 / ADR-12：Bootstrap 窗口仓储（**不是**授权状态；不参与 granted 判定）。
    bootstrap_arms: BootstrapArmRepository
    sessions: SessionRepository
    interactions: InteractionRecordRepository
    activities: ActivityRecordRepository
    browser_auth: BrowserAuthRepository
    # Cloud-0.1.2.1：tenants root catalog 写入端口（唯一写入路径 = TenantProvisioner）。
    tenants: TenantRepository

    def __enter__(self) -> "UnitOfWork": ...

    def __exit__(self, exc_type, exc, tb) -> None: ...

    def commit(self) -> None:
        """显式提交整个事务边界内的全部变更。"""
        ...

    def rollback(self) -> None:
        """显式回滚整个事务边界内的全部变更。"""
        ...
