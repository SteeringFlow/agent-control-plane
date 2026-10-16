"""Step 9.3 —— Session Resolution Execution（8.3 契约的第一次真实执行）。

9.3 是第一个真正依赖 Connection 的 Runtime stage，但 **Connection 只消费、不选
择**（8.4 Routing 的产物 → 9.3 只把它当输入）：

    (identity_id, connection_id, external_conversation_id)
        ↓ SessionRepository.find_active（9.3 新增端口）
        ↓ hit  → REUSE_ACTIVE（existing_session_id = 该 active session 的 id）
        ↓ miss → CREATE_NEW（existing_session_id = None）

**CREATE_NEW 只是决策结果，不是立即 create**：9.3 不做 Session Persistence
（否则 Executor 会同时拥有 Resolution / Persistence mutation / Session lifecycle
三个职责）。

锁定项：
A  成功路径：REUSE_ACTIVE（existing_session_id 与 mode 一致）；
B  miss → CREATE_NEW（existing_session_id = None）；
C  三维隔离：(identity_id, connection_id, external_conversation_id) 四个组合
   各自独立——尤其 conversation 不同不得复用同一 Session；
D  使用 Identity.id（Channel 边界），不是 User.id；
E  connection_id 只消费：不 import ConnectionRepository、不查 Connection；
F  不创建 Session（CREATE_NEW 之后库内计数不变，源码无 create/update 调用）；
G  不修改 SessionRepository 方法面（只有 find_active）；
H  Fail Closed：非法输入 → SessionResolutionFailedError（STOP）；
I  Execution ≠ Resolver：无 SessionResolver/SessionService/SessionManager/…；
J  mutation guard。
"""

import ast
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.application import (
    SessionResolutionExecutor,
    SessionResolutionFailedError,
)
from app.domain.agent import Agent
from app.domain.channel import Channel
from app.domain.connection import Connection
from app.domain.identity import Identity
from app.domain.session_resolution import CREATE_NEW, REUSE_ACTIVE, SessionResolution
from app.domain.user import User
from app.persistence.models.session import SessionModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

APP_DIR = Path(__file__).resolve().parents[1] / "app"
BACKEND_DIR = Path(__file__).resolve().parents[1]
EXECUTOR_PY = APP_DIR / "application" / "session_resolution.py"

FORBIDDEN_ROOTS = (
    "sqlalchemy", "fastapi", "starlette", "pydantic",
    "app.persistence", "app.adapters", "app.api", "app.schemas",
)


def _root_allowed(root: str) -> bool:
    return (root in {"collections", "dataclasses", "typing"}
            or root.startswith("collections.")
            or root.startswith("typing.")
            or root == "app.domain" or root.startswith("app.domain.")
            or root == "app.repositories" or root.startswith("app.repositories.")
            or root == "app.application.errors")


def _factory(db_engine) -> sessionmaker:
    return sessionmaker(bind=db_engine, class_=Session, expire_on_commit=False)


def _executor(db_engine) -> SessionResolutionExecutor:
    return SessionResolutionExecutor(
        uow_factory=lambda: SqlAlchemyUnitOfWork(session_factory=_factory(db_engine))
    )


def _graph(db_engine) -> dict[str, int]:
    """搭出可落 Session 的最小关系图：2 Identity(同 channel) × 2 Connection。"""
    with SqlAlchemyUnitOfWork(session_factory=_factory(db_engine)) as uow:
        u1 = uow.users.create(User(name="alice"))
        u2 = uow.users.create(User(name="bob"))
        channel = uow.channels.create(Channel(name="wecom-1", type="wecom"))
        a1 = uow.agents.create(Agent(name="agent-1", type="acp"))
        a2 = uow.agents.create(Agent(name="agent-2", type="acp"))
        i1 = uow.identities.create(Identity(
            user_id=u1.id, channel_id=channel.id, external_id="wx_1"))
        i2 = uow.identities.create(Identity(
            user_id=u2.id, channel_id=channel.id, external_id="wx_2"))
        c1 = uow.connections.create(Connection(
            channel_id=channel.id, agent_id=a1.id, name="path-1"))
        c2 = uow.connections.create(Connection(
            channel_id=channel.id, agent_id=a2.id, name="path-2"))
        uow.commit()
        return {
            "user1": u1.id, "user2": u2.id, "channel": channel.id,
            "identity1": i1.id, "identity2": i2.id,
            "connection1": c1.id, "connection2": c2.id,
        }


