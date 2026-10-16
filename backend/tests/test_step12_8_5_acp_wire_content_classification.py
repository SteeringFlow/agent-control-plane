"""Step 12.8.5 —— ACP Wire Content Classification。

验证 ACPAdapter 在 Tencent ACP wire 上如何识别并分类 `reasoning` 与 `content`：

- Case A — 独立 `type`：reasoning 块被丢弃，text 不进 content；
- Case B — 仍属 text node 但带语义字段（`role`/`reasoning` 标志）：同样丢弃；
- Case C — reasoning 与正文 fused 在单个 `type:text` 块：结构上无法区分，
  适配器**整体作为 CONTENT 透传**，绝不按自然语言模式删除任何文本。

铁律：**只按结构信号分类，绝不按自然语言启发式。** 识别不到信号即视为 Case C。
"""

import json
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.adapters.agents.acp import (
    CONTENT,
    REASONING,
    ACPAdapter,
    classify_content_block,
    extract_all_texts,
    extract_content_texts,
)
from app.domain.agent_request import AgentRequest
from app.domain.connection import Connection

TOKEN = "acp-token-ok"


# ================================================================== 最小 Tencent ACP Runtime（SSE 推 reasoning + content）


def _sse_frame(payload: dict) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


class _RuntimeServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.expected_token = TOKEN
        self.conn_queues: dict[str, "queue.Queue"] = {}
        self.received_prompt_text = None
        host, port = self.server_address[:2]
        self.url = f"http://{host}:{port}/acp"

    def register(self, conn_id: str) -> "queue.Queue":
        q: "queue.Queue" = queue.Queue()
        self.conn_queues[conn_id] = q
        return q


class _RuntimeHandler(BaseHTTPRequestHandler):
    def _auth(self):
        token = self.headers.get("Authorization")
        conn_id = self.headers.get("Acp-Connection-Id")
        if token != f"Bearer {self.server.expected_token}" or not conn_id:
            return None, None
        return token, conn_id

    def _send_json(self, code: int, obj) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
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
                frame = q.get()
                if frame is None:
                    break
                self.wfile.write(frame)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            srv.conn_queues.pop(conn_id, None)

    def do_POST(self):  # noqa: N802
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
        prompt = params.get("prompt")
        if isinstance(prompt, list) and prompt and isinstance(prompt[0], dict):
            srv.received_prompt_text = prompt[0].get("text", "")

        q = srv.conn_queues.get(conn_id)
        if q is None:
            self._send_json(503, {"error": "no sse stream"})
            return

        # 推送测试注入的 content 块（含 reasoning），随后 isFinalStop 收尾
        for payload in list(getattr(srv, "runtime_payloads", [])):
            q.put(_sse_frame({
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {"sessionUpdate": {"updates": [payload]}},
            }))
            time.sleep(0.005)
        q.put(_sse_frame({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionUpdate": {"updates": [
                {"type": "agent_message_chunk",
                 "message": {"id": "m-final", "role": "agent",
                             "content": [{"type": "text", "text": ""}]},
                 "codebuddy.ai.isFinalStop": True},
            ]}},
        }))
        q.put(None)
        self._send_json(200, {"jsonrpc": "2.0", "id": body.get("id"), "result": {}})

    def log_message(self, *args):
        pass


@pytest.fixture()
def runtime_server():
    server = _RuntimeServer(("127.0.0.1", 0), _RuntimeHandler)
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


def _connection(url: str, **config_extra) -> Connection:
    config = {
        "endpoint": url,
        "protocol": "tencent-acp-sse",
        "bearer_token": TOKEN,
        "runtime_id": "runtime-1",
        "timeout_s": 5.0,
    }
    config.update(config_extra)
    return Connection(channel_id=1, agent_id=1, name="acp", id=10, config=config)


def _request(content: str = "hello") -> AgentRequest:
    return AgentRequest(agent_id=1, control_plane_session_id=7,
                        content=content, external_session_id=None)


# ================================================================== 纯函数：结构分类（不依赖自然语言）


def test_classify_case_a_independent_type() -> None:
    """Case A：`type=="reasoning"` → REASONING（丢弃）。"""
    assert classify_content_block({"type": "reasoning", "text": "让我想想"}) == REASONING
    assert classify_content_block({"type": "thinking", "text": "..."}) == REASONING
    assert classify_content_block({"type": "reasoning_content", "text": "..."}) == REASONING


