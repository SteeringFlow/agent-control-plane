"""Step 7.1 —— Connection Domain Contract 验收。

Connection = Control Plane 中一个 Channel 与一个 Agent 之间的**受控访问路径
（Access Path）**。本步只交付 Contract 层（domain/port/mapper/adapter + UoW 接线），
Application Service 在 7.2 单独做。

核心契约锁定：
- 字段面严格 = 4.2 冻结（不增加字段）；
- UNIQUE(channel_id, agent_id)：一个 Channel → 一个 Agent 只有一条 Connection；
  Channel A→Agent X / Channel A→Agent Y / Channel B→Agent X 都允许；
- name 是显示名，不承担唯一性（无 find_by_name、无 name UNIQUE）；
- status 是 Access Path 可用状态的字符串，不是 HTTP/Runtime 状态；
- config 只承载连接本身配置（不得是 permissions/capabilities/... 万能容器）；
- credential_ref 只存凭据引用（无 credential/token/password/secret 字段）；
- channel_id / agent_id 是 immutable binding（改绑定 = 删除旧 Path + 建新 Path）；
- 不负责 Authorization/RBAC/Policy/Session/Interaction/Routing。

暂不交付（7.2+）：ConnectionService、API、Router、Resolver、Dispatcher、
Session、Interaction、Credential Management、Authorization。
"""

import ast
from pathlib import Path

import pytest
from sqlalchemy import UniqueConstraint, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.domain.agent import Agent
from app.domain.channel import Channel
from app.domain.connection import Connection
from app.persistence.models.connection import ConnectionModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

BACKEND_DIR = Path(__file__).resolve().parents[1]
APP_DIR = BACKEND_DIR / "app"


def _factory(db_engine) -> sessionmaker:
    return sessionmaker(bind=db_engine, class_=Session, expire_on_commit=False)


def _count(db_engine) -> int:
    with Session(db_engine) as session:
        return session.execute(
            select(func.count()).select_from(ConnectionModel)
        ).scalar_one()


def _make_channel(db_engine, name: str, channel_type: str = "wecom") -> Channel:
    with SqlAlchemyUnitOfWork(session_factory=_factory(db_engine)) as uow:
        ch = uow.channels.create(Channel(name=name, type=channel_type))
        uow.commit()
        return ch


def _make_agent(db_engine, name: str = "helper") -> Agent:
    with SqlAlchemyUnitOfWork(session_factory=_factory(db_engine)) as uow:
        ag = uow.agents.create(Agent(name=name, type="acp"))
        uow.commit()
        return ag


# ------------------------------------------------ 1. 契约形状（AST + schema）


def test_domain_field_contract_is_frozen_shape() -> None:
    """Connection dataclass 字段面严格等于 7.1.2 冻结契约，且是 frozen。"""
    py = APP_DIR / "domain" / "connection.py"
    tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
    cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)
               and n.name == "Connection")

    # frozen dataclass
    frozen = any(
        isinstance(d, ast.Call) and isinstance(d.func, ast.Name) and d.func.id == "dataclass"
        and any(kw.arg == "frozen" and getattr(kw.value, "value", False) for kw in d.keywords)
        for d in cls.decorator_list
    )
    assert frozen, "Connection 应是 frozen dataclass"

    fields: list[tuple[str, bool]] = []  # (name, has_default)
    for stmt in cls.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            fields.append((stmt.target.id, stmt.value is not None))

    expected = {
        "id", "channel_id", "agent_id", "name", "status",
        "config", "credential_ref", "created_at", "updated_at",
    }
    assert {name for name, _ in fields} == expected, \
        f"字段面漂移：{sorted({n for n, _ in fields} ^ expected)}"

    # 绑定与名称必填；其余默认
    required = [n for n, has_default in fields if not has_default]
    assert required == ["channel_id", "agent_id", "name"], \
        f"必填字段漂移：{required}"
    assert any(n == "status" and d for n, d in fields)


