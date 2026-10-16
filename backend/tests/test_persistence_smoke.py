"""Step 5.6 —— Persistence Smoke Test（Integration / Smoke，不加架构层）。

把 5.1~5.5 的组件连成一条接近真实应用用法的完整链路验证一次：

    Settings
       ↓
    Engine（真实 file DB，惰性建连）
       ↓
    Session / UoW（默认工厂，与未来 Application 用法一致）
       ↓
    Repository Port / Adapter
       ↓
    Mapper
       ↓
    ORM Model
       ↓
    SQLite

关键纪律：
- 本文件不新增业务代码：新 Repository / Service / Runtime / API /
  Adapter / Routing / Resolver 一律不做；
- 不为了验证 FK 去实现 Connection Repository（5.3 已验 schema FK）；
- 不创建 PersistenceService / create_initial_data() 之类包装；
- /health 自 14.2.3.B 起 = 存活 + 最小 readiness（一次 SELECT 1），
  不再是纯 Process Liveness。
"""

import ast
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.domain.agent import Agent
from app.domain.channel import Channel
from app.domain.user import User
from app.persistence.database import Base, create_database_engine
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

BACKEND_DIR = Path(__file__).resolve().parents[1]

# M7-AUI-9：测试与开发库分离。
# 此前本文件的 fixture 会直接删除开发库（Settings 默认路径下的 SQLite 文件）——
# 本地每跑一次全量测试，开发数据就被清空一次。现在所有冒烟测试都使用独立
# 文件库（pytest tmp_path）；开发库路径本身仍由 Settings 默认值冻结
# （test_step14_2_3_c2 守卫），本文件对它零读、零写、零删除。


def _session_factory(engine) -> sessionmaker:
    """与 app.persistence.database.SessionLocal 同构的 Session 工厂。"""
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


@pytest.fixture()
def real_file_db(tmp_path):
    """真实基础设施文件库（独立于开发库）：无 schema 自动建表（应用不在 startup create_all）。

    schema 由测试 harness 显式 create_all（与应用启动逻辑无关）——
    这正是 5.6.7 要证明的：我们没有在 startup 偷偷建表。
    """
    test_db_path = tmp_path / "persistence_smoke.db"
    test_engine = create_database_engine(f"sqlite:///{test_db_path}")
    Base.metadata.create_all(test_engine)
    yield test_db_path, test_engine
    test_engine.dispose()


# ------------------------------------------------ A/E: 应用式 round-trip（真实默认链）


def test_app_style_uow_round_trip_on_real_db(real_file_db) -> None:
    """模拟未来 Application Service 的实际用法（UoW 绑定真实文件库）。"""
    _, test_engine = real_file_db
    session_factory = _session_factory(test_engine)

    with SqlAlchemyUnitOfWork(session_factory=session_factory) as uow:
        user = uow.users.create(User(name="zhang-san"))
        channel = uow.channels.create(Channel(name="wecom", type="wecom"))
        agent = uow.agents.create(Agent(name="helper", type="acp", endpoint="https://agent.example"))
        uow.commit()

    # 新的 UoW 重新读取 —— Persistence -> Domain round-trip
    with SqlAlchemyUnitOfWork(session_factory=session_factory) as uow:
        fetched_user = uow.users.get_by_id(user.id)
        fetched_channel = uow.channels.get_by_id(channel.id)
        fetched_agent = uow.agents.get_by_id(agent.id)

    assert fetched_user == user
    assert fetched_channel == channel
    assert fetched_agent == agent
    assert fetched_user.name == "zhang-san"
    assert fetched_agent.endpoint == "https://agent.example"


# ------------------------------------------------ B: Rollback（真实链）


