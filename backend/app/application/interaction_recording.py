"""Interaction Persistence Wrapper（Step 10.4.2）—— 10.4.1 裁决的挂点实现。

形状：**RuntimeExecutionCoordinator Protocol 装饰器**——挂在 Runtime 调用边界
（ChannelInboundService ↔ RuntimeCoordinator 之间，10.4.1 §问题 C 裁决）：

    ChannelInboundService
        ↓ PersistingCoordinator（本模块，同一 Protocol 形状）
        ↓ inner RuntimeCoordinator（零改动、零感知）

设计裁决（10.4.2 冻结）：

1. **Executor 不写 Activity**：六段 Executor 与 Coordinator 不增加任何
   persistence 依赖（9.A / 10.3.1 / 10.4.1 全部守卫继续成立）——Evidence
   全部由本 wrapper 在执行边界观察记录。
2. **Evidence = 事实**：只记录真正观察到的东西——
   - 生命周期：interaction_received → PROCESSING → 终态 outcome；
   - 成功：interaction_completed（stage=delivery, SUCCEEDED）；
   - CREATE_NEW 的 session_creation Evidence：10.8.4 起直接取自
     `RuntimeExecutionResult.session_mode == CREATE_NEW`（Runtime 的观察
     事实）；此前由「执行前后 active session 由无到有」的**推导**替代，
     现已升级为直接观察（10.8.1 裁决 ③）；
   - REUSE_ACTIVE **不产生** session_creation Evidence（凭空制造的
     session_creation=SUCCESS 不是事实）；
   - 失败：typed error → (failure_stage, failure_code, retryable) 显式映射
     （STAGE_FAILURE_MAP），记 interaction 终态 + stage_failure Evidence。
     **Delivery 失败 ≠ Processing 失败**：CHANNEL_DELIVERY_FAILED 落
     processing=SUCCEEDED + delivery=FAILED（4.2 冻结的合法组合）。
3. 已知边界（诚实记录）：成功路径的中间段（identity_resolution / routing /
   session_resolution / agent_dispatch）逐段成功 Evidence 需要 stage 级回调
   或结构化回流——Coordinator 只有 execute() -> None（9.1 冻结，不引入
   RuntimeResult），wrapper 观察不到段间边界。本步不为成功段制造未经观察
   的 Evidence；真实需要时走专门裁决。
4. 错误语义保持：wrapper 记录后**原样 re-raise**——typed error 的向上传播
   （webhook 5xx 映射等）行为与 9.8.2 冻结完全一致。

**10.8.4 Result 接线**（承接 10.8.1~10.8.3）：wrapper 接住
`RuntimeExecutionResult`，并把它变成三处持久化事实：

    RuntimeExecutionResult
      ├── agent_response → interactions.response_payload（原样快照）
      │                  → sessions.external_session_id（会话映射绑定）
      ├── session_id     → interactions.session_id（不再自己查询推导）
      └── session_mode   → session_created Evidence 的判定（直接观察）

边界（本步刻意不做）：

- **不做** Idempotency / Retry / Replay / Outbox / Delivery queue / lease；
  response_payload 的存在只是「具备未来 Delivery-only retry 所需数据」，
  不等于现在实现 retry（10.5.3 未决问题保持不变）；
- 失败路径没有 Result（异常传播）→ response_payload 保持 NULL、
  session 归属仍走弱键读路径（开放点，见 `record_failure` docstring）；
- 因此 **Case B（processing=SUCCEEDED × delivery=FAILED）的
  response_payload 仍为 NULL**——这是对 10.7.2 B2 状态矩阵的诚实修订
  （完整成功才落响应快照；Case B 的承载物需未来接线裁决）。
"""

from collections.abc import Callable
from datetime import datetime

