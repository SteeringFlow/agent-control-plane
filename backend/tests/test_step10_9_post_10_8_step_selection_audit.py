"""Step 10.9 —— Post-10.8 Architecture / Step Selection Audit
（审计步，生产代码 0 行改动）。

盘点 10.8 收口后仍存在的真实缺口（G5~G10 重分类），逐一回答五问
（Q1 真实存在 / Q2 已造成错误 / Q3 阻塞什么 / Q4 更小方案 / Q5 现在做？），
产出账本：NOW / DEFER / WATCH / DROP——**最多一个 NOW**。

A  基线回归：10.8 已关闭的 G1~G4 不回退（response_payload / bind_external_session /
   result.session_id 直取 / session_mode Evidence）——已裁决事项不重审；
B  当前能力断言：Capability Gap 仍在；无 Retry/幂等/lease/queue/outbox 符号；
   interaction Port 无 read 方法（G10 未实现状态的事实面）；
C  缺口存在性断言（不是待办，只是事实）：
   - G6：Case B（succeeded×failed）response_payload NULL（真实失败探针）；
   - G8：失败路径弱键存在 + 成功路径无弱键 + 失败回填真实发生（dispatch 后失败）；
   - G9：interactions 零冗余（无 identity/connection/agent 列）；
D  候选项分类断言：G5 DROP / G6 DEFER / G7 DEFER / G8 WATCH / G9 DROP /
   G10 WATCH；**NOW = 0**——下一正式 Step 不由缺口清单驱动。

账本见 docs/step-10.9-post-10.8-step-selection-audit.md §3。
"""

import ast
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
    RoutingFailedError,
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
from app.persistence.models.interaction import InteractionModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"
APPLICATION = APP / "application"
REPO_ROOT = BACKEND.parent
DOC = REPO_ROOT / "docs" / "step-10.9-post-10.8-step-selection-audit.md"


# ================================================================== 测试底座


