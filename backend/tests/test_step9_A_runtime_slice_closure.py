"""Step 9.A —— Runtime Vertical Slice Closure Audit（9.2 ~ 9.8.3 横向闭环）。

不加功能，只回答十个问题：

1. Runtime execution order 是否唯一且一致？
2. 每个 stage 的输入/输出是否仍符合 Frozen Contract？
3. Coordinator 是否仍只是 orchestration？
4. Executor 是否发生职责泄漏？
5. Repository / Persistence 是否被 Runtime 偷渡？
6. Session / Connection / Agent 三层边界是否保持？
7. Channel / Agent Adapter 边界是否保持？
8. Failure semantics 是否统一？
9. REUSE / CREATE 两条路径是否都闭合？
10. 有没有为了跑 E2E 而偷偷引入未来基础设施？

大量细粒度断言已由 9.2~9.8.3 各自覆盖；本文件做**聚合横切**验证，防止「每步都对、
整体漂了」。
"""

import ast
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[1] / "app"
BACKEND_DIR = Path(__file__).resolve().parents[1]
APPLICATION = BACKEND_DIR / "app" / "application"

# (stage, module rel, executor class, 应引用的 domain 契约根)
STAGE_REGISTRY = {
    "identity": ("app/application/identity_resolution.py",
                 "IdentityResolutionExecutor", "app.domain.identity_resolution"),
    "authorization": ("app/application/access_authorization.py",
                      "AccessAuthorizationExecutor",
                      "app.domain.access_authorization"),
    "routing": ("app/application/routing.py", "RoutingExecutor",
                "app.domain.routing_result"),
    "session": ("app/application/session_resolution.py",
                "SessionResolutionExecutor", "app.domain.session_resolution"),
    "session_creation": ("app/application/session_creation.py",
                         "SessionCreationExecutor", "app.domain.session"),
    "dispatch": ("app/application/agent_dispatch.py", "AgentDispatchExecutor",
                 "app.domain.agent_response"),
    "delivery": ("app/application/delivery.py", "DeliveryExecutor",
                 "app.domain.outbound_response"),
}

CANONICAL_ORDER = ("identity_resolution", "authorization", "routing",
                   "session_resolution",
                   "session_creation", "agent_dispatch", "delivery")

# 已冻结的 8.x / 9.x domain 契约名（Runtime 组件不得重新定义）
CONTRACT_NAMES = {
    "InboundInteraction", "IdentityResolution", "AccessAuthorization",
    "RoutingResult",
    "SessionResolution", "AgentRequest", "AgentResponse", "OutboundResponse",
}

# 每个 Executor 允许的 import 根（白名单：stdlib + domain + errors + UoW/出站 Port）
def _executor_root_allowed(root: str) -> bool:
    return (root in {"collections", "dataclasses", "typing"}
            or root.startswith("collections.")
            or root.startswith("typing.")
            or root.startswith("app.domain.")
            or root == "app.application.errors"
            or root == "app.repositories.unit_of_work"
            or root in {"app.repositories.agent_adapter",
                        "app.repositories.channel_adapter"})


# ------------------------------------------------------------------ helpers


def _src(rel: str) -> str:
    return (BACKEND_DIR / rel).read_text(encoding="utf-8")


def _roots(src: str) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            roots.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module)
    return roots


def _class_names(src: str) -> set[str]:
    return {n.name for n in ast.walk(ast.parse(src)) if isinstance(n, ast.ClassDef)}


