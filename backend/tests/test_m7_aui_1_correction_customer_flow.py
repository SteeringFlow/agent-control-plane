"""M7-AUI-1-Correction —— Customer Flow / WeCom / Execution Order 纠偏验收。

冻结的产品决策（本文件锁死）：

1. 产品关系恒为 `Channel → SteeringFlow → Agent`；**它不是用户操作顺序**。
   ①② 各有独立动作，可只做一边、可任意顺序；③ 才把两端接成通路。
   页面里没有 disabled / 隐藏前置 / 步骤校验 / wizard guard。
2. 企业微信默认客户路径 = **Bot + 长连接**（`wecom_bot` = Bot ID + Bot Secret）。
   HTTP 回调 / 企业应用模式（`wecom`）属高级接入，其字段
   （企业 ID / Token / EncodingAESKey / 应用 Secret / 应用 AgentId / Webhook 配置）
   不得出现在默认 Customer Surface。
3. WorkBuddy Agent 可独立验证：`Connect → Agent → WorkBuddy → Agent ID + API Key → 验证`，
   不要求用户配置渠道。
4. Flow Home 允许「只有渠道」「只有 Agent」「两者都有」「都没有」四种合法状态。

方法：静态源码断言（**先剥离注释**，与既有 guard 同纪律）+ Node `vm` 行为断言
（缺 Node 自动 skip），全部只读，不触 DB / 不触网。
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
CONNECT_HTML = FRONTEND / "connect.html"
CONNECT_JS = FRONTEND / "connect.js"
HOME_JS = FRONTEND / "home.js"

_NODE = shutil.which("node")
needs_node = pytest.mark.skipif(
    _NODE is None, reason="node 不可用（行为级验收需要 Node）")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _strip_js_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    src = re.sub(r"//[^\n]*", " ", src)
    return src


def _strip_html_comments(src: str) -> str:
    return re.sub(r"<!--.*?-->", " ", src, flags=re.S)


def _connect_html() -> str:
    """用户可见面（剥离 HTML 注释——注释里说明「不做」的术语不算暴露）。"""
    return _strip_html_comments(_read(CONNECT_HTML))


def _connect_js() -> str:
    return _strip_js_comments(_read(CONNECT_JS))


def _function_body(code: str, name: str) -> str:
    """取出 `async function <name>...` 到下一个顶层 `function`/`async function` 之间的源码。"""
    start = code.index(f"function {name}")
    match = re.search(r"\n(?:async )?function ", code[start + 1:])
    end = start + 1 + match.start() if match else len(code)
    return code[start:end]


# ================================================================== A. WeCom Customer Surface

def test_a1_wecom_default_surface_is_bot_id_and_secret() -> None:
    """A：默认企业微信路径只要求 Bot ID + Bot Secret（wecom_bot 长连接）。"""
    html = _read(CONNECT_HTML)
    assert 'id="f-bot-id"' in html
    assert 'id="f-bot-secret"' in html
    assert 'value="wecom_bot"' in html
    assert "Bot ID" in html and "Bot Secret" in html


def test_a2_legacy_webhook_fields_absent_from_customer_surface() -> None:
    """A：HTTP 回调 / 企业应用模式的字段不得出现在 Connect 的默认面。"""
    html = _connect_html()
    for field_id in ('id="f-corp-id"', 'id="f-token"', 'id="f-aes-key"',
                     'id="f-corpsecret"', 'id="f-agentid"'):
        assert field_id not in html, f"Connect 页出现 legacy webhook 字段：{field_id}"
    for label in ("企业 ID", "回调 Token", "EncodingAESKey", "应用 Secret", "应用 AgentId",
                  "Webhook URL", "Webhook Token"):
        assert label not in html, f"Connect 页出现 legacy webhook 标签：{label}"


def test_a3_legacy_mode_points_at_advanced_entry() -> None:
    """A：另一种企业微信接入模式只留客户语言的指路，不铺字段。

    M7-AUI-8.2：客户面不再出现内部平台术语（`Advanced` / `Adapter Registry`）——
    指路改为「可在『高级 → 渠道接入』里配置」。"""
    html = _connect_html()
    assert "如需其它企业微信接入方式" in html
    assert "渠道接入" in html
    assert "Advanced" not in html, "Connect 客户面不应出现内部平台术语 Advanced"
    assert "Adapter Registry" not in html


def test_a4_connect_js_builds_bot_channel_config_only() -> None:
    """A：渠道 config 只装 bot_id / bot_secret；legacy 键不再出现。"""
    code = _connect_js()
    assert 'bot_id' in code and 'bot_secret' in code
    for legacy in ("corp_id", "encoding_aes_key", "corpsecret", "agentid"):
        assert legacy not in code, f"connect.js 仍构造 legacy 渠道键：{legacy}"


# ================================================================== B. 无步骤锁

def test_b1_three_regions_are_independent_and_visible() -> None:
    """B：三个区域都在 DOM 中，且没有任何一个带 hidden（不是强制顺序）。"""
    html = _connect_html()
    for region in ('id="step-1"', 'id="step-2"', 'id="step-3"'):
        assert region in html, f"缺少区域：{region}"
        assert re.search(r'<div id="step-\d"[^>]*class="[^"]*hidden', html) is None, \
            "区域不得因前置步骤未完成而隐藏"
    assert "disabled" not in html, "Connect 页不得用 disabled 做步骤锁"
    # 三个区域的动作按钮**始终可见**（结果区的重试 / 检查按钮是结果态的可选项，不在此列）
    for action_id in ("btn-save-channel", "btn-save-agent", "btn-verify-agent", "btn-connect"):
        tag = re.search(r'<button[^>]*id="%s"[^>]*>' % action_id, html)
        assert tag is not None, f"缺少区域动作按钮：{action_id}"
        assert "hidden" not in tag.group(0), f"{action_id} 不得默认隐藏"
        assert "disabled" not in tag.group(0), f"{action_id} 不得默认禁用"


def test_b2_each_region_has_its_own_action() -> None:
    """B：① 有自己的动作，② 有自己的动作，③ 才接通。"""
    html = _connect_html()
    assert 'id="btn-save-channel"' in html       # ① 保存渠道
    assert 'id="btn-save-agent"' in html         # ② 保存 Agent
    assert 'id="btn-verify-agent"' in html       # ② 验证 Agent
    assert 'id="btn-connect"' in html            # ③ 接通


def test_b3_region_chips_are_entry_points_not_locks() -> None:
    """B：chip 只是区域入口（点击跳转），不 disabled、不隐藏。"""
    html = _connect_html()
    for chip in ('id="chip-1"', 'id="chip-2"', 'id="chip-3"'):
        assert chip in html
    code = _connect_js()
    assert 'data-region="step-1"' in html and 'data-region="step-2"' in html
    assert "wireRegionChip" in code
    assert "scrollIntoView" in code


def test_b4_no_wizard_guard_machinery() -> None:
    """B：不引入步骤校验 / wizard 机制。"""
    code = (_connect_js() + _read(HOME_JS)).lower()
    for banned in ("steplock", "step_lock", "wizard", "guardstep", "requirestep"):
        assert banned not in code, f"出现步骤锁机制：{banned}"


# ================================================================== C. Agent 独立路径

def test_c1_save_agent_does_not_touch_channels_or_connections() -> None:
    """C：② 保存 Agent 只走 Agent 端点（委托 ensureAgent），不需要渠道。"""
    code = _connect_js()
    body = _function_body(code, "saveAgentOnly")
    assert "ensureAgent" in body, "② 保存 Agent 必须走 Agent 就绪入口"
    for forbidden in ("CHANNELS_API", "CONNECTIONS_API"):
        assert forbidden not in body, f"② 保存 Agent 触碰了 {forbidden}"

    # Agent 端点只在 ensureAgent 里被触碰（单一事实来源）
    agent_body = _function_body(code, "ensureAgent")
    assert "AGENTS_API" in agent_body
    for forbidden in ("CHANNELS_API", "CONNECTIONS_API"):
        assert forbidden not in agent_body, f"ensureAgent 触碰了 {forbidden}"


def test_c2_verify_agent_does_not_require_channel_input() -> None:
    """C：② 验证 Agent 不读渠道表单字段（用户不需要填渠道）。"""
    body = _function_body(_connect_js(), "verifyAgent")
    for field in ("fBotId", "fBotSecret", "fChannelType"):
        assert field not in body, f"② 验证 Agent 依赖渠道输入：{field}"
    assert "verify" in body
    assert "bootstrap-arm" not in body, "验证不应启用（启用属 ③）"


def test_c3_save_channel_does_not_touch_agents_or_connections() -> None:
    """C：① 保存渠道只走渠道端点（委托 buildChannelPayload），不需要 Agent。"""
    code = _connect_js()
    body = _function_body(code, "saveChannelOnly")
    assert "buildChannelPayload" in body
    for forbidden in ("AGENTS_API", "CONNECTIONS_API"):
        assert forbidden not in body, f"① 保存渠道触碰了 {forbidden}"

    # Bot ID / Bot Secret 经 buildChannelConfig 读取（渠道配置的单一事实来源）
    payload_body = _function_body(code, "buildChannelPayload")
    assert "buildChannelConfig" in payload_body
    config_body = _function_body(code, "buildChannelConfig")
    assert "fBotId" in config_body and "fBotSecret" in config_body
    assert config_body.count("wecom_bot") >= 1


def test_c4_provider_gates_its_own_fields() -> None:
    """C：Provider 仍决定自己必需的字段（WorkBuddy = Agent ID + API Key）。"""
    code = _connect_js()
    assert "const PROVIDERS = {" in code
    assert 'workbuddy_agent_id' in code
    html = _connect_html()
    assert 'id="workbuddy-block"' in html and 'id="workbuddy-key-block"' in html
    assert "WorkBuddy Agent ID" in html


# ================================================================== D. Flow Home 允许只有一边

def test_d1_home_partial_states_are_legal_and_named() -> None:
    """D：Flow Home 把「只有渠道」「只有 Agent」当作合法中间状态，只说明还差哪一端。"""
    code = _strip_js_comments(_read(HOME_JS))
    assert "已就绪，还差一个 Agent" in code
    assert "已就绪，还差一条渠道" in code
    # 三节点与五态模型未被重设计
    assert "renderConnecting" in code
    assert "computeState" in code


# ================================================================== E. Node 行为验收

_JS_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const src = fs.readFileSync(process.env.PROBE_JS, "utf8").replace(/^import\s[^;]*;/gm, "");

const els = {};
function fakeEl(tag) {
  const classes = new Set();
  return {
    tag: tag || "div", children: [],
    classList: {
      add(c) { classes.add(c); },
      remove(c) { classes.delete(c); },
      toggle(c, on) {
        if (on === undefined) { classes.has(c) ? classes.delete(c) : classes.add(c); }
        else if (on) { classes.add(c); } else { classes.delete(c); }
      },
      contains(c) { return classes.has(c); },
    },
    textContent: "", innerHTML: "", href: "", value: "",
    open: false, parentNode: null,
    appendChild(child) { this.children.push(child); child.parentNode = this; return child; },
    addEventListener() {}, focus() {},
  };
}
function el(id) { if (!els[id]) els[id] = fakeEl(); return els[id]; }

const calls = [];
function body(url, method, payload) {
  if (method === "POST" && url === "/api/agents") {
    return { id: 501, name: (payload && payload.name) || "agent", type: "acp", status: "active" };
  }
  if (method === "POST" && url === "/api/channels") {
    return { id: 77, name: (payload && payload.name) || "ch",
             type: (payload && payload.type) || "local", status: "active" };
  }
  if (method === "POST" && url === "/api/connections") {
    return { id: 88, channel_id: payload.channel_id, agent_id: payload.agent_id,
             name: payload.name, status: "active", config: payload.config || null };
  }
  if (method === "POST" && /\/api\/connections\/\d+\/verify$/.test(url)) {
    return { connection_id: 88, reachable: true, checked_at: "2026-09-22T00:00:00Z",
             detail: null, probe: "tencent-acp-sse" };
  }
  return { items: [] };
}
const sandbox = {
  console: console,
  document: { getElementById: el, createElement: (tag) => fakeEl(tag) },
  window: { location: { origin: "https://app.example.test", replace() {} } },
  fetch: function (url, options) {
    const method = (options && options.method) || "GET";
    const payload = options && options.body ? JSON.parse(options.body) : null;
    calls.push({ method: method, url: String(url), payload: payload });
    return Promise.resolve({
      ok: true, status: 200,
      json: () => Promise.resolve(body(String(url), method, payload)),
    });
  },
};
vm.createContext(sandbox);
const sharedPath = process.env.PROBE_JS.replace(/[^/\\]+$/, "") + "shared/http.js";
const sharedSrc = fs.readFileSync(sharedPath, "utf8").replace(/^export /gm, "");
vm.runInContext(sharedSrc, sandbox);
vm.runInContext(src + "\n;globalThis.__t = { verifyAgent: verifyAgent, "
  + "saveChannelOnly: saveChannelOnly, saveAgentOnly: saveAgentOnly };", sandbox);

const t = sandbox.__t;
const flush = () => new Promise((r) => setTimeout(r, 0));

(async function () {
  await flush(); await flush();
  calls.length = 0;

  // 场景 B：只配置 WeCom Bot（不需要 Agent）
  els["f-channel-type"].value = "wecom_bot";
  els["f-bot-id"].value = "bot-123";
  els["f-bot-secret"].value = "bot-secret-xyz";
  await t.saveChannelOnly();
  const scenarioB = calls.splice(0, calls.length);

  // 场景 A：只测试 WorkBuddy Agent（零渠道租户）
  vm.runInContext("ref.channels.length = 0; reuseChannelId = null; savedAgentId = null;", sandbox);
  els["f-agent-provider"].value = "workbuddy";
  vm.runInContext("syncProvider();", sandbox);
  els["f-agent-mode"].value = "new";
  els["f-agent-name"].value = "workbuddy-fresh";
  els["f-wb-agent-id"].value = "wb-agent-9";
  els["f-wb-api-key"].value = "wb-key-9";
  await t.verifyAgent();
  const scenarioA = calls.splice(0, calls.length);

  // 文案收口（M7-AUI-7 延伸）：WorkBuddy / 未选择不显示提示；自建 Agent 保留非显而易见的一句
  const hintState = (id) => ({
    text: els[id].textContent,
    hidden: els[id].classList.contains("hidden"),
  });
  const hints = { workbuddyProvider: hintState("provider-hint") };
  els["f-agent-provider"].value = "own";
  vm.runInContext("syncProvider();", sandbox);
  hints.ownProvider = hintState("provider-hint");
  els["f-channel-type"].value = "wecom_bot";
  vm.runInContext("syncChannelType();", sandbox);
  hints.botChannel = hintState("channel-type-hint");
  els["f-channel-type"].value = "local";
  vm.runInContext("syncChannelType();", sandbox);
  hints.localChannel = hintState("channel-type-hint");

  process.stdout.write(JSON.stringify({
    scenarioB: scenarioB,
    scenarioA: scenarioA,
    resultDetail: els["result-detail"].textContent,
    hints: hints,
  }));
})().catch(function (e) {
  process.stderr.write(String((e && e.stack) || e));
  process.exit(2);
});
"""


