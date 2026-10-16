"""M7-AUI-6 —— 已保存渠道凭据的展示 + 原地面板收起。

冻结的行为（本文件锁死）：

1. **已保存的 Bot ID 可以显示**（后端对 `bot_id` 本就不脱敏）；
   **Bot Secret 永不回显** —— 响应里只有脱敏占位符，前端也绝不读取 / 写回 / 存 storage。
   留空即「沿用已保存的 Secret」（更新走既有 `PATCH /api/channels/{id}` 的 merge 语义）。
2. 凭据字段**不接受**浏览器 / 密码管理器的登录凭据自动填充（这不是登录框），
   且登录页 `workspace.html` 的密码管理行为不受影响。
3. 渠道「测试连接」：**成功 → 面板自动收起**（回到轻量 Flow）；
   **失败 → 面板保持展开**且错误就地显示，便于立即重试。
4. 面板始终原地展开 / 收起：**不跳转、不改 URL**。
5. Channel Ready（蓝）≠ Flow Connected（绿）≠ First Success：本任务不改变该语义。

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
ENTRY_HTML = FRONTEND / "workspace.html"

_NODE = shutil.which("node")
needs_node = pytest.mark.skipif(
    _NODE is None, reason="node 不可用（行为级验收需要 Node）")

# 后端脱敏占位符（schemas/channels.py::_REDACTED）——它绝不能出现在任何输入框里
REDACTED = "********"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _html(path: Path = HOME_HTML) -> str:
    return re.sub(r"<!--.*?-->", " ", _read(path), flags=re.S)


def _js(path: Path = HOME_JS) -> str:
    src = re.sub(r"/\*.*?\*/", " ", _read(path), flags=re.S)
    return re.sub(r"//[^\n]*", " ", src)


def _input_tag(html: str, field_id: str) -> str:
    match = re.search(r'<input[^>]*id="%s"[^>]*>' % field_id, html, re.S)
    assert match is not None, f"缺少字段：{field_id}"
    return re.sub(r"\s+", " ", match.group(0))


# ================================================================== A. 新渠道 / B. 已保存渠道

def test_a1_credential_fields_exist_and_secret_is_masked() -> None:
    """字段面：Bot ID（文本）+ Bot Secret（密码）+ 「已保存」提示位。"""
    html = _html()
    bot_id = _input_tag(html, "pc-bot-id")
    secret = _input_tag(html, "pc-bot-secret")
    assert 'type="text"' in bot_id
    assert 'type="password"' in secret, "Secret 必须保持密码态（绝不改成明文框）"
    assert 'id="pc-saved-hint"' in html


def test_a2_no_redacted_placeholder_is_ever_written_back() -> None:
    """脱敏占位符绝不回填：前端不读取 config.bot_secret，也不把 ******** 放进输入框。"""
    code = _js()
    assert REDACTED not in code, "前端不得把脱敏占位符当值使用"
    prefill = code[code.index("function syncChannelPanel"):]
    prefill = prefill[:prefill.index("function channelChangeRequested")]
    assert "bot_secret" not in prefill, "回填逻辑不得触碰 Secret"
    assert "pcBotSecret.value = \"\"" in prefill, "Secret 必须以空值呈现"


# ================================================================== C. autofill

def test_c1_credential_fields_are_not_login_fields() -> None:
    """Bot ID / Bot Secret 不得长成登录框（否则浏览器会填登录邮箱 + 密码）。"""
    html = _html()
    for field_id in ("pc-bot-id", "pc-bot-secret"):
        tag = _input_tag(html, field_id)
        # 名字不是 username/email/password，autocomplete 也不是登录语义
        for banned in ('name="username"', 'name="email"', 'name="password"',
                       'autocomplete="username"', 'autocomplete="current-password"'):
            assert banned not in tag, f"{field_id} 像登录字段：{banned}"
        assert 'autocomplete="off"' in tag or 'autocomplete="new-password"' in tag
        assert "data-lpignore" in tag and "data-1p-ignore" in tag
    # 面板不是 <form>（浏览器自动填充主要针对登录表单）
    panel = _html()[_html().index('id="panel-channel"'):]
    panel = panel[:panel.index('id="panel-agent"')]
    assert "<form" not in panel, "渠道面板不得是登录表单结构"
    # 登录页自身的密码管理行为不受影响（保留 current-password / new-password）
    entry = _read(ENTRY_HTML)
    assert 'autocomplete="current-password"' in entry
    assert 'autocomplete="new-password"' in entry


def test_c2_runtime_backstop_clears_only_autofilled_values() -> None:
    """兜底：页面加载后被自动填充、且用户**未键入**的字段会被清空；已保存回填不清。"""
    code = _js()
    assert "function guardCredentialFields" in code
    body = code[code.index("function guardCredentialFields"):]
    body = body[:body.index("\n}")]
    assert "typed" in body and "el.value = \"\"" in body
    # 程序化回填（已保存的非密值）必须被标记为用户内容
    assert "markTyped" in code
    # 不把凭据写进任何浏览器存储（M7-AUI-10.6.2-C 合同收窄：sessionStorage
    # 仅允许 Flow 选择 key "sf.flow.selection"（仅 {ch, ag} 两个 ID，无凭据）；
    # localStorage / indexeddb 仍全量禁止）
    lowered = _js().lower()
    for banned in ("localstorage", "indexeddb"):
        assert banned not in lowered
    ss_calls = re.findall(r"sessionstorage\.(\w+)\(", lowered)
    assert set(ss_calls) <= {"getitem", "setitem", "removeitem"}, ss_calls
    assert 'const flow_selection_key = "sf.flow.selection";' in lowered
    sel_block = lowered[lowered.index("function saveflowselection")
                        :lowered.index("function extractworkbuddyidentity")]
    for banned in ("api_key", "bearer_token", "runtime_id", "credential"):
        assert banned not in sel_block


def test_c3_agent_panel_untouched_but_hardened() -> None:
    """§九：本任务不动 Agent 面板的交互（只有既有的 autofill 属性）。"""
    code = _js()
    agent_block = code[code.index("function testAgent"):]
    assert "PATCH" not in agent_block, "Agent 侧不得出现编辑写路径"


# ================================================================== D/E. 收起语义

def test_d1_success_collapses_and_failure_keeps_panel_open() -> None:
    """成功 → setOpenPanel(null)；失败 → 不打开展开状态（保持原样 = 展开）。"""
    code = _js()
    body = code[code.index("async function testChannel"):]
    body = body[:body.index("async function testAgent")]
    assert body.count("setOpenPanel(null)") == 2, \
        "成功（新建 / 更新）与「无变更」两条路径都要收起面板"
    # 失败分支（catch 内）不得收起
    catch_block = body[body.index("} catch (err) {"):]
    assert "setOpenPanel" not in catch_block, "失败时必须保持面板展开"
    assert "setPanelError(pcError" in catch_block, "失败信息必须留在面板内"


def test_d2_panel_toggle_never_navigates() -> None:
    """F/J：面板展开 / 收起不跳转、不改 URL。"""
    code = _js()
    assert "function togglePanel" in code
    assert "function setOpenPanel" in code
    # 面板路径里没有导航赋值（唯一允许的是未认证时回登录页）
    for banned in ("location.href =", "location.assign", "location.replace(\"/connect"):
        assert banned not in code, f"面板操作出现导航：{banned}"
    # 只允许一处导航（未认证回 Workspace），且都不指向 connect。
    # M7-AUI-9：退出登录的跳转已随账户菜单统一到 nav.js，不再由 home.js 各自实现。
    assert code.count('window.location.replace("/workspace.html")') == 1, \
        "面板操作不得引入新的导航"
    for banned in ("/connect.html", "/channels.html"):
        assert f'window.location.replace("{banned}"' not in code, f"面板出现跳转：{banned}"
    html = _html()
    tag = re.search(r'<button[^>]*id="cta-channel"[^>]*>', html)
    assert tag is not None and "href" not in tag.group(0), "「查看 / 更换」必须是原位按钮"


# ================================================================== 行为矩阵（Node）

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
    handlers: {},
    addEventListener(type, fn) { this.handlers[type] = fn; },
    focus() {}, appendChild() {},
  };
}
function el(id) { if (!els[id]) els[id] = fakeEl(); return els[id]; }
["panel-channel", "panel-agent", "pa-wb-block", "cta-verify", "cta-primary",
 "flow-badge", "home-error", "pc-error", "pa-error", "pc-saved-hint"].forEach(function (id) {
  el(id).classList.add("hidden");
});
// 复刻 HTML 的初始值（<select> 默认选中企业微信）
el("pc-type").value = "wecom_bot";

// —— 可编程的服务端状态（由外层驱动代码直接改写）——
let channels = [];
let patchStatus = 200;
let postStatus = 201;
const calls = [];

function channelBody(cfg, id, name) {
  return { id: id || 1, name: name || "企业微信", type: "wecom_bot",
           status: "active", config: cfg };
}
const sandbox = {
  console: console,
  document: { getElementById: el, createElement: () => fakeEl() },
  window: { location: { replace() {} } },
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
    if (u.indexOf("/api/agents") >= 0) return ok(200, { items: [] });
    if (u.indexOf("/api/connections") >= 0) return ok(200, { items: [] });
    if (u.indexOf("/api/interactions") >= 0) return ok(200, { items: [] });
    if (u.indexOf("/api/channels") >= 0) {
      if (method === "PATCH") {
        if (patchStatus >= 400) return ok(patchStatus, { detail: "invalid" });
        const merged = Object.assign({ bot_secret: "********" }, channels[0].config,
                                     (payload && payload.config) || {});
        return ok(200, channelBody(merged, channels[0].id, channels[0].name));
      }
      if (method === "POST") {
        if (postStatus >= 400) return ok(postStatus, { detail: "invalid" });
        const created = channelBody(payload.config, 9, payload.name);
        channels.push(created);
        return ok(201, created);
      }
      return ok(200, { items: channels });
    }
    return ok(404, {});
  },
};
vm.createContext(sandbox);
const sharedPath = process.env.PROBE_JS.replace(/[^/\\]+$/, "") + "shared/http.js";
const sharedSrc = fs.readFileSync(sharedPath, "utf8").replace(/^export /gm, "");
vm.runInContext(sharedSrc, sandbox);
vm.runInContext(src + "\n;globalThis.__t = { refresh: refresh, render: render, "
  + "togglePanel: togglePanel, setOpenPanel: setOpenPanel };", sandbox);

const flush = () => new Promise((r) => setTimeout(r, 0));
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const vis = (id) => !els[id].classList.contains("hidden");
function openChannel() {
  sandbox.__t.setOpenPanel(null);
  // M7-AUI-10.4：已保存渠道的编辑入口 = 选择器旁的「编辑」按钮
  els["cta-edit-channel"].handlers.click();
}
function snap() {
  return {
    botId: els["pc-bot-id"].value,
    secret: els["pc-bot-secret"].value,
    hintVisible: vis("pc-saved-hint"),
    hint: els["pc-saved-hint"].textContent,
    panelVisible: vis("panel-channel"),
    error: els["pc-error"].textContent,
    errorVisible: vis("pc-error"),
    note: els["home-detail"].textContent,
    status: els["status-channel"].textContent,
    badge: els["flow-badge"].textContent,
    verifyHidden: els["cta-verify"].classList.contains("hidden"),
  };
}

(async function () {
  const t = sandbox.__t;
  const out = {};

  // ---- A：还没有渠道 ----
  channels = [];
  await t.refresh();
  openChannel();
  out.caseA = snap();

  // ---- B：已保存渠道（服务端只会给脱敏占位符）----
  channels = [channelBody({ bot_id: "bot-saved", bot_secret: "********" }, 1, "企业微信")];
  await t.refresh();
  openChannel();
  out.caseB = snap();
  await wait(400);            // 等 autofill 兜底定时器跑完
  out.caseBAfter400ms = snap();

  // ---- D：没有任何变更 → 不发请求、收起 ----
  calls.length = 0;
  els["pc-test"].handlers.click();
  await flush(); await flush();
  out.caseD = { ...snap(), calls: calls.slice() };

  // ---- F：再次打开（原地）----
  openChannel();
  out.caseF = snap();

  // ---- D3：只改 Bot ID（Secret 留空 = 沿用旧 Secret）----
  calls.length = 0;
  els["pc-bot-id"].value = "bot-changed";
  els["pc-test"].handlers.click();
  await flush(); await flush(); await flush();
  out.caseD3 = { ...snap(), calls: calls.slice() };
  channels[0] = channelBody({ bot_id: "bot-changed", bot_secret: "********" }, 1, "企业微信");

  // ---- D4：填新 Secret → 覆写 ----
  openChannel();
  calls.length = 0;
  els["pc-bot-secret"].value = "new-secret";
  els["pc-test"].handlers.click();
  await flush(); await flush(); await flush();
  out.caseD4 = { calls: calls.slice(), panelVisible: vis("panel-channel") };

  // ---- E：更新失败 → 面板保持展开、错误就地 ----
  openChannel();
  calls.length = 0;
  patchStatus = 422;
  els["pc-bot-secret"].value = "bad-secret";
  els["pc-test"].handlers.click();
  await flush(); await flush(); await flush();
  out.caseE = { ...snap(), calls: calls.slice() };
  patchStatus = 200;

  // ---- A2：全新渠道（首次接入 = POST）----
  channels = [];
  await t.refresh();
  openChannel();
  calls.length = 0;
  els["pc-bot-id"].value = "bot-new";
  els["pc-bot-secret"].value = "secret-new";
  els["pc-test"].handlers.click();
  await flush(); await flush(); await flush();
  out.caseA2 = { calls: calls.slice(), panelVisible: vis("panel-channel"),
                 note: els["home-detail"].textContent, hintVisible: vis("pc-saved-hint") };

  process.stdout.write(JSON.stringify(out));
})().catch(function (e) {
  process.stderr.write(String((e && e.stack) || e));
  process.exit(2);
});
"""


