"""Session Creation Execution（Step 9.7）—— 把 9.3 的 CREATE_NEW 决策物化为真实 CP Session。

为什么现在才做：因为完整链条（Identity → Session Resolution → Routing → Dispatch
→ Delivery）真正跑起来后，若 `CREATE_NEW` 却拿不到 `control_plane_session_id`，
9.5 的 `AgentRequest.control_plane_session_id` 就无法产生——这不是「未来可能需
要」，而是 First E2E 的**实际前置条件**。

    CREATE_NEW（9.3 决策，existing_session_id = None）
        ↓ SessionRepository.create（9.7 最小写路径）
        ↓ 真实 Control Plane Session（id = 将来传给 Dispatch 的
          control_plane_session_id）

**不是重新设计 Session**：不创建 SessionManager / SessionLifecycleService /
SessionService / SessionFactory / ConversationManager。

字段来源（避免自己 Routing）：

- `identity_id` / `external_conversation_id`：来自 9.3 Session Resolution；
- `connection_id`：来自 9.4 RoutingResult（Routing 已选定的 Connection）；
- `channel_id`：派生自已确定的对象（Identity.channel_id / Connection.channel_id），
  由调用方传入——本组件不重新解析；
- `agent_id`：**必须来自 RoutingResult.agent_id / 已选定的 Connection**——本组件
  **绝不自己找 Agent**（不 import Agent/Connection 仓储查询）；
- `external_session_id` / `metadata`：可选（Agent 侧引用 / 附加信息）。

幂等性（M6.6-C4 / CP4-1 更新）：sessions 的会话隔离键已由 0002（tenant 隔离演进）
物化为租户前导的 DB 唯一约束。并发 `CREATE_NEW` 不再由本组件承担竞争恢复——它只表达
**创建意图**，交给 `SessionRepository.find_or_create`（命中复用；竞争输家回滚嵌套事务后
按精确四元组回读复用）。不引入分布式锁 / 事务框架 / Event Sourcing / Saga / Redis。

Fail Closed：输入不合法或持久化失败 → 复用 Session Resolution 阶段失败语义
（SESSION_RESOLUTION_FAILED：拿不到 CP Session = 决议无法落地 → STOP）。并发竞争
**本身不产生 failure**：只有当回读仍找不到 Session 时才落该码。
"""

from collections.abc import Callable

from app.application.errors import SessionResolutionFailedError
from app.domain.session import Session
from app.repositories.unit_of_work import UnitOfWork


class SessionCreationExecutor:
    """Session Creation 执行组件：把 CREATE_NEW 决策物化为真实 CP Session。

    不重新 Routing / 不重新解析 / 不自己找 Agent——所有字段来自已确定的 Resolution +
    Routing 结果；并发去重（唯一约束冲突 → 复用）交由
    `SessionRepository.find_or_create`，本组件不承担数据库竞争恢复。
    """

    def __init__(self, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    def execute(
        self,
        *,
        identity_id: int,
        channel_id: int,
        connection_id: int,
        agent_id: int,
        external_conversation_id: str,
        external_session_id: str | None = None,
        metadata: dict | None = None,
    ) -> Session:
        """创建一条真实 Control Plane Session 并返回带 id 的实例（可作 CP Session id）。"""
        # ---- Fail Closed：字段形状（来源正确性由 Resolution/Routing 保证）----
        if not all(isinstance(v, int) and v > 0 for v in (
                identity_id, channel_id, connection_id, agent_id)):
            raise SessionResolutionFailedError(
                identity_id, connection_id, external_conversation_id)
        if not isinstance(external_conversation_id, str) \
                or not external_conversation_id.strip():
            raise SessionResolutionFailedError(
                identity_id, connection_id, external_conversation_id)
        if external_session_id is not None \
                and not isinstance(external_session_id, str):
            raise SessionResolutionFailedError(
                identity_id, connection_id, external_conversation_id)
        if metadata is not None and not isinstance(metadata, dict):
            raise SessionResolutionFailedError(
                identity_id, connection_id, external_conversation_id)

        session = Session(
            identity_id=identity_id,
            channel_id=channel_id,
            connection_id=connection_id,
            agent_id=agent_id,
            external_conversation_id=external_conversation_id,
            external_session_id=external_session_id,
            metadata=metadata,
        )
        try:
            with self._uow_factory() as uow:
                # 并发 `CREATE_NEW` 由 Port 的 find_or_create 收敛：命中复用 / 唯一冲突
                # 输家回滚嵌套事务后按精确四元组回读复用；回读不到才抛出 → Fail Closed。
                created = uow.sessions.find_or_create(session)
                uow.commit()
        except SessionResolutionFailedError:
            raise
        except Exception as exc:  # FK / 持久化失败 → 决议无法落地 → Fail Closed
            raise SessionResolutionFailedError(
                identity_id, connection_id, external_conversation_id) from exc
        return created
