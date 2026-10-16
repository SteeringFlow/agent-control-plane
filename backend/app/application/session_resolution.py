"""Session Resolution Execution（Step 9.3）—— 8.3 契约的第一次真实执行。

9.3 是第一个真正依赖 Connection 的 Runtime stage，但它**不选择 Connection**：

    8.3 冻结：Session Resolution ≠ Connection Selection
    Connection 是 8.4 Routing 的产物 → 9.3 只把 connection_id 当**输入消费**

执行链（只读；不创建 Session）：

    (identity_id, connection_id, external_conversation_id)
        ↓ SessionRepository.find_active（9.3 新增端口，唯一键 = 8.3 隔离键）
        ↓ hit  → REUSE_ACTIVE（existing_session_id = 该 active session 的 id）
        ↓ miss → CREATE_NEW（existing_session_id = None）

**CREATE_NEW 只是决策结果，不是立即 create**：9.3 刻意不做 Session Persistence——
会话创建 / 生命周期属后续 Slice。否则本 Executor 会同时拥有 Resolution /
Persistence mutation / Session lifecycle 三个职责，后面越来越难拆。

边界（机器化校验见 tests/test_step9_3_session_execution.py）：

- 三维隔离键缺一不可：`(identity_id, connection_id, external_conversation_id)`；
  尤其 `external_conversation_id` 必须参与查找（防群聊 / 会话上下文泄漏）；
- 使用 `Identity.id`（Channel 边界），**不是 `User.id`**；
- 不跨 conversation 复用会话：A+conversation-X 与 A+conversation-Y 是两条空间；
- Fail Closed：输入不合法 → `SessionResolutionFailedError`（STOP）；
  绝不创建 Session / Identity / Connection，绝不猜 conversation；
- 只消费 Connection：不 import ConnectionRepository、不查 Connection；
- 依赖边界：只依赖 Repository / UoW Port——不 import SQLAlchemy / ORM /
  persistence / adapters；
- 是 **Execution** 组件，不是 Resolver：本模块不创建 SessionResolver /
  SessionService / SessionManager / SessionPolicy / SessionLifecycleManager。
"""

from collections.abc import Callable

from app.application.errors import SessionResolutionFailedError
from app.domain.session_resolution import CREATE_NEW, REUSE_ACTIVE, SessionResolution
from app.repositories.unit_of_work import UnitOfWork


class SessionResolutionExecutor:
    """Session Resolution 执行组件：按三维隔离键决定 REUSE_ACTIVE / CREATE_NEW。

    只读查找 + 决策，不创建任何东西。
    """

    def __init__(self, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    def execute(
        self,
        identity_id: int,
        connection_id: int,
        external_conversation_id: str,
    ) -> SessionResolution:
        """解析一次会话归属；输入不合法 → Fail Closed 抛错（STOP）。"""
        # Fail Closed：三个输入缺一不可（identity 存在性由 9.2 Identity
        # Resolution 保证，本组件只校验形状，不重复解析身份）
        for name, value in (("identity_id", identity_id),
                            ("connection_id", connection_id)):
            if not isinstance(value, int) or value <= 0:
                raise SessionResolutionFailedError(
                    identity_id, connection_id, external_conversation_id)
        if not isinstance(external_conversation_id, str) \
                or not external_conversation_id.strip():
            raise SessionResolutionFailedError(
                identity_id, connection_id, external_conversation_id)

        with self._uow_factory() as uow:
            session = uow.sessions.find_active(
                identity_id, connection_id, external_conversation_id
            )
        if session is not None:
            # 15.5.2：REUSE 分支把已复用 Session 的 Agent 侧引用只读带出
            #（可能为 None——首轮 Agent 尚未回流）；不解释、不创建、不管理。
            return SessionResolution(
                mode=REUSE_ACTIVE,
                identity_id=identity_id,
                connection_id=connection_id,
                external_conversation_id=external_conversation_id,
                existing_session_id=session.id,
                external_session_id=session.external_session_id,
            )
        return SessionResolution(
            mode=CREATE_NEW,
            identity_id=identity_id,
            connection_id=connection_id,
            external_conversation_id=external_conversation_id,
            existing_session_id=None,
        )
