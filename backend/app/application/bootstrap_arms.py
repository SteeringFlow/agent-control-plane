"""Bootstrap Arm 管理面用例（M6.6-C3 / ADR-12）。

只做一件事：**为一条已存在的 Connection 开启一个受限的 Bootstrap 窗口**。

    Tenant Admin（已认证会话）
        ↓ 显式 Verify Connection
    Bootstrap Arm（bounded / one-shot / scoped / attributable / auditable）

边界（写死）：

- **Existing Objects Only**：只作用于已存在的 Connection；Channel 由 Connection
  服务端解析，**请求体不接受** `tenant_id` / `channel_id` / `initiator_user_id`
  （防 scope 错配与身份伪造，ADR-12 §16）；
- **不校验 `Connection.status`**：与既有 Grant 创建同形
  （`app/application/connection_conversations.py:74-93` 只校验存在性 + 归属 +
  唯一性）⇒ 不需要新的错误类；Connection 非 active 时首条消息自然落到既有
  `ROUTING_FAILED`；
- **不创建** ACP User / Agent / Channel / Connection，也不修改其中任何一个；
- **不参与**授权判定：Arm 不是授权状态，授权仍只有 `connection_conversations`。
"""

from collections.abc import Callable
from dataclasses import dataclass

from app.application.errors import (
    ConnectionNotFoundError,
    InvalidConnectionInputError,
)
from app.domain.bootstrap_arm import BootstrapArm
from app.repositories.unit_of_work import UnitOfWork

#: Arm 窗口长度（秒）——**代码常量，不是配置项**。
#:
#: 刻意不新增配置维度：Cloud V0 冻结 §3.2 明列「新配置项」非 release blocker；
#: 且本设计不需要运行时可调。单事实源就在本模块。
BOOTSTRAP_ARM_TTL_SECONDS = 600


@dataclass(frozen=True)
class ArmConnection:
    """开启 Bootstrap 窗口的输入契约（应用层校验点，非 Pydantic/API schema）。

    只有 `connection_id`：其余 scope（tenant / channel）与 initiator 全部由
    **服务端**解析，绝不由调用方声明。
    """

    connection_id: int


def _require_positive_id(value: int, field: str) -> None:
    if not isinstance(value, int) or value <= 0:
        raise InvalidConnectionInputError(f"{field} 必须是正整数：{value!r}")


class BootstrapArmApplicationService:
    """Bootstrap Arm 用例编排者（依赖 UoW Port，而非具体 SQLAlchemy 实现）。"""

    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        *,
        initiator_user_id: int,
    ) -> None:
        """`initiator_user_id` 是**已认证操作者**的 user_id（O-4，必填）。

        没有已认证主体 ⇒ 本服务**根本不会被构造**（端点依赖层直接 401）。
        因此它绝不可能是 anonymous / guest / system / sentinel。
        """
        self._uow_factory = uow_factory
        self._initiator_user_id = initiator_user_id

    def arm_connection(self, command: ArmConnection) -> BootstrapArm:
        """为一条已存在的 Connection 开启（或取代）Bootstrap 窗口。

        校验顺序（沿用既有管理面先例，不发明）：

            1. 输入形状（正整数 connection_id）      → Invalid…（422）
            2. connection 存在性（tenant 作用域内）  → ConnectionNotFoundError（404）

        supersede 语义（O-1 / CP3-D §12）：同一 scope 上既有 `armed` 行在同一事务内
        被置为 `revoked`，随后插入新 `armed` 行 ⇒ 重复 Verify 是**刷新窗口**，
        且「至多一条 ARMED」由 **DB 部分唯一索引**最终保证。
        """
        _require_positive_id(command.connection_id, "connection_id")
        with self._uow_factory() as uow:
            connection = uow.connections.get_by_id(command.connection_id)
            if connection is None:
                raise ConnectionNotFoundError(command.connection_id)
            # channel 由 **服务端**从 Connection 解析（绝不采信调用方输入）。
            assert connection.id is not None
            arm = uow.bootstrap_arms.arm(
                BootstrapArm(
                    channel_id=connection.channel_id,
                    connection_id=connection.id,
                    initiator_user_id=self._initiator_user_id,
                ),
                ttl_seconds=BOOTSTRAP_ARM_TTL_SECONDS,
            )
            uow.commit()
        return arm

    def get_arm(self, arm_id: int) -> BootstrapArm | None:
        """按 id 读回一条 Arm（诊断 / 测试断言用；跨租户不可见 ⇒ None）。"""
        _require_positive_id(arm_id, "arm_id")
        with self._uow_factory() as uow:
            return uow.bootstrap_arms.get_by_id(arm_id)
