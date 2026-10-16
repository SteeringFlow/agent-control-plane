"""M7-AUI-5 —— Flow Home 的 Connected 必须绑定到**当前显示的渠道**。

真实事故（M7-AUI-4 审计，tenant 4）：用户配置的「企业微信」渠道**没有 Connection**，
但租户里另一条 Flow（本地测试渠道 → Agent）让 Home 显示成「已接通」并提示
「从你的渠道发送一条消息」→ 用户照做 → 消息在该渠道上没有通路 → Identity
fail-closed。

冻结的判定（本文件锁死）：

    Channel Ready = 当前渠道 active
    Agent Ready   = 当前 Agent active
    Flow Connected = **当前显示的渠道自己**存在 active Connection（且该通路的 Agent active）
    First Success  = 同一 Interaction：processing_status == succeeded
                     AND delivery_status == delivered

**绝不**因为租户存在其他渠道的 Connection 就把当前渠道显示为 Connected。
只改产品语义与文案，不改后端 / 运行时 / Bootstrap / Identity / Grant。
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


def _js(path: Path = HOME_JS) -> str:
    src = re.sub(r"/\*.*?\*/", " ", _read(path), flags=re.S)
    return re.sub(r"//[^\n]*", " ", src)


# ================================================================== A. 静态：判定与文案

def test_a1_connected_is_channel_anchored() -> None:
    """Connected 判定必须锚定当前渠道：租户级 findFlowConnection 只留给 computeState。"""
    code = _js()
    assert "function findFlowForChannel" in code
    body = code[code.index("function findFlowForChannel"):]
    body = body[:body.index("\n}")]
    assert "c.channel_id !== channel.id" in body, "Flow 判定必须要求连接属于当前渠道"
    assert 'c.status !== "active"' in body
    assert 'agent.status === "active"' in body, "该通路的 Agent 必须 active 才算 Flow"
    # render 内不得再出现租户级 Flow 判定（否则跨渠道污染）
    render_body = code[code.index("function render("):]
    assert "findFlowConnection" not in render_body, \
        "render 不得使用租户级 findFlowConnection 判定当前渠道"


def test_a2_connected_only_renders_when_current_channel_has_flow() -> None:
    """「已接通 / 发消息 / 检查首次成功」只在 `if (flow)` 分支内出现。"""
    code = _js()
    start = code.index("if (flow) {")
    end = code.index("if (!flow) {")
    block = code[start:end]
    for marker in ("已接通", "发送第一条消息", "show(ctaVerify)"):
        assert marker in block, f"分支内缺少：{marker}"
    # 其余分支（未接通）绝不出现发消息 / 检查首次成功 / 已接通徽标
    rest = code[end:]
    for banned in ("发送第一条消息", "show(ctaVerify)", 'setBadge("active"'):
        assert banned not in rest, f"未接通分支出现误导内容：{banned}"


def test_a3_disconnected_copy_states_the_precondition() -> None:
    """未接通文案：明确「已准备好，请把它连接到你的 Agent」，不含发消息暗示。"""
    code = _js()
    assert "已准备好，请把它连接到你的 Agent" in code
    for banned in ("你可以只做这一步", "还没有接到你的 Agent「"):
        assert banned not in code, f"仍存在误导文案：{banned}"
    # 渠道测试成功后的前置条件表达
    assert "把它接到你的 Agent 后，从这条渠道发送的消息才会被处理" in code


def test_a4_diagnosis_copy_is_flow_oriented_and_identical() -> None:
    """identity_resolution 诊断按页面上下文给出可行动文案（M7-AUI-10.6.2-B）。

    - Home：现在有「重新验证并引导」入口 → 文案指向该入口 + 窗口内重发消息；
    - connect.html：没有该入口 → 保留「重新接通」语境的既有文案；
    - 两页都不得暴露内部授权术语。
    """
    home = _js(HOME_JS)
    assert "这条渠道还没有完成首条消息引导（或引导窗口已过期）。" in home
    assert "重新验证并引导" in home
    assert "这条渠道上的消息还没有被授权。" not in home
    connect = _js(CONNECT_JS)
    assert "这条渠道还没有接到你的 Agent，或者这条连接尚未启用。" in connect
    assert "这条渠道上的消息还没有被授权。" not in connect


# ================================================================== B. Node 行为矩阵

_JS_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const src = fs.readFileSync(process.env.PROBE_JS, "utf8").replace(/^import\s[^;]*;/gm, "");
const plan = JSON.parse(process.env.PROBE_PLAN || "{}");

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
["panel-channel", "panel-agent", "pa-wb-block", "cta-verify", "cta-primary",
 "flow-badge", "home-error", "pc-error", "pa-error"].forEach(function (id) {
  el(id).classList.add("hidden");
});

const sandbox = {
  console: console,
  document: { getElementById: el, createElement: () => fakeEl() },
  window: { location: { replace() {} } },
  fetch: function (url) {
    const u = String(url);
    if (u.indexOf("/api/interactions") >= 0) {
      return Promise.resolve({ ok: true, status: 200,
        json: () => Promise.resolve({ items: plan.interactions || [] }) });
    }
    return Promise.resolve({ ok: false, status: 401, json: () => Promise.resolve({}) });
  },
};
vm.createContext(sandbox);
const sharedPath = process.env.PROBE_JS.replace(/[^/\\]+$/, "") + "shared/http.js";
const sharedSrc = fs.readFileSync(sharedPath, "utf8").replace(/^export /gm, "");
vm.runInContext(sharedSrc, sandbox);
vm.runInContext(src + "\n;globalThis.__t = { render: render, computeState: computeState, "
  + "checkFirstSuccess: checkFirstSuccess, ref: ref };", sandbox);

// M7-AUI-10.4：当前名称由节点内嵌选择器的值表达（节点不再有独立名称文本）。
const pickName = (pickerId, list, excludeLocal) => {
  const v = els[pickerId].value;
  const item = (sandbox.__t.ref[list] || []).find(
    (x) => String(x.id) === v && (!excludeLocal || x.type !== "local"));
  return item ? item.name : "(空)";
};

const snap = () => ({
  computeState: null,
  badge: els["flow-badge"].textContent,
  badgeHidden: els["flow-badge"].classList.contains("hidden"),
  hub: els["node-hub-value"].textContent,
  hubClass: els["node-hub"].className,
  valChannel: pickName("channel-picker", "channels", true),
  valAgent: pickName("agent-picker", "agents", false),
  linkChannel: els["link-channel"].className,
  linkAgent: els["link-agent"].className,
  lead: els["home-lead"].textContent,
  ctaPrimary: els["cta-primary"].textContent,
  ctaPrimaryHidden: els["cta-primary"].classList.contains("hidden"),
  verifyHidden: els["cta-verify"].classList.contains("hidden"),
  detail: els["home-detail"].textContent,
});

(async function () {
  await new Promise((r) => setTimeout(r, 0));
  const t = sandbox.__t;
  const out = {};
  const LOCAL = { id: 1, name: "本地测试渠道", type: "local", status: "active" };
  const WECOM_A = { id: 10, name: "企业微信 A", type: "wecom_bot", status: "active" };
  const WECOM = { id: 2, name: "企业微信", type: "wecom_bot", status: "active" };
  const AGENT_A = { id: 1, name: "测试", type: "acp", status: "active" };
  const AGENT_B = { id: 2, name: "workbuddy-agent", type: "acp", status: "active" };
  const CONN_A = { id: 1, channel_id: 10, agent_id: 1, status: "active" };
  const CONN_B = { id: 2, channel_id: 2, agent_id: 1, status: "active" };
  const CONN_LOCAL = { id: 9, channel_id: 1, agent_id: 1, status: "active" };

  // A：Channel A + Agent A + Connection A（当前渠道就是 A）→ 已接通
  t.render([AGENT_A], [WECOM_A], [CONN_A]);
  out.caseA = snap();
  out.caseAState = t.computeState([AGENT_A], [WECOM_A], [CONN_A]);

  // B（真实事故形态）：本地测试载体有 Flow；当前显示的是「企业微信」（没有 Connection）
  t.render([AGENT_A], [LOCAL, WECOM], [CONN_LOCAL]);
  out.caseB = snap();
  out.caseBState = t.computeState([AGENT_A], [LOCAL, WECOM], [CONN_LOCAL]);
  t.render([AGENT_A], [LOCAL, WECOM], [CONN_LOCAL]);
  out.caseBRepeat = snap();  // H：重复渲染（刷新后）结果一致

  // C：企业微信就绪、有 Agent、但没有 Connection
  t.render([AGENT_A], [WECOM], []);
  out.caseC = snap();

  // D：企业微信 + Agent + Connection B → 已接通
  t.render([AGENT_A], [WECOM], [CONN_B]);
  out.caseD = snap();

  // E：Connection 停用 → 不得显示已接通
  t.render([AGENT_A], [WECOM], [{ id: 3, channel_id: 2, agent_id: 1, status: "inactive" }]);
  out.caseE = snap();

  // G：多个渠道 / Agent / 连接 —— 当前渠道 = 企业微信，只能用它自己的 Flow
  t.render([AGENT_A, AGENT_B], [LOCAL, WECOM],
           [CONN_A, { id: 4, channel_id: 2, agent_id: 2, status: "active" }]);
  out.caseG = snap();

  // G2：企业微信的连接存在，但它的 Agent 不 active → 不算已接通
  t.render([AGENT_A, { id: 3, name: "停用 Agent", type: "acp", status: "inactive" }],
           [WECOM], [{ id: 5, channel_id: 2, agent_id: 3, status: "active" }]);
  out.caseG2 = snap();

  // F：First Success = 同一 interaction succeeded × delivered
  t.render([AGENT_A], [WECOM], [CONN_B]);
  await t.checkFirstSuccess();
  out.firstSuccessOk = els["home-detail"].textContent;
  process.stdout.write(JSON.stringify(out));
})().catch(function (e) {
  process.stderr.write(String((e && e.stack) || e));
  process.exit(2);
});
"""


