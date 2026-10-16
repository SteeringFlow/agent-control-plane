"""Step 6.7 —— Management Closure Audit（Management Foundation 阶段冻结）。

Agent / Channel / User / Identity 四个 Management Slice 全部完成后，
把整个 Management Foundation 冻结为 **Connection-ready**。

本审计 = 正向一致性（已经实现的东西是对的）+ **反向审计**
（下一阶段的东西确实没有被偷偷提前实现）：

    Step 6 Management
       ├── Agent ✅
       ├── Channel ✅
       ├── User ✅
       └── Identity ✅
              │
              X  ← 反向审计锁定
       Connection / Session / Interaction / Router / Dispatcher / Resolver
       RBAC / Capability / Policy / Gateway / Runtime

（ORM models 层除外——8 张表在 4.2/5.3 已冻结，models/ 存在是合法的。）

正向锁定项：
A  Domain 边界（实体互不越界、纯 dataclass）
B  Application 边界（四个 Service 统一依赖 UoW Port）
C  Repository 边界（Application 不直接碰 ORM）
D  Persistence 边界（Adapter 只负责持久化实现）
E  Transaction（UoW 统一 commit/rollback）
F  Input Contract（Application dataclass，无 API schema 侵入）
G  Error Contract（ApplicationError，不泄漏 HTTP/SQLAlchemy）
H  Identity Boundary（(channel_id, external_id) 唯一键）
I  Identity Linking 不存在（无自动 linking/merge/fuzzy match）
反向锁定项：
J  Connection 未实现
K  Runtime Boundary（Resolver / Router / Dispatcher 不存在）
L  API Boundary（Management API 未提前侵入）
M  Scope（无 RBAC / Capability / Policy / Gateway infra 偷渡）
"""

import ast
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
APP_DIR = BACKEND_DIR / "app"

# 四个已交付 Management Slice
SLICE_ENTITIES = ("agent", "channel", "user", "identity")
DOMAIN_CLASSES = {"agent": "Agent", "channel": "Channel", "user": "User", "identity": "Identity"}
SERVICE_CLASSES = {
    "agent": "AgentApplicationService",
    "channel": "ChannelApplicationService",
    "user": "UserApplicationService",
    "identity": "IdentityApplicationService",
}
SERVICE_FILES = {
    "agent": "agents.py", "channel": "channels.py",
    "user": "users.py", "identity": "identities.py",
}
REPO_ATTRS = {"agent": "agents", "channel": "channels", "user": "users", "identity": "identities"}
EXPECTED_PORT_METHODS = {
    "agent": {"get_by_id", "create", "update", "find_by_name", "list_agents"},
    # 11.8：+ list_channels（Channel Management API 的 List 用例）
    # Cloud-0.1.3.1：+ find_tenant_id_by_id（入站路由 hint → 已认证 Channel 的 tenant_id）
    "channel": {"get_by_id", "create", "update", "find_by_type_and_name",
                "list_channels", "find_tenant_id_by_id"},
    # 12.4：+ list_users（User Management API 的 List 用例）
    "user": {"get_by_id", "create", "update", "list_users"},
    # 12.4：+ list_identities（Identity Management API 的 List 用例）
    # M6.6-C3 / ADR-12 §11：+ find_or_create（Bootstrap 事务内的幂等建立）
    "identity": {"get_by_id", "create", "update", "find_by_channel_and_external_id",
                 "list_identities", "find_or_create"},
}

