"""M7-AUI-10.6.2-B —— First Success Recovery UX（frontend）。

验收：当前 Flow 已接通、但首条消息在 identity_resolution / authorization 失败时，
「检查首次成功」给出可行动说明 + 「重新验证并引导」入口；点击后只复用当前
Channel + Agent 的**既有** Connection，按既有顺序铁律执行 verify → bootstrap-arm；
成功提示只在后端 201 确认后出现（含 expires_at）；失败显示真实结果；绝不新建
Agent / Channel / Connection，绝不重放旧 Interaction。

方法：Node `vm` 行为级（沿用 10.6.2 harness；假后端支持 verify/arm 的可控失败）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOME_JS = REPO_ROOT / "frontend" / "home.js"

_NODE = shutil.which("node")
needs_node = pytest.mark.skipif(_NODE is None, reason="node 不可用（行为级验收需要 Node）")


def _js() -> str:
    return HOME_JS.read_text(encoding="utf-8")


def _fn(code: str, name: str) -> str:
    needle = name if name.startswith("function ") else f"function {name}"
    start = code.index(needle)
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

def test_s1_recovery_action_reuses_existing_connection_only() -> None:
    """恢复动作只查找既有连接；绝不新建 Agent / Channel / Connection。"""
    body = _fn(_js(), "recoverBootstrapArm")
    assert "ref.connections.find(" in body
    assert "ensureBinding" not in body
    for banned in ('request("POST", AGENTS_API', 'request("POST", CHANNELS_API',
                   'request("POST", CONNECTIONS_API'):
        assert banned not in body


def test_s2_arm_after_verify_with_backend_confirmation() -> None:
    """顺序铁律：verify 通过才 bootstrap-arm；成功提示携带后端返回的 expires_at。"""
    body = _fn(_js(), "recoverBootstrapArm")
    assert body.index("/verify") < body.index("bootstrap-arm")
    assert "expires_at" in body
    assert "引导窗口已开启" in body


def test_s3_entry_only_for_identity_authorization() -> None:
    """入口只在身份/授权失败给出；渲染复位恢复模式；主按钮按模式分派。"""
    code = _js()
    cfs = _fn(code, "checkFirstSuccess")
    assert 'diag.stage === "identity_resolution"' in cfs
    assert 'diag.stage === "authorization"' in cfs
    assert "recoveryMode = true" in cfs
    render_body = _fn(code, "function render(")
    assert "recoveryMode = false;" in render_body, "渲染时复位恢复模式"
    handler = code[code.index("ctaPrimary.addEventListener"):]
    assert "recoverBootstrapArm()" in handler and "connectCurrentFlow()" in handler


# ================================================================== B. Node 行为

_JS_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const src = fs.readFileSync(process.env.PROBE_JS, "utf8").replace(/^import\s[^;]*;/gm, "");

const state = { agents: [], channels: [], connections: [], interactions: [],
                reachable: true, failArm: false };
const calls = [];

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
    sessionStorage: { _m: {},
      getItem(k) { return this._m[k] || null; },
      setItem(k, v) { this._m[k] = String(v); },
      removeItem(k) { delete this._m[k]; } },
    document: { getElementById: el, createElement: function () { return fakeEl(); } },
    window: { location: { replace() {}, origin: "https://acp.local" } },
    fetch: function (url, options) {
      const opt = options || {};
      const method = (opt.method || "GET").toUpperCase();
      const payload = opt.body ? JSON.parse(opt.body) : null;
      calls.push({ method: method, url: url, payload: payload });
      if (url === "/api/auth/me") return json({ id: 1, company_id: 4 }, 200);
      if (url === "/api/agents") {
        if (method === "GET") return json({ items: state.agents.slice() }, 200);
        return json({ detail: "blocked by test" }, 409);
      }
      if (url === "/api/channels") {
        if (method === "GET") return json({ items: state.channels.slice() }, 200);
        return json({ detail: "blocked by test" }, 409);
      }
      if (url === "/api/connections") {
        if (method === "GET") return json({ items: state.connections.slice() }, 200);
        return json({ detail: "blocked by test" }, 409);
      }
      if (url.indexOf("/bootstrap-arm") !== -1 && method === "POST") {
        if (state.failArm) return json({ detail: "arm exploded" }, 500);
        const cid = Number(url.split("/")[3]);
        return json({ arm_id: 90 + cid, connection_id: cid, status: "armed",
                      expires_at: "2026-10-09T08:30:00Z" }, 201);
      }
      if (url.indexOf("/verify") !== -1 && method === "POST") {
        return json({ connection_id: Number(url.split("/")[3]),
                      reachable: state.reachable, checked_at: "2026-01-01T00:00:00Z",
                      detail: state.reachable ? "" : "workbuddy connection could not be verified",
                      probe: "workbuddy" }, 200);
      }
      if (url.indexOf("/api/interactions") === 0) {
        return json({ items: state.interactions.slice() }, 200);
      }
      return json({}, 200);
    },
  };
  vm.createContext(sandbox);
  const sharedPath = process.env.PROBE_JS.replace(/[^/\\]+$/, "") + "shared/http.js";
  vm.runInContext(fs.readFileSync(sharedPath, "utf8").replace(/^export /gm, ""), sandbox);
  vm.runInContext(src + "\n;globalThis.__t = {"
    + " checkFirstSuccess: checkFirstSuccess,"
    + " click: function (id) { globalThis.__el(id).__click('click'); },"
    + " pickAgent: function (id) { onAgentPicked(String(id)); },"
    + " pickChannel: function (id) { onChannelPicked(String(id)); },"
    + "};", sandbox);
  return {
    t: sandbox.__t, els: els,
    visible: function (id) {
      const e = els[id];
      return e ? !e.classList.contains("hidden") : false;
    },
    text: function (id) { return els[id] ? els[id].textContent : ""; },
  };
}

const settle = async function () {
  for (let i = 0; i < 12; i += 1) await new Promise(function (r) { setTimeout(r, 0); });
};
function countExact(method, url) {
  return calls.filter(function (c) { return c.method === method && c.url === url; }).length;
}
function counts() {
  return { agentsPost: countExact("POST", "/api/agents"),
           channelsPost: countExact("POST", "/api/channels"),
           connsPost: countExact("POST", "/api/connections") };
}
function seedConnectedFlow() {
  state.channels.push({ id: 15, name: "企业微信 11", type: "wecom_bot", status: "active",
                        config: { bot_id: "b", bot_secret: "s" } });
  state.agents.push({ id: 12, name: "WorkBuddy 9", type: "acp", status: "active", config: null });
  state.connections.push({ id: 16, channel_id: 15, agent_id: 12, name: "你的 Flow",
                           status: "active", config: {} });
  state.interactions.push({ id: 2, channel_id: 15, processing_status: "failed",
                            delivery_status: "pending",
                            failure_stage: "identity_resolution",
                            failure_code: "IDENTITY_RESOLUTION_FAILED" });
}

(async function () {
  const scenario = process.env.PROBE_SCENARIO || "recovery_entry";
  const out = { scenario: scenario };

  if (scenario === "recovery_entry") {
    seedConnectedFlow();
    const p = makePage();
    await settle();
    p.t.pickChannel(15); await settle();
    p.t.pickAgent(12); await settle();
    p.t.click("cta-verify"); await settle();
    out.result = { entryVisible: p.visible("cta-primary"),
                   entryText: p.text("cta-primary"),
                   detail: p.text("home-detail") };
  } else if (scenario === "recover_success") {
    seedConnectedFlow();
    const p = makePage();
    await settle();
    p.t.pickChannel(15); await settle();
    p.t.pickAgent(12); await settle();
    p.t.click("cta-verify"); await settle();
    p.t.click("cta-primary"); await settle();
    out.result = { counts: counts(),
                   verify: countExact("POST", "/api/connections/16/verify"),
                   arm: countExact("POST", "/api/connections/16/bootstrap-arm"),
                   detail: p.text("home-detail"),
                   err: p.visible("home-error") ? p.text("home-error") : "" };
  } else if (scenario === "recover_verify_fail") {
    seedConnectedFlow();
    state.reachable = false;
    const p = makePage();
    await settle();
    p.t.pickChannel(15); await settle();
    p.t.pickAgent(12); await settle();
    p.t.click("cta-verify"); await settle();
    p.t.click("cta-primary"); await settle();
    out.result = { arm: countExact("POST", "/api/connections/16/bootstrap-arm"),
                   err: p.visible("home-error") ? p.text("home-error") : "",
                   detail: p.text("home-detail") };
  } else if (scenario === "recover_arm_fail") {
    seedConnectedFlow();
    state.failArm = true;
    const p = makePage();
    await settle();
    p.t.pickChannel(15); await settle();
    p.t.pickAgent(12); await settle();
    p.t.click("cta-verify"); await settle();
    p.t.click("cta-primary"); await settle();
    out.result = { verify: countExact("POST", "/api/connections/16/verify"),
                   err: p.visible("home-error") ? p.text("home-error") : "",
                   detail: p.text("home-detail") };
  } else if (scenario === "recover_no_connection") {
    seedConnectedFlow();
    state.connections.length = 0; // 当前 Channel + Agent 没有任何连接
    const p = makePage();
    await settle();
    p.t.pickChannel(15); await settle();
    p.t.pickAgent(12); await settle();
    p.t.click("cta-verify"); await settle();
    p.t.click("cta-primary"); await settle();
    out.result = { counts: counts(),
                   err: p.visible("home-error") ? p.text("home-error") : "",
                   detail: p.text("home-detail") };
  } else if (scenario === "routing_stage_no_entry") {
    seedConnectedFlow();
    state.interactions = [{ id: 2, channel_id: 15, processing_status: "failed",
                            delivery_status: "pending", failure_stage: "routing",
                            failure_code: "ROUTING_FAILED" }];
    const p = makePage();
    await settle();
    p.t.pickChannel(15); await settle();
    p.t.pickAgent(12); await settle();
    p.t.click("cta-verify"); await settle();
    out.result = { entryVisible: p.visible("cta-primary"),
                   detail: p.text("home-detail") };
  } else if (scenario === "first_success_no_entry") {
    seedConnectedFlow();
    state.interactions = [{ id: 2, channel_id: 15, processing_status: "succeeded",
                            delivery_status: "delivered", failure_stage: null,
                            failure_code: null }];
    const p = makePage();
    await settle();
    p.t.pickChannel(15); await settle();
    p.t.pickAgent(12); await settle();
    p.t.click("cta-verify"); await settle();
    out.result = { entryVisible: p.visible("cta-primary"),
                   detail: p.text("home-detail") };
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
    proc = subprocess.run(["node", "-e", _JS_HARNESS], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env, check=False)
    assert proc.returncode == 0, f"node 执行失败（{scenario}）：{proc.stderr}"
    return json.loads(proc.stdout)


@needs_node
def test_b1_recovery_entry_shown_for_identity_failure() -> None:
    """§D-1：已接通 + identity_resolution 失败 → 显示「重新验证并引导」入口。"""
    o = _probe("recovery_entry")["result"]
    assert o["entryVisible"] is True, o
    assert o["entryText"] == "重新验证并引导", o
    assert "引导" in o["detail"], o


@needs_node
def test_b2_recovery_uses_existing_connection_and_opens_window() -> None:
    """§D-2/3：点击恢复 → 复用既有连接 16（verify→arm），零创建请求；提示含限时。"""
    o = _probe("recover_success")["result"]
    assert o["verify"] == 1 and o["arm"] == 1, o
    assert o["counts"]["agentsPost"] == 0, o
    assert o["counts"]["channelsPost"] == 0, o
    assert o["counts"]["connsPost"] == 0, o
    assert "引导窗口已开启" in o["detail"], o
    assert "2026-10-09T08:30:00Z" in o["detail"], "成功提示携带后端确认的 expires_at"
    assert o["err"] == "", o


@needs_node
def test_b3_verify_failure_shown_no_arm() -> None:
    """§D-4：verify 未通过 → 真实失败结果 + 不开启窗口 + 不误报成功。"""
    o = _probe("recover_verify_fail")["result"]
    assert o["arm"] == 0, o
    assert "重新验证未通过" in o["err"], o
    assert "引导窗口未开启" in o["err"], o
    assert "引导窗口已开启" not in o["detail"], o


@needs_node
def test_b4_arm_failure_shown() -> None:
    """§D-4：arm 请求失败 → 真实错误上屏，不误报成功。"""
    o = _probe("recover_arm_fail")["result"]
    assert o["verify"] == 1, o
    assert "开启引导窗口失败" in o["err"], o
    assert "引导窗口已开启" not in o["detail"], o


@needs_node
def test_b5_no_connection_blocks_without_create() -> None:
    """§D-5：当前 Channel + Agent 无既有连接 → 明确报错，零创建请求（假后端对
    创建请求一律 409，任何静默创建都会被断言捕获）。"""
    o = _probe("recover_no_connection")["result"]
    assert o["counts"] == {"agentsPost": 0, "channelsPost": 0, "connsPost": 0}, o
    assert "还没有连接" in o["err"], o
    assert "引导窗口已开启" not in o["detail"], o


@needs_node
def test_b6_routing_stage_keeps_own_message_no_entry() -> None:
    """§C：其它失败阶段（routing）沿用既有提示，不出现恢复入口。"""
    o = _probe("routing_stage_no_entry")["result"]
    assert o["entryVisible"] is False, o
    assert "重新验证并引导" not in o["detail"], o
    assert "匹配到你的 Agent" in o["detail"], o


@needs_node
def test_b7_first_success_no_entry() -> None:
    """成功判定不受影响：真实成功 → 无恢复入口。"""
    o = _probe("first_success_no_entry")["result"]
    assert o["entryVisible"] is False, o
    assert "已成功接到你的 Agent" in o["detail"], o
