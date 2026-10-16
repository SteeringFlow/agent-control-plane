"""Step 9.7 —— Session Creation Execution & Runtime State Materialization。

把 9.3 的 `CREATE_NEW` 从**决策**变成真正的 Session Persistence：当 Resolution
给出 CREATE_NEW 时，Control Plane 能产生一个真实的 CP Session，并把它的 id 提供
给后续 Dispatch（→ AgentRequest.control_plane_session_id）。

    CREATE_NEW（existing_session_id = None）
        ↓ SessionRepository.create（9.7 最小写路径）
        ↓ 真实 Session（id → control_plane_session_id）
        ↓ Routing → Dispatch

锁定项：
A  create 返回带 id 的真实 Session（落库）；随后 find_active 命中（REUSE_ACTIVE）；
B  agent_id 来自 RoutingResult.agent_id（已选定的 Connection），不是自己找 Agent；
   channel_id / agent_id 是输入，不重新 Routing / 解析；
C  CREATE_NEW → CP Session id → AgentRequest.control_plane_session_id（链条闭环）；
D  Repository / Mapper 只补最小写路径：find_active + create + find_or_create（CP4-1）、
   to_domain + to_orm；
E  Fail Closed：非法输入 → SessionResolutionFailedError（决议无法落地 = STOP）；
F  evidence-first 已翻转：sessions 表已有 (tenant, identity, connection, conversation)
   唯一约束（0002）；并发冲突由 find_or_create 精确回读复用收敛（CP4-1），非 failure；
G  不创建 SessionManager / SessionService / SessionFactory / ConversationManager；
H  mutation guard。
"""

import ast
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.application import (
    RoutingExecutor,
    SessionCreationExecutor,
    SessionResolutionExecutor,
    SessionResolutionFailedError,
)
from app.domain.agent import Agent
from app.domain.agent_request import AgentRequest
from app.domain.channel import Channel
from app.domain.connection import Connection
from app.domain.identity import Identity
from app.domain.session import Session as DomainSession
from app.domain.session_resolution import CREATE_NEW, REUSE_ACTIVE
from app.domain.user import User
from app.persistence.models.session import SessionModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

APP_DIR = Path(__file__).resolve().parents[1] / "app"
BACKEND_DIR = Path(__file__).resolve().parents[1]
CREATION_PY = APP_DIR / "application" / "session_creation.py"


def _root_allowed(root: str) -> bool:
    return (root in {"collections", "dataclasses", "typing"}
            or root.startswith("collections.")
            or root.startswith("typing.")
            or root == "app.domain" or root.startswith("app.domain.")
            or root == "app.repositories" or root.startswith("app.repositories.")
            or root == "app.application.errors")


class _TestAgentAdapter:
    def __init__(self) -> None:
        self.requests: list[AgentRequest] = []

    def send(self, request: AgentRequest, connection: Connection):
        from app.domain.agent_response import AgentResponse
        self.requests.append(request)
        return AgentResponse(content="ok")


def _factory(db_engine) -> sessionmaker:
    return sessionmaker(bind=db_engine, class_=Session, expire_on_commit=False)


def _uow_factory(db_engine):
    return lambda: SqlAlchemyUnitOfWork(session_factory=_factory(db_engine))


def _graph(db_engine) -> dict[str, int]:
    """1 Identity(在 ch1) + 1 条 active Connection(ch1→agent1) + 1 条备用 Connection。"""
    with SqlAlchemyUnitOfWork(session_factory=_factory(db_engine)) as uow:
        user = uow.users.create(User(name="alice"))
        channel = uow.channels.create(Channel(name="wecom-1", type="wecom"))
        a1 = uow.agents.create(Agent(name="agent-1", type="acp"))
        a2 = uow.agents.create(Agent(name="agent-2", type="acp"))
        identity = uow.identities.create(Identity(
            user_id=user.id, channel_id=channel.id, external_id="wx_1"))
        conn = uow.connections.create(Connection(
            channel_id=channel.id, agent_id=a1.id, name="path-1"))
        uow.connections.create(Connection(
            channel_id=channel.id, agent_id=a2.id, name="path-2",
            status="inactive"))
        uow.commit()
        return {"user": user.id, "channel": channel.id, "identity": identity.id,
                "agent1": a1.id, "connection": conn.id}


