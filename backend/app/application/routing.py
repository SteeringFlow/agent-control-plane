"""Routing Execution（Step 9.4）—— 8.4 语义的第一次真实执行。

8.4 已经把 Routing 压得非常小，9.4 只是把它执行出来：

    channel_id + granted_connection_ids（Implementation Package 2 / Step 2 起）
        ↓ ConnectionRepository.find_active_by_channel（9.4 新增最小读路径）
        ↓ active Connections
        ↓ 与已授权候选取交集（ADR-8：Router 只消费已授权候选）
        ↓ eligible = active ∩ granted
        ↓ 数量判定（Fail Closed）
        ↓
    0 条  → RoutingFailedError
    1 条  → RoutingResult(connection_id=selected.id, agent_id=selected.agent_id)
    >1 条 → RoutingFailedError

**ADR-8 的落点（Step 2）**：Router 的候选集从「该 Channel 的全部 active
Connection」收窄为「已授权集合」，因此 Router **不自行扩大候选集**。但
**授权只收窄候选**——Connection 的存在性、`status`、数量判定**仍完全归 Router**：
「有许可但连接全部 inactive」→ `ROUTING_FAILED`（不是 `UNAUTHORIZED`）；
「有许可但 eligible 多于一条」→ 同样 `ROUTING_FAILED`（授权不做唯一性裁决）。
Router **不查询授权记录**，`granted_connection_ids` 由调用方（Coordinator）经
Authorization 阶段传入。

职责边界（8.4 冻结）：

    Routing
      ├── selects Connection            ✅（唯一职责）
      ├── derives agent_id              ✅（派生自所选 Connection）
      ├── Identity Resolution           ❌（9.2 已完成）
      ├── Session Resolution            ❌（9.3 已完成）
      ├── Agent Dispatch                ❌（9.5）
      ├── fallback / priority / weight  ❌
      ├── load balancing / round robin  ❌
      └── policy / routing rules        ❌

**agent_id 是 Connection 的派生结果，不是 Routing 重新选择的对象**：
选了 Connection 10（agent_id=7）就不可能得出 agent_id=9。

**MVP 选择输入 = channel_id + 已授权候选集**：identity_id /
external_conversation_id / content 都不参与选择——Routing 与
Identity/Session Resolution 之间**没有隐式耦合**。已授权候选集是**唯一**新增
输入（Step 2），它为必填：不提供就等于「候选不受限」，那会把被禁的
「恒 ALLOW 占位」从后门放回来。若未来真实用例证明「仅靠这两项不够」，
再按真实案例升级，现在不提前设计规则系统。

依赖边界：只依赖 Repository / UoW Port——不 import SQLAlchemy / ORM /
persistence / adapters；不 import IdentityRepository / SessionRepository
（不重新解析）。
"""

from collections.abc import Callable

from app.application.errors import RoutingFailedError
from app.domain.routing_result import RoutingResult
from app.repositories.unit_of_work import UnitOfWork


class RoutingExecutor:
    """Routing 执行组件：在**已授权候选**中选出唯一确定的 Agent Access Path。"""

    def __init__(self, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    def execute(
        self, channel_id: int, granted_connection_ids: frozenset[int]
    ) -> RoutingResult:
        """在该 Channel 的 active Connection ∩ 已授权候选中选出唯一一条，并派生 agent_id。

        `granted_connection_ids` 是 **Access Authorization 的产出**（ADR-8）：
        Router 只消费它、绝不自行扩大候选集；它**不是**可选的——省略即等于
        「候选不受限」，那是被禁止的占位授权。

        Fail Closed：channel_id 不合法，或 eligible（active ∩ granted）数量 != 1
        （0 条 / 多条）→ `RoutingFailedError`（不猜、不 fallback、不做优先级）。
        """
        if not isinstance(channel_id, int) or channel_id <= 0:
            raise RoutingFailedError(channel_id)

        with self._uow_factory() as uow:
            active = uow.connections.find_active_by_channel(channel_id)

        # ADR-8：先收窄（授权），再选择（路由）。存在性 / status 仍由本组件判定；
        # 授权不参与唯一性裁决，也不裁定 Connection 状态。
        connections = [c for c in active if c.id in granted_connection_ids]

        # 结果必须唯一且确定：0 条或 >1 条都无法唯一确定 Access Path
        if len(connections) != 1:
            raise RoutingFailedError(channel_id)
        selected = connections[0]
        return RoutingResult(
            connection_id=selected.id, agent_id=selected.agent_id
        )
