"""3B-3.3 —— WeCom Bot Group Chat（群聊 @Bot 消息接纳与字段映射）。

官方依据：企业微信开发者中心《智能机器人长连接》
https://developer.work.weixin.qq.com/document/path/101463

    群聊 @Bot 消息与单聊消息使用**同一** callback（aibot_msg_callback），
    区别仅在 body.chattype="group" 且 body.chatid 出现；官方流程保证
    群聊回调**必为 @Bot 消息**（普通群消息不触发回调，wire 亦无结构化
    mention 字段）→ ACP 不做 mention 判定（3B-3.1 Audit 裁决）。

字段映射（3B-3.3 冻结；不改 InboundInteraction 契约字段面）：

    single → external_identity_id = from.userid
             external_conversation_id = from.userid   （既有行为，不变）
    group  → external_identity_id = from.userid
             external_conversation_id = body.chatid   （新增）
    未知 chattype → acknowledge-ignore（None，不猜、不误判为群聊）

Outbound：回复关联键仍是 headers.req_id（3B-2.6 冻结不变）；req_id 在服务端
绑定回调来源会话 → 群回复自动回到**原始 chatid**，帧上无需携带 chatid，
Delivery 契约零改动。

覆盖：
    A. Adapter 映射矩阵（single / group / unknown / 群聊缺 chatid Fail Closed）
    B. 群回复路由：回复帧经原始 req_id 关联（destination = 原始群会话）
    C. 全链路 Session 验收矩阵（fake WeCom WS + 完整六段 Runtime）：
       Case A  A+G1 ×2  → 同一 Session（复用）
       Case B  A+G1 / A+G2 → 两个 Session
       Case C  A+G1 / B+G1 → 两个 Session
       + 群聊重复帧去重（一次执行 / 一次回复）

边界（刻意不覆盖）：Group Identity / Group Entity / mention 结构化信号 /
群聊主动推送（aibot_send_msg）/ Group Delivery 变体——均为 Non-goal
（3B-3 裁决）；Runtime 侧零改动（Session 键
(identity_id, connection_id, external_conversation_id) 原样承载群聊）。
"""

import json
import queue
import threading
import time

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from websockets.sync.server import serve

from app.adapters.agents.local import LocalAgentAdapter
from app.adapters.agents.registry import MultiAgentAdapter
from app.adapters.channels.registry import MultiChannelAdapter
from app.domain.inbound_interaction import (
    CHAT_TYPE_GROUP,
    CHAT_TYPE_METADATA_KEY,
    CHAT_TYPE_SINGLE,
)
from app.adapters.channels.wecom_bot import (
    BOT_REPLY_METADATA_KEY,
    CHANNEL_TYPE,
    CMD_MSG_CALLBACK,
    CMD_RESPOND_MSG,
    BotConnectionSpec,
    WeComBotAdapter,
    WeComBotConnectionRegistry,
    WeComBotTransport,
    WeComBotTransportConfig,
    connection_spec_from_channel,
    inbound_from_callback,
)
from app.application.agent_dispatch import AgentDispatchExecutor
from app.application.channel_inbound import ChannelInboundService
from app.application.coordinator import RuntimeCoordinator
from app.application.delivery import DeliveryExecutor
from app.application.identity_resolution import IdentityResolutionExecutor
from app.application.interaction_recording import PersistingCoordinator
from app.application.routing import RoutingExecutor
from app.application.session_creation import SessionCreationExecutor
from app.application.session_resolution import SessionResolutionExecutor
from app.application.access_authorization import AccessAuthorizationExecutor
from app.application.worker import Worker
from app.domain.agent import Agent
from app.domain.channel import Channel
from app.domain.connection import Connection
from app.domain.connection_conversation import ConnectionConversation
from app.domain.identity import Identity
from app.domain.outbound_response import OutboundResponse
from app.domain.user import User
from app.persistence.models.interaction import InteractionModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork
from app.repositories.channel_inbound import InboundFormatError


@pytest.fixture()
def db_engine(isolated_db_engine):
    """本模块覆写共享 `db_engine`（3B-3.3 WIP Closure B）。

    与 `test_3b_2_2` 同因：本文件的 C 组用例经 fake WeCom WS → transport 的
    **后台 exec 线程**跑完整 Runtime（含 Worker 与持久化），测试线程同时读 DB。
    共用 conftest 的 `StaticPool` 单连接会让两侧落入同一事务——
    读者脏读（假绿）、读者退出回滚写者在途事务（假红）。

    换用每条连接独立事务的文件型 SQLite，与生产 UoW 隔离语义同形。
    本改法**不**触碰轮询代码、不加 sleep、不放宽 timeout、不加重试。
    """
    yield isolated_db_engine


