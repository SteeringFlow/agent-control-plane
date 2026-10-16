"""M7-AUI-9.2 —— First Success 严格按当前渠道 scoped 验收门。

修复 B3（M7-AUI-9.1 审计结论）：First Success 判定原先是 tenant-scoped
（`GET /api/interactions?limit=200` 全量窗口里找第一条
`processing=succeeded × delivery=delivered`），多渠道下会把**别的渠道**
的成功错误归到当前渠道头上。

修复方式（frontend-only，零后端改动）：

    只考虑 `interaction.channel_id === 当前渠道.id` 的记录，
    然后再判断 succeeded + delivered。

铁律：
- 不改 backend / API / DB / migration；不新增 channel_id query parameter；
  继续使用 `GET /api/interactions?limit=200`，前端过滤；
- `channel_id` 缺失（null / undefined）的记录不归属任何渠道，绝不参与匹配；
- 无当前渠道上下文时不得判定成功（宁缺勿错）；
- M7-AUI-5 的 Connected 语义、Connected ≠ First Success、双状态原生字段
  全部不动；home.js 与 connect.js 映射一致。

方法：静态源码断言 + Node `vm` 行为矩阵（直接执行 home.js 的真实
`render` / `checkFirstSuccess` 与 cta-verify 点击链路），缺 Node 自动 skip；
全部只读、不触 DB / 不触网，后端零改动。
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
FRONTEND = REPO_ROOT / "frontend"
HOME_JS = FRONTEND / "home.js"
CONNECT_JS = FRONTEND / "connect.js"

_NODE = shutil.which("node")
needs_node = pytest.mark.skipif(
    _NODE is None, reason="node 不可用（行为级验收需要 Node）")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _js(path: Path) -> str:
    src = re.sub(r"/\*.*?\*/", " ", _read(path), flags=re.S)
    return re.sub(r"//[^\n]*", " ", src)


def _body(code: str, start_marker: str) -> str:
    """取 `start_marker` 开始的完整函数体（到首个顶格 `\\n}`）。"""
    body = code[code.index(start_marker):]
    return body[:body.index("\n}")]


# ================================================================== A. 静态契约

def test_a1_home_first_success_filters_by_current_channel() -> None:
    """home.js：First Success 与失败诊断都只看当前渠道的记录。"""
    code = _js(HOME_JS)
    body = _body(code, "async function checkFirstSuccess")
    # 过滤到当前渠道（含回落默认解析的 currentChannel()）
    assert "currentChannel()" in body
    assert "items.filter((i) => i.channel_id === current.id)" in body
    # 成功判定与失败诊断都用 scoped 列表（绝不用租户全量 items）
    assert "scoped.find(" in body
    assert "diagnoseFailure(scoped[idx])" in body
    assert "for (let idx = scoped.length" in body


def test_a2_connect_first_success_maps_identically() -> None:
    """connect.js：同样的映射——按本次流程的渠道过滤 + 无上下文不判成功。"""
    code = _js(CONNECT_JS)
    body = _body(code, "async function checkFirstSuccess")
    # 先有渠道上下文才检查（chosen 渠道）
    assert "chosen" in body
    assert "Number.isFinite(chosen.channelId)" in body
    assert "items.filter((i) => i.channel_id === chosen.channelId)" in body
    assert "scoped.find(" in body
    assert "diagnoseFailure(scoped[idx])" in body


def test_a3_request_contract_unchanged_no_backend_change() -> None:
    """不改 backend / API：仍是 `?limit=200`，绝不新增 channel_id 查询参数。"""
    for path in (HOME_JS, CONNECT_JS):
        code = _js(path)
        body = _body(code, "async function checkFirstSuccess")
        assert "?limit=200" in body, f"{path.name} 请求面改变"
        assert "channel_id=" not in body, f"{path.name} 新增了查询参数"
        for banned in ("POST", "PATCH", "PUT", "DELETE"):
            assert f'"{banned}", INTERACTIONS_API' not in body


def test_a4_state_semantics_untouched() -> None:
    """Connected ≠ First Success、双状态字段、M7-AUI-5 语义零改动。"""
    code = _js(HOME_JS)
    for marker in ("PROCESSING_SUCCEEDED", "DELIVERY_DELIVERED",
                   "delivery_status", "processing_status"):
        assert marker in code, f"双状态语义被动过：{marker}"
    # 判定仍然同时要求 processing=succeeded × delivery=delivered
    body = _body(code, "async function checkFirstSuccess")
    assert "PROCESSING_SUCCEEDED" in body and "DELIVERY_DELIVERED" in body


# ================================================================== B. Node 行为矩阵

_JS_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const src = fs.readFileSync(process.env.PROBE_JS, "utf8").replace(/^import\s[^;]*;/gm, "");
const scenario = process.env.PROBE_SCENARIO || "fs";

const els = {};
function fakeEl() {
  const classes = new Set();
  return {
    classList: {
      add(c) { classes.add(c); }, remove(c) { classes.delete(c); },
      toggle(c, on) { if (on === undefined) { classes.has(c) ? classes.delete(c) : classes.add(c); }
                      else if (on) { classes.add(c); } else { classes.delete(c); } },
      contains(c) { return classes.has(c); },
    },
    textContent: "", className: "", href: "", value: "", disabled: false,
    handlers: {},
    addEventListener(type, fn) { this.handlers[type] = fn; },
    focus() {}, appendChild() {},
  };
}
function el(id) { if (!els[id]) els[id] = fakeEl(); return els[id]; }
el("pc-type").value = "wecom_bot";
el("pa-provider").value = "workbuddy";

const state = {
  agents: JSON.parse(process.env.PROBE_AGENTS || "[]"),
  channels: JSON.parse(process.env.PROBE_CHANNELS || "[]"),
  connections: JSON.parse(process.env.PROBE_CONNECTIONS || "[]"),
  interactions: JSON.parse(process.env.PROBE_INTERACTIONS || "[]"),
};
const calls = [];
const navigations = [];

const sandbox = {
  console: console,
  document: { getElementById: el, createElement: () => fakeEl() },
  window: { location: { replace(u) { navigations.push(String(u)); },
                        assign(u) { navigations.push(String(u)); },
                        href: "" } },
  fetch: function (url, options) {
    const method = (options && options.method) || "GET";
    const payload = options && options.body ? JSON.parse(options.body) : null;
    const u = String(url);
    calls.push({ method: method, url: u, payload: payload });
    const ok = (status, body) => Promise.resolve({
      ok: status >= 200 && status < 300, status: status,
      json: () => Promise.resolve(body),
    });
    if (u.indexOf("/api/auth/me") >= 0) return ok(200, { user_id: 1, tenant_id: 2 });
    if (u.indexOf("bootstrap-arm") >= 0) {
      return ok(200, { connection_id: 5, reachable: true, checked_at: "now" });
    }
    if (u.indexOf("/verify") >= 0) {
      return ok(200, { connection_id: 5, reachable: true, checked_at: "now" });
    }
    if (u.indexOf("/api/interactions") >= 0) {
      return ok(200, { items: state.interactions, total: state.interactions.length });
    }
    if (u.indexOf("/api/agents") >= 0) {
      return ok(200, { items: state.agents, total: state.agents.length });
    }
    if (u.indexOf("/api/channels") >= 0) {
      return ok(200, { items: state.channels, total: state.channels.length });
    }
    if (u.indexOf("/api/connections") >= 0) {
      return ok(200, { items: state.connections, total: state.connections.length });
    }
    return ok(404, {});
  },
};
vm.createContext(sandbox);
const sharedPath = process.env.PROBE_JS.replace(/[^/\\]+$/, "") + "shared/http.js";
const sharedSrc = fs.readFileSync(sharedPath, "utf8").replace(/^export /gm, "");
vm.runInContext(sharedSrc, sandbox);
vm.runInContext(src + "\n;globalThis.__t = { render: render, "
  + "checkFirstSuccess: checkFirstSuccess };", sandbox);

const flush = () => new Promise((r) => setTimeout(r, 0));
const settle = async () => { for (let i = 0; i < 8; i += 1) await flush(); };
const detail = () => els["home-detail"].textContent;
const detailHas = (needle) => detail().indexOf(needle) >= 0;
const interactionsCalls = () =>
  calls.filter((c) => c.url.indexOf("/api/interactions") >= 0).map((c) => c.url);
const pickChannel = async (id) => {
  els["channel-picker"].value = String(id);
  els["channel-picker"].handlers.change();
  await settle();
};
const runCheck = async () => {
  calls.length = 0;
  els["cta-verify"].handlers.click();
  await settle();
  return { detail: detail(), url: interactionsCalls()[0] || null,
           navigations: navigations.slice() };
};

(async function () {
  await new Promise((r) => setTimeout(r, 400)); // 等 autofill 兜底定时器跑完
  const t = sandbox.__t;
  const out = { scenario: scenario, navigations: navigations };
  const SUCCESS = "已成功送达并收到回复";
  const DIAG = "没有走通";

  await t.render(state.agents, state.channels, state.connections);

  if (scenario === "fs") {
    // 1-3：当前渠道 = A，A 有 succeeded × delivered → First Success = true
    out.onA = Object.assign(await runCheck(), { success: detailHas(SUCCESS) });
    // 4-6：切换到 B（B 只有失败记录）→ 不得把 A / null / 无主的成功算给 B
    await pickChannel(11);
    out.onB = Object.assign(await runCheck(), {
      success: detailHas(SUCCESS),
      diagFromB: detailHas(DIAG),
    });
    // 7-10：切回 A → First Success 恢复
    await pickChannel(10);
    out.backOnA = Object.assign(await runCheck(), { success: detailHas(SUCCESS) });
    out.navigations = navigations.slice();
    process.stdout.write(JSON.stringify(out));
    return;
  }

  if (scenario === "fsnull") {
    // 11：null / missing channel_id 的成功记录不属于渠道 10 → 不判 First Success
    out.withChannel = Object.assign(await runCheck(), { success: detailHas(SUCCESS) });
    // 12：无当前渠道上下文 → 绝不判定成功
    await t.render([], [], []);
    calls.length = 0;
    await t.checkFirstSuccess();
    await settle();
    out.noChannel = { detail: detail(), success: detailHas(SUCCESS),
                      url: interactionsCalls()[0] || null };
    process.stdout.write(JSON.stringify(out));
    return;
  }

  process.stderr.write("unknown scenario: " + scenario);
  process.exit(2);
})().catch(function (e) {
  process.stderr.write(String((e && e.stack) || e));
  process.exit(2);
});
"""

