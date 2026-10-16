"""AgentRepository 端口（Port）。

只描述业务需要的查询/写入能力，不含实现，不 import SQLAlchemy / persistence。
实现见 app/persistence/repositories/agent.py（SqlAlchemyAgentRepository）。

方法按 Agent 管理语义命名（6.2 起加入 update / find_by_name），
不做 GenericRepository[T] 万能 CRUD。
"""

from typing import Protocol, runtime_checkable

from app.domain.agent import Agent


@runtime_checkable
class AgentRepository(Protocol):
    """Agent 仓储：按业务语义命名的方法，不是 Generic CRUD。"""

    def get_by_id(self, agent_id: int) -> Agent | None:
        """按主键取 Agent；不存在返回 None。"""
        ...

    def create(self, agent: Agent) -> Agent:
        """写入 Agent 并返回带 id 的新实例。"""
        ...

    def update(self, agent: Agent) -> Agent:
        """把 Agent 上的字段覆写到已存在的行（id 必须存在）；返回更新后的实例。"""
        ...

    def find_by_name(self, name: str) -> Agent | None:
        """按 name 精确查找（管理面应用级去重守卫用）；不存在返回 None。"""
        ...

    def list_agents(self) -> list[Agent]:
        """11.3：List 用例——返回全部 Agent（管理面陈列/选择用）。"""
        ...
