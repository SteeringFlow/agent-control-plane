"""M6.6-C3-CP2 Implementation —— Inbound Trust Gate Hardening（Route Matrix + Source Guards）。

上游文档：

    docs/M6_6_C3_CP2_INBOUND_TRUST_GATE_AUDIT.md   只读审计（发现 3 条未认证注入面）
    docs/M6_6_C3_CP2_IMPLEMENTATION.md             本步实现报告

被守护的不变量（CP-2）：

    段 1 · Trust Gate（路由信任模型）必须位于 `_load_channel` 之后、
    `parse_message` 之前；未通过时：

        → HTTP 403（`InboundSignatureError`，不新造错误码）
        → **不产生 Interaction**
        → **不产生 Identity**
        → **不产生 Grant**
        → **不进入任何未来 Bootstrap**（即使 Arm 有效 —— 见 §E）

本文件**不实现** Bootstrap / Arm / First Message Runtime Semantics（属 CP-3）。
§E 的「Arm exists」只是**测试局部的静态语境** + 可观测计数断言，不引入生产 Bootstrap。

12 格矩阵（4 入口 × 3 类型）的单一事实源 = 下方 `ROUTE_MATRIX`；
§A/§B/§C 三个用例族都从它派生，§D 的源码 Guard 再与它交叉校验
（声明集合 == 矩阵中「accepted」的 HTTP 类型集合）。
"""

import ast
import json
from pathlib import Path
from typing import NamedTuple

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.agents.local import LocalAgentAdapter
from app.adapters.agents.registry import MultiAgentAdapter
from app.adapters.channels.local import LocalChannelAdapter
from app.adapters.channels.registry import MultiChannelAdapter
from app.adapters.channels.wecom import (
    WeComAdapter,
    compute_signature,
    encrypt_message,
)
from app.adapters.channels.wecom_bot import (
    CHANNEL_TYPE as WECOM_BOT_CHANNEL_TYPE,
    WeComBotAdapter,
    WeComBotConnectionRegistry,
)
from app.api.runtime.channels import (
    LOCAL_MESSAGE_CHANNEL_TYPE,
    WECOM_WEBHOOK_CHANNEL_TYPE,
    build_channel_inbound_api,
)
from app.application.access_authorization import AccessAuthorizationExecutor
from app.application.agent_dispatch import AgentDispatchExecutor
from app.application.channel_inbound import ChannelInboundService
from app.application.coordinator import RuntimeCoordinator
from app.application.delivery import DeliveryExecutor
from app.application.identity_resolution import IdentityResolutionExecutor
from app.application.interaction_recording import PersistingCoordinator
from app.application.routing import RoutingExecutor
from app.application.session_creation import SessionCreationExecutor
from app.application.session_resolution import SessionResolutionExecutor
from app.application.worker import Worker
from app.domain.agent import Agent
from app.domain.channel import Channel
from app.domain.connection import Connection
from app.domain.connection_conversation import ConnectionConversation
from app.domain.identity import Identity
from app.domain.outbound_response import OutboundResponse
from app.domain.user import User
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork
from app.repositories.channel_inbound import (
    InboundFormatError,
    InboundSignatureError,
)

# ---------------------------------------------------------------- wire 常量

TOKEN = "cp2-webhook-token"
AES_KEY = "c" * 43
CORP_ID = "wwcorp-cp2"
TIMESTAMP = "1409659813"
NONCE = "nonce-cp2"
BOT_ID = "bot-cp2"
BOT_SECRET = "secret-cp2"

EXTERNAL_ID = "zhangsan"          # 单聊：conversation == identity（三渠道同形）
LOCAL_MSG_ID = "cp2-local-1"

POST_WECOM_WEBHOOK = "POST /{id}/wecom/webhook"
GET_WECOM_WEBHOOK = "GET /{id}/wecom/webhook"
POST_LOCAL_MESSAGE = "POST /{id}/local/message"
WS_BOT_CALLBACK = "WS aibot_msg_callback"

