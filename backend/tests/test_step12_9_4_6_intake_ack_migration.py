"""Step 12.9.4.6 —— Intake ACK Boundary Migration（原子切刀）。

迁移后的架构事实（**Inbound ACK ≠ Agent Execution**）：

    ChannelInboundService → create_pending → COMMIT → HTTP 200
                                                ↓（响应之后）
                                    Worker.process(id) → claim → reconstruct
                                        → Runtime.execute → record outcome

七条 Migration Guard：

    G1  ChannelInboundService → Runtime.execute = 0
    G2  Worker → Runtime.execute = 1
    G3  production RuntimeCoordinator instance = 1
    G4  Inbound HTTP response occurs after create_pending COMMIT
    G5  HTTP response does NOT wait for Agent execution
    G6  PENDING interaction can exist without immediate execution
    G7  no old synchronous execution path remains
"""

import ast
from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.adapters.agents.acp import ACPAdapter
from app.adapters.channels.wecom import WeComAdapter
from app.api.runtime.channels import build_channel_inbound_api
from app.application import RuntimeCoordinator
from app.application.agent_dispatch import AgentDispatchExecutor
from app.application.channel_inbound import ChannelInboundService
from app.application.delivery import DeliveryExecutor
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
from app.domain.outbound_response import OutboundResponse
from app.domain.runtime_execution_result import RuntimeExecutionResult
from app.domain.session_resolution import REUSE_ACTIVE
from app.domain.user import User
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

BACKEND = Path(__file__).resolve().parents[1]


# ================================================================== 测试底座


class _SpyRuntime:
    """执行探针：记录 Runtime.execute 被调用的次数（G1 = 0 次 / G2 = 1 次的依据）。

    `result` 为真实 RuntimeExecutionResult（record_success 需要 session FK）；
    G1 场景下执行次数为 0，result 永不被消费。
    """

    def __init__(self, result=None) -> None:
        self.result = result
        self.calls = 0

    def execute(self, inbound: InboundInteraction):
        self.calls += 1
        return self.result


class _EchoAgentAdapter:
    def send(self, request, connection):
        from app.domain.agent_response import AgentResponse
        return AgentResponse(content=f"echo:{request.content}")


class _RecordingChannelAdapter:
    def __init__(self) -> None:
        self.deliveries: list[OutboundResponse] = []

    def deliver(self, outbound: OutboundResponse) -> None:
        self.deliveries.append(outbound)


class _ScriptedInboundAdapter:
    """协议无关 Fake Adapter：入站 wire 不在本步验收范围。"""

    def __init__(self, result) -> None:
        self.result = result

    def verify_handshake(self, channel, **kwargs) -> str:
        return "echo"

    def parse_message(self, channel, **kwargs):
        if callable(self.result):
            return self.result(channel)
        return self.result


def _uow_factory(db_engine) -> Callable[[], SqlAlchemyUnitOfWork]:
    return lambda: SqlAlchemyUnitOfWork(
        session_factory=sessionmaker(bind=db_engine, expire_on_commit=False))


def _seed_channel(db_engine) -> int:
    with _uow_factory(db_engine)() as uow:
        channel = uow.channels.create(Channel(name="wecom-1", type="wecom"))
        uow.commit()
        return channel.id


def _seed_full(db_engine, with_session: bool = False) -> dict:
    """完整 Runtime 链路种子（HTTP E2E / Worker outcome 用）。"""
    with _uow_factory(db_engine)() as uow:
        alice = uow.users.create(User(name="alice"))
        channel = uow.channels.create(Channel(name="wecom-1", type="wecom"))
        agent = uow.agents.create(Agent(name="agent-a", type="acp"))
        identity = uow.identities.create(Identity(
            user_id=alice.id, channel_id=channel.id, external_id="zhangsan"))
        path = uow.connections.create(Connection(
            channel_id=channel.id, agent_id=agent.id, name="path-a",
            status="active",
            config={"endpoint": "http://agent.internal", "timeout_s": 5.0},
            credential_ref="cred-a"))
        # Implementation Package 2 / Step 2：单聊会话键 = FromUserName(zhangsan)
        uow.connection_conversations.create(ConnectionConversation(
            channel_id=channel.id, external_conversation_id="zhangsan",
            connection_id=path.id))
        session_id = None
        if with_session:
            from app.domain.session import Session as SessionEntity
            session_id = uow.sessions.create(SessionEntity(
                identity_id=identity.id, channel_id=channel.id,
                external_conversation_id="conv-1", status="active")).id
        uow.commit()
        return {"channel": channel.id, "session": session_id}


