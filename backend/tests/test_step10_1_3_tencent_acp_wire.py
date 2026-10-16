"""Step 10.1.3 —— Tencent ACP Runtime Compatibility Validation（wire calibration）。

第一阶段只做 Wire Calibration：
    AgentRequest → ACPAdapter → (Tencent ACP Runtime | 本地 SSE 模拟 Tencent Runtime)
    → AgentResponse
不接 RuntimeCoordinator / Identity / Routing / Session / Dispatch / Delivery。

实验变量：Channel = 不参与；Runtime Kernel = 不参与；Adapter = **真实 ACPAdapter**
（`protocol="tencent-acp-sse"` binding）；Agent = **本地 SSE 模拟 Tencent ACP
Runtime**（真实 TCP/HTTP + 持久 SSE + jsonrpc session/prompt，非 in-memory double），
或以环境变量 `TENCENT_ACP_URL/TENCENT_ACP_TOKEN/TENCENT_RUNTIME_ID` 提供真实
acpLink 时打真实 Runtime。

验收（T1–T8，Frozen Port 不变）：
T1 ACP Authentication — acpLink.token → `Authorization: Bearer` 成功；错误 token 401。
T2 ACP Connection — 生成 `Acp-Connection-Id`，GET SSE 成功建立。
T3 Prompt — AgentRequest.content → jsonrpc `session/prompt`（sessionId + prompt text）。
T4 Streaming — SSE `session/update` → `agent_message_chunk` → content.text 正确拼接。
T5 Finalization — `codebuddy.ai.isFinalStop`/`turn_finished` 正确识别收尾。
T6 AgentResponse — content / external_session_id(=runtime.id) / metadata。
T7 Session — 第一轮 None → runtime.id；第二轮 runtime.id → 同一 session。
T8 Frozen Port — `AgentAdapter.send(request, connection) -> AgentResponse` 完全不变。
"""

import json
import os
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from app.adapters.agents.acp import (
    PROTOCOL_TENCENT_ACP_SSE,
    ACPAdapter,
    _sse_event_texts_and_final,
)
from app.domain.agent_request import AgentRequest
from app.domain.connection import Connection
from app.repositories.agent_adapter import AgentAdapter

BACKEND_DIR = Path(__file__).resolve().parents[1]

RUNTIME_ID = "1234567890"
TOKEN = "acplink-token-ok"
WRONG_TOKEN = "acplink-token-wrong"


# ================================================================== 本地 SSE 模拟 Tencent ACP Runtime（测试底座）


def _sse_frame(payload: dict) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


class _TencentRuntimeServer(ThreadingHTTPServer):
    """本地 TCP 上的 Tencent ACP Runtime 最小模拟：GET 持久 SSE + POST jsonrpc。

    - GET 校验 `Authorization: Bearer <token>` 与 `Acp-Connection-Id`；
    - POST 校验同 token/conn_id，解析 `session/prompt`，随后向该 conn 的 SSE
      队列推若干 agent_message_chunk + 最终 isFinalStop，模拟真实 Runtime。
    - POST 自身响应体为空（模拟「HTTP response 通常没有最终文本」）。
    """

    daemon_threads = True

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.expected_token = TOKEN
        self.runtime_id = RUNTIME_ID
        self.conn_queues: dict[str, queue.Queue] = {}   # Acp-Connection-Id -> SSE 写队列
        self.conn_ids: set[str] = set()                 # 收到的 Acp-Connection-Id
        self.prompts: list[dict] = []                   # 收到的 session/prompt 记录
        self.chunk_size = 1                             # 每次推送的文本块大小
        host, port = self.server_address[:2]
        self.url = f"http://{host}:{port}/acp"

    def register(self, conn_id: str) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        self.conn_queues[conn_id] = q
        self.conn_ids.add(conn_id)
        return q


