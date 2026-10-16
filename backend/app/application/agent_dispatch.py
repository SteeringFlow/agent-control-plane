"""Agent Dispatch Execution（Step 9.5）—— 8.5 契约第一次真正穿过 Control Plane → Agent。

    RoutingResult(connection_id, agent_id)
        ↓ ConnectionRepository.get_by_id(routing_result.connection_id)   （6.4/7.1 既有方法）
        ↓ Connection（读取 Routing 已选定的 Access Path 的连接信息）
        ↓ AgentRequest（agent_id 来自 RoutingResult.agent_id）
        ↓ AgentAdapter.send(request, connection)
        ↓ AgentResponse

核心原则（8.5 延续）：**Dispatch is execution, not selection.**

- **Agent ID 来源 = RoutingResult.agent_id**：Executor 不自己找 Agent；
- **Connection 来源 = RoutingResult.connection_id**：`get_by_id` 读取 Access Path，
  绝不是 `find_active_by_channel()` **再次选路**；
- 不重新 Routing / 不重新 Identity Resolution / 不重新 Session Resolution；
- `control_plane_session_id`（已确定的 CP Session）≠ `external_session_id`
  （Agent 侧引用）——两者不混淆；
- AgentResponse 无效 → `AgentResponseInvalidError`（AGENT_RESPONSE_INVALID）；
- Adapter transport 失败 → `AgentDispatchFailedError`（AGENT_DISPATCH_FAILED）；
  不新造 AgentConnectionFailed / AgentAuthenticationFailed / AgentProtocolFailed。

依赖边界：
- 只依赖 Repository/UoW Port + `AgentAdapter` Port（`app/repositories/agent_adapter.py`）——
  不 import SQLAlchemy / ORM / persistence / adapters 实现；
- `agent_adapter` 由构造注入：9.5 阶段注入 Test Adapter，真实 ACP Adapter 以后落地
  在 `app/adapters/agents/`（本模块不 import 具体实现）。
"""

from collections.abc import Callable

from app.application.errors import (
    AgentDispatchFailedError,
    AgentResponseInvalidError,
)
from app.domain.agent_request import AgentRequest
from app.domain.agent_response import AgentResponse
from app.domain.artifact import Artifact
from app.domain.connection import Connection
from app.domain.routing_result import RoutingResult
from app.repositories.agent_adapter import AgentAdapter
from app.repositories.unit_of_work import UnitOfWork


