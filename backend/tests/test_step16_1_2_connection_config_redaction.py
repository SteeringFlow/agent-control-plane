"""Step 16.1.2 —— Connection credential exposure 修复验收（响应脱敏 + 哨兵护栏）。

P1 真实接入（16.1.1 审计）证实：Connection.config 携带 runtime 凭据
（tencent-acp-sse 的 bearer_token），而 11.11 冻结的「config 不脱敏」合同使
GET /api/connections 明文回显真实 token。本步在 schema 层最小修复
（16.1.1 裁决 GO）：

合同（16.1.2 冻结）：
1. 响应面脱敏：GET detail / GET list / POST / PATCH 的 config 敏感键
   （SENSITIVE_CONNECTION_CONFIG_KEYS，键名 strip+lower 匹配）→ ********；
   非敏感键（endpoint / runtime_id / timeout_s / ...）原样返回；
2. 脱敏只在响应序列化层——DB 行仍存真实凭据，runtime dispatch
   （ACPAdapter 直读 Connection.config）零改动、零感知；
3. 写路径哨兵护栏：Create/Update 的 config 值等于 "********" → 422
   （整段替换语义下，防止脱敏回显被原样回写覆写真实凭据；按值匹配、
   与键名无关——任何真实凭据都不会恰好等于 8 个星号）；
4. round-trip：PATCH 省略 config / config=null / 空 body 均不触碰 DB 凭据。

不做（16.1.1 冻结边界）：API route / persistence / ACPAdapter / 部署 / auth。
"""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.api.management.connections import build_connections_api
from app.application.agents import AgentApplicationService, CreateAgent
from app.application.channels import ChannelApplicationService, CreateChannel
from app.application.connections import ConnectionApplicationService
from app.persistence.models.connection import ConnectionModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork
from app.schemas.connections import SENSITIVE_CONNECTION_CONFIG_KEYS

BACKEND = Path(__file__).resolve().parents[1]
SCHEMAS = BACKEND / "app" / "schemas" / "connections.py"

_REDACTED = "********"
_SECRET = "real-bearer-secret-1"
_CONFIG = {
    "protocol": "tencent-acp-sse",
    "endpoint": "http://127.0.0.1:9301",
    "bearer_token": _SECRET,
    "runtime_id": "node-runtime-1",
    "timeout_s": 15,
}
_CRED_REF = "cred_conn_001"


def _factory(db_engine) -> sessionmaker:
    return sessionmaker(bind=db_engine, class_=Session, expire_on_commit=False)


def _uow_factory(db_engine):
    return lambda: SqlAlchemyUnitOfWork(session_factory=_factory(db_engine))


def _service(db_engine) -> ConnectionApplicationService:
    return ConnectionApplicationService(uow_factory=_uow_factory(db_engine))


@pytest.fixture()
def client(db_engine):
    """真实 HTTP 探针：test app 装配 build_connections_api(内存库 Service)。"""
    test_app = FastAPI()
    test_app.include_router(build_connections_api(_service(db_engine)))
    return TestClient(test_app)


def _seed_binding(db_engine, channel_name="wecom-main", agent_name="support-agent") -> tuple[int, int]:
    """经真实 Application 服务播种绑定目标，返回 (channel_id, agent_id)。"""
    uow = _uow_factory(db_engine)
    channel = ChannelApplicationService(uow_factory=uow).create_channel(
        CreateChannel(name=channel_name, type="wecom"))
    agent = AgentApplicationService(uow_factory=uow).create_agent(
        CreateAgent(name=agent_name, type="acp"))
    return channel.id, agent.id


def _payload(channel_id: int, agent_id: int, /, **over) -> dict:
    body = {
        "channel_id": channel_id,
        "agent_id": agent_id,
        "name": "wecom-main → support-agent",
        "config": dict(_CONFIG),
        "credential_ref": _CRED_REF,
    }
    body.update(over)
    return body


def _create(db_engine, client, **over) -> dict:
    channel_id, agent_id = _seed_binding(db_engine)
    resp = client.post("/api/connections", json=_payload(channel_id, agent_id, **over))
    assert resp.status_code == 201, resp.text
    return resp.json()


def _db_get(db_engine, connection_id: int) -> ConnectionModel:
    """绕过全部应用层，直接读 DB 行（验证真实凭据落库 / 未被脱敏污染）。"""
    with Session(db_engine) as session:
        row = session.get(ConnectionModel, connection_id)
        session.expunge(row)
        return row


# ================================================================== 合同 1：响应面脱敏


def test_get_detail_masks_bearer_token_keeps_metadata(db_engine, client) -> None:
    created = _create(db_engine, client)
    resp = client.get(f"/api/connections/{created['id']}")
    assert resp.status_code == 200
    config = resp.json()["config"]
    assert config["bearer_token"] == _REDACTED
    assert config["endpoint"] == "http://127.0.0.1:9301"
    assert config["runtime_id"] == "node-runtime-1"
    assert config["timeout_s"] == 15
    assert _SECRET not in resp.text, "响应泄漏真实 bearer_token"


def test_all_response_faces_masked_list_post_patch(db_engine, client) -> None:
    """四个响应面共用 ConnectionResponse：POST / GET list / PATCH 全部脱敏。"""
    created = _create(db_engine, client)
    cid = created["id"]
    assert created["config"]["bearer_token"] == _REDACTED      # POST 响应
    assert _SECRET not in client.get("/api/connections").text  # GET list
    resp = client.patch(f"/api/connections/{cid}", json={"name": "renamed"})
    assert resp.status_code == 200
    assert resp.json()["config"]["bearer_token"] == _REDACTED  # PATCH 响应
    assert _SECRET not in resp.text