def test_classify_case_b_text_with_semantic_field() -> None:
    """Case B：`type=="text"` 但带语义字段（role/reasoning 标志）→ REASONING。"""
    assert classify_content_block(
        {"type": "text", "role": "reasoning", "text": "..."}) == REASONING
    assert classify_content_block(
        {"type": "text", "reasoning": True, "text": "..."}) == REASONING
    assert classify_content_block(
        {"type": "text", "metadata": {"reasoning": True}, "text": "..."}) == REASONING


def test_classify_case_c_fused_passthrough() -> None:
    """Case C：单个 `type:text` 块、无结构信号 → CONTENT（绝不启发式删除）。"""
    fused = {"type": "text",
             "text": "The user wants... Let me create this... ## 示例"}
    assert classify_content_block(fused) == CONTENT


def test_extract_envelope_signal_thought_chunk_dropped() -> None:
    """12.8.6 真实校准：信号在父级 update.sessionUpdate（agent_thought_chunk）。"""
    node = {
        "method": "session/update",
        "params": {"sessionId": "1", "update": {
            "sessionUpdate": "agent_thought_chunk",
            "content": {"type": "text", "text": "The user asks: ..."},
        }},
    }
    assert extract_content_texts(node) == []
    contents, reasonings = extract_all_texts(node)
    assert contents == [] and reasonings == ["The user asks: ..."]

    message = {
        "method": "session/update",
        "params": {"sessionId": "1", "update": {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "9.8 更大"},
        }},
    }
    assert extract_content_texts(message) == ["9.8 更大"]


def test_extract_drops_reasoning_keeps_content() -> None:
    """extract_content_texts：丢弃 reasoning，收集 content。"""
    node = {
        "params": {"sessionUpdate": {"updates": [{
            "type": "agent_message_chunk",
            "message": {"role": "agent", "content": [
                {"type": "reasoning", "text": "内部思考，必须丢弃"},
                {"type": "text", "text": "正式回复第一段"},
                {"type": "text", "role": "reasoning", "text": "也不能进 content"},
                {"type": "text", "text": "正式回复第二段"},
            ]},
        }]}},
    }
    assert extract_content_texts(node) == ["正式回复第一段", "正式回复第二段"]


def test_extract_all_texts_splits() -> None:
    """extract_all_texts：content / reasoning 两路分离（可观测性）。"""
    node = {"content": [
        {"type": "reasoning", "text": "R1"},
        {"type": "text", "text": "C1"},
    ]}
    contents, reasonings = extract_all_texts(node)
    assert contents == ["C1"]
    assert reasonings == ["R1"]


def test_extract_case_c_fused_not_dropped() -> None:
    """Case C fused 文本不被删除（即使文本里出现 'Let me' 之类字样）。"""
    fused = {"type": "text",
             "text": "Let me think... 结论：今天天气好。"}
    assert extract_content_texts(fused) == [fused["text"]]


# ================================================================== 端到端：适配器真丢弃 reasoning


def test_e2e_adapter_drops_reasoning_case_a(runtime_server) -> None:
    """端到端：wire 推 reasoning + text 混合块，适配器 content 只含正文。"""
    runtime_server.runtime_payloads = [{
        "type": "agent_message_chunk",
        "message": {"id": "m1", "role": "agent", "content": [
            {"type": "reasoning", "text": "（隐藏思考过程，绝不能进 content）"},
            {"type": "text", "text": "可见正文"},
        ]},
    }]
    response = ACPAdapter().send(_request("hi"), _connection(runtime_server.url))
    assert response.content == "可见正文"
    assert "隐藏思考过程" not in response.content


def test_e2e_adapter_drops_reasoning_case_b(runtime_server) -> None:
    """端到端：Case B（text+role=reasoning）同样被丢弃。"""
    runtime_server.runtime_payloads = [{
        "type": "agent_message_chunk",
        "message": {"id": "m1", "role": "agent", "content": [
            {"type": "text", "role": "reasoning", "text": "也不该出现"},
            {"type": "text", "text": "真实答案"},
        ]},
    }]
    response = ACPAdapter().send(_request("hi"), _connection(runtime_server.url))
    assert response.content == "真实答案"
    assert "也不该出现" not in response.content


def test_e2e_case_c_fused_passthrough(runtime_server) -> None:
    """端到端：Case C fused 块整体透传，不被启发式删除。"""
    fused = "The user wants... Let me create this... ## 示例"
    runtime_server.runtime_payloads = [{
        "type": "agent_message_chunk",
        "message": {"id": "m1", "role": "agent", "content": [
            {"type": "text", "text": fused},
        ]},
    }]
    response = ACPAdapter().send(_request("hi"), _connection(runtime_server.url))
    assert response.content == fused
