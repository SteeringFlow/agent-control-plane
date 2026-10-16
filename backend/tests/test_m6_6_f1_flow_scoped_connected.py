"""M6.6-F1 —— Flow-scoped Connected（回归测试）。

目标（Flow-scoped Connected）：
    Connected 不再是「租户内存在任意 active Connection」，而是
    「active Channel + active Connection + active Agent」三者齐备的完整 Flow。

本测试**直接执行** `frontend/home.js` 里真实的 `computeState` 函数（而非静态
字符串扫描）：用 Node `vm` 在桩 DOM 沙箱里加载 home.js 源文件，取出真实的
`computeState`，喂入用例矩阵并断言状态。这样任何对 computeState 语义的改动都会
被真实执行捕获。

守卫范围：
1. computeState 行为矩阵（完整 Flow 才 connected；两端 inactive/缺失 → 非 connected）；
2. 五态模型未被重设计（empty / partial / connected / error 字面仍在）；
3. home.js 不新增 API 端点引用（仍只引用既有 5 个）。

运行：`cd backend && python -m pytest tests/test_m6_6_f1_flow_scoped_connected.py -q`
缺 Node 时自动 skip（不影响既有 pytest 套件）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOME_JS = REPO_ROOT / "frontend" / "home.js"

_NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(_NODE is None, reason="node 不可用（Flow-scoped computeState 需要 Node）")


# ------------------------------------------------------------------ Node 执行器
# 在桩 DOM 沙箱里加载真实 home.js，取出真实 computeState，批量跑用例。
# 约定：home.js 顶层的 DOM 访问 / addEventListener / 末尾 refresh() 全部被桩接管，
# 使脚本可安全加载；computeState 是纯函数，不依赖 DOM。
_JS_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");

const homePath = process.env.F1_HOME_JS;
const src = fs.readFileSync(homePath, "utf8").replace(/^import\s[^;]*;/gm, "");

function fakeEl() {
  return {
    classList: { add() {}, remove() {}, toggle() {} },
    textContent: "",
    href: "",
    addEventListener() {},
  };
}

const sandbox = {
  console: console,
  document: { getElementById: function () { return fakeEl(); } },
  window: { location: { replace: function () {} } },
  // 桩网络：refresh() 的鉴权请求立即失败 → 走 window.location.replace 短路返回，
  // 不进入 render，避免无关 DOM 路径影响本测试。
  fetch: function () { return Promise.reject(new Error("no network in F1 test")); },
};

vm.createContext(sandbox);
const sharedPath = process.env.F1_HOME_JS.replace(/[^/\\]+$/, "") + "shared/http.js";
const sharedSrc = fs.readFileSync(sharedPath, "utf8").replace(/^export /gm, "");
vm.runInContext(sharedSrc, sandbox);
// 显式捕获真实 computeState（不依赖 strict-mode 下的全局函数属性行为）。
vm.runInContext(src + "\n;globalThis.__computeState = computeState;", sandbox);

const computeState = sandbox.__computeState;
if (typeof computeState !== "function") {
  process.stderr.write("computeState 不可用");
  process.exit(2);
}

const cases = JSON.parse(process.env.F1_CASES);
const results = cases.map(function (c) {
  return computeState(c.agents, c.channels, c.connections);
});
process.stdout.write(JSON.stringify(results));
"""


def _compute_states(cases: list[dict]) -> list[str]:
    env = dict(os.environ)
    env["F1_HOME_JS"] = str(HOME_JS)
    env["F1_CASES"] = json.dumps(cases)
    proc = subprocess.run(
        [_NODE, "-e", _JS_HARNESS],
        capture_output=True, text=True, env=env, check=False,
    )
    assert proc.returncode == 0, f"node 执行失败：{proc.stderr}"
    return json.loads(proc.stdout)


def _case(agents, channels, connections):
    return {"agents": agents, "channels": channels, "connections": connections}


def _channel(cid, status="active"):
    return {"id": cid, "name": f"ch{cid}", "type": "wecom", "status": status}


def _agent(aid, status="active"):
    return {"id": aid, "name": f"agent{aid}", "type": "acp", "status": status}


def _conn(cid, channel_id, agent_id, status="active"):
    return {"id": cid, "channel_id": channel_id, "agent_id": agent_id, "status": status}


# ------------------------------------------------------------------ 行为矩阵

