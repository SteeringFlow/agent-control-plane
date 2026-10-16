"""Step 6.5 —— Management Foundation Consistency Audit（**不是**最终的 6.x Closure）。

只回答一个问题：Agent / Channel / User / Identity 各 Management Slice
并行演进之后，Application Management 层有没有发生**结构漂移**？
（6.6 起 audit 参数面扩到 identity；User/Channel/Agent 断言维持不变。）

A  Application Service 一致性 —— 只依赖 Domain + Port，不依赖
   Persistence / FastAPI / SQLAlchemy
B  输入契约 —— Application Input Contract → Domain Entity，
   而不是 API Schema → Application
C  Transaction —— 各 Service 统一 Service → UoW → commit，
   没有各自演化出别的事务模式
D  Repository 方法面 —— 不要求各实体对称，按各自业务语义判定
   （结构可以统一，领域语义不能为了统一而牺牲）
E  Error Model —— 各实体 *NotFoundError 都是 Application-level errors，
   不是 SQLAlchemyError / HTTPException
F  防偷渡 —— ConnectionService / SessionResolver / Router / Dispatcher /
   Runtime 仍然不存在（Connection/… 留待各自 Slice）
"""

import ast
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
APP_DIR = BACKEND_DIR / "app"

# 已交付的 Management Slice
SLICE_ENTITIES = ("agent", "channel", "user", "identity")
SERVICE_FILES = {
    "agent": "agents.py", "channel": "channels.py",
    "user": "users.py", "identity": "identities.py",
}
SERVICE_CLASSES = {
    "agent": "AgentApplicationService",
    "channel": "ChannelApplicationService",
    "user": "UserApplicationService",
    "identity": "IdentityApplicationService",
}
DOMAIN_CLASSES = {"agent": "Agent", "channel": "Channel", "user": "User", "identity": "Identity"}
REPO_ATTRS = {"agent": "agents", "channel": "channels", "user": "users", "identity": "identities"}