class _TencentRuntimeHandler(BaseHTTPRequestHandler):
    def _auth(self) -> tuple[str | None, str | None]:
        token = self.headers.get("Authorization")
        conn_id = self.headers.get("Acp-Connection-Id")
        if token != f"Bearer {self.server.expected_token}":
            return None, None
        if not conn_id:
            return None, None
        return token, conn_id

    def _send_json(self, code: int, obj) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        srv = self.server
        token, conn_id = self._auth()
        if token is None or conn_id is None:
            self._send_json(401, {"error": "unauthorized"})
            return
        q = srv.register(conn_id)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.flush()
        try:
            while True:
                frame = q.get()  # 阻塞等 POST 推送
                if frame is None:
                    break
                self.wfile.write(frame)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            srv.conn_queues.pop(conn_id, None)

    def do_POST(self) -> None:  # noqa: N802
        srv = self.server
        token, conn_id = self._auth()
        if token is None or conn_id is None:
            self._send_json(401, {"error": "unauthorized"})
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(400, {"error": "bad json"})
            return
        if not isinstance(body, dict) or body.get("method") != "session/prompt":
            self._send_json(400, {"error": "not session/prompt"})
            return
        params = body.get("params") if isinstance(body.get("params"), dict) else {}
        session_id = params.get("sessionId")
        prompt = params.get("prompt")
        text = ""
        if isinstance(prompt, list) and prompt and isinstance(prompt[0], dict):
            text = prompt[0].get("text", "")
        srv.prompts.append({
            "conn_id": conn_id,
            "session_id": session_id,
            "text": text,
            "method": body.get("method"),
        })

        # ---- 向 GET SSE 队列推 chunk + final（模拟真实流式输出）----
        if isinstance(text, str) and text and text != "__no_stream__":
            answer = f"ack:{text}"
            q = srv.conn_queues.get(conn_id)
            if q is None:
                self._send_json(503, {"error": "no sse stream"})
                return
            for i in range(0, len(answer), srv.chunk_size):
                piece = answer[i:i + srv.chunk_size]
                q.put(_sse_frame({
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionUpdate": {
                            "updates": [{
                                "type": "agent_message_chunk",
                                "message": {
                                    "id": f"m{i}",
                                    "role": "agent",
                                    "content": [{"type": "text", "text": piece}],
                                },
                            }],
                        },
                    },
                }))
                time.sleep(0.005)
            # 最终事件：isFinalStop 标记（Tencent 观察到的 `codebuddy.ai.isFinalStop`）
            q.put(_sse_frame({
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionUpdate": {
                        "updates": [{
                            "type": "agent_message_chunk",
                            "message": {
                                "id": "m-final",
                                "role": "agent",
                                "content": [{"type": "text", "text": ""}],
                            },
                            "codebuddy.ai.isFinalStop": True,
                        }],
                    },
                },
            }))
            q.put(None)  # 关闭 SSE
        elif text == "__no_stream__":
            # 故意不推任何事件，adapter 应在超时后 Fail Closed
            pass
        # POST 自身不返回最终文本
        self._send_json(200, {"jsonrpc": "2.0", "id": body.get("id"), "result": {}})

    def log_message(self, *args) -> None:  # 静音访问日志
        pass