def _count_sessions(db_engine) -> int:
    from sqlalchemy import func, select
    with Session(db_engine) as session:
        return session.execute(
            select(func.count()).select_from(SessionModel)
        ).scalar_one()


# ------------------------------------------------------------------ AST helpers


def _roots(src: str) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(src, filename=str(CREATION_PY))):
        if isinstance(node, ast.Import):
            roots.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module)
    return roots


def _classes(src: str) -> set[str]:
    return {n.name for n in ast.walk(ast.parse(src, filename=str(CREATION_PY)))
            if isinstance(n, ast.ClassDef)}


def _boundary_problems(src: str) -> list[str]:
    problems: list[str] = []
    for root in sorted(_roots(src)):
        if not _root_allowed(root):
            problems.append(f"越层 import {root}")
        if any(root.startswith(p) for p in (
                "app.repositories.connection", "app.repositories.agent",
                "app.repositories.identity", "app.repositories.user")):
            problems.append(f"不应查询 {root}（不重新 Routing/找 Agent）")
    tree = ast.parse(src, filename=str(CREATION_PY))
    # 注意：Session（领域实体）是合法引用；SQLAlchemy 泄漏靠 import 面捕获
    forbidden_names = {"SqlAlchemyUnitOfWork", "sessionmaker", "create_engine",
                       "Engine", "SessionModel", "ConnectionModel",
                       "HTTPException", "APIRouter", "ConnectionRepository",
                       "AgentRepository", "RoutingExecutor"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in forbidden_names:
            problems.append(f"出现基础设施标识符 {node.id}")
    reselect_calls = {
        node.func.attr for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"find_active_by_channel", "find_by_channel_and_agent",
                               "find_by_channel_and_external_id", "get_by_id"}
    }
    if reselect_calls:
        problems.append(f"不应再次选路/解析：{reselect_calls}")
    extra = _classes(src) - {"SessionCreationExecutor"}
    if extra:
        problems.append(f"出现非 Execution 组件类：{sorted(extra)}")
    return problems


# ================================================================== A 物化


def test_a_create_materializes_real_session(db_engine) -> None:
    g = _graph(db_engine)
    before = _count_sessions(db_engine)
    created = SessionCreationExecutor(_uow_factory(db_engine)).execute(
        identity_id=g["identity"], channel_id=g["channel"],
        connection_id=g["connection"], agent_id=g["agent1"],
        external_conversation_id="conversation-X")

    assert isinstance(created, DomainSession)
    assert created.id is not None, "create 应返回带 id 的真实 Session"
    assert _count_sessions(db_engine) == before + 1
    assert created.identity_id == g["identity"]
    assert created.connection_id == g["connection"]
    assert created.agent_id == g["agent1"]
    assert created.external_conversation_id == "conversation-X"
    assert created.channel_id == g["channel"]
    assert created.status == "active"


def test_a_created_session_is_reusable_by_resolution(db_engine) -> None:
    """物化后：下一次 Resolution 的 find_active 命中 → REUSE_ACTIVE。"""
    g = _graph(db_engine)
    created = SessionCreationExecutor(_uow_factory(db_engine)).execute(
        identity_id=g["identity"], channel_id=g["channel"],
        connection_id=g["connection"], agent_id=g["agent1"],
        external_conversation_id="conversation-X")
    decision = SessionResolutionExecutor(_uow_factory(db_engine)).execute(
        g["identity"], g["connection"], "conversation-X")
    assert decision.mode == REUSE_ACTIVE
    assert decision.existing_session_id == created.id


