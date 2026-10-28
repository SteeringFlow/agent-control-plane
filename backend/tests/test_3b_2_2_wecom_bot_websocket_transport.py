"""3B-2.2 / 3B-2.4 —— WeCom Bot WebSocket Transport（wire 已按官方文档校准）。

官方依据：企业微信开发者中心《智能机器人长连接》
https://developer.work.weixin.qq.com/document/path/101463

    帧骨架（所有帧同构） {"cmd": ..., "headers": {"req_id": ...}, "body": {...}}
    响应帧无 cmd      {"headers": {"req_id": ...}, "errcode": 0, "errmsg": "ok"}

覆盖：
    A. wire 纯函数（订阅 / 心跳 / 回复帧 / 帧解析 / 回调归一化 / Fail Closed）
       + **req_id 只作 transport metadata，不成为 Domain 字段**
    B. 连接注册表：注册 / 取用 / 「旧连接清理不误杀新连接」
    C. WeComBotAdapter：入站（归属校验）/ 出站（Fail Closed 与成功）
    D. 策略常量：Channel type 白名单含 wecom_bot；bot_secret 读时脱敏
    E. **全链路**：fake WeCom WS Server（真实 127.0.0.1 WebSocket）
         → aibot_subscribe(+订阅响应校验) → aibot_msg_callback → InboundInteraction
         → PENDING → Worker → Agent → Delivery → aibot_respond_msg（req_id 透传）
         → fake WS 收到回复
    F. 重推去重：同一 msgid 两帧 → 只执行一次、只回复一次
    G. 3B-2.4 校准点：订阅被拒 / 订阅响应 req_id 不匹配 / 应用层心跳

边界（本切片刻意不覆盖）：群聊（3B-3.3 起由
test_3b_3_3_wecom_bot_group_chat.py 覆盖）/ 真实企业微信联调 / 多副本连接路由 /
delivery 重投 / 流式回复。
"""

import dataclasses
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
    CHAT_TYPE_METADATA_KEY,
    CHAT_TYPE_SINGLE,
)
from app.adapters.channels.wecom_bot import (
    BOT_REPLY_METADATA_KEY,
    CHANNEL_TYPE,
    CMD_MSG_CALLBACK,
    CMD_PING,
    CMD_RESPOND_MSG,
    CMD_SUBSCRIBE,
    DEFAULT_WS_URL,
    BotConnectionSpec,
    WeComBotAdapter,
    WeComBotConnectionRegistry,
    WeComBotSubscribeError,
    WeComBotTransport,
    WeComBotTransportConfig,
    compose_ping_frame,
    compose_respond_frame,
    compose_subscribe_frame,
    connection_spec_from_channel,
    frame_req_id,
    inbound_from_callback,
    parse_frame,
    reply_context_from,
)
from app.application.agent_dispatch import AgentDispatchExecutor
from app.application.channel_inbound import ChannelInboundService
from app.application.channels import (
    ALLOWED_CHANNEL_TYPES,
    ChannelApplicationService,
    CreateChannel,
)
from app.application.coordinator import RuntimeCoordinator
from app.application.delivery import DeliveryExecutor
from app.application.errors import ChannelDeliveryFailedError
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
from app.domain.inbound_interaction import InboundInteraction
from app.domain.interaction_record import (
    DELIVERY_DELIVERED,
    DELIVERY_FAILED,
    PROCESSING_FAILED,
    PROCESSING_PENDING,
    PROCESSING_SUCCEEDED,
)
from app.domain.interaction_snapshot import inbound_from_snapshot, inbound_to_snapshot
from app.domain.outbound_response import OutboundResponse
from app.domain.user import User
from app.persistence.models.interaction import InteractionModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork
from app.repositories.channel_inbound import (
    InboundChannelConfigError,
    InboundFormatError,
)
from app.schemas.channels import sanitize_channel_config


@pytest.fixture()
def db_engine(isolated_db_engine):
    """本模块覆写共享 `db_engine`（3B-3.3 WIP Closure B）。

    `WeComBotTransport` 把 `on_frame`（内含 Worker + 全七段 Runtime 与持久化）
    放到**后台 exec 线程**，而测试线程同时用 `with uow()` 轮询 DB。两者若共用
    conftest 里那条 `StaticPool` 单连接，在 SQLite 眼里就是**同一个连接上的同一个
    事务**——读者能脏读到写者未提交的数据（假绿），读者 Session 退出时的 rollback
    又会把写者在途语句一并回滚（假红）。

    这里换用「每条连接各自独立事务」的文件型 SQLite，与生产的
    「一个 UoW = 一条连接 = 一个隔离事务」同形。

    本改法**不**触碰 `_wait_terminal`、不加 sleep、不放宽任何 timeout、不加重试：
    它只是让测试基础设施的并发事务语义不再弱于生产。
    """
    yield isolated_db_engine


#: 一条官方形状的入站消息帧（headers.req_id + body.*）。
CALLBACK_FRAME = {
    "cmd": CMD_MSG_CALLBACK,
    "headers": {"req_id": "REQ-1"},
    "body": {
        "msgid": "msg-e2e-1",
        "aibotid": "bot-1",
        "chattype": "single",
        "from": {"userid": "bot-user-1"},
        "msgtype": "text",
        "text": {"content": "hello"},
    },
}


def _callback(**body_overrides) -> dict:
    """在 CALLBACK_FRAME 基础上覆写 body 字段。"""
    frame = json.loads(json.dumps(CALLBACK_FRAME))
    frame["body"].update(body_overrides)
    return frame


