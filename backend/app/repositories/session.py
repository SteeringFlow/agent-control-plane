"""SessionRepository 端口（Port，Step 9.3 Session Resolution Execution）。

读路径（9.3）：`find_active`——按会话隔离键查 active session。
写路径（9.7）：`create`——把 9.3 的 `CREATE_NEW` **决策**物化为真实 CP Session。

刻意不做 update / delete / get_by_id：

- 会话生命周期管理（update 状态）属后续 Slice；
- 不为「未来可能需要」扩张方法面（与 IdentityRepository 不做 find_by_name 同纪律）。

**10.8.4 例外（唯一一个写入方法）**：`bind_external_session`——把
Agent 侧会话引用（`AgentResponse.external_session_id`）绑定到已存在的 CP
Session。它不是通用 update（不改状态、不改绑定关系、不清空既有映射），
而是「Result 接线」缺口的唯一落点（10.3.1 §4 / 10.7.2 B4 遗留至今）。

`find_active` 的语义 = 8.3 冻结的会话隔离键，**三个输入缺一不可**：

    (identity_id, connection_id, external_conversation_id)

尤其 `external_conversation_id` 必须参与查找——它是防止群聊 / 会话上下文泄漏的
关键，不能被省略或弱化。

幂等性说明（M6.6-C4 / CP4-1 更新）：sessions 表的语义键
(identity_id, connection_id, external_conversation_id) **已由 0002（tenant 隔离演进）
提升为租户前导的 DB 唯一约束** `uq_sessions_tenant_identity_conn_conv`
——原「9.7 evidence-first：无唯一约束」的陈述已被该演进翻转（见
`tests/test_step9_7_session_creation_execution.py::test_f_sessions_unique_constraint_materialized`）。

并发创建由 `find_or_create` 收敛：`find_active → create` 的唯一违例由 SAVEPOINT 消化后
**按精确四元组回读复用**；回读不到才 Fail Closed。约束本身**不改、不删、不放宽**。
"""

from typing import Protocol, runtime_checkable

from app.domain.session import Session


@runtime_checkable
class SessionRepository(Protocol):
    """会话仓储：按业务语义命名的方法，不是 Generic CRUD。"""

    def find_active(
        self,
        identity_id: int,
        connection_id: int,
        external_conversation_id: str,
    ) -> Session | None:
        """按会话隔离键查找 active session；不存在返回 None。

        只返回 status == "active" 的会话：REUSE_ACTIVE 复用它，否则 → CREATE_NEW。
        """
        ...

    def create(self, session: Session) -> Session:
        """写入一条 Control Plane Session 并返回带 id 的新实例（事务边界归 UoW）。

        9.7：把 9.3 的 CREATE_NEW 决策**物化**为真实会话；id 由 DB 生成。
        """
        ...

    def find_or_create(self, session: Session) -> Session:
        """**幂等**地确保该会话隔离键的 active Session 存在（命中复用 / 未命中创建）。

        M6.6-C4 / CP4-1（Session 并发闭环）：把 `find_active → create` 之间的良性并发
        竞态（唯一约束冲突）恢复为**复用**，而不是 `SESSION_RESOLUTION_FAILED`。

        语义纪律（与 `Identity.find_or_create` / Grant 同构）：

        - 命中 ⇒ **复用**（绝不产生第二行）；
        - 未命中 ⇒ 创建；
        - 写冲突（`IntegrityError`；SQLite 写锁下还可能是 `OperationalError`）⇒
          回滚嵌套事务后**按精确四元组回读**——回读到才复用，回读不到原样抛出
          （由调用方 Fail Closed）。

        回读使用完整四元组（tenant 由 UoW 作用域承担 +
        identity_id / connection_id / external_conversation_id），**不做**
        identity-only / connection-only / 全局查找——杜绝跨租户、跨 conversation 误复用。
        """
        ...

    def bind_external_session(
        self,
        session_id: int,
        external_session_id: str,
    ) -> None:
        """把 Agent 侧会话引用绑定到 CP Session（10.8.4）。

        只写这一条映射：不改会话状态、不改 Identity/Connection 绑定；
        **调用方在 `external_session_id is None` 时不得调用**（避免用 None
        清空已建立的映射——映射一旦建立就是事实）。
        """
        ...

    def find_active_by_identity_and_conversation(
        self,
        identity_id: int,
        external_conversation_id: str,
    ) -> Session | None:
        """按 (identity_id, external_conversation_id) 查 active session（10.4.2）。

        Interaction 持久化 Wrapper 的最小读路径：执行边界上确定本次交互
        所属 Session（成功回填 FK / Dispatch·Delivery 失败时同样回填），
        以及 CREATE_NEW Evidence 推导（执行前无 → 执行后有）。

        **有记录的近似**：隔离键弱化（省略 connection_id）。MVP 语义下
        一个 Channel 只有一个 ACTIVE Connection（8.4 冻结，10.3.1 Capability
        Gap 记录），(identity, conversation) 唯一确定 active session；
        未来单 Channel 多 Connection 落地时，本查询需随真实语义收紧。
        """
        ...

    def list_known_conversation_ids(self, channel_id: int) -> list[str]:
        """列出该 Channel 上**出现过**的会话键（Package 2 / Step 3，B4.5 投影）。

        `DISTINCT external_conversation_id FROM sessions WHERE channel_id = …`
        ——Discovery 的唯一读路径：

        - **projection only**：只回答「过去出现过哪些会话」，**绝不产生**
          授权状态（ADR-6）——known conversation ≠ authorized conversation；
        - 排除 NULL / 空白键（与 0007 回填同一过滤条件）；
        - 排序稳定（陈列面）。
        """
        ...
