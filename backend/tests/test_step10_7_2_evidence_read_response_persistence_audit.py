"""Step 10.7.2 —— Evidence Read & Response Persistence Semantics Audit
（审计步，生产代码 0 行改动）。

A. Read Path：Interaction → 管理员最小查询 → Activity 需要哪些**精确查询契约**：
   - 既有读面 = get_by_id + find_by_channel_and_external_id（单条身份查询已冻结）；
   - 最小缺口 = 恰好 2 个读方法：interactions.find_by_session(session_id)
     （会话→请求列表）+ activities.find_by_interaction(interaction_id)
     （Evidence 时间线）——数据面均已可行（FK+index），读方法缺失；
   - conversation 不上列：入口② = conversation → session（既有弱键读路径）
     → find_by_session；REUSE 天然把同会话请求聚合（免分组）；
   - 排序天然 (created_at, id)；分页形状（limit+游标）记录不实现。

B. Response Persistence：AgentResponse → Interaction.response_payload 记录什么：
   - 裁决 = **AgentResponse 的持久化快照**：{content, external_session_id,
     metadata} 三字段原样（不解释/不加工/不截断——Evidence = 事实）；
   - 状态矩阵：仅 dispatch 前失败为 NULL；succeeded×delivered 与
     succeeded×failed（Case B）都有值——物化后 10.5.3 的「Delivery Retry
     无承载物」获得承载物候选；
   - 谁来写未裁决：wrapper 观察不到 AgentResponse（9.1 execute() -> None），
     与 10.7.1 中间段 Evidence 是同一个接线点；
   - external_session_id 作为响应级事实 ≠ sessions.external_session_id 会话级
     绑定（主链路今天从不写后者——实证 session 行该列为 NULL）。

审计结论见 docs/step-10.7.2-evidence-read-response-persistence-audit.md。
"""

import ast
from dataclasses import fields
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
from app.domain.inbound_interaction import InboundInteraction
from app.domain.outbound_response import OutboundResponse
from app.domain.user import User
from app.persistence.models.activity import ActivityModel
from app.persistence.models.interaction import InteractionModel
from app.persistence.models.session import SessionModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"
APPLICATION = APP / "application"
REPO_ROOT = BACKEND.parent
DOC = REPO_ROOT / "docs" / "step-10.7.2-evidence-read-response-persistence-audit.md"


# ================================================================== 测试底座


class _CountingAgentAdapter:
    def __init__(self) -> None:
        self.calls = 0

    def send(self, request, connection) -> AgentResponse:  # noqa: ARG002
        self.calls += 1
        return AgentResponse(content=f"echo:{request.content}",
                             external_session_id="agent-session-1",
                             metadata={"latency_ms": 42})


class _FailingAgentAdapter:
    """Dispatch 必失败（响应从未诞生）。"""

    def send(self, request, connection) -> AgentResponse:  # noqa: ARG002
        raise AgentDispatchFailedError()


class _FailingChannelAdapter:
    """Delivery 必失败（Case B：响应已诞生）。"""

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


class _RecordingChannelStub:
    def __init__(self) -> None:
        self.deliveries: list[OutboundResponse] = []

    def deliver(self, outbound: OutboundResponse) -> None:
        self.deliveries.append(outbound)


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


def _port_methods(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.name)
    return {node.name for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)}


# ================================================================== A. Read Path


def test_a1_existing_read_surface_is_identity_lookup(db_engine) -> None:
    """既有读面 = get_by_id + find_by_channel_and_external_id；交互身份精确查
    真实可用（排查单条请求的入口①已冻结）。"""
    methods = _port_methods(APP / "repositories" / "interaction_record.py")
    assert {"get_by_id", "find_by_channel_and_external_id"} <= methods

    g = _seed(db_engine)
    _wrapped_runtime(db_engine).execute(_inbound(g["channel"], "msg-1"))
    with _uow_factory(db_engine)() as uow:
        found = uow.interactions.find_by_channel_and_external_id(
            g["channel"], "msg-1")
        assert found is not None
        by_id = uow.interactions.get_by_id(found.id)
        assert by_id is not None and by_id.id == found.id


