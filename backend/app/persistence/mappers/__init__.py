"""ORM 行与领域对象之间的双向映射。

Step 5.4：User / Channel / Agent 三个 Mapper 进入使用状态
（对应 First Vertical Slice），其余等各自 Slice 落地。

映射的独有职责：to_domain（读路径）与 to_orm（写路径）。
不承载业务规则；业务规则归 domain / application。
"""

from app.persistence.mappers.agent import apply_to_orm as agent_apply_to_orm
from app.persistence.mappers.agent import to_domain as agent_to_domain
from app.persistence.mappers.agent import to_orm as agent_to_orm
from app.persistence.mappers.channel import apply_to_orm as channel_apply_to_orm
from app.persistence.mappers.channel import to_domain as channel_to_domain
from app.persistence.mappers.channel import to_orm as channel_to_orm
from app.persistence.mappers.connection import apply_to_orm as connection_apply_to_orm
from app.persistence.mappers.connection import to_domain as connection_to_domain
from app.persistence.mappers.connection import to_orm as connection_to_orm
from app.persistence.mappers.identity import apply_to_orm as identity_apply_to_orm
from app.persistence.mappers.identity import to_domain as identity_to_domain
from app.persistence.mappers.identity import to_orm as identity_to_orm
from app.persistence.mappers.user import apply_to_orm as user_apply_to_orm
from app.persistence.mappers.user import to_domain as user_to_domain
from app.persistence.mappers.user import to_orm as user_to_orm

__all__ = [
    "agent_apply_to_orm",
    "agent_to_domain",
    "agent_to_orm",
    "channel_apply_to_orm",
    "channel_to_domain",
    "channel_to_orm",
    "connection_apply_to_orm",
    "connection_to_domain",
    "connection_to_orm",
    "identity_apply_to_orm",
    "identity_to_domain",
    "identity_to_orm",
    "user_apply_to_orm",
    "user_to_domain",
    "user_to_orm",
]
