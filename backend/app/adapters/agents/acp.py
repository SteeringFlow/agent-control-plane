"""ACP Agent Adapter（Step 10.1.2 + 10.1.3）—— AgentAdapter Port 的第一个真实实现。

位置：`app/adapters/agents/acp.py`（Port 在 `app/repositories/agent_adapter.py`，
由 `AgentDispatchExecutor` 构造注入；本模块**不被** application/coordinator import）。

两种 binding（由 `Connection.config["protocol"]` 选择，默认 minimal 保持 10.1.2 语义）：

1. **minimal**（Step 10.1.2 反证实验用，默认）：最小 ACP HTTP binding
       POST {config.endpoint}  body {"message": content, "session_id": external_session_id}
       headers: Content-Type/Accept: application/json + X-Acp-Credential-Ref
       200 → {"content": str, "session_id": str|None}
   仅用于本地真实 HTTP 反证（无真实 wire spec 时的占位），不面向生产。

2. **tencent-acp-sse**（Step 10.1.3 wire calibration，`protocol="tencent-acp-sse"`）：
   面向腾讯 CodeBuddy Enterprise ACP Runtime（Runtime 创建后给出
   `links.acpLink.url` / `links.acpLink.token`；默认 session id = runtime.id）：
       a. 生成 `Acp-Connection-Id`（每次 send 独立，不缓存连接）；
       b. GET {endpoint}（headers: Authorization Bearer <acpLink.token> +
          Acp-Connection-Id + Accept: text/event-stream）→ 建立持久 SSE；
       c. POST {endpoint} jsonrpc 2.0 `session/prompt`：
            {"jsonrpc":"2.0","id":1,"method":"session/prompt",
             "params":{"sessionId": <runtime.id 或 external_session_id>,
                       "prompt":[{"type":"text","text": <AgentRequest.content>}]}}
       d. 最终文本经 GET 的 SSE 事件送达：`session/update` →
          `agent_message_chunk` → `content.text`（逐块拼接）；
       e. 收尾：事件中出现 `codebuddy.ai.isFinalStop`/`isFinalStop`=true 或
          `turn_finished` 或 SSE EOF（已收文本）即视为完成。
   凭据/配置只来自 `Connection.config`：endpoint / bearer_token（acpLink.token）
   / runtime_id；不做 initialize（腾讯 Runtime 创建即自带 session）、不缓存连接、
   不重试、不刷新 token（10.1.3 范围外的生产连接基础设施一律不做）。

实验变量控制（10.1.2/10.1.3）：Channel = Test Channel / Runtime Kernel = 既有
coordinator+dispatch / Adapter = **真实** ACPAdapter / Agent = **真实 Tencent
ACP Runtime**（或本地 SSE 模拟 Tencent Runtime）。失败只会落在 Adapter / Agent 层。

ACPAdapter 可以：
- 读取 Connection.endpoint/config/credential_ref；做 ACP authentication / HTTP
  transport / ACP serialization；管理 ACP protocol session（external_session_id 透传）；
  把 ACP response 归一化成 AgentResponse；
- `probe(connection)`（Cloud-0.1.2.2.2.1）：即时可达性诊断——只读 Connection.config，
  Fail-Closed，复用真实建连路径，返回 `AgentProbeSnapshot`（不写库/不缓存/不重试/
  不刷新 token）。这是「此刻能否连上」的一次性快照，**不是**持久健康状态系统。

ACPAdapter **不可以**（Protocol Translation，不是 Mini Control Plane）：
- 自己选择 Agent / Connection；查询 Repository；创建或修改 Control Plane Session；
  重新做 Identity Resolution / Routing；直接 Delivery；import ORM/SQLAlchemy；
  把 ACP 专有概念塞进 AgentRequest/AgentResponse。

失败语义（只映射到既有失败码，不新造）：
- transport / HTTP / protocol 失败（不可达、非 200、body 不可解、schema 不符、
  SSE 未在超时内收尾）
  → `AgentDispatchFailedError`（AGENT_DISPATCH_FAILED）；
- ACP 语义可用但 Agent 没有可用内容（content 为空串）→ 归一化为
  `AgentResponse(content="")`，由 Dispatch 契约校验拒绝 →
  `AgentResponseInvalidError`（AGENT_RESPONSE_INVALID）；
- 具体 ACP 错误（401/500/unknown session…）全部留在本 Adapter 内部，不泄漏成
  新的 Control Plane 错误码。
"""

