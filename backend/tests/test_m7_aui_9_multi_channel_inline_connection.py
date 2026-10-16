"""M7-AUI-9 —— Flow Home 多渠道 + 就地连接 验收门。

本步解决三个真实 UX 问题（frontend-only，零后端改动）：

1. 用户如何增加第二个 Channel：`渠道 [▼] [+ 添加渠道]` 当前渠道选择器 +
   本页原地新建（不跳页、无 Tab / 无多渠道同屏 / 无管理面板）。
2. 首次创建 Channel / Agent 不要求手工填写名称：`generateDefaultName`
   生成第一个可用默认名（企业微信 / 企业微信 2 / 企业微信 3 …；
   WorkBuddy / WorkBuddy 2 …；自建 Agent / 自建 Agent 2 …），
   且**绝不修改已有对象的名称**。
3. 「把它们连接起来」在**当前页**直接建立 Connection
   （Channel Ready + Agent Ready 才可执行）→ 刷新当前 Flow 状态；
   绝不跳转 /connect.html（它保留为独立接入 / 恢复入口）。

冻结的状态语义（延续 M7-AUI-5）：

    当前 Flow = 当前渠道 + 当前 Agent
    Channel Ready / Agent Ready = 对象 active
    Connected = 这对 (Channel, Agent) 自己存在 active Connection（且两端 active）
    First Success = 同一 Interaction：processing=succeeded × delivery=delivered

切换当前渠道后，绝不能显示别的渠道的 Connected / First Success。

方法：静态源码断言 + Node `vm` 行为矩阵（直接执行 home.js 的真实
`render` / `generateDefaultName` / 事件处理器），缺 Node 自动 skip；
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
HOME_HTML = FRONTEND / "home.html"
HOME_JS = FRONTEND / "home.js"
CONNECT_HTML = FRONTEND / "connect.html"

_NODE = shutil.which("node")
needs_node = pytest.mark.skipif(
    _NODE is None, reason="node 不可用（行为级验收需要 Node）")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _html() -> str:
    return re.sub(r"<!--.*?-->", " ", _read(HOME_HTML), flags=re.S)


def _js() -> str:
    src = re.sub(r"/\*.*?\*/", " ", _read(HOME_JS), flags=re.S)
    return re.sub(r"//[^\n]*", " ", src)


# ================================================================== A. 结构 / 选择器

def test_a1_flow_home_has_channel_and_agent_pickers() -> None:
    """§四/§十一：当前渠道 + 当前 Agent 选择器 + 「+ 添加」入口。"""
    html = _html()
    for marker in ("channel-picker", "agent-picker",
                   "cta-add-channel", "cta-add-agent"):
        assert f'id="{marker}"' in html, f"Flow Home 缺少：{marker}"
    assert '<select id="channel-picker"' in html
    assert '<select id="agent-picker"' in html
    # 用户语言（§二十一）：添加渠道 / 添加 Agent
    assert "+ 添加渠道" in html and "+ 添加 Agent" in html


def test_a2_pickers_live_inside_flow_nodes() -> None:
    """§六 + M7-AUI-10.4：选择器内嵌在两侧节点上——「渠道 [▼] + 添加渠道」
    本身就表达当前渠道名，节点不再重复显示名称文本。"""
    html = _html()
    for banned in ("tab", "拖拽", "Channel dashboard", "多 Channel"):
        assert banned.lower() not in html.lower() or banned == "tab", banned
    chan_node = html[html.index('id="node-channel"'):html.index('id="link-channel"')]
    assert 'id="channel-picker"' in chan_node and "cta-add-channel" in chan_node
    agent_node = html[html.index('id="node-agent"'):html.index('id="panel-channel"')]
    assert 'id="agent-picker"' in agent_node and "cta-add-agent" in agent_node
    # 节点不再有独立的名称值文本（名称由选择器表达，不重复显示）
    assert 'id="node-channel-value"' not in html
    assert 'id="node-agent-value"' not in html


def test_a3_customer_surface_stays_clean() -> None:
    """§二/§二十一：新增表面不出现内部 machinery 术语（只看产品主表面，
    剥离 href —— 顶栏菜单的 /interactions.html 等管理入口是合法导航）。"""
    html = _html()
    section = html[html.index('id="home-section"'):]
    stripped = re.sub(r'href="[^"]*"', "", section).lower()
    for banned in ("endpoint", "credential", "config", "runtime", "acp",
                   "connection id", "identity", "session", "bootstrap",
                   "channel_id", "grant", "interaction"):
        assert banned not in stripped, f"Home 客户面出现内部术语：{banned}"


# ================================================================== B. 默认名称

def test_b1_default_name_helper_exists_and_is_frontend_only() -> None:
    """§七/§八/§十：generateDefaultName 在前端实现，不新增端点。"""
    code = _js()
    assert "function generateDefaultName" in code
    assert "function normalizeName" in code
    # 渠道 / Agent 默认名都走它（而不是各自硬编码计数）
    body = code[code.index("function uniqueChannelName"):]
    body = body[:body.index("\n}")]
    assert "generateDefaultName(" in body
    agent_body = code[code.index("async function ensureAgent"):]
    agent_body = agent_body[:agent_body.index("\n}")]
    assert "generateDefaultName(" in agent_body


def test_b2_no_name_input_required_on_home() -> None:
    """§七/§八：Home 不要求用户填写 Channel / Agent 名称。"""
    html = _html()
    for banned in ('id="f-name"', "渠道名称", "Agent 名称", "绑定名称"):
        assert banned not in html, f"Home 仍要求填写名称：{banned}"


def test_b3_existing_objects_are_never_renamed() -> None:
    """§九：默认名称只用于新建对象；Home 不得改已有对象名称（无 Agent/Connection 写面）。"""
    code = _js()
    # PATCH 只允许出现一次且只作用于 channels（M7-AUI-3 既有契约，继续成立）
    assert code.count('"PATCH"') == 1
    at = code.index('"PATCH"')
    assert "CHANNELS_API" in code[max(0, at - 400):at + 400]
    # 没有任何「改名」动作：不存在 rename / 更新名称的调用
    assert "rename" not in code.lower()


# ================================================================== C. 就地连接

def test_c1_connect_button_is_in_place_and_never_navigates() -> None:
    """§十二：cta-primary 是按钮，Home 普通路径不再引用 /connect.html。"""
    html = _html()
    tag = re.search(r"<button[^>]*id=\"cta-primary\"[^>]*>", html)
    assert tag is not None, "「把它们连接起来」必须是本页按钮"
    assert "href" not in tag.group(0)
    assert 'href="/connect.html"' not in html
    code = _js()
    assert "/connect.html" not in code, "home.js 仍跳转 connect.html"
    # connect.html 本身保留（§二十：不删除、不修改其既有 onboarding 逻辑）
    assert CONNECT_HTML.is_file()
    assert 'id="workbuddy-block"' in _read(CONNECT_HTML)


def test_c2_inline_connect_reuses_verify_then_arm() -> None:
    """§十二 + 顺序铁律：建通路 → verify（真实探针）→ 通过后才 bootstrap-arm。"""
    code = _js()
    body = code[code.index("async function connectCurrentFlow"):]
    body = body[:body.index("\n}")]
    assert "ensureBinding(" in body, "必须复用既有建通路逻辑"
    assert "/verify" in body and "bootstrap-arm" in body
    assert body.index("/verify") < body.index("bootstrap-arm"), "必须先验证再启用"
    assert "POST" in body
    assert '"PATCH"' not in body and '"DELETE"' not in body


def test_c3_failure_keeps_user_language_plus_detail_fold() -> None:
    """§十五：失败留在本页，用户语言在上、技术细节折叠。"""
    html = _html()
    assert 'id="home-error-detail"' in html
    assert "查看详细信息" in html
    code = _js()
    body = code[code.index("async function connectCurrentFlow"):]
    body = body[:body.index("\n}")]
    assert "连接失败，请检查配置后重试。" in body
    assert "homeErrorDetailBody" in body


# ================================================================== D. Node 行为矩阵

_JS_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const src = fs.readFileSync(process.env.PROBE_JS, "utf8").replace(/^import\s[^;]*;/gm, "");
const scenario = process.env.PROBE_SCENARIO || "success";

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
["panel-channel", "panel-agent", "pa-wb-block", "cta-verify", "cta-primary",
 "cta-advanced", "cta-primary-hint", "flow-badge", "home-error",
 "home-error-detail", "pc-error", "pa-error", "pc-saved-hint"]
 .forEach(function (id) { el(id).classList.add("hidden"); });
el("pc-type").value = "wecom_bot";
el("pa-provider").value = "workbuddy";

// —— 可编程的服务端状态 ——
const state = {
  agents: JSON.parse(process.env.PROBE_AGENTS || "[]"),
  channels: JSON.parse(process.env.PROBE_CHANNELS || "[]"),
  connections: JSON.parse(process.env.PROBE_CONNECTIONS || "[]"),
  verifyReachable: true,
  nextAgentId: 100, nextChannelId: 11, nextConnId: 5,
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
      return ok(200, { connection_id: 5, reachable: state.verifyReachable,
                       checked_at: "now" });
    }
    if (u.indexOf("/api/interactions") >= 0) return ok(200, { items: [] });
    if (u.indexOf("/api/agents") >= 0) {
      if (method === "POST") {
        const a = { id: state.nextAgentId++, name: payload.name,
                    type: payload.type, status: "active" };
        state.agents.push(a);
        return ok(201, a);
      }
      return ok(200, { items: state.agents, total: state.agents.length });
    }
    if (u.indexOf("/api/channels") >= 0) {
      if (method === "POST") {
        const c = { id: state.nextChannelId++, name: payload.name,
                    type: payload.type, status: "active",
                    config: payload.config || null };
        state.channels.push(c);
        return ok(201, c);
      }
      if (method === "PATCH") {
        const id = Number(u.split("/").pop());
        const c = state.channels.find((x) => x.id === id);
        c.config = Object.assign({}, c.config || {}, (payload && payload.config) || {});
        return ok(200, Object.assign({}, c));
      }
      return ok(200, { items: state.channels, total: state.channels.length });
    }
    if (u.indexOf("/api/connections") >= 0) {
      if (method === "POST") {
        const cn = { id: state.nextConnId++, channel_id: payload.channel_id,
                     agent_id: payload.agent_id, name: payload.name,
                     status: "active", config: payload.config || null };
        state.connections.push(cn);
        return ok(201, cn);
      }
      return ok(200, { items: state.connections, total: state.connections.length });
    }
    return ok(404, {});
  },
};
vm.createContext(sandbox);
const sharedPath = process.env.PROBE_JS.replace(/[^/\\]+$/, "") + "shared/http.js";
const sharedSrc = fs.readFileSync(sharedPath, "utf8").replace(/^export /gm, "");
vm.runInContext(sharedSrc, sandbox);
vm.runInContext(src + "\n;globalThis.__t = { render: render, generateDefaultName: "
  + "generateDefaultName, uniqueChannelName: uniqueChannelName, ref: ref };", sandbox);

const flush = () => new Promise((r) => setTimeout(r, 0));
const settle = async () => { for (let i = 0; i < 8; i += 1) await flush(); };
const vis = (id) => !els[id].classList.contains("hidden");
const badge = () => els["flow-badge"].textContent;
const hub = () => els["node-hub-value"].textContent;
// M7-AUI-10.4：当前名称由节点内嵌选择器的值表达（节点不再有独立名称文本）——
// 从选择器选中值 + 页面内 ref 解析名称。
const pickName = (pickerId, list, excludeLocal) => {
  const v = els[pickerId].value;
  const item = (sandbox.__t.ref[list] || []).find(
    (x) => String(x.id) === v && (!excludeLocal || x.type !== "local"));
  return item ? item.name : "(空)";
};
const chName = () => pickName("channel-picker", "channels", true);
const agName = () => pickName("agent-picker", "agents", false);
const chPickerHidden = () => els["channel-picker"].classList.contains("hidden");
const agPickerHidden = () => els["agent-picker"].classList.contains("hidden");
const chLink = () => els["link-channel"].className;
const agLink = () => els["link-agent"].className;
const picker = (id) => els[id].value;
const detail = () => els["home-detail"].textContent;
const postCalls = () => calls.filter((c) => c.method === "POST").map((c) => c.url);

(async function () {
  await new Promise((r) => setTimeout(r, 400)); // 等 autofill 兜底定时器跑完
  const t = sandbox.__t;
  const out = { scenario: scenario, navigations: navigations };

  if (scenario === "toggles") {
    // M7-AUI-10.4 需求③④：「+ 添加」再点一次 = 收起；「编辑」打开 existing 面板，再点收起
    els["cta-add-channel"].handlers.click();
    await settle();
    out.addChannelOpen = { panel: vis("panel-channel"),
                           title: els["panel-channel-title"].textContent };
    els["cta-add-channel"].handlers.click();
    await settle();
    out.addChannelClosed = { panel: vis("panel-channel") };
    els["cta-edit-channel"].handlers.click();
    await settle();
    out.editChannelOpen = { panel: vis("panel-channel"),
                            title: els["panel-channel-title"].textContent };
    els["cta-edit-channel"].handlers.click();
    await settle();
    out.editChannelClosed = { panel: vis("panel-channel") };
    els["cta-add-agent"].handlers.click();
    await settle();
    out.addAgentOpen = { panel: vis("panel-agent"),
                         title: els["panel-agent-title"].textContent };
    els["cta-add-agent"].handlers.click();
    await settle();
    out.addAgentClosed = { panel: vis("panel-agent") };
    els["cta-edit-agent"].handlers.click();
    await settle();
    out.editAgentOpen = { panel: vis("panel-agent"),
                          title: els["panel-agent-title"].textContent };
    els["cta-edit-agent"].handlers.click();
    await settle();
    out.editAgentClosed = { panel: vis("panel-agent") };
    process.stdout.write(JSON.stringify(out));
    return;
  }

  if (scenario === "names") {
    out.names = {
      first: t.generateDefaultName("企业微信", []),
      second: t.generateDefaultName("企业微信", ["企业微信"]),
      third: t.generateDefaultName("企业微信", ["企业微信", "企业微信 2"]),
      wbSecond: t.generateDefaultName("WorkBuddy", ["WorkBuddy"]),
      ownSecond: t.generateDefaultName("自建 Agent", ["自建 Agent"]),
      caseInsensitive: t.generateDefaultName("WorkBuddy", ["workbuddy"]),
      trimsBase: t.generateDefaultName("  企业微信  ", ["企业微信"]),
      trimsExisting: t.generateDefaultName("企业微信", [" 企业微信 "]),
      gap: t.generateDefaultName("企业微信", ["企业微信", "企业微信 3"]),
    };
    out.uniqueLocal = t.uniqueChannelName("local");
    process.stdout.write(JSON.stringify(out));
    return;
  }

  // 初始渲染
  await t.render(state.agents, state.channels, state.connections);
  out.initial = {
    badge: badge(), hub: hub(), chName: chName(), agName: agName(),
    chPicker: picker("channel-picker"), agPicker: picker("agent-picker"),
    chPickerHidden: chPickerHidden(), agPickerHidden: agPickerHidden(),
    chLink: chLink(), agLink: agLink(),
    primaryVisible: vis("cta-primary"),
    primaryText: els["cta-primary"].textContent,
    verifyVisible: vis("cta-verify"),
    detail: detail(),
  };

  if (scenario === "firsttime") {
    // M7-AUI-10.4 需求②：第一次进来（无渠道 / Agent）→ 只显示「+ 添加」入口，
    // 选择器隐藏；创建保存一个之后，选择器才出现。
    out.emptyState = {
      chPickerHidden: chPickerHidden(), agPickerHidden: agPickerHidden(),
      addChannelVisible: vis("cta-add-channel"),
      addAgentVisible: vis("cta-add-agent"),
    };
    state.channels = [{ id: 30, name: "企业微信", type: "wecom_bot", status: "active" }];
    state.agents = [{ id: 40, name: "WorkBuddy", type: "acp", status: "active" }];
    await t.render(state.agents, state.channels, state.connections);
    out.afterCreate = {
      chPickerHidden: chPickerHidden(), agPickerHidden: agPickerHidden(),
      chName: chName(), agName: agName(),
      addChannelVisible: vis("cta-add-channel"),
      addAgentVisible: vis("cta-add-agent"),
    };
    // 再加第二个渠道：切换 + 名称跟随当前选择
    state.channels.push({ id: 31, name: "企业微信 2", type: "wecom_bot", status: "active" });
    await t.render(state.agents, state.channels, state.connections);
    els["channel-picker"].value = "31";
    els["channel-picker"].handlers.change();
    await settle();
    out.afterSwitch = { chName: chName(), chPickerHidden: chPickerHidden() };
    process.stdout.write(JSON.stringify(out));
    return;
  }

  if (scenario === "gating") {
    out.gating = {
      primaryHidden: !vis("cta-primary"),
      hint: els["cta-primary-hint"].textContent,
      hintVisible: vis("cta-primary-hint"),
    };
    // 只补 Agent 侧：渠道缺失时同样不给连接
    state.channels = [];
    await t.render(state.agents, state.channels, state.connections);
    out.emptyPickers = {
      chPicker: picker("channel-picker"), agPicker: picker("agent-picker"),
      chDisabled: els["channel-picker"].disabled,
      agDisabled: els["agent-picker"].disabled,
      chEmptyText: els["channel-picker"].textContent,
      agEmptyText: els["agent-picker"].textContent,
    };
    process.stdout.write(JSON.stringify(out));
    return;
  }

  if (scenario === "fail") {
    state.verifyReachable = false;
    calls.length = 0;
    els["cta-primary"].handlers.click();
    await settle();
    out.fail = {
      error: els["home-error"].textContent,
      detailVisible: vis("home-error-detail"),
      detailBody: els["home-error-detail-body"].textContent,
      badge: badge(),
      posts: postCalls(),
      navigations: navigations.slice(),
      primaryVisible: vis("cta-primary"),
    };
    process.stdout.write(JSON.stringify(out));
    return;
  }

  if (scenario === "scope") {
    // §十八：显式选择 Agent 后，Flow 只能是「这条渠道 ↔ 这个 Agent」
    els["agent-picker"].value = "2";
    els["agent-picker"].handlers.change();
    await settle();
    out.agentSwitched = {
      badge: badge(), hub: hub(), agName: agName(),
      verifyVisible: vis("cta-verify"),
    };
    els["agent-picker"].value = "1";
    els["agent-picker"].handlers.change();
    await settle();
    out.agentBack = { badge: badge(), agName: agName() };
    // §十九：切换到第二条渠道（没有自己的通路）→ 不得显示已接通
    els["channel-picker"].value = "11";
    els["channel-picker"].handlers.change();
    await settle();
    out.channelSwitched = {
      badge: badge(), hub: hub(), chName: chName(),
      chLink: chLink(), primaryVisible: vis("cta-primary"),
      verifyVisible: vis("cta-verify"),
      detail: detail(),
    };
    els["channel-picker"].value = "10";
    els["channel-picker"].handlers.change();
    await settle();
    out.channelBack = { badge: badge(), chName: chName() };
    out.navigations = navigations.slice();
    process.stdout.write(JSON.stringify(out));
    return;
  }

  if (scenario === "addchannel") {
    els["cta-add-channel"].handlers.click();
    out.panelNew = {
      channelPanel: vis("panel-channel"), agentPanel: vis("panel-agent"),
      title: els["panel-channel-title"].textContent,
      botId: els["pc-bot-id"].value,
      botSecret: els["pc-bot-secret"].value,
      type: els["pc-type"].value,
      navigations: navigations.slice(),
    };
    els["pc-bot-id"].value = "bot-two";
    els["pc-bot-secret"].value = "secret-two";
    calls.length = 0;
    els["pc-test"].handlers.click();
    await settle();
    const created = calls.find((c) => c.method === "POST"
      && c.url.indexOf("/api/channels") >= 0);
    out.created = {
      name: created ? created.payload.name : null,
      chPicker: picker("channel-picker"),
      chName: chName(),
      panelClosed: !vis("panel-channel"),
      title: els["panel-channel-title"].textContent,
      navigations: navigations.slice(),
    };
    process.stdout.write(JSON.stringify(out));
    return;
  }

  if (scenario === "addagent") {
    els["cta-add-agent"].handlers.click();
    out.panelNew = {
      agentPanel: vis("panel-agent"),
      title: els["panel-agent-title"].textContent,
      navigations: navigations.slice(),
    };
    els["pa-wb-agent-id"].value = "wb-agent-2";
    els["pa-wb-api-key"].value = "wb-key-2";
    calls.length = 0;
    els["pa-test"].handlers.click();
    await settle();
    const created = calls.find((c) => c.method === "POST"
      && c.url.indexOf("/api/agents") >= 0);
    out.created = {
      name: created ? created.payload.name : null,
      agPicker: picker("agent-picker"),
      agName: agName(),
      navigations: navigations.slice(),
    };
    process.stdout.write(JSON.stringify(out));
    return;
  }

  // scenario === "success"：§十四~§十六 就地连接成功
  els["cta-primary"].handlers.click();
  await settle();
  out.connected = {
    badge: badge(), hub: hub(), chName: chName(), agName: agName(),
    chLink: chLink(), agLink: agLink(),
    detail: detail(),
    verifyVisible: vis("cta-verify"),
    posts: postCalls(),
    navigations: navigations.slice(),
    firstSuccessClaimed: detail().indexOf("已成功送达") >= 0,
  };
  process.stdout.write(JSON.stringify(out));
})().catch(function (e) {
  process.stderr.write(String((e && e.stack) || e));
  process.exit(2);
});
"""