def test_rollback_on_real_db(real_file_db) -> None:
    _, test_engine = real_file_db
    session_factory = _session_factory(test_engine)

    with SqlAlchemyUnitOfWork(session_factory=session_factory) as uow:
        created_user = uow.users.create(User(name="temp-user"))
        uow.channels.create(Channel(name="temp-channel", type="wecom"))
        uow.agents.create(Agent(name="temp-agent", type="acp"))
        uow.rollback()

    with SqlAlchemyUnitOfWork(session_factory=session_factory) as uow:
        assert uow.users.get_by_id(created_user.id) is None
        # 三个 Repository 在同一事务边界，一起被回滚
        from app.persistence.models.agent import AgentModel
        from app.persistence.models.channel import ChannelModel
        from app.persistence.models.user import UserModel
        from sqlalchemy import func, select

        session = uow.session
        assert session.execute(select(func.count()).select_from(UserModel)).scalar_one() == 0
        assert session.execute(select(func.count()).select_from(ChannelModel)).scalar_one() == 0
        assert session.execute(select(func.count()).select_from(AgentModel)).scalar_one() == 0


# ------------------------------------------------ C: Exception Rollback（真实链）


def test_exception_rollback_on_real_db(real_file_db) -> None:
    _, test_engine = real_file_db
    session_factory = _session_factory(test_engine)

    with pytest.raises(RuntimeError):
        with SqlAlchemyUnitOfWork(session_factory=session_factory) as uow:
            uow.users.create(User(name="boom-user"))
            uow.channels.create(Channel(name="boom-channel", type="wecom"))
            uow.agents.create(Agent(name="boom-agent", type="acp"))
            raise RuntimeError("boom")

    with SqlAlchemyUnitOfWork(session_factory=session_factory) as uow:
        assert uow.users.get_by_id(1) is None


# ------------------------------------------------ D: Session cleanup（真实链）


def test_session_cleanup_no_leak_on_real_db(real_file_db) -> None:
    _, test_engine = real_file_db

    before = test_engine.pool.checkedout()
    with SqlAlchemyUnitOfWork(session_factory=_session_factory(test_engine)) as uow:
        uow.users.create(User(name="leak-check"))  # flush 已占用连接
        assert test_engine.pool.checkedout() == before + 1
        uow.commit()  # commit 后连接回到连接池
        assert test_engine.pool.checkedout() == before
    assert test_engine.pool.checkedout() == before


# ------------------------------------------------ F: /health 回归（最小 readiness）


def test_health_returns_ok_and_probes_db(tmp_path, monkeypatch) -> None:
    """/health：存活 + 最小 readiness（14.2.3.B 裁决变更）。

    旧断言「/health 不碰数据库」（5.6 F 节）已被 14.1 审计推翻：
    /health 现在执行一次 `SELECT 1` 以证明 DB 可达；DB 不可达 → 503。
    响应体仍为 `{"status": "ok"}`（不新增字段、不新增状态）。

    M7-AUI-9：DB 探针指向测试专用库——get_db 在调用时解析模块级
    SessionLocal，monkeypatch 即可整体换绑，开发库零接触。
    """
    from app.main import app
    from app.persistence import database as db_module

    probe_engine = create_database_engine(f"sqlite:///{tmp_path / 'health_probe.db'}")
    monkeypatch.setattr(db_module, "SessionLocal", _session_factory(probe_engine))

    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    probe_engine.dispose()


# ------------------------------------------------ G: SQLite 文件生命周期


def test_no_db_file_until_first_real_db_operation(tmp_path) -> None:
    """启动/import 阶段不产生 .db；首次真实 DB 操作才创建文件。

    M7-AUI-9：时序探针改在 pytest tmp_path 上进行，不再借用 / 复位开发库
    （此前本测试反复删除开发库文件，会把开发数据清空）。
    「应用 import 不偷偷建库」的不变量另由 test_app_source_never_auto_creates_schema
    （静态 AST 守卫）与 test_database.py 的 Settings → Engine 接线断言共同守护。
    """
    probe_db = tmp_path / "import_probe.db"
    probe_engine = create_database_engine(f"sqlite:///{probe_db}")
    assert not probe_db.exists(), "创建 Engine 不应建文件（惰性建连）"

    # 首次真实 DB 操作（schema 建立）→ 文件出现
    Base.metadata.create_all(probe_engine)
    assert probe_db.exists()

    probe_engine.dispose()


