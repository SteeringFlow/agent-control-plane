"""M7-AUI-3 —— Flow Home 作为核心操作界面（原地接入）验收门。

冻结的产品决策（本文件锁死）：

1. **首次接入不跳出 Home**：`[连接渠道]` / `[连接 Agent]` 是**原地展开**的按钮
   （不是跳 `/connect.html` 的链接）；两端各自独立、无 ① ② ③ 步骤编号、无 step lock。
2. Home 保持轻量：只显示 Provider 真正需要的字段（渠道 = Bot ID + Bot Secret；
   WorkBuddy = Agent ID + API Key），绝不暴露 Type / Endpoint / Credential Ref /
   Config / 内部 ID。
3. `[测试连接]` 就地反馈：失败状态贴在**对应端点**（面板内的错误 + 该侧连接线失败态），
   不是只在页面顶部给一段文字。
4. 零后端改动：只复用既有端点（含既有 verify / bootstrap-arm），**不新增 API**；
   测试顺序铁律不变（先 verify，通过后才启用）。
5. 不新增 Provider：只复用仓库已支持的渠道类型与 Agent 形态。

方法：静态源码断言 + Node `vm` 行为断言（缺 Node 自动 skip），只读、不触 DB / 不触网。
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


def _function_body(code: str, name: str) -> str:
    start = code.index(f"function {name}")
    match = re.search(r"\n(?:async )?function ", code[start + 1:])
    end = start + 1 + match.start() if match else len(code)
    return code[start:end]


# ================================================================== A. 结构 / 边界

def test_a1_endpoints_expand_in_place_not_navigate() -> None:
    """§五/§六：两端动作是按钮（原地展开），不是跳页链接。"""
    html = _html()
    for cta in ("cta-channel", "cta-agent"):
        tag = re.search(r"<[a-z]+[^>]*id=\"%s\"[^>]*>" % cta, html)
        assert tag is not None, f"缺少端点动作：{cta}"
        assert tag.group(0).startswith("<button"), f"{cta} 必须是按钮（不跳页）：{tag.group(0)}"
        assert "href" not in tag.group(0), f"{cta} 不应带跳转 href"
    # Connect 页面保留，但 Home 的普通用户路径不再依赖它
    assert CONNECT_HTML.is_file(), "/connect.html 必须保留（Advanced / 兼容入口）"


def test_a2_inline_panels_exist_and_start_collapsed() -> None:
    """§七：两个原地面板存在，且默认都不展开（不铺两个大表单）。"""
    html = _html()
    for panel in ("panel-channel", "panel-agent"):
        tag = re.search(r"<div[^>]*id=\"%s\"[^>]*>" % panel, html)
        assert tag is not None, f"缺少面板：{panel}"
        assert "hidden" in tag.group(0), f"{panel} 必须默认收起"
    for marker in ("pc-test", "pa-test"):  # 两端各自的「测试连接」
        assert f'id="{marker}"' in html, f"缺少测试动作：{marker}"


def test_a3_only_provider_native_fields_on_the_surface() -> None:
    """§七/§八：Provider-native 字段 = 渠道 Bot ID/Secret、WorkBuddy Agent ID/API Key。"""
    html = _html()
    for field_id in ("pc-bot-id", "pc-bot-secret", "pa-wb-agent-id", "pa-wb-api-key"):
        assert f'id="{field_id}"' in html, f"缺少 Provider 字段：{field_id}"
    # 未选 Provider 时不显示 WorkBuddy 专属字段
    assert "hidden" in re.search(r'<div[^>]*id="pa-wb-block"[^>]*>', html).group(0)
    # 绝不暴露内部形态
    section = html[html.index('id="home-section"'):]
    lowered = section.lower()
    for banned in ("endpoint", "credential", "config", "runtime", "acp",
                   "connection id", "identity", "session", "bootstrap"):
        assert banned not in lowered, f"Home 主表面暴露内部形态：{banned}"


def test_a4_no_step_numbers_on_home() -> None:
    """§一/§十 最高优先级：关系模型 ≠ 操作顺序（无步骤编号）。"""
    html = _html()
    for banned in ("①", "②", "③", "第一步", "第二步"):
        assert banned not in html, f"Flow Home 出现步骤化语言：{banned}"


def test_a5_connect_page_kept_as_compat_entry() -> None:
    """§十七 + M7-AUI-9：connect.html 保留为独立接入 / 恢复入口；
    但 Home 的「把它们连接起来」改为**本页就地连接**——按钮不再跳转 connect.html，
    普通用户路径也不再引用它。"""
    assert CONNECT_HTML.is_file(), "/connect.html 必须保留（Advanced / 兼容入口）"
    html = _html()
    tag = re.search(r"<button[^>]*id=\"cta-primary\"[^>]*>", html)
    assert tag is not None, "「把它们连接起来」必须是原位按钮（M7-AUI-9 就地连接）"
    assert "href" not in tag.group(0), f"连接按钮不得带跳转 href：{tag.group(0)}"
    assert 'href="/connect.html"' not in html, "Home 普通路径不得再跳转 connect.html"


# ================================================================== B. 复用与顺序

def test_b1_no_new_endpoints_and_no_new_providers() -> None:
    """§十五/§十六：不新增 API、不新增 Provider、无写放大。

    M7-AUI-6 范围更新：Flow Home 允许对 **Channel 凭据** 做 PATCH
    （后端既有 `PATCH /api/channels/{id}`，config 为 merge 语义）；
    仍禁止 PUT / DELETE，也禁止编辑 Agent / Connection。"""
    code = _js()
    referenced = set(re.findall(r'"/api/[a-z]+"', code))
    assert referenced == {'"/api/agents"', '"/api/channels"', '"/api/connections"',
                          '"/api/interactions"', '"/api/auth"'}, referenced
    for banned in ('"DELETE"', '"PUT"'):
        assert banned not in code, f"Home 出现 {banned}"
    assert code.count('"PATCH"') == 1, "PATCH 只允许出现一次（渠道凭据更新）"
    at = code.index('"PATCH"')
    assert "CHANNELS_API" in code[max(0, at - 400):at + 400], \
        "PATCH 只能作用于 channels 资源"
    assert "PROVIDERS = {" in code
    for provider in ("workbuddy", "own"):
        assert f"{provider}:" in code
    # 渠道类型只复用既有两种
    html = _html()
    assert 'value="wecom_bot"' in html and 'value="local"' in html


def test_b2_test_connection_is_verify_then_arm() -> None:
    """§九 + 顺序铁律：测试连接 = 先 verify（真实探针），通过后才启用。"""
    body = _function_body(_js(), "testAgent")
    assert "/verify" in body and "bootstrap-arm" in body
    assert body.index("/verify") < body.index("bootstrap-arm"), \
        "必须先验证再启用"


def test_b3_channel_test_does_not_touch_agents_or_paths() -> None:
    """§五：渠道侧「测试连接」只保存 / 更新渠道（不需要 Agent、不建通路）。"""
    code = _js()
    body = (_function_body(code, "testChannel")
            + _function_body(code, "buildChannelPayload"))
    assert "CHANNELS_API" in body
    for forbidden in ("AGENTS_API", "CONNECTIONS_API"):
        assert forbidden not in body, f"渠道测试触碰了 {forbidden}"


def test_b4_failure_is_expressed_at_the_endpoint() -> None:
    """§四/§九：失败就地表达（该侧连接线失败态 + 面板内错误），不是只在顶部。"""
    code = _js()
    assert "markSideFailed" in code
    for fn in ("testChannel", "testAgent"):
        body = _function_body(code, fn)
        assert "is-error" in body or "markSideFailed" in body, f"{fn} 没有就地失败态"
        assert "pcError" in body or "paError" in body, f"{fn} 错误没有贴到端点面板"


def test_b5_home_still_reuses_the_existing_state_model() -> None:
    """§十五：状态模型不重新发明（五态 + computeState + diagnoseFailure 仍在）。"""
    code = _js()
    for state in ('"empty"', '"partial"', '"connected"', '"error"'):
        assert state in code
    assert "function computeState" in code and "function diagnoseFailure" in code
    for invented in ("FlowStatus", "ChannelConnectionStatus", "AgentConnectionStatus"):
        assert invented not in code, f"发明了新的状态模型：{invented}"


# ================================================================== C. Node 行为

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
      contains(c) { return classes.has(c); },
    },
    textContent: "", className: "", href: "", value: "", disabled: false,
    addEventListener() {}, focus() {}, appendChild() {},
  };
}
function el(id) { if (!els[id]) els[id] = fakeEl(); return els[id]; }

["panel-channel", "panel-agent", "pa-wb-block", "cta-verify",
 "cta-primary", "flow-badge", "home-error", "pc-error", "pa-error"].forEach(function (id) {
  el(id).classList.add("hidden");
});

const redirects = [];
const sandbox = {
  console: console,
  document: { getElementById: el, createElement: () => fakeEl() },
  window: { location: { replace(u) { redirects.push(u); } } },
  fetch: function () {
    return Promise.resolve({ ok: false, status: 401, json: () => Promise.resolve({}) });
  },
};
vm.createContext(sandbox);
const sharedPath = process.env.PROBE_JS.replace(/[^/\\]+$/, "") + "shared/http.js";
const sharedSrc = fs.readFileSync(sharedPath, "utf8").replace(/^export /gm, "");
vm.runInContext(sharedSrc, sandbox);
vm.runInContext(src + "\n;globalThis.__t = { render: render, togglePanel: togglePanel, "
  + "syncAgentProvider: syncAgentProvider, syncChannelType: syncChannelType, "
  + "testChannel: testChannel, testAgent: testAgent };", sandbox);

(async function () {
  await new Promise((r) => setTimeout(r, 0));
  const t = sandbox.__t;
  const visible = (id) => !els[id].classList.contains("hidden");
  const out = {};

  // 点渠道 → 渠道面板展开、Agent 面板收起；全程无跳转
  redirects.length = 0;
  t.togglePanel("channel");
  out.openChannel = { channel: visible("panel-channel"), agent: visible("panel-agent"),
                      redirects: redirects.slice() };
  t.togglePanel("agent");
  out.openAgent = { channel: visible("panel-channel"), agent: visible("panel-agent"),
                    redirects: redirects.slice() };
  t.togglePanel("agent"); // 再点一次收起
  out.toggleOff = { channel: visible("panel-channel"), agent: visible("panel-agent") };

  // Provider 门控：空值兜底（UI 已无「请选择」选项，默认 WorkBuddy；此处守卫
  // value 异常时也不显示 WorkBuddy 字段）
  els["pa-provider"].value = "";
  t.syncAgentProvider();
  out.providerNone = { wbBlock: visible("pa-wb-block") };
  els["pa-provider"].value = "workbuddy";
  t.syncAgentProvider();
  out.providerWorkbuddy = { wbBlock: visible("pa-wb-block") };

  // 渠道类型门控：企业微信显示 Bot 字段；本地测试渠道不显示
  els["pc-type"].value = "wecom_bot";
  t.syncChannelType();
  out.typeBot = { botBlock: visible("pc-bot-block") };
  els["pc-type"].value = "local";
  t.syncChannelType();
  out.typeLocal = { botBlock: visible("pc-bot-block") };

  // 渲染不会把展开态关掉（用户输入不丢）
  t.togglePanel("channel");
  const beforeRender = visible("panel-channel");
  t.render([], [], []);
  out.renderKeepsPanel = { before: beforeRender, after: visible("panel-channel") };

  // 空状态：两端都可点、两侧都没连上
  out.empty = { channel: els["link-channel"].className, agent: els["link-agent"].className,
                ctaChannel: els["cta-channel"].textContent,
                ctaAgent: els["cta-agent"].textContent };

  // 只有渠道对象、无通路 → 已准备（不是已连接）
  const CH = { id: 1, name: "bot-1", type: "wecom_bot", status: "active" };
  const AG = { id: 2, name: "agent-1", type: "acp", status: "active" };
  t.render([], [CH], []);
  out.channelReady = { link: els["link-channel"].className };
  t.render([], [CH], [{ id: 5, channel_id: 1, agent_id: 99, status: "active" }]);
  out.channelLinked = { link: els["link-channel"].className };
  t.render([AG], [CH], [{ id: 9, channel_id: 1, agent_id: 2, status: "disabled" }]);
  out.broken = { channel: els["link-channel"].className, hub: els["node-hub"].className };
  t.render([AG], [CH], [{ id: 7, channel_id: 1, agent_id: 2, status: "active" }]);
  out.connected = { channel: els["link-channel"].className,
                    agent: els["link-agent"].className,
                    verify: visible("cta-verify") };

  process.stdout.write(JSON.stringify(out));
})().catch(function (e) {
  process.stderr.write(String((e && e.stack) || e));
  process.exit(2);
});
"""


