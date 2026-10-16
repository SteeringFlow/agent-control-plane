"""SQLAlchemy AgentRepository 实现（Adapter，实现 app/repositories 端口）。

Session 由调用方注入；本层不 commit（事务边界属 5.5 Unit of Work）。
Cloud-0.1.1-B：可选 `tenant_id` → 显式租户过滤（第一层防线）。
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.agent import Agent
from app.persistence.mappers.agent import apply_to_orm, to_domain, to_orm
from app.persistence.models.agent import AgentModel
from app.repositories.agent import AgentRepository


class SqlAlchemyAgentRepository:
    """实现 AgentRepository 协议。"""

    def __init__(self, session: Session, tenant_id: int | None = None) -> None:
        self._session = session
        self._tenant_id = tenant_id

    def get_by_id(self, agent_id: int) -> Agent | None:
        if self._tenant_id is None:
            row = self._session.get(AgentModel, agent_id)
        else:
            row = self._session.execute(
                select(AgentModel).where(
                    AgentModel.id == agent_id,
                    AgentModel.tenant_id == self._tenant_id,
                )
            ).scalar_one_or_none()
        return to_domain(row) if row is not None else None

    def create(self, agent: Agent) -> Agent:
        row = to_orm(agent)
        if self._tenant_id is not None:
            row.tenant_id = self._tenant_id
        self._session.add(row)
        self._session.flush()
        # 刷新以取回 server_default 生成的时间戳（created_at/updated_at），
        # 保证 create 返回值与随后的 get_by_id 读值一致（11.3 审计回显）。
        self._session.refresh(row)
        return to_domain(row)

    def update(self, agent: Agent) -> Agent:
        if self._tenant_id is None:
            row = self._session.get(AgentModel, agent.id)
        else:
            row = self._session.execute(
                select(AgentModel).where(
                    AgentModel.id == agent.id,
                    AgentModel.tenant_id == self._tenant_id,
                )
            ).scalar_one_or_none()
        # Application 用例在同一 UoW 内先 get_by_id 确认存在才走到这里；
        # 防御性兜底，避免把不存在的更新静默吞掉。
        if row is None:
            raise ValueError(f"更新目标不存在：Agent id={agent.id}")
        apply_to_orm(agent, row)
        self._session.flush()
        # 刷新以取回 onupdate 生成的新 updated_at（11.3 审计回显）。
        self._session.refresh(row)
        return to_domain(row)

    def list_agents(self) -> list[Agent]:
        """11.3：管理面 List 用例——返回全部 Agent（按 id 升序）。

        MVP 不做分页/过滤（11.2 §5.4 裁决：无真实消费者前不过度建设）。
        Cloud-0.1.1-B：租户作用域下只返回本租户行。
        """
        stmt = select(AgentModel)
        if self._tenant_id is not None:
            stmt = stmt.where(AgentModel.tenant_id == self._tenant_id)
        rows = self._session.execute(stmt.order_by(AgentModel.id)).scalars().all()
        return [to_domain(row) for row in rows]

    def find_by_name(self, name: str) -> Agent | None:
        stmt = select(AgentModel).where(AgentModel.name == name)
        if self._tenant_id is not None:
            stmt = stmt.where(AgentModel.tenant_id == self._tenant_id)
        row = self._session.execute(stmt).scalar_one_or_none()
        return to_domain(row) if row is not None else None
