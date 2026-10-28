"""Implementation Package 1 — External Identity Decoupling（Gate 3 变更包落地的证据面）。

上游冻结：
    docs/architecture/implementation-gate-2-6-external-identity-semantic-decision.md（D1–D7）
    docs/architecture/implementation-gate-3-external-identity-contract-and-migration-design.md（A–G）

一句话：把「未绑定 ACP User 的 External Identity」从**语义允许**变成
**结构允许 + 合同保守**——

    模型层诚实   identities.user_id 可空（未绑定是一等运行时身份）
    合同层保守   管理面入参 user_id 仍必填、不新增任何 API（语义拆分 ≠ API 数量增加）
    迁移层最小   1 个 alter_column（0006）

**验证顺序（Package 1 冻结，禁止跳步 —— 不要一上来跑全量）**：

    Phase 1  Static / Schema      结构事实：migration 0006 / ORM / domain / response / FK
    Phase 2  Contract             ★ 最重要的语义回归：
                                      user=None → SUCCESS
                                      Identity not found → 仍 IDENTITY_RESOLUTION_FAILED
    Phase 3  Runtime              真实 DB（**不是 Fake UoW**）：
                                      Identity(user_id=NULL) → Resolution → Session → Interaction
    Phase 4  Managed regression   user_id 已绑定的既有路径完全正常（两条路都要证明）
    Phase 5  Zero auto-create     unknown 身份仍不会 INSERT Identity

**本 Package 禁区（均有守卫）**：

    ❌ ExternalParticipant / Anonymous / Guest / System User 实体
    ❌ sentinel user_id（0 / -1 / 兜底 User）      ❌ 自动创建 Identity
    ❌ Identity discovery / Bind / Unbind API      ❌ PATCH 解绑
    ❌ Session schema / 隔离键变更                 ❌ Router / Connection / Dispatcher / Delivery 变更
    ❌ Tenant / RLS 模型变更                       ❌ Authorization stage（属 Implementation Package 2）

**状态边界（提前锁死）**：
    External Identity Implementation PASS  ≠  Authorization PASS。
    本文件证明的是「Identity Resolution → SUCCESS 成为可能」，
    还**没有** Access Authorization（那是 Package 2：failure_code=UNAUTHORIZED 等）。
"""

import ast
import re
from pathlib import Path
from typing import get_args, get_type_hints

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.application.errors import IdentityResolutionFailedError
from app.application.identities import CreateIdentity, IdentityApplicationService
from app.application.identity_resolution import IdentityResolutionExecutor
from app.application.routing import RoutingExecutor
from app.application.session_creation import SessionCreationExecutor
from app.application.session_resolution import SessionResolutionExecutor
from app.domain.agent import Agent
from app.domain.channel import Channel
from app.domain.connection import Connection
from app.domain.identity import Identity
from app.domain.identity_resolution import IDENTITY_RESOLUTION_FAILED, IdentityResolution
from app.domain.interaction_record import InteractionRecord
from app.domain.session_resolution import CREATE_NEW
from app.domain.user import User
from app.persistence.models import (
    IdentityModel,
    InteractionModel,
    SessionModel,
    UserModel,
)
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork
from app.schemas.identities import IdentityCreate, IdentityResponse

BACKEND_DIR = Path(__file__).resolve().parents[1]
APP_DIR = BACKEND_DIR / "app"
VERSIONS_DIR = BACKEND_DIR / "alembic" / "versions"
MIGRATION_0006 = VERSIONS_DIR / "0006_identities_user_id_nullable.py"
EXECUTOR_PY = APP_DIR / "application" / "identity_resolution.py"


# ------------------------------------------------------------------ helpers


def _factory(db_engine) -> sessionmaker:
    return sessionmaker(bind=db_engine, class_=Session, expire_on_commit=False)


def _uow(db_engine) -> SqlAlchemyUnitOfWork:
    return SqlAlchemyUnitOfWork(session_factory=_factory(db_engine))


def _executor(db_engine) -> IdentityResolutionExecutor:
    return IdentityResolutionExecutor(uow_factory=lambda: _uow(db_engine))


def _count(db_engine, model) -> int:
    with Session(db_engine) as session:
        return session.execute(select(func.count()).select_from(model)).scalar_one()


