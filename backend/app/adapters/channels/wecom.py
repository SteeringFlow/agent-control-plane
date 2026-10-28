"""WeComAdapter —— Inbound + Outbound 双向渠道 Adapter（Step 10.2.1 / 10.2.2）。

位置：`app/adapters/channels/wecom.py`。实现两个 Port：
- 入站 `InboundChannelAdapter`（Port 在 `app/repositories/channel_inbound.py`）；
- 出站 `ChannelAdapter`（Port 在 `app/repositories/channel_adapter.py`，
  10.2.2 起的**正式 Delivery 路径**）。
本模块不被 application/runtime import（Runtime 对 WeCom 的存在一无所知）。

**全部 WeCom wire logic 归属本模块**（10.2.1/10.2.2 Responsibility Map）：

    入站：SHA1 验签 / AES-CBC 解密 / XML 解析 / echostr 握手 / 字段映射
              ↓ InboundInteraction（8.1 契约）
    出站：两条互斥路径（12.8.2）
      - 智能机器人：POST response_url（markdown；无 access_token）——
        回执地址来自 `OutboundResponse.metadata["response_url"]`
        （channel-originated delivery context，由入站解析置入）；
      - 自建应用：access-token 获取 / message payload 构造 / message/send 调用
        （无 response_url 时的既有路径，行为完全不变）。
              ↓（deliver(OutboundResponse) -> None，与 Port 语义天然匹配：
                 「把消息交付给 Channel」，而非「生成待 HTTP 层取走的应答」）

企业微信（自建应用）回调 wire（10.2.1 校准并锁定）：

1. **GET URL 验证**（verify_handshake）：
   query: msg_signature / timestamp / nonce / echostr；
   signature = SHA1(sort(token, timestamp, nonce, echostr))；
   明文 = AES-CBC-Decrypt(base64decode(echostr))，结构
   random(16B) + msg_len(4B BE) + msg + receiveid，receiveid 必须 == corp_id；
   HTTP 应答 = 明文 echostr。
2. **POST 消息**（parse_message）——双格式（12.7 真实验收补充）：
   - 自建应用（10.2.1）：外层 XML `<Encrypt>` → 验签 → AES 解密 → 内层 XML；
   - 智能机器人（官方 101033/100719）：外层 JSON `{"encrypt": ...}` → 验签 →
     AES 解密（receiveid 空串）→ 明文 JSON 报文
     （msgid / aibotid / chattype / from.userid / response_url / msgtype / text.content）。
   判定：body / 明文去空白后以 `{` 开头 → JSON，否则 XML；验签与解密路径共用。
3. **只支持 text 单聊**（MVP 边界延续）：XML 的 image/voice/video/event 与 JSON 的
   group 会话 / image/mixed/voice/file/video/stream → 返回 None
   （acknowledge-ignore，HTTP 侧 200 空体）。不为「未来支持图片/群聊」提前扩
   InboundInteraction（Runtime MVP Freeze 不重开）。

字段映射裁决（10.2.1 §会话语义）：
- `external_interaction_id`  = MsgId（幂等/渠道去重依据，8.1）；
- `external_identity_id`     = FromUserName（WeCom userid，8.1 明示）；
- `external_conversation_id` = FromUserName —— 企业微信**自建应用**消息回调 wire
  只有「用户 ↔ 应用」单聊形态（payload 无群会话 ID 字段）；群聊 ChatId 属于
  机器人/会话事件模型，当前 wire 不出现 → **不猜**。若未来引入群回调，按
  「群消息 → ChatId」单独裁决，不改动本映射的单聊语义。

凭据来源（10.2.1 §Credential 裁决）：只消费 `Channel.config`：

    入站（10.2.1）：
    {"token": "...", "encoding_aes_key": "...", "corp_id": "...",
     "receiveid": ...}   # 可选（12.7 真实验收补充，见下）

**ReceiveId 变体（12.7 Real Product Acceptance 实证补充）**：
解密明文结构中的 receiveid 段按渠道形态取值不同——
- 自建应用回调：receiveid == corp_id（10.2.1 冻结语义，缺省行为不变）；
- 智能机器人回调：receiveid 为**空串**（真实 wire 实证）。
期望值裁决：`config.get("receiveid")`——键缺省 → corp_id（现状语义）；
键存在（含显式空串）→ 按配置值校验（空串绝不当作缺省值）。
    出站（10.2.2 新增，WeCom outbound capability 的渠道凭据）：
    {"corpsecret": "...", "agentid": 1000002,            # message/send 必需
     "api_base": "...",   # 可选，默认官方域名；测试替身 / 私有化部署用
     "timeout_s": 10}     # 可选

`corpsecret` **不属于 Runtime Contract，也不修改 Channel domain schema**——MVP
暂存于 `Channel.config`，credential_ref 化留待生产化决策（与 10.2.1 既有安全
裁决一致，此处显式记录而非偷偷塞入）。

缺任一项 → 入站抛 `InboundChannelConfigError`、出站抛
`ChannelDeliveryFailedError`（出站 Port 契约：不新造错误码，一切底层失败统一
映射 CHANNEL_DELIVERY_FAILED）（Fail Closed）。

**10.2.2 范围控制**：不做 access_token 缓存 / token 刷新框架 / credential
manager / OAuth / 重试 / 异步——每次 deliver 独立取 token（与 10.1.3 的
「不缓存连接」同一纪律）；等真实测试证明需要再单独演进。
**已知限制（10.2.2 记录，不做去重）**：WeCom 5s 超时重推 + CP 无入站幂等 →
重复 Runtime 执行；MsgId（external_interaction_id）已承载去重依据，CP 端
deduplication 一旦要做将进入 Interaction persistence / Execution semantics
领域，不在 Channel Adapter wire 层解决。
**被动回复（passive reply）**：降级为 wire capability / compatibility helper
（`build_passive_reply` 纯函数，仅测试与未来兼容场景），**不是**
`ChannelAdapter.deliver()` 的实现路径——deliver 的语义是交付，不是「生成一个
稍后由 HTTP 层取走的 Response」。
"""