WECOM = {"id": 10, "name": "企业微信", "type": "wecom_bot", "status": "active"}
WECOM2 = {"id": 11, "name": "企业微信 2", "type": "wecom_bot", "status": "active"}
WECOM_OFF = {"id": 12, "name": "企业微信 停用", "type": "wecom_bot", "status": "inactive"}
WB = {"id": 1, "name": "WorkBuddy", "type": "acp", "status": "active"}
WB2 = {"id": 2, "name": "WorkBuddy 2", "type": "acp", "status": "active"}
CONN_WECOM_WB = {"id": 50, "channel_id": 10, "agent_id": 1, "status": "active"}


def _run(scenario: str, *, channels: list | None = None,
         agents: list | None = None, connections: list | None = None) -> dict:
    env = dict(os.environ)
    env["PROBE_JS"] = str(HOME_JS)
    env["PROBE_SCENARIO"] = scenario
    env["PROBE_CHANNELS"] = json.dumps([WECOM] if channels is None else channels)
    env["PROBE_AGENTS"] = json.dumps([WB] if agents is None else agents)
    env["PROBE_CONNECTIONS"] = json.dumps(
        [] if connections is None else connections)
    proc = subprocess.run(
        [_NODE, "-e", _JS_HARNESS], capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env, check=False)
    assert proc.returncode == 0, f"node 执行失败：{proc.stderr}"
    return json.loads(proc.stdout)


