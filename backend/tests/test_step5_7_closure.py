"""Step 5.7 —— Persistence Foundation Closure Audit（纯审计，零新增生产代码）。

唯一问题：Persistence Foundation 是否已达到「可以交给 Application Layer 使用」的冻结状态？

八个审计维度（A1~A8），全部机器化：
A1 目录边界 —— domain / repositories(port) / persistence 各司其职，文件面正确
A2 依赖方向 —— 只允许 Application → Port → Adapter → SQLAlchemy，无反向
A3 Domain Purity —— Domain 不依赖 SQLAlchemy，不出现 Base/Session/Column 等概念
A4 Repository Boundary —— Port 业务语义、Adapter 负责实现、Repository 不 commit
A5 Mapper Boundary —— Domain ↔ ORM 转换只发生在 mappers，不外泄
A6 UoW Boundary —— 事务所有权归 UoW；未演化成 TransactionManager/Service
A7 Schema / Lifecycle —— 8 表与冻结模型一致；create_all 仅存在于测试；migration deferred
A8 Scope Audit —— Service/Resolver/Router/Dispatcher/Runtime/... 均未提前实现
"""

import ast
from pathlib import Path

from app.persistence.database import Base

# import models 以注册 8 张表（A7 需要 metadata）
from app.persistence import models  # noqa: F401

BACKEND_DIR = Path(__file__).resolve().parents[1]
APP_DIR = BACKEND_DIR / "app"

EXPECTED_8_TABLES = {
    "users", "identities", "channels", "agents",
    "connections", "sessions", "interactions", "activities",
}
SLICE_ENTITIES = ("user", "channel", "agent", "identity", "connection")
# 9.3：Session 已开始落地（domain/port/mapper/adapter 读路径），其余仍属后续 Slice
REMAINING_ENTITIES = ("interaction", "activity")


# ---------------------------------------------------------------- AST helpers


def _roots(py_file: Path) -> set[str]:
    """收集被 import 的模块根（含相对导入解析后的绝对名）。"""
    tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    rel = py_file.relative_to(BACKEND_DIR)
    parts = list(rel.parts)
    is_init = parts[-1] == "__init__.py"
    if is_init:
        parts.pop()
    else:
        parts[-1] = parts[-1][: -len(".py")]
    dotted = ".".join(parts)
    anchor = dotted if is_init else dotted.rpartition(".")[0]

    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                parts_anchor = anchor.split(".")
                if node.level > 1:
                    parts_anchor = parts_anchor[: -(node.level - 1)] if node.level - 1 <= len(parts_anchor) else []
                base = ".".join(parts_anchor)
                result.add(f"{base}.{node.module}" if node.module and base else node.module or base)
            elif node.module:
                result.add(node.module)
    return result


def _import_violations(root: str, forbidden: tuple[str, ...]) -> list[str]:
    return [f for f in forbidden if root == f or root.startswith(f + ".")]


def _assert_no_forbidden_imports(directory: str, forbidden: tuple[str, ...], label: str) -> None:
    problems: list[str] = []
    for py in sorted((APP_DIR / directory).rglob("*.py")):
        for root in sorted(_roots(py)):
            problems.extend(
                f"{py.relative_to(BACKEND_DIR)} imports {root} (禁止: {hit})"
                for hit in _import_violations(root, forbidden)
            )
    assert not problems, f"[{label}] 出现反向/越层依赖：\n" + "\n".join(problems)


def _files(directory: str) -> list[str]:
    return sorted(p.name for p in (APP_DIR / directory).glob("*.py"))


# ---------------------------------------------------------------- A1 目录边界


