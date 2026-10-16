"""Step 10.7.1 —— Runtime Observability / Evidence Read Model Audit（审计步，生产代码 0 行）。

只回答一个问题：现有 Interaction + Activity 数据，是否已经足够让管理员回答
「一次请求发生了什么」？（不做 Observability Platform，不引入
Prometheus / Grafana / OpenTelemetry / Elasticsearch / distributed tracing）

A  Interaction 定位能力：行内直达 Who/Channel/Conversation/身份 + 双状态 +
   失败三元组；Connection/Agent/内部 Identity 只能经 sessions join（有条件）；
   response_payload 零写入方（Agent 回复内容不可见——记录缺口）；
B  Activity 证据面：类型面冻结 4 种；成功路径中间段零 Evidence（REUSE 无
   session_created——「Session reused」不可见）；失败路径 = received +
   stage_failure（无 completed）；append-only；时间线可排序；
C  查询链：数据层完整（三条 FK），读取层空白（interaction 无 list、activity
   无读方法）；裸 join 可达全部答案（数据面够用的实证）；
D  不引入的东西：无 Read Model / Projection / tracing 符号；API 面无证据读端点；
E  契约成文。

审计结论见 docs/step-10.7.1-runtime-observability-evidence-read-model-audit.md。
"""

import ast
from pathlib import Path

import pytest
from sqlalchemy import ForeignKey, select
from sqlalchemy.orm import Session as SASession, sessionmaker

from app.application import RuntimeCoordinator
from app.application.agent_dispatch import AgentDispatchExecutor
from app.application.delivery import DeliveryExecutor
from app.application.errors import (
    ChannelDeliveryFailedError,
    IdentityResolutionFailedError,
)
from app.application.access_authorization import AccessAuthorizationExecutor
from app.application.identity_resolution import IdentityResolutionExecutor
from app.application.interaction_recording import PersistingCoordinator
from app.application.routing import RoutingExecutor
from app.application.session_creation import SessionCreationExecutor
from app.application.session_resolution import SessionResolutionExecutor
from app.domain.activity_record import (
    ACTIVITY_COMPLETED,
    ACTIVITY_RECEIVED,
    ACTIVITY_SESSION_CREATED,
    ACTIVITY_STAGE_FAILURE,
)
from app.domain.agent import Agent
from app.domain.agent_response import AgentResponse
from app.domain.channel import Channel
from app.domain.connection import Connection
from app.domain.connection_conversation import ConnectionConversation
from app.domain.identity import Identity
from app.domain.inbound_interaction import InboundInteraction
from app.domain.outbound_response import OutboundResponse
from app.domain.user import User
from app.persistence.models.activity import ActivityModel
from app.persistence.models.interaction import InteractionModel
from app.persistence.models.session import SessionModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"
APPLICATION = APP / "application"
REPO_ROOT = BACKEND.parent
DOC = REPO_ROOT / "docs" / "step-10.7.1-runtime-observability-evidence-read-model-audit.md"


# ================================================================== 测试底座


class _CountingAgentAdapter:
    def __init__(self) -> None:
        self.calls = 0

    def send(self, request, connection) -> AgentResponse:  # noqa: ARG002
        self.calls += 1
        return AgentResponse(content=f"echo:{request.content}",
                             external_session_id="agent-session-1")


class _RecordingChannelAdapter:
    def __init__(self) -> None:
        self.deliveries: list[OutboundResponse] = []

    def deliver(self, outbound: OutboundResponse) -> None:
        self.deliveries.append(outbound)


class _FailingChannelAdapter:
    """Delivery 必失败（transport 语义）。"""

    def deliver(self, outbound: OutboundResponse) -> None:
        raise ChannelDeliveryFailedError(
            channel_id=outbound.channel_id,
            external_conversation_id=outbound.external_conversation_id)


def _uow_factory(db_engine):
    return lambda: SqlAlchemyUnitOfWork(
        session_factory=sessionmaker(bind=db_engine, expire_on_commit=False))


def _seed(db_engine) -> dict[str, int]:
    with _uow_factory(db_engine)() as uow:
        alice = uow.users.create(User(name="alice"))
        channel = uow.channels.create(Channel(name="wecom-1", type="wecom"))
        agent = uow.agents.create(Agent(name="agent-a", type="acp"))
        uow.identities.create(Identity(
            user_id=alice.id, channel_id=channel.id, external_id="wx_a"))
        path = uow.connections.create(Connection(
            channel_id=channel.id, agent_id=agent.id, name="path-a",
            status="active",
            config={"endpoint": "http://agent.internal", "timeout_s": 5.0},
            credential_ref="cred-a"))
        # Implementation Package 2 / Step 2：显式授权本文件用到的每个会话键
        for conversation in ("conv-1", "conv-df"):
            uow.connection_conversations.create(ConnectionConversation(
                channel_id=channel.id, external_conversation_id=conversation,
                connection_id=path.id))
        uow.commit()
        return {"channel": channel.id, "agent": agent.id}


