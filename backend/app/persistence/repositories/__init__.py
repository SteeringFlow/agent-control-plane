"""仓储端口的 SQLAlchemy 实现（Step 5.4 First Vertical Slice）。

第一批实现：SqlAlchemyUserRepository / SqlAlchemyChannelRepository /
SqlAlchemyAgentRepository；6.6 加入 SqlAlchemyIdentityRepository；
7.1 加入 SqlAlchemyConnectionRepository（Domain Contract 层）。
其余随各自 Slice 落地。

实现 ``app/repositories/`` 中声明的端口；Session 只在本层出现，
由调用方注入（来自持久化基础设施，不是 Application 自己创建）。
本层不 commit——事务边界属于 Unit of Work。
"""

from app.persistence.repositories.agent import SqlAlchemyAgentRepository
from app.persistence.repositories.channel import SqlAlchemyChannelRepository
from app.persistence.repositories.connection import SqlAlchemyConnectionRepository
from app.persistence.repositories.identity import SqlAlchemyIdentityRepository
from app.persistence.repositories.user import SqlAlchemyUserRepository

__all__ = [
    "SqlAlchemyAgentRepository",
    "SqlAlchemyChannelRepository",
    "SqlAlchemyConnectionRepository",
    "SqlAlchemyIdentityRepository",
    "SqlAlchemyUserRepository",
]