def _uow_factory(db_engine):
    return lambda: SqlAlchemyUnitOfWork(
        session_factory=sessionmaker(bind=db_engine, expire_on_commit=False))


def _channel(channel_id: int = 9, **config) -> Channel:
    return Channel(
        name="wecom-bot-1", type=CHANNEL_TYPE, id=channel_id,
        config=config or {"bot_id": "bot-1", "bot_secret": "secret-1"})


def _spec(channel_id: int = 9) -> BotConnectionSpec:
    return BotConnectionSpec(
        channel_id=channel_id, bot_id="bot-1", bot_secret="secret-1",
        ws_url="ws://127.0.0.1:1")


# ================================================================== A. wire 纯函数


def test_a1_compose_subscribe_frame_matches_official_shape() -> None:
    """订阅帧：cmd + headers.req_id + body{bot_id, secret}（官方字段名）。"""
    assert compose_subscribe_frame("bot-1", "sec-1", "REQ-A") == {
        "cmd": CMD_SUBSCRIBE,
        "headers": {"req_id": "REQ-A"},
        "body": {"bot_id": "bot-1", "secret": "sec-1"},
    }


@pytest.mark.parametrize("bot_id,secret,req_id", [
    ("", "s", "r"), ("  ", "s", "r"), ("b", "", "r"), ("b", "   ", "r"),
    ("b", "s", ""), ("b", "s", "   "),
])
def test_a1b_compose_subscribe_frame_fail_closed(bot_id, secret, req_id) -> None:
    with pytest.raises(ValueError):
        compose_subscribe_frame(bot_id, secret, req_id)


def test_a2_compose_ping_frame_is_application_level() -> None:
    """心跳是**应用层** cmd=ping（不是 WS 协议级 ping）。"""
    assert compose_ping_frame("REQ-P") == {
        "cmd": CMD_PING, "headers": {"req_id": "REQ-P"}}


@pytest.mark.parametrize("req_id", ["", "   ", None, 123])
def test_a2b_compose_ping_frame_fail_closed(req_id) -> None:
    with pytest.raises(ValueError):
        compose_ping_frame(req_id)


def test_a3_compose_respond_frame_matches_official_shape() -> None:
    """回复帧：headers.req_id 透传 + body{msgtype:markdown, markdown.content}。"""
    frame = compose_respond_frame("你好", {"req_id": "REQ-1"})
    assert frame == {
        "cmd": CMD_RESPOND_MSG,
        "headers": {"req_id": "REQ-1"},
        "body": {"msgtype": "markdown", "markdown": {"content": "你好"}},
    }
    # 顶层不得出现 msgid / msgtype / markdown（关联键只有 headers.req_id）
    assert "msgid" not in frame
    assert "msgtype" not in frame


@pytest.mark.parametrize("content,ctx", [
    ("", {"req_id": "r"}), ("   ", {"req_id": "r"}),
    ("c", {}), ("c", {"msgid": "m"}), ("c", {"req_id": "  "}),
])
def test_a3b_compose_respond_frame_fail_closed(content, ctx) -> None:
    with pytest.raises(ValueError):
        compose_respond_frame(content, ctx)


@pytest.mark.parametrize("bad", ["", "   ", "not-json", "[]", "123"])
def test_a4_parse_frame_rejects_non_object(bad) -> None:
    with pytest.raises(InboundFormatError):
        parse_frame(bad, channel_id=1)


def test_a5_inbound_from_callback_reads_nested_body() -> None:
    inbound = inbound_from_callback(CALLBACK_FRAME, 9)
    assert inbound is not None
    assert inbound.channel_id == 9
    assert inbound.external_interaction_id == "msg-e2e-1"       # body.msgid
    assert inbound.external_identity_id == "bot-user-1"          # body.from.userid
    assert inbound.external_conversation_id == "bot-user-1"      # 单聊：conversation == identity
    assert inbound.content == "hello"
    # 回信句柄 = headers.req_id（不是 msgid）
    # M6.6-C3 / ADR-12 §13：会话形态由 Adapter 显式声明（与回信句柄同走 metadata）
    assert inbound.metadata == {
        BOT_REPLY_METADATA_KEY: {"req_id": "REQ-1"},
        CHAT_TYPE_METADATA_KEY: CHAT_TYPE_SINGLE,
    }


@pytest.mark.parametrize("mutate", [
    {"cmd": "aibot_event_callback"},       # 非消息帧
    {"cmd": "aibot_respond_msg"},
    {"body": {"msgtype": "image"}},        # MVP 只支持 text
    {"body": {"chattype": "unknown"}},     # 未知 chattype → 不猜（3B-3.3：仅
                                           # single/group 合法；group 测试见
                                           # test_3b_3_3_wecom_bot_group_chat.py）
])
def test_a5b_acknowledge_ignore_variants(mutate) -> None:
    frame = json.loads(json.dumps(CALLBACK_FRAME))
    for key, value in mutate.items():
        if key == "body":
            frame["body"].update(value)
        else:
            frame[key] = value
    assert inbound_from_callback(frame, 1) is None


def test_a5c_missing_body_object_fails_closed() -> None:
    """cmd 是消息帧但缺 body 对象：形状非法（不是 acknowledge-ignore）。"""
    with pytest.raises(InboundFormatError):
        inbound_from_callback({"cmd": CMD_MSG_CALLBACK, "headers": {}}, 1)


@pytest.mark.parametrize("mutate", [
    {"msgid": ""}, {"from": {}}, {"text": {}}, {"text": {"content": "   "}},
])
def test_a5d_required_body_fields_fail_closed(mutate) -> None:
    with pytest.raises(InboundFormatError):
        inbound_from_callback(_callback(**mutate), 1)


