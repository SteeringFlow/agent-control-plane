"""Step 10.8.1 —— Runtime Result Boundary Audit（审计步，生产代码 0 行改动）。

只回答一个问题：如果 Runtime 不再只返回 None，它最小需要暴露哪些「执行事实」，
才能同时解决 10.4~10.7 已多次证明的观察缺口？

A. 四个事实逐个裁决（探针实证）：
   ① AgentResponse        —— 必须暴露（response_payload 唯一写入源 + dispatch 成功锚点）
   ② external_session_id  —— 不单独暴露（是 ① 的字段；单独暴露 = 重复表达）
   ③ Session created/reused —— 必须暴露（mode + session_id：消除弱键重推导，
                               CREATE_NEW Evidence 从推导变直接观察）
   ④ Execution/Delivery outcome —— 不暴露（异常通道已完整承载，返回值再带 = 重复）
B. 形状裁决：A 极薄 execution result（session_mode + session_id + agent_response），
   不是 B 全 trace（双份事实源 / 观察层全面耦合 / 诱导制造 Evidence /
   9.1 冻结明确反对「不引入 RuntimeResult 巨型 DTO」）。
C. 治理门槛：改 Protocol = Runtime Core 修改 → 10.1.4 §10 Freeze Rule 六问
   （本步只备答案素材，不接线）。

审计结论见 docs/step-10.8.1-runtime-result-boundary-audit.md。
"""

import ast
from dataclasses import fields, is_dataclass
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session as SASession, sessionmaker

from app.application import RuntimeCoordinator
from app.application.agent_dispatch import AgentDispatchExecutor
from app.application.delivery import DeliveryExecutor
from app.application.errors import (
    AgentDispatchFailedError,
    ChannelDeliveryFailedError,
)
from app.application.access_authorization import AccessAuthorizationExecutor
from app.application.identity_resolution import IdentityResolutionExecutor
from app.application.interaction_recording import PersistingCoordinator
from app.application.routing import RoutingExecutor
from app.application.session_creation import SessionCreationExecutor
from app.application.session_resolution import SessionResolutionExecutor
from app.domain.agent import Agent
from app.domain.agent_response import AgentResponse
from app.domain.channel import Channel
from app.domain.connection import Connection
from app.domain.connection_conversation import ConnectionConversation
from app.domain.identity import Identity
from app.domain.identity_resolution import IdentityResolution
from app.domain.inbound_interaction import InboundInteraction
from app.domain.outbound_response import OutboundResponse
from app.domain.routing_result import RoutingResult
from app.domain.session_resolution import SessionResolution
from app.domain.user import User
from app.persistence.models.interaction import InteractionModel
from app.persistence.models.session import SessionModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"
APPLICATION = APP / "application"
REPO_ROOT = BACKEND.parent
DOC = REPO_ROOT / "docs" / "step-10.8.1-runtime-result-boundary-audit.md"
FREEZE_DOC = REPO_ROOT / "docs" / "step-10.1.4-runtime-vertical-slice-closure.md"


# ================================================================== 测试底座


class _CountingAgentAdapter:
    def send(self, request, connection) -> AgentResponse:  # noqa: ARG002
        return AgentResponse(content="echo:hello",
                             external_session_id="agent-session-1")


class _RecordingChannelStub:
    def __init__(self) -> None:
        self.deliveries: list[OutboundResponse] = []

    def deliver(self, outbound: OutboundResponse) -> None:
        self.deliveries.append(outbound)


class _FailingChannelAdapter:
    def deliver(self, outbound: OutboundResponse) -> None:
        raise ChannelDeliveryFailedError(
            channel_id=outbound.channel_id,
            external_conversation_id=outbound.external_conversation_id)


class _FailingAgentAdapter:
    def send(self, request, connection) -> AgentResponse:  # noqa: ARG002
        raise AgentDispatchFailedError()


def _uow_factory(db_engine):
    return lambda: SqlAlchemyUnitOfWork(
        session_factory=sessionmaker(bind=db_engine, expire_on_commit=False))


def _seed(db_engine) -> dict[str, int]:
    with _uow_factory(db_engine)() as uow:
        alice = uow.users.create(User(name="alice"))
        channel = uow.channels.create(Channel(name="wecom-1", type="wecom"))
        agent = uow.agents.create(Agent(name="agent-a", type="acp"))
        ident = uow.identities.create(Identity(
            user_id=alice.id, channel_id=channel.id, external_id="wx_a"))
        path = uow.connections.create(Connection(
            channel_id=channel.id, agent_id=agent.id, name="path-a",
            status="active",
            config={"endpoint": "http://agent.internal", "timeout_s": 5.0},
            credential_ref="cred-a"))
        # Implementation Package 2 / Step 2：显式授权本文件用到的每个会话键
        for conversation in ("conv-1", "conv-df", "conv-dp"):
            uow.connection_conversations.create(ConnectionConversation(
                channel_id=channel.id, external_conversation_id=conversation,
                connection_id=path.id))
        uow.commit()
        return {"channel": channel.id, "agent": agent.id,
                "identity": ident.id}