def test_a1_directory_boundaries_are_clean() -> None:
    # domain / repositories / persistence 主边界 + models 8 文件
    #（6.6 identity、7.1 connection 契约层各自落地：文件面 +1；
    #  8.1~8.6 契约/值对象陆续落地：仅 domain 增长——都不落库，所以
    #  repositories/mappers/persistence-repositories 不随之增长；
    #  session_resolution 不是 Session 实体、routing_result 不是 Router、
    #  agent_request/agent_response 不是 Adapter、dispatch_failure 不是
    #  Dispatcher、outbound_response 不是 DeliveryService/ChannelAdapter——
    #  session.py / router.py / *_adapter.py / dispatcher.py / delivery_service.py
    #  仍不出现）
    # 9.3：Session 读路径落地——domain / repositories / mappers /
    # persistence-repositories 各 +1（session.py）
    assert _files("domain") == [
        "__init__.py", "access_authorization.py",
        "activity_record.py", "agent.py", "agent_probe_snapshot.py",
        "agent_request.py",
        "agent_response.py", "artifact.py",
        # M6.6-C3 / ADR-12：Bootstrap Arm 领域实体（不落 tenant_id，纪律同 Grant）
        "bootstrap_arm.py",
        "channel.py", "connection.py",
        "connection_conversation.py",
        "dispatch_failure.py", "identity.py", "identity_resolution.py",
        "inbound_interaction.py", "interaction_record.py",
        "interaction_snapshot.py",
        "outbound_response.py", "routing_result.py",
        "runtime_execution_result.py", "session.py",
        "session_resolution.py", "user.py",
    ]  # Package 2 / Step 2: + access_authorization（Access Authorization 结果契约）
    # Package 2 / Step 1: + connection_conversation（授权状态领域镜像，ADR-11）
    # 10.4.2: + interaction_record / activity_record（记录域镜像）
    # Cloud-0.1.2.2.2.1: + agent_probe_snapshot（探针结论值对象；纯 domain，不落库
    # → 其余层不增长；刻意不以 *Result 结尾，避开 c3 的 Runtime Result 守卫）
    # ACP V0.2: + artifact（Agent 工作成果值对象；纯 domain，不落库 → 其余层不增长）
    # 12.9.4.3: + interaction_snapshot（InboundInteraction ⇄ request_payload
    # 快照/重建边界，纯 domain 值对象转换，不落库 → 其余三层不随之增长）
    # 10.8.2: + runtime_execution_result（Runtime 执行结果契约，frozen 三字段，
    # 不接线——RuntimeExecutionCoordinator 仍 execute() -> None）
    # 6.1 起 repositories 新增 UoW Port（unit_of_work.py）；9.3 + session.py；
    # 9.5 + agent_adapter.py；9.6 + channel_adapter.py（出站 Port，Test Adapter 测试注入）
    assert _files("repositories") == [
        "__init__.py", "activity_record.py", "agent.py", "agent_adapter.py",
        # M6.6-C3 / ADR-12：Bootstrap Arm Port
        "bootstrap_arm.py",
        "browser_auth.py", "channel.py", "channel_adapter.py", "channel_inbound.py",
        "connection.py", "connection_conversation.py", "identity.py",
        "interaction_record.py",
        "session.py", "tenant.py", "unit_of_work.py", "user.py",
    ]  # Package 2 / Step 1: + connection_conversation（授权状态 Port）
    # 10.4.2: + interaction_record / activity_record；Cloud-0.1.1-C: + browser_auth.py
    # Cloud-0.1.2.1: + tenant.py（TenantProvisioner 持久化端口）
    assert _files("persistence/mappers") == [
        "__init__.py", "activity_record.py", "agent.py",
        # M6.6-C3 / ADR-12：Bootstrap Arm Mapper
        "bootstrap_arm.py",
        "channel.py",
        "connection.py", "connection_conversation.py", "identity.py",
        "interaction_record.py",
        "session.py", "user.py",
    ]  # Package 2 / Step 1: + connection_conversation（授权状态 Mapper）
    # 10.4.2: + interaction_record / activity_record
    assert _files("persistence/repositories") == [
        "__init__.py", "activity_record.py", "agent.py",
        # M6.6-C3 / ADR-12：Bootstrap Arm Adapter（DB CAS / DB 时钟）
        "bootstrap_arm.py",
        "browser_auth.py",
        "channel.py", "connection.py", "connection_conversation.py", "identity.py",
        "interaction_record.py",
        "session.py", "tenant.py", "user.py",
    ]  # Package 2 / Step 1: + connection_conversation（授权状态 Adapter）
    # 10.4.2: + interaction_record / activity_record；Cloud-0.1.1-C: + browser_auth.py
    # Cloud-0.1.2.1: + tenant.py（SqlAlchemyTenantRepository）
    model_files = sorted(p.name for p in (APP_DIR / "persistence" / "models").glob("*.py"))
    assert model_files == [
        "__init__.py", "activity.py", "agent.py",
        # M6.6-C3 / ADR-12 迁移 0008：bootstrap_arms 表 ORM（含部分唯一索引）
        "bootstrap_arm.py",
        "browser_session.py",
        "channel.py", "connection.py", "connection_conversation.py", "credential.py",
        "identity.py",
        "interaction.py", "session.py", "tenant.py", "user.py",
    ]  # Package 2 / Step 1: + connection_conversation.py（授权状态表 ORM）
    # Cloud-0.1.1-B: + tenant.py；Cloud-0.1.1-C: + credential.py / browser_session.py
    assert (APP_DIR / "persistence" / "database.py").is_file()
    assert (APP_DIR / "persistence" / "unit_of_work.py").is_file()