import base64
import binascii
import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app.application.errors import ChannelDeliveryFailedError
from app.domain.agent_probe_snapshot import AgentProbeSnapshot
from app.domain.artifact_reference import render_outbound_content
from app.domain.channel import Channel
from app.domain.inbound_interaction import (
    CHAT_TYPE_METADATA_KEY,
    CHAT_TYPE_SINGLE,
    InboundInteraction,
)
from app.domain.outbound_response import OutboundResponse
from app.repositories.channel_inbound import (
    InboundSignatureError,
    InboundChannelConfigError,
    InboundFormatError,
)
from app.repositories.channel_adapter import ChannelAdapter
from app.repositories.unit_of_work import UnitOfWork

# PKCS7 按 32 字节块填充（企业微信规范；AES 分组本身是 16B，
# 按最后一字节值 unpad 对 16/32 两种填充均正确）。
_PKCS7_BLOCK = 32
_MSG_TYPE_TEXT = "text"

# ---- 出站（10.2.2）----
DEFAULT_API_BASE = "https://qyapi.weixin.qq.com"
DEFAULT_OUTBOUND_TIMEOUT_S = 10.0

# 智能机器人主动回复的**回执地址**承载键（12.8.2）：
# 只作为 OutboundResponse.metadata 中的 channel-specific delivery context——
# 不新增 InboundInteraction / OutboundResponse 的契约字段面。
RESPONSE_URL_METADATA_KEY = "response_url"

# ---- Provider Verify（M6.6-F3）----
#: 探针标签（与 ACP 探针的 "tencent-acp-sse" / "minimal" 同族，供用户知道走了哪条路径）。
WECOM_PROBE_LABEL = "wecom"
#: 用户可见的失败文案（**泛化**——不含 corpsecret / access_token / HTTP 状态 / URL）。
_PROBE_DETAIL_UNVERIFIED = "wecom connection could not be verified"


# ================================================================== 纯函数（wire 单测直测）


