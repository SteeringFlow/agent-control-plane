"""AgentMapper：AgentModel <-> Agent。"""

from app.domain.agent import Agent
from app.persistence.models.agent import AgentModel


def to_domain(row: AgentModel) -> Agent:
    return Agent(
        id=row.id,
        name=row.name,
        type=row.type,
        endpoint=row.endpoint,
        status=row.status,
        config=row.config,
        credential_ref=row.credential_ref,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def to_orm(entity: Agent) -> AgentModel:
    return AgentModel(
        name=entity.name,
        type=entity.type,
        endpoint=entity.endpoint,
        status=entity.status,
        config=entity.config,
        credential_ref=entity.credential_ref,
    )


def apply_to_orm(entity: Agent, row: AgentModel) -> None:
    """把 entity 的业务字段覆写到已加载的 ORM 行（update 路径）。

    id 与 created_at 保持不变，只覆写可变字段。
    """
    row.name = entity.name
    row.type = entity.type
    row.endpoint = entity.endpoint
    row.status = entity.status
    row.config = entity.config
    row.credential_ref = entity.credential_ref
