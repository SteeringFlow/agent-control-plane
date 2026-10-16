"""Step 7.2 —— Connection Management Application Slice 验收。

Connection 成为可管理对象：Create / Get / Update 全链路。

与 6.6 Identity 同构，但它是**第三个跨实体编排点**：创建同时协调
ChannelRepository + AgentRepository + ConnectionRepository，
且**只编排不复制业务逻辑**（源码级锁定）。

核心契约（7.1 延续，双层防线）：
- 绑定 (channel_id, agent_id) 不可变：
  Application 层 UpdateConnection 无绑定字段；
  Persistence 层 repo.update 不覆写绑定；
- Application duplicate check ≠ DB 唯一约束：并发硬保证仍归
  UNIQUE(channel_id, agent_id)；
- name 只是显示名；status 是 Access Path 可用态；credential_ref 只存引用。

仍然不做：API / FastAPI schema / Authentication / Authorization / RBAC /
Capability / Policy / Router / Resolver / Dispatcher / Session / Interaction /
Channel Adapter / Agent Adapter——尤其不做"Agent token 能不能连接"的连通性测试
（那属于 Runtime / Adapter）。
"""

import ast
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.application import (
    AgentNotFoundError,
    ChannelNotFoundError,
    ConnectionApplicationService,
    ConnectionNotFoundError,
    CreateConnection,
    DuplicateConnectionError,
    InvalidConnectionInputError,
    UpdateConnection,
)
from app.domain.agent import Agent
from app.domain.channel import Channel
from app.domain.connection import Connection
from app.persistence.models.connection import ConnectionModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

BACKEND_DIR = Path(__file__).resolve().parents[1]
CONNECTIONS_PY = BACKEND_DIR / "app" / "application" / "connections.py"


def _factory(db_engine) -> sessionmaker:
    return sessionmaker(bind=db_engine, class_=Session, expire_on_commit=False)


def _service(db_engine) -> ConnectionApplicationService:
    return ConnectionApplicationService(
        uow_factory=lambda: SqlAlchemyUnitOfWork(session_factory=_factory(db_engine))
    )


def _count(db_engine) -> int:
    with Session(db_engine) as session:
        return session.execute(
            select(func.count()).select_from(ConnectionModel)
        ).scalar_one()


def _make_channel(db_engine, name: str = "wecom-main",
                  channel_type: str = "wecom") -> Channel:
    with SqlAlchemyUnitOfWork(session_factory=_factory(db_engine)) as uow:
        ch = uow.channels.create(Channel(name=name, type=channel_type))
        uow.commit()
        return ch


def _make_agent(db_engine, name: str = "sales-helper") -> Agent:
    with SqlAlchemyUnitOfWork(session_factory=_factory(db_engine)) as uow:
        ag = uow.agents.create(Agent(name=name, type="acp"))
        uow.commit()
        return ag


# ------------------------------------------------ AST：Service 依赖与编排边界