def _run_probe() -> dict:
    env = dict(os.environ)
    env["PROBE_JS"] = str(CONNECT_JS)
    proc = subprocess.run(
        [_NODE, "-e", _JS_HARNESS], capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env, check=False)
    assert proc.returncode == 0, f"node 执行失败：{proc.stderr}"
    return json.loads(proc.stdout)


@needs_node
def test_e1_scenario_b_channel_only_needs_no_agent() -> None:
    """场景 B（真实执行）：只配置 WeCom Bot 成功，且完全不触碰 Agent / 通路。"""
    out = _run_probe()
    calls = out["scenarioB"]
    assert [c["url"] for c in calls] == ["/api/channels"], calls
    payload = calls[0]["payload"]
    assert payload["type"] == "wecom_bot"
    assert payload["config"] == {"bot_id": "bot-123", "bot_secret": "bot-secret-xyz"}


@needs_node
def test_e2_scenario_a_agent_only_verify_without_channel() -> None:
    """场景 A（真实执行）：零渠道租户也能验证 WorkBuddy Agent。

    调用序列 = Agent →（自检）/api/channels（local）→ 通路 → /verify；
    绝不会因为「没有渠道」而停下来要求用户先配置渠道。
    """
    out = _run_probe()
    urls = [c["url"] for c in out["scenarioA"]]
    assert "/api/agents" in urls
    assert "/api/channels" in urls
    assert "/api/connections" in urls
    assert any(u.endswith("/verify") for u in urls), urls
    assert not any("bootstrap-arm" in u for u in urls), "验证不得启用连接"
    carrier = next(c for c in out["scenarioA"] if c["url"] == "/api/channels")
    assert carrier["payload"]["type"] == "local", "自检通路必须是无凭据渠道"
    agent_call = next(c for c in out["scenarioA"] if c["url"] == "/api/agents")
    assert "api_key" not in agent_call["payload"], "凭据不得写进 Agent"
    assert "已验证可达" in out["resultDetail"] or "验证通过" in out["resultDetail"]