# ---------------------------------------------------------------- B. 默认名称（行为）

@needs_node
def test_b35_first_time_empty_state_shows_only_add_entries() -> None:
    """M7-AUI-10.4 需求②：第一次进来（无渠道 / Agent）→ 选择器隐藏，
    只显示「+ 添加渠道 / + 添加 Agent」；创建保存后选择器才出现并跟随当前选择。"""
    out = _run("firsttime", channels=[], agents=[], connections=[])
    empty = out["emptyState"]
    assert empty["chPickerHidden"] is True and empty["agPickerHidden"] is True
    assert empty["addChannelVisible"] is True and empty["addAgentVisible"] is True
    created = out["afterCreate"]
    assert created["chPickerHidden"] is False and created["agPickerHidden"] is False
    assert created["chName"] == "企业微信" and created["agName"] == "WorkBuddy"
    # 已有选择器时「+ 添加」仍然在（添加第二个渠道 / Agent）
    assert created["addChannelVisible"] is True and created["addAgentVisible"] is True
    switched = out["afterSwitch"]
    assert switched["chName"] == "企业微信 2"
    assert switched["chPickerHidden"] is False


@needs_node
def test_b36_panel_toggle_add_and_edit() -> None:
    """M7-AUI-10.4 需求③④：「+ 添加」再点收起；「编辑」打开后再点收起；
    编辑面板标题 =「编辑渠道 / 编辑 Agent」。"""
    out = _run("toggles")
    assert out["addChannelOpen"] == {"panel": True, "title": "添加渠道"}
    assert out["addChannelClosed"]["panel"] is False
    assert out["editChannelOpen"] == {"panel": True, "title": "编辑渠道"}
    assert out["editChannelClosed"]["panel"] is False
    assert out["addAgentOpen"] == {"panel": True, "title": "添加 Agent"}
    assert out["addAgentClosed"]["panel"] is False
    assert out["editAgentOpen"] == {"panel": True, "title": "编辑 Agent"}
    assert out["editAgentClosed"]["panel"] is False


