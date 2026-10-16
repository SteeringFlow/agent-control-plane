"""M7-AUI-2 —— Flow Home 连接状态图验收门。

冻结的产品决策（本文件锁死）：

1. `Channel → SteeringFlow → Agent` 是**产品关系模型，不是操作顺序**：
   两端各自独立可连（先渠道 / 先 Agent 都合法），无 ① ② ③ 步骤编号、无 step lock。
2. 连接线颜色只来自**现有真实状态**（既有 connections / channels / agents 的
   status 字段推导），不新增后端状态：
   - 绿 = 该侧存在 active Connection 且该端对象 active；
   - 灰 = 该侧还没连上；红 = 该侧通路被停用；
   - 「页面上有这个对象」绝不足以变绿。
3. 中心节点只表达 SteeringFlow（不需要用户配置）。
4. 顶栏收口：「高级」与「账户」收进下拉浮层；Advanced 页面一个不删。

方法：静态源码断言 + Node `vm` 行为矩阵（直接执行 home.js 的真实 `render`），
缺 Node 自动 skip；全部只读，不触 DB / 不触网。
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

_NODE = shutil.which("node")
needs_node = pytest.mark.skipif(
    _NODE is None, reason="node 不可用（行为级验收需要 Node）")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _strip_html_comments(src: str) -> str:
    return re.sub(r"<!--.*?-->", " ", src, flags=re.S)


# ================================================================== A. 结构

def test_a1_graph_has_two_endpoints_two_links_and_hub() -> None:
    """连接状态图：渠道 / Agent 两个端点节点 + 两段连接线 + 中心节点。"""
    html = _strip_html_comments(_read(HOME_HTML))
    for marker in ("node-channel", "node-agent", "node-hub",
                   "link-channel", "link-agent",
                   "cta-channel", "cta-agent", "flow-graph", "flow-link"):
        assert marker in html, f"缺少连接状态图元素：{marker}"


def test_a2_no_step_numbers_or_wizard_language() -> None:
    """最高优先级约束：产品关系 ≠ 操作顺序——不得出现步骤编号。"""
    html = _strip_html_comments(_read(HOME_HTML))
    for banned in ("①", "②", "③", "第一步", "第二步", "Step 1"):
        assert banned not in html, f"Flow Home 出现步骤化语言：{banned}"


def test_a3_node_actions_follow_connection_state() -> None:
    """§五 + M7-AUI-10.4：未连接 → 连接渠道 / 连接 Agent；
    已就绪 → 选择器旁的「编辑」（由 JS 置换）。"""
    js = _read(HOME_JS)
    assert "连接渠道" in js and "连接 Agent" in js
    assert 'const ACTION_TOGGLE = "编辑"' in js


def test_a4_hub_only_expresses_steeringflow() -> None:
    """§五：中心节点不需要用户配置（无「配置 SteeringFlow」动作）。"""
    html = _strip_html_comments(_read(HOME_HTML))
    hub = html[html.index('id="node-hub"'):html.index('id="link-agent"')]
    assert "btn" not in hub, "中心节点不应有配置动作"


def test_a5_advanced_kept_but_demoted_to_dropdown() -> None:
    """§八 / M7-AUI-8.2：6 个管理能力一个不删，但收进下拉浮层、不占首屏视觉，
    且按**用户任务分组**（接入配置 / 运行与排障 / 成员），不再罗列实体名。"""
    html = _strip_html_comments(_read(HOME_HTML))
    panel = html[html.index('id="nav-advanced-panel"'):]
    panel = panel[:panel.index("</details>")]
    links = re.findall(r'href="([^"]+)"', panel)
    assert links == ["/channels.html", "/connections.html", "/agents.html",
                     "/interactions.html", "/identities.html", "/users.html"], links
    assert "nav-menu" in html
    # M7-AUI-8.2：分组标题 + 用户任务语言；实体名不再是菜单文案。
    for group in ("接入配置", "运行与排障", "成员"):
        assert group in panel, f"缺少菜单分组：{group}"
    for banned in ("Interaction Observability", ">Agents<", ">Channels<",
                   ">Connections<", ">Users<", ">Identities<"):
        assert banned not in panel, f"菜单仍是实体名文案：{banned}"
    # M7-AUI-8.2：Agent 管理的入口必须是 /agents.html（生产 `/` 会 302 到入口页）。
    assert 'href="/"' not in panel


# ================================================================== B. Node 行为矩阵

_JS_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const src = fs.readFileSync(process.env.PROBE_JS, "utf8").replace(/^import\s[^;]*;/gm, "");

const els = {};
function fakeEl() {
  const classes = new Set();
  return {
    classList: {
      add(c) { classes.add(c); },
      remove(c) { classes.delete(c); },
      toggle(c, on) { if (on === undefined) { classes.has(c) ? classes.delete(c) : classes.add(c); }
                      else if (on) { classes.add(c); } else { classes.delete(c); } },
      contains(c) { return classes.has(c); },
    },
    textContent: "", innerHTML: "", href: "", className: "", value: "",
    open: false, parentNode: null,
    appendChild() {}, addEventListener() {}, focus() {},
  };
}
function el(id) { if (!els[id]) els[id] = fakeEl(); return els[id]; }

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
vm.runInContext(src + "\n;globalThis.__t = { render: render, renderConnecting: renderConnecting, ref: ref };", sandbox);

(async function () {
  await new Promise((r) => setTimeout(r, 0));
  const t = sandbox.__t;
  // M7-AUI-10.4：当前名称由节点内嵌选择器的值表达（节点不再有独立名称文本）。
  const pickName = (pickerId, list, excludeLocal) => {
    const v = els[pickerId].value;
    const item = (sandbox.__t.ref[list] || []).find(
      (x) => String(x.id) === v && (!excludeLocal || x.type !== "local"));
    return item ? item.name : "(空)";
  };
  const snap = () => ({
    linkChannel: els["link-channel"].className,
    linkAgent: els["link-agent"].className,
    valChannel: pickName("channel-picker", "channels", true),
    valAgent: pickName("agent-picker", "agents", false),
    hub: els["node-hub"].className,
    hubVal: els["node-hub-value"].textContent,
    ctaChannel: els["cta-channel"].textContent,
    ctaAgent: els["cta-agent"].textContent,
    ctaChannelHidden: els["cta-channel"].classList.contains("hidden"),
    ctaAgentHidden: els["cta-agent"].classList.contains("hidden"),
    chEdit: els["cta-edit-channel"].textContent,
    chEditHidden: els["cta-edit-channel"].classList.contains("hidden"),
    agEdit: els["cta-edit-agent"].textContent,
    agEditHidden: els["cta-edit-agent"].classList.contains("hidden"),
    badge: els["flow-badge"].textContent,
    lead: els["home-lead"].textContent,
    ctaPrimary: els["cta-primary"].textContent,
    ctaPrimaryHidden: els["cta-primary"].classList.contains("hidden"),
    verifyHidden: els["cta-verify"].classList.contains("hidden"),
  });

  const CH = { id: 1, name: "bot-1", type: "wecom_bot", status: "active" };
  const AG = { id: 2, name: "agent-1", type: "acp", status: "active" };
  const out = {};

  t.render([], [], []);
  out.empty = snap();

  t.render([], [CH], [{ id: 5, channel_id: 1, agent_id: 99, status: "active" }]);
  out.channelLinked = snap();

  t.render([], [CH], []);
  out.channelOnlyNoPath = snap();

  t.render([AG], [], [{ id: 6, channel_id: 98, agent_id: 2, status: "active" }]);
  out.agentLinked = snap();

  t.render([AG], [CH], []);
  out.bothNoFlow = snap();

  t.render([AG], [CH], [{ id: 7, channel_id: 1, agent_id: 2, status: "active" }]);
  out.connected = snap();

  t.render([AG], [CH], [{ id: 8, channel_id: 1, agent_id: 2, status: "disabled" }]);
  out.error = snap();

  // 对象存在但 inactive：绝不变绿
  t.render([{ id: 2, name: "agent-1", type: "acp", status: "inactive" }], [CH],
           [{ id: 9, channel_id: 1, agent_id: 2, status: "active" }]);
  out.inactiveAgentNotGreen = snap();

  process.stdout.write(JSON.stringify(out));
})().catch(function (e) {
  process.stderr.write(String((e && e.stack) || e));
  process.exit(2);
});
"""