# ================================================================== 帧构造


def _frame(*, msgid: str, req_id: str, userid: str, chattype: str,
           chatid: str | None = None, content: str = "hello") -> dict:
    """官方形状的 aibot_msg_callback 帧（chatid 仅群聊携带，与官方一致）。"""
    body = {
        "msgid": msgid,
        "aibotid": "bot-1",
        "chattype": chattype,
        "from": {"userid": userid},
        "msgtype": "text",
        "text": {"content": content},
    }
    if chatid is not None:
        body["chatid"] = chatid
    return {"cmd": CMD_MSG_CALLBACK, "headers": {"req_id": req_id}, "body": body}


def _single(msgid="msg-s1", req_id="REQ-S1", userid="user-a") -> dict:
    return _frame(msgid=msgid, req_id=req_id, userid=userid, chattype="single")


def _group(msgid="msg-g1", req_id="REQ-G1", userid="user-a",
           chatid="CHAT-G1", content="hello") -> dict:
    return _frame(msgid=msgid, req_id=req_id, userid=userid,
                  chattype="group", chatid=chatid, content=content)


# ============================================================ A. Adapter 映射


def test_a1_single_mapping_is_unchanged() -> None:
    """single 回归：identity = userid，conversation = userid（既有行为不变）。"""
    inbound = inbound_from_callback(_single(), 9)
    assert inbound is not None
    assert inbound.external_identity_id == "user-a"
    assert inbound.external_conversation_id == "user-a"
    assert inbound.external_interaction_id == "msg-s1"
    # M6.6-C3 / ADR-12 §13：单聊显式声明 `chat_type=single`
    assert inbound.metadata == {
        BOT_REPLY_METADATA_KEY: {"req_id": "REQ-S1"},
        CHAT_TYPE_METADATA_KEY: CHAT_TYPE_SINGLE,
    }


def test_a2_group_mapping_identity_userid_conversation_chatid() -> None:
    """group 新映射：identity = from.userid，conversation = body.chatid。"""
    inbound = inbound_from_callback(_group(userid="user-a", chatid="CHAT-G1"), 9)
    assert inbound is not None
    assert inbound.external_identity_id == "user-a"          # from.userid
    assert inbound.external_conversation_id == "CHAT-G1"     # chatid（不是 userid）
    assert inbound.external_interaction_id == "msg-g1"
    assert inbound.content == "hello"
    # 回信句柄机制与单聊完全同形（req_id 唯一关联键，契约字段面不变）
    # M6.6-C3 / ADR-12 §13：群聊显式声明 `chat_type=group`（Core 据此拒绝 Arm 消费）
    assert inbound.metadata == {
        BOT_REPLY_METADATA_KEY: {"req_id": "REQ-G1"},
        CHAT_TYPE_METADATA_KEY: CHAT_TYPE_GROUP,
    }


def test_a2b_group_conversation_follows_chatid_not_sender() -> None:
    """同一群内不同发送者：conversation 仍是 chatid（不是发送者 userid）。"""
    inbound = inbound_from_callback(
        _group(userid="user-b", chatid="CHAT-G1"), 9)
    assert inbound.external_identity_id == "user-b"
    assert inbound.external_conversation_id == "CHAT-G1"


def test_a2c_group_chatid_whitespace_only_fails_closed() -> None:
    """chatid 空白 = 缺失：不回退 from.userid（绝不把群消息当单聊归属）。"""
    for bad in ("", "   "):
        frame = _group(chatid=bad)
        with pytest.raises(InboundFormatError):
            inbound_from_callback(frame, 1)


@pytest.mark.parametrize("chattype", ["unknown", "room", "", None, "Group"])
def test_a3_unknown_chattype_is_ignored(chattype) -> None:
    """未知 chattype → None（ignore；不猜、不误判为群聊、不 Fail 抛错）。"""
    body = {"msgid": "m", "aibotid": "b", "from": {"userid": "u"},
            "msgtype": "text", "text": {"content": "hi"}}
    if chattype is not None:
        body["chattype"] = chattype
    frame = {"cmd": CMD_MSG_CALLBACK, "headers": {"req_id": "r"}, "body": body}
    assert inbound_from_callback(frame, 1) is None


