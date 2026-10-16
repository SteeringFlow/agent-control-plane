"""Step 7.3 —— Connection Management Consistency / Boundary Audit（综合冻结）。

把五个 Management 对象：

    Agent / Channel / User / Identity / Connection

放在同一把尺子下审计，确认 Step 6.1~7.2 一路演进后，**统一边界没有漂移**：

    Domain → Application → Repository Port → Persistence Adapter → Mapper → UoW

并重点验证两件 6.5/6.7 没有专门收口的事：

1. **跨实体编排**：Identity → User + Channel、Connection → Channel + Agent
   是「编排（orchestration）」而不是「业务逻辑复制」——
   只读端点校验存在性 + 写本实体，绝不构造/创建被依赖实体。
2. **Connection 特有边界**：(channel_id, agent_id) = Access Path 的 identity；
   绑定不可变；name ≠ 唯一身份；credential_ref ≠ credential；
   config ≠ policy/capability/routing 容器。

最后用 **mutation test** 证明反向审计真的能抓到偷渡
（而不是永远返回空）。

边界扩边说明：`InboundInteraction`（8.1 Runtime 入口契约）与 `SessionResolution`
（8.3 Session Resolution 契约，值对象）随各自步骤 **主动冻结**，被精确白名单
放行；除此之外的任何 Session 实体 / Interaction / Resolver / Router / Dispatcher /
Adapter 等仍属偷渡，mutation 测试同步验证白名单不可逃逸。

（6.5 / 6.7 作为历史里程碑保留；本文件是 Connection 加入后的综合版。）
"""

import ast
from pathlib import Path

import pytest

from app.persistence.database import Base

BACKEND_DIR = Path(__file__).resolve().parents[1]
APP_DIR = BACKEND_DIR / "app"

# 五个已交付 Management 对象
ENTITIES = ("agent", "channel", "user", "identity", "connection")
DOMAIN_CLASSES = {e: E for e, E in zip(
    ENTITIES, ("Agent", "Channel", "User", "Identity", "Connection"))}
SERVICE_CLASSES = {e: f"{E}ApplicationService"
                   for e, E in DOMAIN_CLASSES.items()}
SERVICE_FILES = {
    "agent": "agents.py", "channel": "channels.py", "user": "users.py",
    "identity": "identities.py", "connection": "connections.py",
}
REPO_ATTRS = {"agent": "agents", "channel": "channels", "user": "users",
              "identity": "identities", "connection": "connections"}
EXPECTED_PORT_METHODS = {
    "agent": {"get_by_id", "create", "update", "find_by_name", "list_agents"},
    # （identity 见下方 find_or_create 注释）
    # 11.8：+ list_channels（Channel Management API 的 List 用例）
    # Cloud-0.1.3.1：+ find_tenant_id_by_id（入站路由 hint → 已认证 Channel 的 tenant_id）
    "channel": {"get_by_id", "create", "update", "find_by_type_and_name",
                "list_channels", "find_tenant_id_by_id"},
    "user": {"get_by_id", "create", "update", "list_users"},
    # 12.4：+ list_identities（Identity Management API 的 List 用例）
    # M6.6-C3 / ADR-12 §11：+ find_or_create（Bootstrap 事务内的幂等建立）
    "identity": {"get_by_id", "create", "update", "find_by_channel_and_external_id",
                 "list_identities", "find_or_create"},
    # 9.4：+ find_active_by_channel（Routing Execution 的最小读路径）；
    # 11.11：+ list_connections（Connection Management API 的 List 用例）
    "connection": {"get_by_id", "create", "update", "find_by_channel_and_agent",
                   "find_active_by_channel", "list_connections"},
}
# 跨实体编排时，本 Service「只校验存在性、只写自己」的依赖端点
ORCHESTRATION = {
    "identity": {
        "reads": ("uow.users.get_by_id", "uow.channels.get_by_id"),
        "writes": ("uow.identities.create", "uow.identities.update"),
        "must_not_write": ("uow.users.create", "uow.channels.create"),
        "must_not_construct": ("User", "Channel"),
    },
    "connection": {
        "reads": ("uow.channels.get_by_id", "uow.agents.get_by_id",
                  "uow.connections.find_by_channel_and_agent"),
        "writes": ("uow.connections.create", "uow.connections.update"),
        "must_not_write": ("uow.channels.create", "uow.agents.create"),
        "must_not_construct": ("Channel", "Agent"),
    },
}