def test_smoke_never_touches_dev_db_file() -> None:
    """本测试文件对开发库文件零读、零写、零删除（M7-AUI-9 回归守卫）。"""
    src = Path(__file__).read_text(encoding="utf-8")
    # 断言词用拼接构造，避免自指（本文件源码含断言本身）。
    del_call = "un" + "link"
    dev_db = "agent_control_plane" + ".db"
    assert del_call not in src, "测试不得删除任何 DB 文件"
    assert dev_db not in src, "测试不得引用开发库文件名"


def test_app_source_never_auto_creates_schema() -> None:
    """机器化确认：create_all **只允许**出现在一次性初始化入口。

    Step 14.2.2 裁决变更（14.1 拍板：SQLite + create_all，不引入 Alembic）：
    schema 初始化是 **deployment / startup concern**，其唯一合法位置是
    `app/persistence/database.py` 的 `init_database()`——由应用启动装配调用一次。

    其余任何位置出现 create_all 仍然是违规：Runtime / Worker / Driver /
    Repository / Application Service 都不许「发现没表就建表」。
    """
    allowed = "app/persistence/database.py"
    offenders: list[str] = []
    for py_file in sorted((BACKEND_DIR / "app").rglob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "create_all":
                    rel = str(py_file.relative_to(BACKEND_DIR)).replace("\\", "/")
                    if rel != allowed:
                        offenders.append(rel)
    assert not offenders, (
        f"create_all 只允许出现在 {allowed}（一次性初始化）；违规位置：{offenders}")


# ------------------------------------------------ 5.6.10：结构无新增业务层


def test_no_premature_outer_layers_added() -> None:
    """5.6 视角：5.6 不该新增 Service / Resolver / Runtime / Adapter / Routing。

    Step 6.1 起 application/agents.py（AgentApplicationService）与
    repositories/unit_of_work.py（UoW Port）是合法的 Application Boundary 产物，
    因此 application 目录内容随 6.1 演进；其余外层（adapters / api 子包）仍是空壳。
    """
    app_dir = BACKEND_DIR / "app"

    # adapters/agents 自 10.1.2 起含真实实现 acp.py（ACPAdapter）；
    # 15.2.3 起追加 local.py（LocalAgentAdapter）+ registry.py（MultiAgentAdapter）
    assert sorted(p.name for p in (app_dir / "adapters" / "agents").glob("*.py")) == [
        "__init__.py", "acp.py", "local.py", "registry.py", "workbuddy.py"
    ]
    # 10.2.1 起 adapters/channels 含真实实现 wecom.py（WeComAdapter）；
    # 15.2.3 起追加 local.py（LocalChannelAdapter）+ registry.py（MultiChannelAdapter）；
    # 3B-2.2 起追加 wecom_bot.py（WeComBotAdapter：WebSocket transport）
    assert sorted(p.name for p in (app_dir / "adapters" / "channels").glob("*.py")) == [
        "__init__.py", "local.py", "registry.py", "wecom.py", "wecom_bot.py"
    ]
    # api/management 自 11.3 起含 agents.py、11.8 起含 channels.py、
    # 11.11 起含 connections.py、12.4 起含 users.py/identities.py（Management API）、
    # 13.1 起含 interactions.py（Observability 只读面）；
    # api/runtime 自 10.2.1 起含入站端点 channels.py
    assert sorted(p.name for p in (app_dir / "api" / "management").glob("*.py")) == [
        "__init__.py", "agents.py",
        # M6.6-C3 / ADR-12：+ bootstrap_arms.py（Verify Connection / Arm 端点）
        "bootstrap_arms.py", "channels.py", "connection_conversations.py",
        "connections.py",
        "identities.py", "interactions.py", "users.py"
    ]
    assert sorted(p.name for p in (app_dir / "api" / "runtime").glob("*.py")) == [
        "__init__.py", "channels.py"
    ]
    # 全 app 无 *_service.py / *resolver*.py 文件
    for py_file in (app_dir).rglob("*.py"):
        name = py_file.name.lower()
        assert not name.endswith("_service.py"), f"不应出现 Service：{py_file}"
        assert "resolver" not in name, f"不应出现 Resolver：{py_file}"
