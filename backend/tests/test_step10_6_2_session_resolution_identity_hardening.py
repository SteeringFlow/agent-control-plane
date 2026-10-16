"""Step 10.6.2 —— Session Resolution Identity Hardening（决策/验证步，生产代码 0 行改动）。

目标：让 Session Resolution / Persistence Observation 在多 Connection /
多 Agent 场景下拥有**确定的查询语义**。

Session 的真正 Identity = `(identity_id, connection_id, external_conversation_id)`：
    identity   → channel-scoped external actor
    connection → selected Agent boundary
    conversation → channel-side context

关键裁决：**agent_id 不是 Session 的选择键**——Agent 由 Connection 派生，
Connection 是 Session 的 Agent 隔离键；绝不让 agent_id 成为第二个查询维度。

核心验证（不改 Runtime）：
    Case A  X+C+A → 只有 Session A（同键唯一、查询确定）
    Case B  X+C 跨 A/B 两条 Session：X+A+C→A、X+B+C→B，绝不 MultipleResultsFound
    Case C  同 Conversation 两条 Session 可同时 active——切换 Agent = 新 Session，
            不改旧 Session
另：Persistence Observation 的弱键 (identity, conversation) 在 Case B/C 下是
唯一歧义源（MultipleResultsFound）；DB 无 UNIQUE（9.7 evidence-first 快照）。

语义裁决见 docs/step-10.6.2-session-resolution-identity-hardening.md §8 一句话版。
"""

import ast
from pathlib import Path

import pytest
from sqlalchemy import UniqueConstraint, inspect, select
from sqlalchemy.exc import IntegrityError, MultipleResultsFound
from sqlalchemy.orm import Session as SASession, sessionmaker

from app.application.session_creation import SessionCreationExecutor
from app.application.session_resolution import CREATE_NEW, REUSE_ACTIVE
from app.application.session_resolution import SessionResolutionExecutor
from app.domain.agent import Agent
from app.domain.channel import Channel
from app.domain.connection import Connection
from app.domain.identity import Identity
from app.domain.session import Session
from app.domain.user import User
from app.persistence.models.session import SessionModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"
APPLICATION = APP / "application"
REPO_ROOT = BACKEND.parent
DOC = REPO_ROOT / "docs" / "step-10.6.2-session-resolution-identity-hardening.md"


# ================================================================== 测试底座


def _uow_factory(db_engine):
    return lambda: SqlAlchemyUnitOfWork(
        session_factory=sessionmaker(bind=db_engine, expire_on_commit=False))


def _seed_channel(db_engine, channel_name: str = "ch-1") -> dict[str, int]:
    with _uow_factory(db_engine)() as uow:
        user = uow.users.create(User(name="alice"))
        channel = uow.channels.create(Channel(name=channel_name, type="wecom"))
        ident = uow.identities.create(Identity(
            user_id=user.id, channel_id=channel.id, external_id="wx_a"))
        uow.commit()
        return {"channel": channel.id, "identity": ident.id}


def _create_agent(db_engine, name: str) -> int:
    with _uow_factory(db_engine)() as uow:
        agent = uow.agents.create(Agent(name=name, type="acp"))
        uow.commit()
        return agent.id


def _create_connection(db_engine, channel_id: int, agent_id: int,
                       name: str) -> int:
    with _uow_factory(db_engine)() as uow:
        conn = uow.connections.create(Connection(
            channel_id=channel_id, agent_id=agent_id, name=name,
            status="active",
            config={"endpoint": "http://agent.internal", "timeout_s": 5.0},
            credential_ref="cred"))
        uow.commit()
        return conn.id


def _create_session(db_engine, identity_id: int, channel_id: int,
                    connection_id: int, agent_id: int,
                    conversation: str) -> int:
    with _uow_factory(db_engine)() as uow:
        sess = uow.sessions.create(Session(
            identity_id=identity_id, channel_id=channel_id,
            connection_id=connection_id, agent_id=agent_id,
            external_conversation_id=conversation, status="active"))
        uow.commit()
        return sess.id