SERVICE_ALLOWED_ROOTS = (
    "app.domain", "app.repositories", "app.application",
    "collections", "dataclasses", "typing",
)
SERVICE_FORBIDDEN_ROOTS = ("fastapi", "starlette", "pydantic",
                           "app.persistence", "app.api", "app.schemas", "app.adapters")
INFRA_NAMES = {
    "Session", "sessionmaker", "create_engine", "Engine", "SessionLocal",
    "SqlAlchemyUnitOfWork", "SqlAlchemyAgentRepository", "SqlAlchemyChannelRepository",
    "SqlAlchemyUserRepository", "SqlAlchemyIdentityRepository",
    "SqlAlchemyConnectionRepository",
    "AgentModel", "ChannelModel", "UserModel", "IdentityModel", "ConnectionModel",
    "HTTPException", "APIRouter", "FastAPI", "Request", "Response", "BaseModel",
}


# ------------------------------------------------------------------ AST helpers


def _tree(py: Path) -> ast.AST:
    return ast.parse(py.read_text(encoding="utf-8"), filename=str(py))


def _roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module)
    return roots


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    return ""


def _classes(tree: ast.AST) -> dict[str, ast.ClassDef]:
    return {n.name: n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}


def _methods(cls: ast.ClassDef) -> dict[str, ast.FunctionDef]:
    return {n.name: n for n in cls.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _service_path(entity: str) -> Path:
    return APP_DIR / "application" / SERVICE_FILES[entity]


def _calls(tree: ast.AST) -> set[str]:
    out: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            out.add(_dotted(n.func))
    return out


def _entity_layer_files() -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for layer in ("domain", "repositories", "application",
                  "persistence/mappers", "persistence/repositories"):
        result[layer] = {p.stem for p in (APP_DIR / layer).glob("*.py")
                         if p.stem != "__init__"}
    return result


# ================================================================== A Domain 边界


def test_a_domain_entities_pure_and_isolated() -> None:
    """五个实体都是纯 dataclass，彼此零耦合，不继承 ORM Base。"""
    for entity in ENTITIES:
        py = APP_DIR / "domain" / f"{entity}.py"
        roots = _roots(_tree(py))
        # Connection 额外允许 datetime（时间戳回显字段）
        assert roots and "dataclasses" in roots, f"domain/{entity}.py 缺 dataclasses：{roots}"
        assert roots <= {"dataclasses", "datetime"}, \
            f"domain/{entity}.py 越层导入：{roots}"

        cls = _classes(_tree(py))[DOMAIN_CLASSES[entity]]
        decorators = {
            _dotted(d.func) if isinstance(d, ast.Call) else _dotted(d)
            for d in cls.decorator_list
        }
        assert "dataclass" in decorators, f"{entity} 应是 dataclass"

    from app.domain.agent import Agent
    from app.domain.channel import Channel
    from app.domain.connection import Connection
    from app.domain.identity import Identity
    from app.domain.user import User

    for entity in (User, Channel, Agent, Identity, Connection):
        assert not issubclass(entity, Base), f"{entity.__name__} 不应继承 ORM Base"


# ================================================================== B Application 边界


def test_b_services_uniformly_depend_on_uow_port() -> None:
    """五个 Service 结构统一：只依赖 Domain + Port，用例面 = create/get/update。"""
    for entity in ENTITIES:
        tree = _tree(_service_path(entity))
        roots = _roots(tree)
        leaked = [r for r in roots
                  if not any(r == p or r.startswith(p + ".") for p in SERVICE_ALLOWED_ROOTS)]
        assert not leaked, f"{entity} Service 越层导入：{leaked}"
        assert any(r == "app.repositories" or r.startswith("app.repositories.")
                   for r in roots), f"{entity} Service 应依赖 repositories 端口"

        classes = _classes(tree)
        service_names = [n for n in classes if n.endswith("Service")]
        assert service_names == [SERVICE_CLASSES[entity]], f"{entity} Service 类面漂移"

        assert f"Create{DOMAIN_CLASSES[entity]}" in classes
        assert f"Update{DOMAIN_CLASSES[entity]}" in classes

        use_cases = {n for n in _methods(classes[SERVICE_CLASSES[entity]])
                     if not n.startswith("_")}
        expected = {f"create_{entity}", f"get_{entity}", f"update_{entity}"}
        if entity == "agent":   # 11.3：Agent Service 补 List 用例
            expected.add("list_agents")
        if entity == "channel":  # 11.8：Channel Service 补 List 用例
            expected.add("list_channels")
        if entity == "connection":  # 11.11：Connection Service 补 List 用例
            expected.add("list_connections")
        if entity == "user":        # 12.4：User Service 补 List 用例
            expected.add("list_users")
        if entity == "identity":    # 12.4：Identity Service 补 List 用例
            expected.add("list_identities")
        assert use_cases == expected, \
            f"{entity} Service 用例面漂移：{use_cases}"


# ================================================================== C Repository 边界


def test_c_application_never_touches_orm() -> None:
    """Application 不直接碰 ORM / persistence。"""
    for py in sorted((APP_DIR / "application").rglob("*.py")):
        for r in _roots(_tree(py)):
            assert not (r == "app.persistence" or r.startswith("app.persistence.")), \
                f"{py.relative_to(BACKEND_DIR)} import 了 app.persistence：{r}"


def test_c_repositories_are_ports_only() -> None:
    """repositories/ 只有 Port Protocol（依赖 domain），无实现、无 SQLAlchemy。"""
    for entity in ENTITIES:
        tree = _tree(APP_DIR / "repositories" / f"{entity}.py")
        assert "Protocol" in (APP_DIR / "repositories" / f"{entity}.py").read_text(
            encoding="utf-8")
        roots = _roots(tree)
        leaked = [r for r in roots
                  if not (r == "typing" or r.startswith("typing.")
                          or r == "app.domain" or r.startswith("app.domain."))]
        assert not leaked, f"repositories/{entity}.py 越层导入：{leaked}"
        assert not any(r == "sqlalchemy" or r.startswith("sqlalchemy.")
                       for r in roots), f"repositories/{entity}.py 碰 SQLAlchemy"
    # UoW Port 只引用本层 Repository 端口
    uow = _tree(APP_DIR / "repositories" / "unit_of_work.py")
    assert not any(r == "sqlalchemy" or r.startswith("sqlalchemy.")
                   for r in _roots(uow)), "UoW Port 不应碰 SQLAlchemy"


# ================================================================== D Persistence 边界


def test_d_adapters_only_do_persistence() -> None:
    """Adapter 恰好实现 Port 方法面、只 flush 不 commit、不 import Application。"""
    for entity in ENTITIES:
        tree = _tree(APP_DIR / "persistence" / "repositories" / f"{entity}.py")
        roots = _roots(tree)
        forbidden = ("fastapi", "starlette", "app.application", "app.api",
                     "app.schemas", "app.adapters", "pydantic")
        leaked = [r for r in roots if any(r == f or r.startswith(f + ".")
                                          for f in forbidden)]
        assert not leaked, f"{entity} Adapter 越层导入：{leaked}"
        assert any(r == "app.repositories" or r.startswith("app.repositories.")
                   for r in roots), f"{entity} Adapter 应依赖端口"

        methods = {n.name for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and n.name != "__init__"}
        assert methods == EXPECTED_PORT_METHODS[entity], \
            f"{entity} Adapter 方法面与 Port 不一致：{methods}"
        assert not [n for n in ast.walk(tree)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "commit"], \
            f"{entity} Adapter 不应 commit（事务归 UoW）"


def test_d_mappers_convert_only_domain_and_model() -> None:
    """Mapper 只认识 Domain + ORM Model，不 import 端口/API/schemas。"""
    for entity in ENTITIES:
        roots = _roots(_tree(APP_DIR / "persistence" / "mappers" / f"{entity}.py"))
        forbidden = ("fastapi", "starlette", "app.repositories", "app.api",
                     "app.schemas", "app.application", "app.adapters")
        leaked = [r for r in roots if any(r == f or r.startswith(f + ".")
                                          for f in forbidden)]
        assert not leaked, f"mappers/{entity}.py 越层导入：{leaked}"
        assert any(r == "app.domain" or r.startswith("app.domain.") for r in roots), \
            f"mappers/{entity}.py 应依赖 domain"
        assert any(r == "app.persistence.models" or r.startswith("app.persistence.models.")
                   for r in roots), f"mappers/{entity}.py 应依赖 ORM models"


# ================================================================== E Transaction


def test_e_transaction_ownership_belongs_to_uow() -> None:
    """commit/rollback 只存在于 UoW；Service 只 uow.commit()；Adapter 只 flush。"""
    uow_src = (APP_DIR / "persistence" / "unit_of_work.py").read_text(encoding="utf-8")
    assert "def commit(self)" in uow_src and "def rollback(self)" in uow_src

    for entity in ENTITIES:
        tree = _tree(_service_path(entity))
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
                if n.func.attr == "commit":
                    assert _dotted(n.func.value) == "uow", \
                        f"{entity} 出现非 uow.commit()"
                if n.func.attr == "rollback":
                    pytest.fail(f"{entity} Service 不应自己 rollback")

        cls = _classes(tree)[SERVICE_CLASSES[entity]]
        for name, method in _methods(cls).items():
            if name.startswith("_"):
                continue
            opens = any(
                isinstance(n, ast.With)
                and isinstance(n.items[0].context_expr, ast.Call)
                and _dotted(n.items[0].context_expr.func) == "self._uow_factory"
                for n in ast.walk(method)
            )
            assert opens, f"{entity}.{name} 未打开 UoW"

    for entity in ENTITIES:
        src = (APP_DIR / "persistence" / "repositories" / f"{entity}.py").read_text(
            encoding="utf-8")
        tree = ast.parse(src, filename=f"{entity}.py")
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                    and n.func.attr in ("commit", "rollback"):
                pytest.fail(f"{entity} Adapter 不应 commit/rollback")


# ================================================================== F Input Contract


def test_f_input_contracts_are_application_dataclasses() -> None:
    """输入契约 = Application 层 frozen dataclass（Create*/Update*），无 pydantic/schema。"""
    for entity in ENTITIES:
        classes = _classes(_tree(_service_path(entity)))
        for prefix in ("Create", "Update"):
            node = classes[f"{prefix}{DOMAIN_CLASSES[entity]}"]
            frozen = any(
                isinstance(d, ast.Call) and _dotted(d.func).endswith("dataclass")
                and any(kw.arg == "frozen" and getattr(kw.value, "value", False)
                        for kw in d.keywords)
                for d in node.decorator_list
            )
            assert frozen, f"{entity} {prefix} 契约应是 frozen dataclass"

    for py in sorted((APP_DIR / "application").rglob("*.py")):
        for r in _roots(_tree(py)):
            assert not (r == "pydantic" or r.startswith("pydantic.")
                        or r == "app.schemas" or r.startswith("app.schemas.")), \
                f"{py.relative_to(BACKEND_DIR)} 引入 API schema：{r}"


# ================================================================== G Error Contract


def test_g_error_contract_is_application_level() -> None:
    """错误契约 = ApplicationError 体系；不泄漏 HTTP / SQLAlchemy。"""
    errors_py = APP_DIR / "application" / "errors.py"
    tree = _tree(errors_py)
    classes = _classes(tree)

    assert classes["ApplicationError"].bases, "缺 ApplicationError 基类"
    others = [n for n in classes if n != "ApplicationError"]
    assert others, "缺具体 Application 错误"
    for name in others:
        assert [_dotted(b) for b in classes[name].bases] == ["ApplicationError"], \
            f"{name} 未直接继承 ApplicationError"
    assert not _roots(tree), f"errors.py 不应 import：{_roots(tree)}"

    for entity in ENTITIES:
        for n in ast.walk(_tree(_service_path(entity))):
            if isinstance(n, ast.Raise) and n.exc is not None:
                name = _dotted(n.exc.func) if isinstance(n.exc, ast.Call) else _dotted(n.exc)
                assert name in classes, f"{entity} 抛出非 Application 错误：{name}"


# ================================================================== H UoW 暴露全部五仓储


def test_h_uow_exposes_all_five_repositories() -> None:
    """UoW Port 声明 + Adapter 接线，五个仓储一致。"""
    port = _tree(APP_DIR / "repositories" / "unit_of_work.py")
    uow_port = _classes(port)["UnitOfWork"]
    port_attrs = {s.target.id for s in uow_port.body
                  if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)}
    assert set(REPO_ATTRS.values()) <= port_attrs, "UoW Port 缺仓储属性"

    adapter = _tree(APP_DIR / "persistence" / "unit_of_work.py")
    assigned_attrs = {t.attr for n in ast.walk(adapter)
                      if isinstance(n, ast.Assign)
                      for t in n.targets
                      if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
                      and t.value.id == "self"}
    # __enter__ 与 close 都应覆盖全部五个仓储
    assert set(REPO_ATTRS.values()) <= assigned_attrs, "UoW Adapter 接线缺仓储"