@needs_node
def test_b4_default_name_matrix() -> None:
    """§十：base / base 2 / base 3；无冲突回落 base；空白与大小写按唯一规则折叠。"""
    names = _run("names")["names"]
    assert names["first"] == "企业微信"
    assert names["second"] == "企业微信 2"
    assert names["third"] == "企业微信 3"
    assert names["wbSecond"] == "WorkBuddy 2"
    assert names["ownSecond"] == "自建 Agent 2"
    assert names["caseInsensitive"] == "WorkBuddy 2"
    assert names["trimsBase"] == "企业微信 2"
    assert names["trimsExisting"] == "企业微信 2"
    assert names["gap"] == "企业微信 2"


# ---------------------------------------------------------------- A. 选择器（行为）

@needs_node
def test_a4_initial_render_reflects_current_channel_scoped_flow() -> None:
    """A1/A2/A3：当前渠道正确显示，Flow 状态按当前渠道计算（不含他渠道状态）。"""
    out = _run("success", channels=[WECOM], connections=[CONN_WECOM_WB])
    init = out["initial"]
    assert init["chPicker"] == "10"
    assert init["agPicker"] == "1"
    assert init["badge"] == "已接通" and init["hub"] == "已接通"
    assert init["chName"] == "企业微信" and init["agName"] == "WorkBuddy"
    assert "is-on" in init["chLink"] and "is-on" in init["agLink"]
    # 已接通 → 不给连接按钮（Flow 已建立）
    assert init["primaryVisible"] is False


