"""WeComBotAdapter / WeComBotTransport —— 企业微信智能机器人 WebSocket transport（3B-2.2）。

位置：`app/adapters/channels/wecom_bot.py`。同时实现两个 Port：
- 入站 `InboundChannelAdapter`（Port 在 `app/repositories/channel_inbound.py`）；
- 出站 `ChannelAdapter`（Port 在 `app/repositories/channel_adapter.py`）。

与同目录 `wecom.py`（HTTP webhook）的关系：

    Channel(type=wecom)      → WeComAdapter      → HTTP 回调（每请求验签 / response_url）
    Channel(type=wecom_bot)  → WeComBotAdapter   → WebSocket 长连接（订阅 / respond_msg）

两者是**并列且互不感知**的两个渠道形态（`MultiChannelAdapter` 按 `channel.type`
分派）；本模块**不修改** `wecom.py` 的任何 webhook 语义。Runtime / Domain /
Persistence 完全不知道二者差别（`app/main.py` 只加一条映射）。

协议（Bot ID + Secret → 长连接）—— **已按官方文档校准（3B-2.4）**：

    wss://openws.work.weixin.qq.com
        → aibot_subscribe        订阅（headers.req_id + body{bot_id, secret}）
        → 订阅响应                {"headers":{"req_id"},"errcode":0,"errmsg":"ok"}
        → aibot_msg_callback     入站消息帧（机器人 → CP；业务字段在 body）
        → aibot_respond_msg      出站回复帧（CP → 机器人；headers.req_id 透传回调值）
        → ping                   应用层心跳（每 30s；服务端长时间收不到即主动断连）
                                 ⚠ 不能用 WebSocket 协议级 ping：该服务端对「我方主动
                                   发的协议级 PING」的响应会被 websockets 判为协议
                                   错误 → 必须 ping_interval=None（见 _default_connect）

    帧骨架（所有帧同构）：

        {"cmd": "...", "headers": {"req_id": "..."}, "body": {...}}
        响应帧无 cmd：{"headers": {"req_id": "..."}, "errcode": 0, "errmsg": "ok"}

    文档依据：企业微信开发者中心《智能机器人长连接》
    https://developer.work.weixin.qq.com/document/path/101463（2026/05/25）

**信任模型（与 webhook 显式不同，安全审计要点）**：

- webhook：**每请求验签**（SHA1 + AES-CBC，见 `wecom.py`）；
- bot WS：信任来自「用 `bot_secret` 成功建立的这条订阅连接」本身——帧上**没有**
  逐条签名。因此 `parse_message` 的 `signature / timestamp / nonce` 三参数
  在本质上是空占位（与 `LocalChannelAdapter` 同一形状：`app/api/runtime/channels.py`
  对 local 渠道即以空串调用同一条 `accept_message` 入口）。
  真实认证发生在 `aibot_subscribe`（连接建立期），不在每帧。

**入站字段映射（沿用 8.1 契约，字段面不动）**——以下字段均读自帧 `body`：

    msgid        → external_interaction_id （幂等 / 去重依据；官方事件排重键）
    from.userid  → external_identity_id    （渠道侧身份，≠ Identity.id）
    from.userid  → external_conversation_id（单聊：conversation == identity，与
                   `wecom.py` 单聊裁决同形）
    text.content → content
    aibotid      → **不进契约**：它是「哪个 bot 收到的」= Channel 定位键；
                   本模块只用它做**渠道归属校验**（防御跨 bot 误投帧）——
                   与 webhook 用 URL 里的 channel_id 定位同性质
    chatid       → 群聊会话键（3B-3.3 接纳群聊）：官方仅群聊返回。
                   external_conversation_id 映射——
                   single = from.userid（会话 == 身份，与 `wecom.py` 单聊裁决同形）；
                   group  = body.chatid（群会话）。
                   是否 @Bot 不由 ACP 判断：官方回调触发机制已保证群聊回调
                   必为 @Bot 消息（3B-3.1 Audit：普通群消息根本不触发
                   aibot_msg_callback，且 wire 无结构化 mention 字段）；
                   未知 chattype → acknowledge-ignore（不猜、不误判为群聊）。

**回信上下文（channel-originated delivery context）**：

沿用 `wecom.py` 的 `response_url` 同一机制——入站解析把回信句柄放进
`InboundInteraction.metadata["bot_reply"]`，经 Interaction 快照（
`app/domain/interaction_snapshot.py` 恒存 metadata）→ Worker 重建 →
`RuntimeCoordinator` 的 metadata 合并（channel-originated 覆盖 Agent 同名 key）
→ `OutboundResponse.metadata` → 本模块 `deliver()` 取出并组装
`aibot_respond_msg`。**不新增 8.1 / 8.6 的契约字段面，不新增 DB 列。**

**deliver 的有状态性（本 transport 与 webhook 的本质差别，必须知悉）**：

    response_url（wecom）   = 无状态 HTTPS POST：任何进程、任何时刻都能投
    aibot_respond_msg（本模块）= 必须经**本进程持有的活连接**发送

因此 `deliver()` 经进程级 `WeComBotConnectionRegistry` 取「该 channel 当前的
连接」；取不到 → Fail Closed（`ChannelDeliveryFailedError`）。推论：
**backend 必须单实例**（多副本时持有连接的进程未必是执行 Delivery 的进程）。
在无 Redis/Kafka 的约束下，这是本 transport 的固有前提，已在文档层面记录。

**交付结果校验（3B-2.6）—— 三层成功语义**：

    Transport Success   = 帧写进 socket（send 未抛错）
            ↓
    Application Success = 服务端响应 errcode == 0（且 headers.req_id 与本次一致）
            ↓
    Delivery Success    = 两层都成立，才允许落 delivery=DELIVERED

真实平台实证：`aibot_respond_msg` **会被服务端 ACK**
（`{"headers":{"req_id":...},"errcode":0,"errmsg":"ok"}`）。因此本模块**不再
fire-and-forget**：`deliver()` 发帧后等待该 req_id 的响应并校验 errcode。

**必须区分的两个时间维度（不要混淆）**：

    Agent execution timeout        Agent 多久产出 response
                                   → 归 Worker / Runtime，本模块不感知、不设限
    ACK wait timeout（本模块）     发帧之后，WeCom 多久回 errcode 响应
                                   → DEFAULT_ACK_WAIT_TIMEOUT_S，只覆盖「发帧→ACK」

两者**互不叠加、互不换算**。执行 15s 而 ACK 1s 回来 = 正常；执行 8s 而 ACK
11s 才回来 = delivery failure。`DEFAULT_ACK_WAIT_TIMEOUT_S` **不是**「Agent
最多只能跑 10s」——这是本模块最容易被误读的一点，故在常量定义处再次标注。

**收发线程所有权（为什么 ACK 由 transport 线程兑现）**：`recv` 的所有权属于
transport 线程（websockets 同步连接不允许两个线程同时 `recv`）。因此交付线程
（Worker）只**发送**并在 registry 上登记等待槽，由 transport 线程收到响应帧时
**兑现**该槽——`send` 线程安全，`recv` 始终只有一个人调用。

**为什么不用 response_url 取代本路径（实测结论）**：长连接帧同样携带
`response_url`，但它有效期 **≤1 小时**（实测 +30min 有效 / +60min 失效
`errcode=60140`），而本路径的回复窗口是 24h → 长执行回复必须走活连接，
**单实例前提不变**；response_url 至多是无状态快路径优化（本步不做 fallback）。

**协议知识只停留在本模块（Adapter 层）**：`req_id` 是 WeCom transport 的
channel-originated delivery context，**不是** ACP Domain 字段——它只经
`InboundInteraction.metadata["bot_reply"]` 走既有通路传递（Interaction 快照 →
Worker 重建 → `OutboundResponse.metadata` → 本模块回复帧），**Domain 契约字段面
一行未改**（`InboundInteraction` 仍只有 6 个字段）。

**回复关联键是 `headers.req_id`（不是 `msgid`）**：官方要求回复帧透传回调的
`req_id`。它在回调帧中是**必填**字段 → 缺失即 Fail Closed（与 `wecom.py` 对
智能机器人 `response_url` 缺失的裁决同一纪律：绝不默默降级）。
`msgid` 则继续作为 `external_interaction_id`（官方定义的**事件排重**键）。
`req_id` 只作为 transport metadata 流转，不新增任何契约字段。

**校准点集中**：帧构造/解析全部收口在 `compose_subscribe_frame` /
`compose_ping_frame` / `compose_respond_frame` / `inbound_from_callback` 四处，
协议再变时只改这四处，Port / Domain / Runtime 一律不动。

**运行期调和（M6.6-F2 hot discovery）—— 复用既有发现，不新建发现框架**：

    binding_source（组合根的 discover_bot_connections；channel_id = 绑定身份）
        → start()   调用一次（首轮枚举）
        → reconcile() 周期再调用 → ADD / KEEP / REMOVE
              ADD    : 新 channel_id → 起 rx+exec 线程（订阅）
              KEEP   : 同 channel_id → 线程不动；若 spec 变化（如 secret 轮换）→
                       更新 spec 并置 reconnect → 既有 _run 以**新** material 重连
                       （不记录 secret）
              REMOVE : channel_id 消失 → 停**该**绑定 + 注销活连接 + 清理
                       （只停这一条，不影响其它绑定）

- 调和**不是**新的 discovery / plugin / registry 框架：只是把组合根已注入的
  `binding_source` 在既有生命周期里重复调用（与 `ProductionDriver` 的周期轮询
  同纪律）；
- 单条渠道 config 不完整 / 非法由 `binding_source` 内部跳过（单点错误绝不阻断
  其它 bot）；`binding_source` 整体抛错 → 保留现有绑定（不误拆）；
- `reconcile()` **不**解决多副本 / 分布式连接所有权（仍是既有单实例前提）。

**刻意不做**（后置）：消息幂等以外的重试 / delivery 重投 / 多副本
连接路由 / access token / 事件订阅（仅消息帧）。
（3B-3.3：群聊 @Bot 消息已按官方 wire 接纳——仅字段映射，无 Group 语义层、
无 mention 判定、无 Core 改动；群聊主动推送仍不做。）
"""

