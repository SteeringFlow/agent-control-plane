"""Step 6.1 —— Management Application Boundary 验收（签名随 6.2 输入契约演进）。

A  Application → UoW Port（而非直接 SqlAlchemyUoW）
B  Create Agent 全链路真实 round-trip（Domain → Application → UoW → Repo → Mapper → ORM → SQLite → 回读）
C  Get Agent（existing → Agent；missing → None）
D  事务边界：commit 通过 uow.commit()，Service 不直接碰 Session
E  Exception Rollback：create 中途异常 → UoW 自动 rollback → DB 无该 Agent
AST 架构护栏：application 只 import domain / repositories 端口 / 自身；
repositories/unit_of_work.py 是薄 UoW Port（无 Generic/Manager 框架）。
"""

import ast
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.application import AgentApplicationService, CreateAgent
from app.domain.agent import Agent
from app.persistence.models.agent import AgentModel
from app.persistence.repositories.agent import SqlAlchemyAgentRepository
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork
from app.repositories.unit_of_work import UnitOfWork

BACKEND_DIR = Path(__file__).resolve().parents[1]


def _factory(db_engine) -> sessionmaker:
    return sessionmaker(bind=db_engine, class_=Session, expire_on_commit=False)


def _service(db_engine) -> AgentApplicationService:
    return AgentApplicationService(
        uow_factory=lambda: SqlAlchemyUnitOfWork(session_factory=_factory(db_engine))
    )


def _count(db_engine) -> int:
    with Session(db_engine) as session:
        return session.execute(select(func.count()).select_from(AgentModel)).scalar_one()


# ------------------------------------------------ A: Application → UoW Port


def test_application_service_depends_on_uow_port() -> None:
    """agents.py 只 import domain + repositories 端口 + 自身，绝不 import persistence/sqlalchemy。"""
    py = BACKEND_DIR / "app" / "application" / "agents.py"
    tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))

    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module)

    allowed_prefixes = (
        "app.domain", "app.repositories", "app.application",
        "collections", "dataclasses", "typing",
    )
    leaked = [
        r for r in roots
        if not any(r == p or r.startswith(p + ".") for p in allowed_prefixes)
    ]
    assert not leaked, f"agents.py 越层导入：{leaked}"
    assert any(r == "app.repositories" or r.startswith("app.repositories.")
               for r in roots), "Service 应依赖 repositories（UoW/仓储）端口"

    # 源码不出现任何 SQLAlchemy 实现标识符（import 之外，Name 层也不得有）
    forbidden_names = {"SqlAlchemyUnitOfWork", "Session", "sessionmaker",
                       "create_engine", "Engine", "SessionLocal"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in forbidden_names:
            pytest.fail(f"agents.py 出现 SQLAlchemy 概念：{node.id}")


def test_uow_port_is_thin_and_persistence_uow_satisfies_it(db_engine) -> None:
    """UoW Port 是薄协议；SqlAlchemyUnitOfWork 结构上满足它。"""
    port_file = BACKEND_DIR / "app" / "repositories" / "unit_of_work.py"
    tree = ast.parse(port_file.read_text(encoding="utf-8"), filename="unit_of_work.py")
    classes = [n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    assert classes == ["UnitOfWork"], f"UoW Port 应只有 UnitOfWork：{classes}"

    # 结构满足：实例具备端口要求的属性与方法
    with SqlAlchemyUnitOfWork(session_factory=_factory(db_engine)) as uow:
        for member in ("users", "channels", "agents", "identities", "connections",
                       "commit", "rollback"):
            assert hasattr(uow, member), f"SqlAlchemyUnitOfWork 缺 {member}"
        assert callable(uow.commit) and callable(uow.rollback)


# ------------------------------------------------ B: Create Agent 全链路


def test_b_create_agent_full_chain_round_trip(db_engine) -> None:
    service = _service(db_engine)
    created = service.create_agent(
        CreateAgent(
            name="helper",
            type="acp",
            endpoint="https://agent.example/acp",
            config={"timeout_ms": 5000},
            credential_ref="secret://acp/helper",
        )
    )
    assert created.id is not None
    assert _count(db_engine) == 1

    fetched = service.get_agent(created.id)
    assert fetched is not None
    assert fetched == created
    assert fetched == Agent(
        id=created.id, name="helper", type="acp",
        endpoint="https://agent.example/acp", status="active",
        config={"timeout_ms": 5000}, credential_ref="secret://acp/helper",
        created_at=fetched.created_at, updated_at=fetched.updated_at,
    )


# ------------------------------------------------ C: Get Agent


def test_c_get_agent_existing_and_missing(db_engine) -> None:
    service = _service(db_engine)
    created = service.create_agent(CreateAgent(name="exists", type="acp"))
    assert service.get_agent(created.id) == created
    assert service.get_agent(99999) is None


# ------------------------------------------------ D: 事务边界归 UoW


def test_d_commit_goes_through_uow_not_session(db_engine) -> None:
    """Service 源码不出现 session.commit / 直接 Session 操作。"""
    py = BACKEND_DIR / "app" / "application" / "agents.py"
    tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
    # 唯一允许的 commit 是 uow.commit()：检查不存在对 `.session.` 或 `session.commit` 的引用
    session_usages = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Name) and n.id == "session"
    ]
    assert not session_usages, f"Service 不应接触 session 概念：{len(session_usages)} 处"
    commit_calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "commit"
    ]
    assert commit_calls, "Service 应显式 uow.commit()"
    # 行为验证：create 后立即（不经回滚）可从新连接读到 —— commit 已生效
    service = _service(db_engine)
    created = service.create_agent(CreateAgent(name="committed", type="acp"))
    assert _count(db_engine) == 1
    assert created.name == "committed"