def _run(interactions: list | None = None) -> dict:
    env = dict(os.environ)
    env["PROBE_JS"] = str(HOME_JS)
    env["PROBE_PLAN"] = json.dumps({"interactions": interactions or []})
    proc = subprocess.run(
        [_NODE, "-e", _JS_HARNESS], capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env, check=False)
    assert proc.returncode == 0, f"node 执行失败：{proc.stderr}"
    return json.loads(proc.stdout)


@needs_node
def test_a5_rendered_copy_exposes_no_machinery() -> None:
    """用户可见文案（含失败诊断）不含内部术语，且失败诊断指向 Flow 前置条件。"""
    out = _run()
    joined = " ".join([out["caseA"]["lead"], out["caseB"]["lead"],
                       out["caseC"]["lead"], out["caseD"]["lead"]])
    for banned in ("Identity", "Grant", "Bootstrap", "Session", "Interaction",
                   "Authorization", "Connection", "Tenant", "ACP", "Runtime"):
        assert banned not in joined, f"用户文案暴露内部术语：{banned}"
    detail = _run([{"id": 1, "channel_id": 2, "processing_status": "failed",
                    "failure_stage": "identity_resolution"}])["firstSuccessOk"]
    assert "这条渠道还没有完成首条消息引导" in detail, detail
    assert "重新验证并引导" in detail, detail
    assert "单聊消息" in detail, detail
    for banned in ("Identity", "Grant", "Bootstrap", "Session", "授权", "身份"):
        assert banned not in detail, f"失败诊断暴露内部术语：{banned}"