def test_domain_is_not_access_control_container() -> None:
    """Connection 不是万能配置容器：无 permission/capability/policy/user/tool 字段。"""
    src = (APP_DIR / "domain" / "connection.py").read_text(encoding="utf-8")
    tree = ast.parse(src, filename="connection.py")
    cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)
               and n.name == "Connection")
    field_names = {
        stmt.target.id for stmt in cls.body
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
    }
    forbidden = {"permissions", "capabilities", "policy", "routing_rules",
                 "users", "tools", "memory", "roles", "scopes"}
    assert not (field_names & forbidden), f"Access Control 字段混入：{field_names & forbidden}"
    # status 是 str（Access Path 可用态），不是 HTTP/processing 状态
    status_ann = next(
        stmt.annotation for stmt in cls.body
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
        and stmt.target.id == "status"
    )
    assert "str" in ast.unparse(status_ann)
    # credential_ref 只存引用；无明文字段
    assert not (field_names & {"credential", "token", "password", "secret", "api_key"})


def test_domain_is_pure_stdlib() -> None:
    """Connection domain 只 import 标准库（dataclasses/datetime），不碰任何 app 层。"""
    py = APP_DIR / "domain" / "connection.py"
    tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module)
    assert roots == {"dataclasses", "datetime"}, f"domain 越层导入：{roots}"


def test_schema_unique_key_and_name_not_unique() -> None:
    """schema 保留 UNIQUE(channel_id, agent_id)；name 不承担唯一性。"""
    from app.persistence import models  # noqa: F401  # 注册 8 张表
    from app.persistence.database import Base

    connections = Base.metadata.tables["connections"]
    assert any(u.name == "uq_connections_tenant_channel_agent_id"
               for u in connections.constraints), "UNIQUE(tenant_id, channel_id, agent_id) 漂移"
    # name 列不参与任何 UniqueConstraint
    unique_cols = {c.name for u in connections.constraints
                   if isinstance(u, UniqueConstraint)
                   for c in u.columns}
    assert "name" not in unique_cols, "name 不应有唯一约束"
    assert {"channel_id", "agent_id"} <= unique_cols


# ------------------------------------------------ 2. Port 语义（无 find_by_name）


def test_port_has_no_name_lookup() -> None:
    """Port 方法面 = {get_by_id, create, update, find_by_channel_and_agent}。"""
    py = APP_DIR / "repositories" / "connection.py"
    tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
    methods = {
        n.name for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name != "__init__"
    }
    # 9.4：+ find_active_by_channel（Routing 最小读路径）；
    # 11.11：+ list_connections（Management 陈列读）；
    # 仍无 find_by_name / find_all / 规则表查询
    assert methods == {
        "get_by_id", "create", "update", "find_by_channel_and_agent",
        "find_active_by_channel", "list_connections",
    }, f"Connection Port 方法面漂移：{methods}"
    assert not any("name" in m for m in methods), \
        "name 不承担唯一性，不应有按 name 查找"