def _insert_session(db_engine, *, identity_id: int, channel_id: int,
                    connection_id: int, conversation: str,
                    status: str = "active") -> int:
    """直接落一行 SessionModel（9.3 的 Port 没有 create——会话创建不属本 Slice）。"""
    with Session(db_engine) as session:
        row = SessionModel(
            identity_id=identity_id,
            channel_id=channel_id,
            connection_id=connection_id,
            external_conversation_id=conversation,
            status=status,
        )
        session.add(row)
        session.commit()
        return row.id


def _count_sessions(db_engine) -> int:
    with Session(db_engine) as session:
        return session.execute(
            select(func.count()).select_from(SessionModel)
        ).scalar_one()


# ------------------------------------------------------------------ AST helpers


def _roots(src: str) -> set[str]:
    roots: set[str] = set()
    tree = ast.parse(src, filename=str(EXECUTOR_PY))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module)
    return roots


def _classes(src: str) -> set[str]:
    return {n.name for n in ast.walk(ast.parse(src, filename=str(EXECUTOR_PY)))
            if isinstance(n, ast.ClassDef)}


def _boundary_problems(src: str) -> list[str]:
    problems: list[str] = []
    for root in sorted(_roots(src)):
        if not _root_allowed(root):
            problems.append(f"越层 import {root}")
        if root.startswith("app.repositories.connection"):
            problems.append("不应 import ConnectionRepository（Connection 只消费）")
    tree = ast.parse(src, filename=str(EXECUTOR_PY))
    forbidden_names = {"SqlAlchemyUnitOfWork", "Session", "sessionmaker",
                       "create_engine", "Engine", "SessionModel", "HTTPException",
                       "APIRouter", "ConnectionRepository"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in forbidden_names:
            problems.append(f"出现基础设施标识符 {node.id}")
    write_calls = {
        node.func.attr for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"create", "update", "delete"}
    }
    if write_calls:
        problems.append(f"Execution 不应写：{write_calls}")
    extra = _classes(src) - {"SessionResolutionExecutor"}
    if extra:
        problems.append(f"出现非 Execution 组件类：{sorted(extra)}")
    return problems


# ================================================================== A/B 成功路径 + 决策


def test_a_existing_session_is_reused(db_engine) -> None:
    g = _graph(db_engine)
    sid = _insert_session(db_engine, identity_id=g["identity1"], channel_id=g["channel"],
                          connection_id=g["connection1"], conversation="X")
    result = _executor(db_engine).execute(
        g["identity1"], g["connection1"], "X")

    assert isinstance(result, SessionResolution)
    assert result.mode == REUSE_ACTIVE
    assert result.existing_session_id == sid
    assert result.identity_id == g["identity1"]
    assert result.connection_id == g["connection1"]
    assert result.external_conversation_id == "X"


def test_b_missing_session_decides_create_new(db_engine) -> None:
    g = _graph(db_engine)
    result = _executor(db_engine).execute(
        g["identity1"], g["connection1"], "X")

    assert result.mode == CREATE_NEW
    assert result.existing_session_id is None
    assert result.external_conversation_id == "X"


def test_b_mode_and_session_id_are_consistent(db_engine) -> None:
    """mode 与 existing_session_id 一一对应（不允许 REUSE 无 id / CREATE 带 id）。"""
    g = _graph(db_engine)
    _insert_session(db_engine, identity_id=g["identity1"], channel_id=g["channel"],
                    connection_id=g["connection1"], conversation="X")
    executor = _executor(db_engine)
    reused = executor.execute(g["identity1"], g["connection1"], "X")
    fresh = executor.execute(g["identity1"], g["connection1"], "Y")
    assert (reused.mode == REUSE_ACTIVE) == (reused.existing_session_id is not None)
    assert (fresh.mode == CREATE_NEW) == (fresh.existing_session_id is None)


# ================================================================== C 三维隔离