# 结果分类（矩阵每一格必须落其一）：
#   accepted       跨过两段信任，产生 Interaction
#   trust_gate     段 1 拒绝：403，parse_message **未运行**，零 Interaction
#   adapter_reject 段 1 通过但段 2（Adapter）拒绝：parse_message 运行、零 Interaction
#   unreachable    结构不可达（入口不可能被驱动到该类型）
ACCEPTED = "accepted"
TRUST_GATE = "trust_gate"
ADAPTER_REJECT = "adapter_reject"
UNREACHABLE = "unreachable"

#: 「该渠道正处于 Bootstrap Arm 窗口内」的**测试局部**静态语境（§E 用）。
#: 本轮不实现 Arm（CP-3），故生产代码不读取本集合——它只用来把
#: 「即使 Arm 有效，Trust Gate 也必须先拒绝」这条顺序不变量钉成可执行断言。
ARMED_CHANNELS: set[int] = set()


class _Cell(NamedTuple):
    """矩阵一格：入口 × 渠道类型 → 期望结局。"""

    entry: str
    channel_type: str
    outcome: str
    http_status: int | None
    parse_runs: bool
    interaction_created: bool


#: 4 入口 × 3 渠道类型 = 12 格（CP-2 §Route Matrix 的机器化形式）。
ROUTE_MATRIX: tuple[_Cell, ...] = (
    # ---- GET /{id}/wecom/webhook（WeCom URL 握手；路由声明 = wecom）----
    _Cell(GET_WECOM_WEBHOOK, "wecom", ACCEPTED, 200, False, False),
    _Cell(GET_WECOM_WEBHOOK, "wecom_bot", TRUST_GATE, 403, False, False),
    _Cell(GET_WECOM_WEBHOOK, "local", TRUST_GATE, 403, False, False),
    # ---- POST /{id}/wecom/webhook（WeCom 消息回调；路由声明 = wecom）----
    _Cell(POST_WECOM_WEBHOOK, "wecom", ACCEPTED, 200, True, True),
    _Cell(POST_WECOM_WEBHOOK, "wecom_bot", TRUST_GATE, 403, False, False),
    _Cell(POST_WECOM_WEBHOOK, "local", TRUST_GATE, 403, False, False),
    # ---- POST /{id}/local/message（本地开发渠道；路由声明 = local）----
    _Cell(POST_LOCAL_MESSAGE, "local", ACCEPTED, 200, True, True),
    _Cell(POST_LOCAL_MESSAGE, "wecom", TRUST_GATE, 403, False, False),
    _Cell(POST_LOCAL_MESSAGE, "wecom_bot", TRUST_GATE, 403, False, False),
    # ---- WS aibot_msg_callback（长连接；不属 HTTP，不声明 expected_channel_type）----
    _Cell(WS_BOT_CALLBACK, "wecom_bot", ACCEPTED, None, True, True),
    _Cell(WS_BOT_CALLBACK, "wecom", ADAPTER_REJECT, None, True, False),
    _Cell(WS_BOT_CALLBACK, "local", UNREACHABLE, None, False, False),
)

_GET_CELLS = tuple(c for c in ROUTE_MATRIX if c.entry == GET_WECOM_WEBHOOK)
_POST_CELLS = tuple(c for c in ROUTE_MATRIX if c.entry in (POST_WECOM_WEBHOOK,
                                                           POST_LOCAL_MESSAGE))
_WS_CELLS = tuple(c for c in ROUTE_MATRIX if c.entry == WS_BOT_CALLBACK)

_CHANNEL_CONFIG: dict[str, dict] = {
    "wecom": {"token": TOKEN, "encoding_aes_key": AES_KEY, "corp_id": CORP_ID},
    "wecom_bot": {"bot_id": BOT_ID, "bot_secret": BOT_SECRET},
    "local": {},
}

BACKEND = Path(__file__).resolve().parents[1]


# ==================================================================== 装配


def _uow_factory(db_engine):
    return lambda: SqlAlchemyUnitOfWork(
        session_factory=sessionmaker(bind=db_engine, expire_on_commit=False))


def _counts(db_engine) -> dict[str, int]:
    """三张**授权/事实**表的计数（表名取自 4.2 冻结集合）。

    这三项构成本步的核心安全断言：未通过 Trust Gate 的请求必须使三者
    **增量全部为 0**（尤其 identities / connection_conversations = 「授权写入」）。
    """
    with Session(db_engine) as session:
        return {
            "interactions": session.execute(
                text("select count(*) from interactions")).scalar_one(),
            "identities": session.execute(
                text("select count(*) from identities")).scalar_one(),
            "grants": session.execute(
                text("select count(*) from connection_conversations")).scalar_one(),
        }