@needs_node
def test_a5_switching_channel_recomputes_flow_scope() -> None:
    """A3/A4 + D21~D24：切到没有自己通路的渠道 → 不显示已接通；切回即恢复。"""
    out = _run("scope", channels=[WECOM, WECOM2],
               connections=[CONN_WECOM_WB])
    assert out["channelSwitched"]["badge"] != "已接通"
    assert out["channelSwitched"]["hub"] != "已接通"
    assert out["channelSwitched"]["chName"] == "企业微信 2"
    assert "is-on" not in out["channelSwitched"]["chLink"]
    assert out["channelSwitched"]["verifyVisible"] is False
    assert "已接通" not in out["channelSwitched"]["detail"]
    # 切回原渠道 → 已接通恢复
    assert out["channelBack"]["badge"] == "已接通"
    assert out["channelBack"]["chName"] == "企业微信"
    # 全程没有跳页
    assert out["navigations"] == []


@needs_node
def test_a6_agent_selection_scopes_the_flow_pair() -> None:
    """§十八：显式选 Agent 2 后，渠道 10 ↔ Agent 1 的通路不能把这对显示成已接通。"""
    out = _run("scope", channels=[WECOM, WECOM2], agents=[WB, WB2],
               connections=[CONN_WECOM_WB])
    assert out["agentSwitched"]["agName"] == "WorkBuddy 2"
    assert out["agentSwitched"]["badge"] != "已接通"
    assert out["agentSwitched"]["verifyVisible"] is False
    assert out["agentBack"]["badge"] == "已接通"
    assert out["agentBack"]["agName"] == "WorkBuddy"