def test_c_four_independent_resolution_spaces(db_engine) -> None:
    """(identity, connection, conversation) 四维组合各自独立：

    i1+c1+X / i1+c1+Y / i1+c2+X / i2+c1+X 是四条独立的 Session 空间。
    """
    g = _graph(db_engine)
    ids = {
        "i1_c1_X": _insert_session(db_engine, identity_id=g["identity1"],
                                   channel_id=g["channel"],
                                   connection_id=g["connection1"], conversation="X"),
        "i1_c1_Y": _insert_session(db_engine, identity_id=g["identity1"],
                                   channel_id=g["channel"],
                                   connection_id=g["connection1"], conversation="Y"),
        "i1_c2_X": _insert_session(db_engine, identity_id=g["identity1"],
                                   channel_id=g["channel"],
                                   connection_id=g["connection2"], conversation="X"),
        "i2_c1_X": _insert_session(db_engine, identity_id=g["identity2"],
                                   channel_id=g["channel"],
                                   connection_id=g["connection1"], conversation="X"),
    }
    executor = _executor(db_engine)
    assert executor.execute(g["identity1"], g["connection1"], "X").existing_session_id == ids["i1_c1_X"]
    assert executor.execute(g["identity1"], g["connection1"], "Y").existing_session_id == ids["i1_c1_Y"]
    assert executor.execute(g["identity1"], g["connection2"], "X").existing_session_id == ids["i1_c2_X"]
    assert executor.execute(g["identity2"], g["connection1"], "X").existing_session_id == ids["i2_c1_X"]
    assert len(set(ids.values())) == 4


def test_c_no_cross_conversation_reuse(db_engine) -> None:
    """Identity 相同但 conversation 不同 → 绝不复用（防群聊/会话上下文泄漏）。"""
    g = _graph(db_engine)
    _insert_session(db_engine, identity_id=g["identity1"], channel_id=g["channel"],
                    connection_id=g["connection1"], conversation="conversation-X")
    result = _executor(db_engine).execute(
        g["identity1"], g["connection1"], "conversation-Y")
    assert result.mode == CREATE_NEW
    assert result.existing_session_id is None


def test_c_inactive_session_is_not_reused(db_engine) -> None:
    """只复用 active session：非 active 行不算命中。"""
    g = _graph(db_engine)
    _insert_session(db_engine, identity_id=g["identity1"], channel_id=g["channel"],
                    connection_id=g["connection1"], conversation="X", status="closed")
    result = _executor(db_engine).execute(g["identity1"], g["connection1"], "X")
    assert result.mode == CREATE_NEW


# ================================================================== D Identity.id（不是 User.id）


def test_d_identity_id_not_user_id(db_engine) -> None:
    """解析键是 Identity.id：同一 User 的两个 Identity 是两条会话空间。"""
    g = _graph(db_engine)
    # identity2 属于 user2（与 identity1 不同 User），两条空间互不干扰
    _insert_session(db_engine, identity_id=g["identity2"], channel_id=g["channel"],
                    connection_id=g["connection1"], conversation="X")
    result = _executor(db_engine).execute(g["identity1"], g["connection1"], "X")
    assert result.mode == CREATE_NEW
    # user_id 不参与查找（sessions 表无 user_id 列，8.3 已冻结）
    assert "user_id" not in {c.name for c in SessionModel.__table__.columns}


# ================================================================== E/F 不选择 Connection / 不创建


def test_e_connection_is_consumed_not_selected() -> None:
    """只消费 connection_id：不 import ConnectionRepository、不查 Connection。"""
    src = EXECUTOR_PY.read_text(encoding="utf-8")
    roots = _roots(src)
    assert not any(r.startswith("app.repositories.connection") for r in roots)
    assert not _boundary_problems(src), _boundary_problems(src)


def test_f_create_new_does_not_persist_session(db_engine) -> None:
    """CREATE_NEW 只是决策：库内不产生任何新 session。"""
    g = _graph(db_engine)
    assert _count_sessions(db_engine) == 0
    _executor(db_engine).execute(g["identity1"], g["connection1"], "X")
    assert _count_sessions(db_engine) == 0


def test_f_executor_source_has_no_writes() -> None:
    src = EXECUTOR_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    read_calls = {
        node.func.attr for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Attribute)
    }
    assert read_calls == {"find_active"}, read_calls


# ================================================================== G Repository surface


