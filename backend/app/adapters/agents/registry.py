"""MultiAgentAdapter —— type → AgentAdapter 分派（Step 15.2.3）。

位置：`app/adapters/agents/registry.py`。实现 `AgentAdapter` Port
（`app/repositories/agent_adapter.py`），内部按 `Agent.type` 分派到具体 Adapter：

    AgentDispatchExecutor
        ↓  AgentAdapter（不知道 acp / local 的存在）
    MultiAgentAdapter
        ├── "acp"   → ACPAdapter
        └── "local" → LocalAgentAdapter

**Runtime 不认识具体 Agent 类型**：`AgentDispatchExecutor` 只看见一个
`AgentAdapter`；类型集合完全收口在组合根（`main.py`）。新增第三种 Agent
（openai / custom / ...）只需在此登记映射，不改 Runtime / Executor / Domain /
Persistence —— 这是「Dispatch is execution, not selection」在 Adapter 层的延续。

命名纪律（8.x / 6.x 守卫）：本模块不是 Router / Resolver / Dispatcher ——
不选 Agent、不做规则匹配、不决定路由；agent_id 已是 Routing 的确定结果，
本模块只为「按 type 选择协议实现」读回 agent.type（与 WeComAdapter 出站按
channel_id 读 config 同一模式）。

失败语义：agent 不存在 / type 无对应实现 → `AgentDispatchFailedError`
（不 fallback、不新造错误码）。
"""

from collections.abc import Callable

from app.application.errors import AgentDispatchFailedError
from app.domain.agent_request import AgentRequest
from app.domain.agent_response import AgentResponse
from app.domain.connection import Connection
from app.repositories.agent_adapter import AgentAdapter
from app.repositories.unit_of_work import UnitOfWork


class MultiAgentAdapter:
    """按 `Agent.type` 把 `send` 交给对应 Adapter（组合根装配的唯一 Agent 出口）。"""

    def __init__(
        self,
        adapters: dict[str, AgentAdapter],
        *,
        uow_factory: Callable[[], UnitOfWork],
    ) -> None:
        self._adapters = dict(adapters)
        self._uow_factory = uow_factory

    def send(self, request: AgentRequest, connection: Connection) -> AgentResponse:
        """读 agent.type → 交给对应 Adapter；无对应实现 → AGENT_DISPATCH_FAILED。"""
        connection_id = connection.id if isinstance(connection, Connection) else None
        agent_type = self._load_agent_type(request.agent_id)
        adapter = self._adapters.get(agent_type)
        if adapter is None:
            raise AgentDispatchFailedError(
                agent_id=request.agent_id, connection_id=connection_id)
        return adapter.send(request, connection)

    def _load_agent_type(self, agent_id: int) -> str | None:
        """按 agent_id 读回 `Agent.type`（只读，用于选择协议实现）。"""
        with self._uow_factory() as uow:
            agent = uow.agents.get_by_id(agent_id)
        return agent.type if agent is not None else None