def test_service_imports_only_ports_and_domain() -> None:
    """connections.py 只 import domain + repositories 端口 + 自身。"""
    tree = ast.parse(CONNECTIONS_PY.read_text(encoding="utf-8"), filename=str(CONNECTIONS_PY))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module)

    allowed = ("app.domain", "app.repositories", "app.application",
               "collections", "dataclasses", "typing")
    leaked = [r for r in roots
              if not any(r == p or r.startswith(p + ".") for p in allowed)]
    assert not leaked, f"connections.py 越层导入：{leaked}"
    assert any(r == "app.repositories" or r.startswith("app.repositories.")
               for r in roots), "Service 应依赖 repositories 端口"

    forbidden_names = {"SqlAlchemyUnitOfWork", "Session", "sessionmaker",
                       "create_engine", "Engine", "AgentModel", "ChannelModel",
                       "ConnectionModel", "HTTPException", "APIRouter"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in forbidden_names:
            pytest.fail(f"connections.py 出现基础设施概念：{node.id}")


def test_service_orchestrates_without_pollution() -> None:
    """关键边界：只编排三个 Port，绝不复制 Channel/Agent 创建逻辑。"""
    tree = ast.parse(CONNECTIONS_PY.read_text(encoding="utf-8"), filename=str(CONNECTIONS_PY))
    calls = {
        _dotted(n.func) for n in ast.walk(tree)
        if isinstance(n, ast.Call)
    }

    # 编排三仓储：读存在性 + 写 Connection
    assert any(c.startswith("uow.channels.get_by_id") for c in calls), "应校验 channel"
    assert any(c.startswith("uow.agents.get_by_id") for c in calls), "应校验 agent"
    assert any(c.startswith("uow.connections.") for c in calls), "应写 connections"
    assert any("find_by_channel_and_agent" in c for c in calls), "应做绑定预检"

    # 不复制 Channel/Agent 创建逻辑
    assert not any(c.startswith("uow.channels.create") or c.startswith("uow.channels.update")
                   for c in calls), "不应创建/更新 Channel"
    assert not any(c.startswith("uow.agents.create") or c.startswith("uow.agents.update")
                   for c in calls), "不应创建/更新 Agent"
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            dotted = _dotted(node.func)
            assert dotted not in {"Channel", "Agent"}, \
                f"不应构造 Channel/Agent 领域对象：{dotted}"


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    return ""


def test_update_contract_has_no_binding_fields() -> None:
    """UpdateConnection 无 channel_id/agent_id（Application 层第一道防线）。"""
    tree = ast.parse(CONNECTIONS_PY.read_text(encoding="utf-8"), filename=str(CONNECTIONS_PY))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "UpdateConnection":
            fields = {
                stmt.target.id for stmt in node.body
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
            }
            assert fields == {"name", "status", "config", "credential_ref"}, \
                f"UpdateConnection 字段漂移：{fields}"


# ------------------------------------------------ 存在性校验（跨仓储编排）


def test_create_connection_rejects_missing_channel(db_engine) -> None:
    service = _service(db_engine)
    agent = _make_agent(db_engine)
    with pytest.raises(ChannelNotFoundError):
        service.create_connection(
            CreateConnection(channel_id=99999, agent_id=agent.id, name="x")
        )
    assert _count(db_engine) == 0


def test_create_connection_rejects_missing_agent(db_engine) -> None:
    service = _service(db_engine)
    channel = _make_channel(db_engine)
    with pytest.raises(AgentNotFoundError):
        service.create_connection(
            CreateConnection(channel_id=channel.id, agent_id=99999, name="x")
        )
    assert _count(db_engine) == 0


# ------------------------------------------------ Create


def test_create_connection_round_trip(db_engine) -> None:
    service = _service(db_engine)
    channel = _make_channel(db_engine)
    agent = _make_agent(db_engine)

    created = service.create_connection(
        CreateConnection(
            channel_id=channel.id, agent_id=agent.id, name="销售机器人",
            config={"timeout": 30}, credential_ref="cred_sales",
        )
    )
    assert created.id is not None
    assert created.status == "active"
    assert created.channel_id == channel.id
    assert created.agent_id == agent.id

    fetched = service.get_connection(created.id)
    assert fetched is not None
    assert fetched.name == "销售机器人"
    assert fetched.status == "active"
    assert fetched.config == {"timeout": 30}
    assert fetched.credential_ref == "cred_sales"
    assert _count(db_engine) == 1


def test_duplicate_binding_rejected(db_engine) -> None:
    """同 (channel_id, agent_id) 第二条 → DuplicateConnectionError，且不残留。"""
    service = _service(db_engine)
    channel = _make_channel(db_engine)
    agent = _make_agent(db_engine)
    service.create_connection(
        CreateConnection(channel_id=channel.id, agent_id=agent.id, name="first")
    )
    with pytest.raises(DuplicateConnectionError):
        service.create_connection(
            CreateConnection(channel_id=channel.id, agent_id=agent.id,
                             name="second", config={"x": 1})
        )
    assert _count(db_engine) == 1
    assert service.get_connection(1).name == "first"  # 无残留半成品


def test_cross_pairs_allowed(db_engine) -> None:
    """Channel A→X / A→Y / B→X 都允许（7.1.4 语义）。"""
    service = _service(db_engine)
    ch_a = _make_channel(db_engine, "wecom-main")
    ch_b = _make_channel(db_engine, "feishu-main", "feishu")
    ag_x = _make_agent(db_engine, "agent-x")
    ag_y = _make_agent(db_engine, "agent-y")

    ids = [
        service.create_connection(
            CreateConnection(channel_id=ch_a.id, agent_id=ag_x.id, name="A-X")).id,
        service.create_connection(
            CreateConnection(channel_id=ch_a.id, agent_id=ag_y.id, name="A-Y")).id,
        service.create_connection(
            CreateConnection(channel_id=ch_b.id, agent_id=ag_x.id, name="B-X")).id,
    ]
    assert _count(db_engine) == 3
    assert len(set(ids)) == 3


def test_db_unique_constraint_is_concurrency_backstop(db_engine) -> None:
    """绕过 Application 守卫直写 → 撞 DB UNIQUE(channel_id, agent_id)。"""
    service = _service(db_engine)
    channel = _make_channel(db_engine)
    agent = _make_agent(db_engine)
    service.create_connection(
        CreateConnection(channel_id=channel.id, agent_id=agent.id, name="first")
    )
    with pytest.raises(IntegrityError):
        with SqlAlchemyUnitOfWork(session_factory=_factory(db_engine)) as uow:
            uow.connections.create(
                Connection(channel_id=channel.id, agent_id=agent.id, name="second")
            )
            uow.commit()


# ------------------------------------------------ Get / Update


def test_get_connection_existing_and_missing(db_engine) -> None:
    service = _service(db_engine)
    channel = _make_channel(db_engine)
    agent = _make_agent(db_engine)
    created = service.create_connection(
        CreateConnection(channel_id=channel.id, agent_id=agent.id, name="x")
    )
    assert service.get_connection(created.id).name == "x"
    assert service.get_connection(99999) is None


def test_update_mutable_fields_persist_binding_unchanged(db_engine) -> None:
    service = _service(db_engine)
    channel = _make_channel(db_engine)
    agent = _make_agent(db_engine)
    created = service.create_connection(
        CreateConnection(channel_id=channel.id, agent_id=agent.id, name="销售机器人",
                         credential_ref="cred_old")
    )

    updated = service.update_connection(
        created.id,
        UpdateConnection(name="售后机器人", status="disabled",
                         config={"timeout": 60}, credential_ref="cred_new"),
    )
    assert updated.id == created.id
    assert updated.name == "售后机器人"
    assert updated.status == "disabled"
    assert updated.config == {"timeout": 60}
    assert updated.credential_ref == "cred_new"
    # 绑定不变
    assert updated.channel_id == channel.id
    assert updated.agent_id == agent.id

    refetched = service.get_connection(created.id)
    assert refetched == updated
    assert _count(db_engine) == 1


def test_update_missing_connection_raises_not_found(db_engine) -> None:
    service = _service(db_engine)
    with pytest.raises(ConnectionNotFoundError):
        service.update_connection(99999, UpdateConnection(name="ghost"))
    assert _count(db_engine) == 0


# ------------------------------------------------ 输入契约


@pytest.mark.parametrize(
    "bad",
    [
        CreateConnection(channel_id=0, agent_id=1, name="x"),
        CreateConnection(channel_id=-1, agent_id=1, name="x"),
        CreateConnection(channel_id=1, agent_id=0, name="x"),
        CreateConnection(channel_id=1, agent_id=1, name=""),
        CreateConnection(channel_id=1, agent_id=1, name="   "),
    ],
    ids=["zero-channel", "negative-channel", "zero-agent", "empty-name", "blank-name"],
)
def test_create_rejects_invalid_input(db_engine, bad) -> None:
    service = _service(db_engine)
    with pytest.raises(InvalidConnectionInputError):
        service.create_connection(bad)
    assert _count(db_engine) == 0


def test_update_rejects_invalid_input(db_engine) -> None:
    service = _service(db_engine)
    channel = _make_channel(db_engine)
    agent = _make_agent(db_engine)
    created = service.create_connection(
        CreateConnection(channel_id=channel.id, agent_id=agent.id, name="x")
    )
    with pytest.raises(InvalidConnectionInputError):
        service.update_connection(created.id, UpdateConnection(name=""))
    assert service.get_connection(created.id).name == "x"


# ------------------------------------------------ 事务边界


def test_transaction_boundary_commit_is_effective(db_engine) -> None:
    """每个用例自持一个 UoW：create/update 提交后立即可见。"""
    service = _service(db_engine)
    channel = _make_channel(db_engine)
    agent = _make_agent(db_engine)
    created = service.create_connection(
        CreateConnection(channel_id=channel.id, agent_id=agent.id, name="tx-ok")
    )
    assert _count(db_engine) == 1
    service.update_connection(created.id, UpdateConnection(status="disabled"))
    assert _count(db_engine) == 1
    assert service.get_connection(created.id).status == "disabled"


def test_transaction_boundary_rollback_on_exception(db_engine) -> None:
    """存在性校验通过后写入失败 → UoW 自动回滚 → 无残留。"""
    from app.persistence.repositories.connection import SqlAlchemyConnectionRepository

    class _FlushThenRaise(SqlAlchemyConnectionRepository):
        def create(self, connection: Connection) -> Connection:
            super().create(connection)
            raise RuntimeError("boom")

    class _FailingUoW(SqlAlchemyUnitOfWork):
        def __enter__(self):
            super().__enter__()
            self.connections = _FlushThenRaise(self.session)
            return self

    channel = _make_channel(db_engine)
    agent = _make_agent(db_engine)
    service = ConnectionApplicationService(
        uow_factory=lambda: _FailingUoW(session_factory=_factory(db_engine))
    )
    with pytest.raises(RuntimeError):
        service.create_connection(
            CreateConnection(channel_id=channel.id, agent_id=agent.id, name="boom")
        )
    assert _count(db_engine) == 0


# ------------------------------------------------ 反向 Scope（Management ≠ Runtime）


def test_no_runtime_or_connectivity_smuggled() -> None:
    """本 Slice 是 Management：方法面不出现连接性验证 / Adapter / Auth 语义。"""
    src = CONNECTIONS_PY.read_text(encoding="utf-8")
    tree = ast.parse(src, filename="connections.py")
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            lower = node.name.lower()
            # 注意：不加 "connect" 子串——Connection/Connection* 是合法领域词
            assert not any(tok in lower for tok in ("ping", "handshake", "token",
                                                    "auth", "adapter", "resolve",
                                                    "dispatch")), \
                f"Management Slice 混入 Runtime 语义：{node.name}"
    # 用例面 = create/get/update + 11.11 list_connections（Management 陈列读）
    service = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)
                   and n.name == "ConnectionApplicationService")
    use_cases = {n.name for n in service.body
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and not n.name.startswith("_")}
    assert use_cases == {"create_connection", "get_connection", "update_connection",
                         "list_connections"}, \
        f"Service 用例面漂移：{use_cases}"