from __future__ import annotations

import http.client
import json
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from app.application.errors import AgentDispatchFailedError
from app.domain.agent_probe_snapshot import AgentProbeSnapshot
from app.domain.agent_request import AgentRequest
from app.domain.agent_response import AgentResponse
from app.domain.connection import Connection

DEFAULT_TIMEOUT_S = 30.0

# Binding 选择：config["protocol"]；None/""/"minimal" → minimal（10.1.2 反证语义）
PROTOCOL_MINIMAL = "minimal"
PROTOCOL_TENCENT_ACP_SSE = "tencent-acp-sse"
_TENCENT_PROTOCOLS = ("tencent-acp-sse", "tencent", "acp-sse")


# ================================================================== Tencent SSE 事件解析（模块级纯函数，供测试直测）

# ================================================================== Wire Content Classification（Step 12.8.5）
#
# 目标：确定 Tencent ACP wire 上 `reasoning` 以什么结构存在，据此把 content 块
# 分类为 REASONING（丢弃）或 CONTENT（归一为 AgentResponse.content）。
#
# 只按**结构信号**分类，绝不按自然语言启发式（"Let me..." 之类）——那属于启发式
# 污染协议层。三种结构可能性：
#   Case A — 独立 type：{"type":"reasoning","text":...} → 丢弃；
#   Case B — text node + 语义字段：{"type":"text","role":"reasoning",...} → 丢弃；
#   Case C — reasoning 与正文 fused：结构上无法区分 → 整块作为 CONTENT 透传
#            （宁可泄漏 reasoning，绝不误删用户可见正文）。
# 识别不到信号即视为 Case C 兜底。
#
# 12.8.6 真实 wire 校准结论：**Tencent ACP = Case B（信号在父级 update 信封）**。
# reasoning 与正文都是裸 {"type":"text","text":...}，无块级信号；区分靠
# `update.sessionUpdate`："agent_thought_chunk" → REASONING、"agent_message_chunk"
# → CONTENT（真实捕获 96/129 块双向验证，见
# docs/step-12.8.6-real-acp-wire-classification-smoke.md）。

CONTENT = "content"
REASONING = "reasoning"

# Case A：独立 reasoning type 的候选值（ACP / OpenAI-ish / 腾讯内部命名兼容；
# 12.8.6 真实 wire 观测后可在此补充真实分型名）。
_REASONING_TYPES = frozenset({
    "reasoning",
    "reasoning_content",
    "thinking",
    "thought",
    "thinking_content",
})

# Case B：type=="text" 时，role 命中这些才判定为 reasoning。
_REASONING_ROLES = frozenset({"reasoning", "thinking", "thought"})

# 12.8.6 真实 wire 校准（Case B，信号在父级 update 信封）：Tencent 的 reasoning 与
# 正文都是裸 {"type":"text","text":...} 块，区分信号是父级
# `update.sessionUpdate`：`"agent_thought_chunk"` → REASONING，
# `"agent_message_chunk"` → CONTENT（真实捕获 96/129 块双向验证）。
_REASONING_SESSION_UPDATES = frozenset({
    "agent_thought_chunk",
    "agent_thought",
    "thought_chunk",
})


def _update_kind_marks_reasoning(value) -> bool:
    if not isinstance(value, str):
        return False
    lowered = value.lower()
    return lowered in _REASONING_SESSION_UPDATES or "thought" in lowered