import json
import logging
import queue
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from app.application.errors import ChannelDeliveryFailedError
from app.domain.channel import Channel
from app.domain.inbound_interaction import (
    CHAT_TYPE_METADATA_KEY,
    InboundInteraction,
)
from app.domain.outbound_response import OutboundResponse
from app.repositories.channel_inbound import (
    InboundChannelConfigError,
    InboundFormatError,
)

LOGGER = logging.getLogger(__name__)

#: 渠道类型（与 ChannelApplicationService 的 ALLOWED_CHANNEL_TYPES 对齐）。
CHANNEL_TYPE = "wecom_bot"

#: 官方长连接地址（channel.config 的 `ws_url` 可覆写：私有化 / 测试替身）。
DEFAULT_WS_URL = "wss://openws.work.weixin.qq.com"

#: 协议命令（官方字段名，3B-2.4 校准；见模块 docstring）。
CMD_SUBSCRIBE = "aibot_subscribe"
CMD_MSG_CALLBACK = "aibot_msg_callback"
CMD_RESPOND_MSG = "aibot_respond_msg"
#: 应用层心跳命令（官方 cmd=ping，建议 30s；WS 协议级 ping 不满足服务端保活判定）。
CMD_PING = "ping"

#: 帧骨架键：headers 承载 req_id（请求关联 / 回复透传的**唯一**依据）。
_HEADER_REQ_ID = "req_id"

#: 回信句柄在 OutboundResponse.metadata 中的承载键——与 `wecom.py` 的
#: `response_url` 同纪律（channel-specific delivery context，不改契约字段面）。
BOT_REPLY_METADATA_KEY = "bot_reply"

#: Channel.config 读取键（本模块是这些键的唯一事实源；脱敏集合据此维护）。
CONFIG_BOT_ID = "bot_id"
CONFIG_BOT_SECRET = "bot_secret"
CONFIG_WS_URL = "ws_url"

_MSG_TYPE_TEXT = "text"
_CHAT_TYPE_SINGLE = "single"
_CHAT_TYPE_GROUP = "group"      # 3B-3.3：群聊 @Bot 消息（官方合法 chattype 值）
_RESPOND_MSG_TYPE = "markdown"  # 与 wecom.py 的机器人回复一致：文本以 markdown 承载

DEFAULT_HEARTBEAT_INTERVAL_S = 30.0   # 官方建议：每 30 秒一次 cmd=ping
DEFAULT_RECONNECT_BACKOFF_S = 5.0
DEFAULT_RECV_TIMEOUT_S = 1.0
DEFAULT_SUBSCRIBE_TIMEOUT_S = 10.0    # 等待订阅响应（errcode）的上限
DEFAULT_CONNECT_OPEN_TIMEOUT_S = 15.0
DEFAULT_CONNECT_CLOSE_TIMEOUT_S = 5.0
#: **WeCom application-level delivery acknowledgement timeout** —— 等待
#: `aibot_respond_msg` 的 errcode 响应的上限（3B-2.6 交付结果校验）。
#:
#: ⚠️ 它**不是** Agent 执行超时，也不与 Agent 执行时长相加：
#:
#:     Agent execution（8s / 15s / 60s …）   ← 归 Worker / Runtime，与本常量无关
#:              ↓  产出 response 之后，才开始计时
#:     aibot_respond_msg 发帧
#:              ↓  ← 本常量只覆盖这一段
#:     ACK（errcode == 0）
#:
#: 即：Agent 跑 15s 而 ACK 1s 回来 → 正常 DELIVERED；Agent 跑 8s 而 ACK
#: 11s 才回来 → 记 delivery failure。两个时间维度彼此独立，误读本常量为
#: 「Agent 只能跑 10s」是本模块最容易发生的理解偏差。
DEFAULT_ACK_WAIT_TIMEOUT_S = 10.0

