"""Step 10.2.1 Layer 1 —— WeCom wire 单元测试。

完全不需要 HTTP / DB / Runtime / FastAPI：直接测 `WeComAdapter` 与模块级纯函数
（SHA1 验签 / AES 加解密 / XML 解析 / 字段映射 / 失败语义）。

覆盖（10.2.1 §测试分层 · Layer 1）：
    ✓ SHA1 signature        ✓ AES decrypt / encrypt round-trip
    ✓ echostr decrypt       ✓ XML parsing
    ✓ text message mapping  ✓ invalid signature
    ✓ invalid ciphertext    ✓ malformed XML
    ✓ unsupported msg type  ✓ config 缺验签材料
"""

import ast
import hashlib
import json
from pathlib import Path

import pytest

from app.adapters.channels.wecom import (
    WeComAdapter,
    compute_signature,
    decrypt_message,
    encrypt_message,
    parse_xml_fields,
)
from app.domain.channel import Channel
from app.domain.inbound_interaction import InboundInteraction
from app.repositories.channel_inbound import (
    InboundSignatureError,
    InboundChannelAdapter,
    InboundChannelConfigError,
    InboundFormatError,
)

TOKEN = "unit-test-token"
AES_KEY = "a" * 43           # EncodingAESKey 规范长度（43 字符 base64）
CORP_ID = "wwcorp001"
TIMESTAMP = "1409659813"
NONCE = "nonce-1"

TEXT_XML = (
    "<xml><ToUserName><![CDATA[wwcorp001]]></ToUserName>"
    "<FromUserName><![CDATA[zhangsan]]></FromUserName>"
    "<CreateTime>1348831860</CreateTime>"
    "<MsgType><![CDATA[text]]></MsgType>"
    "<Content><![CDATA[帮我查一下夹具规格]]></Content>"
    "<MsgId>1234567890123456</MsgId>"
    "<AgentID>1</AgentID></xml>"
)
IMAGE_XML = (
    "<xml><ToUserName><![CDATA[wwcorp001]]></ToUserName>"
    "<FromUserName><![CDATA[zhangsan]]></FromUserName>"
    "<CreateTime>1348831860</CreateTime>"
    "<MsgType><![CDATA[image]]></MsgType>"
    "<PicUrl><![CDATA[http://example/p.jpg]]></PicUrl>"
    "<MediaId><![CDATA[media-1]]></MediaId>"
    "<MsgId>999888777</MsgId>"
    "<AgentID>1</AgentID></xml>"
)


def _channel(**overrides) -> Channel:
    config = {"token": TOKEN, "encoding_aes_key": AES_KEY, "corp_id": CORP_ID}
    config.update(overrides)
    return Channel(name="wecom-1", type="wecom", id=7, config=config)


def _handshake_params(plaintext_echo: str, *, timestamp: str = TIMESTAMP,
                      nonce: str = NONCE, token: str = TOKEN,
                      receiveid: str = CORP_ID) -> dict[str, str]:
    """明文 echostr → WeCom GET 事实（msg_signature / timestamp / nonce / echostr 密文）。"""
    encrypt = encrypt_message(plaintext_echo, AES_KEY, receiveid)
    return {
        "msg_signature": compute_signature(token, timestamp, nonce, encrypt),
        "timestamp": timestamp, "nonce": nonce, "echostr": encrypt,
    }


def _message_params(plaintext_xml: str, *, timestamp: str = TIMESTAMP,
                    nonce: str = NONCE, token: str = TOKEN,
                    receiveid: str = CORP_ID) -> dict[str, str]:
    """明文消息 XML → WeCom POST 事实 + 加密后的 body。"""
    encrypt = encrypt_message(plaintext_xml, AES_KEY, receiveid)
    outer = (
        f"<xml><ToUserName><![CDATA[{CORP_ID}]]></ToUserName>"
        f"<Encrypt><![CDATA[{encrypt}]]></Encrypt>"
        "<AgentID><![CDATA[1000002]]></AgentID></xml>"
    )
    return {
        "msg_signature": compute_signature(token, timestamp, nonce, encrypt),
        "timestamp": timestamp, "nonce": nonce, "body": outer,
    }


