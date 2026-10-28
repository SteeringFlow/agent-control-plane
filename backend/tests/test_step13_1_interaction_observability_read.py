"""Step 13.1 —— Interaction Observability Read Vertical Slice 验收。

唯一验收标准：**不直接查数据库，仅通过 Observability API，能完整还原一次
真实 Interaction 的执行故事。**

数据基准 = 12.9.4.17 的真实 E2E 流（同构造、同组件）：

    ⑥ HTTP + Driver   WeCom 真实 wire → ACK → Driver → 真六段 → 成功
    失败路径          Delivery 失败（processing=SUCCEEDED × delivery=FAILED，
                      4.2 冻结的合法组合）
    失败路径          Identity 建立前失败（session_id=NULL + identity_resolution）

方法纪律：

- 被测链路全部是**真实** Core（真库 / 真 phase API / 真 Worker / 真 Driver /
  真 RuntimeCoordinator）；只有对外网络跳（Agent wire / Channel 出站）用
  记录型 / 失败型 Adapter 替代（与 12.9.4.17 同一裁决）；
- 故事还原断言**只走 GET /api/interactions***——raw SQL 仅用于「只读证明」
  的前后快照对照（测试侧基线捕获，不是验收路径）；
- API 全部纯读：三次调用后 interactions / activities 两表零变化、状态零跃迁。
"""

import ast
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.adapters.channels.wecom import (
    RESPONSE_URL_METADATA_KEY,
    WeComAdapter,
    compute_signature,
    encrypt_message,
)
from app.api.management.interactions import build_interactions_api
from app.api.runtime.channels import build_channel_inbound_api
from app.application.agent_dispatch import AgentDispatchExecutor
from app.application.channel_inbound import ChannelInboundService
from app.application.coordinator import RuntimeCoordinator
from app.application.delivery import DeliveryExecutor
from app.application.driver import DriverConfig, ProductionDriver
from app.application.access_authorization import AccessAuthorizationExecutor
from app.application.identity_resolution import IdentityResolutionExecutor
from app.application.interaction_observability import (
    InteractionObservabilityService,
)
from app.application.interaction_recording import PersistingCoordinator
from app.application.routing import RoutingExecutor
from app.application.session_creation import SessionCreationExecutor
from app.application.session_resolution import SessionResolutionExecutor
from app.application.worker import Worker
from app.domain.agent import Agent
from app.domain.agent_response import AgentResponse
from app.domain.channel import Channel
from app.domain.connection import Connection
from app.domain.connection_conversation import ConnectionConversation
from app.domain.identity import Identity
from app.domain.inbound_interaction import InboundInteraction
from app.domain.outbound_response import OutboundResponse
from app.domain.runtime_execution_result import RuntimeExecutionResult
from app.domain.session import Session as SessionEntity
from app.domain.user import User
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

BACKEND = Path(__file__).resolve().parents[1]
APP_DIR = BACKEND / "app"

# 与 test_step12_9_4_17 同一套 wire 常量（真实验签 / 加密）
TOKEN = "unit-test-token"
AES_KEY = "a" * 43
CORP_ID = "wwcorp001"
TIMESTAMP = "1409659813"
NONCE = "nonce-1"
ROBOT_URL = "https://qyapi.weixin.qq.com/cgi-bin/response-url-abc"

ROBOT_TEXT = {
    "msgid": "CAIQ16HMjQYY/NGagIOAgAMgq4KM0AI=",
    "aibotid": "AIBOTID",
    "chattype": "single",
    "from": {"userid": "zhangsan"},
    "response_url": ROBOT_URL,
    "msgtype": "text",
    "text": {"content": "帮我查一下夹具规格"},
}


# ================================================================== 测试底座


class _EchoAgentAdapter:
    """Agent wire 替身：不触网络，回声式应答（12.9.4.17 ⑥ 同款）。"""

    def send(self, request, connection) -> AgentResponse:
        return AgentResponse(content=f"echo:{request.content}")


class _RecordingChannelAdapter:
    """出站记录器：不触网络，只记录真正要投递的 OutboundResponse。"""

    def __init__(self) -> None:
        self.deliveries: list[OutboundResponse] = []

    def deliver(self, outbound: OutboundResponse) -> None:
        self.deliveries.append(outbound)


class _FailingChannelAdapter:
    """出站失败器：transport 层失败（DeliveryExecutor 映射为
    CHANNEL_DELIVERY_FAILED，10.7.1 §1.2 的「WeCom delivery failed」场景）。"""

    def deliver(self, outbound: OutboundResponse) -> None:
        raise RuntimeError("wecom transport down")