CHANNEL_A = {"id": 10, "name": "企业微信", "type": "wecom_bot", "status": "active"}
CHANNEL_B = {"id": 11, "name": "企业微信 2", "type": "wecom_bot", "status": "active"}
AGENT_WB = {"id": 1, "name": "WorkBuddy", "type": "acp", "status": "active"}
CONN_A_WB = {"id": 50, "channel_id": 10, "agent_id": 1, "status": "active"}
CONN_B_WB = {"id": 51, "channel_id": 11, "agent_id": 1, "status": "active"}

INT_A_OK = {"id": 301, "channel_id": 10,
            "processing_status": "succeeded", "delivery_status": "delivered"}
INT_B_FAIL = {"id": 302, "channel_id": 11, "processing_status": "failed",
              "delivery_status": "failed", "failure_stage": "agent_dispatch",
              "failure_code": "AGENT_DISPATCH_FAILED", "retryable": True}
INT_NULL_OK = {"id": 303, "channel_id": None,
               "processing_status": "succeeded", "delivery_status": "delivered"}
# missing channel_id 字段本身（模拟防御：旧数据 / 缺字段不得误归属）
INT_ORPHAN_OK = {"id": 304,
                 "processing_status": "succeeded", "delivery_status": "delivered"}


def _run(scenario: str, *, channels: list, agents: list,
         connections: list, interactions: list) -> dict:
    env = dict(os.environ)
    env["PROBE_JS"] = str(HOME_JS)
    env["PROBE_SCENARIO"] = scenario
    env["PROBE_CHANNELS"] = json.dumps(channels)
    env["PROBE_AGENTS"] = json.dumps(agents)
    env["PROBE_CONNECTIONS"] = json.dumps(connections)
    env["PROBE_INTERACTIONS"] = json.dumps(interactions)
    proc = subprocess.run(
        [_NODE, "-e", _JS_HARNESS], capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env, check=False)
    assert proc.returncode == 0, f"node 执行失败：{proc.stderr}"
    return json.loads(proc.stdout)