# ================================================================== I 跨实体编排（orchestration ≠ 复制）


def test_i_cross_entity_orchestration_no_business_logic_replication() -> None:
    """Identity → User+Channel、Connection → Channel+Agent 都是编排，不是复制。"""
    for entity in ("identity", "connection"):
        spec = ORCHESTRATION[entity]
        tree = _tree(_service_path(entity))
        calls = _calls(tree)

        for r in spec["reads"]:
            assert r in calls, f"{entity} 未做存在性编排：{r}"
        for w in spec["writes"]:
            assert w in calls, f"{entity} 未写自身实体：{w}"
        for bad in spec["must_not_write"]:
            assert bad not in calls, f"{entity} 不应复制 {bad}（那是被依赖实体的事）"

        # 不能构造被依赖实体的领域对象
        for klass in spec["must_not_construct"]:
            assert not any(c == klass for c in calls), \
                f"{entity} 不应构造 {klass}（编排而非复制）"


# ================================================================== J Connection 特有边界


def test_j_connection_identity_is_binding_pair() -> None:
    """(channel_id, agent_id) = Access Path 身份；name 不是唯一身份。"""
    from app.persistence import models  # noqa: F401  # 注册 8 张表

    connections = Base.metadata.tables["connections"]
    assert any(u.name == "uq_connections_tenant_channel_agent_id"
               for u in connections.constraints), "UNIQUE(tenant_id, channel_id, agent_id) 漂移"
    unique_cols = {c.name for u in connections.constraints
                   if isinstance(u, __import__("sqlalchemy").UniqueConstraint)
                   for c in u.columns}
    assert {"tenant_id", "channel_id", "agent_id"} <= unique_cols
    assert "name" not in unique_cols, "name 不应是唯一身份"