class AgentDispatchExecutor:
    """Agent Dispatch 执行组件：把已确定的 AgentRequest 交给已确定的 Agent。

    只执行、不选择：agent_id 与 connection_id 全部来自 RoutingResult。
    """

    def __init__(
        self,
        agent_adapter: AgentAdapter,
        uow_factory: Callable[[], UnitOfWork],
    ) -> None:
        self._agent_adapter = agent_adapter
        self._uow_factory = uow_factory

    def execute(
        self,
        routing_result: RoutingResult,
        control_plane_session_id: int,
        content: str,
        external_session_id: str | None = None,
        metadata: dict | None = None,
    ) -> AgentResponse:
        """把 Routing 已确定的调用请求发给 Routing 已确定的 Agent。

        Fail Closed：
        - RoutingResult / 输入不合法 → AgentDispatchFailedError；
        - Connection 读不到，或 Connection.agent_id 与 RoutingResult.agent_id 不一致
          → AgentDispatchFailedError（不允许「Connection=10 / Agent=9」）；
        - Adapter 失败 → AgentDispatchFailedError；响应不满足契约 →
          AgentResponseInvalidError。
        """
        # ---- 输入 Fail Closed（只消费 Routing 已确定的事实）----
        if not isinstance(routing_result, RoutingResult):
            raise AgentDispatchFailedError()
        if not isinstance(routing_result.connection_id, int) \
                or routing_result.connection_id <= 0:
            raise AgentDispatchFailedError(agent_id=routing_result.agent_id)
        if not isinstance(routing_result.agent_id, int) \
                or routing_result.agent_id <= 0:
            raise AgentDispatchFailedError()
        if not isinstance(control_plane_session_id, int) \
                or control_plane_session_id <= 0:
            raise AgentDispatchFailedError(agent_id=routing_result.agent_id)
        if not isinstance(content, str) or not content.strip():
            raise AgentDispatchFailedError(agent_id=routing_result.agent_id)
        if external_session_id is not None and not isinstance(external_session_id, str):
            raise AgentDispatchFailedError(agent_id=routing_result.agent_id)
        if metadata is not None and not isinstance(metadata, dict):
            raise AgentDispatchFailedError(agent_id=routing_result.agent_id)

        # ---- 读取 Routing 已选定的 Access Path（不是重新选路）----
        with self._uow_factory() as uow:
            connection = uow.connections.get_by_id(routing_result.connection_id)
        if connection is None:
            raise AgentDispatchFailedError(
                agent_id=routing_result.agent_id,
                connection_id=routing_result.connection_id,
            )
        # 一致性 Fail Closed：Connection 与 RoutingResult 必须同指一个 Agent
        if connection.agent_id != routing_result.agent_id:
            raise AgentDispatchFailedError(
                agent_id=routing_result.agent_id,
                connection_id=routing_result.connection_id,
            )

        # ---- 交给 AgentAdapter（UoW 已退出，外部 I/O 不占事务）----
        request = AgentRequest(
            agent_id=routing_result.agent_id,
            control_plane_session_id=control_plane_session_id,
            content=content,
            external_session_id=external_session_id,
            metadata=metadata,
        )
        try:
            response = self._agent_adapter.send(request, connection)
        except AgentDispatchFailedError:
            raise
        except Exception as exc:  # transport / protocol / auth → Dispatch failure
            raise AgentDispatchFailedError(
                agent_id=routing_result.agent_id,
                connection_id=routing_result.connection_id,
            ) from exc

        # ---- 响应必须满足 AgentResponse 契约（Fail Closed）----
        if not self._valid_response(response):
            raise AgentResponseInvalidError(agent_id=routing_result.agent_id)
        return response

    @staticmethod
    def _valid_response(response: object) -> bool:
        if not isinstance(response, AgentResponse):
            return False
        if not isinstance(response.content, str) or not response.content.strip():
            return False
        if response.external_session_id is not None \
                and not isinstance(response.external_session_id, str):
            return False
        if response.metadata is not None and not isinstance(response.metadata, dict):
            return False
        # ACP V0.2：artifacts 可选（None = 旧 Agent，天然合法）；携带时必须逐项
        # 满足 Artifact 契约（Fail Closed）——非法 → AGENT_RESPONSE_INVALID。
        artifacts = response.artifacts
        if artifacts is not None:
            if not isinstance(artifacts, list):
                return False
            if not all(AgentDispatchExecutor._valid_artifact(a) for a in artifacts):
                return False
        return True

    @staticmethod
    def _valid_artifact(artifact: object) -> bool:
        """Artifact 契约校验（Fail Closed）：必填 str 非空；可选字段类型正确。"""
        if not isinstance(artifact, Artifact):
            return False
        if not isinstance(artifact.artifact_id, str) \
                or not artifact.artifact_id.strip():
            return False
        if not isinstance(artifact.kind, str) or not artifact.kind.strip():
            return False
        if not isinstance(artifact.name, str) or not artifact.name.strip():
            return False
        if artifact.uri is not None and not isinstance(artifact.uri, str):
            return False
        if artifact.mime_type is not None and not isinstance(artifact.mime_type, str):
            return False
        if artifact.size_bytes is not None and not isinstance(artifact.size_bytes, int):
            return False
        if artifact.metadata is not None and not isinstance(artifact.metadata, dict):
            return False
        return True