# 尚未启动的实体（Connection 契约层已在 7.1 交付，但 Application/运行时层仍未做）
# 9.3：Session 读路径已落地（不再属于"未来实体"）
REMAINING_ENTITIES = ("interaction", "activity")

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
    "SqlAlchemyUserRepository", "AgentModel", "ChannelModel", "UserModel",
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
    return {
        n.name: n for n in cls.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _use_case_methods(cls: ast.ClassDef) -> dict[str, ast.FunctionDef]:
    return {name: m for name, m in _methods(cls).items()
            if not name.startswith("_")}


def _is_frozen_dataclass(node: ast.ClassDef) -> bool:
    for dec in node.decorator_list:
        if isinstance(dec, ast.Call) and _dotted(dec.func).endswith("dataclass"):
            return any(kw.arg == "frozen" and getattr(kw.value, "value", False)
                       for kw in dec.keywords)
    return False


def _service_path(entity: str) -> Path:
    return APP_DIR / "application" / SERVICE_FILES[entity]


def _service_class(entity: str) -> ast.ClassDef:
    classes = _classes(_tree(_service_path(entity)))
    return classes[SERVICE_CLASSES[entity]]


def _port_methods(entity: str) -> set[str]:
    tree = _tree(APP_DIR / "repositories" / f"{entity}.py")
    return {n.name for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name != "__init__"}


def _adapter_methods(entity: str) -> set[str]:
    tree = _tree(APP_DIR / "persistence" / "repositories" / f"{entity}.py")
    return {n.name for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name != "__init__"}


# ================================================================== A Service 一致性


@pytest.mark.parametrize("entity", SLICE_ENTITIES)
def test_a_service_only_depends_on_domain_and_ports(entity: str) -> None:
    """Application Service 只依赖 Domain + Port，不依赖 Persistence/FastAPI/SQLAlchemy。"""
    py = _service_path(entity)
    roots = _roots(_tree(py))

    leaked = [r for r in roots
              if not any(r == p or r.startswith(p + ".") for p in SERVICE_ALLOWED_ROOTS)]
    assert not leaked, f"{entity} Service 越层导入：{leaked}"

    for forbidden in SERVICE_FORBIDDEN_ROOTS:
        assert not any(r == forbidden or r.startswith(forbidden + ".") for r in roots), \
            f"{entity} Service 不应依赖 {forbidden}"

    # 必须依赖 Port（UoW/仓储），不能只靠 Domain 自己玩
    assert any(r == "app.repositories" or r.startswith("app.repositories.")
               for r in roots), f"{entity} Service 应依赖 repositories 端口"


@pytest.mark.parametrize("entity", SLICE_ENTITIES)
def test_a_service_has_no_infrastructure_concepts(entity: str) -> None:
    """Service 源码里不出现 Session / ORM / HTTP 等基础设施标识符。"""
    tree = _tree(_service_path(entity))
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in INFRA_NAMES:
            pytest.fail(f"{entity} Service 出现基础设施概念：{node.id}")
        if isinstance(node, ast.Attribute) and node.attr == "session":
            pytest.fail(f"{entity} Service 直接访问了 session")


@pytest.mark.parametrize("entity", SLICE_ENTITIES)
def test_a_service_shape_is_uniform(entity: str) -> None:
    """三者结构统一：一个 *ApplicationService，构造只吃 uow_factory。"""
    classes = _classes(_tree(_service_path(entity)))
    services = [n for n in classes if n.endswith("Service")]
    assert services == [SERVICE_CLASSES[entity]], \
        f"{entity} 层的 Service 类面漂移：{services}"

    cls = _service_class(entity)
    init = _methods(cls).get("__init__")
    assert init is not None, f"{SERVICE_CLASSES[entity]} 缺 __init__"
    args = [a.arg for a in init.args.args]
    assert args == ["self", "uow_factory"], f"{entity} Service 构造参数漂移：{args}"


# ================================================================== B 输入契约


@pytest.mark.parametrize("entity", SLICE_ENTITIES)
def test_b_input_contracts_are_application_frozen_dataclasses(entity: str) -> None:
    """输入契约是 Application 层的 frozen dataclass，不是 API Schema。"""
    classes = _classes(_tree(_service_path(entity)))
    for prefix in ("Create", "Update"):
        name = f"{prefix}{DOMAIN_CLASSES[entity]}"
        assert name in classes, f"{entity} 缺输入契约 {name}"
        assert _is_frozen_dataclass(classes[name]), f"{name} 应是 frozen dataclass"


@pytest.mark.parametrize("entity", SLICE_ENTITIES)
def test_b_create_flows_input_contract_to_domain(entity: str) -> None:
    """create_*(input: CreateX) -> X，且方法体内从 input 构造领域对象。"""
    domain = DOMAIN_CLASSES[entity]
    contract = f"Create{domain}"
    method_name = f"create_{entity}"

    cls = _service_class(entity)
    method = _methods(cls).get(method_name)
    assert method is not None, f"{SERVICE_CLASSES[entity]} 缺 {method_name}"

    args = [a.arg for a in method.args.args]
    assert args == ["self", "input"], f"{method_name} 签名漂移：{args}"
    assert isinstance(method.args.args[1].annotation, ast.Name) \
        and method.args.args[1].annotation.id == contract, \
        f"{method_name} 入参应是输入契约 {contract}"
    assert isinstance(method.returns, ast.Name) and method.returns.id == domain, \
        f"{method_name} 应返回领域对象 {domain}"

    # 方法体内：构造领域对象，且字段来自 input（Application Contract → Domain）
    body_constructs_domain = False
    reads_from_input = False
    for node in ast.walk(method):
        if isinstance(node, ast.Call) and _dotted(node.func) == domain:
            body_constructs_domain = True
        if isinstance(node, ast.Attribute) and _dotted(node.value) == "input":
            reads_from_input = True
    assert body_constructs_domain, f"{method_name} 应构造 {domain}"
    assert reads_from_input, f"{method_name} 应从 input 读取字段构造 {domain}"


def test_b_no_api_schema_direction_reversal() -> None:
    """Application 层不 import app.schemas / pydantic —— 依赖方向不能倒过来。"""
    problems: list[str] = []
    for py in sorted((APP_DIR / "application").rglob("*.py")):
        roots = _roots(_tree(py))
        for r in roots:
            if r == "pydantic" or r.startswith("pydantic.") \
                    or r == "app.schemas" or r.startswith("app.schemas.") \
                    or r == "app.api" or r.startswith("app.api."):
                problems.append(f"{py.relative_to(BACKEND_DIR)} imports {r}")
        for node in ast.walk(_tree(py)):
            if isinstance(node, ast.Name) and node.id in {"BaseModel", "Field"}:
                problems.append(f"{py.relative_to(BACKEND_DIR)} 使用 {node.id}")
    assert not problems, "Application 出现 API Schema 反向依赖：\n" + "\n".join(problems)


def test_b_update_contracts_are_partial_and_documented() -> None:
    """Update* 输入契约：字段全部可空（部分更新语义），并写明 None 含义。"""
    for entity in SLICE_ENTITIES:
        node = _classes(_tree(_service_path(entity)))[f"Update{DOMAIN_CLASSES[entity]}"]
        ann = {stmt.target.id: stmt.annotation
               for stmt in node.body if isinstance(stmt, ast.AnnAssign)
               and isinstance(stmt.target, ast.Name)}
        assert ann, f"{entity} Update 契约无字段"
        for field, annotation in ann.items():
            assert "None" in ast.unparse(annotation), \
                f"{entity}.{field} 应为可选（部分更新语义）"
        assert "None" in (ast.get_docstring(node) or ""), \
            f"{entity} Update 契约应写明 None 字段含义"


# ================================================================== C Transaction


@pytest.mark.parametrize("entity", SLICE_ENTITIES)
def test_c_every_use_case_opens_a_uow(entity: str) -> None:
    """每个用例方法都打开一个 UoW（事务边界在用例，不在 Repository）。"""
    cls = _service_class(entity)
    use_cases = _use_case_methods(cls)
    assert use_cases, f"{SERVICE_CLASSES[entity]} 没有用例方法"

    for name, method in use_cases.items():
        opens_uow = any(
            isinstance(n, ast.With)
            and isinstance(n.items[0].context_expr, ast.Call)
            and _dotted(n.items[0].context_expr.func) == "self._uow_factory"
            for n in ast.walk(method)
        )
        assert opens_uow, f"{entity}.{name} 未打开 UoW"


@pytest.mark.parametrize("entity", SLICE_ENTITIES)
def test_c_commit_only_through_uow(entity: str) -> None:
    """commit 唯一形式是 uow.commit()；写用例必须提交；Service 不自己 rollback。"""
    tree = _tree(_service_path(entity))

    commits = [n for n in ast.walk(tree)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and n.func.attr == "commit"]
    assert commits, f"{entity} Service 应显式 commit"
    for call in commits:
        assert _dotted(call.func.value) == "uow", \
            f"{entity} 出现非 uow.commit() 的提交：{ast.unparse(call)}"

    rollbacks = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "rollback"]
    assert not rollbacks, f"{entity} Service 不应自己 rollback（交给 UoW 上下文）"

    # 写用例（create_/update_）必须各有一次 commit
    cls = _service_class(entity)
    for name in (f"create_{entity}", f"update_{entity}"):
        method = _methods(cls)[name]
        assert any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                   and n.func.attr == "commit" for n in ast.walk(method)), \
            f"{name} 未 commit"