def compute_signature(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    """WeCom 验签算法：SHA1(sort(token, timestamp, nonce, encrypt) 拼接)。"""
    joined = "".join(sorted([token, timestamp, nonce, encrypt]))
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()


def encrypt_message(plaintext: str, encoding_aes_key: str, corp_id: str) -> str:
    """明文 → WeCom 密文（base64）。

    结构：random(16B) + msg_len(4B BE) + msg + receiveid，PKCS7(32) 填充后
    AES-CBC 加密（IV = AESKey 前 16B）。当前供测试构造合法 wire 使用；
    未来出站（加密被动回复，10.3+）复用同一实现。
    """
    key = _aes_key(encoding_aes_key)
    msg = plaintext.encode("utf-8")
    data = os.urandom(16) + len(msg).to_bytes(4, "big") + msg + corp_id.encode("utf-8")
    amount = _PKCS7_BLOCK - (len(data) % _PKCS7_BLOCK)
    if amount == 0:
        amount = _PKCS7_BLOCK
    data += bytes([amount]) * amount
    encryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
    encrypted = encryptor.update(data) + encryptor.finalize()
    return base64.b64encode(encrypted).decode("ascii")


def decrypt_message(encrypt: str, encoding_aes_key: str, expected_receiveid: str) -> str:
    """WeCom 密文 → 明文；不可信密文 → InboundSignatureError；ReceiveId 不符 → InboundSignatureError。

    第三参数是**期望的 ReceiveId**（自建应用 = corp_id；智能机器人 = ""，见
    模块 docstring 的 ReceiveId 变体说明）。
    """
    key = _aes_key(encoding_aes_key)
    try:
        ciphertext = base64.b64decode(encrypt, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InboundSignatureError("密文不是合法 base64") from exc
    if len(ciphertext) == 0 or len(ciphertext) % 16 != 0:
        raise InboundSignatureError("密文长度不是 AES 分组的整数倍")
    decryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).decryptor()
    raw = decryptor.update(ciphertext) + decryptor.finalize()

    pad = raw[-1]
    if not 1 <= pad <= _PKCS7_BLOCK or raw[-pad:] != bytes([pad]) * pad:
        raise InboundSignatureError("PKCS7 填充非法（密文被篡改或密钥不匹配）")
    raw = raw[:-pad]

    if len(raw) < 20:
        raise InboundSignatureError("解密结果长度不足（缺少 random/msg_len 头）")
    msg_len = int.from_bytes(raw[16:20], "big")
    if 20 + msg_len > len(raw):
        raise InboundSignatureError("msg_len 与实际数据不一致")
    msg, receiveid = raw[20:20 + msg_len], raw[20 + msg_len:]
    if receiveid.decode("utf-8", "replace") != expected_receiveid:
        raise InboundSignatureError("ReceiveId 与期望值不符")
    return msg.decode("utf-8")


def parse_xml_fields(xml_text: str) -> dict[str, str]:
    """WeCom XML（明文或密文外层）→ {tag: text}；畸形 XML → InboundFormatError。"""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise InboundFormatError("XML 解析失败") from exc
    if root.tag != "xml":
        raise InboundFormatError(f"XML 根元素不是 <xml>：{root.tag!r}")
    return {child.tag: (child.text or "") for child in root}


# ================================================================== 出站纯函数（10.2.2，wire 单测直测）


def compose_message_payload(touser: str, agentid: int, content: str) -> dict:
    """WeCom 发送应用消息（text）payload —— recipient 映射的唯一裁决点。

    `touser` = `OutboundResponse.external_conversation_id`（10.2.1 单聊裁决：
    conversation 即 WeCom userid）。Runtime 不知道 touser；WeCom 不知道
    external_conversation_id —— 映射只发生在本函数。
    """
    if not isinstance(touser, str) or not touser.strip():
        raise ValueError("touser must be a non-empty str")
    if not isinstance(agentid, int) or agentid <= 0:
        raise ValueError("agentid must be a positive int")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("content must be a non-empty str")
    return {
        "touser": touser,
        "msgtype": "text",
        "agentid": agentid,
        "text": {"content": content},
    }


def compose_robot_reply_payload(content: str) -> dict:
    """智能机器人主动回复 payload（官方 101138）—— content → markdown。

    官方主动回复**没有 `text` 消息类型**：只支持 `markdown` 与 `template_card`，
    纯文本以 markdown 承载（`content` 原样填入，不做转义/富文本推断）。
    不添加未经官方确认的字段（12.8.1 事实面）。
    """
    if not isinstance(content, str) or not content.strip():
        raise ValueError("content must be a non-empty str")
    return {"msgtype": "markdown", "markdown": {"content": content}}


def _response_url_from(outbound: OutboundResponse) -> str | None:
    """取智能机器人回执地址；不存在 / 不可用 → None（走既有 message/send 路径）。

    判定纪律（12.8.2）：
    - 只认 `metadata[RESPONSE_URL_METADATA_KEY]`（channel-originated context）；
    - 非 dict / 缺键 / 非 str / 空白 → None，**不猜测、不伪造、不 fallback**；
    - 智能机器人 inbound 缺失该字段已在入站解析处 Fail Closed，此处不再放宽。
    """
    metadata = outbound.metadata
    if not isinstance(metadata, dict):
        return None
    value = metadata.get(RESPONSE_URL_METADATA_KEY)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _response_errcode(body: str) -> int | None:
    """HTTP 响应体 → 业务 `errcode`；非 JSON / 非对象 / 无 errcode → None。

    3B-2.6：`response_url` 的**应用层**成功判据来源。真实平台实证（同一路径两次调用）：

        TTL 内 → HTTP 200 + {"errcode":0,"errmsg":"ok"}
        过期后 → HTTP 200 + {"errcode":60140,"errmsg":"invalid response code"}

    即 **HTTP 200 ≠ 送达**；只有 errcode == 0 才是业务成功。None（无法确证）不算成功。
    """
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    errcode = payload.get("errcode")
    return errcode if isinstance(errcode, int) else None


def build_passive_reply(
    touser: str,
    corp_id: str,
    content: str,
    create_time: int,
    *,
    encoding_aes_key: str,
    token: str,
    nonce: str,
) -> str:
    """被动回复 XML（完整 HTTP body）—— wire capability / compatibility helper。

    **不是** `ChannelAdapter.deliver()` 的实现路径（deliver = 主动消息 API，
    10.2.2 裁决）。保留本能力只为：协议完整性验证 / 测试 / 未来兼容场景
    （被动回复受 WeCom 5s 同步窗口约束，不适合 Agent 长执行模型）。

    两层结构：明文回复 XML → encrypt_message → 外层信封
    （Encrypt + MsgSignature + TimeStamp + Nonce）。
    """
    plaintext = (
        f"<xml><ToUserName><![CDATA[{touser}]]></ToUserName>"
        f"<FromUserName><![CDATA[{corp_id}]]></FromUserName>"
        f"<CreateTime>{int(create_time)}</CreateTime>"
        f'<MsgType><![CDATA[{_MSG_TYPE_TEXT}]]></MsgType>'
        f"<Content><![CDATA[{content}]]></Content></xml>"
    )
    encrypt = encrypt_message(plaintext, encoding_aes_key, corp_id)
    timestamp = str(int(create_time))
    signature = compute_signature(token, timestamp, nonce, encrypt)
    return (
        f"<xml><Encrypt><![CDATA[{encrypt}]]></Encrypt>"
        f"<MsgSignature><![CDATA[{signature}]]></MsgSignature>"
        f"<TimeStamp>{timestamp}</TimeStamp>"
        f"<Nonce><![CDATA[{nonce}]]></Nonce></xml>"
    )


# ================================================================== 入站双格式解析（12.7 真实验收补充，wire 层纯函数）


def _envelope_encrypt(body: str, channel_id: int) -> str:
    """POST body 外层信封 → Encrypt 密文（XML 自建应用 / JSON 智能机器人）。

    判定：body 去空白后以 `{` 开头 → JSON 信封（官方 101033）；
    否则 → XML 信封（10.2.1 现状语义）。
    """
    text = body.strip()
    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise InboundFormatError(
                f"channel_id={channel_id} JSON 信封解析失败") from exc
        if not isinstance(payload, dict):
            raise InboundFormatError(f"channel_id={channel_id} JSON 信封不是对象")
        encrypt = payload.get("encrypt")
        if not isinstance(encrypt, str) or not encrypt.strip():
            raise InboundFormatError(
                f"channel_id={channel_id} JSON 信封缺少 encrypt 字段")
        return encrypt
    outer = parse_xml_fields(body)
    encrypt = outer.get("Encrypt") or ""
    if not encrypt.strip():
        raise InboundFormatError(f"channel_id={channel_id} 消息缺少 Encrypt 节点")
    return encrypt


def _inbound_from_xml(fields: dict[str, str], channel_id: int) -> InboundInteraction | None:
    """XML 明文消息 → InboundInteraction（自建应用 wire，10.2.1 冻结语义原样）。

    非 text MsgType → None（acknowledge-ignore）；text 缺必需字段 → InboundFormatError。
    """
    msg_type = (fields.get("MsgType") or "").strip()
    if msg_type != _MSG_TYPE_TEXT:
        return None
    content = fields.get("Content") or ""
    from_user = (fields.get("FromUserName") or "").strip()
    msg_id = (fields.get("MsgId") or "").strip()
    if not content.strip() or not from_user or not msg_id:
        raise InboundFormatError(
            f"channel_id={channel_id} text 消息缺少必需字段"
            "（Content/FromUserName/MsgId）")
    # M6.6-C3 / ADR-12 §13：本 wire **只支持单聊**（10.2.1 冻结）⇒ 显式声明
    # `chat_type`（与 response_url 同走既有 metadata 通路；不动 8.1 契约字段面）。
    return InboundInteraction(
        channel_id=channel_id,
        external_interaction_id=msg_id,
        external_conversation_id=from_user,
        external_identity_id=from_user,
        content=content,
        metadata={CHAT_TYPE_METADATA_KEY: CHAT_TYPE_SINGLE},
    )


def _inbound_from_robot_json(payload: object, channel_id: int) -> InboundInteraction | None:
    """JSON 明文消息 → InboundInteraction（智能机器人 wire，官方文档 100719）。

    - 仅支持 `msgtype=text` 且 `chattype=single`（单聊，10.2.1 单聊裁决延续）；
      群聊/其他类型 → None（acknowledge-ignore，群聊 ChatId 留未来裁决不猜）；
    - `msgid` / `from.userid` / `text.content` 缺失 → InboundFormatError（Fail Closed）；
    - `response_url`（12.8.2）：官方 100719 确认「用户→机器人」消息推送**必然携带**，
      且它是智能机器人**唯一的主动回复通道**（101138）→ 缺失即 Fail Closed，
      **绝不默默回退到自建应用 message/send**（两者是不同渠道形态，混用会静默错投）；
      以 `metadata["response_url"]` 承载（channel-specific delivery context，
      不新增 8.1 契约字段）；
    - `aibotid` / `chatid` 仍不进契约（字段面不动）。
    """
    if not isinstance(payload, dict):
        raise InboundFormatError(f"channel_id={channel_id} JSON 消息明文不是对象")
    if payload.get("msgtype") != _MSG_TYPE_TEXT:
        return None
    if payload.get("chattype") != "single":
        return None
    sender = payload.get("from")
    from_user = sender.get("userid") if isinstance(sender, dict) else None
    from_user = from_user.strip() if isinstance(from_user, str) else ""
    msg_id = payload.get("msgid")
    if isinstance(msg_id, str):
        msg_id = msg_id.strip()
    elif isinstance(msg_id, int):
        msg_id = str(msg_id)
    else:
        msg_id = ""
    text = payload.get("text")
    content = text.get("content") if isinstance(text, dict) else None
    content = content if isinstance(content, str) else ""
    if not content.strip() or not from_user or not msg_id:
        raise InboundFormatError(
            f"channel_id={channel_id} robot text 消息缺少必需字段"
            "（msgid/from.userid/text.content）")
    response_url = payload.get("response_url")
    if not isinstance(response_url, str) or not response_url.strip():
        raise InboundFormatError(
            f"channel_id={channel_id} robot text 消息缺少 response_url"
            "（智能机器人主动回复的唯一回执地址；缺失不回退 message/send）")
    return InboundInteraction(
        channel_id=channel_id,
        external_interaction_id=msg_id,
        external_conversation_id=from_user,
        external_identity_id=from_user,
        content=content,
        # M6.6-C3 / ADR-12 §13：本路径 `chattype` 已由前置判定固定为 single。
        metadata={
            RESPONSE_URL_METADATA_KEY: response_url.strip(),
            CHAT_TYPE_METADATA_KEY: CHAT_TYPE_SINGLE,
        },
    )


# ================================================================== Adapter


def _aes_key(encoding_aes_key: str) -> bytes:
    """EncodingAESKey（43 字符 base64）+ "=" → 32 字节 AESKey；非法 → 配置错误。"""
    if not isinstance(encoding_aes_key, str) or len(encoding_aes_key) != 43:
        raise InboundChannelConfigError(
            f"encoding_aes_key 必须是 43 字符的 EncodingAESKey，"
            f"得到 {len(encoding_aes_key) if isinstance(encoding_aes_key, str) else type(encoding_aes_key).__name__}"
        )
    try:
        key = base64.b64decode(encoding_aes_key + "=", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InboundChannelConfigError("encoding_aes_key 不是合法 base64") from exc
    if len(key) != 32:
        raise InboundChannelConfigError(
            f"EncodingAESKey 解码后应为 32 字节，得到 {len(key)}")
    return key


def _require_str(config: dict, key: str, channel: Channel) -> str:
    value = config.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InboundChannelConfigError(
            f"channel_id={channel.id} 的 config 缺少验签材料：{key}")
    return value


class WeComAdapter:
    """企业微信（自建应用）双向 Adapter（入站 10.2.1 / 出站 10.2.2）。

    入站：`verify_handshake` / `parse_message`（InboundChannelAdapter Port），
    只消费传入的 `Channel.config`，无状态。
    出站：`deliver(outbound)`（ChannelAdapter Port，正式 Delivery 路径）——
    按 `channel_id` 经 UoW Port 解析渠道凭据（OutboundResponse 刻意不带凭据，
    9.6 纪律：DeliveryExecutor 不读库），每次交付独立获取 access_token
    （不缓存 / 不刷新 / 不重试，10.2.2 范围控制）。

    不 import Runtime / 不触碰 ORM / 不修改 OutboundResponse。
    """

    def __init__(self, uow_factory: Callable[[], UnitOfWork] | None = None) -> None:
        # 入站两能力不需要它；出站 deliver 需要按 channel_id 解析 config。
        self._uow_factory = uow_factory

    def _material(self, channel: Channel) -> tuple[str, str, str]:
        """从 Channel.config 读取 (token, encoding_aes_key, corp_id)；缺失 → Fail Closed。"""
        config = channel.config if isinstance(channel.config, dict) else {}
        if not config:
            raise InboundChannelConfigError(
                f"channel_id={channel.id} 缺少 config（无法进行 WeCom 验签）")
        return (
            _require_str(config, "token", channel),
            _require_str(config, "encoding_aes_key", channel),
            _require_str(config, "corp_id", channel),
        )

    @staticmethod
    def _expected_receiveid(channel: Channel, corp_id: str) -> str:
        """期望 ReceiveId 裁决（12.7 真实验收补充，语义冻结）：

        - `receiveid` 键缺省 → corp_id（自建应用语义，10.2.1 现状不变）；
        - `receiveid` 键存在 → 按配置值校验——**显式空串就是空串**
          （智能机器人真实 wire 的 ReceiveId 为空串），绝不当作缺省值。
        """
        config = channel.config if isinstance(channel.config, dict) else {}
        value = config.get("receiveid")
        return corp_id if value is None else value

    def verify_handshake(
        self,
        channel: Channel,
        *,
        signature: str,
        timestamp: str,
        nonce: str,
        echostr: str,
    ) -> str:
        """WeCom GET 握手：验签（token/timestamp/nonce/echostr）→ 解密 → 明文。"""
        token, aes_key, corp_id = self._material(channel)
        if not isinstance(signature, str) or not isinstance(echostr, str) or not echostr:
            raise InboundSignatureError(f"channel_id={channel.id} 握手参数缺失")
        expected = compute_signature(token, timestamp, nonce, echostr)
        if expected != signature:
            raise InboundSignatureError(f"channel_id={channel.id} 握手签名校验失败")
        return decrypt_message(
            echostr, aes_key, self._expected_receiveid(channel, corp_id))

    def parse_message(
        self,
        channel: Channel,
        *,
        signature: str,
        timestamp: str,
        nonce: str,
        body: str,
    ) -> InboundInteraction | None:
        """WeCom POST：验签 → 解密 → 明文映射（XML 自建应用 / JSON 智能机器人）。

        双格式裁决（12.7 真实验收补充，均属 wire 层）：
        - 外层信封：XML `<xml><Encrypt>`（10.2.1）/ JSON `{"encrypt": ...}`（官方 101033）；
        - 解密明文：XML 字段 / JSON 报文（官方 100719）。
        判定统一按「去空白后以 `{` 开头」分派，互不干扰。
        """
        if channel.id is None:
            raise InboundChannelConfigError(
                "未持久化的 Channel（无 id）不能承接入站消息")
        token, aes_key, corp_id = self._material(channel)
        if not isinstance(signature, str) or not isinstance(body, str) or not body.strip():
            raise InboundFormatError(f"channel_id={channel.id} 消息体为空")

        # ---- 外层信封（XML / JSON）→ Encrypt 密文 ----
        encrypt = _envelope_encrypt(body, channel.id)

        # ---- 验签 + 解密（不可信 → 403 语义）----
        if compute_signature(token, timestamp, nonce, encrypt) != signature:
            raise InboundSignatureError(f"channel_id={channel.id} 消息签名校验失败")
        plaintext = decrypt_message(
            encrypt, aes_key, self._expected_receiveid(channel, corp_id))

        # ---- 明文映射（XML / JSON）----
        if plaintext.lstrip().startswith("{"):
            try:
                payload = json.loads(plaintext)
            except json.JSONDecodeError as exc:
                raise InboundFormatError(
                    f"channel_id={channel.id} JSON 消息明文解析失败") from exc
            return _inbound_from_robot_json(payload, channel.id)
        return _inbound_from_xml(parse_xml_fields(plaintext), channel.id)

    # ================================================================ 出站（10.2.2，ChannelAdapter Port）

    def deliver(self, outbound: OutboundResponse) -> None:
        """把 OutboundResponse 交付给 WeCom（正式 Delivery 路径）。

        两条互斥路径（12.8.2）：
        - **智能机器人**：`metadata["response_url"]` 存在 → POST 该回执地址
          （markdown 回复；不使用 corpsecret / agentid / access_token）；
        - **自建应用**：无 response_url → 既有 access_token + message/send 路径
          （行为完全不变）。

        Fail Closed：一切失败统一映射 `ChannelDeliveryFailedError`
        （出站 Port 契约：不新造错误码）。
        语义：**交付**，返回 None；processing SUCCESS ≠ delivery SUCCESS 由
        Executor 层保证（9.6）。
        """
        # ---- 输入 Fail Closed（与 DeliveryExecutor 双防线一致）----
        if not isinstance(outbound, OutboundResponse):
            raise ChannelDeliveryFailedError()
        if not isinstance(outbound.channel_id, int) or outbound.channel_id <= 0:
            raise ChannelDeliveryFailedError()
        if not isinstance(outbound.external_conversation_id, str) \
                or not outbound.external_conversation_id.strip():
            raise ChannelDeliveryFailedError(channel_id=outbound.channel_id)
        if not isinstance(outbound.content, str) or not outbound.content.strip():
            raise ChannelDeliveryFailedError(channel_id=outbound.channel_id)

        # ACP V0.3 Phase 1：Agent 产出的成果**引用** → 出站消息呈现（引用式：
        # 只呈现引用，不取回字节、不上传、不声称已送达）。无 artifact 时
        # `content` **原样**（逐字节一致）⇒ 旧出站消息零变化。
        content = render_outbound_content(outbound.content, outbound.artifacts)

        # ---- 智能机器人：response_url 优先（不读 Channel 出站凭据）----
        response_url = _response_url_from(outbound)
        if response_url is not None:
            self._deliver_via_response_url(response_url, outbound, content)
            return

        channel = self._load_channel_for_send(outbound.channel_id)
        material = self._outbound_material(channel)
        touser = outbound.external_conversation_id  # 单聊裁决（10.2.1）：conversation = userid
        try:
            token = self._fetch_access_token(material)
            self._send_text_message(material, token, touser, content)
        except ChannelDeliveryFailedError as exc:
            if exc.channel_id is None:  # 底层抛出未带上下文 → 在此补全（12.7）
                raise ChannelDeliveryFailedError(
                    channel_id=outbound.channel_id,
                    external_conversation_id=outbound.external_conversation_id,
                ) from exc
            raise
        except Exception as exc:  # noqa: BLE001 transport / JSON / 网络统一收口
            raise ChannelDeliveryFailedError(
                channel_id=outbound.channel_id,
                external_conversation_id=outbound.external_conversation_id,
            ) from exc

    def _deliver_via_response_url(
        self, response_url: str, outbound: OutboundResponse, content: str
    ) -> None:
        """POST response_url —— 智能机器人主动回复（官方 101138）。

        已确认事实（12.8.1）驱动的 wire：
        - method = POST；`Content-Type: application/json`；
        - **不发送 Authorization / access_token**（鉴权由 URL 内 response_code 承载）；
        - URL 直接使用回调返回的完整地址，**不拼接任何参数**；
        - body = markdown（官方主动回复无 text 类型）；
        - 官方明确「每个 response_url 只能调用一次」→ 本路径失败 **retryable=False**。

        **成功判定（3B-2.6 校正；真实平台实证驱动）**：`HTTP 2xx` **不等于**送达。
        同一条路径的两次真实调用响应：

            TTL 内 → HTTP 200 + {"errcode":0,"errmsg":"ok"}                     ← 真的送达
            过期后 → HTTP 200 + {"errcode":60140,"errmsg":"invalid response code"} ← 未送达

        12.8.2 当时以「官方未定义响应 schema，不做未确认推断」为由只判 HTTP 层——
        该推断已被真实响应**证伪**，判据因此收紧为 **HTTP 2xx + errcode == 0**。

        Transport-level success ≠ Application-level success：凡是无法确证
        `errcode == 0` 的响应（含非 JSON / 缺 errcode）一律 Fail Closed——
        宁可误报交付失败，绝不产生「记为 DELIVERED 而用户没收到」的假绿。

        超时取 `DEFAULT_OUTBOUND_TIMEOUT_S`：本路径不读 Channel.config——
        智能机器人回复不依赖 corpsecret / agentid，读凭据反而会 Fail Closed。
        """
        payload = compose_robot_reply_payload(content)
        req = urllib.request.Request(
            response_url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=DEFAULT_OUTBOUND_TIMEOUT_S) as resp:
                status = getattr(resp, "status", 200)
                body = resp.read().decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001 transport / HTTPError / 网络
            raise ChannelDeliveryFailedError(
                channel_id=outbound.channel_id,
                external_conversation_id=outbound.external_conversation_id,
                retryable=False,
            ) from exc
        # Transport 层（HTTP）与应用层（errcode）**两个判据都要过**（3B-2.6）。
        if not 200 <= status < 300 or _response_errcode(body) != 0:
            raise ChannelDeliveryFailedError(
                channel_id=outbound.channel_id,
                external_conversation_id=outbound.external_conversation_id,
                retryable=False,
            )

    def _load_channel_for_send(self, channel_id: int) -> Channel:
        """按 channel_id 经 UoW Port 解析渠道（出站凭据来源）。

        未装配 uow_factory / 渠道不存在 → ChannelDeliveryFailedError
        （出站 Port 契约：无 ChannelNotFound，一律映射既有失败码）。
        """
        if self._uow_factory is None:
            raise ChannelDeliveryFailedError(
                channel_id=channel_id)  # 出站能力未装配（Fail Closed）
        try:
            with self._uow_factory() as uow:
                channel = uow.channels.get_by_id(channel_id)
        except ChannelDeliveryFailedError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ChannelDeliveryFailedError(channel_id=channel_id) from exc
        if channel is None:
            raise ChannelDeliveryFailedError(channel_id=channel_id)
        return channel

    @staticmethod
    def _outbound_material(channel: Channel):
        """读取出站材料 (corp_id, corpsecret, agentid, api_base, timeout_s)。

        corpsecret / agentid 是 10.2.2 新增的 outbound 渠道凭据（MVP 暂存
        Channel.config，见模块 docstring）；缺失 → ChannelDeliveryFailedError。
        """
        config = channel.config if isinstance(channel.config, dict) else {}
        channel_id = channel.id

        def req(key: str) -> str:
            value = config.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ChannelDeliveryFailedError(channel_id=channel_id)
            return value

        corp_id = req("corp_id")
        corpsecret = req("corpsecret")
        agentid = config.get("agentid")
        if not isinstance(agentid, int) or agentid <= 0:
            raise ChannelDeliveryFailedError(channel_id=channel_id)
        api_base = config.get("api_base")
        api_base = api_base.strip().rstrip("/") \
            if isinstance(api_base, str) and api_base.strip() else DEFAULT_API_BASE
        try:
            timeout_s = float(config.get("timeout_s", DEFAULT_OUTBOUND_TIMEOUT_S))
        except (TypeError, ValueError):
            raise ChannelDeliveryFailedError(channel_id=channel_id) from None
        if timeout_s <= 0:
            raise ChannelDeliveryFailedError(channel_id=channel_id)
        return corp_id, corpsecret, agentid, api_base, timeout_s

    @staticmethod
    def _fetch_access_token(material) -> str:
        """GET /cgi-bin/gettoken → access_token；errcode!=0 / 非法响应 → Fail Closed。

        每次 deliver 独立获取（不缓存 / 不刷新 / 不重试，10.2.2 范围控制）。
        """
        corp_id, corpsecret, _, api_base, timeout_s = material
        url = (f"{api_base}/cgi-bin/gettoken"
               f"?corpid={urllib.parse.quote(corp_id)}"
               f"&corpsecret={urllib.parse.quote(corpsecret)}")
        try:
            with urllib.request.urlopen(url, timeout=timeout_s) as resp:
                if getattr(resp, "status", 200) != 200:
                    raise ChannelDeliveryFailedError()
                payload = json.loads(resp.read().decode("utf-8"))
        except ChannelDeliveryFailedError:
            raise
        except Exception as exc:  # noqa: BLE001 HTTPError / URLError / JSON
            raise ChannelDeliveryFailedError() from exc
        if not isinstance(payload, dict) or payload.get("errcode") != 0:
            raise ChannelDeliveryFailedError()
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise ChannelDeliveryFailedError()
        return token

    def _send_text_message(self, material, token: str, touser: str, content: str) -> None:
        """POST /cgi-bin/message/send（text）；errcode!=0 / 非 2xx → Fail Closed。"""
        _, _, agentid, api_base, timeout_s = material
        payload = compose_message_payload(touser, agentid, content)
        url = f"{api_base}/cgi-bin/message/send?access_token={urllib.parse.quote(token)}"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                if getattr(resp, "status", 200) != 200:
                    raise ChannelDeliveryFailedError()
                body = json.loads(resp.read().decode("utf-8"))
        except ChannelDeliveryFailedError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ChannelDeliveryFailedError() from exc
        if not isinstance(body, dict) or body.get("errcode") != 0:
            raise ChannelDeliveryFailedError()

    # ================================================================ Provider Verify（M6.6-F3）

    @staticmethod
    def verify_connection(channel: Channel) -> AgentProbeSnapshot:
        """只读校验 WeCom 平台凭据是否有效（Provider-native credential verification）。

        M6.6-F3 语义：读 `Channel.config`（corp_id / corpsecret / agentid / api_base /
        timeout_s）→ 调既有 `GET /cgi-bin/gettoken`（成功 == 凭据有效）→ 归一为
        `AgentProbeSnapshot`。

        严格契约（F3 硬约束）：
        - **只读诊断**：不写库、不改 Channel / Connection / Identity / User / Grant /
          Session / Runtime，不缓存 token（access_token 取到即弃）；
        - **Fail Closed**：材料缺失 / provider errcode!=0 / 非 200 / 网络失败 / 畸形响应
          → `reachable=False`，绝不抛异常、绝不返回半成品；
        - **不泄漏**：`detail` 恒为**泛化**文案（不含 corpsecret / access_token /
          HTTP 状态 / URL）；不写日志（连异常类型也不落）。

        复用 `_outbound_material` / `_fetch_access_token`（WeCom outbound wire 的既有
        唯一实现）——**不重新实现 gettoken client**。

        设计为 `@staticmethod`：本能力不依赖实例状态（只读 `channel.config`），故组合根
        可以 `WeComAdapter.verify_connection` 直接注入，**无需**（也不得）新增第二个
        `WeComAdapter` 构造点（Step 12.6 组合一致性护栏：双端口共用一个构造点）。
        """
        probe = WECOM_PROBE_LABEL
        if not isinstance(channel, Channel):
            return AgentProbeSnapshot(False, _PROBE_DETAIL_UNVERIFIED, probe)
        try:
            material = WeComAdapter._outbound_material(channel)
            WeComAdapter._fetch_access_token(material)  # 只读探针：token 取到即弃
        except Exception:  # noqa: BLE001 诊断：一切失败都只是「不可达」（不抛、不记）
            return AgentProbeSnapshot(False, _PROBE_DETAIL_UNVERIFIED, probe)
        return AgentProbeSnapshot(True, None, probe)