def _wrapped_runtime(db_engine, agent_adapter=None, channel_adapter=None):
    uow = _uow_factory(db_engine)
    inner = RuntimeCoordinator(
        identity_resolution=IdentityResolutionExecutor(uow),
        access_authorization=AccessAuthorizationExecutor(uow),
        routing=RoutingExecutor(uow),
        session_resolution=SessionResolutionExecutor(uow),
        session_creation=SessionCreationExecutor(uow),
        agent_dispatch=AgentDispatchExecutor(
            agent_adapter=agent_adapter or _CountingAgentAdapter(),
            uow_factory=uow),
        delivery=DeliveryExecutor(
            channel_adapter=channel_adapter or _RecordingChannelAdapter()),
    )
    return PersistingCoordinator(inner=inner, uow_factory=_uow_factory(db_engine))


def _inbound(channel_id: int, interaction: str,
             conversation: str = "conv-1", identity: str = "wx_a",
             content: str = "hello") -> InboundInteraction:
    return InboundInteraction(
        channel_id=channel_id, external_interaction_id=interaction,
        external_conversation_id=conversation,
        external_identity_id=identity, content=content)


def _interaction_rows(db_engine) -> list[InteractionModel]:
    with SASession(db_engine) as s:
        return list(s.execute(select(InteractionModel)
                              .order_by(InteractionModel.id)).scalars().all())


def _activity_rows(db_engine, interaction_id: int | None = None) -> list[ActivityModel]:
    with SASession(db_engine) as s:
        stmt = select(ActivityModel).order_by(ActivityModel.id)
        if interaction_id is not None:
            stmt = stmt.where(ActivityModel.interaction_id == interaction_id)
        return list(s.execute(stmt).scalars().all())


def _py_files(root: Path):
    return sorted(p for p in root.rglob("*.py"))


# ================================================================== A. Interaction 定位能力


def test_a1_interaction_row_locates_the_request(db_engine) -> None:
    """一次请求 = 一行 interaction：渠道 FK + session FK + 交互身份 +
    payload 快照（Who / Which Channel / Which Conversation 直达）。"""
    g = _seed(db_engine)
    _wrapped_runtime(db_engine).execute(_inbound(g["channel"], "msg-1"))

    row = _interaction_rows(db_engine)[0]
    assert row.channel_id == g["channel"]            # Which Channel
    assert row.session_id is not None                # Which Session
    assert row.external_interaction_id == "msg-1"    # Which Interaction
    assert row.request_payload == {                  # Who / Conversation / content
        "channel_id": g["channel"],
        "external_interaction_id": "msg-1",
        "external_conversation_id": "conv-1",
        "external_identity_id": "wx_a",
        "content": "hello",
        "metadata": None,   # 12.9.4.3：完整入站快照（该入站未携带 metadata）
    }
    assert row.created_at is not None                # 09:30:01（起点时间）


def test_a2_connection_agent_identity_only_via_session_join() -> None:
    """interaction 行不携带 identity/connection/agent/user 列——定位它们只能经
    sessions join（数据面上的有条件可答，审计 §1.1 ⚠️ 两项的 schema 证据）。"""
    interaction_cols = {c.name for c in InteractionModel.__table__.columns}
    assert interaction_cols.isdisjoint(
        {"identity_id", "connection_id", "agent_id", "user_id"})
    session_cols = {c.name for c in SessionModel.__table__.columns}
    assert {"identity_id", "connection_id", "agent_id"} <= session_cols


def test_a3_agent_response_content_is_now_stored(db_engine) -> None:
    """（10.8.4 翻转）审计 §1.3 的缺口已关闭：成功路径落 AgentResponse 快照，
    「Agent 回了什么」可查；失败路径（无 Result）仍为 NULL——不可伪造。"""
    recording = (APPLICATION / "interaction_recording.py").read_text(
        encoding="utf-8")
    assert "response_payload" in recording           # 写入方已出现（10.8.4）
    hits = {p.relative_to(APP).as_posix()
            for p in _py_files(APP)
            if "response_payload" in p.read_text(encoding="utf-8")}
    assert hits >= {
        "domain/interaction_record.py",
        "persistence/models/interaction.py",
        "persistence/mappers/interaction_record.py",
        "application/interaction_recording.py",
    }
    g = _seed(db_engine)
    _wrapped_runtime(db_engine).execute(_inbound(g["channel"], "msg-1"))
    row = _interaction_rows(db_engine)[0]
    assert row.response_payload is not None
    assert row.response_payload["content"] == "echo:hello"


