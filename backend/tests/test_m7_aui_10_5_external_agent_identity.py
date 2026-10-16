"""M7-AUI-10.5 —— WorkBuddy External Agent Identity Uniqueness（backend）。

审计基线：`M7-AUI-10.5 External Agent Identity Uniqueness Audit Report`（C3=YES、
C4=NO、C5=NOT YET → 应用级守卫 + 409，零 migration / 零 DB UNIQUE）。

身份契约：`(tenant_id, provider=workbuddy, workbuddy_agent_id)`，作用域 = 一个
Tenant；身份事实源 = `Agent.config` ∪ `Connection.config`（审计 §6：实测 100%
住在 Connection.config，只查 Agent.config 会漏）；**API Key 是凭据，绝不参与
身份判断**；`local` / generic ACP（无 WorkBuddy 身份）行为不变。

强制面（本任务边界内的三条正常创建/更新路径）：
- `POST /api/agents`（create_agent）：config 携带身份且同租户已有其它 Agent 代表
  → DuplicateExternalAgentError → 409；
- `PATCH /api/agents/{id}`（update_agent）：合并后 config 引入/改写身份 → 与**其它**
  Agent 查重（排除自身）→ 409；
- `POST /api/connections`（create_connection）：Home Flow 的身份真实落库点
  （POST /agents 只带 name/type）——身份归属**其它** Agent → 409；排除自身绑定
  → 同一 Agent 多 Channel 复用（既有 Connection 唯一性规则）不受影响。

方法：内存 SQLite + 真实 UoW（租户作用域显式注入）+ TestClient HTTP 探针。
"""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.api.management.agents import build_agents_api
from app.api.management.channels import build_channels_api
from app.api.management.connections import build_connections_api
from app.application.agents import AgentApplicationService, CreateAgent, UpdateAgent
from app.application.channels import ChannelApplicationService
from app.application.connections import (
    ConnectionApplicationService,
    CreateConnection,
    UpdateConnection,
)
from app.application.errors import DuplicateAgentError, DuplicateExternalAgentError
from app.domain.connection import Connection
from app.persistence.models.agent import AgentModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

BACKEND = Path(__file__).resolve().parents[1]
API_AGENTS = BACKEND / "app" / "api" / "management" / "agents.py"
API_CONNECTIONS = BACKEND / "app" / "api" / "management" / "connections.py"

WB_ID = "2098593639567253504"


def _wb_config(agent_id: str = WB_ID, api_key: str = "pt_test_key") -> dict:
    return {"provider": "workbuddy", "workbuddy_agent_id": agent_id, "api_key": api_key}


def _factory(db_engine) -> sessionmaker:
    return sessionmaker(bind=db_engine, class_=Session, expire_on_commit=False)


def _service(db_engine, tenant_id: int | None = None) -> AgentApplicationService:
    return AgentApplicationService(uow_factory=lambda: SqlAlchemyUnitOfWork(
        session_factory=_factory(db_engine), tenant_id=tenant_id))


def _conn_service(db_engine, tenant_id: int | None = None
                  ) -> ConnectionApplicationService:
    return ConnectionApplicationService(uow_factory=lambda: SqlAlchemyUnitOfWork(
        session_factory=_factory(db_engine), tenant_id=tenant_id))


@pytest.fixture()
def client(db_engine):
    """真实 HTTP 探针：agents + channels + connections 管理 API（内存库 Service）。"""
    app = FastAPI()
    app.include_router(build_agents_api(_service(db_engine)))
    app.include_router(build_channels_api(
        ChannelApplicationService(uow_factory=lambda: SqlAlchemyUnitOfWork(
            session_factory=_factory(db_engine)))))
    app.include_router(build_connections_api(_conn_service(db_engine)))
    return TestClient(app)


# ------------------------------------------------------------ 种子工具

def _seed_tenant(db_engine, tenant_id: int) -> None:
    """跨租户用例的前置：tenants 行（FK 依赖）。"""
    from app.persistence.models.tenant import TenantModel
    with Session(db_engine) as session:
        session.add(TenantModel(id=tenant_id, name=f"tenant-{tenant_id}"))
        session.commit()


