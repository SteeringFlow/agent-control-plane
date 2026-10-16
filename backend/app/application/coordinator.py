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
                                    OutboundResponse → DeliveryExecutor
                                            ▼
                                    RuntimeExecutionResult（10.8.2 薄契约）

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

        # 7) Delivery —— 形成 OutboundResponse 并交还 Channel
        #
        # 12.8.2 metadata 传播（**channel-originated 优先**）：
        #   Agent metadata 先展开、inbound（Channel）metadata 后展开 —— 后者自然
        #   覆盖同名 key，因此 Channel 侧交付上下文（如智能机器人 response_url）
        #   **绝不会被 Agent 输出覆盖**；Agent 侧只能补充新 key。
        #   两侧皆空 → `{} or None` → None（保持既有「无 metadata」语义）。
        # 写法约束：Coordinator 纯度守卫禁止任何额外调用（无 helper / 无
        #   isinstance / 无 .items），故用字典解包这一零调用形式表达同一语义。
        metadata = {**(response.metadata or {}), **(inbound.metadata or {})}
        outbound = OutboundResponse(
            channel_id=inbound.channel_id,
            external_conversation_id=inbound.external_conversation_id,
            content=response.content,
            metadata=metadata or None,
        )
        self._delivery.execute(outbound)

        # 8) Result —— 10.8.2 薄结果契约（10.8.1 A 裁决的三事实）：
        #    REUSE_ACTIVE → existing session.id / CREATE_NEW → newly created
        #    session.id（二者已统一为 control_plane_session_id，绝不 None）；
        #    AgentResponse 直接进入 Result（响应级事实的唯一数据源，10.7.2 B）。
        return RuntimeExecutionResult(
            session_mode=decision.mode,
            session_id=control_plane_session_id,
            agent_response=response,
        )