# ---------------------------------------------------------------- C. 就地连接（行为）

@needs_node
def test_c4_inline_connect_success_stays_on_page_and_shows_connected() -> None:
    """C13/C15/C16/C17：两端 Ready → 可连接；不跳页；成功显示已接通、不显示 First Success。"""
    out = _run("success", channels=[WECOM], agents=[WB], connections=[])
    init = out["initial"]
    assert init["primaryVisible"] is True
    assert init["primaryText"] == "把它们连接起来"
    assert init["badge"] != "已接通"

    done = out["connected"]
    # 顺序铁律：建通路 → verify → bootstrap-arm（且只有这三个写请求）
    assert done["posts"] == ["/api/connections",
                             "/api/connections/5/verify",
                             "/api/connections/5/bootstrap-arm"], done["posts"]
    # 成功 → 当前页显示已接通（Channel ═══ SteeringFlow ═══ Agent）
    assert done["badge"] == "已接通" and done["hub"] == "已接通"
    assert done["chName"] == "企业微信" and done["agName"] == "WorkBuddy"
    assert "is-on" in done["chLink"] and "is-on" in done["agLink"]
    # §十六：下一步是「发一条消息试试」——不显示 First Success（还没有真实消息）
    assert "已连接成功" in done["detail"]
    assert "发送一条消息" in done["detail"]
    assert done["firstSuccessClaimed"] is False
    # 全程留在 home.html
    assert done["navigations"] == []