def _seed_agent(db_engine, name: str, config: dict | None = None,
                tenant_id: int | None = None) -> int:
    """绕过查重直接落一条 Agent（种子数据；经仓储写、显式 tenant 标记）。"""
    with SqlAlchemyUnitOfWork(
        session_factory=_factory(db_engine), tenant_id=tenant_id
    ) as uow:
        from app.domain.agent import Agent
        row = uow.agents.create(Agent(name=name, type="acp", config=config))
        uow.commit()
        return row.id


def _seed_connection(db_engine, channel_id: int, agent_id: int,
                     config: dict | None = None) -> int:
    with SqlAlchemyUnitOfWork(session_factory=_factory(db_engine)) as uow:
        row = uow.connections.create(Connection(
            channel_id=channel_id, agent_id=agent_id, name="seed", config=config))
        uow.commit()
        return row.id


def _seed_channel(db_engine) -> int:
    from app.domain.channel import Channel
    with SqlAlchemyUnitOfWork(session_factory=_factory(db_engine)) as uow:
        row = uow.channels.create(Channel(name="种子渠道", type="local", config={}))
        uow.commit()
        return row.id


def _agent_count(db_engine) -> int:
    with Session(db_engine) as session:
        return len(session.query(AgentModel).all())


# ================================================================== Create

def test_c1_create_new_identity_succeeds(db_engine) -> None:
    """§9-1：新身份正常创建（守卫不误伤）。"""
    agent = _service(db_engine).create_agent(CreateAgent(
        name="WorkBuddy", type="acp", config=_wb_config()))
    assert agent.id > 0


def test_c2_same_identity_twice_rejected(db_engine) -> None:
    """§9-2：同租户同 WorkBuddy Agent ID 第二次创建 → 拒绝。"""
    svc = _service(db_engine)
    svc.create_agent(CreateAgent(name="WorkBuddy", type="acp", config=_wb_config()))
    with pytest.raises(DuplicateExternalAgentError) as ei:
        svc.create_agent(CreateAgent(
            name="WorkBuddy 2", type="acp", config=_wb_config()))
    assert ei.value.existing_agents and ei.value.existing_agents[0][1] == "WorkBuddy"


def test_c3_identity_only_in_connection_config_rejected(db_engine) -> None:
    """§9-3：身份只存在于 Connection.config（Home Flow 事实源）→ 仍拒绝。

    这是审计坐实的主存放位置——只查 Agent.config 的实现会在这里漏过。
    """
    svc = _service(db_engine)
    holder = _seed_agent(db_engine, "WorkBuddy")           # Agent.config 无身份
    _seed_channel(db_engine)
    _seed_connection(db_engine, channel_id=1, agent_id=holder, config=_wb_config())
    with pytest.raises(DuplicateExternalAgentError):
        svc.create_agent(CreateAgent(
            name="WorkBuddy 2", type="acp", config=_wb_config()))


def test_c4_identity_in_agent_config_rejected(db_engine) -> None:
    """§9-4：身份在 Agent.config（M7-AUI-10.3 fallback 事实源）→ 拒绝。"""
    svc = _service(db_engine)
    _seed_agent(db_engine, "WorkBuddy", config=_wb_config())
    with pytest.raises(DuplicateExternalAgentError):
        svc.create_agent(CreateAgent(
            name="WorkBuddy 2", type="acp", config=_wb_config()))


def test_c5_no_cross_tenant_match(db_engine) -> None:
    """§9-5：身份冲突绝不跨租户（tenant_id 显式作用域）。"""
    _seed_tenant(db_engine, 7)
    _seed_tenant(db_engine, 8)
    _seed_agent(db_engine, "他租户", config=_wb_config(), tenant_id=7)
    svc_t8 = _service(db_engine, tenant_id=8)
    agent = svc_t8.create_agent(CreateAgent(
        name="本租户", type="acp", config=_wb_config()))
    assert agent.id > 0  # 租户 7 的同身份结构性不可见


def test_c6_different_api_key_still_rejected(db_engine) -> None:
    """§9-6：API Key 不同 ≠ 外部 Agent 不同（key 是凭据，绝不参与身份判断）。"""
    svc = _service(db_engine)
    svc.create_agent(CreateAgent(
        name="WorkBuddy", type="acp",
        config=_wb_config(api_key="pt_key_A")))
    with pytest.raises(DuplicateExternalAgentError):
        svc.create_agent(CreateAgent(
            name="WorkBuddy 2", type="acp",
            config=_wb_config(api_key="pt_key_B")))


