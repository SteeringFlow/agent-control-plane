"""Access Authorization Execution（Implementation Package 2 / Step 2）。

把 Gate 2 设计的 Authorization 阶段落成可执行的 Runtime 组件：

    (channel_id, external_conversation_id)                ← 判定输入（恰好两项）
        ↓ ConnectionConversationRepository.find_granted_connection_ids
        ↓ granted connection ids                          ← 该 Conversation 的许可集合
        ├── 空集 → AccessAuthorizationDeniedError（UNAUTHORIZED，STOP）
        └── 非空 → AccessAuthorization
        ↓
    Routing（消费 granted 集合收窄候选）

边界（写死，逐条都可被机器守卫审计）：

- **不做唯一性裁决**：`|granted|` 是 1 还是多条**都不是授权的事**——选择归 Router
  （Package 2-B §B6）；
- **不消费 Connection 状态**：本组件只读 `connection_conversations`，
  不查 `connections.status`、不查 Connection 是否存在——因此「有许可但路径不可用」
  会自然落到 Routing 的 `ROUTING_FAILED`，不会被误报成 `UNAUTHORIZED`；
- **不消费 Identity / User / content**（ADR-1 / ADR-3 红线）：判定输入只有两个键；
  未绑定 ACP User 的身份与已绑定身份走**同一条**路径；
- **不创建任何东西**（ADR-5 / ADR-6）：无 grant 就 DENY，绝不「首次见到即授权」，
  也绝不为「让判定通过」补建 Identity / User / 授权记录；
- **不做策略求值**（ADR-9）：没有 effect / 优先级 / 条件组合 / 继承；
- 是 **Execution** 组件，不是 Service / Manager / Orchestrator（Gate 2 §3.3）：
  不创建 AuthorizationService / PolicyEngine / DecisionPoint / Manager。
"""

from collections.abc import Callable

from app.application.errors import AccessAuthorizationDeniedError
from app.domain.access_authorization import AccessAuthorization
from app.repositories.unit_of_work import UnitOfWork


class AccessAuthorizationExecutor:
    """Access Authorization 执行组件：按会话键判定许可集合（Fail Closed）。"""

    def __init__(self, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    def execute(
        self, channel_id: int, external_conversation_id: str
    ) -> AccessAuthorization:
        """判定该 Conversation 的许可集合；无许可 → `AccessAuthorizationDeniedError`。

        输入不合法（`channel_id` 非正整数 / `external_conversation_id` 空白）与
        「查不到任何授权记录」**一律 Fail Closed**，且不猜测：

            granted == ∅  ⇔  DENY        （ADR-6：缺授权记录 = 不允许）
            granted ≠ ∅  ⇔  ALLOW        （允许必须显式成立）

        返回值只携带许可集合——是否真的可用（存在 / active / 唯一）由 Routing 判。
        """
        if (
            not isinstance(channel_id, int)
            or channel_id <= 0
            or not isinstance(external_conversation_id, str)
            or not external_conversation_id.strip()
        ):
            raise AccessAuthorizationDeniedError(channel_id, external_conversation_id)

        with self._uow_factory() as uow:
            granted = uow.connection_conversations.find_granted_connection_ids(
                channel_id, external_conversation_id
            )

        if not granted:
            raise AccessAuthorizationDeniedError(channel_id, external_conversation_id)
        return AccessAuthorization(granted_connection_ids=granted)