@pytest.mark.parametrize("headers", [
    {}, None, {"req_id": ""}, {"req_id": "   "}, {"other": "x"},
])
def test_a5e_missing_headers_req_id_fails_closed(headers) -> None:
    """官方必填的回复关联键缺失 → Fail Closed（绝不产生不可回复的交互）。"""
    frame = json.loads(json.dumps(CALLBACK_FRAME))
    if headers is None:
        frame.pop("headers")
    else:
        frame["headers"] = headers
    with pytest.raises(InboundFormatError):
        inbound_from_callback(frame, 1)


def test_a5f_frame_req_id_helper() -> None:
    assert frame_req_id(CALLBACK_FRAME) == "REQ-1"
    assert frame_req_id({"headers": {"req_id": "  x  "}}) == "x"
    assert frame_req_id({}) == ""
    assert frame_req_id({"headers": "not-a-dict"}) == ""


def test_a6_req_id_stays_transport_metadata_not_domain_field() -> None:
    """req_id **不是** ACP Domain 字段——只经 metadata 走既有通路（3B-2.4 纪律）。"""
    # 1) Domain 契约字段面未被污染
    assert "req_id" not in {f.name for f in dataclasses.fields(InboundInteraction)}

    inbound = inbound_from_callback(CALLBACK_FRAME, 1)
    assert set(inbound.metadata) == {BOT_REPLY_METADATA_KEY, CHAT_TYPE_METADATA_KEY}
    assert set(inbound.metadata[BOT_REPLY_METADATA_KEY]) == {"req_id"}

    # 2) 经 Interaction 快照往返后仍完好（Worker 重建场景）
    rebuilt = inbound_from_snapshot(inbound_to_snapshot(inbound))
    assert rebuilt.metadata == inbound.metadata

    # 3) 从 OutboundResponse 侧仍可取回（Delivery 场景）
    outbound = OutboundResponse(
        channel_id=1, external_conversation_id="u", content="c",
        metadata=rebuilt.metadata)
    assert reply_context_from(outbound) == {"req_id": "REQ-1"}


def test_a7_reply_context_from_outbound_requires_req_id() -> None:
    ok = OutboundResponse(
        channel_id=1, external_conversation_id="u", content="c",
        metadata={BOT_REPLY_METADATA_KEY: {"req_id": "r-1"}})
    assert reply_context_from(ok) == {"req_id": "r-1"}

    # 缺失 / 空 / 类型非法 / 缺 req_id / 值空白 → None（不猜测、不回退）
    assert reply_context_from(OutboundResponse(1, "u", "c")) is None
    assert reply_context_from(OutboundResponse(1, "u", "c", metadata={})) is None
    assert reply_context_from(OutboundResponse(
        1, "u", "c", metadata={BOT_REPLY_METADATA_KEY: "x"})) is None
    assert reply_context_from(OutboundResponse(
        1, "u", "c", metadata={BOT_REPLY_METADATA_KEY: {}})) is None
    assert reply_context_from(OutboundResponse(
        1, "u", "c", metadata={BOT_REPLY_METADATA_KEY: {"msgid": "m"}})) is None
    assert reply_context_from(OutboundResponse(
        1, "u", "c", metadata={BOT_REPLY_METADATA_KEY: {"req_id": "  "}})) is None


def test_a8_connection_spec_from_channel() -> None:
    spec = connection_spec_from_channel(_channel(3))
    assert spec == BotConnectionSpec(
        channel_id=3, bot_id="bot-1", bot_secret="secret-1", ws_url=DEFAULT_WS_URL)

    # ws_url 可覆写（私有化 / 测试替身）；缺省即官方地址
    assert DEFAULT_WS_URL == "wss://openws.work.weixin.qq.com"
    assert connection_spec_from_channel(_channel(
        3, bot_id="b", bot_secret="s", ws_url="ws://127.0.0.1:1")).ws_url \
        == "ws://127.0.0.1:1"

    # Fail Closed：未持久化（无 id）
    with pytest.raises(InboundChannelConfigError):
        connection_spec_from_channel(Channel(name="b", type=CHANNEL_TYPE))

    # Fail Closed：config 缺失 / 缺 bot_id / 缺 bot_secret
    for config in (None, {}, {"bot_id": "b"}, {"bot_secret": "s"}):
        with pytest.raises(InboundChannelConfigError):
            connection_spec_from_channel(
                Channel(name="b", type=CHANNEL_TYPE, id=3, config=config))


# ================================================================== B. 连接注册表


def test_b1_registry_lifecycle() -> None:
    registry = WeComBotConnectionRegistry()
    assert registry.sender_for(1) is None
    assert not registry.is_connected(1)
    registry.register(1, print)
    assert registry.sender_for(1) is print
    assert registry.connected_channel_ids() == [1]
    registry.unregister(1)
    assert not registry.is_connected(1)


def test_b2_stale_unregister_does_not_kill_newer_connection() -> None:
    """重连竞态：旧连接的清理绝不允许误杀新注册的连接。"""
    registry = WeComBotConnectionRegistry()
    old, new = print, len
    registry.register(1, old)
    registry.register(1, new)          # 重连：新连接覆盖
    registry.unregister(1, old)        # 旧连接清理
    assert registry.sender_for(1) is new
    registry.unregister(1, new)
    assert not registry.is_connected(1)