def _service(db_engine, adapter, spy: _SpyRuntime) -> ChannelInboundService:
    """生产同形装配：intake（phase API）+ 同一个 spy runtime 交给 Worker。"""
    uow = _uow_factory(db_engine)
    phases = PersistingCoordinator(inner=spy, uow_factory=uow)
    return ChannelInboundService(
        uow_factory=uow, intake=phases, inbound_adapter=adapter)


def _worker(db_engine, spy: _SpyRuntime) -> Worker:
    uow = _uow_factory(db_engine)
    phases = PersistingCoordinator(inner=spy, uow_factory=uow)
    return Worker(runtime=spy, phases=phases, uow_factory=uow)


def _row(db_engine, interaction_id: int):
    with _uow_factory(db_engine)() as uow:
        return uow.interactions.get_by_id(interaction_id)


# ================================================================== G1：入站链路零执行


def test_g1_source_inbound_service_has_zero_runtime_execution() -> None:
    """G1（源码）：ChannelInboundService 不再出现任何执行调用 / Runtime 依赖。"""
    src = (BACKEND / "app" / "application" / "channel_inbound.py").read_text(
        encoding="utf-8")
    assert ".execute(" not in src
    assert "from app.application.runtime" not in src       # 不再依赖执行 Protocol
    assert "from app.application.coordinator" not in src   # 不 import 具体类


def test_g1_behavior_handle_message_never_executes(db_engine) -> None:
    """G1（行为）：handle_message 全程不触碰 Runtime（安全阀在入站侧成立）。"""
    channel_id = _seed_channel(db_engine)
    spy = _SpyRuntime()
    adapter = _ScriptedInboundAdapter(lambda ch: InboundInteraction(
        channel_id=ch.id, external_interaction_id="m-1",
        external_conversation_id="conv-1", external_identity_id="zhangsan",
        content="hello"))
    service = _service(db_engine, adapter, spy)

    interaction_id = service.handle_message(
        channel_id, signature="s", timestamp="t", nonce="n", body="b")

    assert interaction_id is not None
    assert spy.calls == 0                                   # ← G1：0 次执行
    assert _row(db_engine, interaction_id).processing_status == "pending"


# ================================================================== G2：Worker 唯一执行者


def test_g2_source_worker_is_the_single_execution_callsite() -> None:
    """G2（源码）：`self._runtime.execute(` 在 app/ 中唯一的调用点是 worker.py。"""
    hits = [p.name for p in (BACKEND / "app").rglob("*.py")
            if "self._runtime.execute(" in p.read_text(encoding="utf-8")]
    assert hits == ["worker.py"], f"执行调用点漂移：{hits}"


def test_g2_behavior_worker_executes_exactly_once(db_engine) -> None:
    """G2（行为）：Worker.process 恰好触发一次 Runtime.execute。"""
    g = _seed_full(db_engine, with_session=True)
    spy = _SpyRuntime(RuntimeExecutionResult(
        session_mode=REUSE_ACTIVE, session_id=g["session"],
        agent_response=AgentResponse(content="ok")))
    adapter = _ScriptedInboundAdapter(lambda ch: InboundInteraction(
        channel_id=ch.id, external_interaction_id="m-1",
        external_conversation_id="conv-1", external_identity_id="zhangsan",
        content="hello"))
    service = _service(db_engine, adapter, spy)
    interaction_id = service.handle_message(
        g["channel"], signature="s", timestamp="t", nonce="n", body="b")

    assert _worker(db_engine, spy).process(interaction_id) is True
    assert spy.calls == 1                                   # ← G2：1 次执行


# ================================================================== G3：生产实例唯一


