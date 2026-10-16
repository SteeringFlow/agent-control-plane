"""Cloud-0.1.3.3 —— 真实多租户 WeCom Webhook E2E（Cross-Tenant Isolation Matrix）。

把已冻结的架构真正接起来（0.1.3.2 Gate 落地）后的**运行级验证**：

    Tenant A ─ WeCom A ─ Webhook A ─ verify ─ Channel A ─ TenantScope A
              ─ PENDING A ─ Runtime[A] ─ Agent A ─ Response ─ Delivery A
    Tenant B ─（完全对称）

验收矩阵（用户冻结清单）：
    ✓ A → A  PASS；B → B  PASS（真实 wire：SHA1 验签 + AES + XML → 全链）
    ✓ A → B / B → A  IMPOSSIBLE（签名边界 + 执行边界 + 读边界）
    ✓ Interaction / Agent / Session 不串租户
    ✓ Driver fan-out 后各自 Runtime 使用正确 UoW（Trigger 2）
    ✓ 多 tenant 同时存在时 Attempt Fence 仍成立
    ✓ 一个 tenant Runtime 异常不阻塞另一个（Gate Q5）
    ✓ Delivery 使用正确 tenant/channel/connection（fake API 收到的凭据逐租户核对）
    ✓ OSS N=1 仍保持原行为（local 渠道全链，行为 == 隔离前）

装配：生产组合根（app.main.create_app）+ 内存 UoW 替身（test_step12_6 同范式）；
出站连本地 fake WeCom API（test_step10_2.2 同范式）。不触生产库与外网。
"""

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.adapters.channels.wecom import compute_signature, encrypt_message
from app.domain.agent import Agent
from app.domain.channel import Channel
from app.domain.connection import Connection
from app.domain.connection_conversation import ConnectionConversation
from app.domain.identity import Identity
from app.domain.inbound_interaction import InboundInteraction
from app.domain.interaction_record import InteractionRecord, OutcomeCommit
from app.domain.interaction_snapshot import inbound_to_snapshot
from app.persistence.models.tenant import TenantModel
from app.domain.user import User
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

# ================================================================== fake WeCom API


class _FakeWeComServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.token_requests: list[dict] = []
        self.send_requests: list[dict] = []
        host, port = self.server_address[:2]
        self.api_base = f"http://{host}:{port}"


class _FakeWeComHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802  /cgi-bin/gettoken（接受任意租户凭据）
        srv = self.server
        q = parse_qs(urlsplit(self.path).query)
        corpid = q.get("corpid", [""])[0]
        corpsecret = q.get("corpsecret", [""])[0]
        srv.token_requests.append({"corpid": corpid, "corpsecret": corpsecret})
        self._send(200, {"errcode": 0, "errmsg": "ok",
                         "access_token": f"tok-{corpid}", "expires_in": 7200})

    def do_POST(self) -> None:  # noqa: N802  /cgi-bin/message/send
        srv = self.server
        q = parse_qs(urlsplit(self.path).query)
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send(400, {"errcode": -1, "errmsg": "bad json"})
            return
        srv.send_requests.append({"access_token": q.get("access_token", [""])[0],
                                  "payload": payload})
        self._send(200, {"errcode": 0, "errmsg": "ok", "msgid": "MSG-1"})

    def _send(self, code: int, obj) -> None:
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:  # 静音
        pass