#: 运行期调和间隔（M6.6-F2）：重调 `binding_source()` 并做 ADD / KEEP / REMOVE。
#:
#: ⚠️ 这是**既有生命周期的再次枚举**，不是新的发现框架——`binding_source` 仍是
#: 组合根注入的唯一发现入口（`discover_bot_connections`），本间隔只决定多久
#: 再调和一次（与 `ProductionDriver` 的 period 同纪律）。
DEFAULT_RECONCILE_INTERVAL_S = 30.0


# ================================================================== 纯函数（单测直测）


def connection_spec_from_channel(channel: Channel) -> "BotConnectionSpec":
    """Channel → BotConnectionSpec（连接建立所需的全部材料）。

    Fail Closed：未持久化（无 id）/ config 缺 `bot_id` / `bot_secret` →
    `InboundChannelConfigError`（不猜、不回退缺省凭据）。
    `ws_url` 可选，缺省官方地址。
    """
    if channel.id is None:
        raise InboundChannelConfigError("未持久化的 Channel（无 id）不能建立 bot 连接")
    config = channel.config if isinstance(channel.config, dict) else {}

    def require(key: str) -> str:
        value = config.get(key)
        if not isinstance(value, str) or not value.strip():
            raise InboundChannelConfigError(
                f"channel_id={channel.id} 的 config 缺少 bot 连接材料：{key}")
        return value.strip()

    ws_url = config.get(CONFIG_WS_URL)
    if not isinstance(ws_url, str) or not ws_url.strip():
        ws_url = DEFAULT_WS_URL
    return BotConnectionSpec(
        channel_id=channel.id,
        bot_id=require(CONFIG_BOT_ID),
        bot_secret=require(CONFIG_BOT_SECRET),
        ws_url=ws_url.strip(),
    )


@dataclass(frozen=True)
class BotConnectionSpec:
    """一个 bot 渠道的连接材料（channel_id + 订阅凭据 + 地址）。"""

    channel_id: int
    bot_id: str
    bot_secret: str
    ws_url: str = DEFAULT_WS_URL


def _require_req_id(value: object) -> str:
    """req_id 形状校验（非空 str）；其余一律拒绝（不猜、不补）。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("req_id must be a non-empty str")
    return value.strip()


def compose_subscribe_frame(bot_id: str, bot_secret: str, req_id: str) -> dict:
    """订阅帧（官方字段名已校准）：凭据在 `body`，请求标识在 `headers.req_id`。"""
    if not isinstance(bot_id, str) or not bot_id.strip():
        raise ValueError("bot_id must be a non-empty str")
    if not isinstance(bot_secret, str) or not bot_secret.strip():
        raise ValueError("bot_secret must be a non-empty str")
    return {
        "cmd": CMD_SUBSCRIBE,
        "headers": {_HEADER_REQ_ID: _require_req_id(req_id)},
        "body": {"bot_id": bot_id.strip(), "secret": bot_secret.strip()},
    }


def compose_ping_frame(req_id: str) -> dict:
    """应用层心跳帧（官方 `cmd=ping`）。

    为什么不是 WebSocket 协议级 ping：官方心跳定义在**应用层**（本帧），
    服务端长时间收不到即主动断开——协议级 ping 不满足该保活判定。
    """
    return {"cmd": CMD_PING, "headers": {_HEADER_REQ_ID: _require_req_id(req_id)}}


def compose_respond_frame(content: str, reply_context: dict) -> dict:
    """回复帧（官方字段名已校准）——`content` → `body.markdown`。

    关联键是 `headers.req_id`，且必须**透传回调帧的 req_id**：它由入站帧携带
    （channel-originated），是本函数唯一的回复定位依据——不猜、不补、不改写。
    `body.msgtype` 只取 markdown（官方支持的类型之一；纯文本以 markdown 承载，
    与 `wecom.py` 的机器人回复同一裁决）。
    """
    if not isinstance(content, str) or not content.strip():
        raise ValueError("content must be a non-empty str")
    if not isinstance(reply_context, dict) or not reply_context:
        raise ValueError("reply_context must be a non-empty dict")
    return {
        "cmd": CMD_RESPOND_MSG,
        "headers": {_HEADER_REQ_ID: _require_req_id(reply_context.get(_HEADER_REQ_ID))},
        "body": {
            "msgtype": _RESPOND_MSG_TYPE,
            "markdown": {"content": content},
        },
    }


def parse_frame(raw: str, *, channel_id: int) -> dict:
    """原始帧文本 → JSON 对象；非 JSON / 非对象 → InboundFormatError。"""
    if not isinstance(raw, str) or not raw.strip():
        raise InboundFormatError(f"channel_id={channel_id} bot 帧为空")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InboundFormatError(f"channel_id={channel_id} bot 帧不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise InboundFormatError(f"channel_id={channel_id} bot 帧不是 JSON 对象")
    return payload


def _as_text(value: object) -> str:
    """str 原样 / int 转 str / 其它 → 空串（用于 id 类字段的宽容归一）。"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, int):
        return str(value)
    return ""


def _frame_headers(payload: dict) -> dict:
    """帧 headers（非 dict → 空 dict；不猜）。"""
    headers = payload.get("headers")
    return headers if isinstance(headers, dict) else {}


def _frame_body(payload: dict) -> dict | None:
    """帧 body（业务字段的唯一载体）；非 dict → None。"""
    body = payload.get("body")
    return body if isinstance(body, dict) else None


def frame_req_id(payload: dict) -> str:
    """帧 `headers.req_id`（非 str / 空白 → ""，不做任何推断）。"""
    return _as_text(_frame_headers(payload).get(_HEADER_REQ_ID))


