"""Step 12.9.4.17 —— Autonomous Async Runtime E2E Reliability Validation。

**不改架构**，只让真实系统跑起来并**主动制造故障**，验证六条可靠性路径：

    ① Restart Recovery     PENDING 跨 Driver / 进程重启，仍被新 Driver 完成
    ② Lost BackgroundTask  ACK 之后 BackgroundTask 丢失 → Driver 兜底
    ③ Worker Crash/Orphan  claim 后进程崩 → 超时 → reclaim → 新 Worker 完成
    ④ Old Worker Fence     旧 attempt 迟到落 outcome → 围栏 REJECTED，不写穿
    ⑤ Multi-Driver 竞争    两个 Driver 看到同一批 → CAS 保证恰好执行一次
    ⑥ HTTP + Driver        WeCom 真实 wire → ACK → Driver → Worker → 回执地址

方法纪律：全部跑**真实** Core（真库 / 真 phase API / 真 Worker / 真 Driver /
真 RuntimeCoordinator）。只有两处**对外网络跳**（Agent wire、Channel 出站）用
记录型 Adapter 替代——它们不是本步要验证的对象，且回归不能依赖外网。
"""

import json
import os
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.adapters.channels.wecom import (
    RESPONSE_URL_METADATA_KEY,
    WeComAdapter,
    _response_url_from,
    compute_signature,
    encrypt_message,
)
from app.api.runtime.channels import build_channel_inbound_api
from app.application.agent_dispatch import AgentDispatchExecutor
from app.application.channel_inbound import ChannelInboundService
from app.application.coordinator import RuntimeCoordinator
from app.application.delivery import DeliveryExecutor
from app.application.driver import DriverConfig, ProductionDriver
from app.application.access_authorization import AccessAuthorizationExecutor
from app.application.identity_resolution import IdentityResolutionExecutor
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
from app.domain.interaction_record import OutcomeCommit
from app.domain.outbound_response import OutboundResponse
from app.domain.runtime_execution_result import RuntimeExecutionResult
from app.domain.session import Session as SessionEntity
from app.domain.session_resolution import REUSE_ACTIVE
from app.domain.user import User
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

BACKEND = Path(__file__).resolve().parents[1]

# 与 test_step10_2_1_wecom_wire 同一套 wire 常量（真实验签 / 加密）
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

# 回收阈值相关：SQLite CURRENT_TIMESTAMP 为**秒级**精度，故以 1.5s 跨过一秒
ORPHAN_SLEEP_S = 1.5
RECLAIM_THRESHOLD_S = 0


# ================================================================== 测试底座


class _OkRuntime:
    """成功 Runtime（真实 Core 用）：记录执行次数与收到的 inbound。

    P1 / C1：投递是显式的第 7 段（由应用层在结果落库后发起）。
    """

    def __init__(self, result: RuntimeExecutionResult) -> None:
        self.result = result
        self.calls = 0
        self.deliveries: list = []

    def execute(self, inbound: InboundInteraction) -> RuntimeExecutionResult:
        self.calls += 1
        return self.result

    def deliver(self, outbound) -> None:
        self.deliveries.append(outbound)


class _DeadWorker:
    """老进程的 Worker：接到了触发，但进程随即崩溃——什么都没做。"""

    def __init__(self) -> None:
        self.calls: list[int] = []

    def process(self, interaction_id: int) -> bool:
        self.calls.append(interaction_id)
        return False


class _RecordingChannelAdapter:
    """出站记录器：不触网络，只记录真正要投递的 OutboundResponse。"""

    def __init__(self) -> None:
        self.deliveries: list[OutboundResponse] = []

    def deliver(self, outbound: OutboundResponse) -> None:
        self.deliveries.append(outbound)


class _EchoAgentAdapter:
    """Agent wire 替身：不触网络，回声式应答。"""

    def send(self, request, connection) -> AgentResponse:
        return AgentResponse(content=f"echo:{request.content}")


class _StubInboundAdapter:
    """入站适配器替身：直接吐出给定 InboundInteraction（绕开 wire 细节）。"""

    def __init__(self, inbound: InboundInteraction) -> None:
        self.inbound = inbound

    def verify_handshake(self, channel, *, signature, timestamp, nonce,
                         echostr):
        return echostr

    def parse_message(self, channel, *, signature, timestamp, nonce, body):
        return self.inbound