def test_apply_to_orm_never_touches_binding() -> None:
    """apply_to_orm 只覆写可变字段：源码不得给 row.channel_id/agent_id 赋值。"""
    src = (APP_DIR / "persistence" / "mappers" / "connection.py").read_text(encoding="utf-8")
    tree = ast.parse(src, filename="connection.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Attribute) \
                        and t.attr in ("channel_id", "agent_id"):
                    pytest.fail("apply_to_orm 不得覆写绑定 channel_id/agent_id")


def test_apply_to_orm_covers_all_mutable_fields() -> None:
    """apply_to_orm 覆写面 = name/status/config/credential_ref（全部可变字段）。"""
    src = (APP_DIR / "persistence" / "mappers" / "connection.py").read_text(encoding="utf-8")
    tree = ast.parse(src, filename="connection.py")
    assigned = {t.attr for node in ast.walk(tree)
                if isinstance(node, ast.Assign)
                for t in node.targets if isinstance(t, ast.Attribute)}
    assert assigned == {"name", "status", "config", "credential_ref"}, \
        f"update 覆写字段漂移：{assigned}"


# ------------------------------------------------ 3. Mapper 纯转换


def test_mapper_round_trip_translation() -> None:
    """to_orm → to_domain 业务字段无损（时间戳由 DB 生成，映射时允许为空）。"""
    conn = Connection(
        channel_id=10, agent_id=20, name="销售机器人", status="active",
        config={"timeout": 30}, credential_ref="cred_xxx",
    )
    row = ConnectionModel(
        channel_id=conn.channel_id, agent_id=conn.agent_id, name=conn.name,
        status=conn.status, config=conn.config, credential_ref=conn.credential_ref,
    )
    back = Connection(
        id=None, channel_id=row.channel_id, agent_id=row.agent_id, name=row.name,
        status=row.status, config=row.config, credential_ref=row.credential_ref,
    )
    assert back == conn


# ------------------------------------------------ 4. Adapter 行为（真实 DB）


def test_create_and_get_round_trip(db_engine) -> None:
    channel = _make_channel(db_engine, "wecom-main")
    agent = _make_agent(db_engine, "sales-helper")

    with SqlAlchemyUnitOfWork(session_factory=_factory(db_engine)) as uow:
        created = uow.connections.create(
            Connection(
                channel_id=channel.id, agent_id=agent.id, name="销售机器人",
                config={"timeout": 30}, credential_ref="cred_sales",
            )
        )
        uow.commit()
        conn_id = created.id

    with SqlAlchemyUnitOfWork(session_factory=_factory(db_engine)) as uow:
        fetched = uow.connections.get_by_id(conn_id)

    assert fetched is not None
    assert fetched.id == conn_id
    assert fetched.channel_id == channel.id
    assert fetched.agent_id == agent.id
    assert fetched.name == "销售机器人"
    assert fetched.status == "active"
    assert fetched.config == {"timeout": 30}
    assert fetched.credential_ref == "cred_sales"
    assert fetched.created_at is not None
    assert fetched.updated_at is not None
    assert _count(db_engine) == 1


def test_find_by_channel_and_agent(db_engine) -> None:
    channel = _make_channel(db_engine, "wecom-main")
    agent = _make_agent(db_engine, "sales-helper")
    with SqlAlchemyUnitOfWork(session_factory=_factory(db_engine)) as uow:
        created = uow.connections.create(
            Connection(channel_id=channel.id, agent_id=agent.id, name="sales")
        )
        uow.commit()
    with SqlAlchemyUnitOfWork(session_factory=_factory(db_engine)) as uow:
        assert uow.connections.find_by_channel_and_agent(
            channel.id, agent.id
        ).id == created.id
        assert uow.connections.find_by_channel_and_agent(channel.id, 99999) is None
        assert uow.connections.find_by_channel_and_agent(99999, agent.id) is None


def test_many_to_many_but_one_connection_per_pair(db_engine) -> None:
    """Channel A→X / A→Y / B→X 都允许；A→X 只有一条（7.1.4）。"""
    ch_a = _make_channel(db_engine, "wecom-main")
    ch_b = _make_channel(db_engine, "feishu-main", "feishu")
    ag_x = _make_agent(db_engine, "agent-x")
    ag_y = _make_agent(db_engine, "agent-y")

    with SqlAlchemyUnitOfWork(session_factory=_factory(db_engine)) as uow:
        c1 = uow.connections.create(Connection(channel_id=ch_a.id, agent_id=ag_x.id, name="A-X"))
        c2 = uow.connections.create(Connection(channel_id=ch_a.id, agent_id=ag_y.id, name="A-Y"))
        c3 = uow.connections.create(Connection(channel_id=ch_b.id, agent_id=ag_x.id, name="B-X"))
        uow.commit()
    assert _count(db_engine) == 3
    assert len({c1.id, c2.id, c3.id}) == 3


def test_db_unique_constraint_rejects_duplicate_pair(db_engine) -> None:
    """UNIQUE(channel_id, agent_id) 硬约束：同一对只能一条。"""
    channel = _make_channel(db_engine, "wecom-main")
    agent = _make_agent(db_engine, "sales-helper")
    with SqlAlchemyUnitOfWork(session_factory=_factory(db_engine)) as uow:
        uow.connections.create(Connection(channel_id=channel.id, agent_id=agent.id,
                                          name="first"))
        uow.commit()
    with pytest.raises(IntegrityError):
        with SqlAlchemyUnitOfWork(session_factory=_factory(db_engine)) as uow:
            uow.connections.create(Connection(channel_id=channel.id, agent_id=agent.id,
                                              name="second"))
            uow.commit()


def test_update_mutable_fields_only_binding_immutable(db_engine) -> None:
    """update 只改 name/status/config/credential_ref；channel_id/agent_id 永不改变。"""
    ch_a = _make_channel(db_engine, "wecom-main")
    ch_b = _make_channel(db_engine, "feishu-main", "feishu")
    agent = _make_agent(db_engine, "sales-helper")

    with SqlAlchemyUnitOfWork(session_factory=_factory(db_engine)) as uow:
        created = uow.connections.create(
            Connection(channel_id=ch_a.id, agent_id=agent.id, name="销售机器人",
                       config={"timeout": 30}, credential_ref="cred_old")
        )
        uow.commit()

    # 即使传入不同的 channel_id（模拟误改绑定），repo.update 也不得覆写绑定
    changed = Connection(
        id=created.id, channel_id=ch_b.id, agent_id=agent.id,
        name="售后机器人", status="disabled", config={"timeout": 60},
        credential_ref="cred_new",
    )
    with SqlAlchemyUnitOfWork(session_factory=_factory(db_engine)) as uow:
        updated = uow.connections.update(changed)
        uow.commit()

    assert updated.name == "售后机器人"
    assert updated.status == "disabled"
    assert updated.config == {"timeout": 60}
    assert updated.credential_ref == "cred_new"
    # 绑定保持原样（immutable binding：改绑定 = 删旧 + 建新，不是 update）
    assert updated.channel_id == ch_a.id
    assert updated.agent_id == agent.id

    with SqlAlchemyUnitOfWork(session_factory=_factory(db_engine)) as uow:
        refetched = uow.connections.get_by_id(created.id)
    assert refetched.channel_id == ch_a.id
    assert refetched.agent_id == agent.id
    assert _count(db_engine) == 1


def test_adapter_does_not_commit_on_its_own(db_engine) -> None:
    """Repository 不 commit：未提交的 create 在 rollback 后不落库。"""
    channel = _make_channel(db_engine, "wecom-main")
    agent = _make_agent(db_engine, "sales-helper")

    with pytest.raises(RuntimeError):
        with SqlAlchemyUnitOfWork(session_factory=_factory(db_engine)) as uow:
            uow.connections.create(
                Connection(channel_id=channel.id, agent_id=agent.id, name="temp")
            )
            raise RuntimeError("boom")  # 未 commit → 上下文自动 rollback

    assert _count(db_engine) == 0


# ------------------------------------------------ 5. 7.1 反向 Scope


def test_application_file_face_matches_delivered_slices() -> None:
    """application 文件面 = 5 个 Service + errors（Connection Service 于 7.2 交付）。"""
    application_dir = APP_DIR / "application"
    files = sorted(p.name for p in application_dir.glob("*.py"))
    assert files == ["__init__.py", "access_authorization.py", "agent_dispatch.py",
                     # M7-AUI-10.5：WorkBuddy External Agent Identity 语义/查重
                     "agent_identity.py",
                     "agents.py",
                     # M6.6-C3 / ADR-12：Bootstrap 组件 + Bootstrap Arm 管理面用例
                     "bootstrap.py", "bootstrap_arms.py",
                     "browser_auth.py",
                     "channel_inbound.py", "channels.py", "connection_conversations.py",
                     "connections.py",
                     "coordinator.py", "delivery.py", "delivery_recovery.py", "driver.py",
    "errors.py",
                     "identities.py", "identity_resolution.py",
                     "interaction_observability.py", "interaction_recording.py",
                     "routing.py", "runtime.py", "session_creation.py",
                     "session_resolution.py", "tenant_fanout.py",
                     "tenant_provisioning.py", "users.py",
                     "worker.py"], \
        f"application 文件面漂移：{files}"  # 12.9.4.5: + worker / 12.9.4.16: + driver / 13.1: + interaction_observability / Cloud-0.1.2.1: + tenant_provisioning


def test_connection_contract_does_not_cross_into_runtime() -> None:
    """Connection Contract 代码不出现 Session/Interaction/Router/Policy 概念。

    domain / port / mapper 不得 import SQLAlchemy / FastAPI；Adapter 位于
    persistence 层、合法使用 SQLAlchemy（基础边界审计另行覆盖），此处不误伤。
    """
    all_sources = {
        rel: (APP_DIR / rel).read_text(encoding="utf-8")
        for rel in ("domain/connection.py", "repositories/connection.py",
                    "persistence/mappers/connection.py",
                    "persistence/repositories/connection.py")
    }
    # 类面（不含 docstring 里的说明词，只查 ClassDef/FunctionDef 名）
    forbidden_class_tokens = ("session", "interaction", "router", "dispatcher",
                              "resolver", "policy", "capability", "runtime")
    for src in all_sources.values():
        tree = ast.parse(src, filename="connection")
        for node in ast.walk(tree):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                lower = node.name.lower()
                assert not any(tok in lower for tok in forbidden_class_tokens), \
                    f"{node.name} 混入 Runtime/Policy 概念"

    # domain/port/mapper 不 import 基础设施
    for rel in ("domain/connection.py", "repositories/connection.py",
                "persistence/mappers/connection.py"):
        tree = ast.parse(all_sources[rel], filename=rel)
        roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module)
        assert not any(r.startswith("sqlalchemy") or r.startswith("fastapi")
                       for r in roots), f"{rel} 依赖基础设施：{roots}"


