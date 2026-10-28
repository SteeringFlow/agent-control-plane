"""Runtime Coordinator Implementation（Step 9.8.2 / 10.8.3）—— 让
`RuntimeExecutionCoordinator` 边界获得真正实现：把已独立验收的执行组件按
`RUNTIME_EXECUTION_ORDER` 串起来。

                        Coordinator
    InboundInteraction → IdentityResolutionExecutor → IdentityResolution
                      → AccessAuthorizationExecutor → AccessAuthorization
                      → RoutingExecutor            → RoutingResult
                      → SessionResolutionExecutor  → REUSE_ACTIVE / CREATE_NEW
                            ├─ REUSE_ACTIVE ─────────────┐
                            └─ CREATE_NEW                │
                                 ↓                       │
                          SessionCreationExecutor        │
                                 ↓                       │
                                 └───────────┬───────────┘
                                            ▼
                                    control_plane_session_id
                                            ▼
                                    AgentDispatchExecutor → AgentResponse
                                            ▼
                                    RuntimeExecutionResult（10.8.2 薄契约）
                                            │
        ┌───────────────────────────────────┘
        │  **P1 / C1**：Delivery **不再**在 execute() 内部同步发生。
        │  应用层（AgentDeliveryLifecycle）在**结果落库之后**显式发起：
        │      build_outbound_response(inbound, agent_response)
        │          → OutboundResponse → DeliveryExecutor
        ▼
                                    （投递结果单独记录：processing ≠ delivery）

**Coordinator 是 Orchestrator，不是 Domain Service**：它只知道「先调用谁 / 拿到什么 /
下一步调用谁 / 失败在哪里停止」——不知道如何找 Identity / 判定许可 / 选 Connection /
找 Session / 连接 Agent / 发送 Channel。本模块**不 import 任何 Repository / Adapter /
Persistence / SQLAlchemy**，七段能力全部由构造注入。

**错误传播（保留 stage semantics）**：本模块不吞异常、不把一切归一成
RuntimeFailed——各 Executor 抛出的 typed ApplicationError 原样向上传播：

    Identity / Authorization / Routing / Session / Session Creation 失败
        → STOP（不进入后续）
    Agent Dispatch 失败 → STOP（Delivery 不被调用）
    Delivery 失败 → 在 Dispatch 成功之后抛 ChannelDeliveryFailedError
    （processing 仍 SUCCESS、delivery = FAILED 的语义归持久化层，见 4.2/5.3）

**P1 / C1：Delivery 与 Runtime 的解耦（本类的唯一行为变化）**

以前：execute() 内部同步完成「Dispatch → 组装 Outbound → Delivery」
      ⇒ 投递失败会**连带丢掉** Agent 已生成的结果（生产实证：
        该场景的响应快照恒为 NULL）。

现在：execute() 只到 **Dispatch**（六段）即返回 RuntimeExecutionResult；
      出站组装与外部投递由**应用层**在「Agent 成功结果已落库之后」显式发起：

          result  = runtime.execute(...)               # 六段，无外部 I/O
          persist_agent_success(result)                 # 响应快照落库
          outbound = build_outbound_response(inbound, result.agent_response)
          runtime.deliver(outbound)                     # 外部 I/O（事务之外）
          persist_outcome(...)                         # 投递终态单独记录

段序（identity → … → agent_dispatch → delivery）**未变**；改变的只是
「投递由谁、在什么时点发起」。

**None 不是控制流协议**：各 Executor 成功都返回 typed domain 结果，失败抛 typed
error——本模块不出现 `if result is None: ...` 的模糊分支；Session 分支只按
`REUSE_ACTIVE` / `CREATE_NEW` 常量走。Coordinator 自身成功则返回
RuntimeExecutionResult（10.8.2 薄结果契约：session_mode / session_id /
agent_response，10.8.1 A 裁决三事实；REUSE → existing session.id /
CREATE → newly created session.id，AgentResponse 直接进入 Result），失败原
typed error 传播（10.8.3 接线）。

**不变量**：进入 Dispatch 的 `control_plane_session_id` 要么等于
`decision.existing_session_id`（REUSE），要么等于 `SessionCreationExecutor` 返回的
`created.id`（CREATE_NEW），**绝不可能是 None**（否则 Fail Closed）。
"""

from app.application.errors import (
    IdentityResolutionFailedError,
    SessionResolutionFailedError,
)
from app.application.runtime import RuntimeExecutionCoordinator
from app.domain.inbound_interaction import InboundInteraction
from app.domain.outbound_response import OutboundResponse
from app.domain.runtime_execution_result import RuntimeExecutionResult
from app.domain.session_resolution import CREATE_NEW, REUSE_ACTIVE