def _call_attrs(src: str) -> set[str]:
    return {
        node.func.attr for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


# ================================================================== 1. 顺序唯一且一致


def test_order_is_single_source_and_canonical() -> None:
    from app.application.runtime import RUNTIME_EXECUTION_ORDER

    assert RUNTIME_EXECUTION_ORDER == CANONICAL_ORDER
    # 全仓只有 runtime.py 定义执行顺序常量（无第二份漂移）
    declared = []
    for py in sorted(APPLICATION.glob("*.py")):
        src = py.read_text(encoding="utf-8")
        if "RUNTIME_EXECUTION_ORDER = (" in src or \
                "RUNTIME_EXECUTION_ORDER=(" in src:
            declared.append(py.name)
    assert declared == ["runtime.py"], declared


def test_coordinator_uses_constants_not_literals() -> None:
    """Coordinator 按 REUSE_ACTIVE / CREATE_NEW 常量分支，不硬编码字符串。"""
    src = _src("app/application/coordinator.py")
    assert "REUSE_ACTIVE" in src and "CREATE_NEW" in src
    assert '== "REUSE_ACTIVE"' not in src and '== "CREATE_NEW"' not in src


# ================================================================== 2. Stage I/O 符合 Frozen Contract


def test_every_stage_imports_its_contract_from_domain() -> None:
    """每个 Executor 消费/产出 domain 冻结契约，不在本地重新定义。"""
    for rel, cls, contract_root in STAGE_REGISTRY.values():
        src = _src(rel)
        roots = _roots(src)
        assert contract_root in roots, f"{rel} 应引用 {contract_root}"
        assert not (_class_names(src) & CONTRACT_NAMES), f"{rel} 重定义了 8.x 契约"


def test_stage_modules_export_only_their_executor() -> None:
    """每阶段模块类面 = 恰好一个 Executor（无顺手的 DTO/Service）。"""
    for rel, cls, _ in STAGE_REGISTRY.values():
        assert _class_names(_src(rel)) == {cls}, f"{rel} 类面漂移"


def test_frozen_contract_field_faces_unchanged() -> None:
    """关键契约字段面仍为冻结形状（9.8.3 E2E 依赖它们）。"""
    import dataclasses
    from app.domain.agent_request import AgentRequest
    from app.domain.agent_response import AgentResponse
    from app.domain.inbound_interaction import InboundInteraction
    from app.domain.outbound_response import OutboundResponse
    from app.domain.routing_result import RoutingResult
    from app.domain.session_resolution import SessionResolution

    assert {f.name for f in dataclasses.fields(InboundInteraction)} == {
        "channel_id", "external_interaction_id", "external_conversation_id",
        "external_identity_id", "content", "metadata"}
    assert {f.name for f in dataclasses.fields(RoutingResult)} == {
        "connection_id", "agent_id"}
    assert {f.name for f in dataclasses.fields(SessionResolution)} == {
        "mode", "identity_id", "connection_id", "external_conversation_id",
        "existing_session_id",
        "external_session_id"}  # 15.5.2：Agent 侧引用只读投影
    assert {f.name for f in dataclasses.fields(AgentRequest)} == {
        "agent_id", "control_plane_session_id", "content", "external_session_id",
        "metadata"}
    assert {f.name for f in dataclasses.fields(AgentResponse)} == {
        "content", "external_session_id", "metadata", "artifacts"}
    assert {f.name for f in dataclasses.fields(OutboundResponse)} == {
        "channel_id", "external_conversation_id", "content", "metadata",
        "artifacts"}


# ================================================================== 3. Coordinator 纯编排


def test_coordinator_is_pure_orchestration() -> None:
    src = _src("app/application/coordinator.py")
    roots = _roots(src)
    for r in roots:
        assert r.startswith("app.domain") \
            or r in {"app.application.errors", "app.application.runtime"}, \
            f"Coordinator 越层依赖 {r}"
    # 只调 execute；类面 = {RuntimeCoordinator}
    assert _call_attrs(src) == {"execute"}
    assert _class_names(src) == {"RuntimeCoordinator"}


def test_coordinator_injects_seven_stages() -> None:
    """七段能力全部构造注入：Coordinator 不 import 任何 Executor 具体实现。

    Package 2 / Step 2：第 7 依赖 `access_authorization`——与其余六段同形注入，
    不允许为了少一个参数而让 Coordinator 自持 Repository。
    """
    src = _src("app/application/coordinator.py")
    tree = ast.parse(src)
    init = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "__init__")
    params = {a.arg for a in init.args.args} | {a.arg for a in init.args.kwonlyargs}
    params -= {"self"}
    assert params == {"identity_resolution", "access_authorization", "routing",
                      "session_resolution",
                      "session_creation", "agent_dispatch", "delivery"}, params
    # 不 import 具体 executor 模块
    imported = {alias.name for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
                for alias in node.names}
    imported |= {alias.name for node in ast.walk(tree)
                 if isinstance(node, ast.Import) for alias in node.names}
    assert not any("Executor" in n for n in imported), imported


# ================================================================== 4/5. 职责泄漏 & Persistence 偷渡


@pytest.mark.parametrize("stage,rel,cls,_contract", [
    (k, *v) for k, v in STAGE_REGISTRY.items()
])
def test_executor_imports_only_allowed_roots(stage, rel, cls, _contract) -> None:
    src = _src(rel)
    roots = _roots(src)
    bad = [r for r in roots if not _executor_root_allowed(r)]
    assert not bad, f"{stage} 越层 import：{bad}"
    # 不 import persistence/adapters/具体仓储
    for r in roots:
        assert not any(r == f or r.startswith(f + ".") for f in (
            "sqlalchemy", "fastapi", "app.persistence", "app.adapters")), \
            f"{stage} 偷渡 {r}"