class _DeadWorker:
    """响应已发出后进程即崩的 Worker：BackgroundTask 兜底不生效。"""

    def process(self, interaction_id: int) -> bool:
        return False


def _uow(db_engine):
    return lambda: SqlAlchemyUnitOfWork(
        session_factory=sessionmaker(bind=db_engine, expire_on_commit=False))


def _seed(db_engine, *, with_identity: bool = True) -> dict:
    """最小可用链路（12.9.4.17 _seed 同款 + connection_id）。

    session 携带 connection_id：9.3 的三维隔离键
    (identity_id, connection_id, external_conversation_id) 命中 → 真六段
    流程 REUSE_ACTIVE（与 10.7.1 §1 的「Session reused」故事一致）。
    """
    with _uow(db_engine)() as uow:
        user = uow.users.create(User(name="alice"))
        channel = uow.channels.create(Channel(
            name="wecom-1", type="wecom",
            config={"token": TOKEN, "encoding_aes_key": AES_KEY,
                    "corp_id": CORP_ID, "receiveid": ""}))
        agent = uow.agents.create(Agent(name="agent-a", type="acp"))
        identity_id = None
        if with_identity:
            identity = uow.identities.create(Identity(
                user_id=user.id, channel_id=channel.id, external_id="zhangsan"))
            identity_id = identity.id
        connection = uow.connections.create(Connection(
            channel_id=channel.id, agent_id=agent.id, name="path-a",
            status="active",
            config={"endpoint": "http://agent.internal", "timeout_s": 5.0},
            credential_ref="cred-a"))
        session_id = None
        if with_identity:
            session = uow.sessions.create(SessionEntity(
                identity_id=identity_id, channel_id=channel.id,
                connection_id=connection.id,
                external_conversation_id="zhangsan", status="active"))
            session_id = session.id
        # Implementation Package 2 / Step 2：会话键 = zhangSan（单聊），显式授权
        uow.connection_conversations.create(ConnectionConversation(
            channel_id=channel.id, external_conversation_id="zhangsan",
            connection_id=connection.id))
        uow.commit()
        return {"channel": channel.id, "session": session_id}


def _inbound(channel_id: int, content: str = "你好",
             external_id: str = "msg-1") -> InboundInteraction:
    return InboundInteraction(
        channel_id=channel_id, external_interaction_id=external_id,
        external_conversation_id="zhangsan", external_identity_id="zhangsan",
        content=content)


def _robot_inbound(channel_id: int) -> InboundInteraction:
    """智能机器人入站：response_url 走 metadata（12.8.2 冻结）。"""
    return InboundInteraction(
        channel_id=channel_id,
        external_interaction_id=ROBOT_TEXT["msgid"],
        external_conversation_id="zhangsan", external_identity_id="zhangsan",
        content=ROBOT_TEXT["text"]["content"],
        metadata={RESPONSE_URL_METADATA_KEY: ROBOT_URL})


def _real_runtime(uow_factory, channel_adapter) -> RuntimeCoordinator:
    """真实六段 RuntimeCoordinator（仅出站 Adapter 可替换）。"""
    return RuntimeCoordinator(
        identity_resolution=IdentityResolutionExecutor(uow_factory),
        access_authorization=AccessAuthorizationExecutor(uow_factory),
        routing=RoutingExecutor(uow_factory),
        session_resolution=SessionResolutionExecutor(uow_factory),
        session_creation=SessionCreationExecutor(uow_factory),
        agent_dispatch=AgentDispatchExecutor(
            agent_adapter=_EchoAgentAdapter(), uow_factory=uow_factory),
        delivery=DeliveryExecutor(channel_adapter=channel_adapter),
    )


def _observability_app(db_engine) -> TestClient:
    """只挂 Observability router 的应用（被测面 == 验收面）。"""
    service = InteractionObservabilityService(uow_factory=_uow(db_engine))
    app = FastAPI()
    app.include_router(build_interactions_api(service))
    return TestClient(app)


def _robot_post_params() -> dict[str, str]:
    """智能机器人明文 JSON → 真实 WeCom POST 事实（验签 + 加密信封）。"""
    encrypt = encrypt_message(json.dumps(ROBOT_TEXT, ensure_ascii=False),
                              AES_KEY, "")
    return {
        "msg_signature": compute_signature(TOKEN, TIMESTAMP, NONCE, encrypt),
        "timestamp": TIMESTAMP, "nonce": NONCE,
        "body": json.dumps({"encrypt": encrypt}, ensure_ascii=False),
    }