# ---------------------------------------------------------------- A2 依赖方向


def test_a2_dependency_direction() -> None:
    # domain：不得碰任何基础设施 / 外层
    _assert_no_forbidden_imports(
        "domain",
        ("sqlalchemy", "fastapi", "starlette", "app.schemas", "app.api",
         "app.application", "app.persistence", "app.adapters", "app.repositories"),
        "A2.domain",
    )
    # repositories（端口）：只依赖 domain，不得碰 SQLAlchemy / persistence
    _assert_no_forbidden_imports(
        "repositories",
        ("sqlalchemy", "fastapi", "starlette", "app.schemas", "app.api",
         "app.application", "app.persistence", "app.adapters"),
        "A2.repositories",
    )
    # persistence：允许 SQLAlchemy / domain / repositories 端口，不得反向依赖外层
    _assert_no_forbidden_imports(
        "persistence",
        ("fastapi", "starlette", "app.api", "app.schemas",
         "app.application", "app.adapters"),
        "A2.persistence",
    )
    # Adapter 依赖 Port 方向：persistence/repositories 允许 import app.repositories（端口）
    adapter_imports = set()
    for name in SLICE_ENTITIES:
        adapter_imports |= _roots(APP_DIR / "persistence" / "repositories" / f"{name}.py")
    assert any(r == "app.repositories" or r.startswith("app.repositories.")
               for r in adapter_imports), "Adapter 应引用 Port（app.repositories）"


# ---------------------------------------------------------------- A3 Domain Purity


def test_a3_domain_is_pure_python() -> None:
    forbidden_names = {
        "Base", "DeclarativeBase", "Session", "Column", "mapped_column",
        "ForeignKey", "relationship", "Engine", "sessionmaker", "create_engine",
        "select", "String", "Integer", "JSON", "DateTime", "Boolean", "Text",
    }
    problems: list[str] = []
    for name in SLICE_ENTITIES:
        py = APP_DIR / "domain" / f"{name}.py"
        # 领域实体模块只 import 标准库（dataclasses；Connection 额外只允许 datetime）
        roots = _roots(py)
        assert roots and "dataclasses" in roots, f"domain/{name}.py 缺 dataclasses：{roots}"
        assert roots <= {"dataclasses", "datetime"}, \
            f"domain/{name}.py 引入了非 dataclasses/datetime：{roots}"
        # 不出现基础设施概念标识符（docstring 不是 Name 节点，不会误伤）
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in forbidden_names:
                problems.append(f"{name}.py 出现基础设施概念标识符：{node.id}")
    assert not problems, "\n".join(problems)
    # 领域实体是 dataclass，不继承 ORM Base
    from app.domain.agent import Agent
    from app.domain.channel import Channel
    from app.domain.connection import Connection
    from app.domain.identity import Identity
    from app.domain.user import User

    for entity in (User, Channel, Agent, Identity, Connection):
        assert not issubclass(entity, Base), f"{entity.__name__} 不应继承 ORM Base"


# ---------------------------------------------------------------- A4 Repository Boundary