from app.application.errors import (
    AccessAuthorizationDeniedError,
    AgentDispatchFailedError,
    AgentResponseInvalidError,
    ApplicationError,
    ChannelDeliveryFailedError,
    IdentityResolutionFailedError,
    RoutingFailedError,
    SessionResolutionFailedError,
)
from app.domain.activity_record import (
    ACTIVITY_COMPLETED,
    ACTIVITY_RECEIVED,
    ACTIVITY_SESSION_CREATED,
    ACTIVITY_STAGE_FAILURE,
    STAGE_AGENT_DISPATCH,
    STAGE_AUTHORIZATION,
    STAGE_DELIVERY,
    STAGE_IDENTITY_RESOLUTION,
    STAGE_ROUTING,
    STAGE_SESSION_CREATION,
    STAGE_SESSION_RESOLUTION,
    ActivityRecord,
)
from app.domain.artifact import Artifact
from app.domain.inbound_interaction import InboundInteraction
from app.domain.interaction_snapshot import inbound_to_snapshot
from app.domain.interaction_record import (
    DELIVERY_DELIVERED,
    DELIVERY_FAILED,
    DELIVERY_PENDING,
    InteractionRecord,
    OutcomeCommit,
    PROCESSING_FAILED,
    PROCESSING_SUCCEEDED,
)
from app.domain.runtime_execution_result import RuntimeExecutionResult
from app.domain.session_resolution import CREATE_NEW
from app.repositories.unit_of_work import UnitOfWork

# typed error → (failure_stage, failure_code, retryable) —— 显式映射，不猜。
# retryable 语义：transport/timeout 类（可安全重试）= True；契约/配置/身份类 = False。
# SessionCreationExecutor 失败复用 SessionResolutionFailedError（9.7），
# 因此其失败 Evidence 归 stage=session_resolution（错误码不可区分，诚实映射）。
STAGE_FAILURE_MAP: dict[type[Exception], tuple[str, str, bool]] = {
    IdentityResolutionFailedError: (
        STAGE_IDENTITY_RESOLUTION, "IDENTITY_RESOLUTION_FAILED", False),
    # Implementation Package 2 / Step 2：授权拒绝是**许可类**失败——不可重试。
    # 未注册的后果是退化成 (None, UNEXPECTED_FAILURE)，即**丢掉「这是授权拒绝」
    # 这一事实**（Package 2-C §7.6 的诚实性要求）。
    AccessAuthorizationDeniedError: (STAGE_AUTHORIZATION, "UNAUTHORIZED", False),
    RoutingFailedError: (STAGE_ROUTING, "ROUTING_FAILED", False),
    SessionResolutionFailedError: (
        STAGE_SESSION_RESOLUTION, "SESSION_RESOLUTION_FAILED", False),
    AgentDispatchFailedError: (STAGE_AGENT_DISPATCH, "AGENT_DISPATCH_FAILED", True),
    AgentResponseInvalidError: (
        STAGE_AGENT_DISPATCH, "AGENT_RESPONSE_INVALID", False),
    ChannelDeliveryFailedError: (STAGE_DELIVERY, "CHANNEL_DELIVERY_FAILED", True),
}

# 执行已到达「Session 已确定」之后的阶段：失败时 interaction 仍可关联 Session。
SESSION_LINKED_STAGES = (STAGE_AGENT_DISPATCH, STAGE_DELIVERY)

UNEXPECTED_FAILURE_CODE = "UNEXPECTED_FAILURE"


def _snapshot_artifacts(artifacts: list[Artifact] | None) -> list[dict] | None:
    """Artifact 列表 → response_payload 快照（显式字段映射，**不用 asdict 隐式展开**）。

    形状纪律（对齐 interaction_snapshot 裁决 1）：显式列出全部字段——若 Artifact
    契约未来扩字段，已落库快照不会静默改形状。`None → None`（faithful「原样」快照：
    `AgentResponse.artifacts is None` ⇒ 快照亦为 None，与 metadata 同纪律）。

    ACP V0.2 边界：artifact 只进 response_payload（Observation），
    **不进 Delivery / Tencent SSE / DB schema / RuntimeExecutionResult**。
    """
    if artifacts is None:
        return None
    return [
        {
            "artifact_id": a.artifact_id,
            "kind": a.kind,
            "name": a.name,
            "uri": a.uri,
            "mime_type": a.mime_type,
            "size_bytes": a.size_bytes,
            "metadata": a.metadata,
        }
        for a in artifacts
    ]


