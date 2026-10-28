"""Step 10.8.3 —— RuntimeCoordinator Result Integration（接线步）。

只做一件事：RuntimeCoordinator.execute() 从 -> None 正式变成
-> RuntimeExecutionResult（10.8.2 薄结果契约）。

成功路径最终：

    Session（REUSE_ACTIVE → existing session.id / CREATE_NEW → newly created
    session.id，二者统一为 control_plane_session_id，绝不 None）
        +
    AgentResponse（Dispatch 产出，直接进入 Result）
        ↓
    RuntimeExecutionResult → return

失败路径：原 typed exception 原样传播（无结果对象）。

类型接线严格限幅（用户禁改清单全部锁死）：
- ✅ 改：application/runtime.py（Protocol 签名）、application/coordinator.py（返回）；
- ❌ 不改：Persistence wrapper / InteractionRecord / ActivityRecord /
  Repository contract / Adapter / Delivery 语义 / RUNTIME_EXECUTION_ORDER /
  Failure exception model / API response / Idempotency / Retry；
- ❌ 尤其不修 response_payload / sessions.external_session_id（10.8.4 的事）——
  wrapper 仍 -> None、结果被丢弃（过渡态，本文件锁死）。

验收 = 用户 9 条（REUSE/CREATE session_id、frozen AgentResponse、
external_session_id 只经 agent_response、失败走原异常、执行顺序不变、
Coordinator 无 Repository/ORM/Adapter 依赖、无第二套 Result 类型、全量回归）。
"""

import ast
from dataclasses import fields, is_dataclass
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session as SASession, sessionmaker