def _run() -> dict:
    env = dict(os.environ)
    env["PROBE_JS"] = str(HOME_JS)
    proc = subprocess.run(
        [_NODE, "-e", _JS_HARNESS], capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env, check=False)
    assert proc.returncode == 0, f"node 执行失败：{proc.stderr}"
    return json.loads(proc.stdout)


@needs_node
def test_e1_case_a_new_channel_fields_are_empty() -> None:
    """A：还没有渠道时，面板字段为空、没有「已保存」提示。"""
    case = _run()["caseA"]
    assert case["botId"] == "" and case["secret"] == ""
    assert case["hintVisible"] is False
    assert case["panelVisible"] is True


@needs_node
def test_e2_case_b_saved_channel_prefills_bot_id_only() -> None:
    """B：已保存渠道 → Bot ID 回填；Secret 恒为空且给「已保存」提示。"""
    result = _run()
    case = result["caseB"]
    assert case["botId"] == "bot-saved", case["botId"]
    assert case["secret"] == "", "Secret 绝不能被回显"
    assert case["hintVisible"] is True
    assert "已保存" in case["hint"] and "新的 Bot Secret" in case["hint"], case["hint"]
    # 兜底定时器不得清掉这个**已保存**的回填值
    assert result["caseBAfter400ms"]["botId"] == "bot-saved"


