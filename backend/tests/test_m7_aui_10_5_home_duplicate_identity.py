"""M7-AUI-10.5 —— Home Flow WorkBuddy 重复身份（Option A）验收（frontend）。

产品合同（Fix 指令 §1/§6）：显式「+ 添加 Agent」= 用户想新建；若外部身份已存在，
解释冲突并让用户**显式选择**「使用已有 Agent / 取绝」；后端 409 是强制边界，
前端预检只是 UX 改进；绝不自动重试创建。

验收面：
- A. 静态断言：预检先于创建、双按钮、多记录不擅选、取消零副作用、
  标记文案与后端 detail 锁定、检测绝不读取 API Key。
- B. Node `vm` 行为级（真跑 testAgent + 冲突交互，假后端按场景统计端点次数）：
  预检命中零 POST / 复用不二次建 Agent / 取消零副作用 / 多记录安全恢复 /
  后端 409 兜底不自动重试 / 泛化 409 不被误报为身份冲突。

缺 Node 自动 skip；只读、不触 DB / 不触网。M7-AUI-10.4 的 retry/reuse 回归
由 test_m7_aui_10_4_failed_verify_reuse.py 继续覆盖（同套 harness）。
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
HOME_HTML = FRONTEND / "home.html"
AGENTS_JS = FRONTEND / "agents.js"

_NODE = shutil.which("node")
needs_node = pytest.mark.skipif(_NODE is None, reason="node 不可用（行为级验收需要 Node）")


def _js() -> str:
    return HOME_JS.read_text(encoding="utf-8")


def _fn(code: str, name: str) -> str:
    start = code.index(f"function {name}")
    brace = code.index("{", start)
    depth = 0
    for i in range(brace, len(code)):
        if code[i] == "{":
            depth += 1
        elif code[i] == "}":
            depth -= 1
            if depth == 0:
                return code[start:i + 1]
    raise AssertionError(f"函数 {name} 未闭合")


# ================================================================== A. 静态断言

def test_a1_conflict_ui_exists() -> None:
    """Option A 的冲突块与双按钮存在（home.html + home.js 引用齐全）。"""
    html = HOME_HTML.read_text(encoding="utf-8")
    for dom_id in ("pa-dup-conflict", "pa-dup-message", "pa-dup-reuse", "pa-dup-cancel"):
        assert f'id="{dom_id}"' in html, dom_id
    assert "使用已有 Agent" in html and "取消" in html
    code = _js()
    for dom_id in ("pa-dup-conflict", "pa-dup-message", "pa-dup-reuse", "pa-dup-cancel"):
        assert f'getElementById("{dom_id}")' in code, dom_id


def test_a2_detection_precedes_creation_and_is_new_mode_only() -> None:
    """预检只作用于显式 new 模式，且必须先于 ensureAgent（创建入口）。"""
    body = _fn(_js(), "testAgent")
    at_detect = body.index('agentPanelMode === "new"')
    at_ensure = body.index("ensureAgent(provider)")
    assert at_detect < at_ensure
    assert "findExistingWorkBuddyAgents(" in body
    assert "showAgentDupConflict(" in body


def test_a3_conflict_copy_and_marker_locked() -> None:
    """用户文案 + 后端 409 判别标记（与 api/management detail 双侧锁定）。"""
    code = _js()
    assert "这个 WorkBuddy Agent 已经添加过了" in code
    assert "WorkBuddy Agent 已存在" in code, "后端 409 detail 标记"
    advanced = AGENTS_JS.read_text(encoding="utf-8")
    assert "WorkBuddy Agent 已存在" in advanced


def test_a4_detection_never_touches_api_key() -> None:
    """API Key 是凭据：身份提取绝不读取/比对/记录它。"""
    for name in ("extractWorkbuddyIdentity", "findExistingWorkBuddyAgents",
                 "isDuplicateExternalAgentError"):
        assert "api_key" not in _fn(_js(), name), name


def test_a5_multi_match_never_picks_arbitrarily() -> None:
    """多条历史记录命中 → 不显示复用按钮，给安全恢复指引（§6 明文要求）。"""
    body = _fn(_js(), "showAgentDupConflict")
    assert "hide(paDupReuse)" in body
    assert "多条已保存记录" in body


def test_a6_reuse_enters_existing_path_without_post() -> None:
    """复用 = 进入 M7-AUI-10.4 既有的 existing/editing 路径，绝不再次 POST。"""
    code = _js()
    at = code.index('paDupReuse.addEventListener')
    handler = code[at:code.index("});", at)]
    assert 'agentPanelMode = "existing"' in handler
    assert "editingAgentId = existing.id" in handler
    assert "testAgent()" in handler
    assert 'request("POST"' not in handler


def test_a7_cancel_is_side_effect_free() -> None:
    """取消 = 零副作用：不建 Agent、不改既有配置、不建 Connection。"""
    code = _js()
    at = code.index("paDupCancel.addEventListener")
    handler = code[at:code.index("});", at)]
    assert "hideAgentDupConflict()" in handler
    assert "没有创建 Agent，也没有修改任何配置" in handler
    assert "request(" not in handler


def test_a8_binding_rethrows_identity_409() -> None:
    """ensureBinding 不吞重复身份 409（吞掉会把冲突伪装成「无法确认通路」）。"""
    body = _fn(_js(), "ensureBinding")
    assert "err.status !== 409 || isDuplicateExternalAgentError(err)" in body


def test_a9_backend_409_branch_never_auto_retries() -> None:
    """catch 分支：身份 409 → 刷新 + 重新定位 + 冲突选择；绝不再次 POST。"""
    code = _js()
    at = code.index("err.status === 409 && built.value")
    branch = code[at:code.index("setPanelError(paError, \"Agent 连接失败", at)]
    assert "refresh()" in branch
    assert "showAgentDupConflict(" in branch
    assert 'request("POST"' not in branch, "绝不自动重试创建"


def test_a10_write_surface_not_expanded() -> None:
    """写面零扩张：home.js 仍只有既有 5 个 API 常量，无新端点/DELETE/PUT。

    M7-AUI-10.6.2-C 合同收窄：sessionStorage 仅允许 Flow 选择（同 10.4 test_a6）。"""
    code = _js()
    assert set(re.findall(r'"/api/[a-z]+"', code)) == {
        '"/api/agents"', '"/api/channels"', '"/api/connections"',
        '"/api/interactions"', '"/api/auth"'}
    assert "localStorage" not in code and "indexedDB" not in code
    ss_calls = re.findall(r"sessionStorage\.(\w+)\(", code)
    assert set(ss_calls) <= {"getItem", "setItem", "removeItem"}, ss_calls
    assert 'const FLOW_SELECTION_KEY = "sf.flow.selection";' in code


# ================================================================== B. Node 行为

_JS_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const src = fs.readFileSync(process.env.PROBE_JS, "utf8").replace(/^import\s[^;]*;/gm, "");
const els = {};
function fakeEl() {
  const classes = new Set();
  const listeners = {};
  return {
    classList: {
      add(c) { classes.add(c); }, remove(c) { classes.delete(c); },
      toggle(c, on) { if (on === undefined) { classes.has(c) ? classes.delete(c) : classes.add(c); }
                      else if (on) { classes.add(c); } else { classes.delete(c); } },
      contains(c) { return classes.has(c); } },
    textContent: "", className: "", href: "", value: "", disabled: false, open: false,
    dataset: {}, style: {}, children: [], firstChild: null,
    addEventListener(type, fn) { listeners[type] = fn; },
    removeEventListener() {}, appendChild() {}, removeChild() {},
    insertBefore() {}, setAttribute() {}, getAttribute() { return null; },
    querySelectorAll() { return []; }, querySelector() { return null; },
    closest() { return null; }, contains() { return false; }, focus() {}, blur() {},
    scrollIntoView() {}, click() {},
    __click(type) { if (listeners[type]) listeners[type](); } };
}
function el(id) { if (!els[id]) els[id] = fakeEl(); return els[id]; }

const state = { agents: [], channels: [], connections: [],
                nextAgent: 100, nextChannel: 200, nextConn: 300,
                reachable: false, dupConn: null };
const calls = [];
function countExact(method, url) {
  return calls.filter(function (c) { return c.method === method && c.url === url; }).length;
}
function json(body, status) {
  return Promise.resolve({ ok: status < 400, status: status,
    json: function () { return Promise.resolve(body); } });
}
function pick(list, id) {
  for (let i = 0; i < list.length; i += 1) { if (list[i].id === id) return list[i]; }
  return null;
}

const sandbox = {
  console: console,
  __el: el,
  document: { getElementById: el, createElement: function () { return fakeEl(); } },
  window: { location: { replace() {}, origin: "https://acp.local" } },
  fetch: function (url, options) {
    const opt = options || {};
    const method = (opt.method || "GET").toUpperCase();
    const payload = opt.body ? JSON.parse(opt.body) : null;
    calls.push({ method: method, url: url, payload: payload });
    if (url === "/api/auth/me") return json({ id: 1, company_id: 1 }, 200);
    if (url === "/api/agents") {
      if (method === "GET") {
        const items = state.agents.filter(function (a) { return !a.hidden; });
        return json({ items: items.slice() }, 200);
      }
      const created = { id: state.nextAgent++, name: payload.name, type: payload.type,
                        status: "active", config: payload.config || {} };
      state.agents.push(created); return json(created, 201);
    }
    if (url === "/api/channels") {
      if (method === "GET") return json({ items: state.channels.slice() }, 200);
      const created = { id: state.nextChannel++, name: payload.name, type: payload.type,
                        status: "active", config: payload.config || {} };
      state.channels.push(created); return json(created, 201);
    }
    if (url === "/api/connections") {
      if (method === "GET") return json({ items: state.connections.slice() }, 200);
      if (state.dupConn) {
        // 模拟「后端已有该外部身份（归属其它 Agent）」→ 409 + detail 标记；
        // 同时让被隐藏的既有 Agent 变为可见（后续 refresh 可重新定位）。
        state.connections.push({ id: state.nextConn++, channel_id: payload.channel_id,
                                 agent_id: state.dupConn.otherAgentId, name: "Old Flow",
                                 status: "active", config: payload.config || {} });
        for (const a of state.agents) { if (a.id === state.dupConn.otherAgentId) a.hidden = false; }
        const detail = state.dupConn.detail; state.dupConn = null;
        return json({ detail: detail }, 409);
      }
      const created = { id: state.nextConn++, channel_id: payload.channel_id,
                        agent_id: payload.agent_id, name: payload.name,
                        status: "active", config: payload.config || {} };
      state.connections.push(created); return json(created, 201);
    }
    if (url.indexOf("/verify") !== -1 && method === "POST") {
      return json({ connection_id: Number(url.split("/")[3]), reachable: state.reachable,
                    checked_at: "2026-01-01T00:00:00Z", detail: "", probe: "workbuddy" }, 200);
    }
    if (url.indexOf("/bootstrap-arm") !== -1 && method === "POST") return json({ armed: true }, 200);
    if (url.indexOf("/api/interactions") === 0) return json({ items: [] }, 200);
    return json({}, 200);
  },
};
vm.createContext(sandbox);
const sharedPath = process.env.PROBE_JS.replace(/[^/\\]+$/, "") + "shared/http.js";
vm.runInContext(fs.readFileSync(sharedPath, "utf8").replace(/^export /gm, ""), sandbox);
vm.runInContext(src + "\n;globalThis.__t = {"
  + " testAgent: testAgent, openNewAgentPanel: openNewAgentPanel, refresh: refresh,"
  + " ids: function () { return { editingAgentId: editingAgentId,"
  + "   selectedAgentId: selectedAgentId }; },"
  + " click: function (id) { globalThis.__el(id).__click('click'); },"
  + "};", sandbox);

const settle = async function () {
  for (let i = 0; i < 12; i += 1) await new Promise(function (r) { setTimeout(r, 0); });
};
function counts() {
  return { agentsPost: countExact("POST", "/api/agents"),
           channelsPost: countExact("POST", "/api/channels"),
           connsPost: countExact("POST", "/api/connections"),
           verifyCount: calls.filter(function (c) {
             return c.method === "POST" && c.url.indexOf("/verify") !== -1; }).length };
}
function conflict() {
  return { visible: !els["pa-dup-conflict"].classList.contains("hidden"),
           message: els["pa-dup-message"].textContent,
           reuseVisible: !els["pa-dup-reuse"].classList.contains("hidden"),
           paError: els["pa-error"].textContent };
}
function fillAgent(agentId, apiKey) {
  els["pa-provider"].value = "workbuddy";
  els["pa-wb-agent-id"].value = agentId;
  els["pa-wb-api-key"].value = apiKey;
}
function seedIdentity(agentId, name, extId, withConn) {
  state.agents.push({ id: agentId, name: name, type: "acp", status: "active",
                      config: { provider: "workbuddy", workbuddy_agent_id: extId } });
  if (withConn) {
    state.connections.push({ id: 1, channel_id: 1, agent_id: agentId, name: "你的 Flow",
                             status: "active",
                             config: { provider: "workbuddy", workbuddy_agent_id: extId } });
  }
}

(async function () {
  await settle();
  const t = sandbox.__t;
  const scenario = process.env.PROBE_SCENARIO || "dup_detected";
  const out = { scenario: scenario };

  if (scenario === "dup_detected") {
    seedIdentity(7, "Old", "agent-x", true);
    await t.refresh(); await settle();
    t.openNewAgentPanel();
    fillAgent("agent-x", "key-x");
    await t.testAgent(); await settle();
    out.result = { counts: counts(), conflict: conflict(), anchor: t.ids().editingAgentId };
  } else if (scenario === "dup_reuse") {
    seedIdentity(7, "Old", "agent-x", true);
    await t.refresh(); await settle();
    t.openNewAgentPanel();
    fillAgent("agent-x", "key-x");
    await t.testAgent(); await settle();
    t.click("pa-dup-reuse"); await settle();
    out.result = { counts: counts(), conflict: conflict(), anchor: t.ids().editingAgentId,
                   agentIds: state.agents.map(function (a) { return a.id; }) };
  } else if (scenario === "dup_cancel") {
    seedIdentity(7, "Old", "agent-x", true);
    await t.refresh(); await settle();
    t.openNewAgentPanel();
    fillAgent("agent-x", "key-x");
    await t.testAgent(); await settle();
    const before = counts();
    t.click("pa-dup-cancel"); await settle();
    out.result = { before: before, after: counts(), conflict: conflict(),
                   agentIds: state.agents.map(function (a) { return a.id; }),
                   connIds: state.connections.map(function (c) { return c.id; }) };
  } else if (scenario === "dup_multi") {
    seedIdentity(7, "Old", "agent-x", true);
    seedIdentity(8, "Old 2", "agent-x", false);
    await t.refresh(); await settle();
    t.openNewAgentPanel();
    fillAgent("agent-x", "key-x");
    await t.testAgent(); await settle();
    out.result = { counts: counts(), conflict: conflict() };
  } else if (scenario === "dup_backend_409") {
    // 前端 ref 陈旧（身份已存在于后端但 ref 不可见）→ 预检放行 →
    // POST /agents 成功 → POST /connections 409（标记）→ 刷新后冲突选择。
    state.agents.push({ id: 7, name: "Old", type: "acp", status: "active", hidden: true,
                        config: { provider: "workbuddy", workbuddy_agent_id: "agent-x" } });
    state.dupConn = { detail: "WorkBuddy Agent 已存在：workbuddy_agent_id=agent-x"
                      + "（已有 Agent 7「Old」）", otherAgentId: 7 };
    await settle();
    t.openNewAgentPanel();
    fillAgent("agent-x", "key-x");
    await t.testAgent(); await settle();
    out.result = { counts: counts(), conflict: conflict(),
                   anchor: t.ids().editingAgentId };
  } else if (scenario === "generic_409") {
    state.dupConn = { detail: "Connection 已存在：channel_id=1, agent_id=2",
                      otherAgentId: 7 };
    await settle();
    t.openNewAgentPanel();
    fillAgent("agent-x", "key-x");
    await t.testAgent(); await settle();
    out.result = { counts: counts(), conflict: conflict() };
  } else {
    throw new Error("未知场景：" + scenario);
  }

  const json = JSON.stringify(out);
  if (process.env.PROBE_OUT) fs.writeFileSync(process.env.PROBE_OUT, json, "utf8");
  process.stdout.write(json);
})().catch(function (e) { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
"""