def test_a4_repository_boundary() -> None:
    # 每个实体的 Port 方法面 = 该实体管理 Slice 冻结的业务语义集（随 Slice 演进），
    # 各自按真实管理语义演进（不追求对称）：
    # Agent 按 name 去重（find_by_name）、Channel 按 (type, name) 去重
    #（find_by_type_and_name）、User 无去重需求（唯一性归 Identity）、
    # Identity 按唯一键 (channel_id, external_id) 查找、Connection 按唯一键
    # (channel_id, agent_id) 查找（find_by_channel_and_agent；name 不承担唯一性）。
    expected_methods = {
        # 12.4：+ list_users（User Management API 的 List 用例）
        "user": {"get_by_id", "create", "update", "list_users"},
        # 11.8：+ list_channels（Channel Management API 的 List 用例）
        # Cloud-0.1.3.1：+ find_tenant_id_by_id（入站路由 hint → 已认证 Channel 的 tenant_id）
        "channel": {"get_by_id", "create", "update", "find_by_type_and_name",
                    "list_channels", "find_tenant_id_by_id"},
        "agent": {"get_by_id", "create", "update", "find_by_name", "list_agents"},
        # 12.4：+ list_identities（Identity Management API 的 List 用例）
        # M6.6-C3 / ADR-12 §11：+ find_or_create（Bootstrap 事务内的幂等建立；
        # 命中复用，冲突以 SAVEPOINT 消化后回读，不是新的自动开户路径）
        "identity": {
            "get_by_id", "create", "update", "find_by_channel_and_external_id",
            "list_identities", "find_or_create",
        },
        # 9.4：+ find_active_by_channel（Routing 最小读路径）；
        # 11.11：+ list_connections（Connection Management API 的 List 用例）
        "connection": {
            "get_by_id", "create", "update", "find_by_channel_and_agent",
            "find_active_by_channel", "list_connections",
        },
    }
    for name in SLICE_ENTITIES:
        port = (APP_DIR / "repositories" / f"{name}.py").read_text(encoding="utf-8")
        assert "Protocol" in port, f"{name} Port 应为 Protocol 接口"
        assert "class " in port
        # 业务语义方法面：必须是预期集合（不允许 commit/flush 之类的非业务语义漏进 Port）
        tree = ast.parse(port, filename=f"{name}.py")
        methods = {
            n.name for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name != "__init__"
        }
        assert methods == expected_methods[name], \
            f"{name} Port 方法面漂移：实际 {methods}，预期 {expected_methods[name]}"

    # Adapter 不 commit（源码级）
    from app.persistence.repositories import agent as agent_adapter
    from app.persistence.repositories import channel as channel_adapter
    from app.persistence.repositories import connection as connection_adapter
    from app.persistence.repositories import identity as identity_adapter
    from app.persistence.repositories import user as user_adapter

    for mod in (user_adapter, channel_adapter, agent_adapter, identity_adapter,
                connection_adapter):
        tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
        commits = [
            n.func.attr for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "commit"
        ]
        assert not commits, f"{mod.__name__} 不应 commit"


# ---------------------------------------------------------------- A5 Mapper Boundary


def test_a5_mapper_boundary() -> None:
    for name in SLICE_ENTITIES:
        mapper_file = APP_DIR / "persistence" / "mappers" / f"{name}.py"
        roots = _roots(mapper_file)
        # Mapper 只认识 ORM Model + Domain；不 import 端口 / API / schemas / fastapi
        forbidden = ("fastapi", "starlette", "app.repositories",
                     "app.api", "app.schemas", "app.application", "app.adapters")
        leaked = [
            r for r in roots
            if any(r == f or r.startswith(f + ".") for f in forbidden)
        ]
        assert not leaked, f"mappers/{name}.py 越层导入：{leaked}"
        assert any(r == "app.domain" or r.startswith("app.domain.")
                   for r in roots), "Mapper 应依赖 domain"
        assert any(r == "app.persistence.models" or r.startswith("app.persistence.models.")
                   for r in roots), "Mapper 应依赖 ORM Model"

    # Domain 绝不 import mappers（转换只发生在 mappers 层）
    domain_roots = set()
    for name in SLICE_ENTITIES:
        domain_roots |= _roots(APP_DIR / "domain" / f"{name}.py")
    assert not any(r.startswith("app.persistence") for r in domain_roots), \
        "Domain 依赖了 persistence（mappers/models）"


# ---------------------------------------------------------------- A6 UoW Boundary