@needs_node
def test_b1_case_a_own_flow_is_connected() -> None:
    """A：当前渠道自己有 active 通路 → 已接通 + 可以发第一条消息。"""
    out = _run()["caseA"]
    assert out["badge"] == "已接通" and out["hub"] == "已接通"
    assert "发送第一条消息" in out["lead"]
    assert out["verifyHidden"] is False
    assert "is-on" in out["linkChannel"] and "is-on" in out["linkAgent"]


@needs_node
def test_b2_case_b_other_channel_flow_must_not_pollute() -> None:
    """B（真实事故）：只显示本地测试渠道的 Flow 时，当前渠道（企业微信）绝不算已接通。"""
    result = _run()
    case = result["caseB"]
    # 租户级 computeState 仍是 connected（该函数语义未改，M6.6-F1 冻结）……
    assert result["caseBState"] == "connected"
    # ……但 Home 的显示**不得**跟着它走
    assert case["badge"] != "已接通", case["badge"]
    assert case["hub"] != "已接通", case["hub"]
    assert case["verifyHidden"] is True, "未接通不得提供「检查首次成功」"
    assert "发送第一条消息" not in case["lead"], case["lead"]
    assert "已准备好，请把它连接到你的 Agent" in case["lead"], case["lead"]
    assert "把它们连接起来" in case["ctaPrimary"]
    assert "企业微信" in case["valChannel"], case["valChannel"]
    assert "is-on" not in case["linkChannel"], case["linkChannel"]


