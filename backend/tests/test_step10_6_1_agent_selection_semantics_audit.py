"""Step 10.6.1 —— Agent Selection Semantics Audit（审计步，生产代码 0 行改动）。

先不实现多 Agent Routing；把「当 Channel 对应多个 Agent 时 CP 凭什么选择
Agent」的语义现状钉成可回归断言：

A  静态守卫（现状 = 没有独立 Agent Selection）：
   - Routing 只按 channel_id 选 Connection；不消费 identity/session/conversation/content；
   - 无 AgentSelector / Selection / RoutingRule / 命令语法类组件；
   - 一个 Connection = 一个 Agent（UNIQUE + FK + 绑定不可变 + 标量 agent_id）；
   - Session 键含 connection_id（换 Connection = 新会话空间）；Port 签名 = 三维；
   - 无 conversation→agent 一等绑定（8 表冻结不变）；
B  语义探针：
   - Capability Gap 快照：单 Channel 双 ACTIVE Connection → RoutingFailedError；
   - 同 conversation + 同 Connection → REUSE_ACTIVE（会话粘性的事实载体）；
   - 切换 Connection（= 换 Agent）→ CREATE_NEW（旧 Session 保持 active）；
   - Agent 只能经 Connection 派生（result.agent_id = connection.agent_id）；
   - 10.4.2 Wrapper 弱键歧义：同 (identity, conversation) 两条 active session →
     MultipleResultsFound（多 Agent 下的潜在陷阱，today 不可达）；
   - 执行顺序 routing 先于 session_resolution（会话粘性张力的代码坐标）；
C  契约成文。

语义裁决见 docs/step-10.6.1-agent-selection-semantics-audit.md §11 一句话版。
"""

import ast
from dataclasses import fields
from pathlib import Path

import pytest
from sqlalchemy import UniqueConstraint, inspect
from sqlalchemy.exc import IntegrityError, MultipleResultsFound
from sqlalchemy.orm import Session as SASession, sessionmaker
from sqlalchemy import select

from app.application.errors import RoutingFailedError
from app.application.routing import RoutingExecutor
from app.application.session_resolution import CREATE_NEW, REUSE_ACTIVE
from app.application.session_resolution import SessionResolutionExecutor
from app.domain.agent import Agent
from app.domain.channel import Channel
from app.domain.connection import Connection
from app.domain.identity import Identity
from app.domain.session import Session
from app.domain.user import User
from app.persistence.models.connection import ConnectionModel
from app.persistence.models.session import SessionModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"
APPLICATION = APP / "application"
REPO_ROOT = BACKEND.parent
DOC = REPO_ROOT / "docs" / "step-10.6.1-agent-selection-semantics-audit.md"

FROZEN_TABLES = {
    "users", "identities", "channels", "agents",
    "connections", "sessions", "interactions", "activities",
    # Cloud-0.1.1-B：tenants root catalog（非第 9 张业务表；8 张业务表仍冻结不变）
    "tenants",
    # Cloud-0.1.1-C：Browser Auth 2 张表（user_credentials / browser_sessions），
    # 仍不是 conversation→agent 绑定表，业务表集不变。
    "user_credentials", "browser_sessions",
    # Implementation Package 2 / Step 1（ADR-11 显式授权）：授权状态表
    # `connection_conversations` = **Conversation → Connection** 的 allow 关系。
    # 它**不是**本守卫禁止的 conversation→**Agent** 一等绑定：
    # Agent 仍只能由被选中的 Connection 派生（agent_id 是 Routing 的产物），
    # 会话侧没有任何指向 Agent 的绑定。本表是 Package 2-C §1.5 的窄范围例外。
    "connection_conversations",
    # M6.6-C3 / ADR-12：Bootstrap 窗口表。它是「授权**建立**的许可」，
    # 不是 conversation→agent 绑定（Arm 绑定的是 Connection，Agent 仍由
    # Connection 派生）。
    "bootstrap_arms",
}


# ================================================================== 测试底座


def _uow_factory(db_engine):
    return lambda: SqlAlchemyUnitOfWork(
        session_factory=sessionmaker(bind=db_engine, expire_on_commit=False))