def test_a4_dual_status_and_failure_triple_answer_what_and_where(db_engine) -> None:
    """What happened / Where failed / 两段成功性：三种真实终态的行内答案。"""
    g = _seed(db_engine)
    _wrapped_runtime(db_engine).execute(_inbound(g["channel"], "msg-ok"))
    with pytest.raises(ChannelDeliveryFailedError):
        _wrapped_runtime(db_engine,
                         channel_adapter=_FailingChannelAdapter()).execute(
            _inbound(g["channel"], "msg-df", conversation="conv-df"))
    with pytest.raises(IdentityResolutionFailedError):
        _wrapped_runtime(db_engine).execute(
            _inbound(g["channel"], "msg-idf", identity="wx_unknown"))

    rows = {r.external_interaction_id: r for r in _interaction_rows(db_engine)}
    ok, df, idf = rows["msg-ok"], rows["msg-df"], rows["msg-idf"]

    assert (ok.processing_status, ok.delivery_status) == ("succeeded", "delivered")
    assert ok.failure_stage is None and ok.failure_code is None
    assert (df.processing_status, df.delivery_status) == ("succeeded", "failed")
    assert df.failure_stage == "delivery"
    assert df.failure_code == "CHANNEL_DELIVERY_FAILED"
    assert df.retryable is True
    assert (idf.processing_status, idf.delivery_status) == ("failed", "pending")
    assert idf.failure_stage == "identity_resolution"
    assert idf.failure_code == "IDENTITY_RESOLUTION_FAILED"
    assert idf.retryable is False
    assert idf.session_id is None          # Session 建立前失败：无 Session 可挂


# ================================================================== B. Activity 证据面


def test_b1_evidence_type_surface_is_frozen() -> None:
    """Evidence 类型面冻结 4 种（append-only 事实，不是 Audit Platform）。"""
    from app.domain import activity_record as ar
    types = {ar.ACTIVITY_RECEIVED, ar.ACTIVITY_STAGE_FAILURE,
             ar.ACTIVITY_SESSION_CREATED, ar.ACTIVITY_COMPLETED}
    assert types == {"interaction_received", "stage_failure",
                     "session_created", "interaction_completed"}
    stages = {ar.STAGE_IDENTITY_RESOLUTION, ar.STAGE_ROUTING,
              ar.STAGE_SESSION_RESOLUTION, ar.STAGE_SESSION_CREATION,
              ar.STAGE_AGENT_DISPATCH, ar.STAGE_DELIVERY}
    assert stages == {"identity_resolution", "routing", "session_resolution",
                      "session_creation", "agent_dispatch", "delivery"}


def test_b2_success_path_has_no_middle_stage_evidence(db_engine) -> None:
    """成功执行路径：只有 received → [session_created] → completed；中间段
    （identity/routing/dispatch）零 Evidence；REUSE 不产生 session_created
    ——实例里的「Session reused」一跳在 Evidence 中不可见。"""
    g = _seed(db_engine)
    runtime = _wrapped_runtime(db_engine)
    runtime.execute(_inbound(g["channel"], "msg-1", conversation="conv-1"))
    runtime.execute(_inbound(g["channel"], "msg-2", conversation="conv-1"))

    rows = _interaction_rows(db_engine)
    acts_1 = _activity_rows(db_engine, rows[0].id)
    acts_2 = _activity_rows(db_engine, rows[1].id)
    assert [a.type for a in acts_1] == [
        ACTIVITY_RECEIVED, ACTIVITY_SESSION_CREATED, ACTIVITY_COMPLETED]
    assert [a.type for a in acts_2] == [ACTIVITY_RECEIVED, ACTIVITY_COMPLETED]
    # 中间段 Evidence 不存在（成功路径的已知边界，10.4.2 冻结）
    for a in acts_1 + acts_2:
        assert a.type != ACTIVITY_STAGE_FAILURE
        assert a.stage not in ("identity_resolution", "routing",
                               "session_resolution", "agent_dispatch")