def inbound_from_callback(
    payload: dict, channel_id: int,
) -> InboundInteraction | None:
    """`aibot_msg_callback` 帧 → InboundInteraction（官方字段名已校准）。

    帧骨架：`{"cmd":..., "headers":{"req_id":...}, "body":{...}}` —— 业务字段
    全部读自 `body`，回复关联键读自 `headers.req_id`。

    - 非消息帧（订阅/心跳响应、事件推送等）→ None（acknowledge-ignore）；
    - `cmd` 是消息帧但缺 `body` 对象 → InboundFormatError（形状非法）；
    - `body.msgtype=text` 且 `body.chattype ∈ {single, group}`（3B-3.3）：
      - single → external_conversation_id = from.userid（会话 == 身份，既有行为不变）；
      - group  → external_conversation_id = body.chatid（群会话键；官方仅群聊返回，
                 缺失/空白 → InboundFormatError——绝不把群消息当单聊归属）；
      - 其余 chattype（未知值）→ None（acknowledge-ignore：不猜、不回退、
        **不误判为群聊**）；
      - 群聊**不做 mention 判定**（3B-3.1 Audit）：官方回调触发机制已保证群聊
        回调必为 @Bot 消息，wire 亦无结构化 mention 字段可依；
    - `body.msgid` / `body.from.userid` / `body.text.content` 缺失 →
      InboundFormatError（Fail Closed）；
    - `headers.req_id` 缺失 → InboundFormatError（官方必填，且是**唯一**的回复
      关联键；缺失即 Fail Closed，绝不产出一条无法回复的交互）；
    - 回信句柄以 `metadata["bot_reply"] = {"req_id": ...}` 承载
      （transport metadata，**不是** Domain 字段）。群聊回复经同一 req_id
      回到**原始群会话**（回复帧不携带 chatid——req_id 在服务端绑定回调来源
      会话，故 destination = 原始 chatid，无需新增任何字段）。
    """
    if payload.get("cmd") != CMD_MSG_CALLBACK:
        return None
    body = _frame_body(payload)
    if body is None:
        raise InboundFormatError(
            f"channel_id={channel_id} aibot_msg_callback 帧缺少 body 对象")
    if body.get("msgtype") != _MSG_TYPE_TEXT:
        return None
    chattype = body.get("chattype")
    if chattype not in (_CHAT_TYPE_SINGLE, _CHAT_TYPE_GROUP):
        return None  # 未知 chattype：ignore（不猜、不误判为群聊）

    sender = body.get("from")
    from_user = _as_text(sender.get("userid")) if isinstance(sender, dict) else ""
    msg_id = _as_text(body.get("msgid"))
    text = body.get("text")
    content = text.get("content") if isinstance(text, dict) else None
    content = content if isinstance(content, str) else ""
    if not content.strip() or not from_user or not msg_id:
        raise InboundFormatError(
            f"channel_id={channel_id} bot text 消息缺少必需字段"
            "（body.msgid/body.from.userid/body.text.content）")

    if chattype == _CHAT_TYPE_GROUP:
        conversation = _as_text(body.get("chatid"))
        if not conversation:
            raise InboundFormatError(
                f"channel_id={channel_id} 群聊消息缺少 body.chatid"
                "（官方仅群聊返回；缺失不回退 from.userid）")
    else:
        conversation = from_user  # 单聊：conversation == identity（既有裁决不变）

    req_id = frame_req_id(payload)
    if not req_id:
        raise InboundFormatError(
            f"channel_id={channel_id} aibot_msg_callback 缺少 headers.req_id"
            "（官方必填的回复关联键；缺失不回退 msgid、不产生不可回复的交互）")

    # M6.6-C3 / ADR-12 §13：会话形态由 **Adapter 显式声明**（协议知识只在本层）。
    # 走既有 metadata 通路（恒存于 Interaction 快照 ⇒ Worker 可重建），
    # 不新增 8.1 契约字段、不新增 DB 列；Core 只读该键，绝不启发式推断。
    return InboundInteraction(
        channel_id=channel_id,
        external_interaction_id=msg_id,
        external_conversation_id=conversation,
        external_identity_id=from_user,
        content=content,
        metadata={
            BOT_REPLY_METADATA_KEY: {_HEADER_REQ_ID: req_id},
            CHAT_TYPE_METADATA_KEY: chattype,
        },
    )


def reply_context_from(outbound: OutboundResponse) -> dict | None:
    """取回信句柄；不存在 / 不可用 → None（Fail Closed，不回退任何其它通道）。

    判定纪律（与 `wecom.py::_response_url_from` 同形）：
    - 只认 `metadata[BOT_REPLY_METADATA_KEY]`（channel-originated context）；
    - **必须含 `req_id`**（官方唯一的回复关联键）；缺失 → None；
    - 非 dict / 空 dict / 含非 str 或空白值 → None，**不猜测、不伪造**。
    """
    metadata = outbound.metadata
    if not isinstance(metadata, dict):
        return None
    value = metadata.get(BOT_REPLY_METADATA_KEY)
    if not isinstance(value, dict) or not value:
        return None
    if not _as_text(value.get(_HEADER_REQ_ID)):
        return None  # 无关联键 = 无回信句柄（deliver 前置 Fail Closed）
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str) or not item.strip():
            return None
    return dict(value)


# ================================================================== 连接注册表


@dataclass
class _AckSlot:
    """一次在途交付的结果等待槽（3B-2.6）。

    交付线程 `open` → 发送 → `event.wait()`；transport 线程收到该 `req_id` 的
    响应帧时 `resolve`（写入 payload 并置位）。两者分属不同线程，故用 Event。
    """

    event: threading.Event
    payload: dict | None = None