def _run_matrix() -> dict:
    env = dict(os.environ)
    env["PROBE_JS"] = str(HOME_JS)
    proc = subprocess.run(
        [_NODE, "-e", _JS_HARNESS], capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env, check=False)
    assert proc.returncode == 0, f"node 执行失败：{proc.stderr}"
    return json.loads(proc.stdout)


@needs_node
def test_b1_state_a_both_grey_with_both_actions() -> None:
    """状态 A：无任何连接 → 两侧灰线 + [连接渠道] [连接 Agent]，无步骤顺序。"""
    out = _run_matrix()
    a = out["empty"]
    assert "is-off" in a["linkChannel"] and "is-off" in a["linkAgent"]
    assert a["ctaChannel"] == "连接渠道" and a["ctaAgent"] == "连接 Agent"


@needs_node
def test_b2_state_b_channel_side_green_agent_side_grey() -> None:
    """状态 B：渠道侧已有 active 通路 → 渠道侧绿；Agent 侧灰。"""
    out = _run_matrix()
    b = out["channelLinked"]
    assert "is-on" in b["linkChannel"], b["linkChannel"]
    assert "is-off" in b["linkAgent"], b["linkAgent"]
    assert b["ctaChannelHidden"] is True          # 渠道已就绪 → 连接动作让位
    assert b["chEdit"] == "编辑" and not b["chEditHidden"]  # 编辑入口在选择器旁
    assert b["ctaAgent"] == "连接 Agent"


