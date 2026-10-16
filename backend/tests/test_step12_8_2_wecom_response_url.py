"""Step 12.8.2 —— 智能机器人 response_url Delivery（最小实现）。

基于 12.8.1 已确认的官方事实（101138 / 100719）：

    Robot JSON inbound
      → InboundInteraction.metadata["response_url"]   （channel-originated context）
      → Runtime（Coordinator：channel metadata 优先传播）
      → OutboundResponse.metadata
      → WeComAdapter.deliver → POST response_url（markdown）

锁定项：
A  入站：robot JSON → response_url 进入 metadata；缺失 Fail Closed（不回退 message/send）
B  入站：XML / 自建应用路径不产生 metadata（原样回归）
C  传播：inbound metadata → OutboundResponse metadata；Agent metadata 不得覆盖 response_url
D  出站 wire：POST response_url / markdown / 无 Authorization / 无 access_token / 不拼参数
E  出站：无 response_url → 既有 message/send 路径完全不变
F  成功判定（3B-2.6 校正）：**HTTP 2xx 且响应体 errcode == 0**。
   12.8.2 当时以「官方未定义响应 schema，不做未确认推断」为由只判 HTTP 层；
   该推断已被真实响应**证伪**——同一路径实测：
        TTL 内 → HTTP 200 + {"errcode":0,"errmsg":"ok"}
        过期后 → HTTP 200 + {"errcode":60140,"errmsg":"invalid response code"}
   即 HTTP 200 ≠ 送达，只判 HTTP 层会产生「记为 DELIVERED 而用户没收到」的假绿。
G  失败语义：processing=succeeded + delivery=failed + retryable=false（一次性 URL）
H  传播不影响 Session / Routing / Dispatch 的调用与参数
"""

import json
import urllib.request

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.channels.wecom import (
    RESPONSE_URL_METADATA_KEY,
    WeComAdapter,
    _inbound_from_robot_json,
    _inbound_from_xml,
    _response_errcode,
    _response_url_from,
    compose_message_payload,
    compose_robot_reply_payload,
    compute_signature,
    decrypt_message,
    encrypt_message,
)
from app.application.coordinator import RuntimeCoordinator
from app.application.delivery import DeliveryExecutor
from app.application.errors import ChannelDeliveryFailedError
from app.application.interaction_recording import PersistingCoordinator
from app.domain.access_authorization import AccessAuthorization
from app.domain.agent_response import AgentResponse
from app.domain.channel import Channel
from app.domain.inbound_interaction import (
    CHAT_TYPE_METADATA_KEY,
    CHAT_TYPE_SINGLE,
    InboundInteraction,
)
from app.domain.outbound_response import OutboundResponse
from app.domain.routing_result import RoutingResult
from app.domain.session_resolution import REUSE_ACTIVE, SessionResolution
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

ROBOT_URL = "https://qyapi.weixin.qq.com/cgi-bin/aibot/response?response_code=CODE123"
TOKEN = "robot-token"
AES_KEY = "c" * 43
CORP_ID = "wwcorp-robot"
CONTENT = "这是 Agent 的回复"


# ================================================================== 测试底座


class _FakeResponse:
    """最小 HTTP 响应（支持 with / read / status）。"""

    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args) -> bool:
        return False