def test_a4_group_missing_chatid_fails_closed() -> None:
    """官方仅群聊返回 chatid：群聊帧缺 chatid → InboundFormatError（Fail Closed）。"""
    frame = _frame(msgid="m", req_id="r", userid="user-a", chattype="group")
    with pytest.raises(InboundFormatError):
        inbound_from_callback(frame, 1)


# ============================================================ B. 群回复路由


def test_b1_group_reply_is_associated_via_original_req_id() -> None:
    """群回复经**原始回调 req_id** 关联（destination = 原始群会话）。

    回复帧不携带 chatid：req_id 在服务端绑定回调来源会话（3B-2.6 冻结语义），
    故群聊回复自动回到原始 chatid——Delivery 契约零改动、无 Group 变体。
    """
    sent: list[str] = []
    registry = WeComBotConnectionRegistry()

    def sender(text: str) -> None:
        sent.append(text)
        req_id = json.loads(text)["headers"]["req_id"]
        registry.fulfil_ack(7, req_id, {"errcode": 0, "errmsg": "ok"})

    registry.register(7, sender)
    adapter = WeComBotAdapter(registry=registry, ack_wait_timeout_s=2.0)

    inbound = inbound_from_callback(_group(userid="user-a", chatid="CHAT-G1"), 7)
    assert inbound is not None
    adapter.deliver(OutboundResponse(
        channel_id=7,
        external_conversation_id=inbound.external_conversation_id,  # = chatid
        content="Echo: hello",
        metadata=inbound.metadata,                                  # = req_id 句柄
    ))

    assert len(sent) == 1
    reply = json.loads(sent[0])
    assert reply == {
        "cmd": CMD_RESPOND_MSG,
        "headers": {"req_id": "REQ-G1"},            # 原始群回调的 req_id
        "body": {"msgtype": "markdown", "markdown": {"content": "Echo: hello"}},
    }
    # 回复帧面上不出现 chatid / 群语义字段（Destination 即 req_id 绑定的来源群）
    assert "chatid" not in json.dumps(reply)


# ============================================================ C. 全链路 Session


def _uow_factory(db_engine):
    return lambda: SqlAlchemyUnitOfWork(
        session_factory=sessionmaker(bind=db_engine, expire_on_commit=False))


def _seed_group_channel(db_engine, *, ws_url: str) -> int:
    """user A / user B → identity(wecom_bot) → channel → connection → agent(local)。"""
    with _uow_factory(db_engine)() as uow:
        user_a = uow.users.create(User(name="user-a"))
        user_b = uow.users.create(User(name="user-b"))
        channel = uow.channels.create(Channel(
            name="wecom-bot-1", type=CHANNEL_TYPE,
            config={"bot_id": "bot-1", "bot_secret": "secret-1", "ws_url": ws_url}))
        agent = uow.agents.create(Agent(name="local-echo", type="local"))
        uow.identities.create(Identity(
            user_id=user_a.id, channel_id=channel.id, external_id="user-a"))
        uow.identities.create(Identity(
            user_id=user_b.id, channel_id=channel.id, external_id="user-b"))
        path = uow.connections.create(Connection(
            channel_id=channel.id, agent_id=agent.id, name="bot-path",
            status="active"))
        # Implementation Package 2 / Step 2：显式授权本文件用到的每个会话键
        #（单聊 = from.userid；群聊 = body.chatid——授权作用域是 Conversation）
        for conversation in ("user-a", "user-b", "CHAT-G1", "CHAT-G2"):
            uow.connection_conversations.create(ConnectionConversation(
                channel_id=channel.id, external_conversation_id=conversation,
                connection_id=path.id))
        uow.commit()
        return channel.id


def _build_chain(db_engine, registry):
    """完整六段 Runtime + 真实 Adapter（与组合根同构；零 Group 分支）。"""
    uow = _uow_factory(db_engine)
    channel_adapter = MultiChannelAdapter(
        {CHANNEL_TYPE: WeComBotAdapter(registry=registry)}, uow_factory=uow)
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
        delivery=DeliveryExecutor(channel_adapter=channel_adapter),
    )
    phases = PersistingCoordinator(inner=runtime, uow_factory=uow)
    worker = Worker(runtime=runtime, phases=phases, uow_factory=uow)
    service = ChannelInboundService(
        uow_factory=uow, intake=phases, inbound_adapter=channel_adapter)
    return uow, service, worker


