"""Cloud-0.1.2.2.2.1 —— Connection Health Probe 实现验收。

严格按 Gate 0.1.2.2.2 落地：

    GET /api/connections/{id}/health → tenant-scoped Connection
        → ACPAdapter.probe(connection) → ConnectionHealthResponse

响应合同：{connection_id, reachable, checked_at, detail, probe}

冻结行为（Gate 0.1.2.2.2 §2/§3/§7）：
- 不写库 / 不新增 health 字段 / 不新增 health 表 / 不改 Connection.status /
  不建 Health 状态机 / 不缓存 / 不重试 / 不刷新 token / 不修改 Agent /
  RuntimeExecutionResult / Interaction / Driver；
- Fail Closed；凭据只来自 Connection.config / credential_ref；
- probe 复用真实 ACP 建连路径。

覆盖：
A. HTTP 端点：快照合同 / 404 / 503（未装配）/ 不可达映射 / 不写库 / 租户隔离；
B. ACPAdapter.probe：真实本地服务器（GET SSE 建连 + Authorization 用 config
   bearer_token）/ 非 200 / 配置缺失 / TCP 可达性 / Fail-Closed 不抛；
C. 层边界：schema 字段面 / API 源码无持久化痕迹 / 生产装配可见。
"""

from __future__ import annotations

import contextlib
import http.server
import socketserver
import threading
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.agents.acp import ACPAdapter
from app.api.management.connections import build_connections_api
from app.application.agents import AgentApplicationService, CreateAgent
from app.application.channels import ChannelApplicationService, CreateChannel
from app.application.connections import ConnectionApplicationService, CreateConnection
from app.domain.agent_probe_snapshot import AgentProbeSnapshot
from app.domain.connection import Connection
from app.persistence.models.connection import ConnectionModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork
from app.schemas.connections import ConnectionHealthResponse

BACKEND = Path(__file__).resolve().parents[1]
API_ROUTER = BACKEND / "app" / "api" / "management" / "connections.py"

_HEALTH_FIELDS = {"connection_id", "reachable", "checked_at", "detail", "probe"}
_CONFIG = {
    "protocol": "tencent-acp-sse",
    "endpoint": "http://127.0.0.1:9301",
    "bearer_token": "real-bearer-secret",
    "runtime_id": "node-runtime-1",
    "timeout_s": 5,
}
_CRED_REF = "cred_conn_1"


# ================================================================== 公共 harness


def _factory(db_engine) -> sessionmaker:
    return sessionmaker(bind=db_engine, class_=Session, expire_on_commit=False)


def _uow_factory(db_engine):
    return lambda: SqlAlchemyUnitOfWork(session_factory=_factory(db_engine))


def _service(db_engine) -> ConnectionApplicationService:
    return ConnectionApplicationService(uow_factory=_uow_factory(db_engine))


def _tenant_service(db_engine, tenant_id: int) -> ConnectionApplicationService:
    factory = _factory(db_engine)
    return ConnectionApplicationService(
        uow_factory=lambda: SqlAlchemyUnitOfWork(
            session_factory=factory, tenant_id=tenant_id))


class _RecordingProbe:
    """确定性探针桩：记录每次收到的 Connection，返回可配置结论。"""

    def __init__(self, result: AgentProbeSnapshot) -> None:
        self.result = result
        self.calls: list[Connection] = []

    def __call__(self, connection: Connection) -> AgentProbeSnapshot:
        self.calls.append(connection)
        return self.result


@pytest.fixture()
def probe() -> _RecordingProbe:
    return _RecordingProbe(AgentProbeSnapshot(reachable=True, detail=None, probe="minimal"))


@pytest.fixture()
def client(db_engine, probe) -> TestClient:
    app = FastAPI()
    app.include_router(build_connections_api(_service(db_engine), probe=probe))
    return TestClient(app)


def _seed_binding(db_engine, channel_name="wecom-main", agent_name="support-agent"):
    uow = _uow_factory(db_engine)
    channel = ChannelApplicationService(uow_factory=uow).create_channel(
        CreateChannel(name=channel_name, type="wecom"))
    agent = AgentApplicationService(uow_factory=uow).create_agent(
        CreateAgent(name=agent_name, type="acp"))
    return channel.id, agent.id