def _uow(db_engine):
    return lambda: SqlAlchemyUnitOfWork(
        session_factory=sessionmaker(bind=db_engine, expire_on_commit=False))


def _seed(db_engine, *, connection_config: dict | None = None) -> dict:
    """最小可用链路：user / channel / agent / identity / connection / session。

    `connection_config` 用于 ⑥-live 把 Agent wire 指向真实 Tencent ACP。
    """
    config = connection_config or {
        "endpoint": "http://agent.internal", "timeout_s": 5.0}
    with _uow(db_engine)() as uow:
        user = uow.users.create(User(name="alice"))
        channel = uow.channels.create(Channel(
            name="wecom-1", type="wecom",
            config={"token": TOKEN, "encoding_aes_key": AES_KEY,
                    "corp_id": CORP_ID, "receiveid": ""}))
        agent = uow.agents.create(Agent(name="agent-a", type="acp"))
        identity = uow.identities.create(Identity(
            user_id=user.id, channel_id=channel.id, external_id="zhangsan"))
        path = uow.connections.create(Connection(
            channel_id=channel.id, agent_id=agent.id, name="path-a",
            status="active",
            config=config, credential_ref="cred-a"))
        # Implementation Package 2 / Step 2：单聊会话键 = zhangsan
        uow.connection_conversations.create(ConnectionConversation(
            channel_id=channel.id, external_conversation_id="zhangsan",
            connection_id=path.id))
        session = uow.sessions.create(SessionEntity(
            identity_id=identity.id, channel_id=channel.id,
            external_conversation_id="zhangsan", status="active"))
        uow.commit()
        return {"channel": channel.id, "session": session.id}


