"""M6.6-F3 —— Provider-specific Verify（WeCom）实现验收。

范围（F3-1..F3-8）：
- Provider dispatch：workbuddy → WorkBuddyProvisioner；wecom → WeCom verifier；
  其它 → ACPAdapter.probe（既有回退，行为不变）；
- WeCom credential verification：真实调用 `/cgi-bin/gettoken` 判定 reachable；
- Fail closed；No credential leakage；Read-only（updated_config is None）；
- WorkBuddy regression；API/UI 契约不变；tenant isolation。

测试原则：最小、直接针对 F3 语义；不新建 framework；不改既有测试 contract。
真实 WeCom 平台 E2E 需真实 corp_id/corpsecret → 本仓库用**本地替身服务器**验证
「真实 HTTP gettoken 路径」的可达/失败分支（不伪造真实平台结论）。
"""

from __future__ import annotations

import contextlib
import http.server
import itertools
import json
import socketserver
import threading
import urllib.parse
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
from app.domain.agent_probe_snapshot import AgentProbeSnapshot
from app.domain.connection import Connection
from app.persistence.models.connection import ConnectionModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

BACKEND = Path(__file__).resolve().parents[1]

_CORP_SECRET = "corpsecret-super-secret-value"
_CORP_ID = "corp-xyz"
_ACCESS_TOKEN = "access-token-xyz"
_VERIFY_FIELDS = {"connection_id", "reachable", "checked_at", "detail", "probe"}


# ================================================================== harness


def _factory(db_engine) -> sessionmaker:
    return sessionmaker(bind=db_engine, class_=Session, expire_on_commit=False)


def _uow_factory(db_engine, tenant_id: int | None = None):
    factory = _factory(db_engine)
    if tenant_id is None:
        return lambda: SqlAlchemyUnitOfWork(session_factory=factory)
    return lambda: SqlAlchemyUnitOfWork(session_factory=factory, tenant_id=tenant_id)


def _service(db_engine) -> ConnectionApplicationService:
    return ConnectionApplicationService(uow_factory=_uow_factory(db_engine))


def _channel_loader(db_engine, tenant_id: int | None = None):
    uow_factory = _uow_factory(db_engine, tenant_id)

    def load(channel_id: int):
        with uow_factory() as uow:
            return uow.channels.get_by_id(channel_id)

    return load


_seq = itertools.count(1)


def _seed(db_engine, *, channel_type: str = "wecom", channel_config=None,
          connection_config=None, channel_name: str | None = None,
          tenant_id: int | None = None):
    uow_factory = _uow_factory(db_engine, tenant_id)
    n = next(_seq)
    channel = ChannelApplicationService(uow_factory=uow_factory).create_channel(
        CreateChannel(name=channel_name or f"ch-{channel_type}-{n}", type=channel_type,
                      config=channel_config))
    agent = AgentApplicationService(uow_factory=uow_factory).create_agent(
        CreateAgent(name=f"agent-{n}", type="acp"))
    connection = ConnectionApplicationService(uow_factory=uow_factory).create_connection(
        CreateConnection(channel_id=channel.id, agent_id=agent.id, name="binding",
                         config=connection_config))
    return channel, agent, connection


def _client(db_engine, *, service=None, probe=None, verify=None,
            verify_provider=None) -> TestClient:
    app = FastAPI()
    app.include_router(build_connections_api(
        service if service is not None else _service(db_engine),
        probe=probe, verify=verify, verify_provider=verify_provider))
    return TestClient(app)


class _SpyProbe:
    """记录收到的 Connection，返回可配置的 ACP 探针结论。"""

    def __init__(self, reachable: bool = True, detail=None, probe: str = "minimal") -> None:
        self._snap = AgentProbeSnapshot(reachable, detail, probe)
        self.calls: list[Connection] = []

    def __call__(self, connection: Connection) -> AgentProbeSnapshot:
        self.calls.append(connection)
        return self._snap


class _SpyWorkBuddy:
    """记录收到的 Connection，返回可配置 (snapshot, updated_config)。"""

    def __init__(self, result=None) -> None:
        self.result = result or (AgentProbeSnapshot(True, None, "tencent-acp-sse"), {"x": 1})
        self.calls: list[Connection] = []

    def __call__(self, connection: Connection):
        self.calls.append(connection)
        return self.result