# ================================================================== F. 文案收口（M7-AUI-7 延伸）
#
# 与 Flow Home 同一原则：**字段标签已经说明要填什么**，页面不再重复解释；
# 只在非显而易见时给一句话（自建 Agent 无需字段、本地测试渠道无需凭据）。


def test_f1_connect_page_drops_the_redundant_provider_hint() -> None:
    """WorkBuddy 分支不再重复「只需要下面两项；其余由 SteeringFlow 自动准备」。"""
    code = _connect_js()
    assert "只需要下面两项" not in code
    assert "其余由 SteeringFlow 自动准备" not in code


def test_f2_connect_page_drops_the_redundant_channel_hint() -> None:
    """企业微信分支不再重复「只需要 Bot ID 与 Bot Secret；消息经长连接订阅收发」。"""
    code = _connect_js()
    assert "只需要 Bot ID 与 Bot Secret" not in code
    assert "消息经长连接" not in code


def test_f3_no_premature_pledge_hints() -> None:
    """「选择后会只显示该来源 / 该类型必需的字段」这类提前承诺也一并去掉。"""
    assert "只显示该来源必需的字段" not in _connect_js()
    assert "只显示该来源必需的字段" not in _connect_html()
    assert "只显示该类型必需的字段" not in _connect_js()