class PersistingCoordinator:
    """RuntimeCoordinator 的持久化装饰器（同一 Protocol 形状，execute() -> None）。

    命名裁决：不称 RuntimeCoordinator（6.5 Scope 泄漏扫描对类名含 "runtime"
    有精确白名单），本类是持久化装饰器而非 Runtime 编排器——编排完全委托 inner。

    **12.9.4.2 Phase API**：原 `execute()` 内部混合的落库动作拆成可独立调用的
    phase operations（未来 Worker 直接复用，同步路径由 `execute()` 组合）：

        create_pending(inbound)            → PENDING 落库 + received（COMMIT）
        claim_processing(id)               → PENDING → PROCESSING（CAS，COMMIT）
        record_success(id, result)         → 成功终态（COMMIT）
        record_failure(id, inbound, exc)   → 失败终态（COMMIT）
        reclaim_stale_processing(thr)      → PROCESSING 孤儿回收 → PENDING
                                             （12.9.4.12，独立短事务）

    每个 phase 自带独立短事务（一个 `with uow` = 一次 COMMIT），彼此不共享 Session；
    因此执行（~16s）天然落在事务之外，不存在「长事务裹住执行」。

    **12.9.4.12 Attempt Fence**：claim 成功后经 `get_by_id` **读回** DB 写入的
    `processing_started_at` 并持有，透传给 record_*（`expected_started_at`）——
    Worker / 同步路径都绝不自己生成时间戳（12.9.4.11 §1.2）。record_* 返回
    `OutcomeCommit`：REJECTED = ownership lost（正常竞争结果），调用方静默忽略。
    """

    def __init__(
        self,
        inner,
        uow_factory: Callable[[], UnitOfWork],
        *,
        direction: str = "inbound",
    ) -> None:
        self._inner = inner
        self._uow_factory = uow_factory
        self._direction = direction

    def execute(self, inbound: InboundInteraction) -> RuntimeExecutionResult:
        """记录一次完整 Runtime 执行（成功 / typed 失败 / 契约外异常）。

        10.8.4 Result 接线：成功时把 inner 的 RuntimeExecutionResult **原样返回**
        （观察层不改执行，只顺带返回结果）；失败路径仍原样 re-raise（无结果
        对象——异常通道本身就是执行事实通道）。

        12.9.4.2：本方法退化为「四个 phase 的同步组合」——与未来 Worker 的调用序列
        完全一致（create → claim → execute → outcome），差别只在于是否跨进程。
        """
        interaction_id = self.create_pending(inbound)
        # 同步路径下本方法刚 create 的行必然是 PENDING → claim 必然成功。
        # 若 claim 失败（该行已不是 PENDING，即已被他人接管），**绝不执行**——
        # 无条件执行就是双执行（12.9.3.2 §3）。同步路径不可达，故以显式错误暴露，
        # 而不是伪造一个 RuntimeExecutionResult（Evidence = 事实）。
        if not self.claim_processing(interaction_id):
            raise RuntimeError(
                f"interaction_id={interaction_id} 未被 claim（该行已不是 PENDING），"
                "拒绝执行")
        started_at = self._read_started_at(interaction_id)
        try:
            result = self._inner.execute(inbound)
        except Exception as exc:  # noqa: BLE001  typed 映射 + 兜底，记录后 re-raise
            self.record_failure(
                interaction_id, inbound, exc,
                expected_started_at=started_at)
            raise
        self.record_success(interaction_id, result,
                            expected_started_at=started_at)
        return result

    # ------------------------------------------------------------------ phase 0

    def find_pending(self, limit: int = 10) -> list[InteractionRecord]:
        """phase 0（DISCOVER，12.9.4.16）：当前待执行的 Interaction（**纯读**）。

        原样透传 Port 的 `find_pending`：不 claim、不改任何行、不加锁、不产生
        执行权——多个 Driver / Worker 读到同一批候选是设计允许的，唯一性由随后
        `claim_processing` 的 CAS 保证。独立短事务（与其余 phase 一致）。

        之所以经 phase API 暴露、而不是让 Driver 自己开 UoW 去读：Driver 的读
        能力必须和它的写能力一样，是被 Core **显式授予**的（12.9.4.16.7）——
        否则「禁止 Driver 直接碰 persistence」就只是一条靠自觉的约定。
        """
        with self._uow_factory() as uow:
            return uow.interactions.find_pending(limit)

    # ------------------------------------------------------------------ phase 1

    def create_pending(
        self,
        inbound: InboundInteraction,
        *,
        uow_factory: Callable[[], UnitOfWork] | None = None,
    ) -> int:
        """phase 1：PENDING 落库 + received Evidence（独立短事务，提交即可 ACK）。

        10.8.4：不再快照「执行前是否已有 active session」——CREATE_NEW / REUSE 是
        Runtime 的结果事实（`RuntimeExecutionResult.session_mode`），观察层不再用
        自己的查询去推导它。

        未来 Worker 模型中 intake 边界只调用本方法（之后即可返回 200），执行权交给
        Worker 的 claim——PENDING 因此是真实持久化过的、可观察的状态。

        Cloud-0.1.3.1：可选 `uow_factory` 覆盖——入站边界在**验签之后**用它把写入
        落到「已认证 Channel 解析出的 tenant」作用域（缺省 None = 沿用本实例工厂，
        行为不变）。这使 PENDING 行带上正确的租户，而不是入口的默认租户。
        """
        factory = uow_factory if uow_factory is not None else self._uow_factory
        with factory() as uow:
            record = uow.interactions.create(InteractionRecord(
                direction=self._direction,
                channel_id=inbound.channel_id,  # 10.5.2：身份物化（scope = Channel）
                external_interaction_id=inbound.external_interaction_id,
                # 12.9.4.3：完整入站快照（含 metadata）——形状由
                # app.domain.interaction_snapshot 单一事实源定义，Worker 未来只能凭它
                # 重建入站并执行；漏掉 metadata 会让机器人回复静默错投（12.9.3.5）。
                request_payload=inbound_to_snapshot(inbound),
            ))
            uow.activities.append(ActivityRecord(
                type=ACTIVITY_RECEIVED, status="recorded",
                interaction_id=record.id))
            uow.commit()
        return record.id

    # ------------------------------------------------------------------ phase 2

    def claim_processing(self, interaction_id: int) -> bool:
        """phase 2：PENDING → PROCESSING（CAS 原子 claim，独立短事务）。

        True = 本调用方取得执行权；False = 已被他人 claim / 已是终态 → 调用方停止。
        提交即释放锁：phase 之间的执行（~16s）不在任何事务内（12.9.3.4 §D）。
        """
        with self._uow_factory() as uow:
            claimed = uow.interactions.claim_processing(interaction_id)
            uow.commit()
        return claimed

    # ------------------------------------------------------------------ phase 3

    def record_success(
        self,
        interaction_id: int,
        result: RuntimeExecutionResult,
        *,
        expected_started_at: datetime | None = None,
    ) -> OutcomeCommit:
        """phase 3（成功）：成功终态——一切执行事实来自 RuntimeExecutionResult。

        - `session_id` 取 Result（**不再由 wrapper 查询推导**）；
        - `response_payload` = AgentResponse 原样快照（10.7.2 B 裁决）；
        - `sessions.external_session_id` 由 `agent_response.external_session_id`
          绑定（Agent 侧会话映射的唯一写入点；None 不写入、不清空）；
        - session_created Evidence 由 `result.session_mode` 直接判定
          （Evidence = 事实，不再靠「执行前后快照」推导）；
        - `expected_started_at`（12.9.4.12）：attempt fence 等式值，原样透传。
        """
        with self._uow_factory() as uow:
            commit = uow.interactions.record_outcome(
                interaction_id,
                processing_status=PROCESSING_SUCCEEDED,
                delivery_status=DELIVERY_DELIVERED,
                session_id=result.session_id,
                response_payload={
                    "content": result.agent_response.content,
                    "external_session_id": result.agent_response.external_session_id,
                    "metadata": result.agent_response.metadata,
                    "artifacts": _snapshot_artifacts(result.agent_response.artifacts),
                },
                expected_started_at=expected_started_at,
            )
            if commit is OutcomeCommit.REJECTED:
                # ownership lost：本 attempt 已被 reclaim/取代——它的结果不是
                # 该 interaction 的事实，不绑 session、不写 Evidence、不落任何
                # 东西（Evidence = 事实；REJECTED 是正常竞争结果，不抛，
                # 12.9.4.11 §3）。
                uow.commit()
                return commit
            if result.agent_response.external_session_id is not None:
                # 映射一旦建立就是事实：None 不写入，绝不覆盖既有映射
                uow.sessions.bind_external_session(
                    result.session_id, result.agent_response.external_session_id)
            if result.session_mode == CREATE_NEW:
                uow.activities.append(ActivityRecord(
                    type=ACTIVITY_SESSION_CREATED,
                    stage=STAGE_SESSION_CREATION,
                    status="succeeded",
                    interaction_id=interaction_id,
                    session_id=result.session_id,
                    metadata={"session_id": result.session_id},
                ))
            uow.activities.append(ActivityRecord(
                type=ACTIVITY_COMPLETED, stage=STAGE_DELIVERY,
                status="succeeded", interaction_id=interaction_id,
                session_id=result.session_id))
            uow.commit()
        return commit

    def record_failure(
        self,
        interaction_id: int,
        inbound: InboundInteraction,
        exc: Exception,
        *,
        expected_started_at: datetime | None = None,
    ) -> OutcomeCommit:
        """phase 3（失败）：失败终态。

        失败路径**没有** RuntimeExecutionResult（异常传播），因此：
        - response_payload 保持 NULL（响应事实不存在或未被返回，不可伪造）；
        - session 归属仍走既有弱键读路径（10.8.1 §4 记录的开放点：彻底消除
          需要 typed error 携带上下文，等真实证据触发）；
        - `expected_started_at`（12.9.4.12）：attempt fence 等式值，原样透传。
        """
        stage, code, retryable = STAGE_FAILURE_MAP.get(
            type(exc), (None, UNEXPECTED_FAILURE_CODE, False))
        # 12.8.2：实例级 retryable 覆盖——response_url（一次性）路径的 Delivery
        # 失败不可重试；未携带该属性的错误沿用映射表默认值（不新增错误码）。
        retryable = bool(getattr(exc, "retryable", retryable))
        processing = (PROCESSING_SUCCEEDED if stage == STAGE_DELIVERY
                      else PROCESSING_FAILED)
        delivery = (DELIVERY_FAILED if stage == STAGE_DELIVERY
                    else DELIVERY_PENDING)
        with self._uow_factory() as uow:
            session_id = None
            if stage in SESSION_LINKED_STAGES:
                session = self._find_session(uow, inbound)
                session_id = session.id if session is not None else None
            commit = uow.interactions.record_outcome(
                interaction_id,
                processing_status=processing,
                delivery_status=delivery,
                failure_stage=stage,
                failure_code=code,
                retryable=retryable,
                session_id=session_id,
                expected_started_at=expected_started_at,
            )
            if commit is OutcomeCommit.REJECTED:
                # ownership lost：同 record_success——失败 Evidence 也不写
                # （该失败属于已失去 commit authority 的旧 attempt）。
                uow.commit()
                return commit
            uow.activities.append(ActivityRecord(
                type=ACTIVITY_STAGE_FAILURE, stage=stage, status="failed",
                message=str(exc),
                metadata={"failure_code": code, "retryable": retryable},
                interaction_id=interaction_id, session_id=session_id))
            uow.commit()
        return commit

    # ------------------------------------------------------------------ phase 4

    def reclaim_stale_processing(self, threshold_seconds: int) -> int:
        """phase 4（12.9.4.12）：PROCESSING 孤儿回收——超时未落终态 → PENDING。

        独立短事务，原样透传 Port 的 `reclaim_stale_processing`。只回收、
        不 claim、不执行：回收后的行重新进入 PENDING pickup 池，由既有
        claim（CAS）+ Worker 流程接管。未来的 daemon loop 是它的触发者
        （本步不引入触发器）。
        """
        with self._uow_factory() as uow:
            reclaimed = uow.interactions.reclaim_stale_processing(
                threshold_seconds)
            uow.commit()
        return reclaimed

    def _read_started_at(self, interaction_id: int) -> datetime | None:
        """读回 claim 时刻 DB 写入的 `processing_started_at`（fence 持有值）。

        等式两侧必须同为 DB 值（列 vs 读回值）——app 绝不自己生成时间戳
        （12.9.4.11 §1.2：时钟偏移/时区/精度会让合法 attempt 被误判）。
        """
        with self._uow_factory() as uow:
            record = uow.interactions.get_by_id(interaction_id)
        return record.processing_started_at if record is not None else None

    def _find_session(self, uow: UnitOfWork, inbound: InboundInteraction):
        """(channel, external_identity) → identity → active session；任一不存在 → None。"""
        identity = uow.identities.find_by_channel_and_external_id(
            inbound.channel_id, inbound.external_identity_id)
        if identity is None or identity.id is None:
            return None
        return uow.sessions.find_active_by_identity_and_conversation(
            identity.id, inbound.external_conversation_id)