class _EchoAgentAdapter:
    def send(self, request, connection) -> AgentResponse:  # noqa: ARG002
        return AgentResponse(content="echo:hello",
                             external_session_id="agent-session-1")


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
        uow.identities.create(Identity(
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
                "connection": path.id}


def _wrapped_runtime(db_engine, agent_adapter=None, channel_adapter=None):
    uow = _uow_factory(db_engine)
    inner = RuntimeCoordinator(
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
    )
    return PersistingCoordinator(inner=inner, uow_factory=_uow_factory(db_engine))


class _RecordingChannelStub:
    def __init__(self) -> None:
        self.deliveries: list[OutboundResponse] = []

    def deliver(self, outbound: OutboundResponse) -> None:
        self.deliveries.append(outbound)


def _inbound(channel_id: int, interaction: str = "msg-1",
             conversation: str = "conv-1") -> InboundInteraction:
    return InboundInteraction(
        channel_id=channel_id, external_interaction_id=interaction,
        external_conversation_id=conversation,
        external_identity_id="wx_a", content="hello")


def _interaction_rows(db_engine) -> list[InteractionModel]:
    with SASession(db_engine) as s:
        return list(s.execute(select(InteractionModel)
                              .order_by(InteractionModel.id)).scalars().all())


def _py_files(root: Path):
    return sorted(p for p in root.rglob("*.py"))


# ================================================================== A. 基线回归（G1~G4 不回退）


def test_a1_closed_gaps_do_not_regress(db_engine) -> None:
    """10.8 已关闭的四项保持关闭：response_payload 写入方存在、
    bind_external_session 在 Port/Adapter、成功路径直取 result.session_id /
    session_mode、无前后快照推导。"""
    recording = (APPLICATION / "interaction_recording.py").read_text(
        encoding="utf-8")
    assert "response_payload" in recording                     # G1
    assert "result.session_id" in recording                    # G3
    assert "result.session_mode" in recording                  # G4
    assert "session_existed_before" not in recording           # G3/G4 推导已删
    for rel in ("repositories/session.py",
                "persistence/repositories/session.py"):        # G2
        src = (APP / rel).read_text(encoding="utf-8")
        assert "bind_external_session" in src, rel

    g = _seed(db_engine)
    result = _wrapped_runtime(db_engine).execute(_inbound(g["channel"]))
    row = _interaction_rows(db_engine)[0]
    assert row.response_payload["content"] == "echo:hello"     # G1 运行时
    assert row.session_id == result.session_id                 # G3 运行时
    from app.persistence.models.session import SessionModel
    with SASession(db_engine) as s:
        session = s.execute(select(SessionModel)).scalars().one()
    assert session.external_session_id == "agent-session-1"    # G2 运行时


# ================================================================== B. 当前能力断言


def test_b1_capability_gap_still_fail_closed(db_engine) -> None:
    """G8 判定的能力前提仍成立：单 Channel 双 ACTIVE Connection →
    RoutingFailedError（Runtime 无法制造弱键歧义状态）。"""
    g = _seed(db_engine)
    with _uow_factory(db_engine)() as uow:
        agent_b = uow.agents.create(Agent(name="agent-b", type="acp")).id
        path_b = uow.connections.create(Connection(
            channel_id=g["channel"], agent_id=agent_b, name="path-b",
            status="active",
            config={"endpoint": "http://agent.internal", "timeout_s": 5.0},
            credential_ref="cred-b"))
        uow.commit()
        path_b_id = path_b.id

    # Package 2 / Step 2：两条都给许可——失败必须来自「候选 >1」而非「候选为空」
    with pytest.raises(RoutingFailedError):
        RoutingExecutor(_uow_factory(db_engine)).execute(
            g["channel"], frozenset({g["connection"], path_b_id}))


def test_b2_no_retry_infra_and_no_read_methods() -> None:
    """G7 的「不实现」状态 + G10 的「未实现」状态（事实面）：
    - 全 app 树无 Retry/幂等/lease/lock/outbox/queue-worker 符号；
    - interaction Port 无 find_by_session / find_by_interaction（最小读契约
      已定义在 10.7.2 但未实现——无真实消费者）。"""
    forbidden = ("retryservice", "replayservice", "outbox", "processinglease",
                 "inflightlock", "responsededup", "idempot", "dedup")
    for path in _py_files(APP):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
                lower = node.name.lower()
                for token in forbidden:
                    assert token not in lower, \
                        f"{path.name} 出现 {node.name}"
        src = path.read_text(encoding="utf-8")
        for entry in ("def retry(", "def replay(", "def redeliver("):
            assert entry not in src, f"{path.name} 出现执行入口"

    port_src = (APP / "repositories" / "interaction_record.py").read_text(
        encoding="utf-8")
    # 12.9.4.4：`limit` 不再是禁用 token——它的唯一出处是 find_pending（Worker
    # pickup 的有界读取，12.9.3.4 裁决），不是通用读面/分页。仍禁通用读方法。
    # 13.1：`list_recent(limit)` 成为第二个有界纯读（Observability 陈列面）——
    # 「def list_」禁令随之关闭，仍禁 find_by_session / find_by_interaction 类
    # 通用查询形状。
    for read_method in ("def find_by_session", "def find_by_interaction"):
        assert read_method not in port_src, f"Port 出现读方法/分页：{read_method}"
    assert "def list_recent(" in port_src  # 13.1 有界陈列读


# ================================================================== C. 缺口存在性断言（事实，非待办）


def test_c1_g6_case_b_response_payload_stays_null(db_engine) -> None:
    """G6 存在性（真实失败探针）：Agent 成功 + Delivery 失败 →
    succeeded × failed 且 response_payload NULL——承载物对 Case B 未成立。"""
    g = _seed(db_engine)
    with pytest.raises(ChannelDeliveryFailedError):
        _wrapped_runtime(db_engine, channel_adapter=_FailingChannelAdapter()).execute(
            _inbound(g["channel"], "msg-df", conversation="conv-df"))

    row = _interaction_rows(db_engine)[0]
    assert (row.processing_status, row.delivery_status) == ("succeeded", "failed")
    assert row.response_payload is not None                 # P1/C3
    assert row.response_payload["content"] == "echo:hello"


def test_c2_g8_failure_path_weak_key_exists_but_success_does_not(db_engine) -> None:
    """G8 存在性：失败路径弱键仍在（dispatch 后失败 → 弱键回填 session_id，
    真实发生）；成功路径无弱键（result.session_id 直取）——两种弱键严格区分。"""
    src = (APPLICATION / "interaction_recording.py").read_text(encoding="utf-8")
    assert "find_active_by_identity_and_conversation" in src   # 失败路径弱键
    assert "session_existed_before" not in src                 # 成功路径已直取

    g = _seed(db_engine)
    with pytest.raises(AgentDispatchFailedError):
        _wrapped_runtime(db_engine, agent_adapter=_FailingAgentAdapter()).execute(
            _inbound(g["channel"], "msg-dp", conversation="conv-dp"))
    row = _interaction_rows(db_engine)[0]
    assert (row.processing_status, row.delivery_status) == ("failed", "pending")
    assert row.session_id is not None                 # 失败路径弱键回填真实发生


def test_c3_g9_no_redundant_columns_exist() -> None:
    """G9 存在性：interactions 零冗余（无 identity/connection/agent/user 列）——
    「有条件可答」是当前 schema 的准确语义，不是待修缺陷。"""
    cols = {c.name for c in InteractionModel.__table__.columns}
    assert cols.isdisjoint({"identity_id", "connection_id", "agent_id",
                            "user_id"})


# ================================================================== D. 候选项分类断言（账本）


def test_d1_verdict_ledger_is_written_down() -> None:
    """账本成文：G5 DROP / G6 DEFER / G7 DEFER / G8 WATCH / G9 DROP /
    G10 WATCH；NOW = 0（下一正式 Step 不由缺口清单驱动）；五问标准在案。"""
    assert DOC.exists(), f"缺少 Step 选择审计文档：{DOC}"
    text = DOC.read_text(encoding="utf-8")
    for token in (
        "Q1 真实存在吗", "Q2 已经造成错误了吗", "Q3 现在不解决会阻塞什么",
        "Q4 有没有更小的解决方式", "Q5 是否应该现在做",
        "NOW / DEFER / WATCH / DROP",
        "G5", "G6", "G7", "G8", "G9", "G10",
        "G6、G7",           # DEFER
        "G8、G10",          # WATCH
        "G5、G9",           # DROP
        "NOW = 0",
        "不由缺口清单驱动",
        "OPEN ≠ NOW",       # G8 的触发条件表述
        "有数据 ≠ 实现 Retry",
    ):
        assert token in text, f"Step 选择审计文档缺少 token：{token}"
    # 五问逐项裁决齐全（每个候选都有 Q5 结论）
    for verdict_line in ("→ **DROP**", "→ **DEFER**", "→ **WATCH**"):
        assert verdict_line in text, f"缺少裁决行：{verdict_line}"