def _wrapped_runtime(db_engine, agent_adapter=None, channel_adapter=None):
    uow = _uow_factory(db_engine)
    inner = RuntimeCoordinator(
        identity_resolution=IdentityResolutionExecutor(uow),
        access_authorization=AccessAuthorizationExecutor(uow),
        routing=RoutingExecutor(uow),
        session_resolution=SessionResolutionExecutor(uow),
        session_creation=SessionCreationExecutor(uow),
        agent_dispatch=AgentDispatchExecutor(
            agent_adapter=agent_adapter or _CountingAgentAdapter(),
            uow_factory=uow),
        delivery=DeliveryExecutor(
            channel_adapter=channel_adapter or _RecordingChannelStub()),
    )
    return PersistingCoordinator(inner=inner, uow_factory=_uow_factory(db_engine))


def _inbound(channel_id: int, interaction: str,
             conversation: str = "conv-1") -> InboundInteraction:
    return InboundInteraction(
        channel_id=channel_id, external_interaction_id=interaction,
        external_conversation_id=conversation,
        external_identity_id="wx_a", content="hello")


def _interaction_rows(db_engine) -> list[InteractionModel]:
    with SASession(db_engine) as s:
        return list(s.execute(select(InteractionModel)
                              .order_by(InteractionModel.id)).scalars().all())


# ================================================================== A. 四个事实逐个裁决