def test_g3_production_runtime_coordinator_instantiated_once() -> None:
    """G3：RuntimeCoordinator 的生产实例化点唯一（main.py 组合根，AST 判定）。"""
    hits = []
    for path in (BACKEND / "app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "RuntimeCoordinator"):
                hits.append(path.name)
    assert hits == ["main.py"], \
        f"RuntimeCoordinator 生产实例化应唯一（main.py），实际：{hits}"


# ================================================================== G4：ACK 在 COMMIT 之后


def test_g4_ack_returned_after_pending_commit(db_engine) -> None:
    """G4：handle_message 返回时，PENDING 行已**跨事务可见**（COMMIT 已发生）。"""
    channel_id = _seed_channel(db_engine)
    spy = _SpyRuntime()
    adapter = _ScriptedInboundAdapter(lambda ch: InboundInteraction(
        channel_id=ch.id, external_interaction_id="m-1",
        external_conversation_id="conv-1", external_identity_id="zhangsan",
        content="hello"))
    service = _service(db_engine, adapter, spy)

    interaction_id = service.handle_message(
        channel_id, signature="s", timestamp="t", nonce="n", body="b")

    # 全新 Session（与写入事务无关）能读到该行 → COMMIT 已发生，ACK 才返回
    row = _row(db_engine, interaction_id)
    assert row is not None
    assert row.processing_status == "pending"
    assert row.request_payload["content"] == "hello"


# ================================================================== G5：响应不等执行


def test_g5_http_layer_defers_execution_to_after_response() -> None:
    """G5：HTTP 层经 BackgroundTasks 把执行放到**响应之后**（Starlette 契约：
    background tasks 在 response 发送完成后才运行），且位于入站接纳返回后。

    Cloud-0.1.3.3：路由改用 `accept_message`（需要已认证 TenantScope 以解析
    per-tenant Worker）——ACK ≠ 执行的边界语义完全不变。
    """
    src = (BACKEND / "app" / "api" / "runtime" / "channels.py").read_text(
        encoding="utf-8")
    assert "BackgroundTasks" in src                          # 后置执行机制
    assert "acceptance = service.accept_message(" in src
    assert "background_tasks.add_task(" in src
    assert "_worker_for(acceptance.tenant_scope).process" in src
    # add_task 在 accept_message **之后**（源码顺序 = 事实顺序）
    assert src.index("service.accept_message(") < \
        src.index("background_tasks.add_task(")


def test_g5_service_returns_before_any_agent_execution(db_engine) -> None:
    """G5（行为）：产生 ACK 的调用返回时，Agent 执行次数为 0——响应不等执行。"""
    channel_id = _seed_channel(db_engine)
    spy = _SpyRuntime()
    adapter = _ScriptedInboundAdapter(lambda ch: InboundInteraction(
        channel_id=ch.id, external_interaction_id="m-2",
        external_conversation_id="conv-1", external_identity_id="zhangsan",
        content="hello"))
    service = _service(db_engine, adapter, spy)

    interaction_id = service.handle_message(
        channel_id, signature="s", timestamp="t", nonce="n", body="b")

    assert interaction_id is not None
    assert spy.calls == 0          # 此刻（= ACK 时点）Agent 尚未执行


# ================================================================== G6：PENDING 可独立存在


def test_g6_pending_exists_without_execution(db_engine) -> None:
    """G6：PENDING 是可持久的独立状态——不调用 Worker 也真实存在、可被 pickup。"""
    channel_id = _seed_channel(db_engine)
    spy = _SpyRuntime()
    adapter = _ScriptedInboundAdapter(lambda ch: InboundInteraction(
        channel_id=ch.id, external_interaction_id="m-3",
        external_conversation_id="conv-1", external_identity_id="zhangsan",
        content="hello"))
    service = _service(db_engine, adapter, spy)
    interaction_id = service.handle_message(
        channel_id, signature="s", timestamp="t", nonce="n", body="b")

    # 不调用 worker：行保持 PENDING，且出现在 pickup 队列里
    row = _row(db_engine, interaction_id)
    assert row.processing_status == "pending"
    with _uow_factory(db_engine)() as uow:
        pending_ids = [r.id for r in uow.interactions.find_pending()]
    assert pending_ids == [interaction_id]
    assert spy.calls == 0           # 全程零执行


# ================================================================== G7：旧路径消失


def test_g7_no_old_synchronous_execution_path_remains() -> None:
    """G7：app/ 中 runtime 执行调用点仅剩 Worker（新 owner）与遗留 wrapper 定义；
    入站链路与组合根不再有任何执行调用。"""
    offenders = [
        str(p.relative_to(BACKEND))
        for p in (BACKEND / "app").rglob("*.py")
        if ".execute(inbound)" in p.read_text(encoding="utf-8")
        and p.name not in ("worker.py", "interaction_recording.py")
    ]
    # 精确匹配 `.execute(inbound)`：以完整入站对象驱动的执行入口。
    # coordinator.py 的 `self._routing.execute(inbound.channel_id)` 是六段内部的
    # Executor 调用（传 channel_id），不是 Runtime 入口执行，不算旧路径。
    assert not offenders, f"出现新的执行调用点：{offenders}"

    inbound_src = (BACKEND / "app" / "application"
                   / "channel_inbound.py").read_text(encoding="utf-8")
    assert ".execute(" not in inbound_src
    main_src = (BACKEND / "app" / "main.py").read_text(encoding="utf-8")
    assert ".execute(" not in main_src          # 组合根不执行任何东西


# ================================================================== HTTP E2E（生产同形）


def _build_app(db_engine, channel_id: int) -> TestClient:
    """生产同形装配：intake + Worker + BackgroundTasks 后置执行。"""
    uow = _uow_factory(db_engine)
    runtime = RuntimeCoordinator(
        identity_resolution=IdentityResolutionExecutor(uow),
        access_authorization=AccessAuthorizationExecutor(uow),
        routing=RoutingExecutor(uow),
        session_resolution=SessionResolutionExecutor(uow),
        session_creation=SessionCreationExecutor(uow),
        agent_dispatch=AgentDispatchExecutor(
            agent_adapter=_EchoAgentAdapter(), uow_factory=uow),
        delivery=DeliveryExecutor(channel_adapter=_RecordingChannelAdapter()),
    )
    phases = PersistingCoordinator(inner=runtime, uow_factory=uow)
    worker = Worker(runtime=runtime, phases=phases, uow_factory=uow)
    service = ChannelInboundService(
        uow_factory=uow, intake=phases,
        inbound_adapter=_ScriptedInboundAdapter(
            lambda ch: InboundInteraction(
                channel_id=ch.id, external_interaction_id="m-e2e",
                external_conversation_id="zhangsan",
                external_identity_id="zhangsan", content="hello")))
    app = FastAPI()
    app.include_router(build_channel_inbound_api(service, worker))
    return TestClient(app)


def test_http_post_acks_then_worker_executes(db_engine) -> None:
    """HTTP E2E：POST → 200（ACK）→ Worker 后置执行 → 终态落定（生产同形全链）。"""
    g = _seed_full(db_engine)
    client = _build_app(db_engine, g["channel"])

    resp = client.post(
        f"/api/channels/{g['channel']}/wecom/webhook",
        params={"msg_signature": "s", "timestamp": "t", "nonce": "n"},
        content=b"body")

    assert resp.status_code == 200
    with _uow_factory(db_engine)() as uow:
        rows = uow.interactions.find_pending()
    assert rows == []                       # 已被 Worker 取走执行


def test_http_non_text_acks_without_enqueuing(db_engine) -> None:
    """acknowledge-ignore：Adapter 返回 None → 200，不产生任何 PENDING。"""
    g = _seed_full(db_engine)
    uow = _uow_factory(db_engine)
    runtime = RuntimeCoordinator(
        identity_resolution=IdentityResolutionExecutor(uow),
        access_authorization=AccessAuthorizationExecutor(uow),
        routing=RoutingExecutor(uow),
        session_resolution=SessionResolutionExecutor(uow),
        session_creation=SessionCreationExecutor(uow),
        agent_dispatch=AgentDispatchExecutor(
            agent_adapter=_EchoAgentAdapter(), uow_factory=uow),
        delivery=DeliveryExecutor(channel_adapter=_RecordingChannelAdapter()),
    )
    phases = PersistingCoordinator(inner=runtime, uow_factory=uow)
    worker = Worker(runtime=runtime, phases=phases, uow_factory=uow)
    service = ChannelInboundService(
        uow_factory=uow, intake=phases,
        inbound_adapter=_ScriptedInboundAdapter(None))
    app = FastAPI()
    app.include_router(build_channel_inbound_api(service, worker))
    client = TestClient(app)

    resp = client.post(
        f"/api/channels/{g['channel']}/wecom/webhook",
        params={"msg_signature": "s", "timestamp": "t", "nonce": "n"},
        content=b"body")

    assert resp.status_code == 200
    with _uow_factory(db_engine)() as uow:
        assert uow.interactions.find_pending() == []