@pytest.mark.parametrize("entity", SLICE_ENTITIES)
def test_c_repository_writes_go_through_uow_repos(entity: str) -> None:
    """仓储访问统一走 uow.<repos>.<method>()，不直接 new Repository。"""
    tree = _tree(_service_path(entity))
    attr = REPO_ATTRS[entity]
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            owner = _dotted(node.func.value)
            if owner.startswith("SqlAlchemy") or owner in {"Repository"}:
                pytest.fail(f"{entity} Service 直接调用实现：{ast.unparse(node)}")
    # 至少一次经由 uow 的仓储调用
    assert any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and _dotted(n.func.value) == f"uow.{attr}"
               for n in ast.walk(tree)), f"{entity} 未通过 uow.{attr} 访问仓储"


# ================================================================== D Repository 方法面


# 不追求对称：每个 Port 的方法面 = 该实体真实的管理语义
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


@pytest.mark.parametrize("entity", SLICE_ENTITIES)
def test_d_port_method_face_matches_domain_semantics(entity: str) -> None:
    methods = _port_methods(entity)
    assert methods == EXPECTED_PORT_METHODS[entity], \
        f"{entity} Port 方法面漂移：{methods - EXPECTED_PORT_METHODS[entity]} / " \
        f"{EXPECTED_PORT_METHODS[entity] - methods}"


