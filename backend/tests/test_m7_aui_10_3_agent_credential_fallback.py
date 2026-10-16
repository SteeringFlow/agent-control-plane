"""M7-AUI-10.3 —— Agent-level WorkBuddy Credential Ownership 验收门。

核心合同（M7-AUI-10.1 审计裁决的落地）：

    Connection credential 优先；缺失 / 不完整时经 **tenant-scoped** agent_loader
    fallback 到 Agent.config（provider/workbuddy_agent_id/api_key），在 verify/
    provisioning 的**内存路径**合成 effective config，交给既有
    WorkBuddyProvisioner；Agent credential 绝不写回 Connection.config——
    runtime cache 写回前剥离 credential 三键，ownership 仍属于 Agent。

边界（本文件钉住）：

- acp.py / WorkBuddyProvisioner / Runtime pipeline 零改动（Agent credential
  绝不进入 dispatch 面——既有 AST 红线继续生效）；
- agent_loader 与 channel_loader 同一 tenant-scoped 纪律；找不到 / 非
  workbuddy / credential 不完整 → fail closed（回退既有失败路径，绝不猜测）；
- Connection-owned credential 优先：完整时 Agent 即使持有另一个 api_key 也
  不参与（loader 根本不被调用）。

方法：spy 分派器单测（分派与合成语义）+ 真实 FastAPI / 真实
WorkBuddyProvisioner（本地确定性 transport，不触网）端到端行为测试。
"""

from __future__ import annotations

import itertools
import json
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.agents.acp import ACPAdapter
from app.adapters.agents.workbuddy import (
    WorkBuddyManagementClient,
    WorkBuddyProvisioner,
)
from app.adapters.channels.wecom import WeComAdapter
from app.adapters.connection_verify import build_connection_verifier
from app.api.management.connections import build_connections_api
from app.application.agents import AgentApplicationService, CreateAgent
from app.application.channels import ChannelApplicationService, CreateChannel
from app.application.connections import ConnectionApplicationService, CreateConnection
from app.domain.agent import Agent
from app.domain.agent_probe_snapshot import AgentProbeSnapshot
from app.domain.connection import Connection
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

BACKEND = Path(__file__).resolve().parents[1]

_CONN_KEY = "conn-owned-api-key"
_AGENT_KEY = "agent-owned-api-key"
_AGENT_KEY2 = "agent-owned-api-key-2"
_WB_AGENT_ID = "wb-agent-1"
_WB_AGENT_ID_2 = "wb-agent-2"  # M7-AUI-10.5：第二段场景的独立外部身份

_seq = itertools.count(1)


# ================================================================== harness

def _uow_factory(db_engine, tenant_id: int | None = None):
    factory = sessionmaker(bind=db_engine, class_=Session, expire_on_commit=False)
    if tenant_id is None:
        return lambda: SqlAlchemyUnitOfWork(session_factory=factory)
    return lambda: SqlAlchemyUnitOfWork(session_factory=factory, tenant_id=tenant_id)


def _agent_loader(db_engine, tenant_id: int | None = None, calls: list | None = None):
    uow_factory = _uow_factory(db_engine, tenant_id)

    def load(agent_id: int):
        if calls is not None:
            calls.append(agent_id)
        with uow_factory() as uow:
            return uow.agents.get_by_id(agent_id)

    return load


def _channel_loader(db_engine, tenant_id: int | None = None):
    uow_factory = _uow_factory(db_engine, tenant_id)

    def load(channel_id: int):
        with uow_factory() as uow:
            return uow.channels.get_by_id(channel_id)

    return load


class _SpyProbe:
    def __init__(self) -> None:
        self.calls: list[Connection] = []

    def __call__(self, connection: Connection) -> AgentProbeSnapshot:
        self.calls.append(connection)
        return AgentProbeSnapshot(True, "probe", "minimal")


class _SpyWorkBuddy:
    def __init__(self, updated: dict | None = None) -> None:
        self.updated = updated
        self.calls: list[Connection] = []

    def __call__(self, connection: Connection):
        self.calls.append(connection)
        return (AgentProbeSnapshot(True, None, "tencent-acp-sse"),
                self.updated if self.updated is None else dict(self.updated))


def _dispatcher(db_engine, *, probe=None, workbuddy=None, agent_loader=True,
                agent_calls=None, channel_type="local"):
    return build_connection_verifier(
        probe=probe or _SpyProbe(),
        workbuddy_verify=workbuddy or _SpyWorkBuddy(),
        wecom_verify=WeComAdapter.verify_connection,
        channel_loader=_channel_loader(db_engine),
        agent_loader=None if agent_loader is None
        else _agent_loader(db_engine, calls=agent_calls),
    )


