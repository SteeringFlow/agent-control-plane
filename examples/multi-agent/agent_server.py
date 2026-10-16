#!/usr/bin/env python3
"""multi-agent 示例的 standalone Agent 服务（一个脚本跑两个实例，15.3.2）。

与 custom-agent/agent.py 同一 minimal ACP wire：

    POST  body {"message": str, "session_id": str|null}
    200   {"content": str(非空), "session_id": str|null}

差异：响应内容携带实例名（--name），并把收到的每条消息追加写入 --log 文件，
供 runner / 人工验证「A、B 互不串路」。

运行（由 run.ps1 / run.sh 自动执行，也可手动）：
    python agent_server.py --name AgentA --port 9101 --log agent-a.log
    python agent_server.py --name AgentB --port 9102 --log agent-b.log
"""

import argparse
import json
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class EchoAgentServer(BaseHTTPRequestHandler):
    name: str = "Agent"
    log_path: str | None = None
    turns: dict = {}

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

        if self.log_path:
            with open(self.log_path, "a", encoding="utf-8") as handle:
                handle.write(f"received: {message}\n")
        content = f"{self.name} echo: {message} (turn {turn})"
        print(f"[{self.name}] <<< {message!r} session={sid[:8]}... turn={turn}",
              flush=True)
        self._reply(200, {"content": content, "session_id": sid})

    def _reply(self, status: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # 静默默认访问日志
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="multi-agent example agent")
    parser.add_argument("--name", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--log", default=None)
    args = parser.parse_args()

    EchoAgentServer.name = args.name
    EchoAgentServer.log_path = args.log
    EchoAgentServer.turns = {}
    server = ThreadingHTTPServer(("127.0.0.1", args.port), EchoAgentServer)
    print(f"{args.name} listening on http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