def test_f6_connect_page_drops_redundant_select_labels() -> None:
    """区域标题「① 渠道」「② Agent」已说明这是什么：不再重复可见标签（只留 aria-label）。"""
    html = _connect_html()
    assert re.search(r'<label[^>]*for="f-channel-type"', html) is None, "重复标签必须删除"
    assert re.search(r'<label[^>]*for="f-agent-provider"', html) is None, "重复标签必须删除"
    visible = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).lower()
    assert "渠道类型" not in visible
    assert "agent 来自哪里" not in visible
    # 无障碍名保留（屏幕阅读器仍读得出这两个下拉框是什么）
    assert 'aria-label="渠道类型"' in html
    assert 'aria-label="Agent 来自哪里"' in html


def test_f4_non_obvious_hints_survive_and_empty_ones_do_not_take_space() -> None:
    """保留真正影响判断的两句；空提示连元素一起隐藏（不留空隙）。"""
    code = _connect_js()
    assert "无需额外字段" in code, "自建 Agent 的说明必须保留"
    assert "仅用于自检" in code, "本地测试渠道的说明必须保留"
    assert "setHint" in code and 'toggle("hidden", !text)' in code


@needs_node
def test_f5_hint_visibility_behaviour() -> None:
    """行为：WorkBuddy / 企业微信不留提示；自建 Agent / 本地测试渠道仍有说明。"""
    hints = _run_probe()["hints"]
    assert hints["workbuddyProvider"]["text"] == "", hints["workbuddyProvider"]
    assert hints["workbuddyProvider"]["hidden"] is True
    assert hints["botChannel"]["text"] == "", hints["botChannel"]
    assert hints["botChannel"]["hidden"] is True

    assert "无需额外字段" in hints["ownProvider"]["text"]
    assert hints["ownProvider"]["hidden"] is False
    assert "仅用于自检" in hints["localChannel"]["text"]
    assert hints["localChannel"]["hidden"] is False
