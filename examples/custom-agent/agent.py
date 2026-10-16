#!/usr/bin/env python3
"""custom-agent —— standalone 的 minimal ACP HTTP Agent 示例（15.3.2）。

这是一个**独立进程**：不 import Agent Control Plane 的任何代码（无 SDK），
只用 Python 标准库。它实现 `ACPAdapter` 的 **minimal binding**（wire 冻结在
`backend/app/adapters/agents/acp.py` 模块 docstring §1）：

    请求  POST {Connection.config["endpoint"]}
          headers  Content-Type: application/json, Accept: application/json
                   （若 Connection 配置了 credential_ref，另含 X-Acp-Credential-Ref）
          body     {"message": str, "session_id": str|null}
    响应  200      {"content": str(非空), "session_id": str|null}

会话语义（对照 10.8.4 bind_external_session 与 10.3.1 已知缺口）：
    - 本 Agent 无条件在响应中返回 session_id；Control Plane 会把它**记录**到
      CP Session（sessions.external_session_id）；
    - 但当前生产 dispatch **不回传**该引用（10.3.1 冻结记录的缺口：
      external_session_id 不在 Runtime 主链路回流），因此经 ACP 调用时
      每轮 session_id=null → turn 计数恒为 1；
    - turn 计数保留：直连本 Agent（带 session_id 调用）即可看到递增，
      它同时演示了 minimal wire 的完整会话形状。

运行：python agent.py [--host 127.0.0.1] [--port 9101]
"""

import argparse
import json
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class MinimalAcpAgent(BaseHTTPRequestHandler):
    """一次 POST = 一次 AgentRequest；示例会话状态仅存内存（进程重启即失）。"""

    turns: dict = {}  # session_id -> 已对话轮数

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            self._reply(400, {"error": "body 必须是合法 JSON"})
            return
        message = payload.get("message") if isinstance(payload, dict) else None
        session_id = payload.get("session_id") if isinstance(payload, dict) else None
        if not isinstance(message, str) or not message.strip():
            self._reply(
                400,
                {"error": "body 必须是 {\"message\": str(非空), \"session_id\": str|null}"},
            )
            return

        if isinstance(session_id, str) and session_id.strip():
            sid = session_id.strip()
            self.turns[sid] = self.turns.get(sid, 0) + 1
        else:
            sid = uuid.uuid4().hex
            self.turns[sid] = 1
        turn = self.turns[sid]

        content = f"[custom-agent] turn {turn}, you said: {message}"
        print(f"<<< message={message!r} session={sid[:8]}... turn={turn}", flush=True)
        self._reply(200, {"content": content, "session_id": sid})

    def _reply(self, status: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # 静默 BaseHTTPRequestHandler 默认访问日志
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="minimal ACP example agent")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9101)
    args = parser.parse_args()

    MinimalAcpAgent.turns = {}
    server = ThreadingHTTPServer((args.host, args.port), MinimalAcpAgent)
    print(f"custom-agent listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