def test_no_executor_uses_other_stage_repository() -> None:
    """各 Executor 只经 UoW（unit_of_work）读写，不 import 其他实体仓储。"""
    banned = ("app.repositories.identity", "app.repositories.session",
              "app.repositories.connection", "app.repositories.agent",
              "app.repositories.user")
    for rel, cls, _ in STAGE_REGISTRY.values():
        roots = _roots(_src(rel))
        # 精确实体仓储（注意 . 后缀避免误伤 agent_adapter/channel_adapter 出站 Port）
        leaked = [r for r in roots if any(r == b or r.startswith(b + ".")
                                          for b in banned)]
        assert not leaked, f"{rel} 不应直接 import 仓储：{leaked}"


def test_no_repo_or_orm_identifier_in_runtime() -> None:
    """Runtime 组件（coordinator + executors）无 Repository/ORM 标识符。"""
    forbidden = {"Repository", "SqlAlchemy", "sessionmaker", "create_engine",
                 "Engine", "Model", "Base", "mapped_column", "declarative"}
    for rel, cls, _ in STAGE_REGISTRY.values():
        src = _src(rel)
        used = {n.id for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Name)}
        hit = {x for x in used if any(f in x for f in ("Repository", "Model"))
               or x in forbidden}
        # 允许：domain 契约的 Repository 概念不在这些模块；直接断言无此类 Name
        assert not hit, f"{rel} 出现持久化标识符：{hit}"


# ================================================================== 6. Session / Connection / Agent 三层边界


def test_session_keeps_identity_conversation_connection_key() -> None:
    """Session 隔离键含 connection_id + conversation；sessions 表无 user_id。"""
    from app.persistence import models  # noqa: F401
    from app.persistence.database import Base

    cols = set(Base.metadata.tables["sessions"].columns.keys())
    assert {"identity_id", "connection_id", "agent_id",
            "external_conversation_id"} <= cols
    assert "user_id" not in cols
    # SessionResolutionExecutor 不用 user_id 做解析键
    src = _src("app/application/session_resolution.py")
    assert "user_id" not in src


def test_creation_does_not_self_pick_agent_or_connection() -> None:
    """Session Creation 不自己 Routing / 不查 Agent·Connection（AST 级）。"""
    src = _src("app/application/session_creation.py")
    tree = ast.parse(src)
    roots = _roots(src)
    assert not any(r.startswith("app.application.routing")
                   or r.startswith("app.repositories.connection")
                   or r.startswith("app.repositories.agent") for r in roots)
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not (used & {"RoutingExecutor", "ConnectionRepository",
                        "AgentRepository"})
    assert not (_call_attrs(src)
                & {"find_active_by_channel", "find_by_channel_and_agent"})


def test_session_materialization_binds_routed_agent(db_engine) -> None:
    """行为：真实 session 的 agent_id == 其 connection 的 agent_id（不漂移）。"""
    from sqlalchemy import select
    from sqlalchemy.orm import Session as SaSession, sessionmaker

    from app.application import (RoutingExecutor, SessionCreationExecutor,
                                 SessionResolutionExecutor, IdentityResolutionExecutor)
    from app.domain.agent import Agent
    from app.domain.channel import Channel
    from app.domain.connection import Connection
    from app.domain.identity import Identity
    from app.domain.user import User
    from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

    factory = sessionmaker(bind=db_engine, class_=SaSession, expire_on_commit=False)
    uow = lambda: SqlAlchemyUnitOfWork(session_factory=factory)
    with SqlAlchemyUnitOfWork(session_factory=factory) as u:
        usr = u.users.create(User(name="x"))
        ch = u.channels.create(Channel(name="ch1", type="wecom"))
        ag = u.agents.create(Agent(name="ag1", type="acp"))
        ident = u.identities.create(Identity(user_id=usr.id, channel_id=ch.id,
                                             external_id="wx"))
        conn = u.connections.create(Connection(channel_id=ch.id, agent_id=ag.id,
                                               name="p"))
        u.commit()
        ch_id, ident_id, ag_id, conn_id = ch.id, ident.id, ag.id, conn.id
    rr = RoutingExecutor(uow).execute(ch_id, frozenset({conn_id}))
    created = SessionCreationExecutor(uow).execute(
        identity_id=ident_id, channel_id=ch_id, connection_id=conn_id,
        agent_id=rr.agent_id, external_conversation_id="conv-Z")
    assert created.connection_id == conn_id
    assert created.agent_id == ag_id == rr.agent_id
    decision = SessionResolutionExecutor(uow).execute(
        ident_id, conn_id, "conv-Z")
    assert decision.existing_session_id == created.id


# ================================================================== 7. Adapter 边界