def test_b3_failure_path_evidence(db_engine) -> None:
    """失败路径：received + stage_failure（stage=delivery，含 failure_code /
    retryable metadata），无 interaction_completed——终点即失败点。"""
    g = _seed(db_engine)
    with pytest.raises(ChannelDeliveryFailedError):
        _wrapped_runtime(db_engine,
                         channel_adapter=_FailingChannelAdapter()).execute(
            _inbound(g["channel"], "msg-df", conversation="conv-df"))

    row = next(r for r in _interaction_rows(db_engine)
               if r.external_interaction_id == "msg-df")
    acts = _activity_rows(db_engine, row.id)
    assert [a.type for a in acts] == [ACTIVITY_RECEIVED, ACTIVITY_STAGE_FAILURE]
    failure = acts[-1]
    assert failure.stage == "delivery"
    assert failure.status == "failed"
    assert failure.message                                  # 失败原因文本
    assert failure.extra_metadata == {
        "failure_code": "CHANNEL_DELIVERY_FAILED", "retryable": True}
    assert failure.session_id == row.session_id             # Evidence 挂对 Session


def test_b4_activity_is_append_only() -> None:
    """append-only：Activity port 无 update / delete；activities 表无
    updated_at（历史事实不允许改写）。

    （13.1 翻转）原断言「只有 append、零读方法」关闭——Observability Read
    Vertical Slice 增加只读时间线 `list_by_interaction`；append-only 写纪律
    不变（读路径同样不改写历史）。"""
    port_src = (APP / "repositories" / "activity_record.py").read_text(
        encoding="utf-8")
    tree = ast.parse(port_src, filename="activity_record.py")
    methods = {node.name for node in ast.walk(tree)
               if isinstance(node, ast.FunctionDef)}
    assert methods == {"append", "list_by_interaction"}  # 13.1: + 只读时间线
    from app.persistence.models.activity import ActivityModel
    assert "updated_at" not in {c.name for c in ActivityModel.__table__.columns}


def test_b5_timeline_is_orderable(db_engine) -> None:
    """时间线可排序：activities.created_at 非空且随 id 单调（审计 Q7 的
    「排序天然存在」）。"""
    g = _seed(db_engine)
    _wrapped_runtime(db_engine).execute(_inbound(g["channel"], "msg-1"))
    acts = _activity_rows(db_engine)
    assert all(a.created_at is not None for a in acts)
    stamps = [a.created_at for a in acts]
    assert stamps == sorted(stamps)


# ================================================================== C. 查询链：数据完整 / 读取空白


def test_c1_data_chain_is_complete_via_foreign_keys() -> None:
    """Session → Interaction → Activity 数据链完整：三条 FK 已冻结。"""
    fk_targets = {
        (fk.parent.table.name, fk.target_fullname)
        for table in (InteractionModel, ActivityModel)
        for fk in table.__table__.foreign_keys
    }
    assert ("interactions", "sessions.id") in fk_targets
    assert ("activities", "interactions.id") in fk_targets
    assert ("activities", "sessions.id") in fk_targets


def test_c2_read_path_is_absent_at_repository_level() -> None:
    """读取层现状（13.1 翻转）：interaction port 仍无 find_by_session /
    任意分页；但「读取层空白」已由 Observability Read Vertical Slice 关闭——
    `list_recent(limit)` 成为第二个有界纯读（陈列面，最新在前）。

    两个有界纯读（均非通用查询面——无 offset / 无 cursor / 无 page /
    无任意条件过滤，只有一个固定上限 N）：

    - `find_pending(limit)`：Worker pickup（12.9.3.4 裁决）；
    - `list_recent(limit)`：Observability 陈列（13.1，10.7.1 §Q4 触发条件达成）。
    """
    port_src = (APP / "repositories" / "interaction_record.py").read_text(
        encoding="utf-8")
    tree = ast.parse(port_src, filename="interaction_record.py")
    methods = {node.name for node in ast.walk(tree)
               if isinstance(node, ast.FunctionDef)}
    assert methods == {
        "create", "claim_processing", "record_outcome", "get_by_id",
        "find_by_channel_and_external_id", "find_pending",
        # 12.9.4.12 attempt fence：PROCESSING 孤儿回收（只回收，不做幂等决策）
        "reclaim_stale_processing",
        # 13.1 Observability：有界陈列读（最新在前，纯读）
        "list_recent"}
    for forbidden in ("def find_by_session", "def find_by_channel(",
                      "def find_by_time_range", "offset", "cursor", "page"):
        assert forbidden not in port_src, f"Port 出现通用读路径/分页形状：{forbidden}"