def _seed_unbound_identity(db_engine, *, external_id: str = "wx-unbound"):
    """真实 DB：一条 **user_id=NULL** 的 Identity + 其 Channel。"""
    with _uow(db_engine) as uow:
        channel = uow.channels.create(Channel(name=f"p-ch-{external_id}", type="wecom"))
        identity = uow.identities.create(Identity(
            user_id=None, channel_id=channel.id, external_id=external_id))
        uow.commit()
        return channel, identity


def _seed_runtime(db_engine, *, external_id: str, with_user: bool):
    """真实 DB：Channel + Agent + Connection + Identity（user_id 可空）。"""
    with _uow(db_engine) as uow:
        user = uow.users.create(User(name="p-managed-user")) if with_user else None
        channel = uow.channels.create(Channel(name=f"p-rt-{external_id}", type="wecom"))
        agent = uow.agents.create(Agent(name="p-agent", type="acp"))
        connection = uow.connections.create(Connection(
            channel_id=channel.id, agent_id=agent.id, name="p-path", status="active"))
        identity = uow.identities.create(Identity(
            user_id=user.id if user is not None else None,
            channel_id=channel.id, external_id=external_id))
        uow.commit()
        return user, channel, agent, connection, identity


# ================================================================== Phase 1 — Static / Schema


def test_p1_1_migration_0006_only_relaxes_user_id() -> None:
    """0006 存在、revises 0005，且**只**做 alter_column(user_id, nullable)。"""
    assert MIGRATION_0006.is_file(), "缺少 0006 迁移（identities.user_id nullable）"
    src = MIGRATION_0006.read_text(encoding="utf-8")

    assert 'revision: str = "0006"' in src
    assert 'down_revision: Union[str, None] = "0005"' in src
    assert "op.alter_column(" in src
    assert '"identities"' in src and '"user_id"' in src
    assert "nullable=True" in src and "nullable=False" in src, \
        "upgrade 放宽 / downgrade 收紧（有损，见迁移 docstring）"

    # 越界检查：不建/删表、不增/删列、不碰 FK / 索引 / 唯一键 / RLS
    for banned in ("create_table", "drop_table", "add_column", "drop_column",
                   "create_foreign_key", "drop_constraint", "create_index",
                   "ROW LEVEL SECURITY"):
        assert banned not in src, f"0006 越界：{banned}"


def _revision_graph() -> dict[str, str | None]:
    graph: dict[str, str | None] = {}
    for py in sorted(VERSIONS_DIR.glob("*.py")):
        if py.name.startswith("__"):
            continue
        src = py.read_text(encoding="utf-8")
        rev = re.search(r'^revision: str = "([^"]+)"', src, re.M)
        down = re.search(
            r'^down_revision: Union\[str, None\] = (?:"([^"]+)"|None)', src, re.M)
        assert rev is not None, f"{py.name} 缺少 revision"
        assert down is not None, f"{py.name} 缺少 down_revision"
        graph[rev.group(1)] = down.group(1)
    return graph


def test_p1_2_migration_chain_is_linear_and_head_is_current() -> None:
    """迁移链单根、线性、head = 当前 head（本守卫随每次新增迁移推进）。

    本守卫同时是「0003 vs 0005」类漂移的结构性防线——PG-gated 的
    `test_alembic_revision_is_head` 已改为动态取 head，本地这条负责链完整性。
    """
    graph = _revision_graph()
    # M6.6-C3 / ADR-12：0008（bootstrap_arms）成为新 head；链仍单根、线性。
    assert graph.get("0008") == "0007", f"0008 必须 revises 0007：{graph}"
    assert graph.get("0007") == "0006", f"0007 必须 revises 0006：{graph}"
    assert graph.get("0006") == "0005", f"0006 必须 revises 0005：{graph}"
    assert graph.get("0001") is None, "0001 必须是唯一 root（baseline 不可回改）"

    referenced = {d for d in graph.values() if d is not None}
    assert len(referenced) == len(graph) - 1, f"迁移链分叉或断链：{graph}"
    heads = set(graph) - referenced
    # P2 Delivery Recovery：0009（delivery_recovery 三列）成为新 head；
    # 链仍单根、线性。
    assert graph.get("0009") == "0008", f"0009 必须 revises 0008：{graph}"
    assert heads == {"0009"}, f"head 应为 0009，实际 {heads}"