def test_d_ports_are_deliberately_asymmetric() -> None:
    """结构可统一，语义不对称：各实体的查找/去重语义按自身领域各不相同。"""
    methods = {e: _port_methods(e) for e in SLICE_ENTITIES}
    agent, channel, user, identity = (methods[e] for e in SLICE_ENTITIES)

    # Agent：全局 name 去重
    assert "find_by_name" in agent and "find_by_type_and_name" not in agent
    # Channel：平台限定 (type, name) 去重
    assert "find_by_type_and_name" in channel and "find_by_name" not in channel
    # User：唯一性归 Identity，Port 不扩张任何按 name 查找的方法
    assert not any("name" in m for m in user), f"User Port 不应有 name 查找：{user}"
    # Identity：唯一键是 (channel_id, external_id) —— 按渠道+外部身份查找，
    # 绝不按裸 external_id 全局查找（避免隐含"跨渠道同一人"的错误假设）
    finders = {m for m in identity if m.startswith("find_")}
    # M6.6-C3 / ADR-12：`find_or_create` 也是**唯一键**查找（命中复用、未命中才创建），
    # 不引入任何「按裸 external_id 全局查找」的跨渠道假设。
    assert finders == {"find_by_channel_and_external_id", "find_or_create"}, \
        f"Identity Port 查找方法应只有唯一键查找：{finders}"

    # 四者的公共能力仍然一致（读/写/更新）
    common = {"get_by_id", "create", "update"}
    for name, port in methods.items():
        assert common <= port, f"{name} Port 缺公共能力：{common - port}"


@pytest.mark.parametrize("entity", SLICE_ENTITIES)
def test_d_adapter_implements_exactly_port_methods(entity: str) -> None:
    """Adapter 恰好实现 Port 的方法面（不多不少 = 无偷渡、无遗漏）。"""
    assert _adapter_methods(entity) == EXPECTED_PORT_METHODS[entity], \
        f"{entity} Adapter 与 Port 方法面不一致"


@pytest.mark.parametrize("entity", SLICE_ENTITIES)
def test_d_every_port_method_documents_its_semantics(entity: str) -> None:
    """Port 每个方法都要说明业务语义（防止方法面变成默默增长的 CRUD）。"""
    tree = _tree(APP_DIR / "repositories" / f"{entity}.py")
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name != "__init__":
            assert ast.get_docstring(node), f"{entity} Port 方法 {node.name} 缺业务语义说明"


# ================================================================== E Error Model