def _seed_agent_and_connection(
    db_engine, *, connection_config: dict | None = None,
    agent_config: dict | None = None, tenant_id: int | None = None,
) -> tuple[Connection, Agent | None]:
    uow_factory = _uow_factory(db_engine, tenant_id)
    agent_service = AgentApplicationService(uow_factory=uow_factory)
    agent = agent_service.create_agent(CreateAgent(
        name=f"WorkBuddy-{next(_seq)}", type="acp", config=agent_config))
    channel = ChannelApplicationService(uow_factory=uow_factory).create_channel(
        CreateChannel(name=f"ch-local-{next(_seq)}", type="local"))
    connection = ConnectionApplicationService(uow_factory=uow_factory).create_connection(
        CreateConnection(channel_id=channel.id, agent_id=agent.id,
                         name="binding", config=connection_config))
    return connection, agent


_COMPLETE = {"provider": "workbuddy", "workbuddy_agent_id": _WB_AGENT_ID,
             "api_key": _AGENT_KEY}


# ================================================================== A. Connection credential 优先

def test_a1_complete_connection_credential_wins(db_engine) -> None:
    """A1：Connection.config 完整 → 使用 Connection credential。"""
    wb = _SpyWorkBuddy()
    d = _dispatcher(db_engine, workbuddy=wb)
    conn, _ = _seed_agent_and_connection(
        db_engine, connection_config={**_COMPLETE, "api_key": _CONN_KEY})
    d(conn)
    assert len(wb.calls) == 1
    assert wb.calls[0].config["api_key"] == _CONN_KEY


def test_a2_agent_loader_not_called_when_connection_complete(db_engine) -> None:
    """A2：即使 Agent.config 持有另一个 api_key，Connection credential 优先——
    loader 根本不被调用。"""
    agent_calls: list[int] = []
    wb = _SpyWorkBuddy()
    d = _dispatcher(db_engine, workbuddy=wb, agent_calls=agent_calls)
    conn, agent = _seed_agent_and_connection(
        db_engine, connection_config={**_COMPLETE, "api_key": _CONN_KEY},
        agent_config={**_COMPLETE, "api_key": _AGENT_KEY2})
    assert agent.config["api_key"] == _AGENT_KEY2  # 前置：Agent 确实有另一个 key
    d(conn)
    assert wb.calls[0].config["api_key"] == _CONN_KEY
    assert agent_calls == []


# ================================================================== B. Agent fallback

def test_b3_fallback_synthesizes_effective_config(db_engine) -> None:
    """B3/B4：Connection 无 WorkBuddy credential + Agent credential 完整 →
    effective config 携带正确的 Agent credential，且保留 Connection 既有键。"""
    wb = _SpyWorkBuddy()
    d = _dispatcher(db_engine, workbuddy=wb)
    conn, _ = _seed_agent_and_connection(
        db_engine, connection_config={"timeout_s": 5},
        agent_config=_COMPLETE)
    d(conn)
    assert len(wb.calls) == 1
    effective = wb.calls[0].config
    assert effective["provider"] == "workbuddy"
    assert effective["workbuddy_agent_id"] == _WB_AGENT_ID
    assert effective["api_key"] == _AGENT_KEY
    assert effective["timeout_s"] == 5  # Connection 既有键保留


def test_b4_agent_credential_never_written_back(db_engine) -> None:
    """B5：runtime cache 写回前剥离 credential 三键——Connection.config 只留
    per-connection runtime state。"""
    runtime_cache = {"protocol": "tencent-acp-sse",
                     "endpoint": "https://runtime.example/acp",
                     "bearer_token": "acp-link-token", "runtime_id": "rt-1"}
    wb = _SpyWorkBuddy(updated=runtime_cache)
    d = _dispatcher(db_engine, workbuddy=wb)
    conn, _ = _seed_agent_and_connection(db_engine, agent_config=_COMPLETE)
    snapshot, updated = d(conn)
    assert snapshot.reachable is True
    assert updated is not None
    assert updated == runtime_cache  # credential 三键已被剥离
    for key in ("provider", "workbuddy_agent_id", "api_key"):
        assert key not in updated