def test_b3_delivery_ack_slot_lifecycle() -> None:
    """3B-2.6：交付结果等待槽只兑现**匹配 req_id** 的响应（无关响应不误兑现）。"""
    registry = WeComBotConnectionRegistry()
    slot = registry.expect_ack(1, "req-x")
    assert registry.fulfil_ack(1, "other-req", {"errcode": 0}) is False
    assert not slot.event.is_set()                      # 未被误兑现
    assert registry.fulfil_ack(1, "req-x", {"errcode": 60140}) is True
    assert slot.event.wait(0.1)
    assert slot.payload == {"errcode": 60140}
    registry.forget_ack(1, "req-x")
    assert registry.fulfil_ack(1, "req-x", {"errcode": 0}) is False


# ================================================================== C. Adapter


def test_c1_verify_handshake_not_applicable() -> None:
    """WS transport 无 URL 握手：Fail Closed（不伪造一次不存在的握手）。"""
    adapter = WeComBotAdapter(registry=WeComBotConnectionRegistry())
    with pytest.raises(InboundChannelConfigError):
        adapter.verify_handshake(
            _channel(1), signature="s", timestamp="t", nonce="n", echostr="e")


def test_c2_parse_message_normalizes_callback() -> None:
    adapter = WeComBotAdapter(registry=WeComBotConnectionRegistry())
    inbound = adapter.parse_message(
        _channel(9), signature="", timestamp="", nonce="",
        body=json.dumps(CALLBACK_FRAME))
    assert inbound is not None
    assert inbound.channel_id == 9
    assert inbound.content == "hello"
    assert inbound.external_interaction_id == "msg-e2e-1"
    assert inbound.metadata == {
        BOT_REPLY_METADATA_KEY: {"req_id": "REQ-1"},
        CHAT_TYPE_METADATA_KEY: CHAT_TYPE_SINGLE,
    }


def test_c2b_parse_message_is_unaffected_by_signature_params() -> None:
    """信任来自订阅连接（非逐帧验签）——三参数对本 transport 无意义。"""
    adapter = WeComBotAdapter(registry=WeComBotConnectionRegistry())
    assert adapter.parse_message(
        _channel(9), signature="", timestamp="", nonce="",
        body=json.dumps(CALLBACK_FRAME)) == adapter.parse_message(
        _channel(9), signature="whatever", timestamp="t", nonce="n",
        body=json.dumps(CALLBACK_FRAME))


def test_c3_parse_message_rejects_foreign_aibotid() -> None:
    """纵深防御：body.aibotid 与渠道 bot_id 不符 → 拒收（400 语义）。"""
    adapter = WeComBotAdapter(registry=WeComBotConnectionRegistry())
    with pytest.raises(InboundFormatError):
        adapter.parse_message(
            _channel(9), signature="", timestamp="", nonce="",
            body=json.dumps(_callback(aibotid="bot-OTHER")))


def test_c4_deliver_fail_closed_without_reply_context() -> None:
    adapter = WeComBotAdapter(registry=WeComBotConnectionRegistry())
    with pytest.raises(ChannelDeliveryFailedError) as excinfo:
        adapter.deliver(OutboundResponse(
            channel_id=1, external_conversation_id="u", content="hi"))
    assert excinfo.value.retryable is False       # 契约类：重试无意义


def test_c4b_deliver_fail_closed_when_reply_context_lacks_req_id() -> None:
    """回信句柄缺 req_id → 与「无句柄」同判（绝不发一条无关联键的回复）。"""
    adapter = WeComBotAdapter(registry=WeComBotConnectionRegistry())
    with pytest.raises(ChannelDeliveryFailedError) as excinfo:
        adapter.deliver(OutboundResponse(
            channel_id=1, external_conversation_id="u", content="hi",
            metadata={BOT_REPLY_METADATA_KEY: {"msgid": "m"}}))
    assert excinfo.value.retryable is False


def test_c5_deliver_fail_closed_when_not_connected() -> None:
    adapter = WeComBotAdapter(registry=WeComBotConnectionRegistry())
    with pytest.raises(ChannelDeliveryFailedError) as excinfo:
        adapter.deliver(OutboundResponse(
            channel_id=1, external_conversation_id="u", content="hi",
            metadata={BOT_REPLY_METADATA_KEY: {"req_id": "r"}}))
    assert excinfo.value.retryable is True        # transport 类


def _acked_sender(registry, channel_id: int, sent: list[str], *,
                  errcode: int = 0, echo_req_id: bool = True) -> None:
    """模拟 transport 线程：记录发送的帧，并按服务端行为兑现该 req_id 的等待槽。"""
    def sender(text: str) -> None:
        sent.append(text)
        req_id = json.loads(text)["headers"]["req_id"]
        registry.fulfil_ack(
            channel_id, req_id if echo_req_id else "SOME-OTHER-REQ",
            {"errcode": errcode, "errmsg": "ok" if errcode == 0 else "rejected"})
    registry.register(channel_id, sender)


def test_c6_deliver_sends_official_respond_frame() -> None:
    """发帧 + 服务端 ACK errcode==0 → 交付成功；帧为官方形状。"""
    sent: list[str] = []
    registry = WeComBotConnectionRegistry()
    _acked_sender(registry, 7, sent)
    adapter = WeComBotAdapter(registry=registry, ack_wait_timeout_s=2.0)
    adapter.deliver(OutboundResponse(
        channel_id=7, external_conversation_id="bot-user-1", content="hi",
        metadata={BOT_REPLY_METADATA_KEY: {"req_id": "REQ-9"}}))
    assert json.loads(sent[0]) == {
        "cmd": CMD_RESPOND_MSG,
        "headers": {"req_id": "REQ-9"},
        "body": {"msgtype": "markdown", "markdown": {"content": "hi"}},
    }