@pytest.fixture()
def wecom_api():
    server = _FakeWeComServer(("127.0.0.1", 0), _FakeWeComHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


# ================================================================== 租户规格与播种


@dataclass
class TenantSpec:
    """一个租户的 WeCom 全套材料（入站三键 + 出站三键）——每租户互不相同。"""

    tenant_id: int
    name: str
    token: str
    aes_key: str
    corp_id: str
    corp_secret: str
    agentid: int
    from_user: str
    channel_id: int = 0
    agent_id: int = 0
    interaction_ids: list = field(default_factory=list)


TENANT_A = TenantSpec(
    tenant_id=1, name="alpha", token="token-a", aes_key="a" * 43,
    corp_id="ww_corp_alpha", corp_secret="secret-alpha",
    agentid=1000001, from_user="user_a")
TENANT_B = TenantSpec(
    tenant_id=2, name="beta", token="token-b", aes_key="b" * 43,
    corp_id="ww_corp_beta", corp_secret="secret-beta",
    agentid=1000002, from_user="user_b")


@pytest.fixture()
def prod_app(monkeypatch, wecom_api):
    """生产组合根 + 内存 UoW（test_step12_6 production_client 同范式）。

    返回 (TestClient, sessionmaker, runtime_for)。lifespan 不运行——Trigger 2
    由测试经 `runtime_for(tenant_id).driver.run_once()` 确定性驱动；
    Trigger 1 的 Worker 解析与生产同路（`runtime_for` 惰性构建）。
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base = __import__("app.persistence.database", fromlist=["Base"]).Base
    Base.metadata.create_all(engine)          # tenants #1 自动种子（after_create）
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)

    def uow_factory(**kwargs):
        return SqlAlchemyUnitOfWork(
            session_factory=factory, tenant_id=kwargs.get("tenant_id"))

    import app.main as app_main
    monkeypatch.setattr(app_main, "SqlAlchemyUnitOfWork", uow_factory)
    client = TestClient(app_main.create_app())  # lifespan 不运行：ticker 未启动

    for spec in (TENANT_A, TENANT_B):
        _seed_tenant(factory, spec, wecom_api)

    yield client, factory, client.app.state.tenant_runtime_for
    engine.dispose()


def _seed_tenant(factory, spec: TenantSpec, wecom_api) -> None:
    """租户行（bootstrap）+ 该租户的 User/Identity + WeCom channel / agent / connection。

    Identity Resolution 是 Fail-Closed（不自动创建）——外部身份必须预先
    绑定到本租户的 User（Runtime 语义，与单租户 E2E 同一播种纪律）。
    """
    with SqlAlchemyUnitOfWork(session_factory=factory, tenant_id=None) as uow:
        if uow.session.get(TenantModel, spec.tenant_id) is None:
            uow.session.add(TenantModel(
                id=spec.tenant_id, name=spec.name, status="active"))
            uow.commit()
    with SqlAlchemyUnitOfWork(session_factory=factory, tenant_id=spec.tenant_id) as uow:
        user = uow.users.create(User(name=f"owner-{spec.name}"))
        channel = uow.channels.create(Channel(
            name=f"wecom-{spec.name}", type="wecom",
            config={
                # 入站（0.1.3.1 凭据边界：Channel.config）
                "token": spec.token, "encoding_aes_key": spec.aes_key,
                "corp_id": spec.corp_id,
                # 出站（10.2.2）：每租户独立凭据 + fake API 地址
                "corpsecret": spec.corp_secret, "agentid": spec.agentid,
                "api_base": wecom_api.api_base, "timeout_s": 5.0,
            }))
        uow.identities.create(Identity(
            user_id=user.id, channel_id=channel.id, external_id=spec.from_user))
        agent = uow.agents.create(Agent(name=f"agent-{spec.name}", type="local"))
        path = uow.connections.create(Connection(
            channel_id=channel.id, agent_id=agent.id,
            name=f"path-{spec.name}", status="active", config={"timeout_s": 5.0}))
        # Implementation Package 2 / Step 2：单聊会话键 = FromUserName（授权前提）
        uow.connection_conversations.create(ConnectionConversation(
            channel_id=channel.id, external_conversation_id=spec.from_user,
            connection_id=path.id))
        uow.commit()
        spec.channel_id = channel.id
        spec.agent_id = agent.id


def _seed_local_path(factory, tenant_id: int, name: str, external_id: str) -> int:
    """某租户内的 local 渠道全链播种（User/Identity + channel/agent/connection）。"""
    with SqlAlchemyUnitOfWork(session_factory=factory, tenant_id=tenant_id) as uow:
        user = uow.users.create(User(name=f"owner-{name}"))
        channel = uow.channels.create(Channel(name=name, type="local", config={}))
        uow.identities.create(Identity(
            user_id=user.id, channel_id=channel.id, external_id=external_id))
        agent = uow.agents.create(Agent(name=f"agent-{name}", type="local"))
        path = uow.connections.create(Connection(
            channel_id=channel.id, agent_id=agent.id,
            name=f"path-{name}", status="active", config={}))
        # Implementation Package 2 / Step 2：会话键 = external_id（local 入站同形）
        uow.connection_conversations.create(ConnectionConversation(
            channel_id=channel.id, external_conversation_id=external_id,
            connection_id=path.id))
        uow.commit()
        return channel.id


def _wecom_request(spec: TenantSpec, msg_id: str, content: str):
    """构造该租户的合法 WeCom wire（SHA1 验签 + AES-CBC + XML，自建应用形态）。"""
    timestamp, nonce = "1710000000", f"nonce-{spec.tenant_id}"
    inner = (
        f"<xml><ToUserName><![CDATA[{spec.corp_id}]]></ToUserName>"
        f"<FromUserName><![CDATA[{spec.from_user}]]></FromUserName>"
        f"<CreateTime>1710000000</CreateTime>"
        f"<MsgType><![CDATA[text]]></MsgType>"
        f"<Content><![CDATA[{content}]]></Content>"
        f"<MsgId>{msg_id}</MsgId></xml>")
    encrypt = encrypt_message(inner, spec.aes_key, spec.corp_id)
    params = {
        "msg_signature": compute_signature(spec.token, timestamp, nonce, encrypt),
        "timestamp": timestamp, "nonce": nonce,
    }
    body = (f"<xml><ToUserName><![CDATA[{spec.corp_id}]]></ToUserName>"
            f"<Encrypt><![CDATA[{encrypt}]]></Encrypt></xml>").encode("utf-8")
    return params, body


def _post_wecom(client, spec: TenantSpec, msg_id: str, content: str):
    params, body = _wecom_request(spec, msg_id, content)
    return client.post(
        f"/api/channels/{spec.channel_id}/wecom/webhook",
        params=params, content=body)


def _make_pending(factory, tenant_id: int, channel_id: int,
                  external_id: str, from_user: str, content: str) -> int:
    """直接在该租户作用域内落一条 PENDING（带完整入站快照，供 Worker 重建执行）。"""
    inbound = InboundInteraction(
        channel_id=channel_id, external_interaction_id=external_id,
        external_conversation_id=from_user, external_identity_id=from_user,
        content=content)
    with SqlAlchemyUnitOfWork(session_factory=factory, tenant_id=tenant_id) as uow:
        record = uow.interactions.create(InteractionRecord(
            channel_id=channel_id, external_interaction_id=external_id,
            request_payload=inbound_to_snapshot(inbound)))
        uow.commit()
        return record.id


def _row(factory, tenant_id: int, interaction_id: int):
    with SqlAlchemyUnitOfWork(session_factory=factory, tenant_id=tenant_id) as uow:
        return uow.interactions.get_by_id(interaction_id)


# ================================================================== A→A / B→B PASS


def test_aa_and_bb_full_chain_with_correct_credentials(prod_app, wecom_api) -> None:
    """A→A PASS / B→B PASS：真实 wire 全链 + Delivery 使用正确租户凭据。"""
    client, factory, _ = prod_app
    for spec, msg_id, content in (
            (TENANT_A, "9001", "hello-from-alpha"),
            (TENANT_B, "9002", "hello-from-beta")):
        resp = _post_wecom(client, spec, msg_id, content)
        assert resp.status_code == 200 and resp.content == b""
        # 经本租户作用域读取终态
        with SqlAlchemyUnitOfWork(session_factory=factory, tenant_id=spec.tenant_id) as uow:
            found = uow.interactions.find_by_channel_and_external_id(
                spec.channel_id, msg_id)
            assert found is not None
            spec.interaction_ids.append(found.id)
            terminal = uow.interactions.get_by_id(found.id)
            assert terminal.processing_status == "succeeded"
            assert terminal.delivery_status == "delivered"
            assert terminal.response_payload["content"] == f"Echo: {content}"
        # Fake API 收到的就是该租户的凭据（Delivery 使用正确 tenant/channel）
        assert {"corpid": spec.corp_id, "corpsecret": spec.corp_secret} \
            in wecom_api.token_requests
        sends = [s for s in wecom_api.send_requests
                 if s["payload"]["touser"] == spec.from_user]
        assert len(sends) == 1
        assert sends[0]["payload"]["agentid"] == spec.agentid
        assert sends[0]["payload"]["text"]["content"] == f"Echo: {content}"
        assert sends[0]["access_token"] == f"tok-{spec.corp_id}"


# ================================================================== 交叉隔离矩阵


def test_cross_tenant_signature_rejected_a_to_b(prod_app) -> None:
    """A→B IMPOSSIBLE（认证边界）：A 的合法签名对 B 的渠道必然验签失败 → 403。"""
    client, factory, _ = prod_app
    params, body = _wecom_request(TENANT_A, "9101", "smuggled")
    resp = client.post(
        f"/api/channels/{TENANT_B.channel_id}/wecom/webhook",
        params=params, content=body)
    assert resp.status_code == 403
    with SqlAlchemyUnitOfWork(session_factory=factory, tenant_id=2) as uow:
        assert uow.interactions.find_by_channel_and_external_id(
            TENANT_B.channel_id, "9101") is None


def test_cross_tenant_execution_impossible_a_to_b(prod_app) -> None:
    """A→B IMPOSSIBLE（执行边界）：tenant A 的 Worker 无法 claim/执行 B 的 PENDING。"""
    client, factory, runtime_for = prod_app
    iid_b = _make_pending(
        factory, 2, TENANT_B.channel_id, "9201", "user_b", "for-b")
    # tenant A 的 Worker（OSS Runtime）：claim 被 tenant 过滤拒绝 → False，不执行
    assert runtime_for(1).worker.process(iid_b) is False
    row = _row(factory, 2, iid_b)
    assert row.processing_status == "pending"          # 毫发无损
    # tenant A 的读边界：B 的行在 A 作用域不可见
    assert _row(factory, 1, iid_b) is None
    # 正确的 Runtime[B] 才能执行它
    assert runtime_for(2).worker.process(iid_b) is True
    assert _row(factory, 2, iid_b).processing_status == "succeeded"


def test_interactions_agents_sessions_do_not_cross_tenants(prod_app) -> None:
    """Interaction / Identity / Session 全部落在各自租户的作用域内，互不可见。"""
    client, factory, _ = prod_app
    assert _post_wecom(client, TENANT_A, "9301", "a-msg").status_code == 200
    assert _post_wecom(client, TENANT_B, "9302", "b-msg").status_code == 200
    with SqlAlchemyUnitOfWork(session_factory=factory, tenant_id=1) as uow_a:
        a_rows = uow_a.interactions.list_recent(50)
        a_identity = uow_a.identities.find_by_channel_and_external_id(
            TENANT_A.channel_id, "user_a")
        assert a_identity is not None
        a_session = uow_a.sessions.find_active_by_identity_and_conversation(
            a_identity.id, "user_a")
        assert a_session is not None
    with SqlAlchemyUnitOfWork(session_factory=factory, tenant_id=2) as uow_b:
        b_rows = uow_b.interactions.list_recent(50)
        # A 的 identity / session 在 B 作用域不可见（跨租户读 = 找不到）
        assert uow_b.identities.find_by_channel_and_external_id(
            TENANT_A.channel_id, "user_a") is None
        assert uow_b.sessions.find_active_by_identity_and_conversation(
            a_identity.id, "user_a") is None
    a_ids = {r.id for r in a_rows}
    b_ids = {r.id for r in b_rows}
    assert a_ids and b_ids and not (a_ids & b_ids)     # 交互不串租户


# ================================================================== Driver fan-out


def test_driver_fanout_uses_correct_tenant_uow(prod_app, wecom_api) -> None:
    """Trigger 2：per-tenant Driver 各自 DISCOVER/RECOVER——只 pickup 本租户 PENDING，
    经本租户 UoW 执行（fan.start() 生命周期由 B 契约测试覆盖；此处验证数据面）。"""
    client, factory, runtime_for = prod_app
    iid_b = _make_pending(
        factory, 2, TENANT_B.channel_id, "9401", "user_b", "via-driver")
    iid_a = _make_pending(
        factory, 1, TENANT_A.channel_id, "9402", "user_a", "via-driver-a")
    # 每租户各一套忠实实例（对象相互独立），各自跑一轮
    rt_a, rt_b = runtime_for(1), runtime_for(2)
    assert rt_a is not rt_b and rt_a.driver is not rt_b.driver
    assert rt_a.driver.run_once() is True
    assert rt_b.driver.run_once() is True
    # 两条都由**各自租户的 Runtime** 执行（正确 UoW → 正确凭据出站）
    assert _row(factory, 2, iid_b).processing_status == "succeeded"
    assert _row(factory, 1, iid_a).processing_status == "succeeded"
    sends_b = [s for s in wecom_api.send_requests
               if s["payload"]["touser"] == "user_b"
               and s["payload"]["text"]["content"] == "Echo: via-driver"]
    sends_a = [s for s in wecom_api.send_requests
               if s["payload"]["touser"] == "user_a"
               and s["payload"]["text"]["content"] == "Echo: via-driver-a"]
    assert len(sends_b) == 1 and len(sends_a) == 1
    assert {"corpid": TENANT_A.corp_id, "corpsecret": TENANT_A.corp_secret} \
        in wecom_api.token_requests
    assert {"corpid": TENANT_B.corp_id, "corpsecret": TENANT_B.corp_secret} \
        in wecom_api.token_requests


def test_new_tenant_covered_by_trigger1_without_restart(prod_app) -> None:
    """运行期新开通的租户：Trigger 1（webhook/local 入站）经惰性构建的
    Runtime 立即可接纳并执行（durable Driver 于重启后补全——F3 修订）。"""
    client, factory, runtime_for = prod_app
    with SqlAlchemyUnitOfWork(session_factory=factory, tenant_id=None) as uow:
        uow.session.add(TenantModel(id=3, name="gamma", status="active"))
        uow.commit()
    channel_id = _seed_local_path(factory, 3, "local-gamma", "user_g")
    resp = client.post(
        f"/api/channels/{channel_id}/local/message",
        json={"external_id": "user_g", "text": "gamma-msg"})
    assert resp.status_code == 200
    interaction_id = resp.json()["interaction_id"]
    row = _row(factory, 3, interaction_id)
    assert row.processing_status == "succeeded"
    assert row.response_payload["content"] == "Echo: gamma-msg"
    # 该租户的 Runtime 已进入注册表（重启后 Trigger 2 即覆盖它）
    assert runtime_for(3).tenant_id == 3


# ================================================================== Attempt Fence


def test_attempt_fence_holds_across_tenants(prod_app) -> None:
    """多 tenant 并存时 Attempt Fence 仍成立：跨租户不可 claim / 不可回收 /
    即使持正确 fence 值也无法替他租户落 outcome；本租户围栏照常 COMMITTED。"""
    client, factory, runtime_for = prod_app
    iid_a = _make_pending(
        factory, 1, TENANT_A.channel_id, "9601", "user_a", "fence-msg")
    phases_a = runtime_for(1).phases
    phases_b = runtime_for(2).phases
    # 本租户 claim 成功 → 读回 DB 围栏值
    assert phases_a.claim_processing(iid_a) is True
    with SqlAlchemyUnitOfWork(session_factory=factory, tenant_id=1) as uow:
        started_at = uow.interactions.get_by_id(iid_a).processing_started_at
    # 跨租户 claim：tenant 过滤使 rowcount=0 → 拿不到执行权
    assert phases_b.claim_processing(iid_a) is False
    # 跨租户 reclaim：B 的回收语句碰不到 A 的 PROCESSING 行
    assert phases_b.reclaim_stale_processing(0) == 0
    assert _row(factory, 1, iid_a).processing_status == "processing"
    # 跨租户 record_outcome：即使 fence 值正确，tenant 过滤 → REJECTED
    # （record_failure 与 record_success 走同一条件化 UPDATE 围栏等式）
    inbound = InboundInteraction(
        channel_id=TENANT_A.channel_id, external_interaction_id="9601",
        external_conversation_id="user_a", external_identity_id="user_a",
        content="fence-msg")
    commit_b = phases_b.record_failure(
        iid_a, inbound, RuntimeError("cross-tenant"), expected_started_at=started_at)
    assert commit_b is OutcomeCommit.REJECTED
    # 本租户 + 正确围栏值 → COMMITTED（围栏等式在本租户内照常成立）
    commit_a = phases_a.record_failure(
        iid_a, inbound, RuntimeError("in-tenant"), expected_started_at=started_at)
    assert commit_a is OutcomeCommit.COMMITTED
    assert _row(factory, 1, iid_a).processing_status == "failed"


# ================================================================== 错误隔离


def test_one_tenant_runtime_failure_does_not_block_other(
        prod_app, monkeypatch) -> None:
    """Gate Q5：一个 tenant 的 Runtime 故障绝不阻塞其他租户的执行。

    per-tenant 隔离是结构性的：各租户持有相互独立的 Worker/Driver 实例
    （生产中各占独立线程）。故障注入 tenant A 后，tenant B 的入站→执行→
    交付全链照常工作。
    """
    client, factory, runtime_for = prod_app
    runtime_a = runtime_for(1)

    def _boom(interaction_id):
        raise RuntimeError("tenant-a runtime down")

    monkeypatch.setattr(runtime_a.worker, "process", _boom)
    resp = _post_wecom(client, TENANT_B, "9701", "still-works")
    assert resp.status_code == 200
    with SqlAlchemyUnitOfWork(session_factory=factory, tenant_id=2) as uow:
        found = uow.interactions.find_by_channel_and_external_id(
            TENANT_B.channel_id, "9701")
        assert found is not None
        row = uow.interactions.get_by_id(found.id)
        assert row.processing_status == "succeeded"
        assert row.delivery_status == "delivered"


# ================================================================== OSS N=1


def test_oss_single_tenant_local_path_unchanged(prod_app) -> None:
    """OSS N=1：本地渠道全链（local channel + local agent）行为 == 隔离前。"""
    client, factory, _ = prod_app
    channel_id = _seed_local_path(factory, 1, "local-oss", "u1")
    resp = client.post(
        f"/api/channels/{channel_id}/local/message",
        json={"external_id": "u1", "text": "hi-oss"})
    assert resp.status_code == 200
    interaction_id = resp.json()["interaction_id"]
    assert isinstance(interaction_id, int)
    row = _row(factory, 1, interaction_id)
    assert row.processing_status == "succeeded"
    assert row.delivery_status == "delivered"
    assert row.response_payload["content"] == "Echo: hi-oss"