def test_adapters_and_api_still_empty_shells() -> None:
    """Connection ≠ Channel/Agent Adapter：Channel Adapter 与 api 运行时层仍空壳；
    agents 自 10.1.2 起含唯一真实实现 acp.py（ACPAdapter 消费 Connection，不越界）。"""
    for sub in ("channels", "agents"):
        files = sorted(p.name for p in (APP_DIR / "adapters" / sub).glob("*.py"))
        # 15.2.3：agents 含 acp.py+local.py+registry.py；channels 含 wecom.py+local.py+registry.py
        # 3B-2.2：channels 追加 wecom_bot.py（WebSocket transport）
        expected = (["__init__.py", "acp.py", "local.py", "registry.py", "workbuddy.py"] if sub == "agents"
                    else ["__init__.py", "local.py", "registry.py", "wecom.py",
                          "wecom_bot.py"])
        assert files == expected, f"adapters/{sub} 实现面漂移：{files}"
    for sub in ("management",):  # 11.3/11.8/11.11 + 12.4 users/identities + 13.1 interactions
        files = sorted(p.name for p in (APP_DIR / "api" / "management").glob("*.py"))
        # Package 2 / Step 3: + connection_conversations.py（Grant Management API）
        # M6.6-C3 / ADR-12: + bootstrap_arms.py（Verify Connection / Arm 端点）
        assert files == ["__init__.py", "agents.py", "bootstrap_arms.py",
                         "channels.py",
                         "connection_conversations.py", "connections.py",
                         "identities.py", "interactions.py", "users.py"], \
            f"api/{sub} 面漂移：{files}"
        schema_files = sorted(p.name for p in (APP_DIR / "schemas").glob("*.py"))
        # Package 2 / Step 3: + connection_conversations.py（Grant schema）
        # M6.6-C3 / ADR-12: + bootstrap_arms.py（Arm 响应契约）
        assert schema_files == ["__init__.py", "agents.py", "auth.py",
                                "bootstrap_arms.py", "channels.py",
                                "connection_conversations.py", "connections.py",
                                "health.py", "identities.py", "interactions.py",
                                "users.py"], f"schemas 面漂移：{schema_files}"
        # 10.2.1: api/runtime carries the inbound webhook (channels.py)
        runtime_files = sorted(p.name for p in (APP_DIR / "api" / "runtime").glob("*.py"))
        assert runtime_files == ["__init__.py", "channels.py"]