def _probe(scenario: str) -> dict:
    env = dict(os.environ)
    env["PROBE_JS"] = str(HOME_JS)
    env["PROBE_SCENARIO"] = scenario
    proc = subprocess.run([_NODE, "-e", _JS_HARNESS], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env, check=False)
    assert proc.returncode == 0, f"node 执行失败（{scenario}）：{proc.stderr}"
    return json.loads(proc.stdout)


@needs_node
def test_b1_detection_shows_conflict_without_creating() -> None:
    """§9-15：new 模式输入已存在身份 → 冲突解释 + 双按钮，零创建请求。"""
    o = _probe("dup_detected")["result"]
    assert o["counts"]["agentsPost"] == 0, o
    assert o["counts"]["connsPost"] == 0, o
    assert o["conflict"]["visible"], o
    assert "已经添加过了" in o["conflict"]["message"], o
    assert o["conflict"]["reuseVisible"], o


@needs_node
def test_b2_use_existing_reuses_without_second_agent_post() -> None:
    """§9-16：「使用已有 Agent」→ 不发第二个创建请求，身份锚到既有 Agent。"""
    o = _probe("dup_reuse")["result"]
    assert o["counts"]["agentsPost"] == 0, o
    assert o["anchor"] == 7, o
    assert o["agentIds"] == [7], "绝不创建第二条 Agent 记录"
    assert o["counts"]["verifyCount"] == 1, "进入既有路径后真实重新 Verify"
    assert not o["conflict"]["visible"], o