class _SpyInboundAdapter:
    """包住真实 `MultiChannelAdapter`，记录**入站两个能力是否被调用**。

    「parse_message 是否运行」是本步的关键可观测点：Trust Gate 若被误移到
    Adapter 之后（或漏掉），本 spy 会立刻显示它被调用。
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self.parse_calls: list[str] = []
        self.handshake_calls: list[str] = []

    def verify_handshake(self, channel, *, signature, timestamp, nonce, echostr):
        self.handshake_calls.append(channel.type)
        return self._inner.verify_handshake(
            channel, signature=signature, timestamp=timestamp,
            nonce=nonce, echostr=echostr)

    def parse_message(self, channel, *, signature, timestamp, nonce, body):
        self.parse_calls.append(channel.type)
        return self._inner.parse_message(
            channel, signature=signature, timestamp=timestamp,
            nonce=nonce, body=body)


class _InboundRealDeliverSink:
    """入站用**真实** Adapter（信任闸门必须真实），出站改为本地 sink。

    这样测试既不依赖网络（wecom 会 POST `response_url`/`message/send`）、
    也不写本地 outbox 文件，同时保持「入站 wire 逻辑 = 生产实现」。
    """

    def __init__(self, inbound, sink: list[OutboundResponse]) -> None:
        self._inbound = inbound
        self._sink = sink

    def verify_handshake(self, channel, *, signature, timestamp, nonce, echostr):
        return self._inbound.verify_handshake(
            channel, signature=signature, timestamp=timestamp,
            nonce=nonce, echostr=echostr)

    def parse_message(self, channel, *, signature, timestamp, nonce, body):
        return self._inbound.parse_message(
            channel, signature=signature, timestamp=timestamp,
            nonce=nonce, body=body)

    def deliver(self, outbound: OutboundResponse) -> None:
        self._sink.append(outbound)


class _Chain(NamedTuple):
    client: TestClient
    spy: _SpyInboundAdapter
    sink: list[OutboundResponse]
    service: ChannelInboundService
    worker: Worker


def _seed(db_engine, channel_type: str) -> int:
    """三种渠道各建一条**完整可成功**的链（Identity + Grant + Connection + Agent）。

    刻意把授权与身份都预先建好：这样「若 Trust Gate 被绕过」就**必然**产生一条
    真实的 Interaction（而不是因缺授权而偶然失败）——负例因此是**真负例**。
    """
    with _uow_factory(db_engine)() as uow:
        user = uow.users.create(User(name=f"user-{channel_type}"))
        channel = uow.channels.create(Channel(
            name=f"cp2-{channel_type}", type=channel_type,
            config=dict(_CHANNEL_CONFIG[channel_type])))
        agent = uow.agents.create(Agent(name="cp2-echo", type="local"))
        uow.identities.create(Identity(
            user_id=user.id, channel_id=channel.id, external_id=EXTERNAL_ID))
        path = uow.connections.create(Connection(
            channel_id=channel.id, agent_id=agent.id, name="cp2-path",
            status="active"))
        uow.connection_conversations.create(ConnectionConversation(
            channel_id=channel.id, external_conversation_id=EXTERNAL_ID,
            connection_id=path.id))
        uow.commit()
        return channel.id


def _build_chain(db_engine) -> _Chain:
    """完整六段 Runtime + 三个真实入站 Adapter + 真实分派器。"""
    uow = _uow_factory(db_engine)
    sink: list[OutboundResponse] = []
    registry = WeComBotConnectionRegistry()
    real = MultiChannelAdapter(
        {
            "wecom": _InboundRealDeliverSink(WeComAdapter(), sink),
            "wecom_bot": _InboundRealDeliverSink(
                WeComBotAdapter(registry=registry), sink),
            "local": _InboundRealDeliverSink(LocalChannelAdapter(), sink),
        },
        uow_factory=uow,
    )
    spy = _SpyInboundAdapter(real)
    agent_adapter = MultiAgentAdapter(
        {"local": LocalAgentAdapter()}, uow_factory=uow)
    runtime = RuntimeCoordinator(
        identity_resolution=IdentityResolutionExecutor(uow),
        access_authorization=AccessAuthorizationExecutor(uow),
        routing=RoutingExecutor(uow),
        session_resolution=SessionResolutionExecutor(uow),
        session_creation=SessionCreationExecutor(uow),
        agent_dispatch=AgentDispatchExecutor(
            agent_adapter=agent_adapter, uow_factory=uow),
        delivery=DeliveryExecutor(channel_adapter=real),
    )
    phases = PersistingCoordinator(inner=runtime, uow_factory=uow)
    worker = Worker(runtime=runtime, phases=phases, uow_factory=uow)
    service = ChannelInboundService(
        uow_factory=uow, intake=phases, inbound_adapter=spy)
    app = FastAPI()
    app.include_router(build_channel_inbound_api(service, worker))
    return _Chain(TestClient(app), spy, sink, service, worker)


# ---------------------------------------------------------------- wire 构造


def _wecom_text_xml(msg_id: str = "cp2-1001") -> str:
    return (
        f"<xml><ToUserName><![CDATA[{CORP_ID}]]></ToUserName>"
        f"<FromUserName><![CDATA[{EXTERNAL_ID}]]></FromUserName>"
        "<CreateTime>1348831860</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        "<Content><![CDATA[hello from wecom]]></Content>"
        f"<MsgId>{msg_id}</MsgId>"
        "<AgentID>1</AgentID></xml>"
    )


def _signed_wecom_post(msg_id: str = "cp2-1001") -> dict:
    """合法 WeCom 密文信封 + 正确 `msg_signature`（段 2 可过）。"""
    encrypt = encrypt_message(_wecom_text_xml(msg_id), AES_KEY, CORP_ID)
    body = (
        f"<xml><ToUserName><![CDATA[{CORP_ID}]]></ToUserName>"
        f"<Encrypt><![CDATA[{encrypt}]]></Encrypt>"
        "<AgentID><![CDATA[1]]></AgentID></xml>"
    )
    return {
        "params": {
            "msg_signature": compute_signature(TOKEN, TIMESTAMP, NONCE, encrypt),
            "timestamp": TIMESTAMP,
            "nonce": NONCE,
        },
        "content": body.encode("utf-8"),
    }


def _signed_wecom_handshake() -> dict:
    encrypt = encrypt_message("echo-cp2", AES_KEY, CORP_ID)
    return {
        "msg_signature": compute_signature(TOKEN, TIMESTAMP, NONCE, encrypt),
        "timestamp": TIMESTAMP,
        "nonce": NONCE,
        "echostr": encrypt,
    }


def _bot_callback_frame(msg_id: str = "cp2-bot-1", *,
                        content: str = "hello from bot") -> str:
    """合法 `aibot_msg_callback` 帧（**无签名**——长连接协议如此）。"""
    return json.dumps({
        "cmd": "aibot_msg_callback",
        "headers": {"req_id": f"REQ-{msg_id}"},
        "body": {
            "msgid": msg_id,
            "from": {"userid": EXTERNAL_ID},
            "msgtype": "text",
            "chattype": "single",
            "text": {"content": content},
        },
    }, ensure_ascii=False)


# ==================================================================== §A GET 格


@pytest.mark.parametrize("cell", _GET_CELLS, ids=lambda c: f"{c.channel_type}")
def test_a_get_wecom_webhook_matrix(db_engine, cell: _Cell) -> None:
    """GET /{id}/wecom/webhook 只服务 `wecom`；其它类型 → 403 且未进 Adapter。"""
    channel_id = _seed(db_engine, cell.channel_type)
    chain = _build_chain(db_engine)
    before = _counts(db_engine)

    resp = chain.client.get(
        f"/api/channels/{channel_id}/wecom/webhook",
        params=_signed_wecom_handshake())

    assert resp.status_code == cell.http_status
    after = _counts(db_engine)
    assert after == before                                  # 握手从不产生 Interaction
    if cell.outcome == ACCEPTED:
        assert resp.text == "echo-cp2"                      # 既有 WECom 握手语义不变
        assert chain.spy.handshake_calls == ["wecom"]
        assert chain.spy.parse_calls == []
    else:
        assert chain.spy.handshake_calls == []              # Trust Gate 在 Adapter 之前
        assert chain.spy.parse_calls == []


# ==================================================================== §B POST 格


@pytest.mark.parametrize(
    "cell", _POST_CELLS, ids=lambda c: f"{c.entry.split()[1]}-{c.channel_type}")
def test_b_post_routes_matrix(db_engine, cell: _Cell) -> None:
    """两个 POST 入口 × 3 类型：只有「入口 == 渠道信任模型」时产生 Interaction。"""
    channel_id = _seed(db_engine, cell.channel_type)
    chain = _build_chain(db_engine)
    before = _counts(db_engine)

    if cell.entry == POST_WECOM_WEBHOOK:
        req = _signed_wecom_post()
        resp = chain.client.post(
            f"/api/channels/{channel_id}/wecom/webhook",
            params=req["params"], content=req["content"])
    else:
        resp = chain.client.post(
            f"/api/channels/{channel_id}/local/message",
            json={"external_id": EXTERNAL_ID, "text": "hello from local",
                  "message_id": LOCAL_MSG_ID})

    after = _counts(db_engine)
    assert resp.status_code == cell.http_status

    if cell.outcome == ACCEPTED:
        assert chain.spy.parse_calls == [cell.channel_type]
        assert after["interactions"] == before["interactions"] + 1
        assert len(chain.sink) == 1                          # 全链真实跑通（Agent+Delivery）
        assert after["identities"] == before["identities"]    # 入站不建身份（ADR-5）
        assert after["grants"] == before["grants"]            # 入站不写授权（ADR-6）
    else:
        # ★ 核心负例：段 1 拒绝 → Adapter 未被调用 → 零 Interaction / 零授权写入
        assert chain.spy.parse_calls == []
        assert after == before
        assert chain.sink == []


# ==================================================================== §C WS 格


@pytest.mark.parametrize("cell", _WS_CELLS, ids=lambda c: f"{c.channel_type}")
def test_c_ws_bot_callback_matrix(db_engine, cell: _Cell) -> None:
    """WS 长连接：`wecom_bot` 合法行为保持；误绑定到别的类型一律 Fail Closed。

    WS 入口不传 `expected_channel_type`（它不是 HTTP 入口，且本轮修改边界不含
    `main.py`）。因此本族的断言分两种机制：

        wecom_bot     → 真实跑通（既有 3B-2.2 行为不变）
        wecom         → 段 2（Adapter 验签/信封）拒绝
        local         → **结构不可达**：`discover_bot_connections` 只枚举
                        `type == wecom_bot`（§D-5 源码 Guard 锁定）
    """
    channel_id = _seed(db_engine, cell.channel_type)
    chain = _build_chain(db_engine)
    before = _counts(db_engine)

    if cell.outcome == UNREACHABLE:
        # 不驱动：入口在结构上无法持有该类型的 binding（由 §D-5 保证）。
        assert cell.parse_runs is False
        assert _counts(db_engine) == before
        return

    frame = _bot_callback_frame(msg_id=f"cp2-bot-{cell.channel_type}")
    if cell.outcome == ADAPTER_REJECT:
        # 与组合根同形的调用：非 HTTP 入口不声明 expected_channel_type
        with pytest.raises((InboundSignatureError, InboundFormatError)):
            chain.service.accept_message(
                channel_id, signature="", timestamp="", nonce="", body=frame)
        assert chain.spy.parse_calls == ["wecom"]            # 段 2 拒绝（非段 1）
        assert _counts(db_engine) == before
        return

    acceptance = chain.service.accept_message(
        channel_id, signature="", timestamp="", nonce="", body=frame)
    assert acceptance.interaction_id is not None
    chain.worker.process(acceptance.interaction_id)

    after = _counts(db_engine)
    assert chain.spy.parse_calls == ["wecom_bot"]
    assert after["interactions"] == before["interactions"] + 1
    assert len(chain.sink) == 1                              # ACK / response path 未被破坏
    assert after["identities"] == before["identities"]
    assert after["grants"] == before["grants"]


# ============================================================ §D 源码 Guard


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _imports(tree: ast.Module) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
    return found


def _code_literals(tree: ast.Module) -> list[str]:
    """模块内**非 docstring** 的字符串字面量。

    为什么排除 docstring：本步刻意在注释/文档里写出被禁止的形态名
    （例如「不得把 HTTP 路由继续交给 `MultiChannelAdapter` 按类型分派」）。
    守卫要判定的是**可执行代码**，不是文档措辞。
    """
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef,
                             ast.AsyncFunctionDef, ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings.add(doc)
    return [
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
        and n.value not in docstrings
    ]


def _identifiers(tree: ast.Module) -> set[str]:
    """可执行代码里出现的全部标识符（Name / Attribute / 导入名）。"""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.alias):
            found.add(node.name.split(".")[-1])
            if node.asname:
                found.add(node.asname)
    return found


def _find_func(tree: ast.Module, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"未找到函数：{name}")


def _only_call(node, *, attr: str) -> ast.Call:
    calls = [
        n for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == attr
    ]
    assert len(calls) == 1, f"期望恰有 1 处 {attr}() 调用，实际 {len(calls)}"
    return calls[0]


ROUTER_PATH = BACKEND / "app" / "api" / "runtime" / "channels.py"
INBOUND_PATH = BACKEND / "app" / "application" / "channel_inbound.py"
MAIN_PATH = BACKEND / "app" / "main.py"

INBOUND_SRC = INBOUND_PATH.read_text(encoding="utf-8")
MAIN_SRC = MAIN_PATH.read_text(encoding="utf-8")


def _declared_route_channel_types() -> dict[str, str]:
    """路由模块顶层 `*_CHANNEL_TYPE = "..."` 声明（本步的信任模型登记表）。"""
    declared: dict[str, str] = {}
    for node in _tree(ROUTER_PATH).body:
        if (isinstance(node, ast.Assign)
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id.endswith("_CHANNEL_TYPE")
                and isinstance(node.value, ast.Constant)):
            declared[node.targets[0].id] = node.value.value
    return declared


def test_d1_every_http_route_declares_expected_channel_type() -> None:
    """D-1：每条 HTTP 入站路由**必须显式声明**它服务的渠道类型。

    这是「不要让 HTTP route 继续用 channel.type → generic adapter lookup 充当
    Trust Gate」的机器化形式：任何新增/改动路由若忘记声明，本用例即红。
    """
    tree = _tree(ROUTER_PATH)
    service_calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and isinstance(n.func.value, ast.Name) and n.func.value.id == "service"
    ]
    assert service_calls, "路由模块未调用任何入站用例（结构已变，需复核本 Guard）"
    for call in service_calls:
        keyword = {k.arg: k.value for k in call.keywords}
        assert "expected_channel_type" in keyword, (
            f"service.{call.func.attr}() 未声明 expected_channel_type"
            f"（line {call.lineno}）")
        value = keyword["expected_channel_type"]
        assert isinstance(value, ast.Name) and value.id.endswith("_CHANNEL_TYPE"), (
            f"expected_channel_type 必须绑定到路由层常量（line {call.lineno}）")

    # 矩阵中「accepted 的 HTTP 类型集合」必须与源码声明集合**完全一致**
    assert set(_declared_route_channel_types().values()) == {
        c.channel_type for c in _GET_CELLS + _POST_CELLS if c.outcome == ACCEPTED
    }


def test_d2_http_ingress_never_serves_wecom_bot() -> None:
    """D-2：任何 HTTP 入站路由都不得声明 `wecom_bot`（唯一合法入口 = 活连接）。

    同时断言路由模块**没有** `wecom_bot` 的字符串字面量（docstring/注释除外）
    ——即「把 wecom_bot 的 HTTP 入口登记回来」这件事需要**显式**改动本步的
    常量表，无法悄悄发生。
    """
    declared = _declared_route_channel_types()
    assert declared, "未发现任何路由层渠道类型声明"
    assert WECOM_BOT_CHANNEL_TYPE not in declared.values()
    assert WECOM_WEBHOOK_CHANNEL_TYPE != WECOM_BOT_CHANNEL_TYPE
    assert LOCAL_MESSAGE_CHANNEL_TYPE != WECOM_BOT_CHANNEL_TYPE

    tree = _tree(ROUTER_PATH)
    literals = _code_literals(tree)
    assert not [lit for lit in literals if WECOM_BOT_CHANNEL_TYPE in lit], (
        "路由模块出现 wecom_bot 字面量：HTTP 入站不得承载该类型")

    # 路由数量 = 矩阵中的 HTTP 入口数（3）：新增入口必须显式登记并更新本 Guard
    routes = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and isinstance(n.func.value, ast.Name) and n.func.value.id == "router"
        and n.func.attr in {"get", "post", "put", "patch", "delete"}
    ]
    assert len(routes) == 3, f"HTTP 入站路由数量变化（{len(routes)}），需复核矩阵与声明"


def test_d3_trust_gate_runs_between_load_and_parse() -> None:
    """D-3：顺序不变量 —— `_load_channel` → Trust Gate → `parse_message`。"""
    tree = _tree(INBOUND_PATH)
    for func_name, adapter_call in (("accept_message", "parse_message"),
                                    ("handle_handshake", "verify_handshake")):
        func = _find_func(tree, func_name)
        gate = _only_call(func, attr="_assert_route_trust")
        load = _only_call(func, attr="_load_channel")
        adapter = _only_call(func, attr=adapter_call)
        assert load.lineno < gate.lineno < adapter.lineno, (
            f"{func_name}：期望 _load_channel → _assert_route_trust → {adapter_call}"
            f"，实际 {load.lineno} / {gate.lineno} / {adapter.lineno}")


def test_d4_trust_gate_is_type_only_and_dependency_free() -> None:
    """D-4：Trust Gate 只做类型比对，不依赖 Agent / Identity / Grant / 授权 / Router。

    若未来有人把授权写入塞进这一段（或让 Gate 去查 Identity/Grant），本用例即红。
    """
    tree = _tree(INBOUND_PATH)
    forbidden = {
        "app.application.coordinator",
        "app.application.identity_resolution",
        "app.application.access_authorization",
        "app.application.routing",
        "app.application.agent_dispatch",
    }
    imports = _imports(tree)
    assert not (imports & forbidden), f"入站用例引入了运行期依赖：{imports & forbidden}"

    gate = _find_func(tree, "_assert_route_trust")
    attrs = {
        n.attr for n in ast.walk(gate) if isinstance(n, ast.Attribute)
    }
    # 允许：channel.type（判定依据）与 channel.id（错误文案里的定位信息）。
    # 禁止：任何其它属性访问 —— 尤其不得出现身份/授权/运行期对象。
    assert attrs <= {"type", "id"}, (
        f"_assert_route_trust 只应访问 channel.type / channel.id，实际：{attrs}")
    assert "type" in attrs, "Trust Gate 丢失了 channel.type 判定"

    # 可执行代码不得出现身份/授权写入面或 Bootstrap 符号（AST 判定，docstring 除外）
    identifiers = _identifiers(tree)
    for token in ("create_identity", "create_grant", "connection_conversations",
                  "bootstrap", "ensure_bootstrap", "identities"):
        assert token not in identifiers, f"入站用例出现越界符号：{token}"


def test_d5_ws_entry_unchanged_and_type_filtered() -> None:
    """D-5：WS 入口保持原样（不声明类型），且 discovery 只枚举 `wecom_bot`。

    后者是矩阵「WS × local」= 结构不可达 的依据。
    """
    tree = _tree(MAIN_PATH)
    on_frame = _find_func(tree, "_on_bot_frame")
    accept = _only_call(on_frame, attr="accept_message")
    keywords = {k.arg for k in accept.keywords}
    assert "expected_channel_type" not in keywords, (
        "WS 入口不得声明 expected_channel_type（非 HTTP 入口；本轮修改边界不含 main.py）")

    discover = _find_func(tree, "discover_bot_connections")
    segment = ast.get_source_segment(MAIN_SRC, discover) or ""
    assert "WECOM_BOT_CHANNEL_TYPE" in segment, "discovery 未按渠道类型过滤"
    assert any(isinstance(n, ast.Compare)
               and any(isinstance(op, ast.NotEq) for op in n.ops)
               for n in ast.walk(discover)), "discovery 缺少 type != wecom_bot 过滤"
    assert "binding_source=discover_bot_connections" in MAIN_SRC, (
        "transport 的 binding_source 已变化，需复核本 Guard")


def test_d6_router_does_not_lookup_adapter_by_channel_type() -> None:
    """D-6：路由模块不得自行按 `channel.type` 查 Adapter（根因防复发）。

    判定基于 AST（标识符 + 非 docstring 字面量），因此在 docstring 里
    **解释**为什么不能这么做是允许的，而真正引入这些符号会立刻失败。
    """
    tree = _tree(ROUTER_PATH)
    identifiers = _identifiers(tree)
    assert "MultiChannelAdapter" not in identifiers, "路由模块不得持有渠道分派器"
    assert "_for_channel_type" not in identifiers, "路由模块不得按类型查 Adapter"
    assert not [lit for lit in _code_literals(tree)
                if "MultiChannelAdapter" in lit or "_for_channel_type" in lit]
    channel_type_reads = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Attribute) and n.attr == "type"
        and isinstance(n.value, ast.Name) and n.value.id == "channel"
    ]
    assert not channel_type_reads, (
        "路由模块读到了 channel.type：信任模型判定必须由常量 + 用例层完成，"
        "不得由路由按类型自行分派")


# ===================================================== §E Trust Gate 先于 Bootstrap


def test_e1_trust_gate_rejects_even_when_arm_context_exists(db_engine) -> None:
    """E-1：**顺序不变量** —— Trust Gate 是 Bootstrap 的**前提**（即使 Arm 有效）。

    本轮不实现 Arm（CP-3）。本用例用**测试局部**的静态语境
    （`ARMED_CHANNELS`，生产代码不读取）模拟「该渠道正处于 arm 窗口内」，
    然后断言**可观测事实**：

        wecom_bot 经 HTTP POST → 403
                               → Interaction 增量 0
                               → Identity  增量 0
                               → Grant     增量 0

    即：未来 CP-3 的 Arm 只要挂在「通过 Trust Gate 的入站」之后（CP-2 §9.1
    两段式），就无法被这条请求触发。若有人把授权写入插到 Trust Gate 之前，
    本用例会红。
    """
    channel_id = _seed(db_engine, "wecom_bot")
    ARMED_CHANNELS.add(channel_id)          # 「Arm 有效」（测试局部语境）
    try:
        chain = _build_chain(db_engine)
        before = _counts(db_engine)
        frame = _bot_callback_frame(msg_id="cp2-arm-bypass")

        # 两条 HTTP 入口都试一遍（两个方向的旁路）
        resp_webhook = chain.client.post(
            f"/api/channels/{channel_id}/wecom/webhook",
            params={"msg_signature": "", "timestamp": "", "nonce": ""},
            content=frame.encode("utf-8"))
        resp_local = chain.client.post(
            f"/api/channels/{channel_id}/local/message",
            content=frame.encode("utf-8"))

        assert resp_webhook.status_code == 403
        assert resp_local.status_code == 403
        assert chain.spy.parse_calls == []          # 段 1 在 Adapter 之前拒绝
        after = _counts(db_engine)
        assert after == before
        assert after["identities"] == before["identities"]   # 不产生 Identity
        assert after["grants"] == before["grants"]           # 不产生 Grant
        assert after["interactions"] == before["interactions"]  # 不产生 Interaction
    finally:
        ARMED_CHANNELS.discard(channel_id)


def test_e2_gate_rejection_uses_existing_error_code_only(db_engine) -> None:
    """E-2：拒绝走既有错误码（`InboundSignatureError` → 403），不新造错误码。"""
    channel_id = _seed(db_engine, "wecom_bot")
    chain = _build_chain(db_engine)
    with pytest.raises(InboundSignatureError) as excinfo:
        chain.service.accept_message(
            channel_id, signature="", timestamp="", nonce="",
            body=_bot_callback_frame(),
            expected_channel_type=WECOM_WEBHOOK_CHANNEL_TYPE)
    assert "不经本入站入口接入" in str(excinfo.value)

    # 未声明类型（非 HTTP 入口）→ 不施加段 1，行为与隔离前一致
    acceptance = chain.service.accept_message(
        channel_id, signature="", timestamp="", nonce="",
        body=_bot_callback_frame(msg_id="cp2-no-route-type"))
    assert acceptance.interaction_id is not None