# (label, case, expected_state)
_MATRIX = [
    (
        "完整 Flow（active Channel + active Connection + active Agent）→ connected",
        _case([_agent(2)], [_channel(1)], [_conn(9, 1, 2)]),
        "connected",
    ),
    (
        "active Connection 但 Agent inactive → 非 connected（partial）",
        _case([_agent(2, "inactive")], [_channel(1)], [_conn(9, 1, 2)]),
        "partial",
    ),
    (
        "active Connection 但 Channel inactive → 非 connected（partial）",
        _case([_agent(2)], [_channel(1, "inactive")], [_conn(9, 1, 2)]),
        "partial",
    ),
    (
        "active Connection 但引用 Channel 缺失 → 非 connected（partial）",
        _case([_agent(2)], [], [_conn(9, 1, 2)]),
        "partial",
    ),
    (
        "active Connection 但引用 Agent 缺失 → 非 connected（partial）",
        _case([], [_channel(1)], [_conn(9, 1, 2)]),
        "partial",
    ),
    (
        "首条 active Connection 不完整、但第二条完整 → connected（顺序扫描）",
        _case(
            [_agent(2, "inactive"), _agent(4)],
            [_channel(1), _channel(3)],
            [_conn(1, 1, 2), _conn(2, 3, 4)],
        ),
        "connected",
    ),
    (
        "存在完整 Flow 的同时也有 inactive Connection → connected（优先级不回归）",
        _case(
            [_agent(2), _agent(5)],
            [_channel(1)],
            [_conn(1, 1, 2), _conn(2, 1, 5, "inactive")],
        ),
        "connected",
    ),
    (
        "只有 inactive Connection → error（既有语义保持）",
        _case([_agent(2)], [_channel(1)], [_conn(9, 1, 2, "inactive")]),
        "error",
    ),
    (
        "无 Channel 且无 Agent → empty（既有语义保持）",
        _case([], [], []),
        "empty",
    ),
    (
        "有 Channel/Agent 但无 Connection → partial（既有语义保持）",
        _case([_agent(2)], [_channel(1)], []),
        "partial",
    ),
]


@pytest.mark.parametrize("label,case,expected", _MATRIX, ids=[m[0] for m in _MATRIX])
def test_compute_state_flow_scoped(label: str, case: dict, expected: str) -> None:
    """直接执行真实 computeState：只有完整 Flow 才是 connected。"""
    (state,) = _compute_states([case])
    assert state == expected, f"{label}：期望 {expected}，实得 {state}"


def test_compute_state_treats_tenant_global_active_connection_as_not_connected() -> None:
    """回归锚点：F1 之前「租户内任意 active Connection」= connected 的行为必须消失。

    单条 active Connection，其 Channel / Agent 均 inactive（或缺失）时，
    绝不能返回 connected。
    """
    not_connected = [
        _case([_agent(2, "inactive")], [_channel(1)], [_conn(9, 1, 2)]),
        _case([_agent(2)], [_channel(1, "inactive")], [_conn(9, 1, 2)]),
        _case([_agent(2, "inactive")], [_channel(1, "inactive")], [_conn(9, 1, 2)]),
    ]
    states = _compute_states(not_connected)
    assert "connected" not in states, f"tenant-global active Connection 仍被判为 connected：{states}"


# ------------------------------------------------------------------ 模型 / 边界守卫


def _strip_js_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    src = re.sub(r"//[^\n]*", " ", src)
    return src


def test_five_state_model_is_not_redesigned() -> None:
    """不重设计五态模型：四个终态字面 + connecting 渲染均仍在。"""
    code = _strip_js_comments(HOME_JS.read_text(encoding="utf-8"))
    for state in ('"empty"', '"partial"', '"connected"', '"error"'):
        assert state in code, f"五态模型字面缺失：{state}"
    assert "renderConnecting" in code


def test_home_js_references_no_new_endpoints() -> None:
    """只改 home.js 且不新增 API 引用（仍为既有 5 个）。"""
    code = _strip_js_comments(HOME_JS.read_text(encoding="utf-8"))
    referenced = set(re.findall(r'"/api/[a-z]+"', code))
    assert referenced == {
        '"/api/agents"', '"/api/channels"', '"/api/connections"',
        '"/api/interactions"', '"/api/auth"',
    }, f"home.js 引用面漂移：{referenced}"