def _seed_channel(db_engine, channel_name: str = "ch-1",
                  user_name: str = "alice") -> dict[str, int]:
    """一个 Channel + 一个 User/Identity（identity 的 channel scope 事实）。"""
    with _uow_factory(db_engine)() as uow:
        user = uow.users.create(User(name=user_name))
        channel = uow.channels.create(Channel(name=channel_name, type="wecom"))
        ident = uow.identities.create(Identity(
            user_id=user.id, channel_id=channel.id, external_id="wx_a"))
        uow.commit()
        return {"user": user.id, "channel": channel.id, "identity": ident.id}


def _create_agent(db_engine, name: str) -> int:
    with _uow_factory(db_engine)() as uow:
        agent = uow.agents.create(Agent(name=name, type="acp"))
        uow.commit()
        return agent.id


def _create_connection(db_engine, channel_id: int, agent_id: int,
                       name: str, status: str = "active") -> int:
    with _uow_factory(db_engine)() as uow:
        conn = uow.connections.create(Connection(
            channel_id=channel_id, agent_id=agent_id, name=name,
            status=status,
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


def _session_rows(db_engine) -> list[SessionModel]:
    with SASession(db_engine) as s:
        return list(s.execute(select(SessionModel)
                              .order_by(SessionModel.id)).scalars().all())


def _py_files(root: Path):
    return sorted(p for p in root.rglob("*.py"))


# ================================================================== A. 静态守卫：没有独立 Agent Selection


def test_a1_routing_selects_connection_by_channel_only() -> None:
    """Routing 只按 channel_id 选 Connection：不消费 identity / session /
    conversation / content / metadata / Agent 元数据。

    （只扫 AST 代码标识符，docstring 里的说明文字不算引用。）
    """
    src = (APPLICATION / "routing.py").read_text(encoding="utf-8")
    tree = ast.parse(src, filename="routing.py")
    used: set[str] = set()
    find_active_calls = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
            if node.attr == "find_active_by_channel":
                find_active_calls += 1
    assert find_active_calls == 1            # 唯一仓库调用 = 按 channel 选 Connection
    assert used.isdisjoint({                  # 不消费选择之外的输入
        "external_conversation_id", "external_identity_id", "content",
        "SessionResolution", "IdentityResolution", "metadata",
        "agent_metadata", "routing_rules", "find_active",
        "find_active_by_identity_and_conversation"}), \
        f"Routing 消费了选择之外的输入：{used}"


def test_a2_no_agent_selection_component_or_command_grammar() -> None:
    """全 application/domain/repositories 无选择器类/选择类/RoutingRule/
    命令语法类符号（Agent Selection 不是一个独立概念）。"""
    roots = (APPLICATION, APP / "domain", APP / "repositories")
    forbidden = ("selector", "selection", "routingrule", "routing_policy",
                 "command", "router")
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, (ast.ClassDef, ast.FunctionDef,
                                     ast.AsyncFunctionDef)):
                    lower = node.name.lower()
                    for token in forbidden:
                        assert token not in lower, \
                            f"{path.name} 出现选择类符号：{node.name}"
    # 8.4/10.3.1 冻结回归：仍无 RoutingRule / Resolver / Dispatcher 类
    symbols: set[str] = set()
    for path in _py_files(APP):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                symbols.add(node.name)
    assert not {"RoutingRule", "RoutingResolver", "RoutingPolicy",
                "RoutingTable"} & symbols
    # content 不是控制信号：Runtime 入口契约无 command 字段
    from app.domain.inbound_interaction import InboundInteraction
    inbound_names = {f.name for f in fields(InboundInteraction)}
    assert "command" not in inbound_names
    assert inbound_names == {
        "channel_id", "external_interaction_id", "external_conversation_id",
        "external_identity_id", "content", "metadata"}


def test_a3_one_connection_represents_exactly_one_agent() -> None:
    """Connection = 单个 Agent：UNIQUE(channel_id, agent_id)、agent_id 非空 FK。"""
    uniques = [c for c in ConnectionModel.__table__.constraints
               if isinstance(c, UniqueConstraint)]
    assert any({col.name for col in u.columns}
               == {"tenant_id", "channel_id", "agent_id"}
               for u in uniques)
    cols = {c.name: c for c in inspect(ConnectionModel).columns}
    assert cols["agent_id"].nullable is False
    fk = list(ConnectionModel.__table__.columns["agent_id"].foreign_keys)[0]
    assert fk.target_fullname == "agents.id"


def test_a4_session_key_includes_connection_id() -> None:
    """Session 隔离键含 Connection：换 Connection（= 换 Agent）= 新会话空间。"""
    session_fields = {f.name for f in fields(Session)}
    assert {"connection_id", "agent_id", "identity_id",
            "external_conversation_id"} <= session_fields

    model_cols = {c.name for c in inspect(SessionModel).columns}
    assert {"identity_id", "connection_id", "agent_id",
            "external_conversation_id"} <= model_cols

    # Port 的 find_active 签名 = 8.3 三维隔离键
    port_src = (APP / "repositories" / "session.py").read_text(encoding="utf-8")
    tree = ast.parse(port_src, filename="session.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "find_active":
            arg_names = [a.arg for a in node.args.args]
            assert arg_names == ["self", "identity_id", "connection_id",
                                 "external_conversation_id"], arg_names
            break
    else:  # pragma: no cover - 端口必须存在
        raise AssertionError("SessionRepository.find_active 不存在")


def test_a5_no_conversation_to_agent_first_class_binding() -> None:
    """无 conversation→agent 一等绑定：表集合 = 冻结集合（含 Package 2 的授权表）。

    Package 2 / Step 1：`connection_conversations` 是 Conversation → **Connection**
    的 allow 关系，**不是** conversation→Agent 绑定——因此本红线**继续有效**，
    且现在把新表面积也纳入 token 扫描（新域实体 / Port / 适配器不得出现
    conversation→agent 绑定词）。
    """
    tablenames: set[str] = set()
    for path in _py_files(APP / "persistence" / "models"):
        for node in ast.walk(ast.parse(
                path.read_text(encoding="utf-8"), filename=str(path))):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if (isinstance(target, ast.Name)
                            and target.id == "__tablename__"
                            and isinstance(node.value, ast.Constant)):
                        tablenames.add(node.value.value)
    assert tablenames == FROZEN_TABLES, \
        f"出现冻结之外的表：{tablenames - FROZEN_TABLES}"
    for rel in ("domain/session.py", "domain/connection.py",
                "repositories/session.py", "repositories/connection.py",
                "domain/connection_conversation.py",
                "repositories/connection_conversation.py",
                "persistence/repositories/connection_conversation.py"):
        src = (APP / rel).read_text(encoding="utf-8")
        for token in ("conversation_to_agent", "conversation_agent",
                      "agent_binding", "preferred_agent"):
            assert token not in src, f"{rel} 出现会话→Agent 绑定：{token}"


# ================================================================== B. 语义探针


def test_b1_capability_gap_single_channel_dual_active_fails(db_engine) -> None:
    """Capability Gap 快照（10.3.1）：单 Channel 两条 ACTIVE Connection →
    RoutingFailedError——今天多 Agent 于一个 Channel 没有选择依据，Fail Closed。"""
    g = _seed_channel(db_engine)
    agent_x = _create_agent(db_engine, "agent-x")
    agent_y = _create_agent(db_engine, "agent-y")
    conn_x = _create_connection(db_engine, g["channel"], agent_x, "path-x")
    conn_y = _create_connection(db_engine, g["channel"], agent_y, "path-y")

    # Package 2 / Step 2：两条都给许可——失败必须来自「候选 >1」而非「候选为空」
    with pytest.raises(RoutingFailedError):
        RoutingExecutor(_uow_factory(db_engine)).execute(
            g["channel"], frozenset({conn_x, conn_y}))


def test_b2_same_conversation_same_connection_reuses_session(db_engine) -> None:
    """会话粘性的事实载体：同 (identity, connection, conversation) →
    REUSE_ACTIVE 且复用同一条 session。"""
    g = _seed_channel(db_engine)
    agent_x = _create_agent(db_engine, "agent-x")
    conn_x = _create_connection(db_engine, g["channel"], agent_x, "path-x")
    sid = _create_session(db_engine, g["identity"], g["channel"],
                          conn_x, agent_x, "conv-1")

    result = SessionResolutionExecutor(_uow_factory(db_engine)).execute(
        g["identity"], conn_x, "conv-1")
    assert result.mode == REUSE_ACTIVE
    assert result.existing_session_id == sid


def test_b3_switching_agent_breaks_session_reuse(db_engine) -> None:
    """换 Agent = 换 Connection → CREATE_NEW；旧 Session 保持 active（无切换协议）。"""
    g = _seed_channel(db_engine)
    agent_x = _create_agent(db_engine, "agent-x")
    agent_y = _create_agent(db_engine, "agent-y")
    conn_x = _create_connection(db_engine, g["channel"], agent_x, "path-x")
    conn_y = _create_connection(db_engine, g["channel"], agent_y, "path-y")
    _create_session(db_engine, g["identity"], g["channel"],
                    conn_x, agent_x, "conv-1")

    result = SessionResolutionExecutor(_uow_factory(db_engine)).execute(
        g["identity"], conn_y, "conv-1")
    assert result.mode == CREATE_NEW          # 新 Agent = 新会话空间
    assert result.existing_session_id is None

    rows = _session_rows(db_engine)
    assert len(rows) == 1                     # CREATE_NEW 只是决策，未新建
    assert rows[0].connection_id == conn_x    # 旧会话仍在（无关闭/迁移机制）
    assert rows[0].status == "active"


def test_b4_agent_id_is_derived_from_selected_connection(db_engine) -> None:
    """Agent 只能经 Connection 派生：单一 active Connection 时，
    RoutingResult.agent_id == 该 Connection.agent_id（无独立 agent 选择）。"""
    g_a = _seed_channel(db_engine, channel_name="ch-a", user_name="alice")
    g_b = _seed_channel(db_engine, channel_name="ch-b", user_name="bob")
    agent_x = _create_agent(db_engine, "agent-x")
    agent_y = _create_agent(db_engine, "agent-y")
    conn_x = _create_connection(db_engine, g_a["channel"], agent_x, "path-x")
    conn_y = _create_connection(db_engine, g_b["channel"], agent_y, "path-y")

    routing = RoutingExecutor(_uow_factory(db_engine))
    r_a = routing.execute(g_a["channel"], frozenset({conn_x}))
    r_b = routing.execute(g_b["channel"], frozenset({conn_y}))
    assert r_a.connection_id == conn_x and r_a.agent_id == agent_x
    assert r_b.connection_id == conn_y and r_b.agent_id == agent_y
    # 输出形状：connection_id + agent_id 成对（8.4：不允许「选 C 却派给别的 Agent」）
    assert r_a.agent_id != r_b.agent_id


def test_b5_wrapper_weak_key_is_ambiguous_after_agent_switch(db_engine) -> None:
    """10.4.2 Wrapper 弱键歧义（潜在陷阱）：同 (identity, conversation) 在
    两条 Connection 上各有 active session 时，弱键查询 → MultipleResultsFound。
    （今天不可达：MVP 单 active Connection/Channel；多 Agent 落地时必须先收紧。）"""
    g = _seed_channel(db_engine)
    agent_x = _create_agent(db_engine, "agent-x")
    agent_y = _create_agent(db_engine, "agent-y")
    conn_x = _create_connection(db_engine, g["channel"], agent_x, "path-x")
    conn_y = _create_connection(db_engine, g["channel"], agent_y, "path-y")
    _create_session(db_engine, g["identity"], g["channel"],
                    conn_x, agent_x, "conv-1")
    _create_session(db_engine, g["identity"], g["channel"],
                    conn_y, agent_y, "conv-1")

    with _uow_factory(db_engine)() as uow:
        with pytest.raises(MultipleResultsFound):
            uow.sessions.find_active_by_identity_and_conversation(
                g["identity"], "conv-1")


def test_b6_routing_precedes_session_resolution(db_engine) -> None:
    """执行顺序（9.8.1）：routing 先于 session_resolution——Routing 决策时
    看不到会话事实（会话粘性张力的代码坐标，见审计文档 §4.3/N1）。"""
    from app.application.runtime import RUNTIME_EXECUTION_ORDER
    assert RUNTIME_EXECUTION_ORDER.index("routing") < \
        RUNTIME_EXECUTION_ORDER.index("session_resolution")


# ================================================================== C. 契约成文


def test_c1_contract_is_written_down() -> None:
    """审计文档成文（防漂移）：关键裁决 token 都在文档里。"""
    assert DOC.exists(), f"缺少审计文档：{DOC}"
    text = DOC.read_text(encoding="utf-8")
    for token in ("Capability Gap",
                  "agent 只经 Connection 可达",
                  "不存在独立于 Routing 的「Agent Selection」",
                  "会话粘性",
                  "必然新 Session",
                  "Connection Selection 的别名",
                  "只能代表一个 Agent",
                  "conversation→agent",
                  "MultipleResultsFound"):
        assert token in text, f"审计文档缺少 token：{token}"
