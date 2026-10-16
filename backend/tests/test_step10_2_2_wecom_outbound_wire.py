"""Step 10.2.2 Layer A —— WeCom outbound wire 单元测试（无 HTTP / 无 DB）。

覆盖（10.2.2 §测试结构 · A Outbound wire unit）：
    ✓ recipient mapping（external_conversation_id → touser，唯一映射点）
    ✓ content mapping / message type / payload 形状
    ✓ invalid config（缺 corpsecret / agentid / 非法 timeout）
    ✓ 无 uow_factory / 未知渠道（出站凭据解析 Fail Closed）
    ✓ passive reply wire capability（降级 helper：信封签名可重算 + 密文可解密回读）

不做：token 缓存 / 刷新 / 重试 / 去重（10.2.2 范围控制与已知限制）。
"""

import json

import pytest

from app.adapters.channels.wecom import (
    WeComAdapter,
    build_passive_reply,
    compose_message_payload,
    decrypt_message,
    parse_xml_fields,
)
from app.adapters.channels.wecom import compute_signature, encrypt_message
from app.application.errors import ChannelDeliveryFailedError
from app.domain.channel import Channel
from app.domain.outbound_response import OutboundResponse

TOKEN = "wire-token"
AES_KEY = "c" * 43
CORP_ID = "wwcorp-out"
CORPSECRET = "corp-secret-out"
AGENTID = 1000002


class _FakeChannelRepo:
    def __init__(self, channel: Channel | None) -> None:
        self._channel = channel

    def get_by_id(self, channel_id: int) -> Channel | None:
        if self._channel is not None and self._channel.id == channel_id:
            return self._channel
        return None


class _FakeUoW:
    def __init__(self, channel: Channel | None) -> None:
        self.channels = _FakeChannelRepo(channel)

    def __enter__(self) -> "_FakeUoW":
        return self

    def __exit__(self, *args) -> bool:
        return False

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass


def _channel(**overrides) -> Channel:
    config = {
        "token": TOKEN, "encoding_aes_key": AES_KEY, "corp_id": CORP_ID,
        "corpsecret": CORPSECRET, "agentid": AGENTID,
    }
    config.update(overrides)
    return Channel(name="wecom-out", type="wecom", id=11, config=config)


def _adapter(channel: Channel | None = None) -> WeComAdapter:
    return WeComAdapter(uow_factory=lambda: _FakeUoW(channel))


def _outbound(content: str = "给我一份夹持方案", conversation: str = "zhangsan") -> OutboundResponse:
    return OutboundResponse(channel_id=11, external_conversation_id=conversation,
                            content=content)


# ================================================================== A1. message payload


def test_a1_payload_mapping() -> None:
    """recipient/content/agentid 映射 —— external_conversation_id → touser。"""
    payload = compose_message_payload("zhangsan", AGENTID, "回复内容")
    assert payload == {
        "touser": "zhangsan", "msgtype": "text",
        "agentid": AGENTID, "text": {"content": "回复内容"},
    }
    # JSON 序列化稳定（中文不转义，与真实 wire 一致）
    body = json.dumps(payload, ensure_ascii=False)
    assert "回复内容" in body and '"touser"' in body


@pytest.mark.parametrize("bad", [
    ("", AGENTID, "x"), (" ", AGENTID, "x"),
    ("zhangsan", 0, "x"), ("zhangsan", -1, "x"), ("zhangsan", "1000002", "x"),
    ("zhangsan", AGENTID, ""), ("zhangsan", AGENTID, " "),
])
def test_a2_payload_rejects_invalid_input(bad) -> None:
    with pytest.raises(ValueError):
        compose_message_payload(*bad)


# ================================================================== A2. 出站 config Fail Closed


def test_b1_deliver_without_uow_factory_fails_closed() -> None:
    """出站能力未装配（无 uow_factory）→ ChannelDeliveryFailedError。"""
    with pytest.raises(ChannelDeliveryFailedError):
        WeComAdapter().deliver(_outbound())


def test_b2_deliver_unknown_channel_fails_closed() -> None:
    """channel 不存在 → ChannelDeliveryFailedError（Port 契约：无 ChannelNotFound）。"""
    with pytest.raises(ChannelDeliveryFailedError):
        _adapter(_channel()).deliver(
            OutboundResponse(channel_id=99999, external_conversation_id="x",
                             content="y"))


@pytest.mark.parametrize("missing", ["corp_id", "corpsecret"])
def test_b3_deliver_missing_outbound_credential_fails_closed(missing: str) -> None:
    """config 缺 corp_id / corpsecret → ChannelDeliveryFailedError。"""
    channel = _channel(**{missing: None})
    with pytest.raises(ChannelDeliveryFailedError):
        _adapter(channel).deliver(_outbound())


@pytest.mark.parametrize("bad_agentid", [None, 0, -5, "1000002"])
def test_b4_deliver_invalid_agentid_fails_closed(bad_agentid) -> None:
    with pytest.raises(ChannelDeliveryFailedError):
        _adapter(_channel(agentid=bad_agentid)).deliver(_outbound())


@pytest.mark.parametrize("bad_timeout", ["x", 0, -1])
def test_b5_deliver_invalid_timeout_fails_closed(bad_timeout) -> None:
    with pytest.raises(ChannelDeliveryFailedError):
        _adapter(_channel(timeout_s=bad_timeout)).deliver(_outbound())


def test_b6_inbound_unaffected_by_uow_factory() -> None:
    """构造注入 uow_factory 后，入站两能力行为不变（双 Port 同实例无串扰）。"""
    adapter = _adapter(_channel())
    plaintext = "plain-echo"
    encrypt = encrypt_message(plaintext, AES_KEY, CORP_ID)
    sig = compute_signature(TOKEN, "1409659813", "n", encrypt)
    assert adapter.verify_handshake(
        _channel(), signature=sig, timestamp="1409659813", nonce="n",
        echostr=encrypt) == plaintext


# ================================================================== A3. 被动回复 wire capability（降级 helper）


def test_c1_passive_reply_envelope_signature_recomputable() -> None:
    """信封 MsgSignature 可由 (token, TimeStamp, Nonce, Encrypt) 重算验证。"""
    body = build_passive_reply(
        "zhangsan", CORP_ID, "被动回复", 1700000000,
        encoding_aes_key=AES_KEY, token=TOKEN, nonce="n-1")
    fields = parse_xml_fields(body)
    assert compute_signature(TOKEN, fields["TimeStamp"], fields["Nonce"],
                             fields["Encrypt"]) == fields["MsgSignature"]


def test_c2_passive_reply_decrypts_to_plaintext_reply() -> None:
    """信封内 Encrypt 解密（receiveid=corp_id）→ 明文回复 XML 字段正确。"""
    body = build_passive_reply(
        "zhangsan", CORP_ID, "被动回复内容", 1700000000,
        encoding_aes_key=AES_KEY, token=TOKEN, nonce="n-1")
    encrypt = parse_xml_fields(body)["Encrypt"]
    plaintext = decrypt_message(encrypt, AES_KEY, CORP_ID)
    reply = parse_xml_fields(plaintext)
    assert reply["ToUserName"] == "zhangsan"      # 原消息发送者
    assert reply["FromUserName"] == CORP_ID        # 企业主体
    assert reply["MsgType"] == "text"
    assert reply["Content"] == "被动回复内容"