def test_c7_missing_agent_id_creates_no_false_match(db_engine) -> None:
    """§9-7：workbuddy_agent_id 缺失/空白 = 无可比身份；generic ACP 形态不匹配。"""
    svc = _service(db_engine)
    # 无 ID 的 workbuddy 形态不构成身份
    _seed_agent(db_engine, "wb-无ID", config={"provider": "workbuddy"})
    assert svc.create_agent(CreateAgent(
        name="wb-有ID", type="acp", config=_wb_config())).id > 0
    # generic ACP endpoint / bearer 形态绝不被解释成 WorkBuddy 身份
    _seed_agent(db_engine, "acp-generic", config={
        "protocol": "tencent-acp-sse", "bearer_token": "tok"})
    assert svc.create_agent(CreateAgent(
        name="wb-有ID2", type="acp",
        config=_wb_config(agent_id="wb-distinct-2"))).id > 0
    # 空白 ID 同样不匹配
    _seed_agent(db_engine, "wb-空白ID", config={
        "provider": "workbuddy", "workbuddy_agent_id": "   "})
    assert svc.create_agent(CreateAgent(
        name="wb-有ID3", type="acp",
        config=_wb_config(agent_id="wb-distinct-3"))).id > 0


def test_c8_local_and_generic_acp_behavior_unchanged(db_engine) -> None:
    """§9-8：local / 无身份的 generic ACP 创建完全不受守卫影响。"""
    svc = _service(db_engine)
    assert svc.create_agent(CreateAgent(name="本地", type="local")).id > 0
    assert svc.create_agent(CreateAgent(
        name="外部", type="acp",
        config={"protocol": "tencent-acp-sse"})).id > 0


def test_c9_rejection_leaves_no_extra_record(db_engine) -> None:
    """§9-9：拒绝路径零副作用（不留下任何多余 Agent 行）。"""
    svc = _service(db_engine)
    svc.create_agent(CreateAgent(name="WorkBuddy", type="acp", config=_wb_config()))
    before = _agent_count(db_engine)
    with pytest.raises(DuplicateExternalAgentError):
        svc.create_agent(CreateAgent(
            name="WorkBuddy 2", type="acp", config=_wb_config()))
    assert _agent_count(db_engine) == before


# ================================================================== Update

def test_u10_update_without_identity_change_succeeds(db_engine) -> None:
    """§9-10：不改身份的常规更新（改名）不受影响。"""
    svc = _service(db_engine)
    agent = svc.create_agent(CreateAgent(
        name="WorkBuddy", type="acp", config=_wb_config()))
    updated = svc.update_agent(agent.id, UpdateAgent(name="改名"))
    assert updated.name == "改名"


def test_u11_update_to_other_agents_identity_rejected(db_engine) -> None:
    """§9-11：PATCH 把 Agent 改成同租户另一 Agent 的身份 → 拒绝（审计 §八 场景）。"""
    svc = _service(db_engine)
    other = svc.create_agent(CreateAgent(
        name="WorkBuddy", type="acp", config=_wb_config()))
    free = svc.create_agent(CreateAgent(name="自建", type="acp"))
    with pytest.raises(DuplicateExternalAgentError) as ei:
        svc.update_agent(free.id, UpdateAgent(config=_wb_config()))
    assert ei.value.existing_agents[0][0] == other.id


def test_u12_update_to_identity_used_only_by_connection_rejected(db_engine) -> None:
    """§9-12：目标身份只存在于某 Connection.config（归属他人）→ 拒绝。"""
    svc = _service(db_engine)
    holder = _seed_agent(db_engine, "WorkBuddy")
    _seed_channel(db_engine)
    _seed_connection(db_engine, channel_id=1, agent_id=holder, config=_wb_config())
    free = svc.create_agent(CreateAgent(name="自建", type="acp"))
    with pytest.raises(DuplicateExternalAgentError):
        svc.update_agent(free.id, UpdateAgent(config=_wb_config()))


def test_u13_own_connection_identity_update_allowed(db_engine) -> None:
    """排除自身：把身份补进自己名下（自己 Connection 已代表该身份）合法。"""
    svc = _service(db_engine)
    agent = svc.create_agent(CreateAgent(name="WorkBuddy", type="acp"))
    _seed_channel(db_engine)
    _seed_connection(db_engine, channel_id=1, agent_id=agent.id, config=_wb_config())
    updated = svc.update_agent(agent.id, UpdateAgent(config=_wb_config()))
    assert updated.config["workbuddy_agent_id"] == WB_ID