@pytest.mark.parametrize("key", sorted(SENSITIVE_CONNECTION_CONFIG_KEYS))
def test_every_sensitive_key_masked(db_engine, client, key) -> None:
    """敏感键集合逐键验证（strip+lower 匹配的唯一事实来源 = schema 层集合）。"""
    created = _create(db_engine, client, config={key: "s", "endpoint": "http://x"})
    got = client.get(f"/api/connections/{created['id']}").json()["config"]
    assert got[key] == _REDACTED
    assert got["endpoint"] == "http://x"


def test_key_matching_is_stripped_and_lowercased(db_engine, client) -> None:
    """键名 strip + lower 匹配（宁多勿漏，镜像 Channel 11.8 纪律）。"""
    config = {"Bearer_Token": "s1", "  bearer_token  ": "s2", "endpoint": "http://x"}
    created = _create(db_engine, client, config=config)
    got = client.get(f"/api/connections/{created['id']}").json()["config"]
    assert got["Bearer_Token"] == _REDACTED
    assert got["  bearer_token  "] == _REDACTED
    assert got["endpoint"] == "http://x"
    assert "s1" not in client.get(f"/api/connections/{created['id']}").text
    assert "s2" not in client.get(f"/api/connections/{created['id']}").text


def test_non_sensitive_keys_echoed_verbatim(db_engine, client) -> None:
    """非敏感键管理可见：所见即所存（脱敏不扩大到任意键）。"""
    config = {"endpoint": "http://x", "protocol": "tencent-acp-sse",
              "runtime_id": "rt-1", "timeout_s": 10, "note": "hello"}
    created = _create(db_engine, client, config=config)
    got = client.get(f"/api/connections/{created['id']}").json()["config"]
    assert got == config


def test_null_config_response_untouched(db_engine, client) -> None:
    """config=None 的响应保持 None（脱敏函数对 None 透传）。"""
    created = _create(db_engine, client, config=None)
    assert client.get(f"/api/connections/{created['id']}").json()["config"] is None


# ================================================================== 合同 2：脱敏只在响应层，runtime 路径零感知


def test_redaction_is_response_only_db_keeps_real_secret(db_engine, client) -> None:
    created = _create(db_engine, client)
    row = _db_get(db_engine, created["id"])
    assert row.config["bearer_token"] == _SECRET, "DB 真实凭据被脱敏污染"
    assert row.config["endpoint"] == "http://127.0.0.1:9301"


# ================================================================== 合同 3：写路径哨兵护栏


def test_create_rejects_redacted_sentinel_422(db_engine, client) -> None:
    channel_id, agent_id = _seed_binding(db_engine)
    resp = client.post("/api/connections", json=_payload(
        channel_id, agent_id,
        config={"bearer_token": _REDACTED, "endpoint": "http://x"}))
    assert resp.status_code == 422


def test_patch_rejects_redacted_sentinel_422_and_db_untouched(db_engine, client) -> None:
    """整段替换语义下，哨兵回写会覆写真实凭据——合同面 422 拒绝，DB 不变。"""
    created = _create(db_engine, client)
    cid = created["id"]
    resp = client.patch(f"/api/connections/{cid}", json={
        "config": {"endpoint": "http://new", "bearer_token": _REDACTED}})
    assert resp.status_code == 422
    row = _db_get(db_engine, cid)
    assert row.config["bearer_token"] == _SECRET
    assert row.config["endpoint"] == "http://127.0.0.1:9301"


def test_patch_rejects_sentinel_under_any_key_name(db_engine, client) -> None:
    """护栏按值匹配、与键名无关（与 UI 哨兵逻辑同一纪律）。"""
    created = _create(db_engine, client)
    cid = created["id"]
    for cfg in ({"my_custom_credential": _REDACTED}, {"token": _REDACTED}):
        resp = client.patch(f"/api/connections/{cid}", json={"config": cfg})
        assert resp.status_code == 422, f"{cfg} → {resp.status_code}"
    assert _db_get(db_engine, cid).config["bearer_token"] == _SECRET


# ================================================================== 合同 4：round-trip 不破坏


def test_patch_without_config_never_touches_db_secret(db_engine, client) -> None:
    """省略 config / config=null / 空 body = 不修改——真实凭据原样保留。"""
    created = _create(db_engine, client)
    cid = created["id"]
    client.patch(f"/api/connections/{cid}", json={"name": "renamed"})
    client.patch(f"/api/connections/{cid}", json={"config": None})
    client.patch(f"/api/connections/{cid}", json={})
    assert _db_get(db_engine, cid).config["bearer_token"] == _SECRET


def test_patch_real_secret_rotation_still_works(db_engine, client) -> None:
    """轮换凭据 = 提交真实新值（非哨兵）——写路径畅通，响应脱敏。"""
    created = _create(db_engine, client)
    cid = created["id"]
    resp = client.patch(f"/api/connections/{cid}", json={
        "config": {"endpoint": "http://127.0.0.1:9301",
                   "bearer_token": "rotated-secret-2",
                   "runtime_id": "node-runtime-1", "timeout_s": 15}})
    assert resp.status_code == 200
    assert resp.json()["config"]["bearer_token"] == _REDACTED
    assert _db_get(db_engine, cid).config["bearer_token"] == "rotated-secret-2"


# ================================================================== 反向守护


def test_schema_source_keeps_sanitization() -> None:
    """脱敏与护栏实现不被静默移除（源码级反向守护）。"""
    src = SCHEMAS.read_text(encoding="utf-8")
    assert "def sanitize_connection_config" in src
    assert "SENSITIVE_CONNECTION_CONFIG_KEYS" in src
    assert "def _reject_redacted_sentinel" in src