class _SpyWeCom:
    """记录收到的 Channel，返回可配置的 (snapshot)。"""

    def __init__(self, snap=None) -> None:
        self.snap = snap or AgentProbeSnapshot(True, None, "wecom")
        self.calls: list = []

    def __call__(self, channel):
        self.calls.append(channel)
        return self.snap


def _dispatcher(*, probe=None, workbuddy=None, wecom=None, loader=None):
    return build_connection_verifier(
        probe=probe or _SpyProbe(),
        workbuddy_verify=workbuddy or _SpyWorkBuddy(),
        wecom_verify=wecom or _SpyWeCom(),
        channel_loader=loader or (lambda cid: None),
    )


def _wecom_config(base_url: str, **over) -> dict:
    config = {
        "corp_id": _CORP_ID,
        "corpsecret": _CORP_SECRET,
        "agentid": 1000002,
        "api_base": base_url,
        "timeout_s": 5,
    }
    config.update(over)
    return config


# ---------------------------------------------------------------- gettoken 替身服务器


class _GetTokenHandler(http.server.BaseHTTPRequestHandler):
    """记录请求 path；按 server.status / server.body 应答（不触真实 WeCom）。"""

    def do_GET(self):  # noqa: N802 (http.server 约定)
        self.server.captured_paths.append(self.path)
        self.send_response(self.server.status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(self.server.body)

    def log_message(self, *args):  # 静音（不写 stderr）
        return


class _GetTokenServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


@contextlib.contextmanager
def _running_gettoken_server(*, status: int = 200, body: bytes | None = None):
    payload = body if body is not None else json.dumps(
        {"errcode": 0, "access_token": _ACCESS_TOKEN}).encode("utf-8")
    server = _GetTokenServer(("127.0.0.1", 0), _GetTokenHandler)
    server.status = status
    server.body = payload
    server.captured_paths = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


class _CorpGetTokenHandler(http.server.BaseHTTPRequestHandler):
    """按 query 里的 corpid 决定有效/无效——用于「同服务、不同 Connection 凭据」隔离测试。"""

    def do_GET(self):  # noqa: N802
        self.server.captured_paths.append(self.path)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        corp_id = (query.get("corpid") or [""])[0]
        if corp_id == self.server.valid_corp_id:
            payload = json.dumps({"errcode": 0, "access_token": _ACCESS_TOKEN}).encode()
        else:
            payload = json.dumps({"errcode": 40001, "errmsg": "invalid"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # 静音
        return


@contextlib.contextmanager
def _running_corp_gettoken_server(*, valid_corp_id: str):
    server = _GetTokenServer(("127.0.0.1", 0), _CorpGetTokenHandler)
    server.valid_corp_id = valid_corp_id
    server.captured_paths = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _db_config(db_engine, connection_id: int) -> dict:
    with Session(db_engine) as session:
        row = session.get(ConnectionModel, connection_id)
        session.expunge(row)
        return row.config


class _FakeManagementTransport:
    """WorkBuddy Management API 确定性桩（T6 回归：不触网）。"""

    _RESPONSE = json.dumps({
        "data": {"id": "rt-1", "links": {"acpLink": {
            "url": "https://runtime.example/acp", "token": "acp-link-token"}}},
    }).encode("utf-8")

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, method, url, headers, body, timeout_s):
        self.calls.append({"method": method, "url": url, "headers": dict(headers)})
        return 200, self._RESPONSE


# ================================================================== T1 valid → reachable


def test_t1_wecom_valid_credential_reachable(db_engine) -> None:
    """T1：合法 corp_id/corpsecret → gettoken errcode=0 → reachable=True（probe=wecom）。"""
    wecom = _SpyWeCom(snap=AgentProbeSnapshot(True, None, "wecom"))
    dispatcher = _dispatcher(
        probe=_SpyProbe(), workbuddy=_SpyWorkBuddy(), wecom=wecom,
        loader=_channel_loader(db_engine))
    with _running_gettoken_server() as (server, base):
        _, _, connection = _seed(db_engine, channel_config=_wecom_config(base))
        snapshot, updated = dispatcher(connection)
    assert snapshot.reachable is True and updated is None
    assert len(wecom.calls) == 1 and wecom.calls[0].type == "wecom"


def test_t1_end_to_end_http_valid(db_engine) -> None:
    """T1（端到端）：真实 WeComAdapter.verify_connection 打本地 gettoken 替身 → 200 可达。"""
    with _running_gettoken_server() as (server, base):
        _seed(db_engine, channel_config=_wecom_config(base))
        dispatcher = _dispatcher(
            probe=_SpyProbe(), loader=_channel_loader(db_engine),
            wecom=WeComAdapter().verify_connection)
        client = _client(db_engine, verify_provider=lambda: dispatcher)
        resp = client.post("/api/connections/1/verify")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == _VERIFY_FIELDS           # 契约面不变
    assert body["reachable"] is True
    assert body["detail"] is None
    assert body["probe"] == "wecom"
    assert [_CORP_ID in p or "gettoken" in p for p in server.captured_paths] == [True]


# ================================================================== T2 invalid → sanitized


def test_t2_wecom_invalid_credential_unreachable_sanitized(db_engine) -> None:
    """T2：errcode!=0 → reachable=False；detail 泛化；response 不含 corpsecret / provider 原文。"""
    invalid = json.dumps({"errcode": 40001, "errmsg": "invalid credential"}).encode("utf-8")
    with _running_gettoken_server(status=200, body=invalid) as (server, base):
        _seed(db_engine, channel_config=_wecom_config(base))
        dispatcher = _dispatcher(
            probe=_SpyProbe(), loader=_channel_loader(db_engine),
            wecom=WeComAdapter().verify_connection)
        client = _client(db_engine, verify_provider=lambda: dispatcher)
        resp = client.post("/api/connections/1/verify")
    assert resp.status_code == 200
    body = resp.json()
    assert body["reachable"] is False
    assert body["detail"] == "wecom connection could not be verified"
    # 不泄漏：corpsecret / access_token / provider 原文 / errcode 均不得出现
    for leaked in (_CORP_SECRET, _ACCESS_TOKEN, "40001", "invalid credential"):
        assert leaked not in resp.text


def test_t2_adapter_never_leaks_on_invalid(db_engine) -> None:
    """T2（Adapter 级）：detail 为泛化文案，且不含任何凭据/状态码。"""
    invalid = json.dumps({"errcode": 40001, "errmsg": "invalid"}).encode("utf-8")
    with _running_gettoken_server(status=200, body=invalid) as (_server, base):
        _, _, connection = _seed(db_engine, channel_config=_wecom_config(base))
        snap = WeComAdapter().verify_connection(
            _load_channel(db_engine, connection.channel_id))
    assert snap.reachable is False
    assert snap.detail == "wecom connection could not be verified"
    assert _CORP_SECRET not in (snap.detail or "")


def _load_channel(db_engine, channel_id: int):
    with _uow_factory(db_engine)() as uow:
        return uow.channels.get_by_id(channel_id)


# ================================================================== T3 fail closed


@pytest.mark.parametrize("status,body", [
    (500, None),                     # provider 5xx
    (200, b"<html>not json</html>"),  # 畸形响应
    (200, json.dumps({"errcode": 40013, "errmsg": "invalid corpid"}).encode("utf-8")),
    (200, json.dumps({"errcode": 0}).encode("utf-8")),  # 缺 access_token
])
def test_t3_wecom_provider_failures_fail_closed(db_engine, status, body) -> None:
    """T3：provider 5xx / 畸形响应 / errcode!=0 / 缺 token → 一律 reachable=False、无泄漏。"""
    with _running_gettoken_server(status=status, body=body) as (server, base):
        _, _, connection = _seed(db_engine, channel_config=_wecom_config(base))
        snap = WeComAdapter().verify_connection(
            _load_channel(db_engine, connection.channel_id))
    assert snap.reachable is False
    assert snap.detail == "wecom connection could not be verified"
    assert _CORP_SECRET not in (snap.detail or "")


def test_t3_network_failure_fail_closed(db_engine) -> None:
    """T3：网络不可达（端口关闭）→ reachable=False；异常原文（可能含 URL）不得进 detail。"""
    _, _, connection = _seed(
        db_engine, channel_config=_wecom_config("http://127.0.0.1:1"))
    snap = WeComAdapter().verify_connection(
        _load_channel(db_engine, connection.channel_id))
    assert snap.reachable is False
    assert snap.detail == "wecom connection could not be verified"
    assert _CORP_SECRET not in (snap.detail or "")


def test_t3_missing_credential_fail_closed(db_engine) -> None:
    """T3：缺 corpsecret → 不触网、reachable=False、泛化 detail。"""
    _, _, connection = _seed(db_engine, channel_config={
        "corp_id": _CORP_ID, "agentid": 1000002})  # 无 corpsecret
    snap = WeComAdapter().verify_connection(
        _load_channel(db_engine, connection.channel_id))
    assert snap.reachable is False
    assert snap.detail == "wecom connection could not be verified"


def test_t3_adapter_never_raises_on_garbage_channel(db_engine) -> None:
    """T3：非法输入（非 Channel / 非 dict config）绝不抛异常，一律 Fail Closed。"""
    adapter = WeComAdapter()
    assert adapter.verify_connection(object()).reachable is False
    assert adapter.verify_connection(None).reachable is False


# ================================================================== T4 dispatch correctness


def test_t4_dispatch_workbuddy(db_engine) -> None:
    """T4：provider=="workbuddy" → WorkBuddyProvisioner（不解析 Channel、不打 ACP 探针）。"""
    wecom, probe = _SpyWeCom(), _SpyProbe()
    wb = _SpyWorkBuddy()
    dispatcher = _dispatcher(
        probe=probe, workbuddy=wb, wecom=wecom, loader=_channel_loader(db_engine))
    connection = Connection(channel_id=1, agent_id=1, name="c", id=1,
                            config={"provider": "workbuddy", "api_key": "k"})
    snapshot, updated = dispatcher(connection)
    assert len(wb.calls) == 1 and wb.calls[0] is connection
    assert wecom.calls == [] and probe.calls == []
    assert updated == {"x": 1}


def test_t4_dispatch_wecom_by_channel_type(db_engine) -> None:
    """T4：Channel.type=="wecom" → WeCom verifier（收到的是解析出的 Channel）。"""
    channel, _, connection = _seed(db_engine, channel_config=_wecom_config("http://127.0.0.1:1"))
    wecom, probe, wb = _SpyWeCom(), _SpyProbe(), _SpyWorkBuddy()
    dispatcher = _dispatcher(
        probe=probe, workbuddy=wb, wecom=wecom, loader=_channel_loader(db_engine))
    snapshot, updated = dispatcher(connection)
    assert len(wecom.calls) == 1 and wecom.calls[0].id == channel.id
    assert probe.calls == [] and wb.calls == []
    assert updated is None


def test_t4_dispatch_unknown_provider_falls_back(db_engine) -> None:
    """T4：非 wecom 渠道 → ACPAdapter.probe 回退（wecom verifier 不再被调用）。"""
    _seed(db_engine, channel_type="local")
    wecom, probe = _SpyWeCom(), _SpyProbe()
    dispatcher = _dispatcher(
        probe=probe, workbuddy=_SpyWorkBuddy(), wecom=wecom,
        loader=_channel_loader(db_engine))
    snapshot, updated = dispatcher(_conn_for(db_engine))
    assert len(probe.calls) == 1
    assert wecom.calls == []
    assert updated is None


def _conn_for(db_engine) -> Connection:
    with _uow_factory(db_engine)() as uow:
        return uow.connections.list_connections()[0]


# ================================================================== T5 read-only


def test_t5_wecom_verify_is_read_only(db_engine) -> None:
    """T5：WeCom Verify 无写入（updated_config is None；DB 行不变）；Channel 亦不变。"""
    with _running_gettoken_server() as (_server, base):
        channel, _, connection = _seed(db_engine, channel_config=_wecom_config(base))
        before = _db_config(db_engine, connection.id)
        with _uow_factory(db_engine)() as uow:
            ch_before = uow.channels.get_by_id(channel.id)
        dispatcher = _dispatcher(
            probe=_SpyProbe(), loader=_channel_loader(db_engine),
            wecom=WeComAdapter().verify_connection)
        client = _client(db_engine, verify_provider=lambda: dispatcher)
        resp = client.post(f"/api/connections/{connection.id}/verify")
        after = _db_config(db_engine, connection.id)
        with _uow_factory(db_engine)() as uow:
            ch_after = uow.channels.get_by_id(channel.id)
    assert resp.status_code == 200 and resp.json()["reachable"] is True
    assert after == before                    # Connection.config 未被写入
    assert (ch_after.status, ch_after.config) == (ch_before.status, ch_before.config)


def test_t5_dispatcher_wecom_returns_none_config(db_engine) -> None:
    """T5（分派层）：WeCom 分支恒 updated_config=None（即使 verifier 想返回新配置也无）。"""
    channel, _, connection = _seed(db_engine, channel_config=_wecom_config("http://127.0.0.1:1"))
    dispatcher = _dispatcher(
        probe=_SpyProbe(), loader=_channel_loader(db_engine),
        wecom=lambda ch: AgentProbeSnapshot(True, None, "wecom"))
    _snap, updated = dispatcher(connection)
    assert updated is None


# ================================================================== T6 WorkBuddy regression


def test_t6_workbuddy_regression_through_dispatch(db_engine) -> None:
    """T6：WorkBuddy Verify 经分派后行为不变（lazy provisioning + 写回 + 不泄漏）。"""
    transport = _FakeManagementTransport()
    provisioner = WorkBuddyProvisioner(
        probe=lambda c: AgentProbeSnapshot(True, None, "tencent-acp-sse"),
        client=WorkBuddyManagementClient(transport=transport))
    dispatcher = _dispatcher(
        probe=_SpyProbe(), workbuddy=provisioner.verify,
        loader=_channel_loader(db_engine))
    _, _, connection = _seed(db_engine, connection_config={
        "provider": "workbuddy", "workbuddy_agent_id": "agent-9", "api_key": "wb-secret-key"})
    client = _client(db_engine, verify_provider=lambda: dispatcher)
    resp = client.post(f"/api/connections/{connection.id}/verify")

    assert resp.status_code == 200 and resp.json()["reachable"] is True
    assert len(transport.calls) == 1                     # lazy provisioning 仍发生
    stored = _db_config(db_engine, connection.id)
    assert stored["runtime_id"] == "rt-1"                # acpLink 缓存仍写回
    assert stored["endpoint"] == "https://runtime.example/acp"
    assert "wb-secret-key" not in resp.text              # 不泄漏
    assert "acp-link-token" not in resp.text


def test_t6_workbuddy_redaction_unchanged() -> None:
    """T6：WorkBuddy 既有脱敏语义不变（api_key / bearer_token 读时脱敏）。"""
    from app.schemas.connections import sanitize_connection_config
    out = sanitize_connection_config({
        "provider": "workbuddy", "api_key": "wb-secret", "bearer_token": "acp-token",
        "endpoint": "https://runtime.example/acp", "runtime_id": "rt-1"})
    assert out["api_key"] == "********"
    assert out["bearer_token"] == "********"
    assert out["endpoint"] == "https://runtime.example/acp"
    assert out["runtime_id"] == "rt-1"


# ================================================================== T7 fallback regression


def test_t7_unknown_falls_back_to_acp_probe_end_to_end(db_engine) -> None:
    """T7：无 provider-specific verifier 的 Connection → 既有 ACPAdapter.probe 行为不变。"""
    _seed(db_engine, channel_type="local")  # 非 wecom
    dispatcher = _dispatcher(
        probe=ACPAdapter().probe,            # 真实 ACP 探针
        loader=_channel_loader(db_engine),
        wecom=lambda ch: AgentProbeSnapshot(True, None, "wecom"))
    client = _client(db_engine, verify_provider=lambda: dispatcher)
    resp = client.post("/api/connections/1/verify")
    assert resp.status_code == 200
    body = resp.json()
    assert body["reachable"] is False                    # 缺 endpoint（既有 Fail-Closed 语义）
    assert body["probe"] == "minimal"


def test_t7_channel_unresolvable_falls_back(db_engine) -> None:
    """T7：Channel 解析不到（None）→ 回退 ACP 探针（F3-8），Verify 不炸。"""
    probe, wecom = _SpyProbe(), _SpyWeCom()
    dispatcher = _dispatcher(probe=probe, wecom=wecom, loader=lambda cid: None)
    connection = Connection(channel_id=99999, agent_id=1, name="c", id=1)
    _snap, updated = dispatcher(connection)
    assert len(probe.calls) == 1 and wecom.calls == [] and updated is None


def test_t7_loader_error_falls_back(db_engine) -> None:
    """T7：Channel 解析抛错 → 回退 ACP 探针（Fail Closed，Verify 绝不 500）。"""

    def _boom(channel_id):
        raise RuntimeError("db down")

    probe, wecom = _SpyProbe(), _SpyWeCom()
    dispatcher = _dispatcher(probe=probe, wecom=wecom, loader=_boom)
    connection = Connection(channel_id=1, agent_id=1, name="c", id=1)
    _snap, updated = dispatcher(connection)
    assert len(probe.calls) == 1 and wecom.calls == [] and updated is None


# ================================================================== T8 tenant isolation


def test_t8_cross_tenant_channel_not_resolved(db_engine) -> None:
    """T8：tenant B 的 Connection 不得解析到 tenant A 的 Channel（→ 回退探针）。"""
    _seed(db_engine, channel_config=_wecom_config("http://127.0.0.1:1"), tenant_id=1)
    connection_t1 = _conn_for(db_engine)  # 仅用于取 channel_id
    # 同一 channel_id，但 loader 绑定 tenant 2 → 必须解析不到
    probe, wecom = _SpyProbe(), _SpyWeCom()
    dispatcher = _dispatcher(
        probe=probe, wecom=wecom, loader=_channel_loader(db_engine, tenant_id=2))
    connection = Connection(channel_id=connection_t1.channel_id, agent_id=1, name="c", id=1)
    _snap, updated = dispatcher(connection)
    assert wecom.calls == []          # 跨租户未解析到 A 的 Channel
    assert len(probe.calls) == 1      # 回退
    assert updated is None


def test_t8_own_tenant_channel_resolved(db_engine) -> None:
    """T8（对照）：tenant 1 loader 能解析到本租户的 wecom Channel → 走 WeCom verifier。"""
    channel, _, _ = _seed(
        db_engine, channel_config=_wecom_config("http://127.0.0.1:1"), tenant_id=1)
    probe, wecom = _SpyProbe(), _SpyWeCom()
    dispatcher = _dispatcher(
        probe=probe, wecom=wecom, loader=_channel_loader(db_engine, tenant_id=1))
    connection = Connection(channel_id=channel.id, agent_id=1, name="c", id=1)
    _snap, updated = dispatcher(connection)
    assert len(wecom.calls) == 1 and probe.calls == [] and updated is None


# ================================================================== 契约 / 边界守卫


def test_verify_endpoint_503_when_not_wired(db_engine) -> None:
    """既有纪律：未装配任何 verifier → 503（HTTP 契约不变）。"""
    client = _client(db_engine)
    assert client.post("/api/connections/1/verify").status_code == 503


def test_verify_endpoint_404_missing_connection(db_engine) -> None:
    """既有纪律：Connection 不存在 → 404。"""
    dispatcher = _dispatcher(loader=_channel_loader(db_engine))
    client = _client(db_engine, verify_provider=lambda: dispatcher)
    assert client.post("/api/connections/99999/verify").status_code == 404


def test_t9_provider_isolation_across_connections(db_engine) -> None:
    """T8+（用户要求）：跨连接隔离——WeCom A(valid) / WeCom B(invalid) /
    WorkBuddy C(valid) 按 Connection/provider 独立执行，互不影响。"""
    with _running_corp_gettoken_server(valid_corp_id="corp-ok") as (_server, base):
        _seed(db_engine, channel_name="ch-a",
              channel_config=_wecom_config(base, corp_id="corp-ok"))
        _seed(db_engine, channel_name="ch-b",
              channel_config=_wecom_config(base, corp_id="corp-bad",
                                           corpsecret="bad-secret"))
        _seed(db_engine, channel_name="ch-c", channel_type="local",
              connection_config={"provider": "workbuddy",
                                 "workbuddy_agent_id": "agent-9", "api_key": "k"})
        provisioner = WorkBuddyProvisioner(
            probe=lambda c: AgentProbeSnapshot(True, None, "tencent-acp-sse"),
            client=WorkBuddyManagementClient(transport=_FakeManagementTransport()))
        dispatcher = _dispatcher(
            probe=_SpyProbe(), workbuddy=provisioner.verify,
            wecom=WeComAdapter.verify_connection, loader=_channel_loader(db_engine))
        client = _client(db_engine, verify_provider=lambda: dispatcher)
        a = client.post("/api/connections/1/verify").json()
        b = client.post("/api/connections/2/verify").json()
        c = client.post("/api/connections/3/verify").json()

    assert a["reachable"] is True and a["probe"] == "wecom"
    assert b["reachable"] is False
    assert b["detail"] == "wecom connection could not be verified"
    assert c["reachable"] is True and c["probe"] == "tencent-acp-sse"
    assert "bad-secret" not in json.dumps(b)     # B 的内容不泄漏、也不影响 A / C