def test_u14_update_other_tenant_identity_permitted(db_engine) -> None:
    """§9-13：不同租户的同身份不可见 → 更新放行（作用域 = 租户）。"""
    _seed_tenant(db_engine, 7)
    _seed_tenant(db_engine, 8)
    _seed_agent(db_engine, "他租户", config=_wb_config(), tenant_id=7)
    svc_t8 = _service(db_engine, tenant_id=8)
    free = svc_t8.create_agent(CreateAgent(name="自建", type="acp"))
    updated = svc_t8.update_agent(free.id, UpdateAgent(config=_wb_config()))
    assert updated.config["workbuddy_agent_id"] == WB_ID


def test_u15_patch_merge_and_masking_intact(db_engine, client) -> None:
    """§9-14：M7-AUI-10.2 的 merge 语义与读时脱敏在守卫之下保持不变。"""
    created = client.post("/api/agents", json={
        "name": "WorkBuddy", "type": "acp", "config": _wb_config()})
    assert created.status_code == 201
    agent_id = created.json()["id"]
    patched = client.patch(f"/api/agents/{agent_id}", json={
        "config": {"workbuddy_agent_id": WB_ID, "note": "x"}})  # 不带 api_key 键
    assert patched.status_code == 200
    body = patched.json()
    # merge：未提供的 api_key 保留（读时已脱敏）；提供的键覆写
    assert body["config"]["api_key"] == "********"
    assert body["config"]["note"] == "x"
    assert body["config"]["provider"] == "workbuddy"


# ================================================================== API 映射

def test_a16_create_duplicate_maps_to_409_with_marker(db_engine, client) -> None:
    """§5：create 冲突 → 409，detail 带「WorkBuddy Agent 已存在」标记（前端判别），无凭据。"""
    assert client.post("/api/agents", json={
        "name": "WorkBuddy", "type": "acp", "config": _wb_config()}).status_code == 201
    resp = client.post("/api/agents", json={
        "name": "WorkBuddy 2", "type": "acp",
        "config": _wb_config(api_key="pt_other_key")})
    assert resp.status_code == 409
    assert "WorkBuddy Agent 已存在" in resp.json()["detail"]
    assert "pt_" not in resp.json()["detail"]  # API Key 绝不出现在错误信息


def test_a17_update_duplicate_maps_to_409(db_engine, client) -> None:
    """§5：update 引入重复身份 → 409（同一错误族映射）。"""
    holder = client.post("/api/agents", json={
        "name": "WorkBuddy", "type": "acp", "config": _wb_config()}).json()
    free = client.post("/api/agents", json={"name": "自建", "type": "acp"}).json()
    resp = client.patch(f"/api/agents/{free['id']}", json={"config": _wb_config()})
    assert resp.status_code == 409
    assert str(holder["id"]) in resp.json()["detail"]


def test_a18_name_duplicate_still_uses_existing_409_semantics(db_engine, client) -> None:
    """同名 409（DuplicateAgentError）文案不变——前端靠标记区分两种冲突。"""
    assert client.post(
        "/api/agents", json={"name": "同名", "type": "acp"}).status_code == 201
    resp = client.post("/api/agents", json={"name": "同名", "type": "acp"})
    assert resp.status_code == 409
    assert "WorkBuddy Agent 已存在" not in resp.json()["detail"]


# ================================================================== Connection 路径

def test_x19_connection_with_identity_owned_by_other_agent_rejected(
        db_engine) -> None:
    """Home Flow 后端路径：绑定指向的 Agent 不是该身份的代表 → 拒绝。"""
    conn_svc = _conn_service(db_engine)
    agent_svc = _service(db_engine)
    holder = agent_svc.create_agent(CreateAgent(name="WorkBuddy", type="acp"))
    other = agent_svc.create_agent(CreateAgent(name="自建", type="acp"))
    channel_id = _seed_channel(db_engine)
    _seed_connection(db_engine, channel_id=channel_id, agent_id=holder.id,
                     config=_wb_config())
    with pytest.raises(DuplicateExternalAgentError):
        conn_svc.create_connection(CreateConnection(
            channel_id=channel_id, agent_id=other.id,
            name="你的 Flow", config=_wb_config()))