@needs_node
def test_e3_case_d_no_change_does_not_call_api_and_collapses() -> None:
    """D：没有变更 → 不发任何写请求、不谎报测试通过、面板收起。"""
    case = _run()["caseD"]
    assert case["calls"] == [], case["calls"]
    assert case["panelVisible"] is False, "成功后必须自动收起"
    assert "已保存" in case["note"], case["note"]


@needs_node
def test_e4_case_f_reopen_prefills_in_place() -> None:
    """F：点「查看 / 更换」→ 面板原地再次展开并回填。"""
    case = _run()["caseF"]
    assert case["panelVisible"] is True
    assert case["botId"] == "bot-saved"
    assert case["secret"] == ""


@needs_node
def test_e5_case_d3_bot_id_only_keeps_old_secret() -> None:
    """D3：只改 Bot ID → PATCH 只提交 bot_id，**不提交** bot_secret（merge 保留旧 Secret）。"""
    case = _run()["caseD3"]
    writes = [c for c in case["calls"] if c["method"] in ("PATCH", "POST")]
    assert len(writes) == 1 and writes[0]["method"] == "PATCH", writes
    assert writes[0]["url"].endswith("/api/channels/1"), writes[0]["url"]
    assert writes[0]["payload"] == {"config": {"bot_id": "bot-changed"}}, writes[0]["payload"]
    assert case["panelVisible"] is False, "更新成功后必须自动收起"
    assert "已更新" in case["note"], case["note"]