def test_adapters_are_ports_not_implementations() -> None:
    """Port 仍定义在 repositories 层（Protocol）；app/adapters 从 10.1.2 起：
    agents 含唯一真实实现 acp.py（ACPAdapter）；channels 仍是空壳（ChannelAdapter
    尚未实现）。"""
    for rel in ("app/repositories/agent_adapter.py",
                "app/repositories/channel_adapter.py"):
        src = _src(rel)
        assert "Protocol" in src
    agents_files = sorted(p.name for p in (APP_DIR / "adapters" / "agents").glob("*.py"))
    # 15.2.3：+ local.py（LocalAgentAdapter）+ registry.py（MultiAgentAdapter）
    assert agents_files == ["__init__.py", "acp.py", "local.py", "registry.py", "workbuddy.py"], agents_files
    # 15.2.3：channels 含 wecom.py + local.py + registry.py；3B-2.2：+ wecom_bot.py
    channels_files = sorted(p.name for p in (APP_DIR / "adapters" / "channels").glob("*.py"))
    assert channels_files == ["__init__.py", "local.py", "registry.py", "wecom.py",
                              "wecom_bot.py"], channels_files


def test_dispatch_delivery_never_import_adapters_impl() -> None:
    for rel in ("app/application/agent_dispatch.py", "app/application/delivery.py"):
        roots = _roots(_src(rel))
        assert not any(r.startswith("app.adapters") for r in roots), rel


# ================================================================== 8. Failure semantics 统一


def test_failure_semantics_unified_typed_per_stage() -> None:
    """每个 Runtime stage 有唯一的 typed ApplicationError；无统一 RuntimeFailed 吞包。"""
    from app.application.errors import (AgentDispatchFailedError,
                                        AgentResponseInvalidError,
                                        ChannelDeliveryFailedError,
                                        IdentityResolutionFailedError,
                                        RoutingFailedError,
                                        SessionResolutionFailedError)
    expected = {IdentityResolutionFailedError, RoutingFailedError,
                SessionResolutionFailedError, AgentDispatchFailedError,
                AgentResponseInvalidError, ChannelDeliveryFailedError}
    for e in expected:
        assert issubclass(e, Exception)
    # 无 RuntimeFailed 一类“吞掉一切”的错误
    import app.application.errors as err_mod
    src = Path(err_mod.__file__).read_text(encoding="utf-8")
    assert "class RuntimeFailed" not in src
    assert "class RuntimeExecutionFailed" not in src


def test_stop_conditions_stage_ordered() -> None:
    from app.application.runtime import RUNTIME_EXECUTION_ORDER

    assert RUNTIME_EXECUTION_ORDER.index("agent_dispatch") < \
        RUNTIME_EXECUTION_ORDER.index("delivery")
    src = _src("app/application/runtime.py")
    for phrase in ("Identity failure → STOP", "Routing failure → STOP",
                   "Session failure → STOP", "Session Creation failure → STOP",
                   "Dispatch failure → NO Delivery", "delivery = FAILED"):
        assert phrase in src


# ================================================================== 9. REUSE / CREATE 闭合


def test_reuse_and_create_branches_are_closed() -> None:
    """两条路径都产出非 None 的 cp_session_id；未知 mode / None → Fail Closed。"""
    src = _src("app/application/coordinator.py")
    assert "REUSE_ACTIVE" in src and "CREATE_NEW" in src
    assert "control_plane_session_id is None" in src  # 兜底：绝不 None 进 Dispatch
    # 两条路径后都必须有非 None 保证（存在 raise 兜底）
    assert src.count("is None") >= 2


# ================================================================== 10. 无未来基础设施偷渡


def test_no_future_infrastructure_smuggled() -> None:
    """Runtime 层无 MCP/A2A/queue/async/retry/circuit breaker/streaming/network 概念。"""
    infra_tokens = ("asyncio", "httpx", "requests", "aiohttp", "websocket",
                    "mcp", "a2a", "celery", "redis", "kafka", "pika",
                    "circuitbreaker", "failover", "retry", "threadpool",
                    "streaming", "queue")
    runtime_files = [
        "app/application/coordinator.py",
        *(rel for rel, _, _ in STAGE_REGISTRY.values()),
        "app/application/runtime.py",
    ]
    for rel in runtime_files:
        src = _src(rel)
        roots = {r.split(".")[0] for r in _roots(src)}
        hit = [t for t in infra_tokens if t in roots]
        assert not hit, f"{rel} 偷渡未来基础设施：{hit}"


def test_no_interaction_activity_persistence_smuggled() -> None:
    """E2E 没有顺手引入 Interaction/Activity 持久化（schema 表存在 ≠ Runtime 使用）。"""
    runtime_files = [
        "app/application/coordinator.py",
        *(rel for rel, _, _ in STAGE_REGISTRY.values()),
    ]
    for rel in runtime_files:
        src = _src(rel)
        roots = _roots(src)
        assert not any(r.startswith("app.persistence") for r in roots), rel
        names = {n.id for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Name)}
        assert not (names & {"InteractionModel", "ActivityModel",
                             "InteractionRepository", "ActivityRepository"}), rel