def test_c3_raw_join_answers_all_questions(db_engine) -> None:
    """数据面够用的实证：一次裸 join（interaction→session + activities）即可
    回答 Which Channel/Session/Connection/Agent/Identity + What/Where。"""
    g = _seed(db_engine)
    with pytest.raises(ChannelDeliveryFailedError):
        _wrapped_runtime(db_engine,
                         channel_adapter=_FailingChannelAdapter()).execute(
            _inbound(g["channel"], "msg-df", conversation="conv-df"))

    row = next(r for r in _interaction_rows(db_engine)
               if r.external_interaction_id == "msg-df")
    with SASession(db_engine) as s:
        joined = s.execute(
            select(InteractionModel, SessionModel)
            .join(SessionModel, InteractionModel.session_id == SessionModel.id)
            .where(InteractionModel.id == row.id)
        ).one_or_none()
        assert joined is not None
        interaction, session = joined
        acts = list(s.execute(select(ActivityModel).where(
            ActivityModel.interaction_id == row.id)).scalars().all())

    # 一次请求的全部答案：channel/interaction/session 来自行，connection/agent/
    # identity 来自 session join，what/where 来自双状态 + 失败三元组
    assert interaction.channel_id == g["channel"]
    assert interaction.external_interaction_id == "msg-df"
    assert session.id == interaction.session_id
    assert session.connection_id is not None      # Which Connection
    assert session.agent_id == g["agent"]         # Which Agent
    assert session.identity_id is not None        # Which Identity（内部）
    assert interaction.processing_status == "succeeded"
    assert interaction.delivery_status == "failed"
    assert interaction.failure_stage == "delivery"
    assert len(acts) == 2                         # received + stage_failure


# ================================================================== D. 不引入的东西


def test_d1_no_read_model_or_tracing_symbols() -> None:
    """无 Read Model / Projection / CQRS / tracing 符号偷渡（审计 Q5/Q9）。"""
    forbidden = ("projection", "readmodel", "read_model", "cqrs",
                 "trace_id", "span_id", "opentelemetry", "prometheus",
                 "grafana", "elasticsearch")
    for path in _py_files(APP):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
                lower = node.name.lower()
                for token in forbidden:
                    assert token not in lower, f"{path.name} 出现 {node.name}"
        roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
        assert roots.isdisjoint({"prometheus", "opentelemetry", "elastic",
                                 "elasticsearch", "jaeger"}), \
            f"{path.name}: {roots}"


def test_d2_no_evidence_read_endpoints_in_api_layer() -> None:
    """（13.1 翻转）证据读端点已落地：/api/interactions 三个 GET（只读面）——
    除此之外 API 面仍不出现 activity / evidence / audit / observab 维度路由，
    且 interaction 维度路由没有任何写方法形状。

    扫描口径：完整路径 = router prefix（APIRouter(prefix=...)）+ 装饰器 path
    ——列表路由的装饰器 path 是 ""，单看装饰器常量会漏掉它。"""
    api_root = APP / "api"
    routes: set[tuple[str, str]] = set()
    for path in _py_files(api_root):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        prefixes: set[str] = set()
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "APIRouter"):
                for kw in node.keywords:
                    if (kw.arg == "prefix" and isinstance(kw.value, ast.Constant)
                            and isinstance(kw.value.value, str)):
                        prefixes.add(kw.value.value)
        if not prefixes:            # 无声明前缀的 router：path 即完整路径
            prefixes = {""}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if (isinstance(dec, ast.Call)
                        and isinstance(dec.func, ast.Attribute)
                        and dec.func.attr in {"get", "post", "put",
                                              "delete", "patch"}
                        and dec.args
                        and isinstance(dec.args[0], ast.Constant)
                        and isinstance(dec.args[0].value, str)):
                    for prefix in prefixes:
                        routes.add((dec.func.attr, prefix + dec.args[0].value))
    assert routes, "API 面未发现任何路由（断言前提失效）"

    interaction_routes = {(m, p) for m, p in routes if "interaction" in p.lower()}
    assert interaction_routes == {
        ("get", "/api/interactions"),
        ("get", "/api/interactions/{interaction_id}"),
        ("get", "/api/interactions/{interaction_id}/activities"),
    }, f"Observability 端点面漂移（必须只有三个 GET）：{interaction_routes}"

    for token in ("activity", "evidence", "session", "audit", "observab"):
        assert not any(token in p.lower() for _, p in routes), \
            f"出现未冻结的证据维度路由：{token}"


# ================================================================== E. 契约成文


def test_e1_contract_is_written_down() -> None:
    """审计文档成文（防漂移）。"""
    assert DOC.exists(), f"缺少审计文档：{DOC}"
    text = DOC.read_text(encoding="utf-8")
    for token in ("是否已经足够让管理员回答",
                  "数据面基本够用",
                  "response_payload",
                  "append-only",
                  "天然 trace key",
                  "Read Model",
                  "stage_failure",
                  "Session reused",
                  "failure_stage",
                  "外部身份"):
        assert token in text, f"审计文档缺少 token：{token}"