# ================================================================== B agent_id 来源


def test_b_agent_id_matches_routing_result(db_engine) -> None:
    g = _graph(db_engine)
    routing_result = RoutingExecutor(_uow_factory(db_engine)).execute(
        g["channel"], frozenset({g["connection"]}))
    created = SessionCreationExecutor(_uow_factory(db_engine)).execute(
        identity_id=g["identity"], channel_id=g["channel"],
        connection_id=routing_result.connection_id,
        agent_id=routing_result.agent_id,
        external_conversation_id="X")
    # Session.agent_id 必须来自 RoutingResult.agent_id（已选定的 Connection）
    assert created.agent_id == routing_result.agent_id == g["agent1"]


def test_b_creation_does_not_look_up_agent_or_connection() -> None:
    """agent_id/channel_id 是输入：不 import Connection/Agent 仓储、不再选路。"""
    src = CREATION_PY.read_text(encoding="utf-8")
    assert not _boundary_problems(src), _boundary_problems(src)
    roots = _roots(src)
    for r in roots:
        if r.startswith("app.repositories") and not r.startswith("app.repositories.unit"):
            pytest.fail(f"Session Creation 不应依赖 {r}")


# ================================================================== C 链条闭环


def test_c_create_new_materializes_id_for_dispatch(db_engine) -> None:
    """CREATE_NEW → 真实 CP Session id → AgentRequest.control_plane_session_id。"""
    g = _graph(db_engine)
    # 9.3 Resolution 先给出 CREATE_NEW（尚无 session）
    decision = SessionResolutionExecutor(_uow_factory(db_engine)).execute(
        g["identity"], g["connection"], "conversation-X")
    assert decision.mode == CREATE_NEW
    # 9.4 Routing 决定 Connection
    routing_result = RoutingExecutor(_uow_factory(db_engine)).execute(
        g["channel"], frozenset({g["connection"]}))
    # 9.7 物化 Session（channel_id 来自已确定的对象 = graph.channel）
    created = SessionCreationExecutor(_uow_factory(db_engine)).execute(
        identity_id=g["identity"], channel_id=g["channel"],
        connection_id=routing_result.connection_id,
        agent_id=routing_result.agent_id,
        external_conversation_id="conversation-X")
    # 9.5 Dispatch 以 created.id 作为 control_plane_session_id
    from app.application.agent_dispatch import AgentDispatchExecutor
    from app.domain.routing_result import RoutingResult

    adapter = _TestAgentAdapter()
    AgentDispatchExecutor(agent_adapter=adapter, uow_factory=_uow_factory(db_engine))\
        .execute(
            RoutingResult(connection_id=routing_result.connection_id,
                          agent_id=routing_result.agent_id),
            control_plane_session_id=created.id,
            content="hello")
    assert adapter.requests[-1].control_plane_session_id == created.id
    assert isinstance(created.id, int)


# ================================================================== D Repository/Mapper 最小写路径


def test_d_session_repository_surface_is_minimal_write_path() -> None:
    import app.persistence.repositories.session as adapter
    import app.repositories.session as port

    def _methods(module) -> set[str]:
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        return {n.name for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name != "__init__"}

    # 10.4.2: + find_active_by_identity_and_conversation（Interaction Wrapper
    # 最小读路径，有记录的近似——见 SessionRepository Port docstring）
    # 10.8.4: + bind_external_session（会话映射写入点，非通用 update）
    # M6.6-C4 / CP4-1: + find_or_create（唯一约束冲突 → 回滚嵌套事务 → 精确四元组回读复用）
    assert _methods(port) == {
        "find_active", "create", "find_or_create",
        "find_active_by_identity_and_conversation",
        "bind_external_session", "list_known_conversation_ids",
    }, _methods(port)
    assert _methods(adapter) == {
        "find_active", "create", "find_or_create",
        "find_active_by_identity_and_conversation",
        "bind_external_session", "list_known_conversation_ids",
    }, _methods(adapter)