from app.application import RuntimeCoordinator, RuntimeExecutionCoordinator
from app.application.agent_dispatch import AgentDispatchExecutor
from app.application.delivery import DeliveryExecutor
from app.application.errors import (
    AccessAuthorizationDeniedError,
    AgentDispatchFailedError,
    AgentResponseInvalidError,
    ChannelDeliveryFailedError,
    IdentityResolutionFailedError,
    RoutingFailedError,
    SessionResolutionFailedError,
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
from app.domain.inbound_interaction import InboundInteraction
from app.domain.outbound_response import OutboundResponse
from app.domain.runtime_execution_result import RuntimeExecutionResult
from app.domain.session_resolution import CREATE_NEW, REUSE_ACTIVE
from app.domain.user import User
from app.persistence.models.session import SessionModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork
from tests.delivery_lifecycle_helper import DeliveringCoordinator

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"
APPLICATION = APP / "application"
REPO_ROOT = BACKEND.parent
DOC = REPO_ROOT / "docs" / "step-10.8.3-runtime-coordinator-result-integration.md"


# ================================================================== 测试底座


class _EchoAgentAdapter:
    """固定响应（external_session_id 可供 ④ 断言）。"""

    def send(self, request, connection) -> AgentResponse:  # noqa: ARG002
        return AgentResponse(content="echo:hello",
                             external_session_id="agent-session-1")


class _RecordingChannelStub:
    def __init__(self) -> None:
        self.deliveries: list[OutboundResponse] = []

    def deliver(self, outbound: OutboundResponse) -> None:
        self.deliveries.append(outbound)


class _FailingAgentAdapter:
    def send(self, request, connection) -> AgentResponse:  # noqa: ARG002
        raise AgentDispatchFailedError()


class _FailingChannelAdapter:
    def deliver(self, outbound: OutboundResponse) -> None:
        raise ChannelDeliveryFailedError(
            channel_id=outbound.channel_id,
            external_conversation_id=outbound.external_conversation_id)


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
        for conversation in ("conv-1", "conv-dp", "conv-df"):
            uow.connection_conversations.create(ConnectionConversation(
                channel_id=channel.id, external_conversation_id=conversation,
                connection_id=path.id))
        uow.commit()
        return {"channel": channel.id, "agent": agent.id,
                "identity": ident.id}


def _coordinator(db_engine, agent_adapter=None, channel_adapter=None) -> RuntimeCoordinator:
    """直连 RuntimeCoordinator（不经 PersistingCoordinator——wrapper 未接线，
    会丢弃结果；本步验收的是 Runtime 自身的返回值）。"""
    uow = _uow_factory(db_engine)
    return DeliveringCoordinator(RuntimeCoordinator(
        identity_resolution=IdentityResolutionExecutor(uow),
        access_authorization=AccessAuthorizationExecutor(uow),
        routing=RoutingExecutor(uow),
        session_resolution=SessionResolutionExecutor(uow),
        session_creation=SessionCreationExecutor(uow),
        agent_dispatch=AgentDispatchExecutor(
            agent_adapter=agent_adapter or _EchoAgentAdapter(),
            uow_factory=uow),
        delivery=DeliveryExecutor(
            channel_adapter=channel_adapter or _RecordingChannelStub()),
    ))


def _inbound(channel_id: int, interaction: str = "msg-1",
             conversation: str = "conv-1",
             identity: str = "wx_a") -> InboundInteraction:
    return InboundInteraction(
        channel_id=channel_id, external_interaction_id=interaction,
        external_conversation_id=conversation,
        external_identity_id=identity, content="hello")


def _session_rows(db_engine) -> list[SessionModel]:
    with SASession(db_engine) as s:
        return list(s.execute(select(SessionModel)
                              .order_by(SessionModel.id)).scalars().all())


# ================================================================== A. 成功路径返回 RuntimeExecutionResult


def test_a1_create_new_returns_the_new_session_id(db_engine) -> None:
    """CREATE_NEW → newly created session.id：结果 session_id == DB 中新建的
    那条 Session（非 None、> 0）。"""
    g = _seed(db_engine)
    result = _coordinator(db_engine).execute(_inbound(g["channel"]))

    assert isinstance(result, RuntimeExecutionResult)
    assert result.session_mode == CREATE_NEW
    rows = _session_rows(db_engine)
    assert len(rows) == 1
    assert result.session_id == rows[0].id
    assert result.session_id > 0


def test_a2_reuse_active_returns_the_existing_session_id(db_engine) -> None:
    """REUSE_ACTIVE → existing session.id：第二次执行（同会话）返回与首次
    完全相同的 session_id（复用，不新建）。"""
    g = _seed(db_engine)
    coordinator = _coordinator(db_engine)
    first = coordinator.execute(_inbound(g["channel"], "msg-1"))
    second = coordinator.execute(_inbound(g["channel"], "msg-2"))

    assert first.session_mode == CREATE_NEW
    assert second.session_mode == REUSE_ACTIVE
    assert second.session_id == first.session_id      # 复用同一条 Session
    assert len(_session_rows(db_engine)) == 1         # DB 中仍只有一条


def test_a3_both_modes_return_the_same_frozen_agent_response_type(db_engine) -> None:
    """两种模式都返回同一个 frozen AgentResponse 对象类型（8.5 契约，
    直接进入 Result，不复制不包装）。"""
    g = _seed(db_engine)
    coordinator = _coordinator(db_engine)
    created = coordinator.execute(_inbound(g["channel"], "msg-1"))
    reused = coordinator.execute(_inbound(g["channel"], "msg-2"))

    for result in (created, reused):
        assert isinstance(result.agent_response, AgentResponse)
        assert is_dataclass(AgentResponse)
        assert AgentResponse.__dataclass_params__.frozen is True
    assert created.agent_response.content == "echo:hello"
    assert is_dataclass(RuntimeExecutionResult)
    assert RuntimeExecutionResult.__dataclass_params__.frozen is True


def test_a4_external_session_id_only_via_agent_response(db_engine) -> None:
    """external_session_id 只能通过 agent_response 获取：Result 字段面恰好
    三字段，顶层无该字段；agent_response.external_session_id 可读。"""
    g = _seed(db_engine)
    result = _coordinator(db_engine).execute(_inbound(g["channel"]))

    assert {f.name for f in fields(RuntimeExecutionResult)} == {
        "session_mode", "session_id", "agent_response"}
    assert not hasattr(result, "external_session_id")
    assert result.agent_response.external_session_id == "agent-session-1"


# ================================================================== B. 失败路径仍走原异常通道


def test_b1_failures_keep_the_original_typed_exception_channel(db_engine) -> None:
    """失败 → 原 typed exception 原样传播，无结果对象：
    identity / dispatch / delivery 三类失败类型不变（Failure exception model
    零改动）。"""
    g = _seed(db_engine)
    with pytest.raises(IdentityResolutionFailedError):
        _coordinator(db_engine).execute(
            _inbound(g["channel"], identity="wx_unknown"))
    with pytest.raises(AgentDispatchFailedError):
        _coordinator(db_engine, agent_adapter=_FailingAgentAdapter()).execute(
            _inbound(g["channel"], "msg-dp", conversation="conv-dp"))
    with pytest.raises(ChannelDeliveryFailedError):
        _coordinator(db_engine,
                     channel_adapter=_FailingChannelAdapter()).execute(
            _inbound(g["channel"], "msg-df", conversation="conv-df"))


# ================================================================== C. 边界锁死（禁改清单）


def test_c1_execution_order_is_untouched() -> None:
    """RUNTIME_EXECUTION_ORDER 完全不变（Package 2 / Step 2 之前的六段相对顺序零改动）。

    Step 2 只在 Identity 与 Routing 之间插入 authorization——既有六段一个都没动。
    """
    from app.application.runtime import RUNTIME_EXECUTION_ORDER
    assert RUNTIME_EXECUTION_ORDER == (
        "identity_resolution", "authorization", "routing", "session_resolution",
        "session_creation", "agent_dispatch", "delivery",
    )


def test_c2_coordinator_gains_no_repository_orm_adapter_dependency() -> None:
    """Coordinator 不因此获得 Repository / ORM / Adapter 依赖：import 面仍
    errors / runtime Port / app.domain.*；无新类；无 *Service。"""
    src = (APPLICATION / "coordinator.py").read_text(encoding="utf-8")
    tree = ast.parse(src, filename="coordinator.py")
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module)
    assert all(r.startswith("app.domain")
               or r in {"app.application.errors", "app.application.runtime"}
               for r in roots), roots
    assert not any(r.startswith(("app.persistence", "app.adapters",
                                 "app.repositories", "sqlalchemy", "fastapi"))
                   for r in roots)
    classes = {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
    assert classes == {"RuntimeCoordinator"}


def test_c3_no_second_result_type_in_runtime_core() -> None:
    """Runtime Core 没有出现第二套 Result 类型：全 app 树 *Result 类只有
    RoutingResult（8.4）+ RuntimeExecutionResult（10.8.2）。"""
    names: set[str] = set()
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names |= {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
    assert {n for n in names if n.endswith("Result")} == {
        "RoutingResult", "RuntimeExecutionResult"}


def test_c4_forbidden_surfaces_stay_untouched(db_engine) -> None:
    """禁改清单锁死：wrapper / InteractionRecord / ActivityRecord / Repository
    contract / Adapter / API 全部不认识 RuntimeExecutionResult；
    response_payload 与 sessions.external_session_id 仍未被修（10.8.4）；
    Failure exception model（STAGE_FAILURE_MAP）不变。"""
    # （10.8.4 翻转）wrapper 已接线并引用 Result；Runtime use case 仍未引用
    untouched = ("channel_inbound.py",)               # Runtime use case
    for name in untouched:
        src = (APPLICATION / name).read_text(encoding="utf-8")
        assert "RuntimeExecutionResult" not in src, name
    for rel in ("api/runtime/channels.py",           # API response
                "adapters/agents/acp.py",            # Adapter
                "adapters/channels/wecom.py"):
        src = (APP / rel).read_text(encoding="utf-8")
        assert "RuntimeExecutionResult" not in src, rel
    for rel in ("repositories/interaction_record.py",  # Repository contract
                "persistence/repositories/interaction_record.py",
                "domain/interaction_record.py",
                "domain/activity_record.py"):
        src = (APP / rel).read_text(encoding="utf-8")
        assert "RuntimeExecutionResult" not in src, rel

    # （10.8.4 翻转）response_payload / sessions.external_session_id 由 wrapper
    # 从 Result 写入——Runtime 侧（coordinator）仍不写库，分工保持
    recording = (APPLICATION / "interaction_recording.py").read_text(
        encoding="utf-8")
    assert "response_payload" in recording
    assert "external_session_id" in recording
    coordinator_src = (APPLICATION / "coordinator.py").read_text(
        encoding="utf-8")
    # 15.5.2 翻转：Coordinator 透传 external_session_id（只读实参），
    # 但仍不写库——绑定唯一写入点在 wrapper。
    assert "bind_external_session" not in coordinator_src

    # Failure exception model 不变（六段 typed error 映射原样）
    from app.application.interaction_recording import STAGE_FAILURE_MAP
    assert set(STAGE_FAILURE_MAP) == {
        IdentityResolutionFailedError, AccessAuthorizationDeniedError,
        RoutingFailedError,
        SessionResolutionFailedError, AgentDispatchFailedError,
        AgentResponseInvalidError, ChannelDeliveryFailedError,
    }


def test_c5_protocol_conformance_is_preserved() -> None:
    """RuntimeCoordinator 与 PersistingCoordinator 都仍是
    RuntimeExecutionCoordinator 形状：
    - Coordinator 显式继承该 Protocol（MRO 保留）；
    - wrapper 仍以同一 duck-type 形状提供 execute（其返回值接线属 10.8.4）。"""
    mro_names = {c.__name__ for c in RuntimeCoordinator.__mro__}
    assert "RuntimeExecutionCoordinator" in mro_names
    assert callable(getattr(PersistingCoordinator, "execute", None))


def test_c6_runtime_protocol_signature_is_formal() -> None:
    """接线是正式的：runtime.py 的 Protocol execute 注解 =
    RuntimeExecutionResult（AST 校验，非 docstring 口头声明）。"""
    src = (APPLICATION / "runtime.py").read_text(encoding="utf-8")
    tree = ast.parse(src, filename="runtime.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "execute":
            assert node.returns is not None
            assert ast.unparse(node.returns) == "RuntimeExecutionResult"
            break
    else:  # pragma: no cover
        raise AssertionError("RuntimeExecutionCoordinator.execute 不存在")


# ================================================================== D. 契约成文


def test_d1_contract_is_written_down() -> None:
    """接线契约成文（防漂移）。"""
    assert DOC.exists(), f"缺少接线文档：{DOC}"
    text = DOC.read_text(encoding="utf-8")
    for token in ("RuntimeExecutionResult",
                  "REUSE_ACTIVE", "CREATE_NEW",
                  "原 typed exception",
                  "RUNTIME_EXECUTION_ORDER",
                  "response_payload",
                  "sessions.external_session_id",
                  "10.8.4",
                  "PersistingCoordinator"):
        assert token in text, f"接线文档缺少 token：{token}"