def test_a2_find_by_session_is_missing_but_data_ready(db_engine) -> None:
    """契约缺口 #3：find_by_session 缺失（Port 无该方法、无分页形状）；数据面
    可行（session_id FK+index），且 REUSE 天然把同会话请求聚合。"""
    port_src = (APP / "repositories" / "interaction_record.py").read_text(
        encoding="utf-8")
    methods = _port_methods(APP / "repositories" / "interaction_record.py")
    assert "find_by_session" not in methods
    # 12.9.4.4：`limit` 的唯一合法出处 = find_pending（Worker pickup 的有界读取，
    # 12.9.3.4 裁决），且它不是分页——无 offset / 无游标 / 无翻页；可变形状仍禁止。
    for token in ("offset", "*args", "**kwargs"):
        assert token not in port_src, f"Port 出现分页/可变形状：{token}"
    assert InteractionModel.__table__.columns["session_id"].index

    g = _seed(db_engine)
    runtime = _wrapped_runtime(db_engine)
    runtime.execute(_inbound(g["channel"], "msg-1", conversation="conv-1"))
    runtime.execute(_inbound(g["channel"], "msg-2", conversation="conv-1"))

    rows = _interaction_rows(db_engine)
    assert len(rows) == 2 and rows[0].session_id == rows[1].session_id
    with SASession(db_engine) as s:
        listed = list(s.execute(
            select(InteractionModel)
            .where(InteractionModel.session_id == rows[0].session_id)
            .order_by(InteractionModel.id)).scalars().all())
    assert [r.external_interaction_id for r in listed] == ["msg-1", "msg-2"]


def test_a3_activity_read_is_missing_but_data_ready(db_engine) -> None:
    """（13.1 翻转）契约缺口 #4 已关闭：activity port 增加只读时间线
    `list_by_interaction`（数据面一直可行：interaction_id index + 时间线有序）。"""
    assert _port_methods(APP / "repositories" / "activity_record.py") == {
        "append", "list_by_interaction"}
    assert ActivityModel.__table__.columns["interaction_id"].index

    g = _seed(db_engine)
    _wrapped_runtime(db_engine).execute(_inbound(g["channel"], "msg-1"))
    row = _interaction_rows(db_engine)[0]
    with SASession(db_engine) as s:
        acts = list(s.execute(
            select(ActivityModel)
            .where(ActivityModel.interaction_id == row.id)
            .order_by(ActivityModel.id)).scalars().all())
    assert len(acts) >= 2
    stamps = [a.created_at for a in acts]
    assert all(t is not None for t in stamps)
    assert stamps == sorted(stamps)                  # 时间线天然有序


def test_a4_conversation_entry_is_a_two_step_chain(db_engine) -> None:
    """入口②（conversation → 会话历史）= 两步链：既有弱键读路径定位 session →
    按 session_id 取请求列表——conversation 不上列，schema 零演进。"""
    g = _seed(db_engine)
    runtime = _wrapped_runtime(db_engine)
    runtime.execute(_inbound(g["channel"], "msg-1", conversation="conv-1"))
    runtime.execute(_inbound(g["channel"], "msg-2", conversation="conv-1"))

    with _uow_factory(db_engine)() as uow:
        session = uow.sessions.find_active_by_identity_and_conversation(
            g["identity"], "conv-1")
        assert session is not None
    with SASession(db_engine) as s:
        listed = list(s.execute(
            select(InteractionModel)
            .where(InteractionModel.session_id == session.id)
            .order_by(InteractionModel.id)).scalars().all())
    assert [r.external_interaction_id for r in listed] == ["msg-1", "msg-2"]


# ================================================================== B. Response Persistence


def test_b1_response_payload_lands_on_full_success_only(db_engine) -> None:
    """（10.8.4 翻转）10.7.2 B2 状态矩阵的**诚实修订版**：
    - succeeded × delivered → 有值（AgentResponse 原样快照，缺口已修）；
    - succeeded × failed（Case B）→ 仍 NULL（失败走异常通道，Result 不存在）；
    - failed × pending（dispatch 前失败）→ 仍 NULL（响应从未诞生）。
    即：完整成功才落响应快照——Case B 的承载物仍需未来接线裁决。"""
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

    rows = {r.external_interaction_id: r for r in _interaction_rows(db_engine)}
    ok, df, dp = rows["msg-ok"], rows["msg-df"], rows["msg-dp"]
    assert (ok.processing_status, ok.delivery_status) == ("succeeded", "delivered")
    assert (df.processing_status, df.delivery_status) == ("succeeded", "failed")
    assert (dp.processing_status, dp.delivery_status) == ("failed", "pending")
    assert ok.response_payload == {                  # 缺口已修：四字段原样快照（V0.2 +artifacts）
        "content": "echo:hello",
        "external_session_id": "agent-session-1",
        "metadata": {"latency_ms": 42},
        "artifacts": None,
    }
    assert df.response_payload is None               # Case B：仍无承载物（诚实记录）
    assert dp.response_payload is None               # 响应从未诞生（语义上应保持 NULL）