def test_p1_3_orm_user_id_is_nullable_with_fk_intact() -> None:
    """ORM：可空，但 FK 仍在且指向 users.id。"""
    col = IdentityModel.__table__.columns["user_id"]
    assert col.nullable is True, "identities.user_id 必须可空（未绑定合法）"
    assert col.foreign_keys, "FK 不得被移除"
    assert next(iter(col.foreign_keys)).target_fullname == "users.id"


def test_p1_4_domain_identity_user_id_is_optional() -> None:
    """Domain：Identity.user_id 注解含 None。"""
    atoms = get_args(get_type_hints(Identity)["user_id"])
    assert int in atoms, f"user_id 仍应是 int：{atoms}"
    assert type(None) in atoms, f"user_id 必须可为 None：{atoms}"


def test_p1_5_response_optional_but_create_still_required() -> None:
    """合同保守：响应可空（纯放宽），**入参仍必填**（V0 不暴露创建未绑定身份）。"""
    resp = get_args(IdentityResponse.model_fields["user_id"].annotation)
    assert int in resp and type(None) in resp, f"IdentityResponse.user_id 应可空：{resp}"
    assert IdentityCreate.model_fields["user_id"].is_required(), \
        "IdentityCreate.user_id 必须保持必填（Create managed binding 语义不变）"


def test_p1_6_no_orm_cascade_can_delete_identity() -> None:
    """零 relationship ⇒ 不存在任何 ORM cascade 能误删 Identity（结构保证）。

    不对源码做字符串扫描：模型 docstring 合法地提到「不写 relationship()」，
    文本匹配会误伤。改用 SQLAlchemy inspection（权威事实）。
    """
    for model in (UserModel, IdentityModel, SessionModel, InteractionModel):
        assert not inspect(model).relationships, \
            f"{model.__name__} 出现 relationship —— User/Identity 生命周期解耦要求零 Object Graph"


def test_p1_7_deleting_referenced_user_is_blocked(db_engine) -> None:
    """T-4 / Gate 3 A-2：删除被 Identity 引用的 User → 失败（阻断式）。

    冻结语义：**不 SET NULL、不 CASCADE**——「Identity 在 User 消失后保留」
    靠**阻断删除**实现，不靠隐式解绑（隐式解绑会绕过「改绑定 = 显式动作」纪律）。
    """
    user, channel, _agent, _connection, identity = _seed_runtime(
        db_engine, external_id="wx-block", with_user=True)

    with Session(db_engine) as session:
        with pytest.raises(IntegrityError):
            session.execute(delete(UserModel).where(UserModel.id == user.id))
            session.flush()
        session.rollback()

    # User 与 Identity 双双存续
    assert _count(db_engine, UserModel) == 1
    assert _count(db_engine, IdentityModel) == 1
    with _uow(db_engine) as uow:
        assert uow.users.get_by_id(user.id) is not None
        assert uow.identities.get_by_id(identity.id).user_id == user.id


# ================================================================== Phase 2 — Contract


def test_p2_1_unbound_identity_resolves_successfully(db_engine) -> None:
    """★ 最重要的语义回归：user 未绑定 → **成功**且 user=None（不是 FAILED）。"""
    channel, identity = _seed_unbound_identity(db_engine, external_id="wx-p2")

    resolved = _executor(db_engine).execute(channel.id, "wx-p2")

    assert isinstance(resolved, IdentityResolution)
    assert resolved.identity.id == identity.id
    assert resolved.identity.user_id is None
    assert resolved.user is None


def test_p2_2_identity_not_found_still_fails_closed(db_engine) -> None:
    """另一半语义：**Identity 缺失仍然 Fail Closed**（这条绝不能被放宽）。"""
    channel, _ = _seed_unbound_identity(db_engine, external_id="wx-p2")

    with pytest.raises(IdentityResolutionFailedError):
        _executor(db_engine).execute(channel.id, "wx-missing")


def test_p2_3_failure_registry_and_code_not_weakened() -> None:
    """失败码 / failure stage 注册表不得被本次变更放宽，也不得新增错误类。"""
    from app.application.interaction_recording import STAGE_FAILURE_MAP

    assert IDENTITY_RESOLUTION_FAILED == "IDENTITY_RESOLUTION_FAILED"
    assert STAGE_FAILURE_MAP[IdentityResolutionFailedError] == (
        "identity_resolution", "IDENTITY_RESOLUTION_FAILED", False)

    src = (APP_DIR / "application" / "errors.py").read_text(encoding="utf-8")
    for banned in ("IdentityNotBoundError", "IdentityUserMissingError",
                   "UnboundIdentityError", "IdentityUnboundError"):
        assert f"class {banned}" not in src, \
            f"「未绑定」不是失败，不应新增错误类 {banned}"


