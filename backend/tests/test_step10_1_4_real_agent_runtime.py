"""Step 10.1.4.2 —— Real Tencent Agent Runtime E2E（CREATE_NEW）。

目标：证明既有 Runtime Vertical Slice 能**零生产代码改动**地承载真实 Tencent
Enterprise ACP Runtime：

    InboundInteraction
        ↓ RuntimeCoordinator.execute（唯一入口，禁止直接调 ACPAdapter.send）
        ↓ IdentityResolution → Routing → SessionResolution
        ↓ CREATE_NEW → SessionCreation（真实 Control Plane Session 物化）
        ↓ AgentDispatch（注入真实 ACPAdapter）→ 真实 Tencent Runtime
        ↓ Delivery（Test Channel Adapter sink）
        ↓ OutboundResponse

与 10.1.3 的区别：
- 10.1.3 = wire calibration，直接 ACPAdapter.send(request, connection)；
- 10.1.4.2 = **Runtime E2E**，从 RuntimeCoordinator 进入，只缺真实 Tencent
  Connection 的装配（10.1.4.1 审计结论：Production code changes = 0）。

实验变量：Channel = Test Channel（Delivery sink）；Runtime Kernel = 既有六段
Executor（**零改动**）；Adapter = **真实 ACPAdapter**（tencent-acp-sse binding）；
Agent = **真实 Tencent ACP Runtime**（env 提供 acpLink 三值）。

本测试只验证 CREATE_NEW 分支；REUSE_ACTIVE 分支留到 Step 10.1.4.3。

env 门控（backend/.env 或进程环境变量，缺任一 → skip）：
    TENCENT_ACP_URL
    TENCENT_ACP_TOKEN
    TENCENT_RUNTIME_ID
    TENCENT_ACP_TIMEOUT_S（可选，默认 60）
"""

import os
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.agents.acp import PROTOCOL_TENCENT_ACP_SSE, ACPAdapter
from app.application import RuntimeCoordinator
from app.application.agent_dispatch import AgentDispatchExecutor
from app.application.delivery import DeliveryExecutor, build_outbound_response
from app.application.access_authorization import AccessAuthorizationExecutor
from app.application.identity_resolution import IdentityResolutionExecutor
from app.application.routing import RoutingExecutor
from app.application.session_creation import SessionCreationExecutor
from app.application.session_resolution import SessionResolutionExecutor
from app.domain.agent import Agent
from app.domain.channel import Channel
from app.domain.connection import Connection
from app.domain.connection_conversation import ConnectionConversation
from app.domain.identity import Identity
from app.domain.inbound_interaction import InboundInteraction
from app.domain.outbound_response import OutboundResponse
from app.domain.user import User
from app.persistence.models.session import SessionModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork
from app.repositories.agent_adapter import AgentAdapter

BACKEND_DIR = Path(__file__).resolve().parents[1]

_LIVE_ENV = ("TENCENT_ACP_URL", "TENCENT_ACP_TOKEN", "TENCENT_RUNTIME_ID")


def _load_live_env() -> None:
    """从 backend/.env 加载 TENCENT_ACP_*（如已由进程 env 提供则不变）。

    凭据只经 env（进程环境变量或 .env 文件）注入，**不写入代码 / 测试 fixture**；
    .env 已被仓库 .gitignore 排除。
    """
    from dotenv import load_dotenv

    load_dotenv(BACKEND_DIR / ".env", override=False)


def _live_values() -> dict[str, str] | None:
    _load_live_env()
    values = {name: os.environ.get(name, "").strip() for name in _LIVE_ENV}
    if not all(values.values()):
        return None
    return values


# ------------------------------------------------------------------ 测试底座（复用 9.8/10.1.2 装配模式）


class _TestChannelAdapter:
    def __init__(self) -> None:
        self.deliveries: list[OutboundResponse] = []

    def deliver(self, outbound: OutboundResponse) -> None:
        self.deliveries.append(outbound)


def _factory(db_engine) -> sessionmaker:
    return sessionmaker(bind=db_engine, class_=Session, expire_on_commit=False)


def _uow_factory(db_engine):
    return lambda: SqlAlchemyUnitOfWork(session_factory=_factory(db_engine))