def _create(db_engine, client, **over) -> dict:
    channel_id, agent_id = _seed_binding(db_engine)
    body = {
        "channel_id": channel_id,
        "agent_id": agent_id,
        "name": "wecom-main → support-agent",
        "config": dict(_CONFIG),
        "credential_ref": _CRED_REF,
    }
    body.update(over)
    resp = client.post("/api/connections", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _db_get(db_engine, connection_id: int) -> ConnectionModel:
    with Session(db_engine) as session:
        row = session.get(ConnectionModel, connection_id)
        session.expunge(row)
        return row


def _conn(config=None) -> Connection:
    return Connection(channel_id=1, agent_id=1, name="c", id=7, config=config)


# ================================================================== A. HTTP 端点


def test_health_200_snapshot_contract(db_engine, client, probe) -> None:
    """快照字段面恰好 5 个；探针收到 tenant-scoped、含真实 config 的 Connection。"""
    created = _create(db_engine, client)
    resp = client.get(f"/api/connections/{created['id']}/health")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == _HEALTH_FIELDS
    assert body["connection_id"] == created["id"]
    assert body["reachable"] is True
    assert body["probe"] == "minimal"
    assert body["detail"] is None
    assert body["checked_at"]  # 请求时刻生成的非空时间戳

    # 探针恰好被调用一次，且收到的是领域 Connection（真实 config/credential_ref，
    # 供 probe 连接使用——不是响应面脱敏后的副本）。
    assert len(probe.calls) == 1
    passed = probe.calls[0]
    assert passed.id == created["id"]
    assert passed.config["bearer_token"] == "real-bearer-secret"
    assert passed.config["endpoint"] == "http://127.0.0.1:9301"
    assert passed.credential_ref == _CRED_REF


def test_health_404_missing_connection(db_engine, client) -> None:
    assert client.get("/api/connections/99999/health").status_code == 404


def test_health_503_when_probe_not_configured(db_engine) -> None:
    """未装配探针 = 探针不可用（503），在读取 Connection 之前判定。"""
    app = FastAPI()
    app.include_router(build_connections_api(_service(db_engine)))  # 无 probe
    client = TestClient(app)
    assert client.get("/api/connections/1/health").status_code == 503


def test_health_reflects_unreachable(db_engine, probe) -> None:
    """不可达由探针结论映射进快照（detail/probe 原样透传），非 500 炸。"""
    probe.result = AgentProbeSnapshot(
        reachable=False, detail="ConnectionRefusedError: refused", probe="tencent-acp-sse")
    app = FastAPI()
    app.include_router(build_connections_api(_service(db_engine), probe=probe))
    client = TestClient(app)
    created = _create(db_engine, client)

    body = client.get(f"/api/connections/{created['id']}/health").json()
    assert body["reachable"] is False
    assert body["detail"] == "ConnectionRefusedError: refused"
    assert body["probe"] == "tencent-acp-sse"


def test_health_does_not_write_db(db_engine, client) -> None:
    """健康诊断绝不写库：行数、status、config、credential_ref、updated_at 全不变。"""
    created = _create(db_engine, client)
    cid = created["id"]
    before = _db_get(db_engine, cid)
    snap_before = (before.status, before.config, before.credential_ref,
                   before.updated_at, before.name)

    assert client.get(f"/api/connections/{cid}/health").status_code == 200
    assert client.get(f"/api/connections/{cid}/health").status_code == 200

    after = _db_get(db_engine, cid)
    snap_after = (after.status, after.config, after.credential_ref,
                  after.updated_at, after.name)
    assert snap_after == snap_before


def test_health_is_tenant_scoped(db_engine, probe) -> None:
    """tenant 2 看不到 tenant 1 的 Connection → 404，且探针从未被调用。"""
    factory = _factory(db_engine)

    def uowf():
        return SqlAlchemyUnitOfWork(session_factory=factory, tenant_id=1)

    channel = ChannelApplicationService(uow_factory=uowf).create_channel(
        CreateChannel(name="ch-t1", type="wecom"))
    agent = AgentApplicationService(uow_factory=uowf).create_agent(
        CreateAgent(name="ag-t1", type="acp"))
    conn = ConnectionApplicationService(uow_factory=uowf).create_connection(
        CreateConnection(channel_id=channel.id, agent_id=agent.id, name="c-t1"))

    app_t1 = FastAPI()
    app_t1.include_router(build_connections_api(_tenant_service(db_engine, 1), probe=probe))
    assert TestClient(app_t1).get(f"/api/connections/{conn.id}/health").status_code == 200
    assert len(probe.calls) == 1

    app_t2 = FastAPI()
    app_t2.include_router(build_connections_api(_tenant_service(db_engine, 2), probe=probe))
    assert TestClient(app_t2).get(f"/api/connections/{conn.id}/health").status_code == 404
    assert len(probe.calls) == 1  # 跨租户 404 时探针未被触发


# ================================================================== B. ACPAdapter.probe


class _ProbeHandler(http.server.BaseHTTPRequestHandler):
    """记录请求头；按 server.response_status 应答（无 body）。"""

    def do_GET(self):  # noqa: N802 (stdlib 命名)
        self.server.captured_headers = {k.lower(): v for k, v in self.headers.items()}
        self.send_response(self.server.response_status)
        self.end_headers()

    def log_message(self, *args):  # 静默
        pass


class _ProbeServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


@contextlib.contextmanager
def _running_probe_server(status: int = 200):
    server = _ProbeServer(("127.0.0.1", 0), _ProbeHandler)
    server.response_status = status
    server.captured_headers = {}
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    try:
        yield server, f"http://{host}:{port}/sse"
    finally:
        server.shutdown()
        server.server_close()


def test_probe_tencent_reuses_real_connect_path_and_config_credentials() -> None:
    """tencent-acp-sse：真实 GET SSE 建连（status 200 = 可达）；凭据只来自 config。"""
    with _running_probe_server(status=200) as (server, endpoint):
        result = ACPAdapter().probe(_conn(config={
            "protocol": "tencent-acp-sse",
            "endpoint": endpoint,
            "bearer_token": "tok-secret",
            "runtime_id": "rt-1",
            "timeout_s": 5,
        }))
        captured = dict(server.captured_headers)

    assert result.reachable is True
    assert result.probe == "tencent-acp-sse"
    assert result.detail is None
    # 复用真实建连路径：Authorization 由 config bearer_token 派生 +
    # Acp-Connection-Id（每次独立）+ Accept SSE。
    assert captured.get("authorization") == "Bearer tok-secret"
    assert "acp-connection-id" in captured
    assert captured.get("accept") == "text/event-stream"


def test_probe_tencent_non_200_is_unreachable() -> None:
    with _running_probe_server(status=500) as (server, endpoint):
        result = ACPAdapter().probe(_conn(config={
            "protocol": "tencent-acp-sse", "endpoint": endpoint,
            "bearer_token": "t", "timeout_s": 5,
        }))
    assert result.reachable is False
    assert "500" in (result.detail or "")
    assert result.probe == "tencent-acp-sse"


def test_probe_minimal_tcp_reachability() -> None:
    with _running_probe_server(status=200) as (server, endpoint):
        result = ACPAdapter().probe(_conn(config={
            "protocol": "minimal", "endpoint": endpoint, "timeout_s": 5}))
    assert result.reachable is True
    assert result.probe == "minimal"


def test_probe_minimal_closed_port_is_unreachable() -> None:
    result = ACPAdapter().probe(_conn(config={
        "protocol": "minimal", "endpoint": "http://127.0.0.1:1", "timeout_s": 2}))
    assert result.reachable is False
    assert result.probe == "minimal"


@pytest.mark.parametrize(
    "cfg",
    [
        None,
        "not-a-dict",
        {},
        {"protocol": "tencent-acp-sse"},
        {"protocol": "tencent-acp-sse", "endpoint": "http://x"},
        {"protocol": "tencent-acp-sse", "endpoint": "http://x", "bearer_token": "   "},
        {"protocol": "tencent-acp-sse", "endpoint": "ftp://x", "bearer_token": "t"},
        {"protocol": "minimal", "endpoint": "not-a-url"},
        {"protocol": "minimal", "endpoint": "http://x", "timeout_s": "bad"},
        {"protocol": "minimal", "endpoint": "http://x", "timeout_s": 0},
    ],
    ids=["none", "str", "empty", "no-endpoint", "no-token", "blank-token", "bad-scheme",
         "bad-url", "bad-timeout", "zero-timeout"],
)
def test_probe_fail_closed_never_raises(cfg) -> None:
    """配置缺失/非法一律归一为 reachable=False，绝不抛、绝不返回半成品。"""
    result = ACPAdapter().probe(_conn(config=cfg))
    assert isinstance(result, AgentProbeSnapshot)
    assert result.reachable is False


def test_probe_non_connection_input_is_fail_closed() -> None:
    result = ACPAdapter().probe(object())  # type: ignore[arg-type]
    assert result.reachable is False


# ================================================================== C. 层边界


def test_health_response_schema_fields() -> None:
    assert set(ConnectionHealthResponse.model_fields) == _HEALTH_FIELDS
    assert "config" not in ConnectionHealthResponse.model_fields
    assert "status" not in ConnectionHealthResponse.model_fields


def test_connections_api_source_has_no_health_persistence() -> None:
    """诊断端点：无持久化/调度/状态机痕迹（导数不落库）。"""
    src = API_ROUTER.read_text(encoding="utf-8")
    assert "def get_connection_health" in src
    for token in ("uow.connections.update", "ConnectionHealthModel", "health_status",
                  "last_health_check", "BackgroundScheduler", "schedule_health", "add_job"):
        assert token not in src, f"健康端点出现持久化/调度痕迹：{token}"


def test_production_app_exposes_health_endpoint() -> None:
    """main.py 组合根已装配探针：openapi 可见 GET 端点与 5 字段 schema。"""
    from app.main import app

    doc = TestClient(app).get("/openapi.json").json()
    path = "/api/connections/{connection_id}/health"
    assert path in doc["paths"] and "get" in doc["paths"][path]
    props = doc["components"]["schemas"]["ConnectionHealthResponse"]["properties"]
    assert set(props) == _HEALTH_FIELDS
