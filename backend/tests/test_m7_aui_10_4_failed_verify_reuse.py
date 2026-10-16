"""M7-AUI-10.4 —— Failed Verify Reuse & Resource Lifecycle（frontend-only）。

修两个真实缺陷（M7-AUI-10.4 READ-ONLY AUDIT 坐实）：

- **F-1（高）**：Home 的 Agent 侧「测试连接」每次点击都 POST /api/agents，失败重试
  产生 `WorkBuddy / 2 / 3 / …`（真实库已 5 个）。根因：没有 `editingAgentId`；
  `selectedAgentId` 只在 verify 成功后才赋值；`generateDefaultName` 只是**名称**去重。
- **F-2（中）**：渠道面板 `testChannel()` 在 new 模式保存失败后 `editingChannelId`
  仍为 null → Retry 再次 POST → `企业微信 2 / 3 / …`。

顺带修一个**既有潜伏缺陷**（同文件同失败路径，非本轮引入）：`markSideFailed` 仍按
M6.3 旧签名传 `nodeChannelValue` / `nodeAgentValue`，而这两个 const 与对应 DOM 在
M7-AUI-3 重构时已移除 → 失败路径抛 ReferenceError，把「没有接通你的 Agent」这类可行动
文案覆盖成通用文案，且该侧状态永远停在「正在连接…」。

合同：Create ≠ Verify；Verify FAILED ≠ Resource CREATE FAILED；
Retry = SAME Agent + SAME Channel + SAME Connection + Verify again；
只有显式「+ 添加 Agent」/「+ 添加渠道」才创建新资源。

边界：只改 `frontend/home.js`；零后端 / 零 DB / 零 migration / 零 API 契约；
Connection 逻辑不动；Advanced（Disable-only）不碰；不引入 storage / Idempotency-Key。

方法：静态源码断言 + Node `vm` 行为级执行（真跑 testAgent / testChannel /
openNewAgentPanel / openNewChannelPanel / connectCurrentFlow，假后端按**场景增量**
统计端点创建次数）；缺 Node 自动 skip；只读、不触 DB / 不触网。
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

def test_a1_agent_panel_has_existing_new_state() -> None:
    """F-1 根因：Agent 面板具备 existing/new 语义 + 身份锚点。

    M7-AUI-10.6.2-A 合同变更：编辑对象解析从
    `selectedAgent() || displayAgent()`（隐式回退列表第一项，10.6 事故根因）
    收窄为 `selectedAgent() || agentById(renderedAgentId)`（显式选择 → 当前
    Flow 显示对象；解析失败阻断，绝不静默创建）。原字面断言随之更新。
    """
    code = _js()
    assert "let editingAgentId = null;" in code
    assert "let agentPanelMode" in code
    body = _fn(code, "syncAgentPanel")
    assert "agentPanelMode" in body and "editingAgentId" in body
    assert "selectedAgent() || agentById(renderedAgentId)" in body
    assert "displayAgent()" not in body, "编辑解析不得隐式回退列表第一项（10.6.2-A）"
    assert "editingAgentMissing" in body, "解析失败必须阻断（10.6.2-A6）"
    assert "编辑 Agent：" in body, "标题必须显示实际对象名（10.6.2-A1）"
    assert 'paWbApiKey.value = ""' in body, "API Key 绝不在面板回显（M7-AUI-10.2 不动）"


def test_a2_ensure_agent_reuses_before_post() -> None:
    """F-1 核心：复用闸门必须早于 POST。"""
    body = _fn(_js(), "ensureAgent")
    assert body.index("editingAgentId != null") < body.index('request("POST", AGENTS_API')
    assert "const known = ref.agents.find" in body
    assert "generateDefaultName(" in body, "名称去重器仍只服务真正创建"
    assert "err.status === 409" in body and "reconcileCreated(" in body


def test_a3_test_agent_anchors_identity_before_verify() -> None:
    """三条出口（成功 / reachable=false / 抛错）都必须保留身份。"""
    body = _fn(_js(), "testAgent")
    assert body.index("editingAgentId = agentId;") < body.index("/verify")
    assert "selectedAgentId = agentId;" in body
    assert "editingAgentId = null" not in body[body.index("/verify"):]
    cfg = _fn(_js(), "buildWorkBuddyConfig")
    assert "editingAgentId != null" in cfg, "复用态空凭据不得报错（只需 Verify）"
    assert "请填写 WorkBuddy Agent ID。" in cfg and "请填写 API Key。" in cfg


def test_a4_channel_keeps_identity_after_create_and_on_uncertainty() -> None:
    """F-2：创建成功立即记身份；不确定态对账，绝不二次 POST。"""
    body = _fn(_js(), "testChannel")
    assert "editingChannelId = channel.id;" in body
    assert body.index("editingChannelId = channel.id;") < body.index("await refresh()")
    assert "reconcileCreated(CHANNELS_API" in body
    assert "err.timedOut || err.status === 0" in body
    assert 'const creating = built.value.method === "POST";' in body


def test_a5_explicit_new_entry_resets_identity() -> None:
    """只有显式「+ 添加 Agent」才清空身份进入 new 模式。"""
    code = _js()
    new_panel = _fn(code, "openNewAgentPanel")
    assert 'agentPanelMode = "new"' in new_panel and "editingAgentId = null" in new_panel
    handler = code[code.index("ctaEditAgent.addEventListener"):]
    handler = handler[:handler.index("});")]
    assert 'agentPanelMode = "existing"' in handler and "syncAgentPanel()" in handler


def test_a6_no_write_surface_expansion() -> None:
    """写面零扩张：不新增端点 / DELETE / PUT；PATCH 仍只作用于渠道。

    M7-AUI-10.6.2-C 合同收窄（原全量禁令 → 定向禁令）：
    - localStorage / indexedDB / idempotency 仍然**全量禁止**；
    - sessionStorage 仅允许用于 Flow 选择（key 固定 "sf.flow.selection"，
      只存 {ch, ag} 两个 ID），每个调用点都必须带该 key；
    - 资源创建幂等性要求不变（10.4 的 reconcileCreated 语义原样）。
    """
    code = _js()
    assert set(re.findall(r'"/api/[a-z]+"', code)) == {
        '"/api/agents"', '"/api/channels"', '"/api/connections"',
        '"/api/interactions"', '"/api/auth"'}
    for banned in ('"DELETE"', '"PUT"'):
        assert banned not in code
    assert code.count('"PATCH"') == 1, "PATCH 只允许出现一次（渠道凭据更新）"
    at = code.index('"PATCH"')
    assert "CHANNELS_API" in code[max(0, at - 400):at + 400]
    assert "localStorage" not in code and "indexedDB" not in code
    assert "idempotency" not in code
    # sessionStorage 定向合同：只服务 Flow 选择，key 常量固定
    ss_calls = re.findall(r"sessionStorage\.(\w+)\(", code)
    assert set(ss_calls) <= {"getItem", "setItem", "removeItem"}, ss_calls
    assert 'const FLOW_SELECTION_KEY = "sf.flow.selection";' in code
    assert code.count("FLOW_SELECTION_KEY") >= 4, "存/取/清 三处 + 声明"
    # 选择状态不携带任何凭据形态（三键 / runtime cache / 凭据类词根）
    sel_block = code[code.index("跨页面选择恢复"):code.index("外部身份重复")]
    for banned in ("api_key", "provider", "workbuddy_agent_id", "runtime_id",
                   "bearer_token", "endpoint", "config"):
        assert banned not in sel_block, f"Flow 选择状态不得携带 {banned}"


def test_a7_failure_path_has_no_stale_reference() -> None:
    """既有缺陷回归护栏：失败路径不得引用 M6.3 已移除的节点 value 元素。"""
    code = _js()
    assert "nodeChannelValue" not in code and "nodeAgentValue" not in code
    body = _fn(code, "markSideFailed")
    assert 'setNode(nodeAgent, "is-error")' in body
    assert 'setNode(nodeChannel, "is-error")' in body


def test_a8_untouched_contracts_intact() -> None:
    """不得改动：顺序铁律、Connection 复用、First Success、connectCurrentFlow。"""
    code = _js()
    body = _fn(code, "testAgent")
    assert body.index("/verify") < body.index("bootstrap-arm"), "必须先验证再启用"
    binding = _fn(code, "ensureBinding")
    assert binding.index("ref.connections.find") < binding.index('request("POST", CONNECTIONS_API')
    assert "err.status !== 409" in binding
    carrier = _fn(code, "ensureCarrierChannel")
    assert 'type !== "local"' in carrier and 'c.type === "local"' in carrier
    flow = _fn(code, "connectCurrentFlow")
    for forbidden in ("AGENTS_API", "CHANNELS_API", "ensureAgent", "testChannel"):
        assert forbidden not in flow
    assert "ensureBinding" in flow and "/verify" in flow
    fs = _fn(code, "checkFirstSuccess")
    assert "INTERACTIONS_API" in fs and "channel_id" in fs


# ================================================================== B. Node 行为

_JS_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const src = fs.readFileSync(process.env.PROBE_JS, "utf8").replace(/^import\s[^;]*;/gm, "");
const els = {};
function fakeEl() {
  const classes = new Set();
  return {
    classList: {
      add(c) { classes.add(c); }, remove(c) { classes.delete(c); },
      toggle(c, on) { if (on === undefined) { classes.has(c) ? classes.delete(c) : classes.add(c); }
                      else if (on) { classes.add(c); } else { classes.delete(c); } },
      contains(c) { return classes.has(c); } },
    textContent: "", className: "", href: "", value: "", disabled: false, open: false,
    dataset: {}, style: {}, children: [], firstChild: null,
    addEventListener() {}, removeEventListener() {}, appendChild() {}, removeChild() {},
    insertBefore() {}, setAttribute() {}, getAttribute() { return null; },
    querySelectorAll() { return []; }, querySelector() { return null; },
    closest() { return null; }, contains() { return false; }, focus() {}, blur() {},
    scrollIntoView() {}, click() {} };
}
function el(id) { if (!els[id]) els[id] = fakeEl(); return els[id]; }

const state = { agents: [], channels: [], connections: [],
                nextAgent: 100, nextChannel: 200, nextConn: 300,
                reachable: false, failCreateChannel: false };
const calls = [];
function countExact(method, url) {
  return calls.filter(function (c) { return c.method === method && c.url === url; }).length;
}
function json(body, status) {
  return Promise.resolve({ ok: status < 400, status: status,
    json: function () { return Promise.resolve(body); } });
}
function fail() { return Promise.reject(new Error("network down")); }
function pick(list, id) {
  for (let i = 0; i < list.length; i += 1) { if (list[i].id === id) return list[i]; }
  return null;
}

const sandbox = {
  console: console,
  document: { getElementById: el, createElement: function () { return fakeEl(); } },
  window: { location: { replace() {}, origin: "https://acp.local" } },
  fetch: function (url, options) {
    const opt = options || {};
    const method = (opt.method || "GET").toUpperCase();
    const payload = opt.body ? JSON.parse(opt.body) : null;
    calls.push({ method: method, url: url, payload: payload });
    if (url === "/api/auth/me") return json({ id: 1, company_id: 1 }, 200);
    if (url === "/api/agents") {
      if (method === "GET") return json({ items: state.agents.slice() }, 200);
      const created = { id: state.nextAgent++, name: payload.name, type: payload.type,
                        status: "active", config: {} };
      state.agents.push(created); return json(created, 201);
    }
    if (url === "/api/channels") {
      if (method === "GET") return json({ items: state.channels.slice() }, 200);
      if (state.failCreateChannel) {   // 服务端确实建了，客户端收到网络失败
        state.failCreateChannel = false;
        state.channels.push({ id: state.nextChannel++, name: payload.name, type: payload.type,
                              status: "active", config: payload.config || {} });
        return fail();
      }
      const created = { id: state.nextChannel++, name: payload.name, type: payload.type,
                        status: "active", config: payload.config || {} };
      state.channels.push(created); return json(created, 201);
    }
    if (url.indexOf("/api/channels/") === 0 && method === "PATCH") {
      const found = pick(state.channels, Number(url.split("/").pop()));
      if (found) found.config = Object.assign({}, found.config, payload.config || {});
      return json(found, 200);
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
  + " testAgent: testAgent, testChannel: testChannel,"
  + " openNewAgentPanel: openNewAgentPanel, openNewChannelPanel: openNewChannelPanel,"
  + " connectCurrentFlow: connectCurrentFlow,"
  + " ids: function () { return { editingAgentId: editingAgentId,"
  + "   selectedAgentId: selectedAgentId, editingChannelId: editingChannelId }; },"
  + " openEditAgent: function () { agentPanelMode = 'existing'; syncAgentPanel(); },"
  + "};", sandbox);

const settle = async function () {
  for (let i = 0; i < 12; i += 1) await new Promise(function (r) { setTimeout(r, 0); });
};
function fillAgentPanel() {
  els["pa-provider"].value = "workbuddy";
  els["pa-wb-agent-id"].value = "agent-x";
  els["pa-wb-api-key"].value = "key-x";
}
function fillChannelPanel(type, botId, secret) {
  els["pc-type"].value = type;   // 必须在 openNewChannelPanel 之后填（它会清空字段）
  if (type === "wecom_bot") { els["pc-bot-id"].value = botId; els["pc-bot-secret"].value = secret; }
}
function mark() {
  return { agents: state.agents.length, channels: state.channels.length,
           conns: state.connections.length, calls: calls.length };
}
function delta(b, a) {
  return { agents: a.agents - b.agents, channels: a.channels - b.channels,
           conns: a.conns - b.conns, calls: a.calls - b.calls,
           agentsPost: countExact("POST", "/api/agents"),
           channelsPost: countExact("POST", "/api/channels"),
           connsPost: countExact("POST", "/api/connections") };
}
function trace(from, to) {
  return calls.slice(from, to).map(function (c) {
    return c.method + " " + c.url + (c.payload ? " " + JSON.stringify(c.payload) : ""); });
}

(async function () {
  await settle();
  const t = sandbox.__t;
  const scenario = process.env.PROBE_SCENARIO || "agent_retry";
  const out = { scenario: scenario };

  function fillAgent() {
    els["pa-provider"].value = "workbuddy";
    els["pa-wb-agent-id"].value = "agent-x";
    els["pa-wb-api-key"].value = "key-x";
  }
  function fillChannel(botId, secret) {
    // openNewChannelPanel 会清空凭据字段（真实 UX：先开面板、再填），故在其后填。
    els["pc-type"].value = "wecom_bot";
    els["pc-bot-id"].value = botId;
    els["pc-bot-secret"].value = secret;
  }
  function counts() {
    return { agentsPost: countExact("POST", "/api/agents"),
             channelsPost: countExact("POST", "/api/channels"),
             connsPost: countExact("POST", "/api/connections"),
             verifyCount: calls.filter(function (c) {
               return c.method === "POST" && c.url.indexOf("/verify") !== -1; }).length };
  }
  function totals() {
    return { agents: state.agents.map(function (a) { return a.name; }),
             agentIds: state.agents.map(function (a) { return a.id; }),
             channels: state.channels.map(function (c) { return c.name; }),
             channelIds: state.channels.map(function (c) { return c.id; }),
             conns: state.connections.map(function (c) { return [c.channel_id, c.agent_id]; }) };
  }
  function wire() {
    return calls.map(function (c) { return c.method + " " + c.url; });
  }

  if (scenario === "agent_retry") {
    // 首次测试连接（无 Agent）→ Verify 失败 → 再点 3 次
    fillAgent();
    await t.testAgent(); await settle();
    out.first = { counts: counts(), totals: totals(), wire: wire(),
                  anchor: t.ids().editingAgentId,
                  paError: els["pa-error"].textContent,
                  agentNode: els["node-agent"].className };
    await t.testAgent(); await settle();
    await t.testAgent(); await settle();
    await t.testAgent(); await settle();
    out.afterRetries = { counts: counts(), totals: totals(), wire: wire(),
                         anchor: t.ids().editingAgentId,
                         selected: t.ids().selectedAgentId };
  } else if (scenario === "agent_reuse_no_creds") {
    // 复用态：凭据不回显（M7-AUI-10.2），空字段也只做 Verify
    fillAgent();
    await t.testAgent(); await settle();
    const before = counts();
    els["pa-wb-agent-id"].value = ""; els["pa-wb-api-key"].value = "";
    t.openEditAgent();
    await t.testAgent(); await settle();
    out.reuse = { before: before, after: counts(), totals: totals(),
                  anchor: t.ids().editingAgentId };
  } else if (scenario === "agent_explicit_new") {
    // 首次失败 → 显式「+ 添加 Agent」→ 必须真正创建新资源。
    // M7-AUI-10.5：显式新建**不同**外部身份 → 新资源（同身份会触发冲突选择，
    // 该路径由 test_m7_aui_10_5_home_duplicate_identity.py 验收）。
    fillAgent();
    await t.testAgent(); await settle();
    const before = counts();
    t.openNewAgentPanel();
    els["pa-provider"].value = "workbuddy";
    els["pa-wb-agent-id"].value = "agent-y";
    els["pa-wb-api-key"].value = "key-y";
    await t.testAgent(); await settle();
    out.explicit = { before: before, after: counts(), totals: totals(),
                     anchor: t.ids().editingAgentId };
  } else if (scenario === "channel_retry") {
    // 首次保存（POST 一次）→ 改凭据再保存（必须 PATCH）
    t.openNewChannelPanel();
    fillChannel("bot-1", "secret-1");
    await t.testChannel(); await settle();
    const createdId = t.ids().editingChannelId;
    out.first = { counts: counts(), totals: totals(), anchor: createdId };
    els["pc-bot-secret"].value = "secret-2";
    await t.testChannel(); await settle();
    out.afterRetry = { counts: counts(), totals: totals(), anchor: t.ids().editingChannelId,
                       patchOnCreated: createdId == null ? 0
                         : countExact("PATCH", "/api/channels/" + createdId) };
  } else if (scenario === "channel_uncertain") {
    // 新建时「网络失败」但服务端已建 → 对账接管；再保存必须 PATCH
    state.failCreateChannel = true;
    t.openNewChannelPanel();
    fillChannel("bot-timeout", "secret-t");
    await t.testChannel(); await settle();
    const adoptedId = t.ids().editingChannelId;
    out.uncertain = { counts: counts(), totals: totals(), adopted: adoptedId,
                      pcError: els["pc-error"].textContent };
    els["pc-bot-secret"].value = "secret-t2";
    await t.testChannel(); await settle();
    out.afterRetry = { counts: counts(), totals: totals(), anchor: t.ids().editingChannelId,
                       patchOnAdopted: adoptedId == null ? 0
                         : countExact("PATCH", "/api/channels/" + adoptedId) };
  } else if (scenario === "flow_only_binding") {
    // 回归：connectCurrentFlow 只做 ensureBinding + verify，绝不建 Agent/Channel
    fillAgent();
    await t.testAgent(); await settle();
    t.openNewChannelPanel();
    fillChannel("bot-1", "secret-1");
    await t.testChannel(); await settle();
    const before = counts();
    await t.connectCurrentFlow(); await settle();
    out.flow = { before: before, after: counts(), totals: totals() };
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
def test_b1_first_test_creates_exactly_one_agent() -> None:
    """§15-1：没有 Agent → POST /api/agents 恰好一次，且身份立即落下。"""
    o = _probe("agent_retry")["first"]
    c, tot = o["counts"], o["totals"]
    assert c["agentsPost"] == 1 and tot["agents"] == ["WorkBuddy"], o
    assert c["channelsPost"] == 1, "自检载体只建一条（ensureCarrierChannel 幂等）"
    assert c["connsPost"] == 1 and len(tot["conns"]) == 1, o
    assert o["anchor"] is not None, "创建成功必须立即落下 Agent 身份"


@needs_node
def test_b2_failure_is_expressed_at_the_endpoint() -> None:
    """失败就地表达：具体文案 + 该侧节点红（既有 ReferenceError 缺陷的回归护栏）。"""
    o = _probe("agent_retry")["first"]
    assert "没有接通你的 Agent" in o["paError"], f"失败文案被覆盖：{o['paError']!r}"
    assert "is-error" in o["agentNode"], o["agentNode"]


@needs_node
def test_b3_retry_after_failed_verify_creates_nothing() -> None:
    """§15-2~5 / §18：FAILED 后连点 3 次 → Agent/Channel/Connection 零新增。"""
    o = _probe("agent_retry")
    first, after = o["first"]["counts"], o["afterRetries"]["counts"]
    tot = o["afterRetries"]["totals"]
    assert after["agentsPost"] == first["agentsPost"] == 1, o
    assert tot["agents"] == ["WorkBuddy"], f"不得出现 WorkBuddy 2：{tot['agents']}"
    assert after["channelsPost"] == first["channelsPost"], o
    assert after["connsPost"] == first["connsPost"] == 1, o
    assert len(tot["conns"]) == 1, o
    assert after["verifyCount"] == 4, "首次 + 3 次重试都必须真的重新 Verify"
    assert o["afterRetries"]["anchor"] == o["first"]["anchor"], "身份必须跨失败保留"


@needs_node
def test_b4_reuse_mode_needs_no_credentials() -> None:
    """复用态：凭据不回显（M7-AUI-10.2 不动），空字段也只做 Verify。"""
    o = _probe("agent_reuse_no_creds")["reuse"]
    assert o["after"]["agentsPost"] == o["before"]["agentsPost"], o
    assert o["after"]["connsPost"] == o["before"]["connsPost"], o
    assert o["after"]["verifyCount"] == o["before"]["verifyCount"] + 1, o
    assert o["totals"]["agents"] == ["WorkBuddy"], o
    assert o["anchor"] is not None


@needs_node
def test_b5_explicit_add_agent_creates_second_agent() -> None:
    """§15-6/7：「+ 添加 Agent」必须能真正创建新资源。"""
    o = _probe("agent_explicit_new")["explicit"]
    assert o["after"]["agentsPost"] == o["before"]["agentsPost"] + 1, o
    assert o["totals"]["agents"] == ["WorkBuddy", "WorkBuddy 2"], o["totals"]["agents"]
    assert o["anchor"] is not None


@needs_node
def test_b6_channel_retry_is_patch_not_post() -> None:
    """§15-8~12：Channel 首次 POST 一次；再保存走 PATCH，绝不「企业微信 2」。"""
    p = _probe("channel_retry")
    first, retry = p["first"], p["afterRetry"]
    assert first["counts"]["channelsPost"] == 1, first
    assert first["totals"]["channels"] == ["企业微信"], first
    assert first["anchor"] is not None, "创建成功必须立即记住渠道身份（F-2 修复点）"
    assert retry["counts"]["channelsPost"] == 1, f"重试又 POST 了：{retry}"
    assert retry["totals"]["channels"] == ["企业微信"], retry
    assert retry["patchOnCreated"] == 1, retry
    assert retry["anchor"] == first["anchor"], retry


@needs_node
def test_b7_uncertain_create_is_reconciled() -> None:
    """§十三-E：POST 结果不确定 → 按名称对账接管身份，Retry 不再 POST。"""
    p = _probe("channel_uncertain")
    un, retry = p["uncertain"], p["afterRetry"]
    assert un["adopted"] is not None, "对账命中必须接管身份（否则 Retry 会再建一条）"
    assert un["totals"]["channels"] == ["企业微信"], un
    assert retry["counts"]["channelsPost"] == un["counts"]["channelsPost"], retry
    assert retry["totals"]["channels"] == ["企业微信"], retry
    assert retry["patchOnAdopted"] == 1, retry


@needs_node
def test_b8_connect_current_flow_creates_no_agent_or_channel() -> None:
    """§17：connectCurrentFlow() 行为不变——只 ensureBinding + verify。"""
    o = _probe("flow_only_binding")["flow"]
    assert o["after"]["agentsPost"] == o["before"]["agentsPost"], o
    assert o["after"]["channelsPost"] == o["before"]["channelsPost"], o
    assert o["after"]["connsPost"] == o["before"]["connsPost"] + 1, \
        f"全新渠道 × 当前 Agent 是新绑定，合法建 1 条：{o}"
    assert o["after"]["verifyCount"] == o["before"]["verifyCount"] + 1, o