def test_a1_agent_response_never_reaches_the_observation_layer() -> None:
    """① 的现状证据：AgentResponse 领域对象从不到达 wrapper
    （execute() -> None 边界丢弃；AgentResponseInvalidError 只是错误类型键）。"""
    recording = (APPLICATION / "interaction_recording.py").read_text(
        encoding="utf-8")
    tree = ast.parse(recording, filename="interaction_recording.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "app.domain.agent_response", \
                "wrapper import 了 AgentResponse 领域对象"
    assert "AgentResponse(" not in recording
    # ① 的形状来源：AgentResponse 字段面三字段（8.5 冻结）+ V0.2 加性 artifacts
    assert {f.name for f in fields(AgentResponse)} == {
        "content", "external_session_id", "metadata", "artifacts"}


def test_a2_external_session_id_rides_on_agent_response(db_engine) -> None:
    """②：external_session_id 是 AgentResponse 的字段（不单独暴露）；
    （10.8.4 翻转）会话级绑定由 wrapper 从 agent_response 写入；
    （15.5.2 翻转）Coordinator 仅透传该引用给 dispatch（只读实参），
    仍不写库——职责分工保持。"""
    assert "external_session_id" in {f.name for f in fields(AgentResponse)}
    coordinator_src = (APPLICATION / "coordinator.py").read_text(encoding="utf-8")
    assert "bind_external_session" not in coordinator_src

    g = _seed(db_engine)
    _wrapped_runtime(db_engine).execute(_inbound(g["channel"], "msg-1"))
    with SASession(db_engine) as s:
        session = s.execute(select(SessionModel)).scalars().one()
    assert session.external_session_id == "agent-session-1"  # 10.8.4 已落地


def test_a3_session_fact_now_comes_from_the_result() -> None:
    """（10.8.4 翻转）③ 已落地：成功路径的 session_id / session_mode 直接取
    RuntimeExecutionResult——「执行前后快照推导」已删除；弱键只剩**失败路径**
    的 session 归属（无 Result，10.8.1 §4 开放点）。"""
    recording = (APPLICATION / "interaction_recording.py").read_text(
        encoding="utf-8")
    assert "session_existed_before" not in recording, "前后快照推导未移除"
    assert "result.session_id" in recording
    assert "result.session_mode" in recording
    # 失败路径仍用弱键（无结果对象）
    assert "find_active_by_identity_and_conversation" in recording


def test_a4_outcome_already_flows_through_the_exception_channel(db_engine) -> None:
    """④：异常通道已完整承载 outcome（无异常 → succeeded×delivered；
    ChannelDeliveryFailedError → succeeded×failed；AgentDispatchFailedError →
    failed×pending）——RuntimeResult 无需携带 outcome。"""
    g = _seed(db_engine)
    _wrapped_runtime(db_engine).execute(_inbound(g["channel"], "msg-ok"))
    with pytest.raises(ChannelDeliveryFailedError):
        _wrapped_runtime(db_engine,
                         channel_adapter=_FailingChannelAdapter()).execute(
            _inbound(g["channel"], "msg-df", conversation="conv-df"))
    with pytest.raises(AgentDispatchFailedError):
        _wrapped_runtime(db_engine,
                         agent_adapter=_FailingAgentAdapter()).execute(
            _inbound(g["channel"], "msg-dp", conversation="conv-dp"))

    rows = {r.external_interaction_id: r
            for r in _interaction_rows(db_engine)}
    assert (rows["msg-ok"].processing_status,
            rows["msg-ok"].delivery_status) == ("succeeded", "delivered")
    assert (rows["msg-df"].processing_status,
            rows["msg-df"].delivery_status) == ("succeeded", "failed")
    assert (rows["msg-dp"].processing_status,
            rows["msg-dp"].delivery_status) == ("failed", "pending")


def test_a5_facts_exist_inside_the_boundary_and_now_land(db_engine) -> None:
    """（10.8.4 翻转）事实丢失点已修复：AgentResponse.content 在边界内真实存在
    并流到 Delivery（Test Channel 收到 echo:hello），观察层两处均已落地——
    response_payload 有快照 + sessions.external_session_id 已绑定。"""
    g = _seed(db_engine)
    channel = _RecordingChannelStub()
    _wrapped_runtime(db_engine, channel_adapter=channel).execute(
        _inbound(g["channel"], "msg-1"))

    assert len(channel.deliveries) == 1
    assert channel.deliveries[0].content == "echo:hello"   # 事实存在且已外投
    assert channel.deliveries[0].external_conversation_id == "conv-1"

    row = _interaction_rows(db_engine)[0]
    assert row.response_payload["content"] == "echo:hello"  # ① 已落地
    with SASession(db_engine) as s:
        session = s.execute(select(SessionModel)).scalars().one()
    assert session.external_session_id == "agent-session-1"  # ② 已落地


# ================================================================== B. 形状裁决 A vs B


def test_b1_freeze_rule_governs_the_boundary_revision() -> None:
    """9.1 冻结拒绝的是巨型 DTO（原文在 runtime.py）；修改 Protocol =
    Runtime Core 修改 → 受 10.1.4 §10 Freeze Rule 约束（六问门槛真实存在）。

    （10.8.3 翻转：本审计预备的接线已兑现——runtime.py 的 execute 现返回
    10.8.2 薄结果契约 RuntimeExecutionResult；巨型 DTO 禁令原文保留。）"""
    runtime_src = (APPLICATION / "runtime.py").read_text(encoding="utf-8")
    assert "不引入 RuntimeResult 巨型 DTO" in runtime_src
    tree = ast.parse(runtime_src, filename="runtime.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "execute":
            assert node.returns is not None
            assert ast.unparse(node.returns) == "RuntimeExecutionResult"
            break
    else:  # pragma: no cover
        raise AssertionError("RuntimeExecutionCoordinator.execute 不存在")

    assert FREEZE_DOC.exists()
    freeze = FREEZE_DOC.read_text(encoding="utf-8")
    assert "Runtime Core 需要修改" in freeze
    assert "必须先回答" in freeze


def test_b2_thin_result_shape_is_adjudicated_in_doc() -> None:
    """A 形状（恰好三字段）与 B 方案的拒绝理由已成文。"""
    text = DOC.read_text(encoding="utf-8")
    for token in ("极薄 execution result", "RuntimeExecutionResult",
                  "session_mode", "session_id", "agent_response",
                  "双份事实源", "不重复表达", "命名应避开"):
        assert token in text, f"审计文档缺少 token：{token}"


def test_b3_internal_typed_contracts_exist_but_are_not_exposed() -> None:
    """B 方案 = 双份事实源的实证：各段 typed 契约已存在（IdentityResolution /
    RoutingResult / SessionResolution，均 frozen），**契约类本身**不外泄——
    Result 只携带三事实；wrapper 也从不引用这些契约类。

    （10.8.4 注：wrapper 会 import `app.domain.session_resolution` 的 mode
    常量（CREATE_NEW 用于 Evidence 判定）——那是常量复用，不是契约对象外泄。）"""
    for cls in (IdentityResolution, RoutingResult, SessionResolution):
        assert is_dataclass(cls) and cls.__dataclass_params__.frozen
    recording = (APPLICATION / "interaction_recording.py").read_text(
        encoding="utf-8")
    tree = ast.parse(recording, filename="interaction_recording.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module not in ("app.domain.identity_resolution",
                                       "app.domain.routing_result"), \
                f"wrapper 引用了内部契约：{node.module}"
        if isinstance(node, ast.ImportFrom) and \
                node.module == "app.domain.session_resolution":
            imported = {a.name for a in node.names}
            assert imported <= {"CREATE_NEW", "REUSE_ACTIVE"}, imported


# ================================================================== C. 契约成文


def test_c1_contract_is_written_down() -> None:
    """审计文档成文（防漂移）：四事实裁决 + A/B 裁决 + 治理门槛 token。"""
    assert DOC.exists(), f"缺少审计文档：{DOC}"
    text = DOC.read_text(encoding="utf-8")
    for token in ("① AgentResponse", "② external_session_id",
                  "③ Session created/reused", "④ Execution outcome",
                  "不引入 RuntimeResult 巨型 DTO", "10.1.4",
                  "弱键", "异常通道", "Evidence = 事实，不凭空制造"):
        assert token in text, f"审计文档缺少 token：{token}"