def _inbound(channel_id: int, content: str = "你好") -> InboundInteraction:
    return InboundInteraction(
        channel_id=channel_id, external_interaction_id="msg-1",
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


def _ok_result(session_id: int, content: str) -> RuntimeExecutionResult:
    return RuntimeExecutionResult(
        session_mode=REUSE_ACTIVE, session_id=session_id,
        agent_response=AgentResponse(content=content))


def _row(db_engine, interaction_id: int):
    with _uow(db_engine)() as uow:
        return uow.interactions.get_by_id(interaction_id)


def _interaction_count(db_engine) -> int:
    with db_engine.connect() as conn:
        return conn.execute(text("SELECT COUNT(*) FROM interactions")).scalar()


def _phases(db_engine, inner):
    return PersistingCoordinator(inner=inner, uow_factory=_uow(db_engine))


# ================================================================== ① Restart Recovery


def test_1_pending_survives_driver_restart_and_new_driver_completes_it(
        db_engine) -> None:
    """PENDING → 老 Driver 停摆（进程退出）→ 新进程的新 Driver → 完成。

     durability 的唯一来源是 DB，不是 Driver 的内存：老 Driver 拿到过这个 id，
     但进程崩了，工作必须原封不动地留给新 Driver。
    """
    g = _seed(db_engine)
    old_runtime = _OkRuntime(_ok_result(g["session"], "old"))
    phases_old = _phases(db_engine, old_runtime)
    dead_worker = _DeadWorker()
    old_driver = ProductionDriver(source=phases_old, worker=dead_worker,
                                  config=DriverConfig())
    interaction_id = phases_old.create_pending(_inbound(g["channel"]))

    old_driver.start()
    old_driver.stop(timeout=5.0)

    assert dead_worker.calls == [interaction_id]      # 老 Driver 确实接到过
    assert old_runtime.calls == 0
    assert _row(db_engine, interaction_id).processing_status == "pending"

    # 新进程：全新的 Driver / Worker / phase API——**只有 DB 是共享的**
    new_runtime = _OkRuntime(_ok_result(g["session"], "new"))
    phases_new = _phases(db_engine, new_runtime)
    worker_new = Worker(runtime=new_runtime, phases=phases_new,
                        uow_factory=_uow(db_engine))
    new_driver = ProductionDriver(source=phases_new, worker=worker_new,
                                  config=DriverConfig())

    assert new_driver.run_once() is True
    assert new_runtime.calls == 1
    assert _row(db_engine, interaction_id).processing_status == "succeeded"


# ================================================================== ② Lost Task


def test_2_lost_background_task_is_recovered_by_the_driver(db_engine) -> None:
    """create_pending → ACK → BackgroundTask 从未执行 → Driver 兜底完成。

    接纳层**绝不执行**（runtime.calls == 0）；响应之后那条执行触发丢了也没关系——
    PENDING 是既成事实，Driver 会把它捡回来。
    """
    g = _seed(db_engine)
    runtime = _OkRuntime(_ok_result(g["session"], "ok"))
    phases = _phases(db_engine, runtime)
    service = ChannelInboundService(
        uow_factory=_uow(db_engine), intake=phases,
        inbound_adapter=_StubInboundAdapter(_inbound(g["channel"])))

    interaction_id = service.handle_message(
        g["channel"], signature="s", timestamp="t", nonce="n", body="b")

    assert interaction_id is not None
    assert runtime.calls == 0                          # 接纳 = 落库，不是执行
    assert _row(db_engine, interaction_id).processing_status == "pending"

    # BackgroundTask 丢失：这个 id 从未被交给任何 Worker
    driver = ProductionDriver(
        source=phases,
        worker=Worker(runtime=runtime, phases=phases,
                      uow_factory=_uow(db_engine)),
        config=DriverConfig())

    assert driver.run_once() is True
    assert runtime.calls == 1
    assert _row(db_engine, interaction_id).processing_status == "succeeded"


# ================================================================== ③ Orphan


def test_3_orphaned_processing_is_reclaimed_then_reexecuted(db_engine) -> None:
    """claim → PROCESSING → Worker 崩 → 超阈值 → reclaim → 新 Worker 完成。

    reclaim 必须把 `processing_started_at` 置回 NULL——这是新旧 attempt 的围栏
    不碰撞的前提（12.9.4.11 D.2）。
    """
    g = _seed(db_engine)
    runtime = _OkRuntime(_ok_result(g["session"], "ok"))
    phases = _phases(db_engine, runtime)
    interaction_id = phases.create_pending(_inbound(g["channel"]))

    assert phases.claim_processing(interaction_id) is True    # Worker 领到执行权
    assert _row(db_engine, interaction_id).processing_status == "processing"

    time.sleep(ORPHAN_SLEEP_S)                                # Worker 已崩，等超时

    worker = Worker(runtime=runtime, phases=phases,
                    uow_factory=_uow(db_engine))
    driver = ProductionDriver(
        source=phases, worker=worker,
        config=DriverConfig(reclaim_every=1,
                            stale_threshold_seconds=RECLAIM_THRESHOLD_S))

    driver.run_once()                                  # 第一轮：只回收
    row = _row(db_engine, interaction_id)
    assert row.processing_status == "pending"
    assert row.processing_started_at is None           # reclaim 必置 NULL
    assert runtime.calls == 0                          # 回收本身不执行

    driver.run_once()                                  # 第二轮：新 Worker 接手
    assert runtime.calls == 1
    assert _row(db_engine, interaction_id).processing_status == "succeeded"


# ================================================================== ④ Old Worker Fence


def test_4_late_outcome_from_old_attempt_is_rejected(db_engine) -> None:
    """Worker A(T1) 崩 → 回收 → Worker B(T2) 成功 → A 带 T1 迟到 → REJECTED。

    关键断言：旧 attempt 的结果**不会写穿**新 attempt 已落定的终态。
    """
    g = _seed(db_engine)
    runtime_b = _OkRuntime(_ok_result(g["session"], "from-B"))
    phases = _phases(db_engine, runtime_b)
    interaction_id = phases.create_pending(_inbound(g["channel"]))

    assert phases.claim_processing(interaction_id) is True
    t1 = _row(db_engine, interaction_id).processing_started_at
    assert t1 is not None

    time.sleep(ORPHAN_SLEEP_S)
    phases.reclaim_stale_processing(RECLAIM_THRESHOLD_S)   # A 的 attempt 被回收

    worker_b = Worker(runtime=runtime_b, phases=phases,
                      uow_factory=_uow(db_engine))
    assert worker_b.process(interaction_id) is True         # B 重新 claim → T2
    assert runtime_b.calls == 1
    row_after_b = _row(db_engine, interaction_id)
    assert row_after_b.processing_status == "succeeded"
    assert row_after_b.processing_started_at != t1

    # Worker A 复活，带着 T1 迟到落 outcome
    late = phases.record_agent_success(
        interaction_id, _ok_result(g["session"], "from-A"),
        expected_started_at=t1)

    assert late is OutcomeCommit.REJECTED
    final = _row(db_engine, interaction_id)
    assert final.processing_status == "succeeded"
    assert final.response_payload["content"] == "from-B"   # 未被旧 attempt 写穿


# ================================================================== ⑤ Multi-Driver 竞争


def test_5_two_drivers_competing_execute_exactly_once(db_engine) -> None:
    """两个 Driver（= 两个进程）看到同一批候选 → CAS 保证 Agent 执行恰好 1 次。

    find_pending 是纯读，**overlap 是设计允许的**；唯一性完全由随后那一条
    条件化 UPDATE（claim）保证——而不是靠进程内的锁。
    """
    g = _seed(db_engine)
    runtime = _OkRuntime(_ok_result(g["session"], "ok"))
    phases = _phases(db_engine, runtime)
    interaction_id = phases.create_pending(_inbound(g["channel"]))

    batch_a = phases.find_pending(10)
    batch_b = phases.find_pending(10)
    assert [r.id for r in batch_a] == [interaction_id]     # 两个 Driver 看到同一批
    assert [r.id for r in batch_b] == [interaction_id]

    driver_a = ProductionDriver(
        source=phases,
        worker=Worker(runtime=runtime, phases=phases,
                      uow_factory=_uow(db_engine)),
        config=DriverConfig())
    driver_b = ProductionDriver(
        source=phases,
        worker=Worker(runtime=runtime, phases=phases,
                      uow_factory=_uow(db_engine)),
        config=DriverConfig())

    driver_a.run_once()
    driver_b.run_once()

    assert runtime.calls == 1                       # Agent 执行 = 1
    assert _interaction_count(db_engine) == 1       # Interaction 行 = 1
    assert _row(db_engine, interaction_id).processing_status == "succeeded"


# ================================================================== ⑥ HTTP + Driver


def _robot_post_params() -> dict[str, str]:
    """智能机器人明文 JSON → 真实 WeCom POST 事实（验签 + 加密信封）。"""
    encrypt = encrypt_message(json.dumps(ROBOT_TEXT, ensure_ascii=False),
                              AES_KEY, "")
    return {
        "msg_signature": compute_signature(TOKEN, TIMESTAMP, NONCE, encrypt),
        "timestamp": TIMESTAMP, "nonce": NONCE,
        "body": json.dumps({"encrypt": encrypt}, ensure_ascii=False),
    }


def test_6_http_ack_then_driver_completes_via_response_url(db_engine) -> None:
    """WeCom 真实 wire → HTTP 200 ACK → Driver → 真六段 → 回执地址投递。

    三条一起验证：
    - **ACK ≠ 执行**：响应已回，工作仍是 PENDING；
    - Driver 是完成者（路由里的 BackgroundTask 故意不生效，模拟进程响应后即崩）；
    - `response_url` 经「落库快照 → Worker 重建 → Coordinator 合并」后仍在，
      出站仍走机器人回执通道（不静默回退 message/send）。
    """
    g = _seed(db_engine)
    uow_factory = _uow(db_engine)
    channel_adapter = _RecordingChannelAdapter()
    runtime = RuntimeCoordinator(
        identity_resolution=IdentityResolutionExecutor(uow_factory),
        access_authorization=AccessAuthorizationExecutor(uow_factory),
        routing=RoutingExecutor(uow_factory),
        session_resolution=SessionResolutionExecutor(uow_factory),
        session_creation=SessionCreationExecutor(uow_factory),
        agent_dispatch=AgentDispatchExecutor(
            agent_adapter=_EchoAgentAdapter(), uow_factory=uow_factory),
        delivery=DeliveryExecutor(channel_adapter=channel_adapter),
    )
    phases = PersistingCoordinator(inner=runtime, uow_factory=uow_factory)
    service = ChannelInboundService(
        uow_factory=uow_factory, intake=phases,
        inbound_adapter=WeComAdapter(uow_factory=uow_factory))

    # 路由收到一个「BackgroundTask 不生效」的 Worker：模拟响应发出后进程即崩
    lost_worker = _DeadWorker()
    app = FastAPI()
    app.include_router(build_channel_inbound_api(service, lost_worker))
    client = TestClient(app)
    params = _robot_post_params()

    resp = client.post(
        f"/api/channels/{g['channel']}/wecom/webhook",
        params={k: v for k, v in params.items() if k != "body"},
        content=params["body"].encode("utf-8"),
    )

    assert resp.status_code == 200
    assert resp.content == b""                     # ACK 空体
    interaction_id = _row_by_external(
        db_engine, g["channel"], ROBOT_TEXT["msgid"])
    assert lost_worker.calls == [interaction_id]
    assert _row(db_engine, interaction_id).processing_status == "pending"  # ACK≠执行

    driver = ProductionDriver(
        source=phases,
        worker=Worker(runtime=runtime, phases=phases, uow_factory=uow_factory),
        config=DriverConfig())

    assert driver.run_once() is True

    row = _row(db_engine, interaction_id)
    assert row.processing_status == "succeeded"
    assert row.delivery_status == "delivered"
    assert len(channel_adapter.deliveries) == 1
    outbound = channel_adapter.deliveries[0]
    assert outbound.content == "echo:帮我查一下夹具规格"
    assert outbound.metadata[RESPONSE_URL_METADATA_KEY] == ROBOT_URL
    assert _response_url_from(outbound) == ROBOT_URL   # 仍走机器人回执通道


def _row_by_external(db_engine, channel_id: int,
                     external_interaction_id: str) -> int:
    """按 Interaction Identity 取回落库的 interaction id。"""
    with _uow(db_engine)() as uow:
        record = uow.interactions.find_by_channel_and_external_id(
            channel_id, external_interaction_id)
    assert record is not None
    return record.id


# ========================================== ⑥-live Tencent ACP（env 门控）

_LIVE_ENV = ("TENCENT_ACP_URL", "TENCENT_ACP_TOKEN", "TENCENT_RUNTIME_ID")


@pytest.fixture()
def live_env() -> dict[str, str]:
    try:
        from dotenv import load_dotenv

        load_dotenv(BACKEND / ".env", override=False)
    except ImportError:
        pass
    values = {name: os.environ.get(name, "").strip() for name in _LIVE_ENV}
    if not all(values.values()):
        pytest.skip("未配置 TENCENT_ACP_URL/TOKEN/RUNTIME_ID（真实 ACP 链路）")
    return values


@pytest.mark.live
def test_6_live_http_driver_real_tencent_acp(db_engine, live_env) -> None:
    """真实 Tencent ACP：HTTP ACK → Driver → Worker → 真实 ACP 应答（出站仍记录）。

    与离线版同一条链，只把 Agent wire 换成**真实** ACPAdapter；env 缺省则 skip
    （12.8.6 既有先例），不让回归依赖外网。
    """
    from app.adapters.agents.acp import PROTOCOL_TENCENT_ACP_SSE, ACPAdapter

    g = _seed(db_engine, connection_config={
        "endpoint": live_env["TENCENT_ACP_URL"],
        "protocol": PROTOCOL_TENCENT_ACP_SSE,
        "bearer_token": live_env["TENCENT_ACP_TOKEN"],
        "runtime_id": live_env["TENCENT_RUNTIME_ID"],
        "timeout_s": 60.0,
    })
    uow_factory = _uow(db_engine)
    channel_adapter = _RecordingChannelAdapter()
    runtime = RuntimeCoordinator(
        identity_resolution=IdentityResolutionExecutor(uow_factory),
        access_authorization=AccessAuthorizationExecutor(uow_factory),
        routing=RoutingExecutor(uow_factory),
        session_resolution=SessionResolutionExecutor(uow_factory),
        session_creation=SessionCreationExecutor(uow_factory),
        agent_dispatch=AgentDispatchExecutor(
            agent_adapter=ACPAdapter(), uow_factory=uow_factory),
        delivery=DeliveryExecutor(channel_adapter=channel_adapter),
    )
    phases = PersistingCoordinator(inner=runtime, uow_factory=uow_factory)
    interaction_id = phases.create_pending(_robot_inbound(g["channel"]))

    driver = ProductionDriver(
        source=phases,
        worker=Worker(runtime=runtime, phases=phases, uow_factory=uow_factory),
        config=DriverConfig())

    assert driver.run_once() is True
    row = _row(db_engine, interaction_id)
    assert row.processing_status == "succeeded"
    assert len(channel_adapter.deliveries) == 1
    assert _response_url_from(channel_adapter.deliveries[0]) == ROBOT_URL