def test_c6b_deliver_fails_closed_on_nonzero_ack_errcode() -> None:
    """3B-2.6：服务端 ACK errcode != 0（应用层拒绝）→ 交付失败，retryable=False。

    同一个 req_id 不会因为重试而变得有效（过期 / 已消费）→ 重试无意义。
    """
    sent: list[str] = []
    registry = WeComBotConnectionRegistry()
    _acked_sender(registry, 7, sent, errcode=60140)
    adapter = WeComBotAdapter(registry=registry, ack_wait_timeout_s=2.0)
    with pytest.raises(ChannelDeliveryFailedError) as excinfo:
        adapter.deliver(OutboundResponse(
            channel_id=7, external_conversation_id="bot-user-1", content="hi",
            metadata={BOT_REPLY_METADATA_KEY: {"req_id": "REQ-9"}}))
    assert excinfo.value.retryable is False
    assert len(sent) == 1                      # 帧确实发出去了（不是发送失败）


def test_c6c_deliver_fails_closed_when_ack_never_arrives() -> None:
    """3B-2.6：只有发送、拿不到业务结果 → 超时 Fail Closed（绝不当成功）。"""
    sent: list[str] = []
    registry = WeComBotConnectionRegistry()
    registry.register(7, sent.append)          # 只记录，不兑现 ACK
    adapter = WeComBotAdapter(registry=registry, ack_wait_timeout_s=0.2)
    with pytest.raises(ChannelDeliveryFailedError) as excinfo:
        adapter.deliver(OutboundResponse(
            channel_id=7, external_conversation_id="bot-user-1", content="hi",
            metadata={BOT_REPLY_METADATA_KEY: {"req_id": "REQ-9"}}))
    assert excinfo.value.retryable is True     # transport 类（未知是否已送达）
    assert len(sent) == 1


def test_c6d_ack_with_mismatched_req_id_does_not_resolve() -> None:
    """3B-2.6：ACK 的 req_id 与本次请求不符 → 不兑现（等待超时 → 失败）。"""
    sent: list[str] = []
    registry = WeComBotConnectionRegistry()
    _acked_sender(registry, 7, sent, echo_req_id=False)
    adapter = WeComBotAdapter(registry=registry, ack_wait_timeout_s=0.2)
    with pytest.raises(ChannelDeliveryFailedError):
        adapter.deliver(OutboundResponse(
            channel_id=7, external_conversation_id="bot-user-1", content="hi",
            metadata={BOT_REPLY_METADATA_KEY: {"req_id": "REQ-9"}}))


def test_c7_deliver_input_validation() -> None:
    adapter = WeComBotAdapter(registry=WeComBotConnectionRegistry())
    with pytest.raises(ChannelDeliveryFailedError):
        adapter.deliver(OutboundResponse(
            channel_id=1, external_conversation_id="u", content="   ",
            metadata={BOT_REPLY_METADATA_KEY: {"req_id": "r"}}))
    with pytest.raises(ChannelDeliveryFailedError):
        adapter.deliver("not-an-outbound")


# ================================================================== D. 策略常量


def test_d1_channel_type_allowlist_includes_wecom_bot(db_engine) -> None:
    assert CHANNEL_TYPE in ALLOWED_CHANNEL_TYPES
    service = ChannelApplicationService(uow_factory=_uow_factory(db_engine))
    created = service.create_channel(CreateChannel(
        name="wecom-bot-1", type=CHANNEL_TYPE,
        config={"bot_id": "bot-1", "bot_secret": "secret-1"}))
    assert created.type == CHANNEL_TYPE


def test_d2_bot_secret_is_redacted_in_responses() -> None:
    """bot_secret 属 credential-like material：读时脱敏，绝不出现在响应里。"""
    sanitized = sanitize_channel_config({
        "bot_id": "bot-1", "bot_secret": "s3cr3t", "ws_url": "wss://example"})
    assert sanitized == {
        "bot_id": "bot-1", "bot_secret": "********", "ws_url": "wss://example"}


# ================================================================== G. 握手（单元）


class _ScriptedConnection:
    """最小连接替身：按「已发送帧」决定回给客户端什么（订阅握手单元测试用）。

    只实现 transport 真正使用的 4 个成员：send / recv(timeout) / 上下文管理。
    """

    def __init__(self, responder, *, eof_when_empty: bool = False) -> None:
        self._responder = responder
        self._eof_when_empty = eof_when_empty
        self.sent: list[dict] = []
        self._pending: list[str] = []

    def send(self, text: str) -> None:
        frame = json.loads(text)
        self.sent.append(frame)
        reply = self._responder(frame)
        if reply is not None:
            self._pending.append(json.dumps(reply))

    def recv(self, timeout=None):          # noqa: ARG002  替身忽略超时语义
        if self._pending:
            return self._pending.pop(0)
        if self._eof_when_empty:
            return None                    # 对端正常关闭
        raise TimeoutError()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _transport_with(connection, **config) -> WeComBotTransport:
    return WeComBotTransport(
        registry=WeComBotConnectionRegistry(),
        binding_source=list,
        on_frame=lambda spec, raw: None,
        connect=lambda url: connection,
        config=WeComBotTransportConfig(**config))