def test_b5_fallback_reuses_existing_runtime_cache(db_engine) -> None:
    """既有 runtime cache 语义不变：第二次 verify 命中缓存（Provisioner 只探针、
    不重复创建）——fallback 路径同样成立。"""
    conns: list[Connection] = []

    def workbuddy_verify(connection):
        conns.append(connection)
        from app.adapters.agents.workbuddy import WorkBuddyProvisioner as P
        # 直接复用真实 Provisioner 语义：这里用桩模拟「首次创建 / 二次复用」
        if len(conns) == 1:
            return (AgentProbeSnapshot(True, None, "tencent-acp-sse"),
                    {**connection.config, "protocol": "tencent-acp-sse",
                     "endpoint": "https://runtime.example/acp",
                     "bearer_token": "acp-link-token", "runtime_id": "rt-1"})
        return AgentProbeSnapshot(True, None, "tencent-acp-sse"), None

    d = _dispatcher(db_engine, workbuddy=workbuddy_verify)
    conn, _ = _seed_agent_and_connection(db_engine, agent_config=_COMPLETE)
    _, first = d(conn)
    assert first is not None and "runtime_id" in first
    # 模拟写回后的 Connection（runtime cache only，无 credential）
    conn2 = replace(conn, config=first)
    _, second = d(conn2)
    assert second is None  # 缓存命中 → 只探针，不重复写回
    assert len(conns) == 2


# ================================================================== C. Tenant isolation / fail closed

def test_c1_missing_agent_fails_closed(db_engine) -> None:
    """C7：Agent 不存在 → fail closed（回退既有探针路径，绝不猜测）。"""
    probe = _SpyProbe()
    wb = _SpyWorkBuddy()
    d = _dispatcher(db_engine, probe=probe, workbuddy=wb)
    conn = Connection(channel_id=1, agent_id=999, name="c", id=1, config={})
    d(conn)
    assert wb.calls == [] and len(probe.calls) == 1


def test_c2_non_workbuddy_agent_is_not_a_credential(db_engine) -> None:
    """C8：Agent.config provider != workbuddy → 不作为 WorkBuddy credential。"""
    probe = _SpyProbe()
    wb = _SpyWorkBuddy()
    d = _dispatcher(db_engine, probe=probe, workbuddy=wb)
    conn, _ = _seed_agent_and_connection(
        db_engine, agent_config={"provider": "local",
                                 "workbuddy_agent_id": _WB_AGENT_ID,
                                 "api_key": _AGENT_KEY})
    d(conn)
    assert wb.calls == [] and len(probe.calls) == 1


def test_c3_incomplete_agent_credential_fails_closed(db_engine) -> None:
    """C9：Agent credential 缺 api_key / 缺 workbuddy_agent_id → fail closed。"""
    probe = _SpyProbe()
    wb = _SpyWorkBuddy()
    d = _dispatcher(db_engine, probe=probe, workbuddy=wb)
    for agent_config in (
        {"provider": "workbuddy", "workbuddy_agent_id": _WB_AGENT_ID},
        {"provider": "workbuddy", "api_key": _AGENT_KEY},
        {"provider": "workbuddy", "workbuddy_agent_id": "  ", "api_key": _AGENT_KEY},
    ):
        conn, _ = _seed_agent_and_connection(db_engine, agent_config=agent_config)
        d(conn)
    assert wb.calls == [] and len(probe.calls) == 3