class _FakeWeComWS:
    """假企业微信长连接服务端（与 3B-2.2 e2e 同构：订阅 / callback / 回复 ACK）。"""

    def __init__(self, callbacks: list[dict]) -> None:
        self._callbacks = callbacks
        self.subscribes: list[dict] = []
        self.replies: list[dict] = []
        self.reply_event = threading.Event()
        self._lock = threading.Lock()
        self._server = serve(self._handle, "127.0.0.1", 0)
        self.port: int = self._server.socket.getsockname()[1]

    def _handle(self, connection) -> None:
        for raw in connection:
            try:
                frame = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(frame, dict):
                continue
            cmd = frame.get("cmd")
            req_id = (frame.get("headers") or {}).get("req_id")
            if cmd == "aibot_subscribe":
                with self._lock:
                    self.subscribes.append(frame)
                connection.send(json.dumps(
                    {"headers": {"req_id": req_id}, "errcode": 0, "errmsg": "ok"}))
                for callback in self._callbacks:
                    connection.send(json.dumps(callback, ensure_ascii=False))
            elif cmd == "ping":
                connection.send(json.dumps(
                    {"headers": {"req_id": req_id}, "errcode": 0, "errmsg": "ok"}))
            elif cmd == CMD_RESPOND_MSG:
                with self._lock:
                    self.replies.append(frame)
                connection.send(json.dumps(
                    {"headers": {"req_id": req_id}, "errcode": 0, "errmsg": "ok"}))
                self.reply_event.set()

    def start(self) -> None:
        threading.Thread(
            target=self._server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self._server.shutdown()


_TERMINAL = ("succeeded", "failed")


def _wait_terminal(uow, channel_id: int, external_id: str, timeout: float = 20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with uow() as u:
            record = u.interactions.find_by_channel_and_external_id(
                channel_id, external_id)
        if record is not None and record.processing_status in _TERMINAL:
            return record
        time.sleep(0.05)
    raise AssertionError(f"interaction 未落终态：{external_id}")


def _session_id(uow, channel_id: int, external_id: str):
    with uow() as u:
        record = u.interactions.find_by_channel_and_external_id(
            channel_id, external_id)
    assert record is not None, external_id
    return record.session_id


def _run_group_bot(db_engine, callbacks: list[dict]):
    fake = _FakeWeComWS(callbacks=callbacks)
    fake.start()
    channel_id = _seed_group_channel(db_engine, ws_url=f"ws://127.0.0.1:{fake.port}")
    registry = WeComBotConnectionRegistry()
    uow, service, worker = _build_chain(db_engine, registry)
    with uow() as u:
        spec = connection_spec_from_channel(u.channels.get_by_id(channel_id))

    def on_frame(binding, raw: str) -> None:
        acceptance = service.accept_message(
            binding.channel_id, signature="", timestamp="", nonce="", body=raw)
        if acceptance.interaction_id is not None:
            worker.process(acceptance.interaction_id)

    transport = WeComBotTransport(
        registry=registry, binding_source=lambda: [spec], on_frame=on_frame,
        config=WeComBotTransportConfig(reconnect_backoff_s=0.2, recv_timeout_s=0.2))
    transport.start()
    return fake, transport, uow, channel_id


def test_c1_group_session_matrix_case_a_reuse(db_engine) -> None:
    """Case A：A + G1 ×2 → 同一 Session（复用既有 active session）。"""
    fake, transport, uow, channel_id = _run_group_bot(db_engine, [
        _group(msgid="m1", req_id="REQ-1", userid="user-a", chatid="CHAT-G1"),
        _group(msgid="m2", req_id="REQ-2", userid="user-a", chatid="CHAT-G1"),
    ])
    try:
        assert fake.reply_event.wait(20)
        _wait_terminal(uow, channel_id, "m1")
        _wait_terminal(uow, channel_id, "m2")
        s1 = _session_id(uow, channel_id, "m1")
        s2 = _session_id(uow, channel_id, "m2")
        assert s1 is not None
        assert s1 == s2                       # 同一 Session
    finally:
        transport.stop()
        fake.stop()


def test_c2_group_session_matrix_case_b_different_group(db_engine) -> None:
    """Case B：A+G1 与 A+G2 → 两个 Session（conversation 隔离）。"""
    fake, transport, uow, channel_id = _run_group_bot(db_engine, [
        _group(msgid="m1", req_id="REQ-1", userid="user-a", chatid="CHAT-G1"),
        _group(msgid="m3", req_id="REQ-3", userid="user-a", chatid="CHAT-G2"),
    ])
    try:
        assert fake.reply_event.wait(20)
        _wait_terminal(uow, channel_id, "m1")
        _wait_terminal(uow, channel_id, "m3")
        s1 = _session_id(uow, channel_id, "m1")
        s3 = _session_id(uow, channel_id, "m3")
        assert s1 is not None and s3 is not None
        assert s1 != s3                       # 两个 Session
    finally:
        transport.stop()
        fake.stop()


def test_c3_group_session_matrix_case_c_different_user(db_engine) -> None:
    """Case C：A+G1 与 B+G1 → 两个 Session（identity 隔离，同群不共享）。"""
    fake, transport, uow, channel_id = _run_group_bot(db_engine, [
        _group(msgid="m1", req_id="REQ-1", userid="user-a", chatid="CHAT-G1"),
        _group(msgid="m4", req_id="REQ-4", userid="user-b", chatid="CHAT-G1"),
    ])
    try:
        assert fake.reply_event.wait(20)
        _wait_terminal(uow, channel_id, "m1")
        _wait_terminal(uow, channel_id, "m4")
        s1 = _session_id(uow, channel_id, "m1")
        s4 = _session_id(uow, channel_id, "m4")
        assert s1 is not None and s4 is not None
        assert s1 != s4                       # 两个 Session
    finally:
        transport.stop()
        fake.stop()


def test_c4_group_end_to_end_reply_and_duplicate_dedup(db_engine) -> None:
    """全链路群聊闭环 + 重复帧去重：

    A@G1（m1）→ A@G1 重复帧（同 msgid）→ A@G2（m3）→ B@G1（m4）：
      - 4 条回复帧，req_id 逐条透传原始回调值（群回复回到来源群）；
      - m1 重复帧只执行一次 / 只回复一次（既有状态感知去重，群聊同形）；
      - Session 矩阵：A/B/C 三态同时成立。
    """
    fake, transport, uow, channel_id = _run_group_bot(db_engine, [
        _group(msgid="m1", req_id="REQ-1", userid="user-a", chatid="CHAT-G1"),
        _group(msgid="m1", req_id="REQ-1", userid="user-a", chatid="CHAT-G1"),  # 重复
        _group(msgid="m2", req_id="REQ-2", userid="user-a", chatid="CHAT-G1"),
        _group(msgid="m3", req_id="REQ-3", userid="user-a", chatid="CHAT-G2"),
        _group(msgid="m4", req_id="REQ-4", userid="user-b", chatid="CHAT-G1"),
    ])
    try:
        assert fake.reply_event.wait(20)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and len(fake.replies) < 4:
            time.sleep(0.05)
        # 1) 去重：4 条不同 msgid → 恰好 4 条回复（重复 m1 不产生第 5 条）
        assert len(fake.replies) == 4, fake.replies
        with Session(db_engine) as session:
            total = session.execute(
                select(func.count()).select_from(InteractionModel)).scalar_one()
        assert total == 4
        # 2) 群回复路由：req_id 透传原始回调值（帧面无 chatid）
        assert sorted(r["headers"]["req_id"] for r in fake.replies) \
            == ["REQ-1", "REQ-2", "REQ-3", "REQ-4"]
        assert all("chatid" not in json.dumps(r) for r in fake.replies)
        # 3) Session 矩阵（Case A / B / C）
        s1 = _session_id(uow, channel_id, "m1")
        s2 = _session_id(uow, channel_id, "m2")
        s3 = _session_id(uow, channel_id, "m3")
        s4 = _session_id(uow, channel_id, "m4")
        assert s1 == s2                       # Case A：同 user + 同群 → 复用
        assert s1 != s3                       # Case B：同 user + 异群 → 隔离
        assert s1 != s4                       # Case C：异 user + 同群 → 隔离
        assert s3 != s4
        # 4) 终态健康
        record = _wait_terminal(uow, channel_id, "m4")
        assert record.processing_status == "succeeded"
        assert record.delivery_status == "delivered"
    finally:
        transport.stop()
        fake.stop()


def test_c5_bot_connection_spec_unchanged_for_group() -> None:
    """连接材料与 chattype 无关（同一 bot 连接同时承载单聊与群聊回调）。"""
    spec = BotConnectionSpec(channel_id=9, bot_id="bot-1", bot_secret="s")
    assert spec.ws_url  # 形状守卫：3B-3.3 未触碰连接建立面