EXPECTED_ERRORS = {
    "ApplicationError",
    "AgentNotFoundError", "DuplicateAgentError", "InvalidAgentInputError",
    "ChannelNotFoundError", "DuplicateChannelError", "InvalidChannelInputError",
    "UserNotFoundError", "InvalidUserInputError",
    "IdentityNotFoundError", "DuplicateIdentityError", "InvalidIdentityInputError",
    # 7.1 起 Connection 契约层错误（Application Service 在 7.2 消费）
    "ConnectionNotFoundError", "DuplicateConnectionError", "InvalidConnectionInputError",
    # 9.2 Runtime Identity Resolution 执行失败（Fail Closed，Execution 消费）
    "IdentityResolutionFailedError",
    # 9.3 Runtime Session Resolution 执行失败（Fail Closed，Execution 消费）
    "SessionResolutionFailedError",
    # 9.4 Runtime Routing 执行失败（无法唯一确定 Connection → Fail Closed）
    "RoutingFailedError",
    # 9.5 Runtime Agent Dispatch 失败（AGENT_DISPATCH_FAILED / AGENT_RESPONSE_INVALID）
    "AgentDispatchFailedError", "AgentResponseInvalidError",
    # 9.6 Runtime Channel Delivery 失败（CHANNEL_DELIVERY_FAILED）
    "ChannelDeliveryFailedError",
    # Package 2 / Step 2：Access Authorization 拒绝（UNAUTHORIZED）
    "AccessAuthorizationDeniedError",
    # Package 2 / Step 3：Grant Management（409 / 404 / 422）
    "DuplicateConnectionConversationError",
    "ConnectionConversationNotFoundError",
    "InvalidConnectionConversationInputError",
    # Cloud-0.1.1-C Browser Auth：登录名重复 / 输入不合法
    "DuplicateBrowserLoginError", "InvalidBrowserAuthInputError",
    # M7-AUI-10.5：WorkBuddy 外部身份租户内重复（应用级软约束 → 409）
    "DuplicateExternalAgentError",
    # M7-AUI-10.6.7：WeCom Bot 渠道 bot_id 租户内重复（409 duplicate_bot_channel）
    "DuplicateBotChannelError",
}


def _error_classes() -> dict[str, ast.ClassDef]:
    return _classes(_tree(APP_DIR / "application" / "errors.py"))


def test_e_errors_are_application_level() -> None:
    """错误是纯 Application 层类型：继承 ApplicationError，且不 import 任何基础设施。"""
    py = APP_DIR / "application" / "errors.py"
    tree = _tree(py)
    classes = _error_classes()

    assert set(classes) == EXPECTED_ERRORS, \
        f"错误类型面漂移：多 {set(classes) - EXPECTED_ERRORS} / 缺 {EXPECTED_ERRORS - set(classes)}"

    # 不许继承 SQLAlchemyError / HTTPException 之类
    forbidden_bases = {"SQLAlchemyError", "HTTPException", "Exception"}  # Exception 只允许基类用
    for name, node in classes.items():
        bases = [_dotted(b) for b in node.bases]
        if name == "ApplicationError":
            assert bases == ["Exception"], f"ApplicationError 基类漂移：{bases}"
            continue
        assert bases == ["ApplicationError"], f"{name} 应直接继承 ApplicationError：{bases}"
        assert not any(b in forbidden_bases for b in bases)

    # errors.py 自身不 import 任何东西（保持纯语义）
    assert not _roots(tree), f"errors.py 不应 import：{_roots(tree)}"


@pytest.mark.parametrize("entity", SLICE_ENTITIES)
def test_e_not_found_error_is_application_level(entity: str) -> None:
    """三个 NotFound 错误都存在且是 ApplicationError 子类（不是 SQLAlchemy/HTTP 异常）。"""
    name = f"{DOMAIN_CLASSES[entity]}NotFoundError"
    classes = _error_classes()
    assert name in classes, f"缺 {name}"
    assert [_dotted(b) for b in classes[name].bases] == ["ApplicationError"]