def test_x20_same_agent_second_channel_still_allowed(db_engine) -> None:
    """§8：同一 SteeringFlow Agent → 多 Channel 多 Connection 复用不受影响。"""
    conn_svc = _conn_service(db_engine)
    agent_svc = _service(db_engine)
    agent = agent_svc.create_agent(CreateAgent(
        name="WorkBuddy", type="acp", config=_wb_config()))
    channel_id = _seed_channel(db_engine)
    first = conn_svc.create_connection(CreateConnection(
        channel_id=channel_id, agent_id=agent.id, name="你的 Flow"))
    # 第二条 Channel
    from app.domain.channel import Channel as Ch
    with SqlAlchemyUnitOfWork(session_factory=_factory(db_engine)) as uow:
        ch2 = uow.channels.create(Ch(name="第二渠道", type="local", config={}))
        uow.commit()
    second = conn_svc.create_connection(CreateConnection(
        channel_id=ch2.id, agent_id=agent.id, name="你的 Flow",
        config=_wb_config()))
    assert first.id != second.id


def test_x21_connection_duplicate_maps_to_409(db_engine, client) -> None:
    """connections API 的 409 映射 + 标记（前端 ensureBinding 据此上抛）。"""
    holder = client.post(
        "/api/agents", json={"name": "WorkBuddy", "type": "acp"}).json()
    other = client.post(
        "/api/agents", json={"name": "自建", "type": "acp"}).json()
    channel = client.post(
        "/api/channels", json={"name": "渠道", "type": "local"}).json()
    first = client.post("/api/connections", json={
        "channel_id": channel["id"], "agent_id": holder["id"],
        "name": "你的 Flow", "config": _wb_config()})
    assert first.status_code == 201
    second = client.post("/api/connections", json={
        "channel_id": channel["id"], "agent_id": other["id"],
        "name": "你的 Flow", "config": _wb_config()})
    assert second.status_code == 409
    assert "WorkBuddy Agent 已存在" in second.json()["detail"]


# ================================================================== Connection PATCH（M7-AUI-10.5.1）

def test_p1_patch_to_identity_owned_by_other_agent_rejected(db_engine) -> None:
    """PATCH 整段替换 config 引入他人身份 → 拒绝，且**零持久化副作用**。"""
    svc = _conn_service(db_engine)
    _service(db_engine).create_agent(CreateAgent(
        name="WorkBuddy", type="acp", config=_wb_config()))
    other = _seed_agent(db_engine, "自建")            # 绑定目标：无任何身份
    channel_id = _seed_channel(db_engine)
    conn = svc.create_connection(CreateConnection(
        channel_id=channel_id, agent_id=other, name="你的 Flow"))
    with pytest.raises(DuplicateExternalAgentError):
        svc.update_connection(conn.id, UpdateConnection(
            config=_wb_config(api_key="pt_new_key")))
    # 拒绝后连接保持原样（未持久化冲突配置）
    after = _conn_service(db_engine).get_connection(conn.id)
    assert after.config is None


def test_p2_patch_without_identity_change_valid(db_engine) -> None:
    """不改有效身份的常规 PATCH（改名/停用/config=None）不受影响。"""
    svc = _conn_service(db_engine)
    agent_id = _seed_agent(db_engine, "WorkBuddy", config=_wb_config())
    channel_id = _seed_channel(db_engine)
    conn = svc.create_connection(CreateConnection(
        channel_id=channel_id, agent_id=agent_id, name="你的 Flow"))
    renamed = svc.update_connection(conn.id, UpdateConnection(name="改名"))
    assert renamed.name == "改名" and renamed.status == "active"
    disabled = svc.update_connection(conn.id, UpdateConnection(status="inactive"))
    assert disabled.status == "inactive"


def test_p3_own_identity_and_second_channel_patch_valid(db_engine) -> None:
    """§8：同一 Agent 多 Channel 复用不受影响；把身份补进自己名下连接合法。"""
    svc = _conn_service(db_engine)
    agent_id = _seed_agent(db_engine, "WorkBuddy", config=_wb_config())
    ch1 = _seed_channel(db_engine)
    conn = svc.create_connection(CreateConnection(
        channel_id=ch1, agent_id=agent_id, name="你的 Flow"))
    # PATCH 自己的连接 → 同身份（归属自己）→ 排除自身 → 放行
    updated = svc.update_connection(conn.id, UpdateConnection(
        config={**_wb_config(), "runtime_id": "rt-1"}))
    assert updated.config["workbuddy_agent_id"] == WB_ID