def classify_content_block(block) -> str:
    """单个 wire content 块 → CONTENT 或 REASONING（只按结构信号）。"""
    if not isinstance(block, dict):
        return CONTENT
    btype = block.get("type")
    if isinstance(btype, str) and btype in _REASONING_TYPES:
        return REASONING
    if btype == "text":
        role = block.get("role")
        if isinstance(role, str) and role.lower() in _REASONING_ROLES:
            return REASONING
        if block.get("reasoning") is True:
            return REASONING
        meta = block.get("metadata")
        if isinstance(meta, dict) and meta.get("reasoning") is True:
            return REASONING
    return CONTENT


def _is_content_block(node) -> bool:
    """dict 是否应被当作 content 块消费（不再递归）。"""
    if not isinstance(node, dict):
        return False
    btype = node.get("type")
    if not isinstance(btype, str):
        return False
    if btype in _REASONING_TYPES:
        return True
    if btype == "text":
        # 只有确实带 text 的 text 块才作为叶块消费；仅含子结构的 text node
        # 继续递归，避免漏收嵌套正文（Case C 兜底）。
        return isinstance(node.get("text"), str)
    return False


def extract_content_texts(node) -> list[str]:
    """递归遍历 SSE 事件 JSON，仅收集 CONTENT 块（REASONING 丢弃）。

    信封感知：text 块所在 `update.sessionUpdate` 命中 reasoning 信号 → REASONING
    （12.8.6 真实 wire：reasoning 与正文同形，靠父级信封区分）。
    """
    out: list[str] = []
    _walk_content(node, out)
    return out


def extract_all_texts(node) -> tuple[list[str], list[str]]:
    """递归遍历；返回 (content_parts, reasoning_parts)，reasoning 不丢弃。"""
    contents: list[str] = []
    reasonings: list[str] = []
    _walk_content_split(node, contents, reasonings)
    return contents, reasonings


def _walk_content(node, contents: list[str], in_reasoning: bool = False) -> None:
    if isinstance(node, dict):
        update_kind = node.get("sessionUpdate")
        if isinstance(update_kind, str):
            in_reasoning = _update_kind_marks_reasoning(update_kind)
        if _is_content_block(node):
            if not in_reasoning and classify_content_block(node) == CONTENT:
                text = node.get("text")
                if isinstance(text, str) and text:
                    contents.append(text)
            return
        for value in node.values():
            _walk_content(value, contents, in_reasoning)
    elif isinstance(node, list):
        for item in node:
            _walk_content(item, contents, in_reasoning)


def _walk_content_split(
    node, contents: list[str], reasonings: list[str], in_reasoning: bool = False,
) -> None:
    if isinstance(node, dict):
        update_kind = node.get("sessionUpdate")
        if isinstance(update_kind, str):
            in_reasoning = _update_kind_marks_reasoning(update_kind)
        if _is_content_block(node):
            text = node.get("text")
            if isinstance(text, str) and text:
                if in_reasoning or classify_content_block(node) == REASONING:
                    reasonings.append(text)
                else:
                    contents.append(text)
            return
        for value in node.values():
            _walk_content_split(value, contents, reasonings, in_reasoning)
    elif isinstance(node, list):
        for item in node:
            _walk_content_split(item, contents, reasonings, in_reasoning)


# ================================================================== SSE 事件解析


def _sse_event_texts_and_final(raw: str) -> tuple[bool, list[str], str | None]:
    """解析一条 SSE `data:` JSON 事件 → (is_final, text_parts, error)。

    - is_final：事件内含 `codebuddy.ai.isFinalStop`/`isFinalStop`=true 或
      `type=="turn_finished"`；
    - text_parts：递归收集 `type=="text"` 且带非空 `text` 的 content 块
      （agent_message_chunk / agent_message_delta 的标准内容形态）；
    - error：事件为 jsonrpc `error` 对象时返回错误文本（调用方转 Dispatch failure）。
    """
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return False, [], None
    if not isinstance(obj, dict):
        return False, [], None
    final = _json_contains_final(obj)
    error = _json_rpc_error_text(obj)
    if error is not None:
        return False, [], error
    texts: list[str] = []
    _json_collect_text(obj, texts)
    return final, texts, None