@needs_node
def test_b3_object_alone_is_never_green() -> None:
    """§三红线：只有渠道对象、没有 active 通路 → 仍灰线（不因存在而变绿）。"""
    out = _run_matrix()
    assert "is-on" not in out["channelOnlyNoPath"]["linkChannel"]


@needs_node
def test_b4_inactive_endpoint_never_green() -> None:
    """§三红线：Agent inactive 时，即使 Connection active，该侧也不绿。"""
    out = _run_matrix()
    assert "is-on" not in out["inactiveAgentNotGreen"]["linkAgent"]


@needs_node
def test_b5_state_c_agent_side_green_channel_side_grey() -> None:
    """状态 C：先连 Agent 完全合法 → Agent 侧绿，渠道侧灰。"""
    out = _run_matrix()
    c = out["agentLinked"]
    assert "is-on" in c["linkAgent"] and "is-off" in c["linkChannel"]
    assert c["ctaChannel"] == "连接渠道" and not c["ctaChannelHidden"]
    assert c["agEdit"] == "编辑" and not c["agEditHidden"]


@needs_node
def test_b6_state_e_both_present_but_not_connected() -> None:
    """状态 E：两端都在但 Flow 未建立 → 两侧都不是绿，且提供「把它们连接起来」。"""
    out = _run_matrix()
    e = out["bothNoFlow"]
    assert "is-on" not in e["linkChannel"] and "is-on" not in e["linkAgent"]
    assert "把它们连接起来" in e["ctaPrimary"] and not e["ctaPrimaryHidden"]


@needs_node
def test_b7_state_d_both_green_named_endpoints() -> None:
    """状态 D：完整 Flow → 两侧绿、两端各自具名、引导发送第一条消息。"""
    out = _run_matrix()
    d = out["connected"]
    assert "is-on" in d["linkChannel"] and "is-on" in d["linkAgent"]
    # M7-AUI-9：两端节点**都只显示名字**——渠道侧不再带类型前缀（如「企业微信 Bot」）
    assert d["valChannel"] == "bot-1", d["valChannel"]
    assert d["valAgent"] == "agent-1", d["valAgent"]
    assert d["hubVal"] == "已接通" and d["badge"] == "已接通"
    assert "发送第一条消息" in d["lead"] and not d["verifyHidden"]
    # M7-AUI-9：lead 句子同样只说名字——类型词（企业微信 Bot 等）不再进句子
    assert d["lead"].startswith("「bot-1」已连接到你的 Agent「agent-1」"), d["lead"]
    assert "企业微信" not in d["lead"], f"lead 仍带类型词：{d['lead']}"


@needs_node
def test_b8_error_state_is_never_green() -> None:
    """验收 6：连接失败 → 该侧不是绿（红/灰），并给可行动错误。"""
    out = _run_matrix()
    f = out["error"]
    assert "is-on" not in f["linkChannel"] and "is-on" not in f["linkAgent"]
    assert "is-error" in f["linkChannel"] and "is-error" in f["hub"]


@needs_node
def test_b9_both_ends_always_actionable() -> None:
    """§四：任何状态下两个节点动作都在——「连接 …」或「编辑」二选一可见。"""
    out = _run_matrix()
    for name, snap in out.items():
        channel_actionable = (snap["ctaChannel"] and not snap["ctaChannelHidden"]) \
            or (snap["chEdit"] and not snap["chEditHidden"])
        agent_actionable = (snap["ctaAgent"] and not snap["ctaAgentHidden"]) \
            or (snap["agEdit"] and not snap["agEditHidden"])
        assert channel_actionable, f"{name} 渠道端缺动作"
        assert agent_actionable, f"{name} Agent 端缺动作"
