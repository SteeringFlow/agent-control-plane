"""M7-AUI-10.6.2-A+C —— Explicit Flow Selection & Cross-Page Restoration（frontend）。

A：编辑 Agent / 渠道的对象解析 = 显式选择 → 当前 Flow 显示对象；**禁止隐式回退
列表第一项**；解析失败阻断编辑与测试连接（绝不静默转创建）；面板标题显示实际
对象名。
C：sessionStorage（key 固定 "sf.flow.selection"，仅 {ch, ag} 两个 ID）跨页面恢复
选择；恢复前按当前租户对象列表校验；失效 → 清除 + 提示，绝不静默改绑其它对象。

方法：静态源码断言 + Node `vm` 行为级（**双页仿真**：同一 sessionStorage 与假后端
state 跨两次模块加载，复刻「进入其它页面再返回」）。缺 Node 自动 skip。
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

def test_s1_agent_edit_resolution_has_no_first_item_fallback() -> None:
    """A：编辑 Agent 解析 = 显式选择 → Flow 显示对象；无 displayAgent() 隐式回退。"""
    body = _fn(_js(), "syncAgentPanel")
    assert "selectedAgent() || agentById(renderedAgentId)" in body
    assert "displayAgent()" not in body
    assert "编辑 Agent：" in body, "标题显示实际对象名"
    assert "editingAgentMissing" in body


def test_s2_channel_edit_resolution_has_no_first_item_fallback() -> None:
    """A：编辑渠道解析 = 显式选择 → Flow 显示对象；标题显示实际对象名。"""
    body = _fn(_js(), "syncChannelPanel")
    assert "channelById(selectedChannelId) || channelById(renderedChannelId)" in body
    assert "displayChannel()" not in body
    assert "编辑渠道：" in body
    assert "editingChannelMissing" in body


def test_s3_existing_missing_blocks_create() -> None:
    """A6：existing 解析失败 → testAgent / testChannel 阻断；ensureAgent 不静默创建。"""
    code = _js()
    ta = _fn(code, "testAgent")
    assert 'agentPanelMode === "existing" && editingAgentMissing' in ta
    assert ta.index("editingAgentMissing") < ta.index("ensureAgent(provider)")
    tc = _fn(code, "testChannel")
    assert 'channelPanelMode === "existing" && editingChannelMissing' in tc
    assert tc.index("editingChannelMissing") < tc.index("buildChannelPayload()")
    ea = _fn(code, "ensureAgent")
    at = ea.index('agentPanelMode === "existing"')
    assert "要编辑的 Agent 已不存在" in ea[at:at + 200]
    assert ea.index("editingAgentId = null") > at, "existing 分支不得落到创建路径"


def test_s4_selection_storage_is_ids_only() -> None:
    """C：跨页面状态只存 {ch, ag} 两个 ID；不携带凭据 / 配置 / 创建结果。"""
    code = _js()
    assert 'const FLOW_SELECTION_KEY = "sf.flow.selection";' in code
    save = _fn(code, "saveFlowSelection")
    assert "FLOW_SELECTION_KEY" in save
    assert "JSON.stringify({" in save and "ch:" in save and "ag:" in save
    for banned in ("api_key", "provider", "workbuddy_agent_id", "runtime_id",
                   "bearer_token", "endpoint", "config", "credential"):
        assert banned not in save, f"选择状态不得携带 {banned}"
    restore = _fn(code, "restoreFlowSelection")
    assert "FLOW_SELECTION_KEY" in restore
    assert "removeItem" in restore, "失效选择必须清除"
    assert "selectedChannelId = saved.ch" in restore
    assert "selectedAgentId = saved.ag" in restore


# ================================================================== B. Node 行为

_JS_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const src = fs.readFileSync(process.env.PROBE_JS, "utf8").replace(/^import\s[^;]*;/gm, "");

// 共享假后端 state + 共享 sessionStorage（跨「两次页面加载」存活）
const state = { agents: [], channels: [], connections: [],
                nextAgent: 100, nextChannel: 200, nextConn: 300,
                reachable: false };
const calls = [];
const storage = { _m: {},
  getItem(k) { return Object.prototype.hasOwnProperty.call(this._m, k) ? this._m[k] : null; },
  setItem(k, v) { this._m[k] = String(v); },
  removeItem(k) { delete this._m[k]; } };

function makePage() {
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

  function json(body, status) {
    return Promise.resolve({ ok: status < 400, status: status,
      json: function () { return Promise.resolve(body); } });
  }

  const sandbox = {
    console: console,
    __el: el,
    sessionStorage: storage,
    document: { getElementById: el, createElement: function () { return fakeEl(); } },
    window: { location: { replace() {}, origin: "https://acp.local" } },
    fetch: function (url, options) {
      const opt = options || {};
      const method = (opt.method || "GET").toUpperCase();
      const payload = opt.body ? JSON.parse(opt.body) : null;
      calls.push({ method: method, url: url, payload: payload });
      if (url === "/api/auth/me") return json({ id: 1, company_id: 4 }, 200);
      if (url === "/api/agents") {
        if (method === "GET") return json({ items: state.agents.filter(function (a) { return !a.hidden; }) }, 200);
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
    + " testAgent: testAgent, testChannel: testChannel, refresh: refresh,"
    + " openNewAgentPanel: openNewAgentPanel,"
    + " ids: function () { return { editingAgentId: editingAgentId,"
    + "   selectedAgentId: selectedAgentId, editingChannelId: editingChannelId,"
    + "   selectedChannelId: selectedChannelId }; },"
    + " click: function (id) { globalThis.__el(id).__click('click'); },"
    + " pickAgent: function (id) { onAgentPicked(String(id)); },"
    + " pickChannel: function (id) { onChannelPicked(String(id)); },"
    + "};", sandbox);
  return {
    t: sandbox.__t, els: els,
    title: function (id) { return els[id] ? els[id].textContent : ""; },
    error: function (id) {
      const e = els[id];
      return (e && !e.classList.contains("hidden")) ? e.textContent : "";
    },
  };
}

const settle = async function () {
  for (let i = 0; i < 12; i += 1) await new Promise(function (r) { setTimeout(r, 0); });
};
function counts() {
  return { agentsPost: calls.filter(function (c) { return c.method === "POST" && c.url === "/api/agents"; }).length,
           channelsPost: calls.filter(function (c) { return c.method === "POST" && c.url === "/api/channels"; }).length,
           connsPost: calls.filter(function (c) { return c.method === "POST" && c.url === "/api/connections"; }).length };
}

(async function () {
  const scenario = process.env.PROBE_SCENARIO || "edit_agent_explicit";
  const out = { scenario: scenario };

  if (scenario === "edit_agent_explicit") {
    state.agents.push({ id: 1, name: "测试", type: "acp", status: "active", config: null });
    state.agents.push({ id: 12, name: "WorkBuddy 9", type: "acp", status: "active", config: null });
    const p = makePage();
    await settle();
    p.t.pickAgent(12); await settle();
    p.t.click("cta-edit-agent"); await settle();
    out.result = { title: p.title("panel-agent-title"),
                   editingAgentId: p.t.ids().editingAgentId,
                   err: p.error("pa-error") };
  } else if (scenario === "edit_channel_explicit") {
    state.channels.push({ id: 14, name: "企业微信A", type: "wecom_bot", status: "active",
                          config: { bot_id: "b1", bot_secret: "s1" } });
    state.channels.push({ id: 15, name: "企业微信 11", type: "wecom_bot", status: "active",
                          config: { bot_id: "b2", bot_secret: "s2" } });
    const p = makePage();
    await settle();
    p.els["pc-type"].value = "wecom_bot"; // 真实 select 的默认选中项
    p.t.pickChannel(15); await settle();
    p.t.click("cta-edit-channel"); await settle();
    out.result = { title: p.title("panel-channel-title"),
                   editingChannelId: p.t.ids().editingChannelId,
                   err: p.error("pc-error") };
  } else if (scenario === "missing_blocked") {
    state.agents.push({ id: 1, name: "测试", type: "acp", status: "active", config: null });
    state.channels.push({ id: 14, name: "企业微信A", type: "wecom_bot", status: "active",
                          config: { bot_id: "b1", bot_secret: "s1" } });
    const p = makePage();
    await p.t.refresh(); await settle();
    // 对象消失（被移除）→ 重载后 existing 编辑必须阻断
    state.agents.length = 0; state.channels.length = 0;
    await p.t.refresh(); await settle();
    p.t.click("cta-edit-agent"); await settle();
    const agentPart = { title: p.title("panel-agent-title"),
                        err: p.error("pa-error") };
    await p.t.testAgent(); await settle();
    agentPart.agentsPost = counts().agentsPost;
    p.t.click("cta-edit-channel"); await settle();
    const channelPart = { err: p.error("pc-error") };
    await p.t.testChannel(); await settle();
    channelPart.channelsPost = counts().channelsPost;
    out.result = { agent: agentPart, channel: channelPart };
  } else if (scenario === "restore_selection") {
    state.channels.push({ id: 15, name: "企业微信 11", type: "wecom_bot", status: "active",
                          config: { bot_id: "b", bot_secret: "s" } });
    state.agents.push({ id: 12, name: "WorkBuddy 9", type: "acp", status: "active", config: null });
    const p1 = makePage();
    await settle();
    p1.t.pickChannel(15); await settle();
    p1.t.pickAgent(12); await settle();
    out.saved = storage.getItem("sf.flow.selection");
    // 第二次页面加载（从其它页面返回）：同一 storage + 同一后端 state
    const p2 = makePage();
    await settle();
    out.result = { channelValue: p2.els["channel-picker"].value,
                   agentValue: p2.els["agent-picker"].value,
                   editingAgentId: p2.t.ids().selectedAgentId,
                   selectedChannelId: p2.t.ids().selectedChannelId };
  } else if (scenario === "restore_invalid") {
    storage.setItem("sf.flow.selection", JSON.stringify({ ch: 999, ag: 999 }));
    state.channels.push({ id: 15, name: "企业微信 11", type: "wecom_bot", status: "active",
                          config: { bot_id: "b", bot_secret: "s" } });
    state.agents.push({ id: 12, name: "WorkBuddy 9", type: "acp", status: "active", config: null });
    const p = makePage();
    await settle();
    out.result = { channelValue: p.els["channel-picker"].value,
                   agentValue: p.els["agent-picker"].value,
                   detail: p.els["home-detail"].textContent,
                   storedAfter: storage.getItem("sf.flow.selection") };
  } else if (scenario === "explicit_new_still_creates") {
    state.channels.push({ id: 15, name: "企业微信 11", type: "wecom_bot", status: "active",
                          config: { bot_id: "b", bot_secret: "s" } });
    state.agents.push({ id: 12, name: "WorkBuddy 9", type: "acp", status: "active", config: null });
    const p = makePage();
    await settle();
    p.t.openNewAgentPanel(); await settle();
    p.els["pa-provider"].value = "workbuddy";
    p.els["pa-wb-agent-id"].value = "wb-new-1";
    p.els["pa-wb-api-key"].value = "pt-new-1";
    await p.t.testAgent(); await settle();
    out.result = { counts: counts(),
                   agents: state.agents.map(function (a) { return a.name; }) };
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
def test_b1_edit_agent_uses_explicit_selection() -> None:
    """§验收-1：显式选择 WorkBuddy 9 → 编辑标题与实际 Agent ID 一致（不是列表第一项）。"""
    o = _probe("edit_agent_explicit")["result"]
    assert o["title"] == "编辑 Agent：WorkBuddy 9", o
    assert o["editingAgentId"] == 12, o
    assert o["err"] == "", o


@needs_node
def test_b2_edit_channel_uses_explicit_selection() -> None:
    """§验收-2：显式选择企业微信 11 → 编辑标题与实际 Channel ID 一致。"""
    o = _probe("edit_channel_explicit")["result"]
    assert o["title"] == "编辑渠道：企业微信 11", o
    assert o["editingChannelId"] == 15, o
    assert o["err"] == "", o


@needs_node
def test_b3_missing_object_blocks_instead_of_fallback() -> None:
    """§验收-3/4：对象消失 → 不静默编辑其它对象；existing 阻断，零创建请求。"""
    o = _probe("missing_blocked")["result"]
    assert o["agent"]["title"] == "编辑 Agent", o
    assert "没有找到" in o["agent"]["err"], o
    assert o["agent"]["agentsPost"] == 0, o
    assert "没有找到" in o["channel"]["err"], o
    assert o["channel"]["channelsPost"] == 0, o


@needs_node
def test_b4_selection_restored_after_page_return() -> None:
    """§验收-5/6：返回 Home / 重新加载 → 恢复之前的 Channel + Agent。"""
    o = _probe("restore_selection")
    assert json.loads(o["saved"]) == {"ch": 15, "ag": 12}, o["saved"]
    r = o["result"]
    assert r["channelValue"] == "15" and r["agentValue"] == "12", r
    assert r["selectedChannelId"] == 15 and r["editingAgentId"] == 12, r


@needs_node
def test_b5_invalid_selection_cleared_with_notice() -> None:
    """§验收-7：失效选择 → 清除 + 提示，绝不静默改绑其它对象。

    失效 ID 被清除后，render 会以**当前有效**的默认选择重写存储（反映真实
    状态）——断言核心是：失效 ID（999）绝不再被存储。"""
    o = _probe("restore_invalid")["result"]
    stored = json.loads(o["storedAfter"]) if o["storedAfter"] else None
    assert stored is None or (stored.get("ch") != 999 and stored.get("ag") != 999), o
    assert "已不可用" in o["detail"], o


@needs_node
def test_b6_explicit_new_still_creates() -> None:
    """§验收-8 部分：显式「+ 添加 Agent」的创建行为不变（新外部身份）。"""
    o = _probe("explicit_new_still_creates")["result"]
    assert o["counts"]["agentsPost"] == 1, o
    # 默认名称生成：既有 "WorkBuddy 9" → "WorkBuddy" 未被占用
    assert "WorkBuddy" in o["agents"], o