class _FakeUrlOpen:
    """记录所有出站 HTTP 调用；可编排 status / 异常 / body。"""

    def __init__(self, status: int = 200, exc: Exception | None = None,
                 body: bytes | None = None) -> None:
        self.calls: list[dict] = []
        self._status = status
        self._exc = exc
        self._body = body

    def __call__(self, req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        self.calls.append({
            "url": url,
            "method": req.get_method() if hasattr(req, "get_method") else "GET",
            "headers": dict(req.header_items()) if hasattr(req, "header_items") else {},
            "data": getattr(req, "data", None),
            "timeout": timeout,
        })
        if self._exc is not None:
            raise self._exc
        body = self._body
        if body is None:
            body = (b'{"errcode": 0, "access_token": "tok-123"}' if "gettoken" in url
                    else b'{"errcode": 0}')
        return _FakeResponse(self._status, body)


def _robot_payload(**overrides) -> dict:
    payload = {
        "msgid": "msg-1",
        "aibotid": "bot-1",
        "chattype": "single",
        "from": {"userid": "zhangsan"},
        "msgtype": "text",
        "text": {"content": "你好"},
        "response_url": ROBOT_URL,
    }
    payload.update(overrides)
    return payload


def _outbound_with_response_url(content: str = CONTENT,
                                url: str = ROBOT_URL) -> OutboundResponse:
    return OutboundResponse(
        channel_id=1, external_conversation_id="zhangsan", content=content,
        metadata={RESPONSE_URL_METADATA_KEY: url},
    )


@pytest.fixture()
def patched_urlopen(monkeypatch):
    """安装并返回可编排的 urlopen 记录器。"""

    def _install(**kwargs):
        fake = _FakeUrlOpen(**kwargs)
        monkeypatch.setattr(urllib.request, "urlopen", fake)
        return fake

    return _install


# ================================================================== A. 入站：response_url → metadata


def test_a1_robot_json_inbound_puts_response_url_into_metadata() -> None:
    """robot text 单聊 → response_url 进入 InboundInteraction.metadata。"""
    inbound = _inbound_from_robot_json(_robot_payload(), channel_id=7)
    assert inbound is not None
    # M6.6-C3 / ADR-12 §13：会话形态由 Adapter 显式声明，与 response_url 同走 metadata。
    assert inbound.metadata == {
        RESPONSE_URL_METADATA_KEY: ROBOT_URL,
        CHAT_TYPE_METADATA_KEY: CHAT_TYPE_SINGLE,
    }
    # 8.1 契约字段面不动（response_url 不是字段，只在 metadata 中）
    assert inbound.channel_id == 7
    assert inbound.external_interaction_id == "msg-1"
    assert inbound.external_conversation_id == "zhangsan"
    assert inbound.external_identity_id == "zhangsan"
    assert inbound.content == "你好"


@pytest.mark.parametrize("missing", [None, "", "   ", 12345, {"a": 1}])
def test_a2_missing_response_url_fails_closed(missing) -> None:
    """缺失 / 非法 response_url → InboundFormatError（不默默回退 message/send）。

    官方 100719：用户→机器人消息推送必然携带 response_url；
    官方 101138：它是智能机器人唯一的主动回复通道。
    → 缺失即协议异常，Fail Closed（不伪造、不 fallback）。
    """
    with pytest.raises(Exception) as excinfo:
        _inbound_from_robot_json(
            _robot_payload(response_url=missing), channel_id=7)
    # 入站协议错误类型（不进入 Runtime Failure Model）
    assert excinfo.value.__class__.__name__ == "InboundFormatError"
    assert "response_url" in str(excinfo.value)


@pytest.mark.parametrize("overrides", [
    {"msgtype": "image"},           # 非 text → 原有 acknowledge-ignore 语义不变
    {"chattype": "group"},          # 群聊 → 仍返回 None（单聊裁决延续）
])
def test_a3_unsupported_robot_message_still_returns_none(overrides) -> None:
    assert _inbound_from_robot_json(_robot_payload(**overrides), 7) is None


def test_a4_full_envelope_parse_message_carries_response_url() -> None:
    """走完整信封（JSON 外层 + 验签 + 解密，receiveid=\"\"）→ metadata 携带 response_url。"""
    channel = Channel(name="robot", type="wecom", id=7, config={
        "token": TOKEN, "encoding_aes_key": AES_KEY, "corp_id": CORP_ID,
        "receiveid": "",  # 智能机器人：receiveid 为空串
    })
    plaintext = json.dumps(_robot_payload(), ensure_ascii=False)
    encrypt = encrypt_message(plaintext, AES_KEY, "")
    signature = compute_signature(TOKEN, "1700000000", "n1", encrypt)
    body = json.dumps({"encrypt": encrypt})

    inbound = WeComAdapter().parse_message(
        channel, signature=signature, timestamp="1700000000", nonce="n1", body=body)
    assert inbound is not None
    assert inbound.metadata == {
        RESPONSE_URL_METADATA_KEY: ROBOT_URL,
        CHAT_TYPE_METADATA_KEY: CHAT_TYPE_SINGLE,
    }


# ================================================================== B. 入站：自建应用 XML 原样回归


def test_b1_xml_self_built_inbound_has_no_response_url() -> None:
    """XML 自建应用路径无 response_url → 出站走既有 message/send。

    M6.6-C3 / ADR-12 §13：本路径**仍然显式声明** `chat_type`（它只支持单聊），
    但不携带 response_url —— 因此 `_response_url_from` 依旧返回 None，
    出站路径**逐字节不变**。
    """
    inbound = _inbound_from_xml({
        "MsgType": "text", "Content": "你好",
        "FromUserName": "zhangsan", "MsgId": "1001",
    }, channel_id=7)
    assert inbound is not None
    assert inbound.metadata == {CHAT_TYPE_METADATA_KEY: CHAT_TYPE_SINGLE}
    assert RESPONSE_URL_METADATA_KEY not in inbound.metadata
    assert _response_url_from(OutboundResponse(
        channel_id=7, external_conversation_id="zhangsan", content=CONTENT)) is None


# ================================================================== C. metadata 传播（Coordinator）


class _FakeIdentity:
    def __init__(self, identity_id: int = 1) -> None:
        self.identity = type("R", (), {"id": identity_id})()


class _FakeIdentityResolution:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def execute(self, channel_id, external_identity_id):
        self.calls.append((channel_id, external_identity_id))
        return _FakeIdentity()


class _FakeRouting:
    def __init__(self) -> None:
        self.calls: list[int] = []

    def execute(self, channel_id, granted_connection_ids):
        self.calls.append(channel_id)
        # Package 2 / Step 2：Router 只消费已授权候选（ADR-8）
        return RoutingResult(connection_id=11, agent_id=21)


class _FakeSessionResolution:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def execute(self, identity_id, connection_id, external_conversation_id):
        self.calls.append((identity_id, connection_id, external_conversation_id))
        return SessionResolution(
            mode=REUSE_ACTIVE, identity_id=identity_id,
            connection_id=connection_id,
            external_conversation_id=external_conversation_id,
            existing_session_id=31,
        )


class _FakeSessionCreation:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        return type("S", (), {"id": 99})()


class _FakeDispatch:
    def __init__(self, response: AgentResponse) -> None:
        self._response = response
        self.calls: list[tuple] = []

    def execute(self, routing_result, control_plane_session_id, content,
                external_session_id=None, metadata=None):  # noqa: ARG002  15.5.2 透传形参
        self.calls.append((routing_result, control_plane_session_id, content))
        return self._response


class _FakeDelivery:
    def __init__(self) -> None:
        self.outbounds: list[OutboundResponse] = []

    def execute(self, outbound) -> None:
        self.outbounds.append(outbound)


class _FakeAuthorization:
    """Package 2 / Step 2：恒 ALLOW 的**测试替身**（不是生产占位实现）。

    生产侧 `granted_connection_ids` 是必填、无默认值；这里由替身给出候选集，
    以便本文件继续只验证「metadata 传播」这一件事。
    """

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def execute(self, channel_id, external_conversation_id):
        self.calls.append((channel_id, external_conversation_id))
        return AccessAuthorization(granted_connection_ids=frozenset({11}))


def _coordinator(agent_response: AgentResponse):
    identity, routing = _FakeIdentityResolution(), _FakeRouting()
    session_res, session_cre = _FakeSessionResolution(), _FakeSessionCreation()
    dispatch, delivery = _FakeDispatch(agent_response), _FakeDelivery()
    return RuntimeCoordinator(
        identity_resolution=identity, access_authorization=_FakeAuthorization(),
        routing=routing,
        session_resolution=session_res, session_creation=session_cre,
        agent_dispatch=dispatch, delivery=delivery,
    ), (identity, routing, session_res, session_cre, dispatch, delivery)


def _inbound(metadata=None) -> InboundInteraction:
    return InboundInteraction(
        channel_id=1, external_interaction_id="m1",
        external_conversation_id="zhangsan", external_identity_id="zhangsan",
        content="你好", metadata=metadata,
    )


def test_c1_inbound_metadata_reaches_outbound_metadata() -> None:
    """channel metadata（response_url）传播到 OutboundResponse.metadata。"""
    coord, (_, _, _, _, _, delivery) = _coordinator(AgentResponse(content=CONTENT))
    coord.execute(_inbound(metadata={RESPONSE_URL_METADATA_KEY: ROBOT_URL}))
    assert delivery.outbounds[0].metadata == {RESPONSE_URL_METADATA_KEY: ROBOT_URL}


def test_c2_agent_metadata_cannot_overwrite_response_url() -> None:
    """Agent metadata 不得覆盖 response_url；只能补充新 key。"""
    agent = AgentResponse(content=CONTENT, metadata={
        RESPONSE_URL_METADATA_KEY: "https://evil.example.com/hijack",
        "agent_note": "keep-me",
    })
    coord, (_, _, _, _, _, delivery) = _coordinator(agent)
    coord.execute(_inbound(metadata={RESPONSE_URL_METADATA_KEY: ROBOT_URL}))
    metadata = delivery.outbounds[0].metadata
    assert metadata[RESPONSE_URL_METADATA_KEY] == ROBOT_URL   # channel 优先，未被覆盖
    assert metadata["agent_note"] == "keep-me"                 # Agent 补充保留


def test_c3_agent_metadata_survives_when_no_channel_metadata() -> None:
    """无 channel metadata 时，Agent metadata 原样透传（既有行为不变）。"""
    coord, (_, _, _, _, _, delivery) = _coordinator(
        AgentResponse(content=CONTENT, metadata={"k": "v"}))
    coord.execute(_inbound(metadata=None))
    assert delivery.outbounds[0].metadata == {"k": "v"}


def test_c4_no_metadata_at_all_yields_none() -> None:
    """两侧都无 metadata → None（不制造空 dict，保持既有语义）。"""
    coord, (_, _, _, _, _, delivery) = _coordinator(AgentResponse(content=CONTENT))
    coord.execute(_inbound(metadata=None))
    assert delivery.outbounds[0].metadata is None


def test_c5_response_url_metadata_does_not_disturb_session_routing_dispatch() -> None:
    """response_url metadata 不影响 Session / Routing / Dispatch 的调用与参数。"""
    coord, (identity, routing, session_res, session_cre, dispatch, delivery) = \
        _coordinator(AgentResponse(content=CONTENT))
    coord.execute(_inbound(metadata={RESPONSE_URL_METADATA_KEY: ROBOT_URL}))

    assert identity.calls == [(1, "zhangsan")]           # identity 参数不变
    assert routing.calls == [1]                          # routing 参数不变
    assert session_res.calls == [(1, 11, "zhangsan")]    # 消费 routing 的 connection_id
    assert session_cre.calls == []                       # REUSE_ACTIVE 不创建
    assert dispatch.calls[0][1] == 31                    # control_plane_session_id
    assert dispatch.calls[0][2] == "你好"                 # content 未被 metadata 污染
    assert delivery.outbounds[0].content == CONTENT      # 交付内容 = Agent 产出


# ================================================================== D. 出站 wire：POST response_url


def test_d1_response_url_delivery_posts_markdown(
        patched_urlopen) -> None:
    """POST response_url，body = markdown（官方无 text 类型）。"""
    fake = patched_urlopen()
    # 不装配 uow_factory：response_url 路径不读 Channel 凭据（见 d3）
    WeComAdapter().deliver(_outbound_with_response_url())

    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == ROBOT_URL                      # URL 原样使用，不拼接参数
    assert call["headers"].get("Content-type") == "application/json"
    assert json.loads(call["data"].decode("utf-8")) == {
        "msgtype": "markdown", "markdown": {"content": CONTENT},
    }


def test_d2_response_url_delivery_sends_no_authorization_or_token(
        patched_urlopen) -> None:
    """不发送 Authorization header、不添加 access_token（鉴权在 URL 内）。"""
    fake = patched_urlopen()
    WeComAdapter().deliver(_outbound_with_response_url())

    headers = {k.lower(): v for k, v in fake.calls[0]["headers"].items()}
    assert "authorization" not in headers
    assert "access_token" not in fake.calls[0]["url"]
    assert "corpsecret" not in fake.calls[0]["url"]
    assert "agentid" not in fake.calls[0]["url"]


def test_d3_response_url_delivery_does_not_require_channel_credentials(
        patched_urlopen) -> None:
    """智能机器人路径不读 Channel.config（无 corpsecret / agentid 也能交付）。

    这同时证明：response_url 与 message/send 是两条独立路径，
    不会因为智能机器人渠道缺少自建应用凭据而 Fail Closed。
    """
    fake = patched_urlopen()
    WeComAdapter(uow_factory=None).deliver(_outbound_with_response_url())
    assert len(fake.calls) == 1


def test_d4_response_url_delivery_success_requires_errcode_zero(
        patched_urlopen) -> None:
    """3B-2.6：HTTP 200 + errcode==0 才算送达（默认替身体即 {"errcode": 0}）。"""
    fake = patched_urlopen(status=200, body=b'{"errcode": 0, "errmsg": "ok"}')
    WeComAdapter().deliver(_outbound_with_response_url())   # 不抛异常 = 成功
    assert len(fake.calls) == 1


def test_d4b_expired_response_code_is_delivery_failure(patched_urlopen) -> None:
    """真实实证样本（3B-2.5 实测）：过期 response_code → HTTP **200** + errcode 60140。

    只判 HTTP 层会把它当成功（假绿）；现在必须落 delivery failure，且因 URL 一次性
    故 retryable=False。
    """
    fake = patched_urlopen(status=200, body=json.dumps(
        {"errcode": 60140, "errmsg": "invalid response code"}).encode("utf-8"))
    with pytest.raises(ChannelDeliveryFailedError) as excinfo:
        WeComAdapter().deliver(_outbound_with_response_url())
    assert excinfo.value.retryable is False
    assert len(fake.calls) == 1


@pytest.mark.parametrize("body", [
    b"not-json-at-all",   # 非 JSON
    b"[]",                # JSON 但不是对象
    b"{}",                # 对象但无 errcode
    b'{"errmsg": "ok"}',  # 无 errcode
    b'{"errcode": "0"}',  # errcode 类型非法（非 int）
    b"",                  # 空体
])
def test_d4c_unconfirmable_body_is_delivery_failure(body, patched_urlopen) -> None:
    """无法确证 `errcode == 0` 一律 Fail Closed（宁可误报失败，绝不假绿）。"""
    patched_urlopen(status=200, body=body)
    with pytest.raises(ChannelDeliveryFailedError) as excinfo:
        WeComAdapter().deliver(_outbound_with_response_url())
    assert excinfo.value.retryable is False


@pytest.mark.parametrize("body,expected", [
    ('{"errcode":0}', 0),
    ('{"errcode":60140,"errmsg":"invalid response code"}', 60140),
    ('{"errcode":"0"}', None),      # 类型不符 → 不可确证
    ("not-json", None),
    ("[]", None),
    ("{}", None),
    ("", None),
])
def test_d4d_response_errcode_helper(body, expected) -> None:
    assert _response_errcode(body) == expected


@pytest.mark.parametrize("status", [400, 401, 403, 500])
def test_d5_non_2xx_is_delivery_failure(status, patched_urlopen) -> None:
    """非 2xx → CHANNEL_DELIVERY_FAILED（只做 HTTP 层判定）。"""
    patched_urlopen(status=status)
    with pytest.raises(ChannelDeliveryFailedError):
        WeComAdapter().deliver(_outbound_with_response_url())


def test_d6_transport_error_is_delivery_failure(patched_urlopen) -> None:
    """transport / 网络异常 → CHANNEL_DELIVERY_FAILED（不泄漏底层异常类型）。"""
    patched_urlopen(exc=OSError("network down"))
    with pytest.raises(ChannelDeliveryFailedError):
        WeComAdapter().deliver(_outbound_with_response_url())


@pytest.mark.parametrize("bad", ["", "   "])
def test_d7_compose_robot_reply_payload_rejects_empty_content(bad) -> None:
    with pytest.raises(ValueError):
        compose_robot_reply_payload(bad)


# ================================================================== E. 无 response_url → message/send 原样回归


def _self_built_channel(**overrides) -> Channel:
    config = {
        "token": TOKEN, "encoding_aes_key": AES_KEY, "corp_id": CORP_ID,
        "corpsecret": "corp-secret", "agentid": 1000002,
    }
    config.update(overrides)
    return Channel(name="wecom-out", type="wecom", id=1, config=config)


class _FakeChannelRepo:
    def __init__(self, channel: Channel) -> None:
        self._channel = channel

    def get_by_id(self, channel_id: int):
        return self._channel if self._channel.id == channel_id else None


class _FakeUoW:
    def __init__(self, channel: Channel) -> None:
        self.channels = _FakeChannelRepo(channel)

    def __enter__(self) -> "_FakeUoW":
        return self

    def __exit__(self, *args) -> bool:
        return False

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass


def test_e1_no_response_url_uses_legacy_message_send(patched_urlopen) -> None:
    """无 response_url → 既有 gettoken + message/send（text payload）完全不变。"""
    fake = patched_urlopen()
    adapter = WeComAdapter(uow_factory=lambda: _FakeUoW(_self_built_channel()))
    adapter.deliver(OutboundResponse(
        channel_id=1, external_conversation_id="zhangsan", content=CONTENT))

    assert [c["url"].split("?")[0] for c in fake.calls] == [
        "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
        "https://qyapi.weixin.qq.com/cgi-bin/message/send",
    ]
    send = fake.calls[1]
    assert "access_token=tok-123" in send["url"]
    assert json.loads(send["data"].decode("utf-8")) == compose_message_payload(
        "zhangsan", 1000002, CONTENT)


def test_e2_legacy_message_send_failure_remains_retryable(patched_urlopen) -> None:
    """既有 message/send 路径的失败保持 retryable=True（与 response_url 路径区分）。"""
    patched_urlopen(status=500)
    adapter = WeComAdapter(uow_factory=lambda: _FakeUoW(_self_built_channel()))
    with pytest.raises(ChannelDeliveryFailedError) as excinfo:
        adapter.deliver(OutboundResponse(
            channel_id=1, external_conversation_id="zhangsan", content=CONTENT))
    assert excinfo.value.retryable is True


def test_e3_response_url_failure_is_not_retryable(patched_urlopen) -> None:
    """response_url（一次性）失败 → retryable=False。"""
    patched_urlopen(status=500)
    with pytest.raises(ChannelDeliveryFailedError) as excinfo:
        WeComAdapter().deliver(_outbound_with_response_url())
    assert excinfo.value.retryable is False


# ================================================================== F. 失败语义落库（状态矩阵）


def _uow_factory(db_engine):
    factory = sessionmaker(bind=db_engine, class_=Session, expire_on_commit=False)
    return lambda: SqlAlchemyUnitOfWork(session_factory=factory)


def _seed_channel(db_engine) -> int:
    with _uow_factory(db_engine)() as uow:
        channel = uow.channels.create(Channel(name="robot", type="wecom"))
        uow.commit()
        return channel.id


def test_f1_response_url_delivery_failure_status_matrix(db_engine) -> None:
    """response_url Delivery 失败 → processing=succeeded / delivery=failed /
    stage=delivery / code=CHANNEL_DELIVERY_FAILED / retryable=false。"""
    channel_id = _seed_channel(db_engine)
    factory = sessionmaker(bind=db_engine, class_=Session, expire_on_commit=False)

    class _FailingDelivery:
        def execute(self, outbound) -> None:
            # 模拟 response_url 路径（一次性）失败
            raise ChannelDeliveryFailedError(
                channel_id=outbound.channel_id,
                external_conversation_id=outbound.external_conversation_id,
                retryable=False,
            )

    class _InnerSuccess:
        """Agent 已成功，仅 Delivery 失败（Case B）。"""

        def execute(self, inbound):
            from app.domain.runtime_execution_result import RuntimeExecutionResult
            from app.domain.session_resolution import CREATE_NEW
            _FailingDelivery().execute(OutboundResponse(
                channel_id=inbound.channel_id,
                external_conversation_id=inbound.external_conversation_id,
                content=CONTENT,
                metadata={RESPONSE_URL_METADATA_KEY: ROBOT_URL},
            ))
            return RuntimeExecutionResult(
                session_mode=CREATE_NEW, session_id=1,
                agent_response=AgentResponse(content=CONTENT),
            )

    coord = PersistingCoordinator(
        _InnerSuccess(), _uow_factory(db_engine))
    with pytest.raises(ChannelDeliveryFailedError):
        coord.execute(InboundInteraction(
            channel_id=channel_id, external_interaction_id="m-robot",
            external_conversation_id="zhangsan", external_identity_id="zhangsan",
            content="你好", metadata={RESPONSE_URL_METADATA_KEY: ROBOT_URL},
        ))

    with SqlAlchemyUnitOfWork(session_factory=factory) as uow:
        record = uow.interactions.get_by_id(1)
    assert record.processing_status == "succeeded"      # Agent 成功
    assert record.delivery_status == "failed"           # 投递失败
    assert record.failure_stage == "delivery"
    assert record.failure_code == "CHANNEL_DELIVERY_FAILED"
    assert record.retryable is False                    # 一次性 URL 不可重试


def test_f2_message_send_delivery_failure_still_retryable_true(db_engine) -> None:
    """对照组：既有 message/send Delivery 失败 → retryable=true（不被 12.8.2 改变）。"""
    channel_id = _seed_channel(db_engine)
    factory = sessionmaker(bind=db_engine, class_=Session, expire_on_commit=False)

    class _InnerSuccess:
        def execute(self, inbound):
            raise ChannelDeliveryFailedError(
                channel_id=inbound.channel_id,
                external_conversation_id=inbound.external_conversation_id,
            )

    coord = PersistingCoordinator(_InnerSuccess(), _uow_factory(db_engine))
    with pytest.raises(ChannelDeliveryFailedError):
        coord.execute(InboundInteraction(
            channel_id=channel_id, external_interaction_id="m-legacy",
            external_conversation_id="zhangsan", external_identity_id="zhangsan",
            content="你好",
        ))

    with SqlAlchemyUnitOfWork(session_factory=factory) as uow:
        record = uow.interactions.get_by_id(1)
    assert record.processing_status == "succeeded"
    assert record.delivery_status == "failed"
    assert record.retryable is True


def test_f3_expired_response_url_state_matrix_via_real_adapter(
        db_engine, patched_urlopen) -> None:
    """**真实链路回归（3B-2.6）**：Agent 成功 + 真实 response_url 过期。

    与 f1 的差别：f1 用替身模拟失败；本测试走**真实** `WeComAdapter.deliver`
    （真实 `_deliver_via_response_url` + 真实 HTTP 响应解析）→ 真实
    `PersistingCoordinator` 落库，验证状态矩阵是

        processing = SUCCEEDED     ← Agent 本身已完成任务
        delivery   = FAILED        ← 失败的是 Channel Delivery

    **绝不是** processing = FAILED（那会让 Observation 误报「Agent 失败」）。
    """
    channel_id = _seed_channel(db_engine)
    patched_urlopen(status=200, body=json.dumps(
        {"errcode": 60140, "errmsg": "invalid response code"}).encode("utf-8"))

    coordinator = RuntimeCoordinator(
        identity_resolution=_FakeIdentityResolution(),
        access_authorization=_FakeAuthorization(),
        routing=_FakeRouting(),
        session_resolution=_FakeSessionResolution(),
        session_creation=_FakeSessionCreation(),
        agent_dispatch=_FakeDispatch(AgentResponse(content=CONTENT)),
        delivery=DeliveryExecutor(channel_adapter=WeComAdapter()),   # 真实 Adapter
    )
    persisted = PersistingCoordinator(coordinator, _uow_factory(db_engine))

    with pytest.raises(ChannelDeliveryFailedError):
        persisted.execute(InboundInteraction(
            channel_id=channel_id, external_interaction_id="m-expired",
            external_conversation_id="zhangsan", external_identity_id="zhangsan",
            content="你好", metadata={RESPONSE_URL_METADATA_KEY: ROBOT_URL},
        ))

    factory = sessionmaker(bind=db_engine, class_=Session, expire_on_commit=False)
    with SqlAlchemyUnitOfWork(session_factory=factory) as uow:
        record = uow.interactions.get_by_id(1)
    assert record.processing_status == "succeeded"      # Agent 已完成
    assert record.delivery_status == "failed"           # 只有投递失败
    assert record.failure_stage == "delivery"
    assert record.failure_code == "CHANNEL_DELIVERY_FAILED"
    assert record.retryable is False                    # 一次性 URL 不可重试