def _snapshot(db_engine) -> list[tuple]:
    """两表全量快照（只读证明的基线捕获；不是验收路径）。"""
    with db_engine.connect() as conn:
        interactions = conn.execute(text(
            "SELECT * FROM interactions ORDER BY id")).all()
        activities = conn.execute(text(
            "SELECT * FROM activities ORDER BY id")).all()
    return [tuple(r) for r in interactions] + [tuple(r) for r in activities]


def _timeline_types(activities: list[dict]) -> list[str]:
    return [a["type"] for a in activities]


# ============================================================ S1 只读纪律（AST）


def test_s1_read_service_has_no_write_capability() -> None:
    """Observability Service AST：零写能力、零 Runtime/持久化依赖。"""
    src = (APP_DIR / "application" / "interaction_observability.py").read_text(
        encoding="utf-8")
    tree = ast.parse(src)
    called = {node.func.attr for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    forbidden = {"commit", "rollback", "create", "append", "claim_processing",
                 "record_outcome", "reclaim_stale_processing",
                 "bind_external_session", "execute", "update", "delete"}
    assert not called & forbidden, f"只读服务出现写能力调用：{called & forbidden}"

    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    for root in ("sqlalchemy", "fastapi", "app.persistence", "app.adapters"):
        assert not any(r == root or r.startswith(root + ".") for r in imports), \
            f"只读服务越层 import：{root}"


def test_s1_observability_api_has_no_write_method() -> None:
    """Observability API 只有 GET（无 POST/PATCH/PUT/DELETE 端点形状）。"""
    src = (APP_DIR / "api" / "management" / "interactions.py").read_text(
        encoding="utf-8")
    tree = ast.parse(src)
    methods = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for deco in node.decorator_list:
                if isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute):
                    methods.add(deco.func.attr)
    assert methods == {"get"}, f"Observability API 出现非 GET 端点：{methods}"