SERVICE_ALLOWED_ROOTS = (
    "app.domain", "app.repositories", "app.application",
    "collections", "dataclasses", "typing",
)
SERVICE_FORBIDDEN_ROOTS = (
    "sqlalchemy", "fastapi", "starlette", "pydantic",
    "app.persistence", "app.api", "app.schemas", "app.adapters",
)
INFRA_NAMES = {
    "Session", "session", "sessionmaker", "create_engine", "Engine", "SessionLocal",
    "SqlAlchemyUnitOfWork", "SqlAlchemyAgentRepository", "SqlAlchemyChannelRepository",
    "SqlAlchemyUserRepository", "SqlAlchemyIdentityRepository",
    "AgentModel", "ChannelModel", "UserModel", "IdentityModel",
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


def _port_methods(entity: str) -> set[str]:
    tree = _tree(APP_DIR / "repositories" / f"{entity}.py")
    return {n.name for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name != "__init__"}


def _entity_layer_files() -> dict[str, set[str]]:
    """各业务层已实现的实体文件名集合（不含 models，models 8 表在 4.2 冻结）。"""
    result: dict[str, set[str]] = {}
    for layer in ("domain", "repositories", "application",
                  "persistence/mappers", "persistence/repositories"):
        result[layer] = {p.stem for p in (APP_DIR / layer).glob("*.py")
                         if p.stem != "__init__"}
    return result


# ================================================================== A Domain 边界


def test_a_domain_entities_are_isolated_pure_dataclasses() -> None:
    """四个实体相互不 import、不越界；都是纯 dataclass，不继承 ORM。"""
    from app.persistence.database import Base

    for name in SLICE_ENTITIES:
        py = APP_DIR / "domain" / f"{name}.py"
        # 每个实体模块只 import 标准库 —— 实体之间、与任何 app 层零耦合。
        # 11.3：agent 补 created_at/updated_at 只读审计回显 → 额外允许 datetime
        #（与 Session 实体同一先例）。
        allowed_roots = {"dataclasses", "datetime"} if name == "agent" \
            else {"dataclasses"}
        roots = _roots(_tree(py))
        assert roots == allowed_roots, f"domain/{name}.py 越层导入：{roots}"

        cls = _classes(_tree(py))[DOMAIN_CLASSES[name]]
        decorators = {
            _dotted(d.func) if isinstance(d, ast.Call) else _dotted(d)
            for d in cls.decorator_list
        }
        assert "dataclass" in decorators, f"{name} 应是 dataclass"

    # 运行时：不继承 ORM Base
    from app.domain.agent import Agent
    from app.domain.channel import Channel
    from app.domain.identity import Identity
    from app.domain.user import User

    for entity in (User, Channel, Agent, Identity):
        assert not issubclass(entity, Base), f"{entity.__name__} 不应继承 ORM Base"


# ================================================================== B Application 边界


def test_b_four_services_uniformly_depend_on_uow_port() -> None:
    """四个 Service 结构统一：只依赖 Domain + Port（UoW），方法面 = create/get/update。"""
    for entity in SLICE_ENTITIES:
        py = _service_path(entity)
        tree = _tree(py)
        roots = _roots(tree)

        leaked = [r for r in roots
                  if not any(r == p or r.startswith(p + ".") for p in SERVICE_ALLOWED_ROOTS)]
        assert not leaked, f"{entity} Service 越层导入：{leaked}"
        assert any(r == "app.repositories" or r.startswith("app.repositories.")
                   for r in roots), f"{entity} Service 应依赖 repositories 端口"

        # 结构面：一个 *ApplicationService + Create* + Update*
        classes = _classes(tree)
        service_names = [n for n in classes if n.endswith("Service")]
        assert service_names == [SERVICE_CLASSES[entity]], f"{entity} Service 类面漂移"
        assert f"Create{DOMAIN_CLASSES[entity]}" in classes
        assert f"Update{DOMAIN_CLASSES[entity]}" in classes

        # 用例面：create_*/get_*/update_*（11.3：agent 另有 list_agents；
        # 11.8：channel 另有 list_channels；12.4：user/identity 另有 list_*）
        use_cases = {n for n in _methods(classes[SERVICE_CLASSES[entity]])
                     if not n.startswith("_")}
        expected = {f"create_{entity}", f"get_{entity}", f"update_{entity}"}
        if entity == "agent":
            expected.add("list_agents")
        if entity == "channel":
            expected.add("list_channels")
        if entity == "user":
            expected.add("list_users")
        if entity == "identity":
            expected.add("list_identities")
        assert use_cases == expected, f"{entity} Service 用例面漂移：{use_cases}"


# ================================================================== C Repository 边界


def test_c_application_never_touches_orm() -> None:
    """Application 不直接碰 ORM / persistence：不 import 任何 app.persistence。"""
    for py in sorted((APP_DIR / "application").rglob("*.py")):
        for r in _roots(_tree(py)):
            assert not (r == "app.persistence" or r.startswith("app.persistence.")), \
                f"{py.relative_to(BACKEND_DIR)} import 了 app.persistence：{r}"
            assert not (r == "app.models" or r.startswith("app.models.")), \
                f"{py.relative_to(BACKEND_DIR)} import 了 app.models：{r}"


def test_c_repositories_are_ports_protocols_only() -> None:
    """repositories/ 只有 Port Protocol（依赖 domain），无实现、无 SQLAlchemy。"""
    for entity in SLICE_ENTITIES:
        py = APP_DIR / "repositories" / f"{entity}.py"
        tree = _tree(py)
        assert "Protocol" in py.read_text(encoding="utf-8")
        roots = _roots(tree)
        # Port 模块只 import typing（Protocol）+ app.domain（对应实体）
        leaked = [r for r in roots
                  if not (r == "typing" or r.startswith("typing.")
                          or r == "app.domain" or r.startswith("app.domain."))]
        assert not leaked, f"repositories/{entity}.py 越层导入：{leaked}"
        assert not any(r == "sqlalchemy" or r.startswith("sqlalchemy.")
                       for r in roots), f"repositories/{entity}.py 碰 SQLAlchemy"

    # UoW Port 只引用本层 Repository 端口
    uow = _tree(APP_DIR / "repositories" / "unit_of_work.py")
    roots = _roots(uow)
    assert not any(r == "sqlalchemy" or r.startswith("sqlalchemy.")
                   for r in roots), "UoW Port 不应碰 SQLAlchemy"
    assert any(r == "app.repositories" or r.startswith("app.repositories.")
               for r in roots), "UoW Port 应引用仓库端口"


# ================================================================== D Persistence 边界


def test_d_adapters_only_do_persistence() -> None:
    """Adapter 恰好实现 Port 方法面、只 flush 不 commit、不 import Application。"""
    for entity in SLICE_ENTITIES:
        py = APP_DIR / "persistence" / "repositories" / f"{entity}.py"
        tree = _tree(py)
        roots = _roots(tree)
        forbidden = ("fastapi", "starlette", "app.application", "app.api",
                     "app.schemas", "app.adapters", "pydantic")
        leaked = [r for r in roots if any(r == f or r.startswith(f + ".") for f in forbidden)]
        assert not leaked, f"{entity} Adapter 越层导入：{leaked}"
        assert any(r == "app.repositories" or r.startswith("app.repositories.")
                   for r in roots), f"{entity} Adapter 应依赖端口"

        methods = {n.name for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and n.name != "__init__"}
        assert methods == EXPECTED_PORT_METHODS[entity], \
            f"{entity} Adapter 方法面与 Port 不一致：{methods}"

        commits = [n for n in ast.walk(tree)
                   if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                   and n.func.attr == "commit"]
        assert not commits, f"{entity} Adapter 不应 commit（事务归 UoW）"


def test_d_mappers_convert_only_domain_and_model() -> None:
    """Mapper 只认识 Domain + ORM Model，不 import 端口/API/schemas。"""
    for entity in SLICE_ENTITIES:
        py = APP_DIR / "persistence" / "mappers" / f"{entity}.py"
        roots = _roots(_tree(py))
        forbidden = ("fastapi", "starlette", "app.repositories", "app.api",
                     "app.schemas", "app.application", "app.adapters")
        leaked = [r for r in roots if any(r == f or r.startswith(f + ".") for f in forbidden)]
        assert not leaked, f"mappers/{entity}.py 越层导入：{leaked}"
        assert any(r == "app.domain" or r.startswith("app.domain.")
                   for r in roots), f"mappers/{entity}.py 应依赖 domain"
        assert any(r == "app.persistence.models" or r.startswith("app.persistence.models.")
                   for r in roots), f"mappers/{entity}.py 应依赖 ORM models"


# ================================================================== E Transaction


def test_e_transaction_ownership_belongs_to_uow() -> None:
    """commit/rollback 只存在于 UoW；Service 只 uow.commit()；Adapter 只 flush。"""
    uow_src = (APP_DIR / "persistence" / "unit_of_work.py").read_text(encoding="utf-8")
    assert "def commit(self)" in uow_src and "def rollback(self)" in uow_src

    # Service 的 commit 唯一形式 uow.commit()；且不自己 rollback
    for entity in SLICE_ENTITIES:
        tree = _tree(_service_path(entity))
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
                if n.func.attr == "commit":
                    assert _dotted(n.func.value) == "uow", \
                        f"{entity} 出现非 uow.commit()"
                if n.func.attr == "rollback":
                    pytest.fail(f"{entity} Service 不应自己 rollback")

        # 每个用例方法都打开 UoW 并（写用例）commit
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

    # Repository Adapter 无 commit（源码级，含 identity）
    for entity in SLICE_ENTITIES:
        src = (APP_DIR / "persistence" / "repositories" / f"{entity}.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(src, filename=f"{entity}.py")
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                    and n.func.attr in ("commit", "rollback"):
                pytest.fail(f"{entity} Adapter 不应 commit/rollback")


# ================================================================== F Input Contract


def test_f_input_contracts_are_application_dataclasses() -> None:
    """输入契约 = Application 层 frozen dataclass（Create*/Update*），无 pydantic/schema。"""
    for entity in SLICE_ENTITIES:
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

    # Application 层绝不出现 pydantic / app.schemas
    for py in sorted((APP_DIR / "application").rglob("*.py")):
        for r in _roots(_tree(py)):
            assert not (r == "pydantic" or r.startswith("pydantic.")
                        or r == "app.schemas" or r.startswith("app.schemas.")), \
                f"{py.relative_to(BACKEND_DIR)} 引入 API schema：{r}"


# ================================================================== G Error Contract


def test_g_error_contract_is_application_level() -> None:
    """错误契约 = ApplicationError 体系；不泄漏 HTTP / SQLAlchemy。"""
    errors_py = APP_DIR / "application" / "errors.py"
    errors_src = errors_py.read_text(encoding="utf-8")
    tree = _tree(errors_py)
    classes = _classes(tree)

    assert classes["ApplicationError"].bases, "缺 ApplicationError 基类"
    others = [n for n in classes if n != "ApplicationError"]
    assert others, "缺具体 Application 错误"
    for name in others:
        assert [_dotted(b) for b in classes[name].bases] == ["ApplicationError"], \
            f"{name} 未直接继承 ApplicationError"
    # errors.py 自身零 import（纯语义）
    assert not _roots(tree), f"errors.py 不应 import：{_roots(tree)}"

    # 每个 Service 抛出的只能是 Application 错误
    for entity in SLICE_ENTITIES:
        tree2 = _tree(_service_path(entity))
        for n in ast.walk(tree2):
            if isinstance(n, ast.Raise) and n.exc is not None:
                name = _dotted(n.exc.func) if isinstance(n.exc, ast.Call) else _dotted(n.exc)
                assert name in classes, f"{entity} 抛出非 Application 错误：{name}"


# ================================================================== H Identity Boundary


def test_h_identity_uniqueness_is_channel_scoped() -> None:
    """(channel_id, external_id) 唯一键：结构冻结 + Port 语义 + 契约不改绑定。"""
    from app.persistence import models  # noqa: F401  # 注册 8 张表
    from app.persistence.database import Base

    identities = Base.metadata.tables["identities"]
    assert any(u.name == "uq_identities_tenant_channel_external_id"
               for u in identities.constraints), "UNIQUE(tenant_id, channel_id, external_id) 漂移"

    # Identity Port 的查找方法只有唯一键查找（无裸 external_id / email 全局查找）
    finders = {m for m in _port_methods("identity") if m.startswith("find_")}
    # M6.6-C3 / ADR-12：`find_or_create` 仍是**唯一键**查找（命中复用、未命中才创建），
    # 不引入裸 external_id / email 全局查找。
    assert finders == {"find_by_channel_and_external_id", "find_or_create"}, \
        f"查找方法面漂移：{finders}"

    # UpdateIdentity 只含档案字段，不含绑定骨架
    classes = _classes(_tree(_service_path("identity")))
    fields = {
        stmt.target.id for stmt in classes["UpdateIdentity"].body
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
    }
    assert fields == {"display_name", "status", "metadata"}, f"UpdateIdentity 漂移：{fields}"


# ================================================================== I Identity Linking 不存在


# 10.8.4 精确放行：bind_external_session 绑定的是 **Agent 侧会话引用**，
# 与 Identity Linking（身份合并 / 匹配 / 解析）无关——不是 linking 能力。
SESSION_BINDING_ALLOWLIST = {
    ("app/repositories/session.py", "bind_external_session"),
    ("app/persistence/repositories/session.py", "bind_external_session"),
}

# Package 2 / Step 1 精确放行：`find_by_binding` 按 (channel_id,
# external_conversation_id, connection_id) 查**一条 Conversation → Connection
# 授权**——它绑定的是「会话键 ↔ 访问路径」，完全不触碰 Identity / User，
# 既不做身份合并、也不做跨渠道匹配，因此**不是** Identity Linking 能力。
# （token 扫描命中 "bind" 属假阳性；与本文件 10.8.4 的处理方式一致：
#   显式放行 + 说明理由，而不是为绕开守卫改名。）
CONVERSATION_ACCESS_BINDING_ALLOWLIST = {
    ("app/repositories/connection_conversation.py", "find_by_binding"),
    ("app/persistence/repositories/connection_conversation.py", "find_by_binding"),
}

# M6.6-F2 精确放行：WeCom Bot transport 的**运行期调和**内部符号——`_Binding`
# 表示「一条 channel（channel_id = 绑定身份）↔ 其 WS 长连接」的进程内运行时装，
# `_new_binding` / `_launch_binding` / `_teardown_binding` 分别做装配 / 启动 /
# 停止+清理。它们**完全不触碰 Identity / User**（既不合并身份、也不做跨渠道
# 匹配 / 解析），与 Identity Linking 无关——token 扫描命中 "bind" 属假阳性。
# 处理方式与上一条（`find_by_binding`）及 10.8.4（`bind_external_session`）
# 完全一致：显式放行 + 说明理由，而不是为绕开守卫改名。
WECOM_BOT_BINDING_ALLOWLIST = {
    ("app/adapters/channels/wecom_bot.py", "_Binding"),
    ("app/adapters/channels/wecom_bot.py", "_new_binding"),
    ("app/adapters/channels/wecom_bot.py", "_launch_binding"),
    ("app/adapters/channels/wecom_bot.py", "_teardown_binding"),
}


def test_i_no_identity_linking_capability() -> None:
    """当前无自动 linking / merge / fuzzy match / resolver 的任何能力。"""
    tokens = ("link", "merge", "match", "resolve", "bind", "unify", "dedup")
    # Cloud-0.1.1-B：TenantResolver = 入口租户解析端口（Protocol），不是 Identity Linking。
    TENANT_INGRESS_ALLOWLIST = {("app/core/tenant.py", "TenantResolver"),
                                ("app/core/tenant.py", "resolve")}
    problems: list[str] = []
    for py in sorted(APP_DIR.rglob("*.py")):
        if (APP_DIR / "persistence" / "models") in py.parents:
            continue  # models 层是冻结 schema，跳过
        rel = py.relative_to(BACKEND_DIR).as_posix()
        tree = _tree(py)
        for node in ast.walk(tree):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                if (rel, node.name) in SESSION_BINDING_ALLOWLIST:
                    continue
                if (rel, node.name) in CONVERSATION_ACCESS_BINDING_ALLOWLIST:
                    continue
                if (rel, node.name) in WECOM_BOT_BINDING_ALLOWLIST:
                    continue
                if (rel, node.name) in TENANT_INGRESS_ALLOWLIST:
                    continue
                lower = node.name.lower()
                if any(tok in lower for tok in tokens):
                    problems.append(f"{py.relative_to(BACKEND_DIR)}::{node.name}")
        # UserRepository 不能按 email/phone/name 查用户（那是 linking 的抓手）
        if py == APP_DIR / "repositories" / "user.py":
            methods = {n.name for n in ast.walk(tree)
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
            assert not any("email" in m or "phone" in m for m in methods), \
                f"User Port 出现 linking 抓手：{methods}"
    assert not problems, "出现 Identity Linking 能力：\n" + "\n".join(problems)


# ================================================================== J Connection 未实现


def test_j_connection_management_layer_complete_runtime_not() -> None:
    """7.1 契约层 + 7.2 Application 层已交付；Runtime/Adapter/API 仍未交付。

    Connection 的 Service 叫 ConnectionApplicationService（application 层合法），
    但不存在 ConnectionService / ConnectionManager / ConnectionResolver 之类的
    运行时组件；adapters/ 仍空壳；Session/Interaction 仍不落地。
    """
    # 契约层 + Application 层已交付
    assert (APP_DIR / "domain" / "connection.py").is_file()
    assert (APP_DIR / "repositories" / "connection.py").is_file()
    assert (APP_DIR / "persistence" / "mappers" / "connection.py").is_file()
    assert (APP_DIR / "persistence" / "repositories" / "connection.py").is_file()
    assert (APP_DIR / "application" / "connections.py").is_file()

    # 无运行时类（非 models 层）
    forbidden = {"ConnectionService", "ConnectionManager", "ConnectionResolver",
                 "SessionResolver"}
    for py in sorted(APP_DIR.rglob("*.py")):
        if (APP_DIR / "persistence" / "models") in py.parents:
            continue
        tree = _tree(py)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name in forbidden:
                pytest.fail(f"{py.relative_to(BACKEND_DIR)}::{node.name} 提前实现")


# ================================================================== K Runtime Boundary


def test_k_no_resolver_router_dispatcher() -> None:
    """Resolver / Router / Dispatcher 类、方法、文件、导入全不存在。

    8.4 边界扩边：`RoutingResult`（domain 值对象 = Routing 输出契约）随 8.4
    **主动冻结**，被精确白名单放行；8.5 再放行 `dispatch_failure`（Agent Dispatch
    失败码常量文件，不含任何 Dispatcher 类）。除此之外的 Router / RoutingRule /
    Dispatcher / Resolver 等仍属偷渡。
    """
    # 已冻结契约 / 执行组件（精确到文件 + 类名）：
    # 8.4 RoutingResult（Routing 输出契约）；8.5 dispatch_failure（Agent
    # Dispatch 失败码文件——不含任何 Dispatcher 类，只是失败码常量）；
    # 9.4 RoutingExecutor + RoutingFailedError（Routing **执行**组件与 Fail-Closed
    # 错误——是 Execution，不是 Router / 规则引擎）。
    ROUTING_CONTRACT_FILE = "app/domain/routing_result.py"
    ROUTING_CONTRACT_CLASS = "RoutingResult"
    DISPATCH_FAILURE_FILE = "app/domain/dispatch_failure.py"
    RUNTIME_ALLOWED_CLASSES = {
        ("app/application/routing.py", "RoutingExecutor"),
        ("app/application/errors.py", "RoutingFailedError"),
        # 9.5 Agent Dispatch Execution（是 Execution，不是 Dispatcher）
        ("app/application/agent_dispatch.py", "AgentDispatchExecutor"),
        ("app/application/errors.py", "AgentDispatchFailedError"),
        # Cloud-0.1.1-B：TenantResolver = 入口租户解析端口（Protocol，非 Runtime 组件）
        ("app/core/tenant.py", "TenantResolver"),
    }
    ALLOWED_IMPORT_ROOTS = {"app.domain.routing_result",
                            "app.application.routing",
                            "app.application.agent_dispatch"}

    tokens = ("resolver", "router", "dispatcher", "routing", "dispatch")
    problems: list[str] = []
    for py in sorted(APP_DIR.rglob("*.py")):
        rel = "/".join(py.relative_to(BACKEND_DIR).parts)
        tree = _tree(py)
        for r in _roots(tree):
            # 允许 import 8.4 冻结的 Routing 契约 / 9.4 Routing Execution 模块
            if r in ALLOWED_IMPORT_ROOTS:
                continue
            if any(tok in r.lower() for tok in ("resolver", "router", "dispatcher", "routing")):
                problems.append(f"{py.relative_to(BACKEND_DIR)} imports {r}")
        for node in ast.walk(tree):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                if rel == ROUTING_CONTRACT_FILE and node.name == ROUTING_CONTRACT_CLASS:
                    continue
                if (rel, node.name) in RUNTIME_ALLOWED_CLASSES:
                    continue
                if any(tok in node.name.lower() for tok in tokens):
                    problems.append(f"{py.relative_to(BACKEND_DIR)}::{node.name}")
    assert not problems, "Runtime 组件提前出现：\n" + "\n".join(problems)

    # 无路由/分发文件（目录/占位 __init__.py 与已冻结契约/执行文件除外）
    contract_files = {ROUTING_CONTRACT_FILE, DISPATCH_FAILURE_FILE,
                      "app/application/routing.py",
                      "app/application/agent_dispatch.py"}
    for py in sorted(APP_DIR.rglob("*.py")):
        stem = py.stem.lower()
        if stem == "__init__":
            continue
        if "/".join(py.relative_to(BACKEND_DIR).parts) in contract_files:
            continue
        assert not any(tok in stem for tok in ("_router", "_dispatcher", "_resolver",
                                               "routing", "dispatch")), f"文件提前出现：{py}"


# ================================================================== L API Boundary


def test_l_management_api_boundary() -> None:
    """11.3 起 Management API 已引入（agents.py），边界随之收口：
    api/management 只经 build_agents_api 消费 Application Service + schemas，
    绝不 import persistence / SQLAlchemy。"""
    files = sorted(p.name for p in (APP_DIR / "api" / "management").glob("*.py"))
    # 11.3 agents.py；11.8 channels.py；11.11 connections.py；12.4 users/identities；
    # 13.1 interactions.py（Observability 只读面）
    # M6.6-C3 / ADR-12: + bootstrap_arms.py（Verify Connection / Arm 端点）
    assert files == ["__init__.py", "agents.py", "bootstrap_arms.py",
                     "channels.py",
                     "connection_conversations.py", "connections.py",
                     "identities.py", "interactions.py", "users.py"], \
        f"api/management 文件面漂移：{files}"
    # 10.2.1: api/runtime carries the inbound webhook (channels.py)
    runtime_files = sorted(p.name for p in (APP_DIR / "api" / "runtime").glob("*.py"))
    assert runtime_files == ["__init__.py", "channels.py"]

    api_files = sorted(p.name for p in (APP_DIR / "api").glob("*.py"))
    # Cloud-0.1.1-C: + auth.py（Browser Auth router）
    # M5.2: + version.py（Deployment Provenance：/api/version = version + GIT_SHA）
    # M7-WB: + oauth_callback.py（WorkBuddy 重定向 URI 落地页；公开、无 OAuth 逻辑）
    assert api_files == ["__init__.py", "auth.py", "health.py", "oauth_callback.py",
                         "version.py"], \
        f"api 层文件面漂移：{api_files}"

    # schemas：health（5.1）+ agents（11.3）+ channels（11.8）+ connections（11.11）
    #          + users/identities（12.4）+ interactions（13.1 Observability）
    #          + auth（Cloud-0.1.1-C Browser Auth）
    schema_files = sorted(p.name for p in (APP_DIR / "schemas").glob("*.py"))
    # M6.6-C3 / ADR-12: + bootstrap_arms.py（Arm 响应契约）
    assert schema_files == ["__init__.py", "agents.py", "auth.py",
                            "bootstrap_arms.py", "channels.py",
                            "connection_conversations.py",
                            "connections.py",
                            "health.py", "identities.py", "interactions.py", "users.py"], \
        f"schemas 文件面漂移：{schema_files}"

    # Management API 的依赖面：Application Service + schemas 合法（11.3 冻结边界）；
    # persistence / SQLAlchemy / pydantic-绕过 一律不允许。
    mgmt_roots: set[str] = set()
    for py in sorted((APP_DIR / "api" / "management").rglob("*.py")):
        mgmt_roots |= _roots(_tree(py))
    assert any(r == "app.application" or r.startswith("app.application.")
               for r in mgmt_roots), "Management API 应消费 Application Service"
    for forbidden in ("app.persistence", "sqlalchemy"):
        assert not any(r == forbidden or r.startswith(forbidden + ".")
                       for r in mgmt_roots), f"Management API 不应依赖 {forbidden}"


# ================================================================== M Scope（防偷渡全集）


def test_m_no_rbac_capability_policy_gateway_infra() -> None:
    """无 RBAC / Capability / Policy / Gateway / Runtime infra 偷渡。"""
    # Cloud-0.1.1-C：Browser Auth（auth）已是 Gate 裁决的合法基础设施，
    # 从本步 6.7 的偷渡 token 中移除；「禁止引入的认证概念」改由
    # tests/test_cloud_0_1_1_c_browser_authentication_gate.py 精确守护
    # （oauth / sso / rbac / scim / jwks / jwt / refresh_token）。
    infra_tokens = ("rbac", "capability", "policy", "gateway", "permission",
                    "secret", "token")
    problems: list[str] = []

    for py in sorted(APP_DIR.rglob("*.py")):
        name = py.name.lower()
        # adapters/agents、adapters/channels、api/runtime 是占位目录；_err 之类的临时文件不算
        if (APP_DIR / "adapters") in py.parents and py.stem == "__init__":
            continue
        if (APP_DIR / "api" / "runtime") in py.parents and py.stem == "__init__":
            continue
        if (APP_DIR / "persistence" / "models") in py.parents:
            # models 冻结 8 表；metadata 列与 secret_ref 无关——但列名里不含上述 infra 词
            pass
        tree = _tree(py)
        for r in _roots(tree):
            if any(tok in r.lower() for tok in infra_tokens):
                problems.append(f"{py.relative_to(BACKEND_DIR)} imports {r}")
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                lower = node.name.lower()
                if any(tok in lower for tok in infra_tokens):
                    problems.append(f"{py.relative_to(BACKEND_DIR)}::{node.name}")

    assert not problems, "RBAC/Capability/Policy/Gateway 等基础设施偷渡：\n" + "\n".join(problems)

    # 全 app 无 *_adapter.py 的实现文件（Adapter/Runtime Slice 未到）。
    # 例外：repositories/{agent,channel}_adapter.py 是 **出站 Port**（9.5/9.6 交付
    # 的端口抽象，在端口层，不是 adapters/ 实现）。
    PORT_FILE_ALLOWED = {"app/repositories/agent_adapter.py",
                         "app/repositories/channel_adapter.py"}
    for py in sorted(APP_DIR.rglob("*.py")):
        rel = "/".join(py.relative_to(BACKEND_DIR).parts)
        stem = py.stem.lower()
        if stem == "__init__" or rel in PORT_FILE_ALLOWED:
            continue
        assert not stem.endswith("_adapter"), f"Adapter 实现提前出现：{py}"
        assert "adapter" not in stem or (APP_DIR / "adapters") in py.parents, \
            f"Adapter 文件提前出现：{py}"


# ================================================================== 反向审计汇总


def test_reverse_audit_next_stage_not_smuggled() -> None:
    """反向审计汇总：Contract 之后的实体/组件在非 models 层一律不存在。

    7.1 已交付 Connection 契约层（domain/repositories/mappers/persistence
    repositories/UoW），因此下一阶段 = Session/Interaction/Activity +
    Router/Dispatcher/Resolver/Capability/Policy + Connection 的
    Application 层（7.2 才做）。
    """
    layer_files = _entity_layer_files()
    # 9.3：Session 读路径已落地，从"未来实体"移除
    forbidden_next = {"interaction", "activity",
                      "router", "dispatcher", "resolver", "capability", "policy"}

    problems: list[str] = []
    for layer, files in layer_files.items():
        hit = files & forbidden_next
        if hit:
            problems.append(f"{layer}: {sorted(hit)}")

    # application 层文件面 = 5 个 Service + errors + runtime.py（9.1 Runtime
    # Boundary）+ identity_resolution.py（9.2）+ session_resolution.py（9.3）
    # + interaction_recording.py（10.4.2 Interaction 持久化 Wrapper）
    # + interaction_observability.py（13.1 Observability 只读面）
    app_files = sorted(p.stem for p in (APP_DIR / "application").glob("*.py")
                       if p.stem != "__init__")
    # M6.6-C3 / ADR-12: + bootstrap（执行边界组件）/ bootstrap_arms（管理面用例）
    assert app_files == ["access_authorization",
                         "agent_dispatch", "agents", "bootstrap", "bootstrap_arms",
                         "browser_auth", "channel_inbound",
                         "channels", "connection_conversations", "connections",
                         "coordinator", "delivery",
                         "driver", "errors", "identities", "identity_resolution",
                         "interaction_observability", "interaction_recording",
                         "routing", "runtime", "session_creation",
                         "session_resolution", "tenant_fanout",
                         "tenant_provisioning", "users", "worker"], \
        f"application 文件面漂移：{app_files}"  # 12.9.4.5: + worker / 12.9.4.16: + driver / 13.1: + interaction_observability / C: + browser_auth / Cloud-0.1.2.1: + tenant_provisioning

    assert not problems, "下一阶段实体被提前实现：\n" + "\n".join(problems)