def _run_probe() -> dict:
    env = dict(os.environ)
    env["PROBE_JS"] = str(HOME_JS)
    proc = subprocess.run(
        [_NODE, "-e", _JS_HARNESS], capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env, check=False)
    assert proc.returncode == 0, f"node 执行失败：{proc.stderr}"
    return json.loads(proc.stdout)


@needs_node
def test_c1_panels_are_exclusive_and_never_navigate() -> None:
    """真实执行：点哪一端展开哪一侧（互斥），且**没有任何跳转**。"""
    out = _run_probe()
    assert out["openChannel"]["channel"] is True
    assert out["openChannel"]["agent"] is False
    assert out["openAgent"]["channel"] is False
    assert out["openAgent"]["agent"] is True
    assert out["openChannel"]["redirects"] == [] and out["openAgent"]["redirects"] == []
    assert out["toggleOff"]["channel"] is False


@needs_node
def test_c2_provider_gates_its_own_fields() -> None:
    """真实执行：默认 WorkBuddy 显示其字段；空值兜底不显示；切自建即收起。"""
    out = _run_probe()
    assert out["providerNone"]["wbBlock"] is False
    assert out["providerWorkbuddy"]["wbBlock"] is True
    assert out["typeBot"]["botBlock"] is True
    assert out["typeLocal"]["botBlock"] is False


@needs_node
def test_c3_inline_state_matrix() -> None:
    """真实执行：空 → 灰；已准备 → is-ready；已建立 → is-on；停用 → is-error。"""
    out = _run_probe()
    assert "is-off" in out["empty"]["channel"] and "is-off" in out["empty"]["agent"]
    assert out["empty"]["ctaChannel"] == "连接渠道"
    assert out["empty"]["ctaAgent"] == "连接 Agent"
    assert "is-ready" in out["channelReady"]["link"]
    assert "is-on" in out["channelLinked"]["link"]
    assert "is-error" in out["broken"]["channel"]
    assert "is-on" in out["connected"]["channel"] and "is-on" in out["connected"]["agent"]
    assert out["connected"]["verify"] is True


@needs_node
def test_c4_render_does_not_close_the_open_panel() -> None:
    """原地更新（refresh → render）不会把用户正在填的面板关掉。"""
    out = _run_probe()
    assert out["renderKeepsPanel"]["before"] is True
    assert out["renderKeepsPanel"]["after"] is True