class WeComBotConnectionRegistry:
    """进程级「channel 在途 transport 状态」注册表（线程安全）。

    持有两件**同域**的事（都是「本进程内、按 channel 计的连接侧状态」）：

    1. `channel_id → 活连接发送函数`：`aibot_respond_msg` 必须经**活连接**发送，
       而 delivery 由 Worker 在任意时刻调用（`app/application/worker.py`）——
       连接句柄是进程内物理资源，必须有一处显式持有；
    2. `在途交付结果等待槽`（3B-2.6）：交付线程发送后在此登记并等待，由
       **transport 线程**在收到该 req_id 的响应帧时兑现（`recv` 只能有一个调用者，
       见模块 docstring 的线程所有权说明）。

    刻意**不含**订阅 / 重连 / 心跳逻辑（那些在 `WeComBotTransport`），
    也不认识 Channel / Runtime。

    同一 channel 重连时后注册者覆盖前注册者；`unregister` 只允许移除**自己**
    注册的那一个（旧连接的清理绝不误杀新连接——重连竞态下的关键不变量）。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._senders: dict[int, Callable[[str], None]] = {}
        self._acks: dict[tuple[int, str], _AckSlot] = {}

    def register(self, channel_id: int, sender: Callable[[str], None]) -> None:
        with self._lock:
            self._senders[channel_id] = sender

    def unregister(
        self, channel_id: int, sender: Callable[[str], None] | None = None,
    ) -> None:
        """移除 channel 的发送函数；给出 `sender` 时仅在它是当前注册者时移除。"""
        with self._lock:
            current = self._senders.get(channel_id)
            if current is None:
                return
            if sender is not None and current is not sender:
                return  # 已被更新的连接取代：不误杀
            self._senders.pop(channel_id, None)

    def sender_for(self, channel_id: int) -> Callable[[str], None] | None:
        with self._lock:
            return self._senders.get(channel_id)

    def is_connected(self, channel_id: int) -> bool:
        with self._lock:
            return channel_id in self._senders

    def connected_channel_ids(self) -> list[int]:
        with self._lock:
            return sorted(self._senders)

    # ------------------------------------------------------------ 交付结果等待槽

    def expect_ack(self, channel_id: int, req_id: str) -> _AckSlot:
        """登记「等待该 req_id 的交付结果」槽（交付线程调用，随后必须 close）。"""
        slot = _AckSlot(event=threading.Event())
        with self._lock:
            self._acks[(channel_id, req_id)] = slot
        return slot

    def fulfil_ack(
        self, channel_id: int, req_id: str, payload: dict,
    ) -> bool:
        """兑现等待槽（**transport 线程**在收到响应帧时调用）。

        返回 True = 命中某个在途交付；False = 该响应与本进程任何交付无关
        （如 ping 响应 / 订阅后遗留帧）→ 静默忽略，不当成交付结果。
        """
        with self._lock:
            slot = self._acks.get((channel_id, req_id))
        if slot is None:
            return False
        slot.payload = payload
        slot.event.set()
        return True

    def forget_ack(self, channel_id: int, req_id: str) -> None:
        """移除等待槽（交付线程在 finally 中调用，避免槽泄漏）。"""
        with self._lock:
            self._acks.pop((channel_id, req_id), None)


# ================================================================== Adapter


class WeComBotAdapter:
    """`type="wecom_bot"` 双向 Adapter（入站帧解析 / 出站 respond_msg）。

    入站：`verify_handshake` / `parse_message`（InboundChannelAdapter Port）。
    出站：`deliver(outbound)`（ChannelAdapter Port）——经注入的注册表取活连接。

    不 import Runtime / 不触碰 ORM / 不修改契约字段面（与 `wecom.py` 同纪律）。
    """

    def __init__(
        self, *,
        registry: WeComBotConnectionRegistry,
        ack_wait_timeout_s: float = DEFAULT_ACK_WAIT_TIMEOUT_S,
    ) -> None:
        self._registry = registry
        self._ack_wait_timeout_s = ack_wait_timeout_s

    # ------------------------------------------------------------ 入站

    def verify_handshake(
        self,
        channel: Channel,
        *,
        signature: str,
        timestamp: str,
        nonce: str,
        echostr: str,
    ) -> str:
        """WS transport **没有** URL 握手语义（不存在 GET echostr 验证步骤）。

        Fail Closed 而非返回占位值：HTTP 路由 `/api/channels/{id}/wecom/webhook`
        是本 transport 之外的入口，对它给出「成功」等于伪造一次不存在的握手。
        """
        raise InboundChannelConfigError(
            f"channel_id={channel.id} 为 wecom_bot（WebSocket transport）："
            "无 URL 握手语义，回调入口不适用（连接认证发生在 aibot_subscribe）")

    def parse_message(
        self,
        channel: Channel,
        *,
        signature: str,
        timestamp: str,
        nonce: str,
        body: str,
    ) -> InboundInteraction | None:
        """WS 帧 body → InboundInteraction。

        `signature / timestamp / nonce` 对本 transport 无意义（信任来自订阅成功，
        见模块 docstring）——保留参数只为满足 Port 形状（与 local 渠道同形）。
        """
        if channel.id is None:
            raise InboundChannelConfigError(
                "未持久化的 Channel（无 id）不能承接入站消息")
        payload = parse_frame(body, channel_id=channel.id)

        # 渠道归属校验（纵深防御）：帧 body 自称的 aibotid 与渠道配置不符 → 拒收。
        # 与 webhook 用 URL channel_id 定位同性质——都只是「路由/归属」判断，
        # 不构成信任来源；真正的信任是那条订阅连接。
        config = channel.config if isinstance(channel.config, dict) else {}
        expected_bot_id = config.get(CONFIG_BOT_ID)
        frame_body = _frame_body(payload)
        claimed_bot_id = frame_body.get("aibotid") if frame_body is not None else None
        if (isinstance(expected_bot_id, str) and expected_bot_id.strip()
                and isinstance(claimed_bot_id, str) and claimed_bot_id.strip()
                and claimed_bot_id.strip() != expected_bot_id.strip()):
            raise InboundFormatError(
                f"channel_id={channel.id} bot 帧 aibotid 与渠道 bot_id 不符")

        return inbound_from_callback(payload, channel.id)

    # ------------------------------------------------------------ 出站

    def deliver(self, outbound: OutboundResponse) -> None:
        """把 OutboundResponse 经**活连接**发为 `aibot_respond_msg`。

        与 webhook 的 `response_url` 路径的本质差别：本路径**有状态**（依赖本
        进程持有的连接）→ 无连接即 Fail Closed。

        **两层成功语义（3B-2.6）**：
        - Transport：帧写入 socket（发送未抛错）；
        - Application：服务端对该 `req_id` 回 `errcode == 0`（由 transport 线程
          兑现等待槽，见 registry）。
        只有两层都成立才算交付成功——**不再 fire-and-forget**。

        Fail Closed：输入非法 / 缺回信句柄 / 无活连接 / 发送失败 / ACK 超时 /
        ACK errcode != 0 → `ChannelDeliveryFailedError`（不新造错误码）。
        retryable 裁决（仅事实记录；本模块**绝不**自行重试）：
        - 缺回信句柄（契约类，重试无意义）→ False；
        - 无活连接 / 发送失败 / ACK 超时（transport 类）→ True；
        - **ACK errcode != 0（应用层明确拒绝）→ False**：同一个 req_id 不会
          因为重试而变得有效（如已过期 / 已消费），重试无意义。
        """
        if not isinstance(outbound, OutboundResponse):
            raise ChannelDeliveryFailedError()
        if not isinstance(outbound.channel_id, int) or outbound.channel_id <= 0:
            raise ChannelDeliveryFailedError()
        if not isinstance(outbound.external_conversation_id, str) \
                or not outbound.external_conversation_id.strip():
            raise ChannelDeliveryFailedError(channel_id=outbound.channel_id)
        if not isinstance(outbound.content, str) or not outbound.content.strip():
            raise ChannelDeliveryFailedError(channel_id=outbound.channel_id)

        reply_context = reply_context_from(outbound)
        if reply_context is None:
            raise ChannelDeliveryFailedError(
                channel_id=outbound.channel_id,
                external_conversation_id=outbound.external_conversation_id,
                retryable=False,
            )

        sender = self._registry.sender_for(outbound.channel_id)
        if sender is None:
            raise ChannelDeliveryFailedError(
                channel_id=outbound.channel_id,
                external_conversation_id=outbound.external_conversation_id,
                retryable=True,
            )

        req_id = _as_text(reply_context.get(_HEADER_REQ_ID))
        if not req_id:      # 防御性：reply_context_from 已保证非空（理论不可达）
            raise ChannelDeliveryFailedError(
                channel_id=outbound.channel_id,
                external_conversation_id=outbound.external_conversation_id,
                retryable=False,
            )

        frame = compose_respond_frame(outbound.content, reply_context)
        slot = self._registry.expect_ack(outbound.channel_id, req_id)
        try:
            try:
                sender(json.dumps(frame, ensure_ascii=False))
            except ChannelDeliveryFailedError:
                raise
            except Exception as exc:  # noqa: BLE001  连接已断 / 序列化 / 发送失败
                raise ChannelDeliveryFailedError(
                    channel_id=outbound.channel_id,
                    external_conversation_id=outbound.external_conversation_id,
                    retryable=True,
                ) from exc

            if not slot.event.wait(self._ack_wait_timeout_s):
                # 帧已写出但拿不到业务结果：未知是否送达 → transport 类失败。
                raise ChannelDeliveryFailedError(
                    channel_id=outbound.channel_id,
                    external_conversation_id=outbound.external_conversation_id,
                    retryable=True,
                )

            payload = slot.payload if isinstance(slot.payload, dict) else {}
            if payload.get("errcode") != 0:
                # 应用层明确拒绝（req_id 过期 / 已消费 / 限频）→ 重试无意义。
                raise ChannelDeliveryFailedError(
                    channel_id=outbound.channel_id,
                    external_conversation_id=outbound.external_conversation_id,
                    retryable=False,
                )
        finally:
            self._registry.forget_ack(outbound.channel_id, req_id)


# ================================================================== Transport


class WeComBotSubscribeError(Exception):
    """订阅未成功（errcode != 0 / 响应超时 / 连接中断）。

    这是 **transport 层**错误（连接生命周期），**不是** CP 失败码、也不是
    Runtime Failure Model 成员——不外泄到 Application / Domain，只由本模块的
    supervisor 捕获后按退避重连。
    """


@dataclass(frozen=True)
class WeComBotTransportConfig:
    """连接节奏参数（只含基础设施语义，无业务语义）。"""

    heartbeat_interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S
    reconnect_backoff_s: float = DEFAULT_RECONNECT_BACKOFF_S
    recv_timeout_s: float = DEFAULT_RECV_TIMEOUT_S
    subscribe_timeout_s: float = DEFAULT_SUBSCRIBE_TIMEOUT_S
    reconcile_interval_s: float = DEFAULT_RECONCILE_INTERVAL_S


@dataclass
class _Binding:
    """一条 channel 绑定在 transport 内的运行时装（**身份 = channel_id**）。

    - `spec` 可变：DB 侧凭据轮换（同 channel_id、新 `bot_secret`）时由 `reconcile()`
      用**新** material 覆盖并置 `reconnect`，既有 `_run` 据此以新 spec 重连——
      **绝不记录 secret**（只比较/使用，不打日志）；
    - `stop` 是**每绑定**停止事件：REMOVE 只停这一条，不影响其它绑定；
    - `reconnect` 是**每绑定**重连请求：secret 轮换时不新起线程（不产生重复订阅），
      而是让既有 rx 线程关掉当前连接后以更新后的 spec 重连。
    """

    spec: BotConnectionSpec
    inbox: queue.Queue
    stop: threading.Event
    reconnect: threading.Event
    threads: list[threading.Thread]

    @property
    def channel_id(self) -> int:
        return self.spec.channel_id


class WeComBotTransport:
    """长连接 supervisor：每 binding 两条 daemon 线程（rx 收帧 / exec 执行）。

    线程模型（3B-2.6 引入，是**交付结果校验的前置条件**）：

        rx  线程 = socket I/O：connect / subscribe / heartbeat / reconnect / recv。
                   收到消息帧 → 放进 inbox（**绝不在此执行**）；
                   收到响应帧 → 兑现交付等待槽。
        exec线程 = **串行**消费 inbox → `on_frame`（可能阻塞很久：内含 Runtime
                   执行与 Delivery 的 ACK 等待）。

    **为什么必须分开**：`deliver()` 要等待服务端对本进程发出的回复帧的 ACK，而
    ACK 只能由「持有 `recv` 的那个线程」收到。若把 Worker（含 Delivery）跑在 rx
    线程上（3B-2.5 及之前 `main.py::_on_bot_frame` 的实际形态），等待 ACK 就是
    **自我锁死**——唯一能收到 ACK 的线程正卡在等 ACK。分开后 rx 线程始终空闲，
    能持续收帧并兑现 ACK。

    附带收益：执行期间（可能数秒）不再阻塞收帧，心跳与后续用户消息不被拖延。

    职责严格二分（与 `ProductionDriver` 的「Driver 不拥有业务状态机」同纪律）：

        WeComBotTransport  = 连接生命周期（connect / subscribe / heartbeat / reconnect）
        on_frame           = 入站事实如何进入 Core（由组合根注入，通常是
                             「accept_message → worker.process」，即 Trigger 1）

    **绑定集合的运行期调和（M6.6-F2）**：`start()` 只枚举一次 `binding_source`；
    此后 `reconcile()` 周期重调**同一个**注入源（`channel_id` = 绑定身份）并做
    ADD / KEEP / REMOVE——复用既有发现，**不新建发现 / 插件 / registry 框架**。
    单条渠道错误在 `binding_source` 内部隔离，绝不阻断其它 bot。

    本类**不解析业务语义**（只按 `cmd` 分流；另把响应帧兑现给交付等待槽），
    **不执行 Runtime**，**不写库**——它把已确认是消息帧的**原始文本**交给
    `on_frame`，归一化仍由 Adapter / Application 边界唯一完成。

    异常纪律：任何单帧处理失败只记日志并继续（绝不终止循环）；连接异常 →
    退避重连。重复帧由既有 intake 去重（`(channel_id, external_interaction_id)`
    状态感知接纳）兜底，本类不做去重。
    """

    def __init__(
        self,
        *,
        registry: WeComBotConnectionRegistry,
        binding_source: Callable[[], list[BotConnectionSpec]],
        on_frame: Callable[[BotConnectionSpec, str], None],
        connect: Callable[[str], object] | None = None,
        config: WeComBotTransportConfig | None = None,
    ) -> None:
        self._registry = registry
        self._binding_source = binding_source
        self._on_frame = on_frame
        self._connect = connect if connect is not None else _default_connect
        self._config = config or WeComBotTransportConfig()
        self._stop = threading.Event()              # 全局门闩（start/stop）
        self._lock = threading.RLock()              # 保护 _started / _bindings
        self._reconcile_lock = threading.Lock()      # 串行化 reconcile()
        self._started = False
        self._bindings: dict[int, _Binding] = {}     # channel_id → 运行时装
        self._reconciler: threading.Thread | None = None

    # ------------------------------------------------------------ 生命周期

    def start(self) -> None:
        """首轮枚举 `binding_source()` 建绑定线程，并启动运行期调和（幂等）。

        之后由 `reconcile()` 周期重调**同一个**注入源做 ADD / KEEP / REMOVE——
        复用既有发现，不新建发现框架。
        """
        with self._lock:
            if self._started:
                return
            self._started = True
            self._stop.clear()
        self.reconcile()
        self._reconciler = threading.Thread(
            target=self._reconcile_loop, name="wecom-bot-reconcile", daemon=True)
        self._reconciler.start()

    def stop(self, timeout: float | None = 5.0) -> None:
        """请求停止并等待各线程退出（不中断在途 DB 事务）。"""
        with self._lock:
            self._started = False
            self._stop.set()
            bindings = list(self._bindings.values())
            self._bindings = {}
            reconciler = self._reconciler
            self._reconciler = None
        for binding in bindings:
            binding.stop.set()
            self._registry.unregister(binding.channel_id)
        for binding in bindings:
            self._join_threads(binding, timeout)
        if reconciler is not None:
            reconciler.join(timeout)

    @property
    def running(self) -> bool:
        with self._lock:
            return self._started

    def reconcile(self) -> None:
        """运行期调和：重调 `binding_source()`，对 channel_id 做 ADD / KEEP / REMOVE。

        纪律（M6.6-F2）：
        - 复用注入的 `binding_source`（组合根的 `discover_bot_connections`），
          本方法**不**自己发现、不引入新的 registry / plugin；
        - `channel_id` 是绑定身份；
        - **ADD**：新 `channel_id` → 起 rx+exec 线程（订阅）；
        - **KEEP**：同 `channel_id` → 线程不动（**绝不产生重复线程 / 重复订阅**）；
          若 spec 变化（如 secret 轮换）→ 更新 spec 并置 `reconnect`，由既有
          `_run` 以**新** material 重连（只读不打印 secret）；
        - **REMOVE**：消失的 `channel_id` → 停**该**绑定 + 注销活连接 + 清理
          （只停这一条，不影响其它绑定）；
        - 单条渠道非法 / 不完整 config 由 `binding_source` 内部跳过（单点错误绝不
          阻断其它 bot）；`binding_source` 整体抛错 → 保留现有绑定（不误拆）。
        """
        if not self._started:
            return
        with self._reconcile_lock:
            if not self._started:
                return
            try:
                specs = self._binding_source()
            except Exception:  # noqa: BLE001  发现读取失败：保留现状，不误拆
                LOGGER.exception(
                    "wecom bot binding discovery failed; keeping existing bindings")
                return
            desired: dict[int, BotConnectionSpec] = {}
            for spec in specs:
                if spec.channel_id in desired:
                    LOGGER.warning(
                        "duplicate wecom_bot binding ignored: channel_id=%s",
                        spec.channel_id)
                    continue
                desired[spec.channel_id] = spec

            to_stop: list[_Binding] = []
            to_start: list[_Binding] = []
            with self._lock:
                if not self._started:      # 与 stop() 竞态：不再变更
                    return
                for channel_id, binding in list(self._bindings.items()):
                    new_spec = desired.pop(channel_id, None)
                    if new_spec is None:
                        to_stop.append(binding)          # REMOVE
                        del self._bindings[channel_id]
                    elif new_spec != binding.spec:
                        binding.spec = new_spec          # KEEP（新 material）
                        binding.reconnect.set()           # → 既有线程重连
                for channel_id, spec in desired.items():  # ADD
                    binding = self._new_binding(spec)
                    self._bindings[channel_id] = binding
                    to_start.append(binding)

            for binding in to_stop:
                self._teardown_binding(binding)
            for binding in to_start:
                self._launch_binding(binding)

    def _reconcile_loop(self) -> None:
        """周期调和（异常绝不终止循环）。"""
        while not self._stop.wait(self._config.reconcile_interval_s):
            try:
                self.reconcile()
            except Exception:  # noqa: BLE001
                LOGGER.exception("wecom bot reconcile failed")

    # ------------------------------------------------------------ 绑定装配

    @staticmethod
    def _new_binding(spec: BotConnectionSpec) -> _Binding:
        """构造运行时装（**不**起线程；由 `_launch_binding` 统一启动）。"""
        return _Binding(
            spec=spec, inbox=queue.Queue(), stop=threading.Event(),
            reconnect=threading.Event(), threads=[])

    def _launch_binding(self, binding: _Binding) -> None:
        """为一条绑定启动 rx + exec 两条线程（已停止则跳过，防泄漏）。"""
        if binding.stop.is_set():
            return
        for target, role in ((self._consume, "exec"), (self._run, "rx")):
            thread = threading.Thread(
                target=target, args=(binding,),
                name=f"wecom-bot-{role}-{binding.channel_id}", daemon=True)
            binding.threads.append(thread)
            thread.start()
        LOGGER.info(
            "wecom bot transport started, channel_id=%s", binding.channel_id)

    def _teardown_binding(
        self, binding: _Binding, timeout: float | None = 5.0,
    ) -> None:
        """停**这一条**绑定并清理（只影响该 channel_id）。"""
        binding.stop.set()
        self._registry.unregister(binding.channel_id)
        self._join_threads(binding, timeout)

    @staticmethod
    def _join_threads(binding: _Binding, timeout: float | None) -> None:
        """等待已启动的线程退出（未启动的线程不可 join）。"""
        for thread in binding.threads:
            if thread.ident is not None:
                thread.join(timeout)

    # ------------------------------------------------------------ 内部

    def _consume(self, binding: _Binding) -> None:
        """exec 线程：**串行**消费入站帧并交给 `on_frame`（可长时间阻塞）。

        刻意串行（而非每帧一条线程）：不引入新的 DB 并发面，且与「一次一条
        interaction 执行」的既有语义一致；同时保证 rx 线程始终空闲以兑现 ACK。
        业务失败（含 Runtime typed error——其终态已由 Worker 落定）只记日志，
        绝不终止循环。
        """
        while not binding.stop.is_set():
            try:
                raw = binding.inbox.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._on_frame(binding.spec, raw)
            except Exception:  # noqa: BLE001
                LOGGER.exception(
                    "wecom bot inbound handling failed: channel_id=%s",
                    binding.channel_id)

    def _run(self, binding: _Binding) -> None:
        """rx 线程：断线 → 退避 → 重连（异常绝不终止循环）。

        停止粒度是**每绑定**（`binding.stop`），故 REMOVE 只停这一条；`binding.spec`
        变化（如 secret 轮换）时 reconcile 置 `binding.reconnect`，本循环据此关掉
        当前连接并**立即**（不退避）以新 material 重连。
        """
        while not binding.stop.is_set():
            try:
                self._connection_attempt(
                    binding.spec, binding.inbox,
                    binding.stop, binding.reconnect)
            except Exception:  # noqa: BLE001  连接层失败：重连是唯一正确反应
                LOGGER.exception(
                    "wecom bot session failed, will reconnect: channel_id=%s",
                    binding.channel_id)
            finally:
                self._registry.unregister(binding.channel_id)
            if binding.stop.is_set():
                return
            if binding.reconnect.is_set():
                binding.reconnect.clear()   # spec 已更新：立即重连，不退避
                continue
            # 退避等待期间可被 stop() 立刻唤醒（与 ProductionDriver 同纪律）。
            if binding.stop.wait(self._config.reconnect_backoff_s):
                return

    def _connection_attempt(
        self, spec: BotConnectionSpec, inbox: queue.Queue,
        stop: threading.Event | None = None,
        reconnect: threading.Event | None = None,
    ) -> None:
        """一次连接生命周期的完整流程（返回或抛错都意味着需要重连）。

        `stop` / `reconnect` 为**每绑定**事件（`_run` 传入）；单测直接调用时可省，
        此时以全局 `self._stop` 为停止条件（与旧行为一致）。**本方法不注销活连接**
        ——注销由 `_run` 的 finally 负责（订阅成功后连接保持注册到会话结束，3B-2.2
        既有语义）。
        """
        stop = stop if stop is not None else self._stop
        connection = self._connect(spec.ws_url)
        with connection:
            self._subscribe(connection, spec, stop)
            self._registry.register(spec.channel_id, connection.send)
            LOGGER.info("wecom bot connected, channel_id=%s", spec.channel_id)
            self._receive_loop(spec, connection, inbox, stop, reconnect)

    def _subscribe(
        self, connection, spec: BotConnectionSpec, stop: threading.Event,
    ) -> None:
        """发送 `aibot_subscribe` 并**等待并校验**订阅响应（errcode 必须为 0）。

        为什么必须校验：凭据错误 / 被限频时服务端返回 `errcode != 0`。若不校验就
        注册连接，本地会显示「已连接」而后续 deliver 全部落空（**假绿**）。
        因此**只有 errcode == 0 才注册**；否则抛 `WeComBotSubscribeError` →
        supervisor 退避重连（Fail Closed，不进入 connected）。

        `spec.bot_secret` 只用于构造订阅帧，**绝不写日志**（凭据纪律）。
        """
        req_id = uuid.uuid4().hex
        connection.send(json.dumps(
            compose_subscribe_frame(spec.bot_id, spec.bot_secret, req_id),
            ensure_ascii=False))
        response = self._await_response(connection, req_id, stop)
        errcode = response.get("errcode")
        if errcode != 0:
            raise WeComBotSubscribeError(
                f"channel_id={spec.channel_id} 订阅被拒："
                f"errcode={errcode} errmsg={response.get('errmsg')!r}")

    def _await_response(
        self, connection, req_id: str, stop: threading.Event,
    ) -> dict:
        """等待 `headers.req_id` 匹配的响应帧（响应帧无 `cmd`、有 `errcode`）。

        期间若收到推送帧（带 `cmd`）则忽略——订阅阶段不应有业务推送。超时 /
        连接关闭 / 被要求停止 → `WeComBotSubscribeError`（Fail Closed）。
        """
        deadline = time.monotonic() + self._config.subscribe_timeout_s
        while not stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WeComBotSubscribeError(f"订阅响应超时（req_id={req_id}）")
            try:
                raw = connection.recv(
                    timeout=min(remaining, self._config.recv_timeout_s))
            except TimeoutError:
                continue
            if raw is None:
                raise WeComBotSubscribeError("订阅响应到达前连接被关闭")
            try:
                payload = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue          # 非 JSON / 二进制帧：忽略（不猜）
            if not isinstance(payload, dict) or payload.get("cmd"):
                continue          # 推送帧：订阅阶段忽略
            echoed = frame_req_id(payload)
            if echoed and echoed != req_id:
                continue          # 不是本次请求的响应
            if "errcode" not in payload:
                continue
            return payload
        raise WeComBotSubscribeError("等待订阅响应期间收到停止请求")

    def _receive_loop(
        self, spec: BotConnectionSpec, connection, inbox: queue.Queue,
        stop: threading.Event, reconnect: threading.Event | None = None,
    ) -> None:
        """收帧主循环；按**官方节奏**（建议 30s）发送应用层心跳。

        心跳必须节流：`recv` 超时是秒级的，每次超时都发 ping 会变成 1s 一次，
        既不符合官方语义也容易触发服务端频率保护。

        `reconnect`（每绑定）被置位时主动返回 → 连接关闭 → `_run` 以更新后的
        spec 重连（secret 轮换走这条路，**不**新起线程 / 不产生重复订阅）。
        """
        last_beat = time.monotonic()
        while not stop.is_set():
            if reconnect is not None and reconnect.is_set():
                return
            try:
                raw = connection.recv(timeout=self._config.recv_timeout_s)
            except TimeoutError:
                now = time.monotonic()
                if now - last_beat >= self._config.heartbeat_interval_s:
                    self._send_ping(connection, spec.channel_id)
                    last_beat = now
                continue
            if raw is None:  # 对端正常关闭
                return
            self._handle_frame(spec, raw, inbox)

    @staticmethod
    def _send_ping(connection, channel_id: int) -> None:
        """应用层心跳（官方 `cmd=ping`，每 30s）。

        发送失败不在此抛错：由下一次 `recv` 暴露（断连）并触发 supervisor 重连。
        """
        try:
            connection.send(json.dumps(
                compose_ping_frame(uuid.uuid4().hex), ensure_ascii=False))
        except Exception:  # noqa: BLE001
            LOGGER.debug(
                "wecom bot heartbeat send failed: channel_id=%s",
                channel_id, exc_info=True)

    def _handle_frame(
        self, spec: BotConnectionSpec, raw: object, inbox: queue.Queue,
    ) -> None:
        """按 cmd 分流：响应帧兑现等待槽；消息帧入队给 exec 线程；其余忽略。"""
        try:
            payload = json.loads(raw) if isinstance(raw, str) else None
        except json.JSONDecodeError:
            payload = None
        if not isinstance(payload, dict):
            LOGGER.warning(
                "wecom bot frame is not a JSON object, ignored: channel_id=%s",
                spec.channel_id)
            return
        if payload.get("cmd") != CMD_MSG_CALLBACK:
            # 响应帧（无 cmd、有 errcode、echo req_id）：兑现该 req_id 的在途投递
            # 等待槽（3B-2.6 交付结果校验）。ping 响应 / 其它无关 req_id 不会命中
            # 任何槽 → 静默忽略（不会把无关响应误当成交付结果）。
            req_id = frame_req_id(payload)
            if req_id and payload.get("errcode") is not None:
                self._registry.fulfil_ack(spec.channel_id, req_id, payload)
            return
        # 消息帧：交给 exec 线程处理——rx 线程**绝不阻塞在执行/等待 ACK 上**
        # （见类 docstring 的自我锁死说明）。
        try:
            inbox.put(raw)
        except Exception:  # noqa: BLE001
            LOGGER.exception(
                "wecom bot inbox rejected frame: channel_id=%s",
                spec.channel_id)


def _default_connect(url: str):
    """缺省连接工厂（生产）：`websockets` 同步客户端。

    懒 import：本模块的纯函数 / 注册表 / Adapter 语义不需要该依赖即可测试与阅读。

    **必须禁用 websockets 内置的协议级 keepalive（`ping_interval=None`）**——
    3B-2.5 真实联调实证（DEBUG 帧级证据）：

        > PING 54 b4 25 7c [binary, 4 bytes]      ← websockets 默认 ping_interval=20
        % sent keepalive ping
        > CLOSE 1002 (protocol error) incorrect masking   ← 12ms 后自行判协议错误并关闭

    即：内置 keepalive 会发送 **WebSocket 协议级 PING**，而该服务端对该 PING 的
    响应会被 websockets 判为 `incorrect masking` → 客户端主动以 1002 关闭 →
    连接约每 25s 断一次，并反复重订阅（有触发官方「订阅请求频率保护」的实际风险）。

    官方协议把心跳定义在**应用层**（`{"cmd":"ping"}`，建议 30s，见 `_send_ping`），
    因此这里必须**关闭**协议级 keepalive（`ping_interval=None`），而不是调参；
    服务端主动发来的协议级 PING 由 websockets 自动回 PONG（联调实测正常，
    不受本开关影响）。
    """
    from websockets.sync.client import connect

    return connect(
        url,
        ping_interval=None,          # 关闭内置协议级 keepalive（原因见 docstring）
        open_timeout=DEFAULT_CONNECT_OPEN_TIMEOUT_S,
        close_timeout=DEFAULT_CONNECT_CLOSE_TIMEOUT_S,
    )