# ================================================================== A. 验签算法


def test_a1_signature_known_vector() -> None:
    """SHA1(sort(token,timestamp,nonce,encrypt) 拼接)：固定向量锁定算法。"""
    expected = hashlib.sha1(b"1ent").hexdigest()   # sorted(["t","1","n","e"]) = 1,e,n,t
    assert compute_signature("t", "1", "n", "e") == expected


def test_a2_encrypt_decrypt_round_trip() -> None:
    """加密 → 解密 round-trip（含中文，验证 PKCS7(32) + receiveid 结构）。"""
    plaintext = "帮我查一下夹具规格 hello 123"
    encrypt = encrypt_message(plaintext, AES_KEY, CORP_ID)
    assert decrypt_message(encrypt, AES_KEY, CORP_ID) == plaintext


def test_a3_decrypt_rejects_wrong_corp_id() -> None:
    """ReceiveId 与 corp_id 不符 → InboundSignatureError（跨企业密文不可读）。"""
    encrypt = encrypt_message(TEXT_XML, AES_KEY, CORP_ID)
    with pytest.raises(InboundSignatureError):
        decrypt_message(encrypt, AES_KEY, "ww-other-corp")


def test_a4_decrypt_rejects_broken_ciphertext() -> None:
    """结构性损坏的密文 → InboundSignatureError（绝不返回半成品明文）。

    注意：AES-CBC 本身无认证，密文完整性由 msg_signature（SHA1）在更早一步保证；
    decrypt_message 负责的是「能解就解、解不开就 Fail Closed」——截断 / padding
    非法 / msg_len 错乱 / ReceiveId 不符全部收敛为 InboundSignatureError。
    """
    encrypt = encrypt_message(TEXT_XML, AES_KEY, CORP_ID)
    with pytest.raises(InboundSignatureError):
        decrypt_message(encrypt[: len(encrypt) // 2], AES_KEY, CORP_ID)   # 截断
    with pytest.raises(InboundSignatureError):
        # 篡改最后一个密文块（对应明文的 receiveid/pad 区域）
        bad = encrypt[:-8] + ("B" if encrypt[-8] != "B" else "C") + encrypt[-7:]
        decrypt_message(bad, AES_KEY, CORP_ID)


def test_a5_aes_key_length_guard() -> None:
    """EncodingAESKey 长度不是 43 → InboundChannelConfigError（配置错误而非攻击）。"""
    with pytest.raises(InboundChannelConfigError):
        decrypt_message("x" * 64, "short-key", CORP_ID)


def test_a6_parse_xml_fields() -> None:
    """XML → {tag: text}（CDATA 自动展开）；畸形 → InboundFormatError。"""
    fields = parse_xml_fields(TEXT_XML)
    assert fields["MsgType"] == "text"
    assert fields["Content"] == "帮我查一下夹具规格"
    assert fields["MsgId"] == "1234567890123456"
    with pytest.raises(InboundFormatError):
        parse_xml_fields("this is not xml")
    with pytest.raises(InboundFormatError):
        parse_xml_fields("<root><a>1</a></root>")   # 根元素不是 <xml>


# ================================================================== B. 握手（verify_handshake）


def test_b1_handshake_returns_plaintext_echostr() -> None:
    """合法握手 → 返回明文 echostr（HTTP 边界原样回给渠道）。"""
    adapter = WeComAdapter()
    params = _handshake_params("echo-plaintext-9182")
    result = adapter.verify_handshake(
        _channel(),
        signature=params["msg_signature"], timestamp=params["timestamp"],
        nonce=params["nonce"], echostr=params["echostr"],
    )
    assert result == "echo-plaintext-9182"


def test_b2_handshake_rejects_bad_signature() -> None:
    """签名不符 → InboundSignatureError（不可信请求）。"""
    adapter = WeComAdapter()
    params = _handshake_params("echo-plaintext")
    with pytest.raises(InboundSignatureError):
        adapter.verify_handshake(
            _channel(),
            signature=params["msg_signature"][:-1] + ("0" if params["msg_signature"][-1] != "0" else "1"),
            timestamp=params["timestamp"], nonce=params["nonce"],
            echostr=params["echostr"],
        )


# ================================================================== C. 消息解析（parse_message）


def test_c1_text_message_mapped_to_contract() -> None:
    """text 消息 → InboundInteraction 全字段归一化（含单聊 conversation 裁决）。"""
    adapter = WeComAdapter()
    params = _message_params(TEXT_XML)
    inbound = adapter.parse_message(
        _channel(),
        signature=params["msg_signature"], timestamp=params["timestamp"],
        nonce=params["nonce"], body=params["body"],
    )
    assert isinstance(inbound, InboundInteraction)
    assert inbound.channel_id == 7
    assert inbound.external_interaction_id == "1234567890123456"   # MsgId
    assert inbound.external_identity_id == "zhangsan"              # FromUserName
    assert inbound.external_conversation_id == "zhangsan"          # 单聊裁决（10.2.1）
    assert inbound.content == "帮我查一下夹具规格"


def test_c2_parse_rejects_bad_signature() -> None:
    """POST 签名不符 → InboundSignatureError。"""
    adapter = WeComAdapter()
    params = _message_params(TEXT_XML)
    with pytest.raises(InboundSignatureError):
        adapter.parse_message(
            _channel(),
            signature="0" * 40, timestamp=params["timestamp"],
            nonce=params["nonce"], body=params["body"],
        )


def test_c3_parse_rejects_body_without_encrypt() -> None:
    """body 缺 Encrypt 节点 → InboundFormatError（可信但格式非法）。"""
    adapter = WeComAdapter()
    with pytest.raises(InboundFormatError):
        adapter.parse_message(
            _channel(),
            signature="s", timestamp=TIMESTAMP, nonce=NONCE,
            body="<xml><ToUserName>x</ToUserName></xml>",
        )


def test_c4_parse_rejects_malformed_xml() -> None:
    """畸形 body → InboundFormatError。"""
    adapter = WeComAdapter()
    with pytest.raises(InboundFormatError):
        adapter.parse_message(
            _channel(), signature="s", timestamp=TIMESTAMP,
            nonce=NONCE, body="not-xml-at-all",
        )


def test_c5_unsupported_msg_type_returns_none() -> None:
    """image（及未来 voice/video/event）→ None = acknowledge-ignore（MVP 只支持 text）。"""
    adapter = WeComAdapter()
    params = _message_params(IMAGE_XML)
    result = adapter.parse_message(
        _channel(),
        signature=params["msg_signature"], timestamp=params["timestamp"],
        nonce=params["nonce"], body=params["body"],
    )
    assert result is None


def test_c6_parse_rejects_empty_text_content() -> None:
    """text 消息 Content 为空 → InboundFormatError（wire 完整性校验，早于 Runtime）。"""
    adapter = WeComAdapter()
    empty = TEXT_XML.replace("帮我查一下夹具规格", "")
    params = _message_params(empty)
    with pytest.raises(InboundFormatError):
        adapter.parse_message(
            _channel(),
            signature=params["msg_signature"], timestamp=params["timestamp"],
            nonce=params["nonce"], body=params["body"],
        )


# ================================================================== D. 配置 Fail Closed


@pytest.mark.parametrize("missing", ["token", "encoding_aes_key", "corp_id"])
def test_d1_missing_verification_material_fails_closed(missing: str) -> None:
    """config 缺任一验签材料 → InboundChannelConfigError（HTTP 500 语义）。"""
    adapter = WeComAdapter()
    channel = _channel(**{missing: None})
    with pytest.raises(InboundChannelConfigError):
        adapter.parse_message(
            channel, signature="s", timestamp=TIMESTAMP, nonce=NONCE,
            body="<xml><Encrypt>e</Encrypt></xml>")


def test_d2_unpersisted_channel_rejected() -> None:
    """未持久化的 Channel（id=None）不能承接入站消息。"""
    adapter = WeComAdapter()
    channel = Channel(name="wecom-1", type="wecom", id=None,
                      config={"token": TOKEN, "encoding_aes_key": AES_KEY, "corp_id": CORP_ID})
    with pytest.raises(InboundChannelConfigError):
        adapter.parse_message(
            channel, signature="s", timestamp=TIMESTAMP, nonce=NONCE,
            body="<xml><Encrypt>e</Encrypt></xml>")


# ================================================================== E. Port 一致性 / wire logic 隔离


def test_e1_adapter_satisfies_port() -> None:
    """WeComAdapter 满足 runtime_checkable 的 InboundChannelAdapter 协议。"""
    assert isinstance(WeComAdapter(), InboundChannelAdapter)


# ================================================================== F. ReceiveId 变体（12.7 真实验收补充）
#
# 实证：自建应用回调 receiveid == corp_id；企业微信智能机器人回调 receiveid
# 为**空串**。期望值裁决：`config.get("receiveid")`——键缺省 → corp_id（现状
# 语义不变）；键存在（含显式空串）→ 按配置值校验，空串绝不当作缺省值。


ROBOT_XML = (
    "<xml><ToUserName><![CDATA[wwcorp001]]></ToUserName>"
    "<FromUserName><![CDATA[zhangsan]]></FromUserName>"
    "<CreateTime>1348831860</CreateTime>"
    "<MsgType><![CDATA[text]]></MsgType>"
    "<Content><![CDATA[机器人单聊消息]]></Content>"
    "<MsgId>2234567890123456</MsgId>"
    "<AgentID>1</AgentID></xml>"
)


def test_f1_robot_wire_empty_receiveid_handshake_passes() -> None:
    """智能机器人 wire：receiveid="" 配置 + 空串 ReceiveId 密文 → 握手通过。"""
    adapter = WeComAdapter()
    channel = _channel(receiveid="")
    params = _handshake_params("echo-robot-1", receiveid="")
    result = adapter.verify_handshake(
        channel,
        signature=params["msg_signature"], timestamp=params["timestamp"],
        nonce=params["nonce"], echostr=params["echostr"],
    )
    assert result == "echo-robot-1"


def test_f2_robot_wire_empty_receiveid_message_maps_to_contract() -> None:
    """智能机器人 wire：空串 ReceiveId 的 text 消息 → InboundInteraction 正常归一化。"""
    adapter = WeComAdapter()
    channel = _channel(receiveid="")
    params = _message_params(ROBOT_XML, receiveid="")
    inbound = adapter.parse_message(
        channel,
        signature=params["msg_signature"], timestamp=params["timestamp"],
        nonce=params["nonce"], body=params["body"],
    )
    assert isinstance(inbound, InboundInteraction)
    assert inbound.external_identity_id == "zhangsan"
    assert inbound.external_conversation_id == "zhangsan"
    assert inbound.content == "机器人单聊消息"


def test_f3_explicit_nonempty_receiveid_validated_against_config() -> None:
    """显式非空 receiveid → 按配置值校验：匹配通过、不符拒绝。"""
    adapter = WeComAdapter()
    channel = _channel(receiveid="wrk-robot-001")
    params = _handshake_params("echo-custom-1", receiveid="wrk-robot-001")
    assert adapter.verify_handshake(
        channel,
        signature=params["msg_signature"], timestamp=params["timestamp"],
        nonce=params["nonce"], echostr=params["echostr"],
    ) == "echo-custom-1"
    # 实际 ReceiveId 与配置不符 → 拒绝
    mismatch = _handshake_params("echo-custom-2", receiveid="ww-other")
    with pytest.raises(InboundSignatureError):
        adapter.verify_handshake(
            channel,
            signature=mismatch["msg_signature"], timestamp=mismatch["timestamp"],
            nonce=mismatch["nonce"], echostr=mismatch["echostr"],
        )


def test_f4_empty_receiveid_not_treated_as_default() -> None:
    """空串绝不当作缺省值：未配置 receiveid（corp_id 语义）时，空串密文被拒。"""
    adapter = WeComAdapter()
    params = _handshake_params("echo-robot-2", receiveid="")
    with pytest.raises(InboundSignatureError):
        adapter.verify_handshake(
            _channel(),
            signature=params["msg_signature"], timestamp=params["timestamp"],
            nonce=params["nonce"], echostr=params["echostr"],
        )


def test_f5_receiveid_round_trip_decrypt() -> None:
    """decrypt_message 期望值 = 空串 / 非空自定义值均可 round-trip。"""
    encrypt_empty = encrypt_message("payload-a", AES_KEY, "")
    assert decrypt_message(encrypt_empty, AES_KEY, "") == "payload-a"
    encrypt_custom = encrypt_message("payload-b", AES_KEY, "wrk-robot-001")
    assert decrypt_message(encrypt_custom, AES_KEY, "wrk-robot-001") == "payload-b"
    with pytest.raises(InboundSignatureError):
        decrypt_message(encrypt_custom, AES_KEY, "")


# ================================================================== G. 智能机器人 JSON wire（12.7 真实验收补充）
#
# 官方文档 101033 / 100719 实证：智能机器人 POST 外层信封与解密明文均为 JSON
# （区别于自建应用 XML）。信封 {"encrypt": ...}；明文报文
# {msgid, aibotid, chattype, from.userid, response_url, msgtype, text.content}。


def _robot_message_params(payload: dict, *, timestamp: str = TIMESTAMP,
                          nonce: str = NONCE, token: str = TOKEN,
                          receiveid: str = "") -> dict[str, str]:
    """机器人明文 JSON 报文 → WeCom POST 事实（JSON 信封 + body）。"""
    encrypt = encrypt_message(json.dumps(payload, ensure_ascii=False),
                              AES_KEY, receiveid)
    body = json.dumps({"encrypt": encrypt}, ensure_ascii=False)
    return {
        "msg_signature": compute_signature(token, timestamp, nonce, encrypt),
        "timestamp": timestamp, "nonce": nonce, "body": body,
    }


ROBOT_TEXT = {
    "msgid": "CAIQ16HMjQYY/NGagIOAgAMgq4KM0AI=",
    "aibotid": "AIBOTID",
    "chattype": "single",
    "from": {"userid": "zhangsan"},
    "response_url": "RESPONSEURL",
    "msgtype": "text",
    "text": {"content": "Hello"},
}


def test_g1_robot_json_text_message_maps_to_contract() -> None:
    """JSON 信封 + JSON 明文（single/text）→ InboundInteraction 全字段归一化。"""
    adapter = WeComAdapter()
    channel = _channel(receiveid="")
    params = _robot_message_params(ROBOT_TEXT)
    inbound = adapter.parse_message(
        channel,
        signature=params["msg_signature"], timestamp=params["timestamp"],
        nonce=params["nonce"], body=params["body"],
    )
    assert isinstance(inbound, InboundInteraction)
    assert inbound.channel_id == 7
    assert inbound.external_interaction_id == "CAIQ16HMjQYY/NGagIOAgAMgq4KM0AI="
    assert inbound.external_identity_id == "zhangsan"
    assert inbound.external_conversation_id == "zhangsan"   # 单聊裁决延续
    assert inbound.content == "Hello"


def test_g2_robot_json_non_text_ignored() -> None:
    """机器人 image（及未来 voice/file/video/stream/mixed）→ None（acknowledge-ignore）。"""
    adapter = WeComAdapter()
    channel = _channel(receiveid="")
    payload = {**ROBOT_TEXT, "msgtype": "image",
               "image": {"url": "https://example/p.jpg"}}
    del payload["text"]
    params = _robot_message_params(payload)
    result = adapter.parse_message(
        channel,
        signature=params["msg_signature"], timestamp=params["timestamp"],
        nonce=params["nonce"], body=params["body"],
    )
    assert result is None


def test_g3_robot_json_group_chat_ignored() -> None:
    """群聊（chattype=group）→ None（10.2.1 群聊 ChatId 留未来裁决不猜）。"""
    adapter = WeComAdapter()
    channel = _channel(receiveid="")
    payload = {**ROBOT_TEXT, "chattype": "group", "chatid": "CHATID"}
    params = _robot_message_params(payload)
    result = adapter.parse_message(
        channel,
        signature=params["msg_signature"], timestamp=params["timestamp"],
        nonce=params["nonce"], body=params["body"],
    )
    assert result is None


@pytest.mark.parametrize("mutate", [
    lambda p: {**p, "msgid": ""},
    lambda p: {**p, "msgid": None},
    lambda p: {**p, "from": {}},
    lambda p: {**p, "text": {"content": ""}},
    lambda p: {k: v for k, v in p.items() if k != "text"},
])
def test_g4_robot_json_missing_required_fields_fail_closed(mutate) -> None:
    """msgid / from.userid / text.content 缺失 → InboundFormatError（Fail Closed）。"""
    adapter = WeComAdapter()
    channel = _channel(receiveid="")
    params = _robot_message_params(mutate(ROBOT_TEXT))
    with pytest.raises(InboundFormatError):
        adapter.parse_message(
            channel,
            signature=params["msg_signature"], timestamp=params["timestamp"],
            nonce=params["nonce"], body=params["body"],
        )


def test_g5_robot_json_envelope_missing_encrypt() -> None:
    """JSON 信封缺 encrypt 字段 → InboundFormatError。"""
    adapter = WeComAdapter()
    channel = _channel(receiveid="")
    with pytest.raises(InboundFormatError):
        adapter.parse_message(
            channel, signature="s", timestamp=TIMESTAMP, nonce=NONCE,
            body=json.dumps({"other": "x"}))


def test_g6_robot_json_bad_signature_rejected() -> None:
    """JSON 信封签名不符 → InboundSignatureError（与 XML 同一验签路径）。"""
    adapter = WeComAdapter()
    channel = _channel(receiveid="")
    params = _robot_message_params(ROBOT_TEXT)
    with pytest.raises(InboundSignatureError):
        adapter.parse_message(
            channel,
            signature="0" * 40, timestamp=params["timestamp"],
            nonce=params["nonce"], body=params["body"],
        )


def test_g7_xml_wire_unchanged_alongside_json() -> None:
    """双格式并存：XML 自建应用消息（C1 同构）与 JSON 机器人消息互不干扰。"""
    adapter = WeComAdapter()
    xml_params = _message_params(TEXT_XML)
    xml_inbound = adapter.parse_message(
        _channel(),
        signature=xml_params["msg_signature"], timestamp=xml_params["timestamp"],
        nonce=xml_params["nonce"], body=xml_params["body"],
    )
    assert isinstance(xml_inbound, InboundInteraction)
    assert xml_inbound.content == "帮我查一下夹具规格"
    channel = _channel(receiveid="")
    robot_params = _robot_message_params(ROBOT_TEXT)
    robot_inbound = adapter.parse_message(
        channel,
        signature=robot_params["msg_signature"], timestamp=robot_params["timestamp"],
        nonce=robot_params["nonce"], body=robot_params["body"],
    )
    assert isinstance(robot_inbound, InboundInteraction)
    assert robot_inbound.content == "Hello"


def test_e2_wire_logic_isolated_in_adapter() -> None:
    """cryptography 依赖只出现在 adapters/channels/wecom.py —— wire logic 不泄漏。"""
    app_dir = Path(__file__).resolve().parents[1] / "app"
    offenders = []
    for py_file in sorted(app_dir.rglob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module]
            if any(m == "cryptography" or m.startswith("cryptography.") for m in mods):
                rel = py_file.relative_to(app_dir)
                if rel.as_posix() != "adapters/channels/wecom.py":
                    offenders.append(f"{rel}")
    assert not offenders, f"cryptography 被引入 Adapter 之外：{offenders}"