@pytest.mark.parametrize("entity", SLICE_ENTITIES)
def test_e_services_raise_only_application_errors(entity: str) -> None:
    """Service 抛出的只能是 Application 层错误类型。"""
    allowed = set(_error_classes())
    tree = _tree(_service_path(entity))
    raises = [n for n in ast.walk(tree) if isinstance(n, ast.Raise) and n.exc is not None]
    assert raises, f"{entity} Service 应有错误抛出路径"

    for node in raises:
        if isinstance(node.exc, ast.Call):
            name = _dotted(node.exc.func)
        else:
            name = _dotted(node.exc)
        assert name in allowed, f"{entity} 抛出了非 Application 错误：{name}"


def test_e_application_has_no_http_or_sqlalchemy_error_semantics() -> None:
    """Application 层不出现 HTTPException / 状态码 / SQLAlchemy 错误语义。"""
    forbidden_roots = ("sqlalchemy", "fastapi", "starlette", "pydantic", "app.api")
    # 注意：status 不在其中——它是领域实体/输入契约里的业务状态字段，不是 HTTP 状态码
    forbidden_names = {"HTTPException", "SQLAlchemyError", "IntegrityError",
                       "JSONResponse", "status_code"}
    problems: list[str] = []
    for py in sorted((APP_DIR / "application").rglob("*.py")):
        for r in _roots(_tree(py)):
            if any(r == f or r.startswith(f + ".") for f in forbidden_roots):
                problems.append(f"{py.relative_to(BACKEND_DIR)} imports {r}")
        for node in ast.walk(_tree(py)):
            if isinstance(node, ast.Name) and node.id in forbidden_names:
                problems.append(f"{py.relative_to(BACKEND_DIR)} 使用 {node.id}")
    assert not problems, "Application 混入了 HTTP/SQLAlchemy 错误语义：\n" + "\n".join(problems)


# ================================================================== F 防偷渡


def test_f_application_file_face_is_fixed() -> None:
    files = sorted(p.name for p in (APP_DIR / "application").glob("*.py"))
    assert files == ["__init__.py", "access_authorization.py", "agent_dispatch.py",
                     # M7-AUI-10.5：WorkBuddy External Agent Identity 语义/查重
                     "agent_identity.py",
                     "agents.py",
                     # M6.6-C3 / ADR-12：Bootstrap 组件 + Bootstrap Arm 管理面用例
                     "bootstrap.py", "bootstrap_arms.py",
                     "browser_auth.py",
                     "channel_inbound.py", "channels.py", "connection_conversations.py",
                     "connections.py",
                     "coordinator.py",
                     "delivery.py", "driver.py", "errors.py", "identities.py",
                     "identity_resolution.py", "interaction_observability.py",
                     "interaction_recording.py",
                     "routing.py", "runtime.py",
                     "session_creation.py", "session_resolution.py",
                     "tenant_fanout.py", "tenant_provisioning.py", "users.py",
                     "worker.py"], \
        f"application 文件面漂移：{files}"  # 12.9.4.5: + worker / 12.9.4.16: + driver / 13.1: + interaction_observability / Cloud-0.1.2.1: + tenant_provisioning / M7-AUI-10.5: + agent_identity


def test_f_remaining_entities_not_premature() -> None:
    """connection / session / interaction / activity 仍未提前实现（identity 已交付）。"""
    premature: list[str] = []
    for name in REMAINING_ENTITIES:
        candidates = [
            APP_DIR / "domain" / f"{name}.py",
            APP_DIR / "repositories" / f"{name}.py",
            APP_DIR / "persistence" / "repositories" / f"{name}.py",
            APP_DIR / "persistence" / "mappers" / f"{name}.py",
            APP_DIR / "application" / f"{name}.py",
        ]
        premature.extend(str(p.relative_to(BACKEND_DIR)) for p in candidates if p.exists())
    assert not premature, "被偷渡实现：\n" + "\n".join(premature)