def test_c4_cross_tenant_agent_is_invisible(db_engine) -> None:
    """C6：loader 绑定 tenant 8，Agent 实际属于 tenant 7 → 不可见 → fail closed；
    loader 只被以 connection.agent_id 请求。"""
    from sqlalchemy import text

    # 建租户 7 / 8（满足 FK），Agent/Connection 属于 tenant 7
    factory = sessionmaker(bind=db_engine, class_=Session, expire_on_commit=False)
    with factory() as session:
        for tid in (7, 8):
            session.execute(text(
                "INSERT INTO tenants (id, name, status, config, created_at, updated_at) "
                f"VALUES ({tid}, 'tenant-{tid}', 'active', NULL, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
        session.commit()

    probe = _SpyProbe()
    wb = _SpyWorkBuddy()
    agent_calls: list[int] = []
    d = build_connection_verifier(
        probe=probe,
        workbuddy_verify=wb,
        wecom_verify=WeComAdapter.verify_connection,
        channel_loader=_channel_loader(db_engine, tenant_id=8),
        agent_loader=_agent_loader(db_engine, tenant_id=8, calls=agent_calls),
    )
    conn, agent = _seed_agent_and_connection(
        db_engine, agent_config=_COMPLETE, tenant_id=7)
    d(conn)
    assert wb.calls == [] and len(probe.calls) == 1
    assert agent_calls == [agent.id]  # 只按 connection.agent_id 请求，且查不到


def test_c5_loader_exception_fails_closed(db_engine) -> None:
    """loader 抛异常 → fail closed（不炸 Verify，回退探针）。"""
    probe = _SpyProbe()
    wb = _SpyWorkBuddy()

    def bad_loader(agent_id: int):
        raise RuntimeError("db down")

    d = build_connection_verifier(
        probe=probe, workbuddy_verify=wb,
        wecom_verify=WeComAdapter.verify_connection,
        channel_loader=_channel_loader(db_engine),
        agent_loader=bad_loader)
    conn, _ = _seed_agent_and_connection(db_engine, agent_config=_COMPLETE)
    d(conn)
    assert wb.calls == [] and len(probe.calls) == 1


def test_c6_incomplete_connection_credential_uses_agent_fallback(db_engine) -> None:
    """Connection 有 provider 但缺 api_key：Agent 可补全 → fallback 生效；
    Agent 无法补全 → 既有 WorkBuddy fail-closed 路径（不回退探针）。

    M7-AUI-10.5：两段使用**不同**的 WorkBuddy Agent ID——同租户内同一外部身份
    只能由一个 Agent 代表（第二段沿用 _WB_AGENT_ID 会构成身份重复，被 10.5
    守卫拒绝）；本测试验证的是凭据完整性路径，与外部 ID 取值无关。"""
    wb = _SpyWorkBuddy()
    d = _dispatcher(db_engine, workbuddy=wb)
    conn, _ = _seed_agent_and_connection(
        db_engine,
        connection_config={"provider": "workbuddy",
                           "workbuddy_agent_id": _WB_AGENT_ID},
        agent_config=_COMPLETE)
    d(conn)
    assert len(wb.calls) == 1
    assert wb.calls[0].config["api_key"] == _AGENT_KEY  # Agent 补全缺口

    # Agent 侧也无凭据 → 既有失败路径（workbuddy_verify 仍被调用，不回退探针）
    probe = _SpyProbe()
    wb2 = _SpyWorkBuddy()
    d2 = _dispatcher(db_engine, probe=probe, workbuddy=wb2)
    conn2, _ = _seed_agent_and_connection(
        db_engine,
        connection_config={"provider": "workbuddy",
                           "workbuddy_agent_id": _WB_AGENT_ID_2},
        agent_config=None)
    d2(conn2)
    assert len(wb2.calls) == 1 and probe.calls == []


# ================================================================== D. 安全

def test_d1_verify_source_never_logs_credentials() -> None:
    """D10（静态）：connection_verify.py 无日志面（凭据只走内存路径）。"""
    src = (BACKEND / "app" / "adapters" / "connection_verify.py").read_text(
        encoding="utf-8")
    for banned in ("logging", "LOGGER", "print(", "logger"):
        assert banned not in src, banned


def test_d2_acp_red_line_unchanged() -> None:
    """D13：acp.py 仍只读 Connection.config，绝不引用 agent.credential_ref。"""
    src = (BACKEND / "app" / "adapters" / "agents" / "acp.py").read_text(
        encoding="utf-8")
    assert "connection.config" in src
    assert "agent.credential_ref" not in src


# ================================================================== E. 端到端（真实 Provisioner + 确定性 transport）

class _FakeWbTransport:
    """WorkBuddy Management API 确定性桩（不触网；计数防重复创建）。"""

    _RESPONSE = json.dumps({
        "data": {"id": "rt-1", "links": {"acpLink": {
            "url": "https://runtime.example/acp", "token": "acp-link-token"}}},
    }).encode("utf-8")

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, method, url, headers, body, timeout_s):
        self.calls += 1
        return 200, self._RESPONSE


def _wb_probe(_connection) -> AgentProbeSnapshot:
    return AgentProbeSnapshot(True, None, "tencent-acp-sse")


def _e2e_client(db_engine, transport: _FakeWbTransport) -> TestClient:
    uow_factory = _uow_factory(db_engine)

    def _load_channel(channel_id: int):
        with uow_factory() as uow:
            return uow.channels.get_by_id(channel_id)

    def _load_agent(agent_id: int):
        with uow_factory() as uow:
            return uow.agents.get_by_id(agent_id)

    dispatcher = build_connection_verifier(
        probe=ACPAdapter().probe,
        workbuddy_verify=WorkBuddyProvisioner(
            probe=_wb_probe,
            client=WorkBuddyManagementClient(transport=transport)).verify,
        wecom_verify=WeComAdapter.verify_connection,
        channel_loader=_load_channel,
        agent_loader=_load_agent,
    )
    app = FastAPI()
    app.include_router(build_connections_api(
        ConnectionApplicationService(uow_factory=uow_factory),
        verify_provider=lambda: dispatcher))
    return TestClient(app)


def test_e1_end_to_end_fallback_provisions_and_caches_without_credential(
        db_engine) -> None:
    """E14/B3/B5 端到端：Agent-owned credential → lazy provisioning 成功；
    Connection.config 只落 runtime cache；Agent.config 保持 ownership；
    响应与 Interaction 均无 api_key。"""
    transport = _FakeWbTransport()
    uow_factory = _uow_factory(db_engine)
    agent_service = AgentApplicationService(uow_factory=uow_factory)
    conn_service = ConnectionApplicationService(uow_factory=uow_factory)

    agent = agent_service.create_agent(CreateAgent(
        name="WorkBuddy", type="acp", config=_COMPLETE))
    channel = ChannelApplicationService(uow_factory=uow_factory).create_channel(
        CreateChannel(name="ch-bot", type="wecom_bot"))
    connection = conn_service.create_connection(CreateConnection(
        channel_id=channel.id, agent_id=agent.id, name="binding"))

    client = _e2e_client(db_engine, transport)
    resp = client.post(f"/api/connections/{connection.id}/verify")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reachable"] is True and body["probe"] == "tencent-acp-sse"
    assert _AGENT_KEY not in resp.text  # D11：凭据绝不进入错误/正常响应

    # Connection.config：只有 runtime cache，credential 三键不落库
    stored = conn_service.get_connection(connection.id)
    assert stored.config["protocol"] == "tencent-acp-sse"
    assert stored.config["runtime_id"] == "rt-1"
    for key in ("provider", "workbuddy_agent_id", "api_key"):
        assert key not in stored.config

    # Agent.config：ownership 不变（真实 key 仍在 Agent 上）
    assert agent_service.get_agent(agent.id).config["api_key"] == _AGENT_KEY

    # D12：provisioning 路径不产生 Interaction
    from sqlalchemy import create_engine  # noqa: F401
    import sqlite3
    # 复用同一内存库需要经由 SQLAlchemy 引擎——直接用 uow 会话查询
    factory = sessionmaker(bind=db_engine, class_=Session, expire_on_commit=False)
    with factory() as session:
        count = session.execute(
            __import__("sqlalchemy").text("SELECT COUNT(*) FROM interactions")
        ).scalar()
    assert count == 0

    # 二次 verify：runtime cache 命中（fallback → Provisioner 复用），不重复创建
    resp2 = client.post(f"/api/connections/{connection.id}/verify")
    assert resp2.status_code == 200 and resp2.json()["reachable"] is True
    assert transport.calls == 1


def test_e2_wecom_and_probe_paths_unchanged(db_engine) -> None:
    """E15/E16：WeCom 分派与 generic probe 回退行为不变（fallback 不越位）。"""
    probe = _SpyProbe()
    wb = _SpyWorkBuddy()
    d = _dispatcher(db_engine, probe=probe, workbuddy=wb)
    # wecom 渠道 → wecom 分派（渠道侧集成身份优先于 Agent fallback）
    uow_factory = _uow_factory(db_engine)
    agent_service = AgentApplicationService(uow_factory=uow_factory)
    agent = agent_service.create_agent(CreateAgent(
        name="WorkBuddy", type="acp", config=_COMPLETE))
    channel = ChannelApplicationService(uow_factory=uow_factory).create_channel(
        CreateChannel(name="ch-wecom", type="wecom"))
    conn_service = ConnectionApplicationService(uow_factory=uow_factory)
    conn = conn_service.create_connection(CreateConnection(
        channel_id=channel.id, agent_id=agent.id, name="binding"))
    d(conn)
    assert wb.calls == [] and probe.calls == []  # wecom_verify 分支命中（无写入）

    # 无 credential 的普通 acp agent → generic probe（行为不变）
    conn2, _ = _seed_agent_and_connection(db_engine, agent_config=None)
    d(conn2)
    assert wb.calls == []