class RuntimeCoordinator(RuntimeExecutionCoordinator):
    """Runtime Coordinator：按固定执行顺序编排七段已冻结能力。

    七段依赖全部由构造注入（Executor 实例），本类不实例化任何 Executor /
    Repository / Adapter，也不触碰 ORM。

    **第 7 依赖（Implementation Package 2 / Step 2）**：Access Authorization 与其余
    六段同形注入——不得为了少一个参数而让本类自己拿 Repository / 建执行器
    （那会把依赖边界与持久化边界一起打穿）。
    """

    def __init__(
        self,
        *,
        identity_resolution,
        access_authorization,
        routing,
        session_resolution,
        session_creation,
        agent_dispatch,
        delivery,
    ) -> None:
        self._identity = identity_resolution
        self._authorization = access_authorization
        self._routing = routing
        self._session_resolution = session_resolution
        self._session_creation = session_creation
        self._dispatch = agent_dispatch
        self._delivery = delivery

    def execute(self, inbound: InboundInteraction) -> RuntimeExecutionResult:
        """按 RUNTIME_EXECUTION_ORDER 执行一次 Inbound → … → Delivery（Fail Closed）。

        成功 → RuntimeExecutionResult（session_mode / session_id /
        agent_response）；失败 → 原 typed exception 原样传播（无结果对象）。
        """
        # 1) Who —— Identity Resolution
        resolved = self._identity.execute(
            inbound.channel_id, inbound.external_identity_id)
        identity_id = resolved.identity.id
        if identity_id is None:  # 无 id 的 Identity 不可用 → Fail Closed
            raise IdentityResolutionFailedError(
                inbound.channel_id, inbound.external_identity_id)

        # 2) May it? —— Access Authorization（ADR-7：在 Session Resolution 之前；
        #    ADR-8：在最终 Routing 之前）。判定输入只有会话键——不消费 Identity /
        #    User / 内容；无许可 → AccessAuthorizationDeniedError（STOP：不 Routing、
        #    不建 Session、不 Dispatch、不 Delivery）。
        authorized = self._authorization.execute(
            inbound.channel_id, inbound.external_conversation_id)

        # 3) Where —— Routing 决定 Access Path（必须先于 Session）
        #    ADR-8：Router 只消费**已授权候选**——候选 = active(channel) ∩ granted(session)。
        #    授权只负责收窄候选，**不**接管存在性与唯一性判定（那是 Router 的职责）。
        routing_result = self._routing.execute(
            inbound.channel_id, authorized.granted_connection_ids)

        # 4) Which CP Session —— Session Resolution（消费 Routing 产出的 connection_id）
        decision = self._session_resolution.execute(
            identity_id, routing_result.connection_id, inbound.external_conversation_id)

        # 5) 分支：REUSE_ACTIVE 不创建；CREATE_NEW 才物化（9.7）
        if decision.mode == REUSE_ACTIVE:
            control_plane_session_id = decision.existing_session_id
        elif decision.mode == CREATE_NEW:
            created = self._session_creation.execute(
                identity_id=identity_id,
                channel_id=inbound.channel_id,
                connection_id=routing_result.connection_id,
                agent_id=routing_result.agent_id,
                external_conversation_id=inbound.external_conversation_id,
            )
            control_plane_session_id = created.id
        else:  # 未知 mode → Fail Closed（绝不猜）
            raise SessionResolutionFailedError(
                identity_id, routing_result.connection_id,
                inbound.external_conversation_id)

        # 不变量：control_plane_session_id 绝不可能是 None（REUSE 必带 id / CREATE 必带 id）
        if control_plane_session_id is None:
            raise SessionResolutionFailedError(
                identity_id, routing_result.connection_id,
                inbound.external_conversation_id)

        # 6) Execute —— Dispatch（execution, not selection）
        #    15.5.2：external_session_id 由 SessionResolution 只读带出
        #   （REUSE = 已绑定的 Agent 侧引用 / CREATE_NEW = None），此处仅透传
        #    给 Adapter；不读库、不写库——会话绑定仍由持久化 wrapper 唯一完成。
        response = self._dispatch.execute(
            routing_result, control_plane_session_id, inbound.content,
            external_session_id=decision.external_session_id)

        # 7) Result —— 10.8.2 薄结果契约（10.8.1 A 裁决的三事实）：
        #    REUSE_ACTIVE → existing session.id / CREATE_NEW → newly created
        #    session.id（二者已统一为 control_plane_session_id，绝不 None）；
        #    AgentResponse 直接进入 Result（响应级事实的唯一数据源，10.7.2 B）。
        #
        #    **P1 / C1：Delivery 不再是本方法的一部分**——外部投递由应用层在
        #    **持久化 Agent 成功结果之后**显式发起（见本类 `deliver()`）。
        #    段序本身未变（delivery 仍是最后一段），改变的只是**发起方**：
        #    以前「投递」在 Runtime 内部同步发生 → 投递失败会连带**丢掉**
        #    Agent 已生成的结果（生产实证：该场景的响应快照恒为 NULL）；
        #    现在结果先落库，投递结果**单独**记录（processing ≠ delivery）。
        return RuntimeExecutionResult(
            session_mode=decision.mode,
            session_id=control_plane_session_id,
            agent_response=response,
        )

    # ------------------------------------------------------------------ 显式投递

    def deliver(self, outbound: OutboundResponse) -> None:
        """执行一次外部投递（C1：第 7 段的**显式**入口）。

        由**应用层**在「Agent 成功结果已持久化」之后调用（`AgentDeliveryLifecycle`
        编排：persist → build_outbound_response → deliver → 记录投递结果）。

        本方法只做一件事：把出站消息交给既有的 `DeliveryExecutor`
        （Delivery is transport, not routing——不重新选路、不重新解析身份/会话）。
        投递失败照旧抛 `ChannelDeliveryFailedError`（由调用方**单独**记录为
        delivery=FAILED，**不回滚**已持久化的 Agent 结果、不改写 processing）。
        """
        self._delivery.execute(outbound)