def test_a6_uow_boundary() -> None:
    uow_file = APP_DIR / "persistence" / "unit_of_work.py"
    tree = ast.parse(uow_file.read_text(encoding="utf-8"), filename="unit_of_work.py")

    methods = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    allowed = {"__init__", "__enter__", "__exit__", "commit", "rollback", "close"}
    assert methods <= allowed, f"UoW 演化出了非事务方法：{methods - allowed}"

    roots = _roots(uow_file)
    assert not any(r == "app.domain" or r.startswith("app.domain.")
                   for r in roots), "UoW 不应知道 Domain"
    assert not any(r == "app.repositories" or r.startswith("app.repositories.")
                   for r in roots), "UoW 不应直接依赖端口模块（它持有 Adapter 实例）"

    # 事务所有权：只有 unit_of_work 源码含 commit/rollback 的业务入口（可接受），
    # 但全局不得出现 TransactionManager / TransactionService 类
    for py in sorted(APP_DIR.rglob("*.py")):
        t = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(t):
            if isinstance(node, ast.ClassDef):
                lower = node.name.lower()
                assert "transactionmanager" not in lower and "transactionservice" not in lower, \
                    f"{py.relative_to(BACKEND_DIR)} 出现事务框架类：{node.name}"


# ---------------------------------------------------------------- A7 Schema / Lifecycle


def test_a7_schema_no_drift() -> None:
    tables = set(Base.metadata.tables)
    # Cloud-0.1.1-B：8 张冻结业务表 + tenants root catalog
    # Cloud-0.1.1-C：+ 2 张 Browser Auth 表（user_credentials / browser_sessions）
    # Package 2 / Step 1：+ connection_conversations（授权状态存储，非 4.2 业务表）
    assert tables == EXPECTED_8_TABLES | {
        "tenants", "user_credentials", "browser_sessions",
        "connection_conversations",
        # M6.6-C3 / ADR-12：Bootstrap 窗口存储（不是授权状态）。
        "bootstrap_arms"}, \
        f"表集合漂移：{tables ^ (EXPECTED_8_TABLES | {'tenants', 'user_credentials', 'browser_sessions', 'connection_conversations', 'bootstrap_arms'})}"

    t = lambda n: Base.metadata.tables[n]  # noqa: E731

    # FK / Unique / JSON / credential_ref / timestamps 敏感点抽查（与 4.2 冻结一致）
    identities = t("identities")
    assert {c.name for c in identities.columns} == {
        "id", "user_id", "channel_id", "external_id", "display_name",
        "status", "metadata", "created_at", "updated_at", "tenant_id",
    }
    assert any(
        u.name == "uq_identities_tenant_channel_external_id"
        for u in identities.constraints
    ), "identities UNIQUE(tenant_id, channel_id, external_id) 漂移"

    connections = t("connections")
    assert any(
        u.name == "uq_connections_tenant_channel_agent_id"
        for u in connections.constraints
    ), "connections UNIQUE(tenant_id, channel_id, agent_id) 漂移"
    for name in ("agents", "connections"):
        cols = {c.name for c in t(name).columns}
        assert "credential_ref" in cols, f"{name} 缺 credential_ref"
        assert not any(
            "secret" in c or "password" in c or "token" in c or "api_key" in c
            for c in cols
        ), f"{name} 出现凭证明文列"

    activities = t("activities")
    assert "updated_at" not in activities.columns, "activities 必须 append-only（无 updated_at）"

    interactions = t("interactions")
    assert "processing_status" in interactions.columns
    assert "delivery_status" in interactions.columns
    assert "status" not in interactions.columns, "interactions 不得合成 status"

    for name in EXPECTED_8_TABLES - {"activities"}:
        assert "updated_at" in t(name).columns, f"{name} 缺 updated_at"


