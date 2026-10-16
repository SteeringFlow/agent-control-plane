"""仓储抽象端口（Ports，Step 5.4 First Vertical Slice）。

第一批端口：UserRepository / ChannelRepository / AgentRepository；
Step 6.6 起加入 IdentityRepository（外部身份桥梁，唯一键 (channel_id, external_id)）；
Step 7.1 起加入 ConnectionRepository（Access Path，唯一键 (channel_id, agent_id)）；
Step 9.3 起加入 SessionRepository（会话解析读路径 find_active，
隔离键 (identity_id, connection_id, external_conversation_id)）。
其余（Interaction/Activity）随各自 Slice 落地。

这里只放接口（Protocol）——描述"业务需要什么能力"，
**不含任何实现**，不 import SQLAlchemy / persistence。

与 ``app/persistence/repositories/`` 的分工：

    app/repositories/             = 「需要什么能力」（抽象端口，供 application 依赖）
    app/persistence/repositories/ = 「怎么落地实现」（SQLAlchemy 具体实现）

方法按业务语义命名（get_by_id/create/find_active_by_...），
不做 GenericRepository[T] 式的万能 CRUD。
"""

from app.repositories.agent import AgentRepository
from app.repositories.channel import ChannelRepository
from app.repositories.connection import ConnectionRepository
from app.repositories.identity import IdentityRepository
from app.repositories.session import SessionRepository
from app.repositories.user import UserRepository

__all__ = [
    "AgentRepository", "ChannelRepository", "ConnectionRepository",
    "IdentityRepository", "SessionRepository", "UserRepository",
]