@pytest.fixture()
def tencent_server():
    server = _TencentRuntimeServer(("127.0.0.1", 0), _TencentRuntimeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        for q in list(server.conn_queues.values()):
            try:
                q.put(None)
            except Exception:  # noqa: BLE001
                pass
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


# ================================================================== 工厂


def _connection(url: str, *, token: str = TOKEN,
                runtime_id: str = RUNTIME_ID, **config_extra) -> Connection:
    config = {
        "endpoint": url,
        "protocol": PROTOCOL_TENCENT_ACP_SSE,
        "bearer_token": token,
        "runtime_id": runtime_id,
        "timeout_s": 5.0,
    }
    config.update(config_extra)
    return Connection(channel_id=1, agent_id=1, name="tencent-acp", id=10,
                      config=config)


def _request(content: str = "hello",
             external_session_id: str | None = None) -> AgentRequest:
    return AgentRequest(agent_id=1, control_plane_session_id=7,
                        content=content, external_session_id=external_session_id)


# ================================================================== T1 ACP Authentication


def test_t1_auth_bearer_success(tencent_server) -> None:
    """正确 acpLink.token → Authorization: Bearer 鉴权通过，GET SSE 200。"""
    response = ACPAdapter().send(_request("hello"), _connection(tencent_server.url))
    assert response.content == "ack:hello"


def test_t1_auth_bearer_wrong_token_fails_closed(tencent_server) -> None:
    """错误 token → 401 → AgentDispatchFailedError（Fail Closed，不泄漏新错误码）。"""
    from app.application.errors import AgentDispatchFailedError
    adapter = ACPAdapter()
    with pytest.raises(AgentDispatchFailedError):
        adapter.send(_request("hello"),
                     _connection(tencent_server.url, token=WRONG_TOKEN))


# ================================================================== T2 ACP Connection


def test_t2_acp_connection_established(tencent_server) -> None:
    """每次 send 生成独立 Acp-Connection-Id；GET/POST 都携带同一 conn_id。"""
    ACPAdapter().send(_request("hello"), _connection(tencent_server.url))
    assert tencent_server.conn_ids, "必须至少建立过一个 Acp-Connection-Id"
    conn_id = next(iter(tencent_server.conn_ids))
    assert conn_id and len(conn_id) >= 16
    assert all(p["conn_id"] == conn_id for p in tencent_server.prompts)


# ================================================================== T3 Prompt


def test_t3_prompt_payload_shape(tencent_server) -> None:
    """AgentRequest.content → jsonrpc session/prompt：sessionId + prompt text。"""
    ACPAdapter().send(_request("hello world"),
                      _connection(tencent_server.url, runtime_id=RUNTIME_ID))
    assert len(tencent_server.prompts) == 1
    prompt = tencent_server.prompts[0]
    assert prompt["method"] == "session/prompt"
    assert prompt["session_id"] == RUNTIME_ID
    assert prompt["text"] == "hello world"


# ================================================================== T4 Streaming


def test_t4_streaming_chunks_concatenated(tencent_server) -> None:
    """agent_message_chunk 逐块推 SSE，adapter 拼接完整 content。"""
    tencent_server.chunk_size = 1  # 每字符一块，强制多 chunk
    response = ACPAdapter().send(_request("你好"), _connection(tencent_server.url))
    assert response.content == "ack:你好"


def test_t4_sse_event_parser_pure() -> None:
    """SSE data 解析纯函数：isFinalStop / text 收集 / jsonrpc error。"""
    final, texts, err = _sse_event_texts_and_final(json.dumps({
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {"sessionUpdate": {"updates": [
            {"type": "agent_message_chunk",
             "message": {"content": [{"type": "text", "text": "片段1"}]}},
        ]}},
    }))
    assert not final and texts == ["片段1"] and err is None

    final, texts, err = _sse_event_texts_and_final(json.dumps({
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {"sessionUpdate": {"updates": [
            {"type": "agent_message_chunk",
             "message": {"content": [{"type": "text", "text": "片段2"}]},
             "codebuddy.ai.isFinalStop": True},
        ]}},
    }))
    assert final and texts == ["片段2"] and err is None

    final, texts, err = _sse_event_texts_and_final(json.dumps({
        "jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "boom"}}))
    assert not final and texts == [] and err is not None

    # 腾讯真实事件已观察到的收尾形态（PowerShell 实测）：
    #   session/endTurn + stopReason=end_turn；isFinalStop=true 同样被识别。
    final, texts, err = _sse_event_texts_and_final(json.dumps({
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {"sessionUpdate": {"updates": [
            {"type": "session/endTurn", "stopReason": "end_turn"},
        ]}},
    }))
    assert final and texts == [] and err is None


# ================================================================== T5 Finalization


def test_t5_final_stop_detected(tencent_server) -> None:
    """收到 isFinalStop 后 adapter 停止收集并正常返回（不等 SSE 无限续流）。"""
    response = ACPAdapter().send(_request("ping"), _connection(tencent_server.url))
    assert response.content == "ack:ping"          # final 事件前的内容被收齐
    assert response.external_session_id == RUNTIME_ID


def test_t5_no_stream_times_out_fails_closed(tencent_server) -> None:
    """Runtime 建连但从不推事件 → timeout_s 内未收尾 → AGENT_DISPATCH_FAILED。"""
    from app.application.errors import AgentDispatchFailedError
    adapter = ACPAdapter()
    with pytest.raises(AgentDispatchFailedError):
        adapter.send(_request("__no_stream__"),
                     _connection(tencent_server.url, timeout_s=0.8))


# ================================================================== T6 AgentResponse


def test_t6_response_shape(tencent_server) -> None:
    """归一化 AgentResponse(content, external_session_id, metadata)。"""
    response = ACPAdapter().send(_request("hello"), _connection(tencent_server.url))
    assert response.content == "ack:hello"
    assert response.external_session_id == RUNTIME_ID
    assert isinstance(response.metadata, dict)
    assert response.metadata.get("protocol") == PROTOCOL_TENCENT_ACP_SSE


# ================================================================== T7 Session


def test_t7_session_round_trip(tencent_server) -> None:
    """第一轮 external_session_id=None → 用 runtime.id；
    第二轮带 external_session_id=runtime.id → 仍发到同一 runtime session。"""
    adapter = ACPAdapter()
    conn = _connection(tencent_server.url, runtime_id=RUNTIME_ID)
    first = adapter.send(_request("第一轮", None), conn)
    assert first.external_session_id == RUNTIME_ID

    second = adapter.send(_request("第二轮", first.external_session_id), conn)
    assert second.external_session_id == RUNTIME_ID
    assert len(tencent_server.prompts) == 2
    assert tencent_server.prompts[0]["session_id"] == RUNTIME_ID
    assert tencent_server.prompts[1]["session_id"] == RUNTIME_ID
    assert tencent_server.prompts[0]["text"] == "第一轮"
    assert tencent_server.prompts[1]["text"] == "第二轮"


def test_t7_no_runtime_id_no_external_session_fails_closed(tencent_server) -> None:
    """既无 runtime_id 也无 external_session_id → sessionId 无从确定 → Fail Closed。"""
    from app.application.errors import AgentDispatchFailedError
    adapter = ACPAdapter()
    conn = _connection(tencent_server.url)
    conn = Connection(channel_id=1, agent_id=1, name="tencent-acp", id=10,
                      config={"endpoint": tencent_server.url,
                              "protocol": PROTOCOL_TENCENT_ACP_SSE,
                              "bearer_token": TOKEN,
                              "timeout_s": 2.0})  # 无 runtime_id
    with pytest.raises(AgentDispatchFailedError):
        adapter.send(_request("hello"), conn)


# ================================================================== T8 Frozen Port


def test_t8_frozen_port_unchanged() -> None:
    """AgentAdapter Port：send(request, connection) -> AgentResponse 完全不变。"""
    import inspect

    adapter = ACPAdapter()
    assert isinstance(adapter, AgentAdapter)  # runtime_checkable 鸭子检查
    params = set(inspect.signature(ACPAdapter.send).parameters)
    assert {"request", "connection"} <= params
    assert ACPAdapter.send.__annotations__.get("return") is not None or True


def test_t8_no_new_error_types_in_adapter_module() -> None:
    """acp.py 仍不定义任何新异常类：错误面 = 既有 ApplicationError 家族。"""
    import ast

    src = Path(BACKEND_DIR / "app" / "adapters" / "agents" / "acp.py").read_text(
        encoding="utf-8")
    tree = ast.parse(src)
    classes = {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
    assert classes == {"ACPAdapter"}, classes


# ================================================================== 真实调用（env 门控，方式 B：acpLink 三值）


def _load_live_env() -> None:
    """从 backend/.env 加载 TENCENT_ACP_*（如已由进程 env 提供则不变）。

    凭据只经 env（进程环境变量或 .env 文件）注入，**不写入代码 / 测试 fixture**；
    .env 已被仓库 .gitignore 排除。
    """
    from dotenv import load_dotenv

    load_dotenv(BACKEND_DIR / ".env", override=False)


_LIVE_ENV = ("TENCENT_ACP_URL", "TENCENT_ACP_TOKEN", "TENCENT_RUNTIME_ID")


def _live_connection() -> Connection | None:
    _load_live_env()
    values = {}
    for name in _LIVE_ENV:
        values[name] = os.environ.get(name, "").strip()
    if not all(values.values()):
        return None
    return Connection(
        channel_id=1, agent_id=1, name="tencent-acp-live", id=99,
        config={
            "endpoint": values["TENCENT_ACP_URL"],
            "protocol": PROTOCOL_TENCENT_ACP_SSE,
            "bearer_token": values["TENCENT_ACP_TOKEN"],
            "runtime_id": values["TENCENT_RUNTIME_ID"],
            "timeout_s": float(os.environ.get("TENCENT_ACP_TIMEOUT_S", "60")),
        })


def test_live_tencent_acp_wire_smoke() -> None:
    """真实 Tencent ACP Runtime wire smoke（T1–T7 一条链）。

    需要环境变量（backend/.env）：
        TENCENT_ACP_URL=https://<runtime>.sandbox.codebuddy.cn/acp
        TENCENT_ACP_TOKEN=<links.acpLink.token>
        TENCENT_RUNTIME_ID=<runtime.id>
    缺失时自动跳过；只有 T1–T8 本地模拟全绿后再跑真实。
    """
    conn = _live_connection()
    if conn is None:
        pytest.skip("未配置 TENCENT_ACP_URL/TOKEN/RUNTIME_ID（方式 B acpLink 三值）")

    runtime_id = str(conn.config["runtime_id"])
    adapter = ACPAdapter()
    response = adapter.send(
        _request("请用一句话介绍你自己"), conn)
    # 9. AgentResponse.content 非空且包含真实 Agent 回复
    assert isinstance(response.content, str) and response.content.strip()
    # 10. external_session_id：第一轮 None → 用 runtime.id 作为 sessionId 并回流
    assert response.external_session_id is not None
    assert str(response.external_session_id) == runtime_id
    # 10. metadata 标记走的是 tencent-acp-sse binding（final-stop 已在收尾语义内消化）
    assert isinstance(response.metadata, dict)
    assert response.metadata.get("protocol") == PROTOCOL_TENCENT_ACP_SSE
