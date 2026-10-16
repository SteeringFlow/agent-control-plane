"""M7-AUI-10.5.2 —— WorkBuddy Credential Discovery for Multi-Channel Connections。

根因（10.5.2 诊断坐实）：WorkBuddy 凭据只住在 Agent 的**第一条** Connection.config
（Home Flow 从不写 Agent.config）；「把它们连接起来」建立的第二条连接 config=null
→ `connection_verify.py` 发现不到 WorkBuddy 凭据 → 掉进 generic ACP 探针 →
fail-closed「verify 未通过」。

修复：凭据发现优先级（全部仅内存、绝不写回）：
  1. 本 Connection.config（既有，最高优先）；
  2. 同 tenant `Agent.config`（M7-AUI-10.3，不动）；
  3. 同 tenant **同一 Agent** 的兄弟 Connection.config（本任务新增）——
     只取 credential 三键，绝不复用兄弟的 runtime cache；
     候选按连接 id 升序确定性选择；来源 workbuddy_agent_id 不一致 → Fail Closed。

方法：内存 SQLite + Spy 分派器（真跑 `build_connection_verifier`），沿用
M7-AUI-10.3 的 harness 约定；租户隔离用真实 tenant-scoped UoW 验证。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.adapters.connection_verify import build_connection_verifier
from app.adapters.agents.workbuddy import API_KEY_KEY
from app.application.agents import AgentApplicationService, CreateAgent
from app.application.channels import ChannelApplicationService, CreateChannel
from app.application.connections import (
    ConnectionApplicationService,
    CreateConnection,
)
from app.domain.agent_probe_snapshot import AgentProbeSnapshot
from app.domain.connection import Connection
from app.persistence import models  # noqa: F401
from app.persistence.database import Base
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork
from app.adapters.channels.wecom import WeComAdapter

_CONN_KEY = "conn-owned-key"
_AGENT_KEY = "agent-owned-key"
_SIB_KEY = "sibling-owned-key"
_WB_AGENT_ID = "wb-agent-main"
_SIB_AGENT_ID = "wb-agent-other"


def _wb_config(agent_id: str = _WB_AGENT_ID, api_key: str = _CONN_KEY) -> dict:
    return {"provider": "workbuddy", "workbuddy_agent_id": agent_id, "api_key": api_key}


# ================================================================ harness

@pytest.fixture()
def db_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def _factory(db_engine, tenant_id: int | None = None) -> sessionmaker:
    return sessionmaker(bind=db_engine, class_=Session, expire_on_commit=False)


def _uow_factory(db_engine, tenant_id: int | None = None):
    factory = _factory(db_engine, tenant_id)
    if tenant_id is None:
        return lambda: SqlAlchemyUnitOfWork(session_factory=factory)
    return lambda: SqlAlchemyUnitOfWork(session_factory=factory, tenant_id=tenant_id)


class _SpyProbe:
    def __init__(self, reachable: bool = False) -> None:
        self.reachable = reachable
        self.calls: list[Connection] = []

    def __call__(self, connection: Connection) -> AgentProbeSnapshot:
        self.calls.append(connection)
        return AgentProbeSnapshot(self.reachable, "probe", "minimal")


class _SpyWorkBuddy:
    def __init__(self, updated: dict | None = None) -> None:
        self.updated = updated
        self.calls: list[Connection] = []

    def __call__(self, connection: Connection):
        self.calls.append(connection)
        return (AgentProbeSnapshot(True, None, "tencent-acp-sse"),
                self.updated if self.updated is None else dict(self.updated))


def _seed_agent(db_engine, *, agent_config: dict | None = None,
                tenant_id: int | None = None) -> int:
    _seed_agent.n += 1  # type: ignore[attr-defined]
    svc = AgentApplicationService(uow_factory=_uow_factory(db_engine, tenant_id))
    return svc.create_agent(CreateAgent(
        name=f"WB-{_seed_agent.n}", type="acp", config=agent_config)).id  # type: ignore[attr-defined]


def _add_channel(db_engine, tenant_id: int | None = None) -> int:
    _add_channel.n += 1  # type: ignore[attr-defined]
    svc = ChannelApplicationService(uow_factory=_uow_factory(db_engine, tenant_id))
    return svc.create_channel(CreateChannel(
        name=f"ch-{_add_channel.n}", type="local")).id  # type: ignore[attr-defined]


def _add_connection(db_engine, agent_id: int, config: dict | None = None,
                    tenant_id: int | None = None) -> Connection:
    _add_connection.n += 1  # type: ignore[attr-defined]
    svc = ConnectionApplicationService(uow_factory=_uow_factory(db_engine, tenant_id))
    return svc.create_connection(CreateConnection(
        channel_id=_add_channel(db_engine, tenant_id), agent_id=agent_id,
        name=f"flow-{_add_connection.n}", config=config))  # type: ignore[attr-defined]


def _counters():
    return {"agent": 0, "sibling": 0}


def _seed_tenant(db_engine, tenant_id: int) -> None:
    from app.persistence.models.tenant import TenantModel
    with Session(db_engine) as session:
        session.add(TenantModel(id=tenant_id, name=f"tenant-{tenant_id}"))
        session.commit()


def _agent_loader(db_engine, calls: dict | None, tenant_id: int | None = None):
    def load(agent_id: int):
        if calls is not None:
            calls["agent"] += 1
        with _uow_factory(db_engine, tenant_id)() as uow:
            return uow.agents.get_by_id(agent_id)
    return load


def _sibling_loader(db_engine, calls: dict | None, tenant_id: int | None = None):
    def load(agent_id: int):
        if calls is not None:
            calls["sibling"] += 1
        with _uow_factory(db_engine, tenant_id)() as uow:
            return uow.connections.list_connections()
    return load


def _dispatcher(db_engine, *, probe=None, workbuddy=None, agent_calls=None,
                sibling_calls=None, tenant_id: int | None = None,
                with_agent_loader: bool = True, with_sibling_loader: bool = True):
    def load_channel(channel_id: int):
        with _uow_factory(db_engine, tenant_id)() as uow:
            return uow.channels.get_by_id(channel_id)

    return build_connection_verifier(
        probe=probe or _SpyProbe(),
        workbuddy_verify=workbuddy or _SpyWorkBuddy(),
        wecom_verify=WeComAdapter.verify_connection,
        channel_loader=load_channel,
        agent_loader=_agent_loader(db_engine, agent_calls, tenant_id)
        if with_agent_loader else None,
        sibling_loader=_sibling_loader(db_engine, sibling_calls, tenant_id)
        if with_sibling_loader else None,
    )


# 给 name 生成器挂计数器
_seed_agent.n = 1  # type: ignore[attr-defined]
_add_channel.n = 1  # type: ignore[attr-defined]
_add_connection.n = 1  # type: ignore[attr-defined]


# ================================================================ 优先级 1：本连接

def test_d1_own_complete_short_circuits_all_fallbacks(db_engine) -> None:
    """§5-1/11：本连接凭据完整 → 直接使用；Agent/兄弟 loader 均不被征询。"""
    agent_calls, sibling_calls = _counters(), _counters()
    probe, wb = _SpyProbe(), _SpyWorkBuddy()
    agent_id = _seed_agent(db_engine)
    target = _add_connection(db_engine, agent_id, config=_wb_config())
    # 一个「诱饵」兄弟连接（不同身份）——若被征询就会误选
    _add_connection(db_engine, agent_id, config=_wb_config(_SIB_AGENT_ID, _SIB_KEY))
    d = _dispatcher(db_engine, probe=probe, workbuddy=wb,
                    agent_calls=agent_calls, sibling_calls=sibling_calls)
    snap, updated = d(target)
    assert snap.reachable is True
    assert wb.calls[0].config[API_KEY_KEY] == _CONN_KEY
    assert agent_calls == {"agent": 0, "sibling": 0}
    assert sibling_calls == {"agent": 0, "sibling": 0}


# ================================================================ 优先级 2：Agent.config

def test_d2_agent_fallback_precedes_sibling(db_engine) -> None:
    """§5-2/12：本连接不完整 + Agent.config 完整 → 用 Agent 凭据；兄弟不被征询。"""
    agent_calls, sibling_calls = _counters(), _counters()
    wb = _SpyWorkBuddy()
    agent_id = _seed_agent(db_engine, agent_config=_wb_config(api_key=_AGENT_KEY))
    target = _add_connection(db_engine, agent_id, config={
        "provider": "workbuddy", "workbuddy_agent_id": _WB_AGENT_ID})  # 缺 api_key
    _add_connection(db_engine, agent_id, config=_wb_config(_SIB_AGENT_ID, _SIB_KEY))
    d = _dispatcher(db_engine, workbuddy=wb,
                    agent_calls=agent_calls, sibling_calls=sibling_calls)
    snap, updated = d(target)
    assert snap.reachable is True
    assert wb.calls[0].config[API_KEY_KEY] == _AGENT_KEY
    assert sibling_calls == {"agent": 0, "sibling": 0}, "Agent.config 已补全 → 兄弟不被征询"


# ================================================================ 优先级 3：兄弟连接

def test_d3_sibling_discovery_synthesizes_credential(db_engine) -> None:
    """§5-3/6：本连接与 Agent.config 均无凭据 → 兄弟连接补全；
    effective config 只含 credential 三键，**绝不**含兄弟的 runtime cache。"""
    wb = _SpyWorkBuddy()
    agent_id = _seed_agent(db_engine)
    # 兄弟连接：完整凭据 + 连接专属 runtime cache（绝不外借）
    _add_connection(db_engine, agent_id, config={
        **_wb_config(api_key=_SIB_KEY),
        "runtime_id": "rt-sibling", "endpoint": "https://sibling.example/acp",
        "bearer_token": "sibling-bearer"})
    target = _add_connection(db_engine, agent_id, config=None)  # §诊断场景：config=null
    d = _dispatcher(db_engine, workbuddy=wb)
    snap, updated = d(target)
    assert snap.reachable is True
    effective = wb.calls[0].config
    assert effective[API_KEY_KEY] == _SIB_KEY
    assert effective["workbuddy_agent_id"] == _WB_AGENT_ID
    assert effective["provider"] == "workbuddy"
    for banned in ("runtime_id", "endpoint", "bearer_token"):
        assert banned not in effective, f"兄弟连接的 {banned} 绝不外借"
    # 目标连接走自己的 provisioning 路径（传入的是目标连接本体）
    assert wb.calls[0].id == target.id and wb.calls[0].agent_id == agent_id
    # runtime cache 写回归目标连接，credential 键被剥离（§5-7/8）——
    # provisioner 的写回形如 {**input_config, **runtime_cache}，故模拟含三键的写回
    wb2 = _SpyWorkBuddy(updated={"runtime_id": "rt-target-new",
                                 "endpoint": "https://t.example/acp",
                                 "provider": "workbuddy",
                                 "workbuddy_agent_id": _WB_AGENT_ID,
                                 API_KEY_KEY: _SIB_KEY})
    d2 = _dispatcher(db_engine, workbuddy=wb2)
    _, out = d2(target)
    assert out == {"runtime_id": "rt-target-new", "endpoint": "https://t.example/acp"}
    for banned in ("api_key", "provider", "workbuddy_agent_id"):
        assert banned not in (out or {}), "兄弟凭据绝不随写回持久化到目标连接"


def test_d4_different_agent_sibling_never_borrowed(db_engine) -> None:
    """§5-4：兄弟连接属于**其它 Agent** → 绝不借用 → 既有回退（generic 探针）。"""
    probe, wb = _SpyProbe(), _SpyWorkBuddy()
    other_id = _seed_agent(db_engine)
    _add_connection(db_engine, other_id, config=_wb_config(api_key=_SIB_KEY))
    target_agent = _seed_agent(db_engine)
    target = _add_connection(db_engine, target_agent, config=None)
    d = _dispatcher(db_engine, probe=probe, workbuddy=wb)
    snap, updated = d(target)
    assert wb.calls == [], "绝不借用其它 Agent 的凭据"
    assert len(probe.calls) == 1 and updated is None
    assert snap.reachable is False


def test_d5_cross_tenant_sibling_invisible(db_engine) -> None:
    """§5-5：兄弟 loader 按租户作用域——他租户的同形凭据结构性不可见。"""
    _seed_tenant(db_engine, 7)
    _seed_tenant(db_engine, 8)
    # tenant 8：目标 Agent + 一条无凭据连接 + 一条**同租户**完整凭据兄弟
    agent8 = _seed_agent(db_engine, tenant_id=8)
    _add_connection(db_engine, agent8, config=_wb_config(api_key=_SIB_KEY),
                    tenant_id=8)
    target = _add_connection(db_engine, agent8, config=None, tenant_id=8)
    # tenant 7：一枚「诱饵」完整凭据连接（loader 若跨租户就会被误用）
    decoy_agent = _seed_agent(db_engine, tenant_id=7)
    _add_connection(db_engine, decoy_agent, config=_wb_config(api_key=_CONN_KEY),
                    tenant_id=7)
    wb = _SpyWorkBuddy()
    probe = _SpyProbe()
    d = _dispatcher(db_engine, probe=probe, workbuddy=wb, tenant_id=8)
    snap, updated = d(target)
    # tenant-8 loader 看不到 tenant-7 诱饵 → 同租户兄弟正常被发现
    assert wb.calls[0].config[API_KEY_KEY] == _SIB_KEY
    # 反向：tenant-7 作用域的 loader 对 tenant-8 目标 → 无候选 → 探针
    d7 = _dispatcher(db_engine, probe=_SpyProbe(), workbuddy=_SpyWorkBuddy(),
                     tenant_id=7)
    snap7, _ = d7(target)
    assert snap7.reachable is False


# ================================================================ Fail Closed

def test_d9_incomplete_sibling_no_false_success(db_engine) -> None:
    """§5-9：不完整的兄弟凭据不构成候选 → 无假成功（走既有探针路径）。"""
    probe, wb = _SpyProbe(reachable=False), _SpyWorkBuddy()
    agent_id = _seed_agent(db_engine)
    _add_connection(db_engine, agent_id, config={
        "provider": "workbuddy", "workbuddy_agent_id": _WB_AGENT_ID})  # 缺 api_key
    target = _add_connection(db_engine, agent_id, config=None)
    d = _dispatcher(db_engine, probe=probe, workbuddy=wb)
    snap, updated = d(target)
    assert wb.calls == [], "不完整兄弟凭据绝不被借用"
    assert len(probe.calls) == 1 and snap.reachable is False and updated is None


def test_d10_conflicting_siblings_fail_closed(db_engine) -> None:
    """§5-10：兄弟来源 workbuddy_agent_id 不一致 → Fail Closed（安全诊断）。"""
    probe, wb = _SpyProbe(), _SpyWorkBuddy()
    agent_id = _seed_agent(db_engine)
    _add_connection(db_engine, agent_id, config=_wb_config(_WB_AGENT_ID, _SIB_KEY))
    _add_connection(db_engine, agent_id, config=_wb_config(_SIB_AGENT_ID, _SIB_KEY))
    target = _add_connection(db_engine, agent_id, config=None)
    d = _dispatcher(db_engine, probe=probe, workbuddy=wb)
    snap, updated = d(target)
    assert snap.reachable is False
    assert "不一致" in (snap.detail or "")
    assert _WB_AGENT_ID in snap.detail and _SIB_AGENT_ID in snap.detail
    assert _SIB_KEY not in (snap.detail or ""), "诊断绝不携带 API Key"
    assert wb.calls == [] and probe.calls == [] and updated is None


def test_d10b_agent_hint_conflict_fails_closed(db_engine) -> None:
    """来源一致性包含 Agent.config 提示：Agent（不完整）指向 A、兄弟指向 B → 拒绝。"""
    probe, wb = _SpyProbe(), _SpyWorkBuddy()
    agent_id = _seed_agent(db_engine, agent_config={
        "provider": "workbuddy", "workbuddy_agent_id": _WB_AGENT_ID})  # 不完整
    _add_connection(db_engine, agent_id, config=_wb_config(_SIB_AGENT_ID, _SIB_KEY))
    target = _add_connection(db_engine, agent_id, config=None)
    d = _dispatcher(db_engine, probe=probe, workbuddy=wb)
    snap, updated = d(target)
    assert snap.reachable is False and "不一致" in (snap.detail or "")
    assert wb.calls == [] and probe.calls == []


def test_d11_own_identity_conflict_with_sibling_still_uses_own(db_engine) -> None:
    """本连接完整凭据优先：即使兄弟携带不同身份也不征询、不触发一致性拒绝。"""
    wb = _SpyWorkBuddy()
    agent_calls, sibling_calls = _counters(), _counters()
    agent_id = _seed_agent(db_engine)
    target = _add_connection(db_engine, agent_id, config=_wb_config())
    _add_connection(db_engine, agent_id, config=_wb_config(_SIB_AGENT_ID, _SIB_KEY))
    d = _dispatcher(db_engine, workbuddy=wb,
                    agent_calls=agent_calls, sibling_calls=sibling_calls)
    snap, _ = d(target)
    assert snap.reachable is True
    assert wb.calls[0].config[API_KEY_KEY] == _CONN_KEY
    assert sibling_calls == {"agent": 0, "sibling": 0}


# ================================================================ 行为不变

def test_d13_generic_and_local_behavior_unchanged(db_engine) -> None:
    """§5-13：generic ACP（无 provider、无任何 WorkBuddy 来源）→ 既有只读探针。"""
    probe, wb = _SpyProbe(), _SpyWorkBuddy()
    agent_calls, sibling_calls = _counters(), _counters()
    agent_id = _seed_agent(db_engine)
    target = _add_connection(db_engine, agent_id, config={"protocol": "tencent-acp-sse"})
    d = _dispatcher(db_engine, probe=probe, workbuddy=wb,
                    agent_calls=agent_calls, sibling_calls=sibling_calls)
    snap, updated = d(target)
    assert wb.calls == [] and updated is None
    assert len(probe.calls) == 1
    # loader 被征询但一无所获 → 既有行为（F3-8）


def test_d13b_discovery_disabled_keeps_103_behavior(db_engine) -> None:
    """sibling_loader 未注入（None）→ M7-AUI-10.3 行为逐字节不变。"""
    probe, wb = _SpyProbe(), _SpyWorkBuddy()
    agent_id = _seed_agent(db_engine)
    _add_connection(db_engine, agent_id, config=_wb_config(api_key=_SIB_KEY))
    target = _add_connection(db_engine, agent_id, config=None)
    d = _dispatcher(db_engine, probe=probe, workbuddy=wb, with_sibling_loader=False)
    snap, updated = d(target)
    assert wb.calls == [] and len(probe.calls) == 1 and updated is None