def _seed(db_engine, values: dict[str, str]) -> dict[str, int]:
    """最小数据图：User → Channel → Identity → Agent(type=acp) → active Connection。

    Connection.config 由真实 env 构造（endpoint / protocol / bearer_token /
    runtime_id / timeout_s），且必须是该 Channel 唯一 active Connection，
    从而能被 RoutingExecutor.find_active_by_channel() 命中。
    """
    with SqlAlchemyUnitOfWork(session_factory=_factory(db_engine)) as uow:
        alice = uow.users.create(User(name="alice"))
        channel = uow.channels.create(Channel(name="wecom-1", type="wecom"))
        agent_a = uow.agents.create(Agent(name="tencent-agent", type="acp"))
        agent_b = uow.agents.create(Agent(name="standby-agent", type="acp"))
        identity_a = uow.identities.create(Identity(
            user_id=alice.id, channel_id=channel.id, external_id="wx_a"))
        conn_a = uow.connections.create(Connection(
            channel_id=channel.id, agent_id=agent_a.id, name="tencent-path",
            config={
                "endpoint": values["TENCENT_ACP_URL"],
                "protocol": PROTOCOL_TENCENT_ACP_SSE,
                "bearer_token": values["TENCENT_ACP_TOKEN"],
                "runtime_id": values["TENCENT_RUNTIME_ID"],
                "timeout_s": float(os.environ.get("TENCENT_ACP_TIMEOUT_S", "60")),
            }))
        # inactive Connection：不干扰 find_active_by_channel 的唯一性判定
        uow.connections.create(Connection(
            channel_id=channel.id, agent_id=agent_b.id, name="standby-path",
            status="inactive"))
        # Implementation Package 2 / Step 2：显式授权该会话（default-deny 前提）
        uow.connection_conversations.create(ConnectionConversation(
            channel_id=channel.id, external_conversation_id="conversation-e2e",
            connection_id=conn_a.id))
        uow.commit()
        return {"channel": channel.id, "identity_a": identity_a.id,
                "agent_a": agent_a.id, "connection_a": conn_a.id}


def _coordinator(db_engine, agent_adapter, channel_adapter) -> RuntimeCoordinator:
    uow = _uow_factory(db_engine)
    return RuntimeCoordinator(
        identity_resolution=IdentityResolutionExecutor(uow),
        access_authorization=AccessAuthorizationExecutor(uow),
        routing=RoutingExecutor(uow),
        session_resolution=SessionResolutionExecutor(uow),
        session_creation=SessionCreationExecutor(uow),
        agent_dispatch=AgentDispatchExecutor(agent_adapter=agent_adapter,
                                             uow_factory=uow),
        delivery=DeliveryExecutor(channel_adapter=channel_adapter),
    )


def _inbound(channel_id: int, content: str = "请用一句话介绍你自己") -> InboundInteraction:
    return InboundInteraction(
        channel_id=channel_id, external_interaction_id="e2e-1",
        external_conversation_id="conversation-e2e",
        external_identity_id="wx_a", content=content)


# ================================================================== CREATE_NEW 真实 E2E


def test_runtime_e2e_create_new_with_real_tencent(db_engine) -> None:
    """RuntimeCoordinator 全链 → 真实 Tencent ACPAdapter → 真实 Runtime → Delivery。

    第一次请求（新 DB 无 active Session）必须走：
        SessionResolution → CREATE_NEW → SessionCreation → Dispatch → Delivery。
    """
    values = _live_values()
    if values is None:
        pytest.skip("未配置 TENCENT_ACP_URL/TOKEN/RUNTIME_ID（方式 B acpLink 三值）")

    g = _seed(db_engine, values)

    # 注入真实 ACPAdapter（runtime_checkable AgentAdapter Port）
    agent_adapter = ACPAdapter()
    assert isinstance(agent_adapter, AgentAdapter)
    channel_adapter = _TestChannelAdapter()

    # P1 / C1：投递是**显式**的第 7 段（execute() 只到 Dispatch）。
    inbound = _inbound(g["channel"])
    coordinator = _coordinator(db_engine, agent_adapter, channel_adapter)
    result = coordinator.execute(inbound)
    coordinator.deliver(build_outbound_response(inbound, result.agent_response))

    # 11. execute 成功（未抛错）
    # 12a. Delivery 恰好发生一次
    assert len(channel_adapter.deliveries) == 1
    delivery = channel_adapter.deliveries[0]
    # 12b/12c. delivery.content 非空且为真实 Agent 回复
    assert isinstance(delivery.content, str) and delivery.content.strip()
    # 出口 = 入口
    assert delivery.channel_id == g["channel"]
    assert delivery.external_conversation_id == "conversation-e2e"
    # 12d. Control Plane Session 被物化（CREATE_NEW 真实落库）
    with Session(db_engine) as session:
        rows = session.execute(select(func.count()).select_from(SessionModel)
                               ).scalar_one()
    assert rows == 1