def test_s1_read_repository_methods_are_pure_select() -> None:
    """两个读方法函数体：只有 select —— 无 update/insert/delete/add/flush。"""
    forbidden_calls = {"update", "insert", "delete", "add", "flush", "commit"}

    def _method_calls(path: Path, name: str) -> set[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and node.name == name:
                return {n.func.attr for n in ast.walk(node)
                        if isinstance(n, ast.Call)
                        and isinstance(n.func, ast.Attribute)}
        raise AssertionError(f"方法不存在：{path.name}::{name}")

    inter = APP_DIR / "persistence" / "repositories" / "interaction_record.py"
    assert not _method_calls(inter, "list_recent") & forbidden_calls
    act = APP_DIR / "persistence" / "repositories" / "activity_record.py"
    assert not _method_calls(act, "list_by_interaction") & forbidden_calls


# ============================================== S2 成功故事（12.9.4.17 ⑥ 复刻）


def test_s2_http_driver_success_story_reconstructed_via_api_only(
        db_engine) -> None:
    """真实 E2E（⑥）：WeCom wire → ACK → Driver → 真六段 → API-only 还原故事。

    最终验收路径：ACK 后（PENDING 事实）→ Driver 完成 → 三次 GET 全程不碰
    DB，即可还原「收到 → 处理成功 → 投递成功 → Agent 回了什么」的完整故事。
    """
    g = _seed(db_engine)
    channel_adapter = _RecordingChannelAdapter()
    uow_factory = _uow(db_engine)
    runtime = _real_runtime(uow_factory, channel_adapter)
    phases = PersistingCoordinator(inner=runtime, uow_factory=uow_factory)
    service = ChannelInboundService(
        uow_factory=uow_factory, intake=phases,
        inbound_adapter=WeComAdapter(uow_factory=uow_factory))
    app = FastAPI()
    app.include_router(build_channel_inbound_api(service, _DeadWorker()))
    client = TestClient(app)
    params = _robot_post_params()

    # -- 真实入站：ACK ≠ 执行（BackgroundTask 兜底不生效，Driver 是完成者）
    resp = client.post(
        f"/api/channels/{g['channel']}/wecom/webhook",
        params={k: v for k, v in params.items() if k != "body"},
        content=params["body"].encode("utf-8"),
    )
    assert resp.status_code == 200

    api = _observability_app(db_engine)

    # -- API-only：找到这条 interaction（不查 DB、不按 Identity 猜测）
    listing = api.get("/api/interactions").json()
    assert listing["total"] == 1
    pending_item = listing["items"][0]
    interaction_id = pending_item["id"]
    assert pending_item["processing_status"] == "pending"   # ACK ≠ 执行的事实
    assert pending_item["delivery_status"] == "pending"

    # -- Driver 完成执行（真实 Worker + 真六段）
    driver = ProductionDriver(
        source=phases,
        worker=Worker(runtime=runtime, phases=phases, uow_factory=uow_factory),
        config=DriverConfig())
    assert driver.run_once() is True
    assert len(channel_adapter.deliveries) == 1

    # -- API-only：详情 = 完整执行故事
    detail = api.get(f"/api/interactions/{interaction_id}").json()
    assert detail["processing_status"] == "succeeded"
    assert detail["delivery_status"] == "delivered"
    assert detail["failure_stage"] is None          # 没有失败点（事实为空）
    assert detail["failure_code"] is None
    assert detail["session_id"] == g["session"]     # REUSE_ACTIVE 的会话归属
    # Who / Which Channel / Which Conversation（request_payload 快照直达）
    assert detail["external_interaction_id"] == ROBOT_TEXT["msgid"]
    assert detail["channel_id"] == g["channel"]
    assert detail["request_payload"]["content"] == ROBOT_TEXT["text"]["content"]
    assert detail["request_payload"]["external_identity_id"] == "zhangsan"
    assert detail["request_payload"]["metadata"][
        RESPONSE_URL_METADATA_KEY] == ROBOT_URL
    # Agent 回了什么（response_payload 原样快照，10.7.2 B 裁决）
    assert detail["response_payload"]["content"] == "echo:帮我查一下夹具规格"
    # Attempt fence / 审计时间戳（DB 写入，读路径回显）
    assert detail["processing_started_at"] is not None
    assert detail["created_at"] is not None
    assert detail["updated_at"] is not None
    # 执行故事时间线：received → completed（delivery 成功）
    types = _timeline_types(detail["activities"])
    assert types == ["interaction_received", "interaction_completed"]
    completed = detail["activities"][-1]
    assert completed["stage"] == "delivery"
    assert completed["status"] == "succeeded"
    for activity in detail["activities"]:
        assert activity["created_at"] is not None

    # -- 时间线端点与详情内嵌一致（同一事实，两种读法）
    timeline = api.get(f"/api/interactions/{interaction_id}/activities").json()
    assert [a["id"] for a in timeline["items"]] == \
        [a["id"] for a in detail["activities"]]
    assert timeline["total"] == len(detail["activities"])

    # -- 列表回显终态
    final = api.get("/api/interactions").json()["items"][0]
    assert final["processing_status"] == "succeeded"
    assert final["delivery_status"] == "delivered"


# ============================================== S3 投递失败故事（10.7.1 §1.2）


def test_s3_delivery_failure_story_reconstructed_via_api_only(db_engine) -> None:
    """「Agent 已成功 → 投递失败」：双状态合法组合 + 失败三元组原样还原。"""
    g = _seed(db_engine)
    uow_factory = _uow(db_engine)
    runtime = _real_runtime(uow_factory, _FailingChannelAdapter())
    phases = PersistingCoordinator(inner=runtime, uow_factory=uow_factory)
    worker = Worker(runtime=runtime, phases=phases, uow_factory=uow_factory)

    interaction_id = phases.create_pending(
        _inbound(g["channel"], "加工薄壁铝件"))
    with pytest.raises(Exception):  # noqa: B017  失败事实已在抛出前落定
        worker.process(interaction_id)

    api = _observability_app(db_engine)
    detail = api.get(f"/api/interactions/{interaction_id}").json()

    # 4.2 冻结的合法组合：processing=SUCCEEDED × delivery=FAILED（绝不合成）
    assert detail["processing_status"] == "succeeded"
    assert detail["delivery_status"] == "failed"
    assert detail["failure_stage"] == "delivery"
    assert detail["failure_code"] == "CHANNEL_DELIVERY_FAILED"
    assert detail["retryable"] is True
    assert detail["session_id"] == g["session"]   # 失败已发生在 Session 确定后
    assert detail["response_payload"] is not None       # P1/C3
    assert detail["response_payload"]["content"].startswith("echo:")
    # 执行故事：received → stage_failure（无 completed——失败路径不写完成 Evidence）
    types = _timeline_types(detail["activities"])
    assert types == ["interaction_received", "stage_failure"]
    failure = detail["activities"][-1]
    assert failure["stage"] == "delivery"
    assert failure["status"] == "failed"
    assert failure["message"]
    assert failure["metadata"]["failure_code"] == "CHANNEL_DELIVERY_FAILED"


# ====================================== S4 Session 建立前失败（identity 阶段）


def test_s4_pre_session_failure_story_reconstructed_via_api_only(
        db_engine) -> None:
    """Session 建立前失败：session_id=NULL 本身就是「还没走到会话」的事实。"""
    g = _seed(db_engine, with_identity=False)
    uow_factory = _uow(db_engine)
    runtime = _real_runtime(uow_factory, _RecordingChannelAdapter())
    phases = PersistingCoordinator(inner=runtime, uow_factory=uow_factory)
    worker = Worker(runtime=runtime, phases=phases, uow_factory=uow_factory)

    interaction_id = phases.create_pending(
        _inbound(g["channel"], "我是谁？"))
    with pytest.raises(Exception):  # noqa: B017  Fail Closed（9.2 冻结）
        worker.process(interaction_id)

    api = _observability_app(db_engine)
    detail = api.get(f"/api/interactions/{interaction_id}").json()

    assert detail["processing_status"] == "failed"
    assert detail["delivery_status"] == "pending"
    assert detail["failure_stage"] == "identity_resolution"
    assert detail["failure_code"] == "IDENTITY_RESOLUTION_FAILED"
    assert detail["retryable"] is False
    assert detail["session_id"] is None            # 前段失败：还没有 Session
    types = _timeline_types(detail["activities"])
    assert types == ["interaction_received", "stage_failure"]
    assert detail["activities"][-1]["stage"] == "identity_resolution"


# ============================================================ S5 列表面


def test_s5_list_is_summary_view_newest_first_with_bounded_limit(
        db_engine) -> None:
    """列表 = 陈列面：最新在前、不含 payload / 时间线、limit 合同面拒绝越界。"""
    g = _seed(db_engine)
    uow_factory = _uow(db_engine)
    runtime = _real_runtime(uow_factory, _RecordingChannelAdapter())
    phases = PersistingCoordinator(inner=runtime, uow_factory=uow_factory)
    worker = Worker(runtime=runtime, phases=phases, uow_factory=uow_factory)

    first = phases.create_pending(_inbound(g["channel"], "第一条", "msg-1"))
    assert worker.process(first) is True
    second = phases.create_pending(_inbound(g["channel"], "第二条", "msg-2"))
    assert worker.process(second) is True

    api = _observability_app(db_engine)
    body = api.get("/api/interactions").json()
    assert body["total"] == 2
    assert [i["id"] for i in body["items"]] == [second, first]  # 最新在前
    for item in body["items"]:
        assert "request_payload" not in item
        assert "response_payload" not in item
        assert "activities" not in item
        assert item["processing_status"] in {"pending", "processing",
                                             "succeeded", "failed"}

    assert api.get("/api/interactions", params={"limit": 1}).json()["total"] == 1
    assert api.get("/api/interactions", params={"limit": 0}).status_code == 422
    assert api.get("/api/interactions", params={"limit": 201}).status_code == 422


# ==================================================== S6 404 + 纯读证明


def test_s6_unknown_interaction_is_404_and_reads_change_nothing(
        db_engine) -> None:
    """不存在 → 404（详情与时间线一致）；三次 GET 后两表零变化。"""
    api = _observability_app(db_engine)
    assert api.get("/api/interactions/999").status_code == 404
    assert api.get("/api/interactions/999/activities").status_code == 404

    g = _seed(db_engine)
    uow_factory = _uow(db_engine)
    runtime = _real_runtime(uow_factory, _RecordingChannelAdapter())
    phases = PersistingCoordinator(inner=runtime, uow_factory=uow_factory)
    worker = Worker(runtime=runtime, phases=phases, uow_factory=uow_factory)
    interaction_id = phases.create_pending(_inbound(g["channel"]))
    worker.process(interaction_id)

    before = _snapshot(db_engine)
    assert api.get("/api/interactions").status_code == 200
    assert api.get(f"/api/interactions/{interaction_id}").status_code == 200
    assert api.get(
        f"/api/interactions/{interaction_id}/activities").status_code == 200
    assert _snapshot(db_engine) == before          # 行集合 + 全部列值零变化

    # 状态零跃迁：读路径不产生 claim / outcome / 新 Evidence
    detail = api.get(f"/api/interactions/{interaction_id}").json()
    assert detail["processing_status"] == "succeeded"
    assert _timeline_types(detail["activities"]) == [
        "interaction_received", "interaction_completed"]