def test_a7_create_all_only_in_init_entrypoint_cloud_alembic_introduced() -> None:
    """create_all 只允许出现在一次性 schema 初始化入口；Alembic 已引入（Cloud）。

    Step 14.2.2：MVP 用 `create_all` 做**一次性**初始化（deployment / startup
    concern）。旧断言「create_all 只出现在测试目录」已失效，替换为更精确的不变量：

    - 唯一合法位置 = `app/persistence/database.py`（`init_database`）；
    - Driver / Worker / Runtime / Repository 仍**不得**建表。

    Cloud-0.1.1-A 变更：Alembic 作为 **Cloud PostgreSQL schema evolution
    mechanism** 引入（不再 deferred）——OSS SQLite 仍走 create_all（上方唯一合法
    位置断言），Cloud PostgreSQL 由 `alembic upgrade head` 管理。
    """
    allowed = "app/persistence/database.py"
    offenders = []
    for py in sorted((BACKEND_DIR / "app").rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "create_all":
                rel = str(py.relative_to(BACKEND_DIR)).replace("\\", "/")
                if rel != allowed:
                    offenders.append(rel)
    assert not offenders, (
        f"create_all 只允许出现在 {allowed}（一次性初始化）；违规位置：{offenders}")

    # Cloud-0.1.1-A：Alembic schema evolution 基础设施已引入
    assert (BACKEND_DIR / "alembic.ini").is_file()
    assert (BACKEND_DIR / "alembic" / "versions").is_dir()
    assert not (BACKEND_DIR / "migrations").exists()  # 约定目录是 alembic/
    req_lines = (BACKEND_DIR / "requirements.txt").read_text(encoding="utf-8").splitlines()
    deps = [ln.strip() for ln in req_lines if ln.strip() and not ln.strip().startswith("#")]
    assert any(d.lower().startswith("alembic") for d in deps), \
        "Cloud schema evolution 需要 alembic 依赖"


# ---------------------------------------------------------------- A8 Scope Audit


def test_a8_scope_audit() -> None:
    # 不应出现的文件（按文件名判断，规避 docstring 提词的误伤）
    forbidden_basenames = {
        "_service.py", "_router.py", "_dispatcher.py", "_resolver.py",
        "event_bus.py", "outbox.py", "cqrs.py", "persistence_service.py",
        "generic_repository.py", "transaction_manager.py", "transaction_service.py",
    }
    # 不应出现 *.py 文件名中含 service / resolver / dispatcher / router
    problems: list[str] = []
    for py in sorted(APP_DIR.rglob("*.py")):
        base = py.name.lower()
        if any(base.endswith(f) for f in forbidden_basenames):
            problems.append(str(py.relative_to(BACKEND_DIR)))
        for token in ("service", "resolver", "dispatcher", "router"):
            if token in py.stem.lower():
                # 允许 api/health.py（无 token）与 api/runtime 目录壳
                problems.append(str(py.relative_to(BACKEND_DIR)))
                break

    # 类面：无 *Service / *Manager / GenericRepository 等。
    # 例外：Step 6.1 起 application/ 是 Application Service 的正当归属
    #（如 AgentApplicationService），因此 *Service 只允许出现在 application 层。
    application_dir = APP_DIR / "application"
    for py in sorted(APP_DIR.rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            name = node.name
            is_app_service = application_dir in py.parents and name.endswith("Service")
            if is_app_service:
                continue
            if name.endswith("Service") or name.endswith("Manager") \
                    or name == "GenericRepository" or "PersistenceService" in name:
                problems.append(f"{py.relative_to(BACKEND_DIR)}::{name}")

    assert not problems, f"Scope 泄漏（提前实现）：\n" + "\n".join(problems)


def test_a8_no_external_infra_imports() -> None:
    # 全 app 不 import Redis / Kafka / Celery / Event Bus 等
    forbidden_roots = ("redis", "kafka", "celery", "aiokafka", "eventbus", "outbox")
    problems: list[str] = []
    for py in sorted(APP_DIR.rglob("*.py")):
        for root in _roots(py):
            for f in forbidden_roots:
                if root == f or root.startswith(f + "."):
                    problems.append(f"{py.relative_to(BACKEND_DIR)} imports {root}")
    assert not problems, "\n".join(problems)


def test_a8_remaining_entities_not_premature() -> None:
    """其余五个实体（identity/.../activity）的 port/adapter/mapper/domain 仍未实现。"""
    premature: list[str] = []
    for name in REMAINING_ENTITIES:
        candidates = [
            APP_DIR / "repositories" / f"{name}.py",
            APP_DIR / "persistence" / "repositories" / f"{name}.py",
            APP_DIR / "persistence" / "mappers" / f"{name}.py",
            APP_DIR / "domain" / f"{name}.py",
        ]
        premature.extend(str(p) for p in candidates if p.exists())
    assert not premature, "其余五个实体被提前实现：\n" + "\n".join(premature)