def test_b2_what_to_record_is_the_agent_response_field_surface() -> None:
    """「记录什么」的来源：AgentResponse 字段面 = {content, external_session_id,
    metadata, artifacts}——response_payload 的候选形状即此四字段原样（V0.2 +artifacts）。"""
    names = {f.name for f in fields(AgentResponse)}
    assert names == {"content", "external_session_id", "metadata", "artifacts"}


def test_b3_wrapper_now_consumes_the_result_not_the_response_object() -> None:
    """（10.8.4 翻转）「谁来写」已落地：wrapper 接住 RuntimeExecutionResult
    并写 response_payload —— 但它仍不 import AgentResponse 领域对象本身
    （经 Result 的字段取值，不经响应类型）。"""
    recording = (APPLICATION / "interaction_recording.py").read_text(
        encoding="utf-8")
    # AgentResponse 领域对象从未到达 wrapper（AgentResponseInvalidError 只是
    # STAGE_FAILURE_MAP 的错误类型键，不是响应对象）
    recording_tree = ast.parse(recording, filename="interaction_recording.py")
    for node in ast.walk(recording_tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "app.domain.agent_response", \
                "wrapper import 了 AgentResponse 领域对象"
    assert "AgentResponse(" not in recording
    for name in ("coordinator.py", "runtime.py"):
        src = (APPLICATION / name).read_text(encoding="utf-8")
        tree = ast.parse(src, filename=name)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "execute":
                assert node.returns is not None
                # 10.8.3 翻转：Runtime 边界现返回 10.8.2 薄结果契约
                assert ast.unparse(node.returns) == "RuntimeExecutionResult", \
                    f"{name}.execute"
    # 10.8.4：wrapper 已接住 Result（同签名返回），但仍不经 AgentResponse 类型
    wrapper_src = (APPLICATION / "interaction_recording.py").read_text(
        encoding="utf-8")
    wrapper_tree = ast.parse(wrapper_src, filename="interaction_recording.py")
    for node in ast.walk(wrapper_tree):
        if isinstance(node, ast.FunctionDef) and node.name == "execute":
            assert node.returns is not None
            assert ast.unparse(node.returns) == "RuntimeExecutionResult", \
                "wrapper.execute"
            assert any(isinstance(sub, ast.Return) and sub.value is not None
                       for sub in ast.walk(node)), "wrapper 未返回结果"


def test_b4_session_level_external_session_id_is_bound_by_10_8_4(db_engine) -> None:
    """（10.8.4 翻转）响应级事实 → 会话级绑定已接通；
    （15.5.2 翻转）Coordinator 仅透传 external_session_id 给 dispatch（只读
    实参），仍不写库——绑定由 wrapper 经 `agent_response.external_session_id`
    写入，写入点唯一。"""
    for name in ("coordinator.py",):
        src = (APPLICATION / name).read_text(encoding="utf-8")
        assert "bind_external_session" not in src, \
            f"{name} 不应写会话级引用（写入点唯一在 wrapper）"
    recording = (APPLICATION / "interaction_recording.py").read_text(
        encoding="utf-8")
    assert "external_session_id" in recording        # wrapper 是唯一写入点

    g = _seed(db_engine)
    _wrapped_runtime(db_engine).execute(_inbound(g["channel"], "msg-1"))
    with SASession(db_engine) as s:
        session = s.execute(select(SessionModel)).scalars().one()
    assert session.external_session_id == "agent-session-1"


# ================================================================== C. 契约成文


def test_c1_contract_is_written_down() -> None:
    """审计文档成文（防漂移）：最小读契约 + response_payload 裁决 token。"""
    assert DOC.exists(), f"缺少审计文档：{DOC}"
    text = DOC.read_text(encoding="utf-8")
    for token in ("find_by_session(session_id)",
                  "find_by_interaction(interaction_id)",
                  "AgentResponse 的持久化快照",
                  "不解释、不加工、不截断",
                  "「Delivery Retry 无承载物」",
                  "execute() -> None",
                  "响应级事实",
                  "Channel scope",
                  "limit"):
        assert token in text, f"审计文档缺少 token：{token}"