@needs_node
def test_b3_case_c_channel_ready_without_flow() -> None:
    """C：渠道就绪但没有通路 → 不提示发消息，也不给检查首次成功。"""
    case = _run()["caseC"]
    assert case["badge"] == "未完成"
    assert case["verifyHidden"] is True
    assert "发送第一条消息" not in case["lead"]
    assert "已准备好，请把它连接到你的 Agent" in case["lead"]
    assert "is-on" not in case["linkChannel"]


@needs_node
def test_b4_case_d_own_connection_is_connected() -> None:
    """D：当前渠道自己的通路建立后 → 才进入已接通。"""
    case = _run()["caseD"]
    assert case["badge"] == "已接通" and case["hub"] == "已接通"
    assert "is-on" in case["linkChannel"] and "is-on" in case["linkAgent"]
    assert case["verifyHidden"] is False
    assert "发送第一条消息" in case["lead"]


@needs_node
def test_b5_case_e_inactive_connection_is_not_connected() -> None:
    """E：通路停用 → 不得显示已接通，也不给发消息/首次成功。"""
    case = _run()["caseE"]
    assert case["badge"] == "需要处理"
    assert case["verifyHidden"] is True
    assert "发送第一条消息" not in case["lead"]
    assert "is-on" not in case["linkChannel"]


@needs_node
def test_b6_case_g_multi_entity_isolation() -> None:
    """G：多渠道/多 Agent/多连接时，当前渠道只用自己的 Flow（显示对应的 Agent）。"""
    case = _run()["caseG"]
    assert "企业微信" in case["valChannel"]
    assert case["valAgent"] == "workbuddy-agent", case["valAgent"]
    assert case["badge"] == "已接通"
    # G2：连接存在但 Agent 不 active → 不算已接通
    case2 = _run()["caseG2"]
    assert case2["badge"] != "已接通", case2["badge"]
    assert case2["verifyHidden"] is True


@needs_node
def test_b7_case_h_repeat_render_is_stable() -> None:
    """H：同样的真实数据重复渲染（等价于刷新后）结果一致。"""
    result = _run()
    assert result["caseB"] == result["caseBRepeat"]


@needs_node
def test_b8_first_success_only_from_dual_status() -> None:
    """F：First Success 仍由「同一条 interaction succeeded × delivered」推导
    （M7-AUI-9.2：且该 interaction 必须属于当前渠道 channel_id=2）。"""
    ok = _run([{"id": 1, "channel_id": 2, "processing_status": "succeeded",
                "delivery_status": "delivered"}])["firstSuccessOk"]
    assert "成功送达" in ok, ok
    partial = _run([{"id": 2, "channel_id": 2, "processing_status": "succeeded",
                     "delivery_status": "pending"}])["firstSuccessOk"]
    assert "成功送达" not in partial, partial