def _active_rows(db_engine) -> list[SessionModel]:
    with SASession(db_engine) as s:
        return list(s.execute(
            select(SessionModel).where(SessionModel.status == "active")
            .order_by(SessionModel.id)).scalars().all())


# ================================================================== A. Identity 语义（静态）


def test_a1_canonical_key_is_three_dimensions_no_agent_id() -> None:
    """三维键是 find_active / Session Resolution 的完整签名：没有 agent_id 参数。"""
    # SessionResolutionExecutor.execute
    src = (APPLICATION / "session_resolution.py").read_text(encoding="utf-8")
    tree = ast.parse(src, filename="session_resolution.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "execute":
            args = [a.arg for a in node.args.args]
            assert args == ["self", "identity_id", "connection_id",
                            "external_conversation_id"], args
            assert "agent_id" not in args
            break
    else:  # pragma: no cover
        raise AssertionError("SessionResolutionExecutor.execute 不存在")

    # SessionRepository.find_active（Port）
    port_src = (APP / "repositories" / "session.py").read_text(encoding="utf-8")
    tree = ast.parse(port_src, filename="session.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "find_active":
            args = [a.arg for a in node.args.args]
            assert args == ["self", "identity_id", "connection_id",
                            "external_conversation_id"], args
            break
    else:  # pragma: no cover
        raise AssertionError("SessionRepository.find_active 不存在")


def test_a2_adapter_query_does_not_filter_agent_id() -> None:
    """Adapter 的 find_active WHERE 只按三维键 + status 过滤：agent 不参与选择。"""
    src = (APP / "persistence" / "repositories"
           / "session.py").read_text(encoding="utf-8")
    lines = src.splitlines()
    tree = ast.parse(src, filename="session.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "find_active":
            body = "\n".join(lines[node.lineno - 1:node.end_lineno])
            assert "agent_id" not in body, "find_active 过滤了 agent_id"
            assert "identity_id" in body and "connection_id" in body \
                and "external_conversation_id" in body
            break
    else:  # pragma: no cover
        raise AssertionError("find_active 不存在于 Adapter")


def test_a3_sessions_unique_constraint_materialized() -> None:
    """Cloud-0.1.1-B：sessions 三维隔离键（+ tenant 前导）已物化为 DB 唯一约束
    （9.7 的 evidence-first「无唯一约束」快照被隔离演进翻转）。
    唯一键 = (tenant_id, identity_id, connection_id, external_conversation_id)。"""
    uniques = [c for c in SessionModel.__table__.constraints
               if isinstance(c, UniqueConstraint)]
    assert any(
        {col.name for col in u.columns} == {
            "tenant_id", "identity_id", "connection_id", "external_conversation_id"}
        for u in uniques
    ), f"sessions 缺少租户前导三维唯一约束：{uniques}"


def test_a4_session_creation_derives_agent_id_not_self_lookup() -> None:
    """Session Creation 的 agent_id 来自调用方（RoutingResult），组件不自查 Agent
    ——Agent 只经 Connection 派生，仓储/域层无 agent 选择入口。"""
    src = (APPLICATION / "session_creation.py").read_text(encoding="utf-8")
    assert "agent_id: int" in src                    # 输入必带（来自 Routing）
    tree = ast.parse(src, filename="session_creation.py")
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    forbidden = ("app.repositories.agent", "app.repositories.connection",
                 "app.domain.agent", "app.domain.connection")
    hit = [r for r in roots if any(r == f or r.startswith(f + ".")
                                   for f in forbidden)]
    assert not hit, f"Session Creation 自我查找 Agent/Connection：{hit}"


# ================================================================== B. 核心验证：Case A / B / C


def test_b1_case_a_single_session_query_is_deterministic(db_engine) -> None:
    """Case A：Identity X + Conversation C + Connection A → Session A（唯一）。
    查询 X+A+C 稳定返回同一条，REUSE 复用同一 id。"""
    g = _seed_channel(db_engine)
    agent_x = _create_agent(db_engine, "agent-x")
    conn_a = _create_connection(db_engine, g["channel"], agent_x, "conn-a")
    sa = _create_session(db_engine, g["identity"], g["channel"],
                         conn_a, agent_x, "conv-1")

    with _uow_factory(db_engine)() as uow:
        found = uow.sessions.find_active(g["identity"], conn_a, "conv-1")
        assert found is not None and found.id == sa
        again = uow.sessions.find_active(g["identity"], conn_a, "conv-1")
        assert again is not None and again.id == sa    # 稳定同一 Session

    result = SessionResolutionExecutor(_uow_factory(db_engine)).execute(
        g["identity"], conn_a, "conv-1")
    assert result.mode == REUSE_ACTIVE
    assert result.existing_session_id == sa
    assert len(_active_rows(db_engine)) == 1


def test_b2_case_b_cross_connection_queries_are_isolated(db_engine) -> None:
    """Case B：同 (X, conv) 跨 Connection A/B 两条 Session——查询 X+A+C→SA、
    X+B+C→SB，各自精确命中，绝不 MultipleResultsFound。"""
    g = _seed_channel(db_engine)
    agent_x = _create_agent(db_engine, "agent-x")
    agent_y = _create_agent(db_engine, "agent-y")
    conn_a = _create_connection(db_engine, g["channel"], agent_x, "conn-a")
    conn_b = _create_connection(db_engine, g["channel"], agent_y, "conn-b")
    sa = _create_session(db_engine, g["identity"], g["channel"],
                         conn_a, agent_x, "conv-1")
    sb = _create_session(db_engine, g["identity"], g["channel"],
                         conn_b, agent_y, "conv-1")

    with _uow_factory(db_engine)() as uow:
        # 两条查询各自精确返回，任何一条都不触发 MultipleResultsFound
        got_a = uow.sessions.find_active(g["identity"], conn_a, "conv-1")
        got_b = uow.sessions.find_active(g["identity"], conn_b, "conv-1")
    assert got_a is not None and got_a.id == sa
    assert got_b is not None and got_b.id == sb
    assert got_a.id != got_b.id
    assert len(_active_rows(db_engine)) == 2


def test_b3_case_c_both_sessions_coexist_active_old_unchanged(db_engine) -> None:
    """Case C：同 Conversation 在 Connection A/B 的两条 Session 同时 active——
    切换 Agent = 新 Session，不是修改旧 Session（SA 原样保持）。"""
    g = _seed_channel(db_engine)
    agent_x = _create_agent(db_engine, "agent-x")
    agent_y = _create_agent(db_engine, "agent-y")
    conn_a = _create_connection(db_engine, g["channel"], agent_x, "conn-a")
    conn_b = _create_connection(db_engine, g["channel"], agent_y, "conn-b")
    sa = _create_session(db_engine, g["identity"], g["channel"],
                         conn_a, agent_x, "conv-1")
    sb = _create_session(db_engine, g["identity"], g["channel"],
                         conn_b, agent_y, "conv-1")

    rows = _active_rows(db_engine)
    assert {r.id for r in rows} == {sa, sb}          # 同时 active
    row_a = next(r for r in rows if r.id == sa)
    assert row_a.connection_id == conn_a             # 旧 Session 未被修改
    assert row_a.identity_id == g["identity"]
    assert row_a.external_conversation_id == "conv-1"
    assert row_a.status == "active"


def test_b4_switch_agent_is_create_new_then_reuse_own(db_engine) -> None:
    """切换 Connection（= 换 Agent）：先 CREATE_NEW；建好后再查该 Connection
    则 REUSE 自己的那一条（B 不干扰 A，A 不干扰 B）。"""
    g = _seed_channel(db_engine)
    agent_x = _create_agent(db_engine, "agent-x")
    agent_y = _create_agent(db_engine, "agent-y")
    conn_a = _create_connection(db_engine, g["channel"], agent_x, "conn-a")
    conn_b = _create_connection(db_engine, g["channel"], agent_y, "conn-b")
    sa = _create_session(db_engine, g["identity"], g["channel"],
                         conn_a, agent_x, "conv-1")
    resolver = SessionResolutionExecutor(_uow_factory(db_engine))

    before = resolver.execute(g["identity"], conn_b, "conv-1")
    assert before.mode == CREATE_NEW                   # B 上还没有会话

    sb = _create_session(db_engine, g["identity"], g["channel"],
                         conn_b, agent_y, "conv-1")
    after_b = resolver.execute(g["identity"], conn_b, "conv-1")
    after_a = resolver.execute(g["identity"], conn_a, "conv-1")
    assert after_b.mode == REUSE_ACTIVE and after_b.existing_session_id == sb
    assert after_a.mode == REUSE_ACTIVE and after_a.existing_session_id == sa


def test_b5_persistence_observation_weak_key_is_only_ambiguity_source(db_engine) -> None:
    """Persistence Observation 弱键 (identity, conversation) 在 Case B/C 下歧义
    （MultipleResultsFound）——它是唯一歧义源；三维键查询免疫。"""
    g = _seed_channel(db_engine)
    agent_x = _create_agent(db_engine, "agent-x")
    agent_y = _create_agent(db_engine, "agent-y")
    conn_a = _create_connection(db_engine, g["channel"], agent_x, "conn-a")
    conn_b = _create_connection(db_engine, g["channel"], agent_y, "conn-b")
    _create_session(db_engine, g["identity"], g["channel"],
                    conn_a, agent_x, "conv-1")
    _create_session(db_engine, g["identity"], g["channel"],
                    conn_b, agent_y, "conv-1")

    with _uow_factory(db_engine)() as uow:
        with pytest.raises(MultipleResultsFound):
            uow.sessions.find_active_by_identity_and_conversation(
                g["identity"], "conv-1")


def test_b6_duplicate_same_key_fails_loud_not_silently(db_engine) -> None:
    """Cloud-0.1.1-B：sessions 三维键（+ tenant 前导）已物化为 DB 唯一约束——
    同键重复创建在 DB 层 conflict（fail-loud），绝不静默产生第二条。
    （9.7 快照「重复被允许、靠查询 fail-loud」被隔离演进翻转。）"""
    g = _seed_channel(db_engine)
    agent_x = _create_agent(db_engine, "agent-x")
    conn_a = _create_connection(db_engine, g["channel"], agent_x, "conn-a")
    _create_session(db_engine, g["identity"], g["channel"],
                    conn_a, agent_x, "conv-1")
    # 同键第二条：DB UNIQUE 在 create 时直接 conflict
    with pytest.raises(IntegrityError):
        _create_session(db_engine, g["identity"], g["channel"],
                        conn_a, agent_x, "conv-1")


# ================================================================== C. 契约成文


def test_c1_contract_is_written_down() -> None:
    """决策/验证文档成文（防漂移）：三维键、agent_id 非键、Case A/B/C token。"""
    assert DOC.exists(), f"缺少决策文档：{DOC}"
    text = DOC.read_text(encoding="utf-8")
    for token in ("(identity_id, connection_id, external_conversation_id)",
                  "Connection 是 Session 的 Agent 隔离键",
                  "agent_id 不是 Session 的第二个选择键",
                  "Case A", "Case B", "Case C",
                  "MultipleResultsFound",
                  "切换 Agent = 新 Session",
                  "Persistence Observation",
                  "弱键"):
        assert token in text, f"决策文档缺少 token：{token}"