def test_j_connection_binding_is_immutable() -> None:
    """绑定不可变：UpdateConnection 无绑定字段；apply_to_orm 不覆写绑定。"""
    classes = _classes(_tree(_service_path("connection")))
    fields = {
        stmt.target.id for stmt in classes["UpdateConnection"].body
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
    }
    assert fields == {"name", "status", "config", "credential_ref"}, \
        f"UpdateConnection 漂移：{fields}"

    src = (APP_DIR / "persistence" / "mappers" / "connection.py").read_text(encoding="utf-8")
    tree = ast.parse(src, filename="connection.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Attribute) and t.attr in ("channel_id", "agent_id"):
                    pytest.fail("apply_to_orm 不得覆写绑定 channel_id/agent_id")


def test_j_connection_name_not_unique_identity() -> None:
    """name 是显示名：Port 无 find_by_name；schema 无 name 唯一约束（J 已含 schema）。"""
    port_methods = {n.name for n in ast.walk(_tree(APP_DIR / "repositories" / "connection.py"))
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and n.name != "__init__"}
    assert port_methods == EXPECTED_PORT_METHODS["connection"]
    assert not any("name" in m for m in port_methods), "name 不应承担唯一性"


def test_j_connection_credential_ref_is_reference_only() -> None:
    """credential_ref 只存引用（"cred_xxx"），不是 credential 本身。"""
    cls = _classes(_tree(APP_DIR / "domain" / "connection.py"))["Connection"]
    field_names = {
        stmt.target.id for stmt in cls.body
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
    }
    # 唯一承载凭据的字段是 credential_ref
    assert "credential_ref" in field_names
    # 不得出现承载凭据本体的字段
    assert not (field_names & {"credential", "token", "password", "secret", "api_key"})


def test_j_connection_config_is_not_policy_container() -> None:
    """config 不是 policy/capability/routing 万能容器。"""
    cls = _classes(_tree(APP_DIR / "domain" / "connection.py"))["Connection"]
    field_names = {
        stmt.target.id for stmt in cls.body
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
    }
    forbidden = {"permissions", "capabilities", "policy", "routing_rules",
                 "users", "tools", "memory", "roles", "scopes"}
    assert not (field_names & forbidden), f"Access Control 字段混入：{field_names & forbidden}"


# ================================================================== K 反向审计 + 变异探测


# 9.3：Session 已落地（domain/session.py + Port/Mapper/Adapter + UoW），
# 因此 "session" 不再属于"下一阶段"——它现在是已交付实体。
NEXT_STAGE_FILE_STEMS = {
    "interaction", "activity", "router", "dispatcher",
    "resolver", "capability", "policy", "authorization", "adapter",
}
NEXT_STAGE_NAME_TOKENS = (
    "interaction", "router", "dispatcher", "resolver",
    "capability", "policy", "authorization",
)

# 已冻结的 Runtime 契约（精确白名单：只允许出现在指定文件 + 指定类名）。
# 除此之外任何 "interaction/session/..." 命名的类、函数、文件仍然是偷渡。
# - 8.1 InboundInteraction（Runtime 入口契约，domain 值对象）
# - 8.3 SessionResolution（Session Resolution 契约，domain 值对象；不创建
#   Session 实体、不实现 SessionResolver）
RUNTIME_CONTRACTS = {
    ("app/domain/inbound_interaction.py", "InboundInteraction"),
    ("app/domain/session_resolution.py", "SessionResolution"),
    # Cloud-0.1.1-B：TenantResolver = 入口租户解析端口（Protocol；类名含 "resolver"
    # 但属 ingress 边界契约，非 Runtime 决策组件——精确白名单放行）
    ("app/core/tenant.py", "TenantResolver"),
    # Package 2 / Step 2：Access Authorization 是**已交付阶段**（ADR-3/7/8），
    # 不是"下一阶段"。它含 "authorization" 一词，但语义是 Conversation-scoped
    # 访问判定（只有许可集合），不是权限体系（无 effect / role / principal /
    # 条件组合 / 继承）。三处精确放行；token 仍留在 NEXT_STAGE_* 集合里——
    # 任何**新增**的 authorization 命名仍会被判为偷渡。
    ("app/domain/access_authorization.py", "AccessAuthorization"),
    ("app/application/access_authorization.py", "AccessAuthorizationExecutor"),
    ("app/application/errors.py", "AccessAuthorizationDeniedError"),
    # 10.4.2 Interaction 持久化：记录域镜像（≠ 8.1 契约）及其 Port/Adapter
    ("app/domain/interaction_record.py", "InteractionRecord"),
    ("app/repositories/interaction_record.py", "InteractionRecordRepository"),
    ("app/persistence/repositories/interaction_record.py",
     "SqlAlchemyInteractionRecordRepository"),
    # 13.1 Interaction Observability 只读面（Service 用例 / 读方法 / API 端点 /
    # Schema 投影——纯读，不拥有任何执行/状态变更语义）
    ("app/api/management/interactions.py", "build_interactions_api"),
    ("app/api/management/interactions.py", "list_interactions"),
    ("app/api/management/interactions.py", "get_interaction"),
    ("app/application/interaction_observability.py",
     "InteractionObservabilityService"),
    ("app/application/interaction_observability.py", "list_interactions"),
    ("app/application/interaction_observability.py", "get_interaction"),
    ("app/persistence/repositories/activity_record.py", "list_by_interaction"),
    ("app/repositories/activity_record.py", "list_by_interaction"),
    ("app/schemas/interactions.py", "InteractionSummaryResponse"),
    ("app/schemas/interactions.py", "InteractionDetailResponse"),
    ("app/schemas/interactions.py", "InteractionListResponse"),
}


def _is_frozen_runtime_contract(py: Path, class_name: str) -> bool:
    rel = "/".join(py.relative_to(BACKEND_DIR).parts)
    return (rel, class_name) in RUNTIME_CONTRACTS


def _next_stage_problems() -> list[str]:
    """扫描磁盘，返回所有"下一阶段实体/组件被提前实现"的问题。

    8.1 起：InboundInteraction（domain 值对象，Runtime 入口契约）属于**已冻结**
    的边界，白名单放行；其余 Interaction/Session/Resolver/Router/... 仍未到。
    """
    problems: list[str] = []
    layer_files = _entity_layer_files()
    for layer, files in layer_files.items():
        hit = files & NEXT_STAGE_FILE_STEMS
        if hit:
            problems.append(f"{layer}: {sorted(hit)}")

    for py in sorted(APP_DIR.rglob("*.py")):
        if (APP_DIR / "persistence" / "models") in py.parents:
            continue
        tree = _tree(py)
        for node in ast.walk(tree):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                if _is_frozen_runtime_contract(py, node.name):
                    continue
                lower = node.name.lower()
                if any(tok in lower for tok in NEXT_STAGE_NAME_TOKENS):
                    problems.append(f"{py.relative_to(BACKEND_DIR)}::{node.name}")
    return problems


def test_k_reverse_audit_clean() -> None:
    """反向审计汇总（综合版）：五个 Management 对象 + 8.1 契约已交付，其余未偷渡。"""
    problems = _next_stage_problems()
    assert not problems, "下一阶段实体/组件被提前实现：\n" + "\n".join(problems)

    # 9.x：runtime.py = Runtime Execution Boundary；identity_resolution.py =
    # 9.2 Identity Resolution Execution（首个 Runtime 执行组件，非 Management）
    app_files = sorted(p.stem for p in (APP_DIR / "application").glob("*.py")
                       if p.stem != "__init__")
    # M6.6-C3 / ADR-12: + bootstrap（执行边界组件）/ bootstrap_arms（管理面用例）
    assert app_files == ["access_authorization",
                         "agent_dispatch",
                         # M7-AUI-10.5：WorkBuddy External Agent Identity 语义/查重
                         "agent_identity",
                         "agents", "bootstrap", "bootstrap_arms",
                         "browser_auth", "channel_inbound",
                         "channels", "connection_conversations", "connections",
                         "coordinator", "delivery",
                         "driver", "errors", "identities", "identity_resolution",
                         "interaction_observability", "interaction_recording",
                         "routing", "runtime", "session_creation",
                         "session_resolution", "tenant_fanout",
                         "tenant_provisioning", "users", "worker"], \
        f"application 文件面漂移：{app_files}"  # 12.9.4.5: + worker / 12.9.4.16: + driver / 13.1: + interaction_observability / C: + browser_auth / Cloud-0.1.2.1: + tenant_provisioning / M7-AUI-10.5: + agent_identity


def test_k_reverse_audit_mutation_catches_smuggling() -> None:
    """变异探测：注入偷渡文件后，反向审计必须报红；证明审计不是永远返回空。"""
    injected: list[Path] = []
    try:
        # 1) 以一个 premature entity 文件名偷渡
        #    （9.3 起 Session 已交付，改用仍属未来态的 interaction 验证守卫）
        f1 = APP_DIR / "domain" / "interaction.py"
        f1.write_text("class Interaction:\n    pass\n")
        injected.append(f1)
        problems = _next_stage_problems()
        assert any("interaction" in p.lower() for p in problems), \
            f"反向审计未抓到 interaction 偷渡：{problems}"
        f1.unlink()
        injected.remove(f1)

        # 2) 文件名无辜、但类名是 forbidden 组件（伪装偷渡）
        f2 = APP_DIR / "domain" / "helper.py"
        f2.write_text("class Policy:\n    def resolve(self) -> None:\n        pass\n")
        injected.append(f2)
        problems = _next_stage_problems()
        assert any("Policy" in p for p in problems), \
            f"反向审计未抓到 Policy 偷渡：{problems}"
        f2.unlink()
        injected.remove(f2)

        # 3) 在契约白名单之外重声明 InboundInteraction（逃逸白名单 = 偷渡）
        f3 = APP_DIR / "domain" / "extra.py"
        f3.write_text("class InboundInteraction:\n    pass\n")
        injected.append(f3)
        problems = _next_stage_problems()
        assert any("InboundInteraction" in p for p in problems), \
            f"反向审计未抓到白名单逃逸：{problems}"
    finally:
        for f in injected:
            if f.exists():
                f.unlink()
