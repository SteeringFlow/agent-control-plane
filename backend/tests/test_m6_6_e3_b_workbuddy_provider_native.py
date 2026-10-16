"""M6.6-E3-B —— WorkBuddy Provider-native Adapter 实现验收。

范围（E3-B §23 T1–T12）：
- Provider-native Agent config / API Key 认证 / API Key 脱敏 / secrets 注入（官方契约）；
- Lazy Runtime creation（Connect 不创建 / Verify 创建 / 二次 Verify 复用）；
- Runtime 创建响应解析 / acpLink token 不泄漏；
- WorkBuddy 执行复用既有 ACP SSE client；Provider 失败映射既有 error model；
- WorkBuddy 不改 ACP Core；既有 ACP provider path 回归不受影响。

真实 WorkBuddy E2E：需要真实 API Key / Agent ID → 本仓库**无真实凭据**，标
`NOT VERIFIED`（见实现报告 §15）。不伪造真实 E2E。
"""

from __future__ import annotations

import ast
import contextlib
import http.server
import json
import socketserver
import threading
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.agents.acp import ACPAdapter
from app.adapters.agents.workbuddy import (
    WorkBuddyManagementClient,
    WorkBuddyManagementError,
    WorkBuddyProvisioner,
)
from app.api.management.connections import build_connections_api
from app.application.agents import AgentApplicationService, CreateAgent
from app.application.channels import ChannelApplicationService, CreateChannel
from app.application.connections import ConnectionApplicationService, CreateConnection
from app.application.errors import AgentDispatchFailedError
from app.domain.agent_probe_snapshot import AgentProbeSnapshot
from app.domain.agent_request import AgentRequest
from app.domain.connection import Connection
from app.persistence.models.connection import ConnectionModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork
from app.schemas.connections import sanitize_connection_config

BACKEND = Path(__file__).resolve().parents[1]
APP_DIR = BACKEND / "app"

_API_TOKEN = "acp-token-secret"
_API_KEY = "wb-api-key-secret"
_MANAGEMENT_RESPONSE = json.dumps({
    "data": {
        "id": "rt-abc-1",
        "links": {"acpLink": {"url": "https://runtime.example/acp", "token": _API_TOKEN}},
    },
}).encode("utf-8")

_WB_CONFIG = {
    "provider": "workbuddy",
    "workbuddy_agent_id": "agent-9",
    "api_key": _API_KEY,
}


# ================================================================== harness


def _factory(db_engine) -> sessionmaker:
    return sessionmaker(bind=db_engine, class_=Session, expire_on_commit=False)


def _uow_factory(db_engine):
    return lambda: SqlAlchemyUnitOfWork(session_factory=_factory(db_engine))


def _service(db_engine) -> ConnectionApplicationService:
    return ConnectionApplicationService(uow_factory=_uow_factory(db_engine))


class _FakeTransport:
    """确定性 Management API 传输桩：记录调用，返回可配置响应（不触网）。"""

    def __init__(self, *, status: int = 200, body: bytes = _MANAGEMENT_RESPONSE,
                 error: Exception | None = None) -> None:
        self.status = status
        self.body = body
        self.error = error
        self.calls: list[dict] = []

    def __call__(self, method, url, headers, body, timeout_s):
        self.calls.append({
            "method": method, "url": url, "headers": dict(headers), "body": body,
        })
        if self.error is not None:
            raise self.error
        return self.status, self.body


class _RecordingProbe:
    """记录收到的 Connection，返回可配置结论（替代真实可达性探针）。"""

    def __init__(self, reachable: bool = True, detail=None,
                 probe: str = "tencent-acp-sse") -> None:
        self._snap = AgentProbeSnapshot(reachable, detail, probe)
        self.calls: list[Connection] = []

    def __call__(self, connection: Connection) -> AgentProbeSnapshot:
        self.calls.append(connection)
        return self._snap


def _conn(config=None, *, conn_id: int = 7) -> Connection:
    return Connection(channel_id=1, agent_id=1, name="c", id=conn_id, config=config)