# ------------------------------------------------ E: Exception Rollback


class _FlushThenRaiseAgentRepository(SqlAlchemyAgentRepository):
    """create 真实 flush 写入后抛异常，模拟 Application 操作中途失败。"""

    def create(self, agent: Agent) -> Agent:
        created = super().create(agent)  # flush：真实写进事务
        raise RuntimeError("simulated failure after flush")


class _FailingAgentUoW(SqlAlchemyUnitOfWork):
    def __enter__(self) -> "SqlAlchemyUnitOfWork":
        super().__enter__()
        self.agents = _FlushThenRaiseAgentRepository(self.session)
        return self


def test_e_exception_rollback_through_service(db_engine) -> None:
    service = AgentApplicationService(
        uow_factory=lambda: _FailingAgentUoW(session_factory=_factory(db_engine))
    )
    with pytest.raises(RuntimeError):
        service.create_agent(CreateAgent(name="boom-agent", type="acp"))

    assert _count(db_engine) == 0, "异常后 DB 不应存在该 Agent（UoW 已回滚）"
    assert service.get_agent(1) is None


# ------------------------------------------------ 防漂移：不引入 API / Runtime


def test_no_api_or_runtime_introduced() -> None:
    app_dir = BACKEND_DIR / "app"
    # 11.3：agents.py；11.8：channels.py；11.11：connections.py；
    # 12.4：users.py/identities.py（Management API，非空壳）；
    # 13.1：interactions.py（Observability 只读面）
    files = sorted(p.name for p in (app_dir / "api" / "management").glob("*.py"))
    # Package 2 / Step 3: + connection_conversations.py（Grant Management API）
    # M6.6-C3 / ADR-12: + bootstrap_arms.py（Verify Connection / Arm 端点）
    assert files == ["__init__.py", "agents.py", "bootstrap_arms.py",
                     "channels.py",
                     "connection_conversations.py", "connections.py",
                     "identities.py", "interactions.py", "users.py"], \
        f"management API 面漂移：{files}"
    # 10.2.1: api/runtime carries the inbound webhook (channels.py)
    runtime_files = sorted(p.name for p in (app_dir / "api" / "runtime").glob("*.py"))
    assert runtime_files == ["__init__.py", "channels.py"]
    # 全 app 无 *_router.py / *_dispatcher.py
    for py in (app_dir).rglob("*.py"):
        base = py.name.lower()
        assert not base.endswith("_router.py"), f"不应出现 Router：{py}"
        assert not base.endswith("_dispatcher.py"), f"不应出现 Dispatcher：{py}"