@needs_node
def test_b1_first_success_true_on_current_channel() -> None:
    """1-3：Channel A 有 succeeded × delivered 且为当前渠道 → First Success。"""
    out = _run("fs", channels=[CHANNEL_A, CHANNEL_B], agents=[AGENT_WB],
               connections=[CONN_A_WB, CONN_B_WB],
               interactions=[INT_A_OK, INT_B_FAIL, INT_NULL_OK, INT_ORPHAN_OK])
    assert out["onA"]["success"] is True
    # 请求面不变：仍是 GET /api/interactions?limit=200，无 channel_id 参数
    assert out["onA"]["url"] == "/api/interactions?limit=200"
    assert out["onA"]["navigations"] == []


@needs_node
def test_b2_switch_to_channel_b_no_first_success() -> None:
    """4-6：当前渠道 = B（只有失败记录）→ 不显示 First Success；
    B 自己的失败诊断正常给出（A 的成功 / 无主成功绝不冒名）。"""
    out = _run("fs", channels=[CHANNEL_A, CHANNEL_B], agents=[AGENT_WB],
               connections=[CONN_A_WB, CONN_B_WB],
               interactions=[INT_A_OK, INT_B_FAIL, INT_NULL_OK, INT_ORPHAN_OK])
    on_b = out["onB"]
    assert on_b["success"] is False
    assert on_b["diagFromB"] is True  # B 的失败被诊断出来
    assert on_b["navigations"] == []