def _seed_binding(db_engine):
    uow = _uow_factory(db_engine)
    channel = ChannelApplicationService(uow_factory=uow).create_channel(
        CreateChannel(name="wecom-main", type="wecom"))
    agent = AgentApplicationService(uow_factory=uow).create_agent(
        CreateAgent(name="workbuddy", type="acp"))
    return channel.id, agent.id


def _client(db_engine, *, probe=None, verify=None) -> TestClient:
    app = FastAPI()
    app.include_router(build_connections_api(_service(db_engine), probe=probe, verify=verify))
    return TestClient(app)


def _create_connection(db_engine, client, config) -> dict:
    channel_id, agent_id = _seed_binding(db_engine)
    resp = client.post("/api/connections", json={
        "channel_id": channel_id, "agent_id": agent_id,
        "name": "wecom-main → workbuddy", "config": config,
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


def _db_config(db_engine, connection_id: int) -> dict:
    with Session(db_engine) as session:
        row = session.get(ConnectionModel, connection_id)
        session.expunge(row)
        return row.config


# ================================================================== T1 config namespace / cache


def test_t1_workbuddy_config_namespace_and_cache_writeback() -> None:
    """T1：provider/agent_id/api_key 输入 → 缓存写回既有 tencent-acp-sse 键。"""
    transport = _FakeTransport()
    provisioner = WorkBuddyProvisioner(
        probe=_RecordingProbe(True), client=WorkBuddyManagementClient(transport=transport))
    snapshot, updated = provisioner.verify(_conn(dict(_WB_CONFIG)))

    assert snapshot.reachable is True
    assert updated is not None
    assert updated["provider"] == "workbuddy"
    assert updated["workbuddy_agent_id"] == "agent-9"
    assert updated["api_key"] == _API_KEY          # 保留（Provider 凭据）
    assert updated["protocol"] == "tencent-acp-sse"
    assert updated["endpoint"] == "https://runtime.example/acp"
    assert updated["bearer_token"] == _API_TOKEN
    assert updated["runtime_id"] == "rt-abc-1"


def test_t1_non_workbuddy_connection_is_read_only_probe() -> None:
    """非 WorkBuddy 连接：verify 退化为只读探针（无写入、无 Management 调用）。"""
    transport = _FakeTransport()
    probe = _RecordingProbe(True, probe="minimal")
    provisioner = WorkBuddyProvisioner(
        probe=probe, client=WorkBuddyManagementClient(transport=transport))
    snapshot, updated = provisioner.verify(_conn({"protocol": "minimal", "endpoint": "http://x"}))

    assert updated is None
    assert transport.calls == []
    assert len(probe.calls) == 1


# ================================================================== T2/T8 credential masking / no leak


def test_t2_api_key_and_runtime_token_masked_on_read() -> None:
    """T2：api_key / bearer_token 读时脱敏；非敏感键原样可见。"""
    out = sanitize_connection_config({
        "provider": "workbuddy", "workbuddy_agent_id": "agent-9",
        "api_key": _API_KEY, "bearer_token": _API_TOKEN,
        "endpoint": "https://runtime.example/acp", "runtime_id": "rt-abc-1",
    })
    assert out["api_key"] == "********"
    assert out["bearer_token"] == "********"
    assert out["provider"] == "workbuddy"
    assert out["workbuddy_agent_id"] == "agent-9"
    assert out["runtime_id"] == "rt-abc-1"


def test_t8_verify_never_leaks_runtime_token_in_http_response(db_engine) -> None:
    """T8：Verify HTTP 响应与其后 GET 详情都不泄漏 acpLink token / API Key。"""
    transport = _FakeTransport()
    provisioner = WorkBuddyProvisioner(
        probe=_RecordingProbe(True), client=WorkBuddyManagementClient(transport=transport))
    client = _client(db_engine, verify=provisioner.verify)
    created = _create_connection(db_engine, client, dict(_WB_CONFIG))

    resp = client.post(f"/api/connections/{created['id']}/verify")
    assert resp.status_code == 200
    assert _API_TOKEN not in resp.text
    assert _API_KEY not in resp.text

    detail = client.get(f"/api/connections/{created['id']}")
    assert _API_TOKEN not in detail.text
    assert _API_KEY not in detail.text
    assert detail.json()["config"]["bearer_token"] == "********"
    assert detail.json()["config"]["api_key"] == "********"


# ================================================================== T3 API key in manifest secrets（官方契约）


def test_t3_api_key_in_agent_manifest_secrets() -> None:
    """T3：agentManifest.secrets 必含 CODEBUDDY_API_KEY（官方契约，缺失 → 上游 502）。

    E3-A §12 的「绝不注入 secrets」裁决已被真实 wire 推翻：CodeBuddy 上游在
    secrets 缺失时直接 502；官方对接说明明确要求把 API Key 注入 Runtime secrets
    （Agent 沙箱依赖它调 CodeBuddy API）。该值仅随创建请求发送，不落库、不回显。
    """
    transport = _FakeTransport()
    WorkBuddyManagementClient(transport=transport).create_runtime(
        api_key=_API_KEY, agent_id="agent-9", runtime_name="rn-1")

    call = transport.calls[0]
    sent = json.loads(call["body"].decode("utf-8"))
    assert sent["runtimeName"] == "rn-1"
    assert sent["agentManifest"]["id"] == "agent-9"
    assert sent["agentManifest"]["manifestVersion"] == "1.0"
    secrets = sent["agentManifest"]["secrets"]
    assert isinstance(secrets, list) and len(secrets) == 1
    assert secrets[0]["key"] == "CODEBUDDY_API_KEY"
    assert secrets[0]["value"] == _API_KEY
    # API Key 只出现在管理面 Authorization 头与创建请求 body（官方契约要求）。
    assert call["headers"]["Authorization"] == f"Bearer {_API_KEY}"


def test_t3b_source_app_header_only_when_configured() -> None:
    """X-Source-App 只在 provider configuration 提供时发送（不猜生产值）。"""
    without = _FakeTransport()
    WorkBuddyManagementClient(transport=without).create_runtime(
        api_key=_API_KEY, agent_id="a", runtime_name="rn")
    assert "X-Source-App" not in without.calls[0]["headers"]

    with_app = _FakeTransport()
    WorkBuddyManagementClient(transport=with_app, source_app="steeringflow").create_runtime(
        api_key=_API_KEY, agent_id="a", runtime_name="rn")
    assert with_app.calls[0]["headers"]["X-Source-App"] == "steeringflow"


# ================================================================== T4 Connect 不创建 Runtime


def test_t4_connect_does_not_create_runtime(db_engine) -> None:
    """T4：POST /api/connections（Connect）只建立绑定；绝不创建 Runtime。"""
    transport = _FakeTransport()
    client = _client(db_engine, verify=WorkBuddyProvisioner(
        probe=_RecordingProbe(True),
        client=WorkBuddyManagementClient(transport=transport)).verify)

    created = _create_connection(db_engine, client, dict(_WB_CONFIG))

    assert transport.calls == []
    stored = _db_config(db_engine, created["id"])
    assert stored["provider"] == "workbuddy"
    assert "runtime_id" not in stored       # 未缓存 Runtime
    assert "endpoint" not in stored


# ================================================================== T5/T6/T7 lazy create + reuse + parse


def test_t5_and_t6_verify_creates_then_reuses_runtime(db_engine) -> None:
    """T5 Verify 创建 Runtime；T6 二次 Verify 复用（不重复创建）。"""
    transport = _FakeTransport()
    provisioner = WorkBuddyProvisioner(
        probe=_RecordingProbe(True), client=WorkBuddyManagementClient(transport=transport))
    client = _client(db_engine, verify=provisioner.verify)
    created = _create_connection(db_engine, client, dict(_WB_CONFIG))
    cid = created["id"]

    first = client.post(f"/api/connections/{cid}/verify")
    assert first.status_code == 200 and first.json()["reachable"] is True
    assert len(transport.calls) == 1

    second = client.post(f"/api/connections/{cid}/verify")
    assert second.status_code == 200 and second.json()["reachable"] is True
    assert len(transport.calls) == 1        # 复用 Runtime A——未创建 B / C

    # 缓存确实落库（经既有 update），且敏感键读时脱敏。
    stored = _db_config(db_engine, cid)
    assert stored["runtime_id"] == "rt-abc-1"
    assert stored["endpoint"] == "https://runtime.example/acp"
    assert stored["bearer_token"] == _API_TOKEN


def test_t7_runtime_response_parsing_is_fail_closed() -> None:
    """T7：解析 data.id / links.acpLink.url / token；残缺响应 → 管理面错误。"""
    ok = _FakeTransport()
    link = WorkBuddyManagementClient(transport=ok).create_runtime(
        api_key=_API_KEY, agent_id="a", runtime_name="rn")
    assert (link.runtime_id, link.url, link.token) == (
        "rt-abc-1", "https://runtime.example/acp", _API_TOKEN)

    for bad_body in (
        b"not-json",
        json.dumps({"data": {}}).encode(),                        # 缺 id / links
        json.dumps({"data": {"id": "r", "links": {}}}).encode(),  # 缺 acpLink
        json.dumps({"data": {"id": "r", "links": {"acpLink": {"url": "https://x"}}}}).encode(),
    ):
        with pytest.raises(WorkBuddyManagementError):
            WorkBuddyManagementClient(transport=_FakeTransport(body=bad_body)).create_runtime(
                api_key=_API_KEY, agent_id="a", runtime_name="rn")


def test_t7b_management_failure_is_fail_closed_no_write(db_engine) -> None:
    """管理面失败 → 探针不可达快照 + 无缓存写入（绝不盲目创建）。"""
    transport = _FakeTransport(error=OSError("connection refused"))
    provisioner = WorkBuddyProvisioner(
        probe=_RecordingProbe(True), client=WorkBuddyManagementClient(transport=transport))
    snapshot, updated = provisioner.verify(_conn(dict(_WB_CONFIG)))

    assert snapshot.reachable is False
    assert updated is None
    assert snapshot.detail == "workbuddy connection could not be verified"
    assert "runtime.example" not in (snapshot.detail or "")


def test_t7c_cached_unreachable_is_not_recreated(db_engine) -> None:
    """已缓存但不可达 → Fail closed，不重复创建（E3-B §18）。"""
    transport = _FakeTransport()
    probe = _RecordingProbe(False, detail="ConnectionRefusedError")
    provisioner = WorkBuddyProvisioner(
        probe=probe, client=WorkBuddyManagementClient(transport=transport))
    cached_config = {
        **_WB_CONFIG,
        "protocol": "tencent-acp-sse",
        "endpoint": "https://runtime.example/acp",
        "bearer_token": _API_TOKEN,
        "runtime_id": "rt-abc-1",
    }
    snapshot, updated = provisioner.verify(_conn(cached_config))

    assert snapshot.reachable is False
    assert updated is None
    assert transport.calls == []


# ================================================================== T9 existing ACP SSE reuse


class _SseProbeHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (stdlib)
        self.server.captured_headers = {k.lower(): v for k, v in self.headers.items()}
        self.send_response(self.server.response_status)
        self.end_headers()

    def log_message(self, *args):
        pass


class _SseProbeServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


@contextlib.contextmanager
def _running_probe_server(status: int = 200):
    server = _SseProbeServer(("127.0.0.1", 0), _SseProbeHandler)
    server.response_status = status
    server.captured_headers = {}
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    try:
        yield server, f"http://{host}:{port}/acp"
    finally:
        server.shutdown()
        server.server_close()


def test_t9_cached_runtime_uses_existing_acp_sse_binding() -> None:
    """T9：缓存 acpLink → 既有 ACPAdapter（tencent-acp-sse binding）直接可用。"""
    with _running_probe_server(status=200) as (server, endpoint):
        cached = {
            "provider": "workbuddy",
            "protocol": "tencent-acp-sse",
            "endpoint": endpoint,
            "bearer_token": _API_TOKEN,
            "runtime_id": "rt-abc-1",
            "timeout_s": 5,
        }
        snapshot = ACPAdapter().probe(_conn(cached))
        captured = dict(server.captured_headers)

    assert snapshot.reachable is True
    assert snapshot.probe == "tencent-acp-sse"
    assert captured.get("authorization") == f"Bearer {_API_TOKEN}"
    assert "acp-connection-id" in captured


# ================================================================== T10 error model reuse


def test_t10_provider_failure_maps_to_existing_error_model() -> None:
    """T10：Provider 失败映射既有 error model，不新增错误 taxonomy。"""
    # ACPAdapter 对残缺 config 仍抛既有 AGENT_DISPATCH_FAILED（未被 WorkBuddy 改动）。
    request = AgentRequest(agent_id=1, control_plane_session_id=1, content="hi")
    with pytest.raises(AgentDispatchFailedError):
        ACPAdapter().send(request, _conn({"protocol": "tencent-acp-sse"}))

    # Provisioner 失败 → 快照不可达（无异常、无新错误码）。
    transport = _FakeTransport(status=401)
    snapshot, updated = WorkBuddyProvisioner(
        probe=_RecordingProbe(True),
        client=WorkBuddyManagementClient(transport=transport)).verify(_conn(dict(_WB_CONFIG)))
    assert snapshot.reachable is False and updated is None


# ================================================================== HTTP 端点边界


def test_verify_endpoint_503_when_not_wired(db_engine) -> None:
    client = _client(db_engine)
    assert client.post("/api/connections/1/verify").status_code == 503


def test_verify_endpoint_404_missing_connection(db_engine) -> None:
    client = _client(db_engine, verify=lambda connection: (AgentProbeSnapshot(True, None, None), None))
    assert client.post("/api/connections/99999/verify").status_code == 404


def test_health_endpoint_stays_read_only_for_workbuddy(db_engine) -> None:
    """health 对 WorkBuddy 连接仍严格只读（不 provisioning、不写库）。"""
    transport = _FakeTransport()
    probe = _RecordingProbe(False, detail="no cached runtime")
    client = _client(db_engine, probe=probe, verify=WorkBuddyProvisioner(
        probe=probe, client=WorkBuddyManagementClient(transport=transport)).verify)
    created = _create_connection(db_engine, client, dict(_WB_CONFIG))
    before = _db_config(db_engine, created["id"])

    resp = client.get(f"/api/connections/{created['id']}/health")
    assert resp.status_code == 200
    assert transport.calls == []                       # health 未触发 provisioning
    assert _db_config(db_engine, created["id"]) == before


# ================================================================== T11/T12 boundaries & regression


def test_t11_workbuddy_lives_only_in_adapter_layer() -> None:
    """T11：WorkBuddy 复杂性别停停在 Adapter 边界内——无 Core / Domain 分叉。"""
    assert (APP_DIR / "adapters" / "agents" / "workbuddy.py").is_file()
    for sub in ("application", "domain", "repositories"):
        assert not list((APP_DIR / sub).rglob("*workbuddy*")), f"{sub} 出现 WorkBuddy 分叉"

    names: set[str] = set()
    for path in APP_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                names.add(node.name)
    for banned in ("WorkBuddyAgent", "WorkBuddyRuntime", "WorkBuddyConnection",
                   "WorkBuddySession", "WorkBuddyCredential"):
        assert banned not in names, f"新增 WorkBuddy Domain 分叉类：{banned}"


def test_t12_existing_acp_bindings_unchanged() -> None:
    """T12：既有 ACP provider path（minimal / tencent-acp-sse）回归不受影响。"""
    from app.adapters.agents.acp import (
        PROTOCOL_MINIMAL,
        PROTOCOL_TENCENT_ACP_SSE,
        ACPAdapter as Adapter,
    )

    assert Path(Adapter.__module__.split(".")[-1]).name == "acp"
    # minimal 仍默认（config 无 protocol）。
    with _running_probe_server(status=200) as (server, endpoint):
        assert Adapter().probe(_conn({"endpoint": endpoint, "timeout_s": 5})).probe == PROTOCOL_MINIMAL
        assert Adapter().probe(_conn({
            "protocol": PROTOCOL_TENCENT_ACP_SSE, "endpoint": endpoint,
            "bearer_token": "t", "timeout_s": 5,
        })).probe == PROTOCOL_TENCENT_ACP_SSE