# ================================================================== Phase 3 — Runtime（真实 DB）


def test_p3_unbound_identity_reaches_session_and_interaction(db_engine) -> None:
    """T-6：`Identity(user_id=NULL)` 走通 **Resolution → Router → Session → Interaction**。

    这是「结构上没有 user_id 依赖」的行为化证据：
    用**真实 DB**（原先只能靠 Fake UoW 模拟那条路径）。
    """
    user, channel, agent, connection, identity = _seed_runtime(
        db_engine, external_id="wx-p3", with_user=False)
    assert user is None, "Phase 3 前提：这条 Identity **未绑定** ACP User"

    # 1) Identity Resolution → SUCCESS（不抛 IDENTITY_RESOLUTION_FAILED）
    resolved = _executor(db_engine).execute(channel.id, "wx-p3")
    assert resolved.identity.id == identity.id
    assert resolved.identity.user_id is None
    assert resolved.user is None
    assert resolved.identity.id is not None

    # 2) Routing → Connection → Agent（未绑定身份不影响 Access Path）
    #    Package 2 / Step 2：Routing 的候选集来自授权——本 Phase 3 只验证
    #    「未绑定身份不阻断 Access Path」，故显式给出该 connection 的许可。
    routing = RoutingExecutor(uow_factory=lambda: _uow(db_engine)).execute(
        channel.id, frozenset({connection.id}))
    assert (routing.connection_id, routing.agent_id) == (connection.id, agent.id)

    # 3) Session Resolution（三维隔离键，actor = Identity.id）
    decision = SessionResolutionExecutor(uow_factory=lambda: _uow(db_engine)).execute(
        resolved.identity.id, routing.connection_id, "conv-p3")
    assert decision.mode == CREATE_NEW, "首轮应为 CREATE_NEW"

    # 4) Session Creation → 真实 Session 行（identity_id 指向未绑定 Identity）
    created = SessionCreationExecutor(uow_factory=lambda: _uow(db_engine)).execute(
        identity_id=resolved.identity.id,
        channel_id=channel.id,
        connection_id=routing.connection_id,
        agent_id=routing.agent_id,
        external_conversation_id="conv-p3",
    )
    assert created.id is not None

    # 5) Interaction 落库并挂到该 Session（Interaction 侧无 identity/user 列）
    with _uow(db_engine) as uow:
        record = uow.interactions.create(InteractionRecord(
            channel_id=channel.id,
            external_interaction_id="msg-p3",
            session_id=created.id,
        ))
        uow.commit()

    with _uow(db_engine) as uow:
        read = uow.interactions.get_by_id(record.id)
        assert read is not None and read.session_id == created.id
        active = uow.sessions.find_active(
            resolved.identity.id, connection.id, "conv-p3")
        assert active is not None
        assert active.id == created.id
        assert active.identity_id == resolved.identity.id
        assert active.channel_id == channel.id


def test_p3_session_schema_has_no_user_id(db_engine) -> None:
    """Session 不新增 user_id；隔离键仍是 (identity_id, connection_id, external_conversation_id)。"""
    session_cols = {c.name for c in SessionModel.__table__.columns}
    assert "user_id" not in session_cols, "Session 不得引入 user_id（8.3 冻结）"
    assert {"identity_id", "connection_id", "external_conversation_id"} <= session_cols

    interaction_cols = {c.name for c in InteractionModel.__table__.columns}
    assert "user_id" not in interaction_cols and "identity_id" not in interaction_cols, \
        "Interaction Identity = (channel_id, external_interaction_id)，不依赖 Identity/User"


# ================================================================== Phase 4 — Managed regression


def test_p4_1_managed_identity_still_returns_user(db_engine) -> None:
    """T-4 的另一半：user_id 已绑定 → 既有路径**完全正常**（identity + user 都在）。"""
    user, channel, _agent, _connection, identity = _seed_runtime(
        db_engine, external_id="wx-bound", with_user=True)

    resolved = _executor(db_engine).execute(channel.id, "wx-bound")

    assert resolved.identity.id == identity.id
    assert resolved.identity.user_id == user.id
    assert resolved.user is not None and resolved.user.id == user.id