def test_g1_subscribe_rejected_raises_subscribe_error() -> None:
    """errcode != 0（凭据错误 / 限频）→ 订阅失败，绝不进入 connected（防假绿）。"""
    def responder(frame):
        assert frame["cmd"] == CMD_SUBSCRIBE
        assert frame["body"] == {"bot_id": "bot-1", "secret": "secret-1"}
        return {"headers": {"req_id": frame["headers"]["req_id"]},
                "errcode": 40001, "errmsg": "invalid secret"}

    transport = _transport_with(_ScriptedConnection(responder))
    with pytest.raises(WeComBotSubscribeError):
        transport._connection_attempt(_spec(), queue.Queue())


def test_g2_subscribe_ack_must_match_echoed_req_id() -> None:
    """响应帧 req_id 与本次请求不符 → 不认（等待至超时 → 订阅失败）。"""
    def responder(frame):
        return {"headers": {"req_id": "SOME-OTHER-REQ"},
                "errcode": 0, "errmsg": "ok"}

    transport = _transport_with(
        _ScriptedConnection(responder), subscribe_timeout_s=0.2,
        recv_timeout_s=0.05)
    with pytest.raises(WeComBotSubscribeError):
        transport._connection_attempt(_spec(), queue.Queue())


def test_g3_subscribe_ack_without_errcode_is_not_accepted() -> None:
    """无 errcode 的帧不是订阅响应 → 不认（不得把任意推送当作订阅成功）。"""
    def responder(frame):
        return {"headers": {"req_id": frame["headers"]["req_id"]}, "foo": "bar"}

    transport = _transport_with(
        _ScriptedConnection(responder), subscribe_timeout_s=0.2,
        recv_timeout_s=0.05)
    with pytest.raises(WeComBotSubscribeError):
        transport._connection_attempt(_spec(), queue.Queue())


def test_g4_subscribe_success_registers_connection() -> None:
    """errcode == 0 → 注册连接（此后 deliver 才有句柄可用）。"""
    def responder(frame):
        if frame["cmd"] == CMD_SUBSCRIBE:
            return {"headers": {"req_id": frame["headers"]["req_id"]},
                    "errcode": 0, "errmsg": "ok"}
        return None

    registry = WeComBotConnectionRegistry()
    transport = WeComBotTransport(
        registry=registry, binding_source=list,
        on_frame=lambda spec, raw: None,
        connect=lambda url: _ScriptedConnection(
            responder, eof_when_empty=True),
        config=WeComBotTransportConfig(recv_timeout_s=0.05))
    # 订阅成功后对端正常关闭 → 会话正常结束（不重连），连接保持注册
    transport._connection_attempt(_spec(9), queue.Queue())
    assert registry.is_connected(9)


# ================================================================== E/F. 全链路


class _FakeWeComWS:
    """假企业微信长连接服务端（真实 WebSocket over 127.0.0.1，官方帧形状）。

    行为：
      - 收到 `aibot_subscribe` → 记录 → 按 `errcode` 回订阅响应（**echo req_id**）
        → errcode==0 时投递配置的 callback 帧；
      - 收到 `ping` → 记录 → 回 `{"headers":{"req_id"},"errcode":0}`；
      - 收到 `aibot_respond_msg` → 记录 → **像真实服务端一样回 errcode 响应**
        （3B-2.5 帧级实证：真实服务确实会对回复帧回 `errcode`），
        再置事件。
    """

    def __init__(self, callbacks: list[dict], *, errcode: int = 0,
                 respond_errcode: int = 0) -> None:
        self._callbacks = callbacks
        self._errcode = errcode
        self._respond_errcode = respond_errcode
        self.subscribes: list[dict] = []
        self.pings: list[dict] = []
        self.replies: list[dict] = []
        self.reply_event = threading.Event()
        self.subscribed = threading.Event()
        self._lock = threading.Lock()
        self._server = serve(self._handle, "127.0.0.1", 0)
        self.port: int = self._server.socket.getsockname()[1]
        self._thread: threading.Thread | None = None

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
            if cmd == CMD_SUBSCRIBE:
                with self._lock:
                    self.subscribes.append(frame)
                connection.send(json.dumps(
                    {"headers": {"req_id": req_id}, "errcode": self._errcode,
                     "errmsg": "ok" if self._errcode == 0 else "rejected"}))
                self.subscribed.set()
                if self._errcode == 0:
                    for callback in self._callbacks:
                        connection.send(json.dumps(callback, ensure_ascii=False))
            elif cmd == CMD_PING:
                with self._lock:
                    self.pings.append(frame)
                connection.send(json.dumps(
                    {"headers": {"req_id": req_id}, "errcode": 0, "errmsg": "ok"}))
            elif cmd == CMD_RESPOND_MSG:
                with self._lock:
                    self.replies.append(frame)
                # 3B-2.6：真实服务端会对 aibot_respond_msg 回 errcode（echo req_id）
                connection.send(json.dumps(
                    {"headers": {"req_id": req_id},
                     "errcode": self._respond_errcode,
                     "errmsg": "ok" if self._respond_errcode == 0 else "rejected"}))
                self.reply_event.set()

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()


def _seed_bot_channel(db_engine, *, ws_url: str,
                      external_id: str = "bot-user-1") -> int:
    """user → identity(wecom_bot) → channel(wecom_bot) → connection → agent(local)。"""
    with _uow_factory(db_engine)() as uow:
        user = uow.users.create(User(name="bot-user"))
        channel = uow.channels.create(Channel(
            name="wecom-bot-1", type=CHANNEL_TYPE,
            config={"bot_id": "bot-1", "bot_secret": "secret-1", "ws_url": ws_url}))
        agent = uow.agents.create(Agent(name="local-echo", type="local"))
        uow.identities.create(Identity(
            user_id=user.id, channel_id=channel.id, external_id=external_id))
        path = uow.connections.create(Connection(
            channel_id=channel.id, agent_id=agent.id, name="bot-path",
            status="active"))
        # Implementation Package 2 / Step 2：单聊会话键 = from.userid（授权前提；
        # 只补 default-deny 所需的显式授权，不改本文件 3B-3.3 WIP 的任何逻辑）
        uow.connection_conversations.create(ConnectionConversation(
            channel_id=channel.id, external_conversation_id=external_id,
            connection_id=path.id))
        uow.commit()
        return channel.id