@needs_node
def test_c5_inline_connect_failure_stays_with_user_language() -> None:
    """C18/C19/C20：失败留在本页、用户语言在上、技术详情可展开。"""
    out = _run("fail", channels=[WECOM], agents=[WB], connections=[])
    fail = out["fail"]
    assert fail["error"] == "连接失败，请检查配置后重试。"
    assert fail["detailVisible"] is True
    assert fail["detailBody"], "技术详情不得为空（折叠展示）"
    # verify 未通过 → 绝不 bootstrap-arm；也不刷新成已接通
    assert fail["posts"] == ["/api/connections", "/api/connections/5/verify"]
    assert fail["badge"] != "已接通"
    assert fail["navigations"] == []
    # 失败不吞按钮：留在本页可重试
    assert fail["primaryVisible"] is True


@needs_node
def test_c6_not_ready_never_offers_connect() -> None:
    """C14 + §十三：Channel / Agent 未就绪 → 不能连接，原因就地说明。"""
    out = _run("gating", channels=[WECOM_OFF], agents=[WB])
    assert out["gating"]["primaryHidden"] is True
    assert "还没有就绪" in out["gating"]["hint"]
    assert out["gating"]["hintVisible"] is True
    empty = out["emptyPickers"]
    # 桩 DOM 不派生 select.textContent：空态用「不可选 + 值为空」判定
    assert empty["chDisabled"] is True and empty["chPicker"] == ""
    assert empty["agDisabled"] is False and empty["agPicker"] == "1"