def _json_contains_final(node) -> bool:
    if isinstance(node, dict):
        for key, value in node.items():
            # 腾讯真实事件已观察到三种收尾形态（PowerShell 实测）：
            #   1. `codebuddy.ai.isFinalStop`/`isFinalStop` = true
            #   2. update `type == "session/endTurn"`（带 stopReason）
            #   3. `stopReason == "end_turn"`
            if key in ("codebuddy.ai.isFinalStop", "isFinalStop") and value is True:
                return True
            if key == "type" and value in ("turn_finished", "session/endTurn", "endTurn"):
                return True
            if key == "stopReason" and value == "end_turn":
                return True
            if _json_contains_final(value):
                return True
    elif isinstance(node, list):
        for item in node:
            if _json_contains_final(item):
                return True
    return False


def _json_collect_text(node, texts: list[str]) -> None:
    """递归收集 SSE 事件内所有 CONTENT 块（REASONING 由分类器丢弃，Step 12.8.5）。

    仅按 wire 结构信号分类：Case A/B 的 reasoning 块被丢弃，Case C（无法从结构上
    区分 reasoning 与正文）整块作为 CONTENT 保留——绝不做自然语言启发式过滤。
    """
    texts.extend(extract_content_texts(node))


def _json_rpc_error_text(node) -> str | None:
    if isinstance(node, dict):
        if isinstance(node.get("error"), dict):
            return json.dumps(node["error"], ensure_ascii=False)
        for value in node.values():
            err = _json_rpc_error_text(value)
            if err is not None:
                return err
    elif isinstance(node, list):
        for item in node:
            err = _json_rpc_error_text(item)
            if err is not None:
                return err
    return None


def _split_endpoint(endpoint: str) -> tuple[str, str, int, str]:
    """endpoint URL → (scheme, host, port, path_with_query)。"""
    parts = urllib.parse.urlsplit(endpoint)
    scheme = (parts.scheme or "https").lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"unsupported scheme: {scheme}")
    host = parts.hostname or ""
    if not host:
        raise ValueError("missing host")
    port = parts.port or (443 if scheme == "https" else 80)
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    return scheme, host, port, path


#: 探针失败文案（M6.6-F4-A Q9）：**泛化、可行动**——绝不回显 endpoint / host /
#: port / URL / 凭据 / 异常原文。`detail` 是给用户看的诊断结论，不是内部 stack：
#: 任何「连接细节」都可能含敏感信息，故一律收敛为固定文案（HTTP 状态码除外，
#: 它不含连接细节且有既有测试依赖）。
_PROBE_DETAIL_UNREACHABLE = "agent endpoint could not be reached"
_PROBE_DETAIL_INVALID_ENDPOINT = "connection config missing/invalid endpoint"