@needs_node
def test_b3_cancel_creates_nothing_and_changes_nothing() -> None:
    """§9-17：取消 → 零请求、零新记录、凭据不动。"""
    o = _probe("dup_cancel")["result"]
    assert o["after"] == o["before"], o
    assert o["agentIds"] == [7] and o["connIds"] == [1], o
    assert not o["conflict"]["visible"], o
    assert "已取消" in o["conflict"]["paError"], o


@needs_node
def test_b4_multi_match_shows_recovery_not_arbitrary_pick() -> None:
    """§9-20：多条历史记录 → 绝不擅自挑选，只给恢复指引（无复用按钮）。"""
    o = _probe("dup_multi")["result"]
    assert o["counts"]["agentsPost"] == 0, o
    assert o["conflict"]["visible"], o
    assert "多条已保存记录" in o["conflict"]["message"], o
    assert not o["conflict"]["reuseVisible"], o


@needs_node
def test_b5_backend_409_handled_without_auto_retry() -> None:
    """§9-18：后端身份 409 → 刷新 + 冲突选择；绝不自动重试创建。"""
    o = _probe("dup_backend_409")["result"]
    assert o["counts"]["agentsPost"] == 1, f"初始 POST 一次，之后绝不重试：{o}"
    assert o["conflict"]["visible"], o
    assert "已经添加过了" in o["conflict"]["message"], o
    assert o["conflict"]["reuseVisible"], o


@needs_node
def test_b6_generic_409_not_misreported_as_identity_conflict() -> None:
    """§9-19：泛化 409（绑定重复等）不得被当作外部身份冲突呈现。"""
    o = _probe("generic_409")["result"]
    assert not o["conflict"]["visible"], o
    assert "WorkBuddy Agent 已存在" not in o["conflict"]["paError"], o
    assert "Agent 连接失败" in o["conflict"]["paError"], o