# ---------------------------------------------------------------- A6/B：添加渠道 / 添加 Agent（行为）

@needs_node
def test_a7_add_channel_creates_and_selects_in_place() -> None:
    """A5/A6 + §五 + §七：不跳页打开面板；自动命名 企业微信 2；新渠道成为当前渠道。"""
    out = _run("addchannel", channels=[WECOM], agents=[WB], connections=[])
    panel = out["panelNew"]
    assert panel["channelPanel"] is True and panel["agentPanel"] is False
    assert panel["title"] == "添加渠道"
    assert panel["botId"] == "" and panel["botSecret"] == ""
    assert panel["navigations"] == []

    created = out["created"]
    assert created["name"] == "企业微信 2", "新渠道必须自动命名（不要求用户填写）"
    assert created["chPicker"] == "11", "新渠道必须成为当前选中的渠道"
    assert created["chName"] == "企业微信 2"
    assert created["panelClosed"] is True
    assert created["title"] == "编辑渠道"
    assert created["navigations"] == []


@needs_node
def test_a8_add_agent_creates_and_selects_in_place() -> None:
    """§八/§十一：Agent 名称自动生成（WorkBuddy 2）；新 Agent 成为当前 Agent。"""
    out = _run("addagent", channels=[WECOM], agents=[WB], connections=[])
    panel = out["panelNew"]
    assert panel["agentPanel"] is True
    assert panel["title"] == "添加 Agent"
    assert panel["navigations"] == []

    created = out["created"]
    assert created["name"] == "WorkBuddy 2"
    assert created["agPicker"] == "100", "新 Agent 必须成为当前 Agent"
    assert created["agName"] == "WorkBuddy 2"
    assert created["navigations"] == []


# ================================================================== E. 回归

def test_e1_home_still_only_uses_existing_endpoints() -> None:
    """§二十四：零新端点、零 DELETE/PUT；PATCH 仍只有渠道凭据一处。"""
    code = _js()
    referenced = set(re.findall(r'"/api/[a-z]+"', code))
    assert referenced == {'"/api/agents"', '"/api/channels"', '"/api/connections"',
                          '"/api/interactions"', '"/api/auth"'}, referenced
    for banned in ('"DELETE"', '"PUT"'):
        assert banned not in code
    assert code.count('"PATCH"') == 1


def test_e2_workbuddy_and_channel_panel_fields_untouched() -> None:
    """E25/E26/E27：WorkBuddy 接入与渠道凭据面板不受影响。"""
    html = _html()
    for marker in ("pc-bot-id", "pc-bot-secret", "pa-wb-agent-id", "pa-wb-api-key",
                   "pa-wb-redirect-uri", 'data-redirect-path="/oauth/callback"'):
        assert marker in html, f"Flow Home 缺少既有元素：{marker}"
    code = _js()
    for fn in ("testChannel", "testAgent", "syncChannelPanel",
               "ensureCarrierChannel", "ensureBinding", "checkFirstSuccess",
               "diagnoseFailure", "computeState", "findFlowForChannel"):
        assert f"function {fn}" in code or f"async function {fn}" in code, fn
    # 渠道面板「查看 / 更换」仍是编辑当前渠道（不回填到新建模式）
    body = code[code.index("function syncChannelPanel"):]
    body = body[:body.index("\n}")]
    assert 'channelPanelMode === "new"' in body


def test_e3_advanced_pages_and_connect_untouched_in_contract() -> None:
    """E28/E29：Advanced 页面与 connect.html 的既有契约标记仍在（未被本步波及）。"""
    for name, marker in (
        ("channels.html", 'id="channel-form"'),
        ("agents.html", 'id="agent-form"'),
        ("connections.html", 'id="connection-form"'),
        ("connect.html", 'id="workbuddy-block"'),
    ):
        assert marker in _read(FRONTEND / name), name
    # connect.html 仍是自己的 onboarding（带 Redirect URI 展示），未被重写
    connect = _read(CONNECT_HTML)
    assert 'data-redirect-path="/oauth/callback"' in connect
    assert 'id="btn-copy-redirect"' in connect


def test_e4_flow_home_graph_elements_still_present() -> None:
    """E：连接状态图骨架不变（节点 / 连接线 / 中心节点 / 两端动作）。"""
    html = _html()
    for marker in ("node-channel", "node-agent", "node-hub", "link-channel",
                   "link-agent", "cta-channel", "cta-agent", "flow-graph"):
        assert marker in html, marker