def _build_chain(db_engine, registry):
    """完整六段 Runtime + 真实 Adapter（渠道侧只有 wecom_bot）。"""
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


#: 处理态终态（3B-2.6：断言必须等**终态**，不能只等「≠ pending」）。
_TERMINAL_PROCESSING = (PROCESSING_SUCCEEDED, PROCESSING_FAILED)

#: 投递态终态（P1 / C3：投递是**独立**的一层事实，processing 先落、delivery 后落）。
_TERMINAL_DELIVERY = (DELIVERY_DELIVERED, DELIVERY_FAILED)


def _wait_terminal(uow, channel_id: int, external_id: str, timeout: float = 15.0):
    """轮询等待 interaction 落**终态**。

    只等「!= pending」是不够的：Worker 会先把行 CAS 成 `processing`，终态在其后
    才写入——于是可能读到中间态 `processing` 并提前返回（3B-2.6 线程模型解耦后
    首次触发该竞态）。

    **P1 / C3 追加**：终态现在分两层——`processing` 先落 SUCCEEDED（Agent 结果
    已落库），**投递**随后才落 DELIVERED / FAILED。故等待条件必须**同时**等到
    delivery 终态，否则会读到「Agent 已成功、投递尚未落定」这个合法的中间态。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with uow() as u:
            record = u.interactions.find_by_channel_and_external_id(
                channel_id, external_id)
        if record is not None \
                and record.processing_status in _TERMINAL_PROCESSING \
                and record.delivery_status in _TERMINAL_DELIVERY:
            return record
        time.sleep(0.05)
    raise AssertionError(f"interaction 未在 {timeout}s 内落终态：{external_id}")


def _interaction_count(db_engine) -> int:
    with Session(db_engine) as session:
        return session.execute(
            select(func.count()).select_from(InteractionModel)).scalar_one()


def _run_fake_bot(db_engine, callbacks: list[dict], *, errcode: int = 0,
                  respond_errcode: int = 0, **config):
    """启动 fake WS + 真实链路；返回 (fake, registry, transport, uow, channel_id)。"""
    fake = _FakeWeComWS(callbacks=callbacks, errcode=errcode,
                        respond_errcode=respond_errcode)
    fake.start()
    channel_id = _seed_bot_channel(db_engine, ws_url=f"ws://127.0.0.1:{fake.port}")
    registry = WeComBotConnectionRegistry()
    uow, service, worker = _build_chain(db_engine, registry)
    with uow() as u:
        spec = connection_spec_from_channel(u.channels.get_by_id(channel_id))

    def on_frame(binding, raw: str) -> None:
        # 与组合根同构：同一 accept_message 边界 + 同一 Worker（Trigger 1）
        acceptance = service.accept_message(
            binding.channel_id, signature="", timestamp="", nonce="", body=raw)
        if acceptance.interaction_id is not None:
            worker.process(acceptance.interaction_id)

    settings = {"reconnect_backoff_s": 0.2, "recv_timeout_s": 0.2}
    settings.update(config)
    transport = WeComBotTransport(
        registry=registry, binding_source=lambda: [spec], on_frame=on_frame,
        config=WeComBotTransportConfig(**settings))
    transport.start()
    return fake, registry, transport, uow, channel_id


def test_e1_fake_wecom_ws_end_to_end(db_engine) -> None:
    """fake WeCom WS → aibot_subscribe(校验 errcode) → aibot_msg_callback
    → PENDING → Worker → Agent → Delivery → aibot_respond_msg（req_id 透传）
    → fake WS 收到回复（完整闭环，官方帧形状）。"""
    fake, registry, transport, uow, channel_id = _run_fake_bot(
        db_engine, [json.loads(json.dumps(CALLBACK_FRAME))])
    try:
        assert fake.reply_event.wait(20), "fake WeCom WS 未收到 aibot_respond_msg"

        # 1) 订阅帧：官方形状（headers.req_id + body 凭据）
        assert len(fake.subscribes) == 1
        subscribe = fake.subscribes[0]
        assert subscribe["cmd"] == CMD_SUBSCRIBE
        assert subscribe["body"] == {"bot_id": "bot-1", "secret": "secret-1"}
        assert isinstance(subscribe["headers"]["req_id"], str)
        assert subscribe["headers"]["req_id"].strip()

        # 2) 回复帧：req_id **透传回调值**（唯一关联键）+ 官方 body 形状
        assert len(fake.replies) == 1
        reply = fake.replies[0]
        assert reply["cmd"] == CMD_RESPOND_MSG
        assert reply["headers"]["req_id"] == "REQ-1"
        assert reply["body"]["msgtype"] == "markdown"
        assert reply["body"]["markdown"]["content"] == "Echo: hello"
        assert "msgid" not in reply           # 关联键不是 msgid

        # 3) Observation：完整成功（持久化证据与渠道副作用互相印证）
        record = _wait_terminal(uow, channel_id, "msg-e2e-1")
        assert record.processing_status == PROCESSING_SUCCEEDED
        assert record.delivery_status == DELIVERY_DELIVERED
        assert record.response_payload is not None
        assert record.response_payload["content"] == "Echo: hello"
        assert record.session_id is not None

        # 4) 连接在 delivery 之后仍然存活
        assert registry.is_connected(channel_id)
    finally:
        transport.stop()
        fake.stop()


def test_f1_duplicate_callback_is_deduplicated(db_engine) -> None:
    """重复帧（重连重推）不产生第二次执行 / 第二次回复（既有状态感知去重）。"""
    duplicate = json.loads(json.dumps(CALLBACK_FRAME))
    fake, registry, transport, uow, channel_id = _run_fake_bot(
        db_engine, [json.loads(json.dumps(CALLBACK_FRAME)), duplicate])
    try:
        assert fake.reply_event.wait(20), "fake WeCom WS 未收到 aibot_respond_msg"
        # 给第二帧留出处理窗口：若去重失效会立刻出现第二条回复（快速失败）
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and len(fake.replies) <= 1:
            time.sleep(0.05)
        assert len(fake.replies) == 1, fake.replies
        assert _interaction_count(db_engine) == 1
        record = _wait_terminal(uow, channel_id, "msg-e2e-1")
        assert record.processing_status == PROCESSING_SUCCEEDED
    finally:
        transport.stop()
        fake.stop()


def test_f2_respond_ack_rejected_state_matrix(db_engine) -> None:
    """**真实链路回归（3B-2.6）**：Agent 成功 + 服务端拒绝回复帧（ACK errcode != 0）。

    状态矩阵必须是

        processing = SUCCEEDED   ← Agent 自己完成了任务
        delivery   = FAILED      ← 失败的是 Channel Delivery

    **绝不是** processing = FAILED —— 否则 Observation 会把「Agent 完成但没送达」
    误报成「Agent 执行失败」。
    """
    fake, _registry, transport, uow, channel_id = _run_fake_bot(
        db_engine, [json.loads(json.dumps(CALLBACK_FRAME))], respond_errcode=60140)
    try:
        assert fake.reply_event.wait(20), "fake WeCom WS 未收到 aibot_respond_msg"
        record = _wait_terminal(uow, channel_id, "msg-e2e-1")
        assert record.processing_status == PROCESSING_SUCCEEDED   # Agent 成功
        assert record.delivery_status == DELIVERY_FAILED          # 投递失败
        assert record.failure_stage == "delivery"
        assert record.failure_code == "CHANNEL_DELIVERY_FAILED"
        assert record.retryable is False                          # 应用层明确拒绝
        # **P1 / C3**：投递失败**不再**丢掉 Agent 已生成的结果——响应快照在
        # 投递之前就已落库，并原样保留（旧行为：此处恒为 NULL）。
        assert record.response_payload is not None
        assert record.response_payload["content"] == "Echo: hello"
    finally:
        transport.stop()
        fake.stop()


def test_g5_subscribe_rejected_over_real_socket_never_connected(db_engine) -> None:
    """B5 集成：服务端 errcode != 0 → 渠道绝不进入 connected（且持续重试）。"""
    fake, registry, transport, _uow, channel_id = _run_fake_bot(
        db_engine, [], errcode=40001)
    try:
        assert fake.subscribed.wait(10), "fake WeCom WS 未收到订阅请求"
        time.sleep(0.6)                       # 观察若干个重连周期
        assert not registry.is_connected(channel_id)
        assert len(fake.subscribes) >= 1
        assert fake.replies == []
    finally:
        transport.stop()
        fake.stop()


def test_g6_heartbeat_is_application_level_ping(db_engine) -> None:
    """B4：空闲时按 heartbeat 间隔发**应用层** cmd=ping 帧（官方保活语义）。"""
    fake, _registry, transport, _uow, _channel_id = _run_fake_bot(
        db_engine, [], heartbeat_interval_s=0.2)
    try:
        assert fake.subscribed.wait(10), "fake WeCom WS 未收到订阅请求"
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not fake.pings:
            time.sleep(0.05)
        assert fake.pings, "未观察到应用层心跳帧"
        ping = fake.pings[0]
        assert ping["cmd"] == CMD_PING
        assert isinstance(ping["headers"]["req_id"], str)
        assert ping["headers"]["req_id"].strip()
    finally:
        transport.stop()
        fake.stop()


def test_h1_default_connect_disables_builtin_protocol_keepalive(monkeypatch) -> None:
    """回归守卫（3B-2.5 真实联调暴露的真实缺陷）：**协议级 keepalive 必须关闭**。

    帧级实证：websockets 默认 `ping_interval=20` 会自行发送 WebSocket 协议级 PING，
    该服务端对该 PING 的响应被 websockets 判为 `incorrect masking` → 客户端以 1002
    主动关闭 → 连接约每 25s 断一次并反复重订阅（有触发官方「订阅频率保护」的风险）。

    因此：心跳只能走**应用层** `{"cmd":"ping"}`；`_default_connect` 必须传
    `ping_interval=None`。本测试直接锁住该参数，防止将来被"顺手优化"回去。
    """
    import websockets.sync.client as ws_client

    from app.adapters.channels.wecom_bot import _default_connect

    captured: dict = {}

    def fake_connect(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(ws_client, "connect", fake_connect)
    _default_connect("wss://example.invalid")

    assert captured["url"] == "wss://example.invalid"
    assert captured["ping_interval"] is None, captured
    assert captured["open_timeout"] > 0, captured
    assert captured["close_timeout"] > 0, captured