class ACPAdapter:
    """把 Frozen `AgentRequest` 经 ACP 发给真实 Agent（minimal / Tencent SSE）。

    `send(request, connection)` 与 Port 形状一致（T8：Port 不变）。只消费
    `Connection.credential_ref` / `Connection.config` 完成连接与鉴权。

    `event_recorder`（Step 12.8.6 wire 观测，默认 None = 无行为变化）：可选
    回调 `callable(raw_event: str) -> None`，在 Tencent SSE binding 下逐条收到
    原始 `data:` 事件时调用（解析前透传），用于真实 wire 校准/分类 smoke。
    """

    def __init__(self, event_recorder=None) -> None:
        self._event_recorder = event_recorder

    def send(self, request: AgentRequest, connection: Connection) -> AgentResponse:
        """一次 request → response（Fail Closed，不返回半成品）。"""
        protocol = (connection.config or {}).get("protocol") or PROTOCOL_MINIMAL
        if protocol in _TENCENT_PROTOCOLS:
            return self._send_tencent_acp_sse(request, connection)
        return self._send_minimal(request, connection)

    # ------------------------------------------------------------------ 可达性即时诊断（Cloud-0.1.2.2.2.1）

    def probe(self, connection: Connection) -> AgentProbeSnapshot:
        """此刻能否连上 Agent runtime —— 即时诊断（非持久健康状态系统）。

        严格契约（Gate 0.1.2.2.2 §2/§3/§7）：

        - **只读** `Connection.config` / `credential_ref`（凭据纪律同 `send`，
          绝不读 `Agent.credential_ref`）；
        - **Fail Closed**：配置缺失 / 不可达 / 非 200 → `reachable=False`（附
          `detail`），绝不抛异常、绝不返回半成品；
        - **复用真实 ACP 建连路径**：tencent-acp-sse 走真实 GET SSE 建连（同
          `_send_tencent_acp_sse` 第 1 步，带 Authorization Bearer + Acp-Connection-Id）；
          minimal 走最小 TCP 可达性；
        - **无副作用**：不写库、不缓存连接、不重试、不刷新 token、不发送 prompt。

        返回纯值对象 `AgentProbeSnapshot`（`checked_at` 由调用方在请求时刻生成）。
        输入非法一律归一为 `reachable=False`，不抛。
        """
        if not isinstance(connection, Connection):
            return AgentProbeSnapshot(
                reachable=False, detail="invalid connection", probe=None)
        config = connection.config if isinstance(connection.config, dict) else {}
        protocol = config.get("protocol") or PROTOCOL_MINIMAL
        if protocol in _TENCENT_PROTOCOLS:
            return self._probe_tencent_acp_sse(config)
        return self._probe_minimal(config)

    def _probe_tencent_acp_sse(self, config: dict) -> AgentProbeSnapshot:
        """Tencent ACP SSE：复用真实 GET SSE 建连（status 200 = 可达），随即关闭。"""
        probe = PROTOCOL_TENCENT_ACP_SSE
        endpoint = config.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint.strip() \
                or not endpoint.startswith(("http://", "https://")):
            return AgentProbeSnapshot(False, "connection config missing/invalid endpoint", probe)
        token = config.get("bearer_token")
        if not isinstance(token, str) or not token.strip():
            return AgentProbeSnapshot(False, "connection config missing bearer_token", probe)
        try:
            timeout_s = float(config.get("timeout_s", DEFAULT_TIMEOUT_S))
        except (TypeError, ValueError):
            return AgentProbeSnapshot(False, "connection config invalid timeout_s", probe)
        if timeout_s <= 0:
            return AgentProbeSnapshot(False, "connection config invalid timeout_s", probe)
        try:
            scheme, host, port, path = _split_endpoint(endpoint)
        except ValueError:
            return AgentProbeSnapshot(False, _PROBE_DETAIL_INVALID_ENDPOINT, probe)

        conn: http.client.HTTPConnection | None = None
        try:
            conn = _open_http_connection(scheme, host, port, timeout_s)
            conn.request("GET", path, headers={
                "Authorization": f"Bearer {token}",
                "Acp-Connection-Id": uuid.uuid4().hex,
                "Accept": "text/event-stream",
            })
            resp = conn.getresponse()
            if resp.status == 200:
                return AgentProbeSnapshot(True, None, probe)
            return AgentProbeSnapshot(False, f"GET SSE status {resp.status}", probe)
        except Exception:  # noqa: BLE001 诊断：任何失败都只是「不可达」
            return AgentProbeSnapshot(False, _PROBE_DETAIL_UNREACHABLE, probe)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass

    def _probe_minimal(self, config: dict) -> AgentProbeSnapshot:
        """minimal binding：最小 TCP 可达性（不发送消息，无副作用）。"""
        probe = PROTOCOL_MINIMAL
        endpoint = config.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint.strip() \
                or not endpoint.startswith(("http://", "https://")):
            return AgentProbeSnapshot(False, "connection config missing/invalid endpoint", probe)
        try:
            timeout_s = float(config.get("timeout_s", DEFAULT_TIMEOUT_S))
        except (TypeError, ValueError):
            return AgentProbeSnapshot(False, "connection config invalid timeout_s", probe)
        if timeout_s <= 0:
            return AgentProbeSnapshot(False, "connection config invalid timeout_s", probe)
        try:
            scheme, host, port, _path = _split_endpoint(endpoint)
        except ValueError:
            return AgentProbeSnapshot(False, _PROBE_DETAIL_INVALID_ENDPOINT, probe)

        conn: http.client.HTTPConnection | None = None
        try:
            conn = _open_http_connection(scheme, host, port, timeout_s)
            conn.connect()
            return AgentProbeSnapshot(True, None, probe)
        except Exception:  # noqa: BLE001 诊断：任何失败都只是「不可达」
            return AgentProbeSnapshot(False, _PROBE_DETAIL_UNREACHABLE, probe)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass

    # ------------------------------------------------------------------ minimal binding（10.1.2 默认）

    def _send_minimal(
        self, request: AgentRequest, connection: Connection,
    ) -> AgentResponse:
        endpoint, timeout_s = self._endpoint_and_timeout(request, connection)
        headers, body = self._compose_acp_request(request, connection)
        raw = self._http_post(request, connection, endpoint, headers, body, timeout_s)
        return self._normalize_acp_response(request, connection, raw)

    # ------------------------------------------------------------------ Tencent ACP SSE binding（10.1.3）

    def _send_tencent_acp_sse(
        self, request: AgentRequest, connection: Connection,
    ) -> AgentResponse:
        """GET 持久 SSE + POST jsonrpc session/prompt，SSE 事件收尾 → AgentResponse。

        Fail Closed：endpoint/token/session 缺失或非法 → AGENT_DISPATCH_FAILED；
        SSE 未在 timeout_s 内以 final/EOF 收尾 → AGENT_DISPATCH_FAILED。
        """
        config = connection.config if isinstance(connection.config, dict) else {}
        endpoint = config.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint.strip() \
                or not endpoint.startswith(("http://", "https://")):
            raise AgentDispatchFailedError(
                agent_id=request.agent_id, connection_id=connection.id)
        token = config.get("bearer_token")
        if not isinstance(token, str) or not token.strip():
            raise AgentDispatchFailedError(
                agent_id=request.agent_id, connection_id=connection.id)
        runtime_id = config.get("runtime_id")
        try:
            timeout_s = float(config.get("timeout_s", DEFAULT_TIMEOUT_S))
        except (TypeError, ValueError):
            raise AgentDispatchFailedError(
                agent_id=request.agent_id, connection_id=connection.id) from None
        if timeout_s <= 0:
            raise AgentDispatchFailedError(
                agent_id=request.agent_id, connection_id=connection.id)

        # sessionId：优先 request.external_session_id（第二轮回流），否则 runtime.id
        session_id = request.external_session_id
        if session_id is None:
            if runtime_id is None:
                raise AgentDispatchFailedError(
                    agent_id=request.agent_id, connection_id=connection.id)
            session_id = str(runtime_id)

        conn_id = uuid.uuid4().hex
        headers = {
            "Authorization": f"Bearer {token}",
            "Acp-Connection-Id": conn_id,
        }
        scheme, host, port, path = _split_endpoint(endpoint)

        # ---- 1. GET 建立持久 SSE（后台读线程）----
        events: queue.Queue[str | None] = queue.Queue()
        errors: list[str] = []
        ready = threading.Event()
        sse_holder: dict[str, http.client.HTTPConnection | None] = {"conn": None}

        def _sse_reader() -> None:
            conn: http.client.HTTPConnection | None = None
            try:
                conn = _open_http_connection(scheme, host, port, timeout_s)
                sse_holder["conn"] = conn
                conn.request(
                    "GET", path,
                    headers={**headers, "Accept": "text/event-stream"})
                resp = conn.getresponse()
                if resp.status != 200:
                    errors.append(f"GET SSE status {resp.status}")
                    ready.set()
                    return
                ready.set()
                while True:
                    line = resp.readline()
                    if not line:
                        break
                    if line.startswith(b"data:"):
                        data = line[5:].strip()
                        if data:
                            events.put(data.decode("utf-8", "replace"))
            except Exception as exc:  # noqa: BLE001 网络失败统一收口
                errors.append(f"{type(exc).__name__}: {exc}")
            finally:
                ready.set()
                events.put(None)
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:  # noqa: BLE001
                        pass

        reader = threading.Thread(target=_sse_reader, daemon=True)
        reader.start()
        if not ready.wait(timeout=timeout_s):
            raise AgentDispatchFailedError(
                agent_id=request.agent_id, connection_id=connection.id)
        if errors:
            raise AgentDispatchFailedError(
                agent_id=request.agent_id, connection_id=connection.id)

        # ---- 2. POST jsonrpc session/prompt ----
        body_obj = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "session/prompt",
            "params": {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": request.content}],
            },
        }
        try:
            post_conn = _open_http_connection(scheme, host, port, timeout_s)
            try:
                post_conn.request(
                    "POST", path,
                    body=json.dumps(body_obj).encode("utf-8"),
                    headers={
                        **headers,
                        "Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream",
                    })
                post_resp = post_conn.getresponse()
                if post_resp.status not in (200, 202):
                    post_resp.read()
                    raise AgentDispatchFailedError(
                        agent_id=request.agent_id, connection_id=connection.id)
                post_resp.read()
            finally:
                post_conn.close()
        except AgentDispatchFailedError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AgentDispatchFailedError(
                agent_id=request.agent_id, connection_id=connection.id) from exc

        # ---- 3. 收集 SSE 事件直到 final / EOF / 超时 ----
        parts: list[str] = []
        deadline = time.monotonic() + timeout_s
        finished = False
        timed_out = False
        while True:
            if errors:
                raise AgentDispatchFailedError(
                    agent_id=request.agent_id, connection_id=connection.id)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                item = events.get(timeout=remaining)
            except queue.Empty:
                timed_out = True
                break
            if item is None:
                break  # SSE EOF（自然收尾；若 EOF 前已有错误会在下次循环头拦截）
            if self._event_recorder is not None:
                try:
                    self._event_recorder(item)
                except Exception:  # noqa: BLE001 观测回调绝不影响协议路径
                    pass
            final, texts, error = _sse_event_texts_and_final(item)
            if error is not None:
                raise AgentDispatchFailedError(
                    agent_id=request.agent_id, connection_id=connection.id)
            parts.extend(texts)
            if final:
                finished = True
                break

        # 关闭 GET 连接（解阻塞 reader 线程）
        sse_conn = sse_holder.get("conn")
        if sse_conn is not None:
            try:
                sse_conn.close()
            except Exception:  # noqa: BLE001
                pass
        reader.join(timeout=1.0)

        # finished=True 时 SSE 已以 isFinalStop/turn_finished 收尾——主动 close
        # 连接带来的 reader 侧 socket 错误是**我们**造成的，不是 transport 失败。
        if finished:
            return AgentResponse(
                content="".join(parts),
                external_session_id=session_id,
                metadata={"protocol": PROTOCOL_TENCENT_ACP_SSE},
            )

        # reader 侧任何 transport 错误（含 SSE 建连后 socket 超时）→ Fail Closed
        if errors:
            raise AgentDispatchFailedError(
                agent_id=request.agent_id, connection_id=connection.id)

        # Fail Closed：超时未以 final/EOF 收尾 = 未完成，绝不返回半成品；
        # EOF 自然收尾但无任何文本 = 归一 content="" 交由 Dispatch 判无效。
        if timed_out:
            raise AgentDispatchFailedError(
                agent_id=request.agent_id, connection_id=connection.id)

        return AgentResponse(
            content="".join(parts),
            external_session_id=session_id,
            metadata={"protocol": PROTOCOL_TENCENT_ACP_SSE},
        )

    # ------------------------------------------------------------------ Fail Closed 读 Connection

    @staticmethod
    def _endpoint_and_timeout(
        request: AgentRequest, connection: Connection,
    ) -> tuple[str, float]:
        """只从 Connection.config 读 endpoint / timeout_s；缺失或非法 → Fail Closed。"""
        config = connection.config if isinstance(connection.config, dict) else {}
        endpoint = config.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint.strip() \
                or not endpoint.startswith(("http://", "https://")):
            raise AgentDispatchFailedError(
                agent_id=request.agent_id, connection_id=connection.id)
        try:
            timeout_s = float(config.get("timeout_s", DEFAULT_TIMEOUT_S))
        except (TypeError, ValueError):
            raise AgentDispatchFailedError(
                agent_id=request.agent_id, connection_id=connection.id) from None
        if timeout_s <= 0:
            raise AgentDispatchFailedError(
                agent_id=request.agent_id, connection_id=connection.id)
        return endpoint.strip(), timeout_s

    # ------------------------------------------------------------------ ACP serialization（minimal，adapter 内部）

    @staticmethod
    def _compose_acp_request(
        request: AgentRequest, connection: Connection,
    ) -> tuple[dict[str, str], bytes]:
        """AgentRequest → minimal ACP request body/headers（凭据只来自 Connection）。"""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if isinstance(connection.credential_ref, str) and connection.credential_ref:
            headers["X-Acp-Credential-Ref"] = connection.credential_ref
        body = json.dumps({
            "message": request.content,
            "session_id": request.external_session_id,
        }).encode("utf-8")
        return headers, body

    # ------------------------------------------------------------------ HTTP transport（minimal，adapter 内部）

    @staticmethod
    def _http_post(
        request: AgentRequest,
        connection: Connection,
        endpoint: str,
        headers: dict[str, str],
        body: bytes,
        timeout_s: float,
    ) -> bytes:
        """POST 到 minimal ACP Agent；transport/HTTP/protocol 失败 → AGENT_DISPATCH_FAILED。"""
        req = urllib.request.Request(
            endpoint, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                if getattr(resp, "status", 200) != 200:
                    raise AgentDispatchFailedError(
                        agent_id=request.agent_id, connection_id=connection.id)
                return resp.read()
        except AgentDispatchFailedError:
            raise
        except urllib.error.HTTPError as exc:  # 4xx / 5xx（含 401 鉴权失败）
            raise AgentDispatchFailedError(
                agent_id=request.agent_id, connection_id=connection.id) from exc
        except (urllib.error.URLError, OSError) as exc:  # 不可达 / 超时 / 连接拒绝
            raise AgentDispatchFailedError(
                agent_id=request.agent_id, connection_id=connection.id) from exc

    # ------------------------------------------------------------------ ACP response 归一化（minimal，adapter 内部）

    @staticmethod
    def _normalize_acp_response(
        request: AgentRequest, connection: Connection, raw: bytes,
    ) -> AgentResponse:
        """minimal ACP response → AgentResponse；schema 不符 → AGENT_DISPATCH_FAILED。

        content 为空串（Agent 200 但无可用内容）→ 归一化 `content=""`，交给 Dispatch
        契约校验拒绝（AGENT_RESPONSE_INVALID）——不在这里猜一个 content。
        """
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise AgentDispatchFailedError(
                agent_id=request.agent_id, connection_id=connection.id) from None
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            raise AgentDispatchFailedError(
                agent_id=request.agent_id, connection_id=connection.id) from None
        if not isinstance(payload, dict):
            raise AgentDispatchFailedError(
                agent_id=request.agent_id, connection_id=connection.id)
        content = payload.get("content")
        if not isinstance(content, str):
            raise AgentDispatchFailedError(
                agent_id=request.agent_id, connection_id=connection.id)
        session_id = payload.get("session_id")
        if session_id is not None and (not isinstance(session_id, str)
                                       or not session_id):
            raise AgentDispatchFailedError(
                agent_id=request.agent_id, connection_id=connection.id)
        return AgentResponse(content=content, external_session_id=session_id)


def _open_http_connection(
    scheme: str, host: str, port: int, timeout_s: float,
) -> http.client.HTTPConnection:
    """按 scheme 打开 HTTP/HTTPS 连接（http.client 底层，供 SSE 长连与 POST 使用）。"""
    if scheme == "https":
        return http.client.HTTPSConnection(host, port, timeout=timeout_s)
    return http.client.HTTPConnection(host, port, timeout=timeout_s)