def test_g_session_repository_surface_is_minimal() -> None:
    """SessionRepository（Port + Adapter）方法面最小化：find_active + create
    + find_active_by_identity_and_conversation（10.4.2 Interaction Wrapper
    最小读路径，有记录的近似——见 Port docstring）。"""
    import app.persistence.repositories.session as adapter
    import app.repositories.session as port

    def _methods(module) -> set[str]:
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        return {n.name for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name != "__init__"}

    # 10.8.4: + bind_external_session（会话映射写入点，非通用 update）
    # Package 2 / Step 3: + list_known_conversation_ids（B4.5 Discovery 投影，
    # **projection only**——known conversation ≠ authorized conversation）
    # M6.6-C4 / CP4-1: + find_or_create（并发唯一冲突 → 回滚 → 精确四元组回读复用）
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


def test_g_find_active_key_is_three_dims() -> None:
    """find_active 参数 = 三维隔离键，缺一不可（Port 与 Adapter 一致）。"""
    for rel in ("app/repositories/session.py", "app/persistence/repositories/session.py"):
        tree = ast.parse((BACKEND_DIR / rel).read_text(encoding="utf-8"))
        node = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "find_active")
        args = [a.arg for a in node.args.args if a.arg != "self"]
        assert args == ["identity_id", "connection_id", "external_conversation_id"], \
            f"{rel}::find_active 参数漂移：{args}"


# ================================================================== H Fail Closed


@pytest.mark.parametrize("identity_id,connection_id,conversation", [
    (0, 1, "X"), (-1, 1, "X"), ("1", 1, "X"),
    (1, 0, "X"), (1, -2, "X"),
    (1, 1, ""), (1, 1, "   "), (1, 1, None),
])
def test_h_invalid_input_fails_closed(db_engine, identity_id, connection_id,
                                      conversation) -> None:
    g = _graph(db_engine)
    ident = g["identity1"] if identity_id == 1 else identity_id
    conn = g["connection1"] if connection_id == 1 else connection_id
    before = _count_sessions(db_engine)
    with pytest.raises(SessionResolutionFailedError):
        _executor(db_engine).execute(ident, conn, conversation)
    assert _count_sessions(db_engine) == before


# ================================================================== I Execution ≠ Resolver


def test_i_executor_is_not_a_resolver() -> None:
    src = EXECUTOR_PY.read_text(encoding="utf-8")
    assert _classes(src) == {"SessionResolutionExecutor"}, _classes(src)
    for banned in ("SessionResolver", "SessionService", "SessionManager",
                   "SessionPolicy", "SessionLifecycleManager"):
        assert f"class {banned}" not in src, f"不应出现 {banned}"


# ================================================================== J mutation 探测


@pytest.mark.parametrize("inject", [
    "import sqlalchemy",
    "from app.persistence.models.session import SessionModel",
    "from app.repositories.connection import ConnectionRepository",
])
def test_j_forbidden_import_is_caught(inject: str) -> None:
    src = EXECUTOR_PY.read_text(encoding="utf-8") + "\n" + inject + "\n"
    assert _boundary_problems(src), f"边界守卫未抓到 {inject}"


def test_j_write_call_is_caught() -> None:
    src = EXECUTOR_PY.read_text(encoding="utf-8").replace(
        "session = uow.sessions.find_active(",
        "session = uow.sessions.create(",
    )
    assert any("不应写" in p for p in _boundary_problems(src)), "写调用未被守卫抓住"


def test_j_session_resolver_injection_caught_by_reverse_audit() -> None:
    """真实文件注入 SessionResolver → 7.3 反向审计必须报红（Execution ≠ Resolver）。"""
    backup = EXECUTOR_PY.read_text(encoding="utf-8")
    try:
        EXECUTOR_PY.write_text(
            backup + "\n\nclass SessionResolver:\n    pass\n", encoding="utf-8")
        audit_py = BACKEND_DIR / "tests" / "test_step7_3_connection_management_audit.py"
        ns: dict = {"__file__": str(audit_py), "__name__": "audit_under_9_3"}
        exec(compile(audit_py.read_text(encoding="utf-8"), str(audit_py), "exec"), ns)
        problems = ns["_next_stage_problems"]()
        assert any("SessionResolver" in p for p in problems), \
            f"反向审计未抓到 SessionResolver 偷渡：{problems}"
        assert _boundary_problems(EXECUTOR_PY.read_text(encoding="utf-8")), \
            "模块类面守卫未抓到偷渡"
    finally:
        EXECUTOR_PY.write_text(backup, encoding="utf-8")