def test_d_session_mapper_has_to_domain_and_to_orm() -> None:
    import app.persistence.mappers.session as mapper

    src = Path(mapper.__file__).read_text(encoding="utf-8")
    assert "def to_domain" in src
    assert "def to_orm" in src
    assert "def apply_to_orm" not in src, "update 属后续 Slice"


# ================================================================== E Fail Closed


@pytest.mark.parametrize("kwargs", [
    {"identity_id": 0, "channel_id": 1, "connection_id": 1, "agent_id": 1,
     "external_conversation_id": "X"},
    {"identity_id": 1, "channel_id": -1, "connection_id": 1, "agent_id": 1,
     "external_conversation_id": "X"},
    {"identity_id": "1", "channel_id": 1, "connection_id": 1, "agent_id": 1,
     "external_conversation_id": "X"},
    {"identity_id": 1, "channel_id": 1, "connection_id": 1, "agent_id": 1,
     "external_conversation_id": ""},
    {"identity_id": 1, "channel_id": 1, "connection_id": 1, "agent_id": 1,
     "external_conversation_id": "X", "external_session_id": 7},
    {"identity_id": 1, "channel_id": 1, "connection_id": 1, "agent_id": 1,
     "external_conversation_id": "X", "metadata": "nope"},
])
def test_e_invalid_input_fails_closed(db_engine, kwargs) -> None:
    before = _count_sessions(db_engine)
    with pytest.raises(SessionResolutionFailedError):
        SessionCreationExecutor(_uow_factory(db_engine)).execute(**kwargs)
    assert _count_sessions(db_engine) == before


# ================================================================== F evidence-first：无唯一约束


def test_f_sessions_unique_constraint_materialized() -> None:
    """Cloud-0.1.1-B：sessions 三维隔离键（+ tenant 前导）已物化为 DB 唯一约束。
    （9.7 的 evidence-first「无唯一约束」快照由隔离演进翻转。）"""
    import sqlalchemy as sa
    from sqlalchemy import inspect

    engine = sa.create_engine("sqlite:///:memory:")
    from app.persistence import models  # noqa: F401
    from app.persistence.database import Base

    Base.metadata.create_all(engine)
    inspector = inspect(engine)
    uq = {tuple(sorted(c["column_names"])) for c in inspector.get_unique_constraints("sessions")}
    assert ("connection_id", "external_conversation_id", "identity_id", "tenant_id") in uq, \
        f"sessions 表缺少租户前导三维唯一约束：{uq}"


# ================================================================== G 组件纪律


def test_g_no_manager_or_factory_classes() -> None:
    src = CREATION_PY.read_text(encoding="utf-8")
    assert _classes(src) == {"SessionCreationExecutor"}, _classes(src)
    for banned in ("SessionManager", "SessionLifecycleService", "SessionService",
                   "SessionFactory", "ConversationManager"):
        assert f"class {banned}" not in src, f"不应出现 {banned}"


# ================================================================== H mutation 探测


@pytest.mark.parametrize("inject", [
    "import sqlalchemy",
    "from app.persistence.models.session import SessionModel",
    "from app.repositories.connection import ConnectionRepository",
    "from app.application.routing import RoutingExecutor",
])
def test_h_forbidden_import_is_caught(inject: str) -> None:
    src = CREATION_PY.read_text(encoding="utf-8") + "\n" + inject + "\n"
    assert _boundary_problems(src), f"边界守卫未抓到 {inject}"


def test_h_reselect_call_is_caught() -> None:
    src = CREATION_PY.read_text(encoding="utf-8").replace(
        "created = uow.sessions.find_or_create(session)",
        "created = uow.connections.find_active_by_channel(1)",
    )
    assert any("再次选路" in p for p in _boundary_problems(src)), "选路调用未被守卫抓住"