@needs_node
def test_e6_case_d4_new_secret_overwrites() -> None:
    """D4：填了新 Secret → PATCH 提交新 Secret（覆写）。"""
    case = _run()["caseD4"]
    writes = [c for c in case["calls"] if c["method"] == "PATCH"]
    assert len(writes) == 1, writes
    assert writes[0]["payload"]["config"].get("bot_secret") == "new-secret"
    assert writes[0]["payload"]["config"].get("bot_id") == "bot-changed"
    assert case["panelVisible"] is False


@needs_node
def test_e7_case_e_failure_keeps_panel_open_with_inline_error() -> None:
    """E：更新失败 → 面板保持展开、错误就地显示、连接线为失败态。"""
    case = _run()["caseE"]
    assert case["panelVisible"] is True, "失败时必须保持展开"
    assert case["errorVisible"] is True
    assert "Bot ID" in case["error"] or "Bot Secret" in case["error"], case["error"]
    assert case["status"].startswith("连接失败"), case["status"]


@needs_node
def test_e8_case_a2_new_channel_uses_post() -> None:
    """A2：全新渠道（首次接入）→ 仍走 POST 新建，成功后收起。"""
    case = _run()["caseA2"]
    writes = [c for c in case["calls"] if c["method"] in ("PATCH", "POST")]
    assert len(writes) == 1 and writes[0]["method"] == "POST", writes
    assert writes[0]["url"].endswith("/api/channels")
    assert writes[0]["payload"]["config"] == {"bot_id": "bot-new",
                                             "bot_secret": "secret-new"}
    assert case["panelVisible"] is False
    assert case["hintVisible"] is False


@needs_node
def test_e9_channel_ready_is_not_flow_connected() -> None:
    """G/H：渠道就绪（蓝）≠ 已接通；本任务的改动不得把它画成 Connected。"""
    result = _run()
    ready = result["caseB"]
    assert ready["badge"] != "已接通", ready["badge"]
    assert ready["verifyHidden"] is True