@needs_node
def test_b3_switch_back_restores_first_success() -> None:
    """7-10：切回 A → First Success 随当前渠道恢复。"""
    out = _run("fs", channels=[CHANNEL_A, CHANNEL_B], agents=[AGENT_WB],
               connections=[CONN_A_WB, CONN_B_WB],
               interactions=[INT_A_OK, INT_B_FAIL, INT_NULL_OK, INT_ORPHAN_OK])
    assert out["backOnA"]["success"] is True
    assert out["backOnA"]["navigations"] == []


@needs_node
def test_b4_null_or_missing_channel_id_never_counts() -> None:
    """11：channel_id = null / 缺失的成功记录不属于当前渠道 → 不判 First Success。"""
    out = _run("fsnull", channels=[CHANNEL_A], agents=[AGENT_WB],
               connections=[CONN_A_WB],
               interactions=[INT_NULL_OK, INT_ORPHAN_OK])
    assert out["withChannel"]["success"] is False
    assert out["withChannel"]["url"] == "/api/interactions?limit=200"


@needs_node
def test_b5_no_current_channel_never_claims_success() -> None:
    """12：无当前渠道上下文 → 绝不判定 First Success（宁缺勿错）。"""
    out = _run("fsnull", channels=[CHANNEL_A], agents=[AGENT_WB],
               connections=[CONN_A_WB],
               interactions=[INT_NULL_OK, INT_ORPHAN_OK])
    assert out["noChannel"]["success"] is False