def test_p4_cross_tenant_patch_identity_valid(db_engine) -> None:
    """跨租户身份重用合法（作用域 = 租户）。"""
    _seed_tenant(db_engine, 7)
    _seed_tenant(db_engine, 8)
    _seed_agent(db_engine, "他租户", config=_wb_config(), tenant_id=7)
    svc_t8 = _conn_service(db_engine, tenant_id=8)
    agent_id = _seed_agent(db_engine, "本租户", tenant_id=8)
    from app.domain.channel import Channel
    with SqlAlchemyUnitOfWork(
        session_factory=_factory(db_engine), tenant_id=8
    ) as uow:
        row = uow.channels.create(Channel(name="渠道8", type="local", config={}))
        uow.commit()
    conn = svc_t8.create_connection(CreateConnection(
        channel_id=row.id, agent_id=agent_id, name="你的 Flow"))
    updated = svc_t8.update_connection(conn.id, UpdateConnection(
        config=_wb_config()))
    assert updated.config["workbuddy_agent_id"] == WB_ID


def test_p5_patch_semantics_intact(db_engine) -> None:
    """11.11 冻结语义不变：config 缺省 = 不修改（整段替换只在提供时发生）。"""
    svc = _conn_service(db_engine)
    agent_id = _seed_agent(db_engine, "WorkBuddy")
    channel_id = _seed_channel(db_engine)
    conn = svc.create_connection(CreateConnection(
        channel_id=channel_id, agent_id=agent_id, name="你的 Flow",
        config={"protocol": "tencent-acp-sse"}))
    updated = svc.update_connection(conn.id, UpdateConnection(name="改名"))
    assert updated.config == {"protocol": "tencent-acp-sse"}


def test_p6_connection_patch_duplicate_maps_to_409(db_engine, client) -> None:
    """API 层映射：PATCH 引入重复身份 → 409 + 标记，detail 无凭据。"""
    holder = client.post("/api/agents", json={
        "name": "WorkBuddy", "type": "acp", "config": _wb_config()}).json()
    other = client.post("/api/agents", json={"name": "自建", "type": "acp"}).json()
    channel = client.post(
        "/api/channels", json={"name": "渠道", "type": "local"}).json()
    conn = client.post("/api/connections", json={
        "channel_id": channel["id"], "agent_id": other["id"],
        "name": "你的 Flow"}).json()
    resp = client.patch(f"/api/connections/{conn['id']}", json={
        "config": _wb_config(api_key="pt_secret_value")})
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "WorkBuddy Agent 已存在" in detail
    assert "pt_" not in detail


# ================================================================== 边界守护

def test_b22_identity_keys_mirror_adapter_and_api_contract() -> None:
    """键名常量与 adapter / 错误文案的两侧锁定（漂移即测试失败）。"""
    from app.application.agent_identity import (
        AGENT_ID_KEY,
        PROVIDER_KEY,
        WORKBUDDY_PROVIDER,
    )
    from app.adapters.agents.workbuddy import (
        AGENT_ID_KEY as ADAPTER_AGENT_ID_KEY,
        PROVIDER_KEY as ADAPTER_PROVIDER_KEY,
        WORKBUDDY_PROVIDER as ADAPTER_WORKBUDDY_PROVIDER,
    )
    assert (PROVIDER_KEY, AGENT_ID_KEY, WORKBUDDY_PROVIDER) == (
        ADAPTER_PROVIDER_KEY, ADAPTER_AGENT_ID_KEY, ADAPTER_WORKBUDDY_PROVIDER)
    agents_src = API_AGENTS.read_text(encoding="utf-8")
    connections_src = API_CONNECTIONS.read_text(encoding="utf-8")
    for src in (agents_src, connections_src):
        assert "DuplicateExternalAgentError" in src
        assert "status_code=409" in src


def test_b23_no_db_unique_no_migration() -> None:
    """C5 = NOT YET：本任务绝不引入 DB UNIQUE / migration / generic 身份抽象。"""
    identity_src = (
        BACKEND / "app" / "application" / "agent_identity.py").read_text(encoding="utf-8")
    for banned in ("CREATE UNIQUE", "unique_index", "external_agent_id = Column",
                   "alembic", "UniqueConstraint"):
        assert banned not in identity_src