def test_p4_2_management_contract_unchanged(db_engine) -> None:
    """管理面 = Create managed binding：入参必填、响应回显；可空只是纯放宽。"""
    service = IdentityApplicationService(uow_factory=lambda: _uow(db_engine))
    with _uow(db_engine) as uow:
        user = uow.users.create(User(name="m-user"))
        channel = uow.channels.create(Channel(name="m-ch", type="wecom"))
        uow.commit()

    created = service.create_identity(CreateIdentity(
        user_id=user.id, channel_id=channel.id, external_id="m-ext"))
    assert IdentityResponse.model_validate(created).user_id == user.id

    # 未绑定行也能被响应体接受（否则出现 NULL 时管理面会 500）
    unbound = Identity(user_id=None, channel_id=channel.id, external_id="m-ext-2", id=999)
    assert IdentityResponse.model_validate(unbound).user_id is None

    # 合同面仍必填：缺 user_id → 校验失败（extra=forbid + 必填不变）
    with pytest.raises(ValidationError):
        IdentityCreate(channel_id=channel.id, external_id="m-ext-3")


# ================================================================== Phase 5 — Zero auto-create


def test_p5_1_unknown_identity_does_not_create_anything(db_engine) -> None:
    """unknown 身份 → 仍 Fail Closed，且 Identity / User 计数**不变**（零自动创建）。"""
    channel, _ = _seed_unbound_identity(db_engine, external_id="wx-known")
    before = (_count(db_engine, IdentityModel), _count(db_engine, UserModel))

    for attempt in ("wx-unknown-1", "wx-unknown-2"):
        with pytest.raises(IdentityResolutionFailedError):
            _executor(db_engine).execute(channel.id, attempt)

    after = (_count(db_engine, IdentityModel), _count(db_engine, UserModel))
    assert before == after, "失败解析路径发生了写入（自动创建被禁止）"


def test_p5_2_executor_source_has_zero_write_calls() -> None:
    """源码级：Execution 模块只读——「user 缺失」不得被转换成「那就补一个」。"""
    tree = ast.parse(EXECUTOR_PY.read_text(encoding="utf-8"), filename=str(EXECUTOR_PY))

    writes = sorted({
        node.func.attr for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"create", "update", "delete"}
    })
    assert not writes, f"Execution 不应写：{writes}"

    imported = {
        alias.name for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert "IdentityApplicationService" not in imported, \
        "Runtime 不得 import 管理 Service（6.6 Management ≠ Runtime Resolution）"


def test_p5_3_identity_creation_still_only_in_management() -> None:
    """全仓唯一 Identity 创建路径仍是管理面（ADR-5；Gate 2.6 D4 未裁决项不得偷渡）。"""
    offenders = [
        "/".join(py.relative_to(BACKEND_DIR).parts)
        for py in sorted(APP_DIR.rglob("*.py"))
        if not (py.parent.name == "application" and py.name == "identities.py")
        and re.search(r"\buow\.identities\.create\b", py.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"管理面之外出现 Identity 创建：{offenders}"


def test_p5_4_no_anonymous_guest_system_or_sentinel_identity() -> None:
    """Gate 2.6 D2 红线：不存在匿名/访客/系统主体，也不存在 sentinel user_id。"""
    banned_classes = {
        "anonymousidentity", "anonymoususer", "guestidentity", "guestuser",
        "systemuser", "serviceaccount",
    }
    class_problems: list[str] = []
    sentinel_problems: list[str] = []

    for py in sorted(APP_DIR.rglob("*.py")):
        rel = "/".join(py.relative_to(BACKEND_DIR).parts)
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.lower() in banned_classes:
                    class_problems.append(f"{rel}::{node.name}")
            if isinstance(node, ast.keyword) and node.arg == "user_id":
                value = node.value
                if isinstance(value, ast.Constant) and isinstance(value.value, int) \
                        and value.value <= 0:
                    sentinel_problems.append(f"{rel}::user_id={value.value}")

    assert not class_problems, f"出现匿名/访客/系统主体：{class_problems}"
    assert not sentinel_problems, f"出现 sentinel user_id：{sentinel_problems}"
