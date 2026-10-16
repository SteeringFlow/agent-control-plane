"""Identity Resolution Execution（Step 9.2）—— 8.2 契约的第一次真实执行。

8.2 冻结的是「语义契约」（不碰 Persistence）；9.2 正式允许 Runtime Application
通过 Repository Port 使用 Persistence：

    Runtime Identity Execution
        │ channel_id / external_identity_id
        ▼
    IdentityRepository.find_by_channel_and_external_id   （6.6 已冻结，无新方法）
        ▼
    Identity                                             ← 命中即解析成功
        │ user_id（**可为 None**：未绑定 ACP Account）
        ▼
    UserRepository.get_by_id                             （6.4 已冻结，无新方法）
        ▼
    User | None                                          ← 可选的管理关联
        ▼
    IdentityResolution(identity=…, user=None | User)

边界（机器化校验见 tests/test_step9_2_identity_execution.py）：

- 是 **Execution** 组件，不是 Resolver：本模块不创建 IdentityResolver /
  IdentityResolutionService / IdentityLinker / IdentityMatcher /
  IdentityResolutionPolicy ——「resolution」语义不构成创建 Resolver 概念的理由；
- **成功条件（Gate 2.6 D3 / Implementation Package 1）**：
  Identity 命中即 RESOLVED；**User 存在与否不再决定成败**——
  未绑定（`user_id is None`）是**正常结果**（`user = None`），不是失败；
- Fail Closed：**未找到 Identity** → `IdentityResolutionFailedError`（STOP）；
  **绝不创建** Identity / User，绝不猜测（无 fuzzy / email / phone matching /
  自动 linking）——「user 缺失」**不得**被转换成「那就补一个 User」；
- Channel 隔离：(channel_id, external_id) 是唯一键（4.2/6.6 冻结）——同一
  external_id 跨 Channel 是两个独立查找空间，绝不跨渠道合并；
- 依赖边界：只依赖 Repository / UoW Port——不 import SQLAlchemy / ORM /
  persistence / adapters；不 import IdentityApplicationService
  （6.6 Management ≠ Runtime Resolution）；
- 不修改 IdentityRepository / UserRepository surface：只消费既有冻结方法，
  Runtime 不反过来污染 Repository Contract。
"""

from collections.abc import Callable

from app.application.errors import IdentityResolutionFailedError
from app.domain.identity_resolution import IdentityResolution
from app.repositories.unit_of_work import UnitOfWork


class IdentityResolutionExecutor:
    """Identity Resolution 执行组件：按 (channel_id, external_id) 反查 Identity（+ 可选 User）。

    只读组合已冻结的领域对象（Identity + User），不创建 / 不猜测 / 不跨渠道合并。
    """

    def __init__(self, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    def execute(self, channel_id: int, external_identity_id: str) -> IdentityResolution:
        """解析一次外部身份 → Identity（+ 可选的管理关联 User）。

        未找到 Identity → Fail Closed（`IdentityResolutionFailedError`）；
        **Identity 命中即成功**——User 是否绑定不影响成败（Gate 2.6 D3）。
        """
        with self._uow_factory() as uow:
            identity = uow.identities.find_by_channel_and_external_id(
                channel_id, external_identity_id
            )
            if identity is None:
                raise IdentityResolutionFailedError(channel_id, external_identity_id)
            # 2.6 D1/D2/D3：ACP User 关联是**可选**的——未绑定（user_id is None）
            # 时 user = None 是正常结果，不是失败；也绝不创建 User / Identity（ADR-5）。
            user = None
            if identity.user_id is not None:
                user = uow.users.get_by_id(identity.user_id)
        return IdentityResolution(identity=identity, user=user)