# 9.1 Runtime Execution Boundary（精确白名单）：application/runtime.py 的
# RuntimeExecutionCoordinator 是已冻结的 Runtime Coordinator 边界——类名含
# "runtime"，但只是编排 8.x 契约的 Coordinator，不是 Runtime Service/实体。
RUNTIME_BOUNDARY_ALLOWLIST = {
    ("app/application/runtime.py", "RuntimeExecutionCoordinator"),
    # 9.8.2：RuntimeCoordinator = Coordinator 的**实现**（Orchestrator，非 Domain Service）
    ("app/application/coordinator.py", "RuntimeCoordinator"),
    # 10.8.2：RuntimeExecutionResult = Runtime 执行结果契约（frozen 三字段值对象，
    # 类名含 "runtime" 但只是事实记录，不是 Runtime Service/实体；未接线）
    ("app/domain/runtime_execution_result.py", "RuntimeExecutionResult"),
    # Cloud-0.1.1-B：TenantResolver = 入口租户解析**端口**（Protocol，非实现）。
    # 类名含 "resolver"，但它是 Control Plane ingress 边界契约（app/core/tenant.py），
    # 不是 Runtime 决策组件（实现见实现边界 §12，本步只定义接口）。
    ("app/core/tenant.py", "TenantResolver"),
}


def test_f_no_smuggled_services_or_runtime_classes() -> None:
    """IdentityService / ConnectionService / SessionResolver / Router / Dispatcher /
    Runtime 组件均不存在（9.1 RuntimeExecutionCoordinator 属精确白名单）。"""
    forbidden_exact = {"IdentityService", "ConnectionService", "SessionResolver",
                       "Router", "Dispatcher", "Runtime"}
    forbidden_tokens = ("router", "dispatcher", "resolver", "runtime")

    problems: list[str] = []
    for py in sorted(APP_DIR.rglob("*.py")):
        in_application = (APP_DIR / "application") in py.parents
        rel = "/".join(py.relative_to(BACKEND_DIR).parts)
        for node in ast.walk(_tree(py)):
            if not isinstance(node, ast.ClassDef):
                continue
            name = node.name
            lower = name.lower()
            if (rel, name) in RUNTIME_BOUNDARY_ALLOWLIST:
                continue
            if name in forbidden_exact:
                problems.append(f"{py.relative_to(BACKEND_DIR)}::{name}")
                continue
            if any(tok in lower for tok in forbidden_tokens):
                problems.append(f"{py.relative_to(BACKEND_DIR)}::{name}")
                continue
            # *Service 只允许出现在 application 层（且必须是 Application Service）
            if name.endswith("Service") and not in_application:
                problems.append(f"{py.relative_to(BACKEND_DIR)}::{name}")
    assert not problems, "Scope 泄漏：\n" + "\n".join(problems)


def test_f_adapters_and_api_still_empty_shells() -> None:
    """Channel Adapter / api 运行时仍空壳；agents 自 10.1.2 起含唯一真实实现 acp.py。"""
    for sub in ("channels", "agents"):
        files = sorted(p.name for p in (APP_DIR / "adapters" / sub).glob("*.py"))
        # 15.2.3：+ local.py / registry.py（Local 与 type→Adapter 分派）
        # 3B-2.2：+ wecom_bot.py（WebSocket transport）
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
        # 10.2.1: api/runtime carries the inbound webhook (channels.py)
        runtime_files = sorted(p.name for p in (APP_DIR / "api" / "runtime").glob("*.py"))
        assert runtime_files == ["__init__.py", "channels.py"]


def test_f_no_router_dispatcher_service_files() -> None:
    for py in sorted(APP_DIR.rglob("*.py")):
        name = py.name.lower()
        assert not name.endswith(("_router.py", "_dispatcher.py", "_resolver.py",
                                  "_service.py")), f"不应出现：{py}"
        for token in ("router", "dispatcher", "resolver"):
            assert token not in py.stem.lower(), f"不应出现 {token} 文件：{py}"
