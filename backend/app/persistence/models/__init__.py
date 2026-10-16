"""8 张核心业务表 ORM + tenants root catalog（Step 5.3 / Cloud-0.1.1-B）。
+ Cloud-0.1.1-C 的 2 张 Browser Auth 表（user_credentials / browser_sessions）。
+ Implementation Package 2 的授权状态表 `connection_conversations`。

导入本包即完成 Base.metadata 注册（12 张表：8 张 4.2 冻结业务表 + Cloud-0.1.1-B
引入的 root 表 `tenants` + Cloud-0.1.1-C 引入的 `user_credentials` /
`browser_sessions` + Implementation Package 2 引入的 `connection_conversations`
（V0 唯一授权状态存储，Package 2-C §1.5 窄范围例外））。

声明约定：
- 只声明 Schema（PK/FK/唯一约束/索引），不写 relationship()——
  MVP 需要的是「FK + 正确 Schema」，需要哪些 relationship 由
  5.4 Repository 的真实查询决定，不预先构造 Object Graph；
- `metadata` 列在 ORM 层统一命名为 `extra_metadata`
  （metadata 是 SQLAlchemy Declarative 保留属性名）；
- ID 统一 INTEGER 自增主键（MVP 不引入 UUID/ULID/雪花）；
- 时间统一 created_at/updated_at，UTC 存储（SQLite CURRENT_TIMESTAMP 即 UTC），
  展示层再做时区转换；
- activities 无 updated_at：append-only；
- Cloud-0.1.1-B：8 张业务表各新增 `tenant_id`（FK → tenants.id, NOT NULL,
  default=1）——纯加性分区维度，不改变任何既有列 / 既有 FK / 业务语义。

禁止在本包出现：Repository / Mapper / Service / 新业务实体。
"""

from app.persistence.models.tenant import TenantModel
from app.persistence.models.activity import ActivityModel
from app.persistence.models.agent import AgentModel
from app.persistence.models.browser_session import BrowserSessionModel
from app.persistence.models.channel import ChannelModel
from app.persistence.models.connection import ConnectionModel
from app.persistence.models.connection_conversation import ConnectionConversationModel
from app.persistence.models.credential import UserCredentialModel
from app.persistence.models.identity import IdentityModel
from app.persistence.models.interaction import InteractionModel
from app.persistence.models.session import SessionModel
from app.persistence.models.user import UserModel

__all__ = [
    "ActivityModel",
    "AgentModel",
    "BrowserSessionModel",
    "ChannelModel",
    "ConnectionConversationModel",
    "ConnectionModel",
    "IdentityModel",
    "InteractionModel",
    "SessionModel",
    "TenantModel",
    "UserCredentialModel",
    "UserModel",
]
