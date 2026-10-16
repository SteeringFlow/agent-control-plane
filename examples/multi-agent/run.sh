#!/usr/bin/env bash
# multi-agent 示例 runner（Linux / macOS，15.3.2）。
# 前置：ACP backend 已启动（cd backend && uvicorn app.main:app）。
# 行为：启动两个 standalone Agent（9101/9102）→ 经 API 建两条完整访问路径 →
# 各发一条可区分消息 → Observation 断言 A≠B 且 Agent 日志零串路 → PASS/FAIL。
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
BASE="${1:-http://127.0.0.1:8000}"
suffix="$(date +%H%M%S)"

api() { # api METHOD PATH [JSON]
  local method="$1" path="$2" body="${3:-}"
  if [ -n "$body" ]; then
    curl -sf -X "$method" "$BASE$path" -H 'Content-Type: application/json' -d "$body"
  else
    curl -sf "$BASE$path"
  fi
}

# 0. backend 必须已启动
curl -sf "$BASE/health" >/dev/null || {
  echo "FAIL: ACP backend not reachable at $BASE — 先启动: cd backend && uvicorn app.main:app"; exit 1; }

# 1. 启动两个 standalone Agent（日志落盘供串路审计）
rm -f "$here/agent-a.log" "$here/agent-b.log"
python3 "$here/agent_server.py" --name AgentA --port 9101 --log "$here/agent-a.log" &
pA=$!
python3 "$here/agent_server.py" --name AgentB --port 9102 --log "$here/agent-b.log" &
pB=$!
trap 'kill $pA $pB 2>/dev/null || true' EXIT

for port in 9101 9102; do
  for i in $(seq 1 20); do
    (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null && { exec 3>&-; break; } || sleep 0.5
  done
done

# 2. 两条完整访问路径（名字带后缀，可重复运行不撞 409）
user_id=$(api POST /api/users "{\"name\":\"multi-agent-$suffix\"}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
agent_a=$(api POST /api/agents "{\"name\":\"custom-agent-a-$suffix\",\"type\":\"acp\"}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
agent_b=$(api POST /api/agents "{\"name\":\"custom-agent-b-$suffix\",\"type\":\"acp\"}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
ch_a=$(api POST /api/channels "{\"name\":\"local-a-$suffix\",\"type\":\"local\"}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
ch_b=$(api POST /api/channels "{\"name\":\"local-b-$suffix\",\"type\":\"local\"}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
api POST /api/identities "{\"user_id\":$user_id,\"channel_id\":$ch_a,\"external_id\":\"local-user-1\"}" >/dev/null
api POST /api/identities "{\"user_id\":$user_id,\"channel_id\":$ch_b,\"external_id\":\"local-user-1\"}" >/dev/null
api POST /api/connections "{\"channel_id\":$ch_a,\"agent_id\":$agent_a,\"name\":\"path-a-$suffix\",\"config\":{\"protocol\":\"minimal\",\"endpoint\":\"http://127.0.0.1:9101\"}}" >/dev/null
api POST /api/connections "{\"channel_id\":$ch_b,\"agent_id\":$agent_b,\"name\":\"path-b-$suffix\",\"config\":{\"protocol\":\"minimal\",\"endpoint\":\"http://127.0.0.1:9102\"}}" >/dev/null

# 3. 各发一条可区分消息
int_a=$(api POST "/api/channels/$ch_a/local/message" '{"external_id":"local-user-1","text":"msg-from-A"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["interaction_id"])')
int_b=$(api POST "/api/channels/$ch_b/local/message" '{"external_id":"local-user-1","text":"msg-from-B"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["interaction_id"])')

# 4. 轮询 Observation 至终态
wait_terminal() {
  for i in $(seq 1 20); do
    st=$(api GET "/api/interactions/$1" | python3 -c 'import sys,json;print(json.load(sys.stdin)["processing_status"])')
    [ "$st" = "succeeded" ] || [ "$st" = "failed" ] && return 0
    sleep 1
  done
  echo "FAIL: interaction $1 did not reach terminal state"; exit 1
}
wait_terminal "$int_a"; wait_terminal "$int_b"
obs_a=$(api GET "/api/interactions/$int_a" | python3 -c 'import sys,json;print(json.load(sys.stdin)["response_payload"]["content"])')
obs_b=$(api GET "/api/interactions/$int_b" | python3 -c 'import sys,json;print(json.load(sys.stdin)["response_payload"]["content"])')

# 5. 断言：A→AgentA、B→AgentB、双方零串路
fail=0
check() { if eval "$2"; then echo "  [PASS] $1"; else echo "  [FAIL] $1"; fail=$((fail+1)); fi; }
check "A: answered by AgentA"        '[[ "$obs_a" == AgentA*msg-from-A* ]]'
check "A: AgentA log got msg-from-A" 'grep -q "msg-from-A" "$here/agent-a.log"'
check "A: AgentA log clean of B"     '! grep -q "msg-from-B" "$here/agent-a.log"'
check "B: answered by AgentB"        '[[ "$obs_b" == AgentB*msg-from-B* ]]'
check "B: AgentB log got msg-from-B" 'grep -q "msg-from-B" "$here/agent-b.log"'
check "B: AgentB log clean of A"     '! grep -q "msg-from-A" "$here/agent-b.log"'
echo "  A response: $obs_a"
echo "  B response: $obs_b"
if [ "$fail" -eq 0 ]; then echo "MULTI-AGENT ISOLATION: PASS"; else echo "MULTI-AGENT ISOLATION: FAIL ($fail)"; exit 1; fi
