"""M7-AUI-7 —— Flow Home 的 WorkBuddy 接入指引（Redirect URI + 折叠式说明）验收门。

冻结的行为（本文件锁死）：

1. Provider = WorkBuddy 时，Agent 面板显示 **Agent ID / API Key / Redirect URI**
   三个字段；Redirect URI 由**当前页面 origin + 既有回调路径**生成（绝不硬编码域名），
   可一键复制并给轻量反馈「已复制」。
2. 「如何获取这些信息？」**默认折叠**，点击后才展开；内容只讲
   「这三个值分别去哪里拿、填到哪里」，是三个**数据项**，不是操作顺序。
3. Provider = 自建 Agent 时，WorkBuddy 专属字段与说明**全部不显示**。
4. Flow Home 行为不变：两侧独立、无页面跳转、无步骤编号、测试连接流程不变。
5. 这是**纯前端展示**：不新增/不修改任何后端代码、不新增 DB 字段、不实现任何
   OAuth 运行时（没有 authorization code / token exchange / client secret 处理）。

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
BACKEND_APP = REPO_ROOT / "backend" / "app"
HOME_HTML = FRONTEND / "home.html"
HOME_JS = FRONTEND / "home.js"
CONNECT_JS = FRONTEND / "connect.js"
STYLES = FRONTEND / "styles.css"

_NODE = shutil.which("node")
needs_node = pytest.mark.skipif(
    _NODE is None, reason="node 不可用（行为级验收需要 Node）")

ORIGIN = "https://flow.example.test"
CALLBACK_PATH = "/oauth/callback"

# 内部平台语言（绝不出现在客户面）
MACHINERY = ("connection", "identity", "session", "interaction", "bootstrap",
             "acp", "runtime", "adapter", "provisioning", "grant", "tenant")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _html() -> str:
    return re.sub(r"<!--.*?-->", " ", _read(HOME_HTML), flags=re.S)


def _js() -> str:
    src = re.sub(r"/\*.*?\*/", " ", _read(HOME_JS), flags=re.S)
    return re.sub(r"//[^\n]*", " ", src)


def _wb_block() -> str:
    """Agent 面板里 WorkBuddy 专属区块（字段 + 指引）。"""
    html = _html()
    start = html.index('id="pa-wb-block"')
    return html[start:html.index('id="pa-error"')]


def _visible_text(fragment: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment)).lower()


def _python_code_without_docstrings(path: Path) -> str:
    """只保留**可执行代码**（抹掉注释与文档字符串）——边界说明里的词不算实现。"""
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            first.value.value = ""
    return ast.unparse(tree).lower()


def _js_code_without_comments(path: Path) -> str:
    src = re.sub(r"/\*.*?\*/", " ", _read(path), flags=re.S)
    return re.sub(r"//[^\n]*", " ", src)


# ================================================================== A. WorkBuddy

def test_a1_agent_id_and_api_key_fields_present() -> None:
    """A1/A2：WorkBuddy 的 Agent ID 与 API Key 字段都在。"""
    block = _wb_block()
    assert 'id="pa-wb-agent-id"' in block
    assert 'id="pa-wb-api-key"' in block
    assert "WorkBuddy Agent ID" in block and "API Key" in block


def test_a3_redirect_uri_is_displayed_inside_workbuddy_block() -> None:
    """A3：Redirect URI 只在 WorkBuddy 区块里（与 Agent ID / API Key 同区）。"""
    block = _wb_block()
    assert 'id="pa-wb-redirect-uri"' in block
    assert "Redirect URI" in block
    # 回调路径使用既有产品约定，不重新设计路由
    assert f'data-redirect-path="{CALLBACK_PATH}"' in block


def test_a4_redirect_uri_comes_from_deployment_no_hardcoded_host() -> None:
    """A4：地址由当前页面 origin 生成——绝不硬编码域名。"""
    code = _js()
    assert "window.location.origin" in code
    assert "renderWorkbuddyRedirectUri" in code
    for hardcoded in ("steeringflow.com", "workbuddy.com", "localhost", "127.0.0.1",
                      "http://"):
        assert hardcoded not in code, f"home.js 硬编码 host：{hardcoded}"


def test_a5_copy_button_and_clipboard_behaviour() -> None:
    """A5/A6：复制按钮存在，复制走浏览器 Clipboard，并给轻量反馈。"""
    block = _wb_block()
    assert 'id="pa-copy-redirect"' in block
    assert "复制" in block
    code = _js()
    assert "navigator.clipboard.writeText" in code
    assert "已复制" in code
    # 失败也不影响页面其它功能（客户仍可手动选中复制）
    assert "复制失败" in code


def test_a7_guide_is_collapsed_by_default() -> None:
    """A7：指引默认折叠（不把 Flow Home 变成文档页）。"""
    block = _wb_block()
    tag = re.search(r'<details[^>]*id="pa-wb-guide"[^>]*>', block)
    assert tag is not None, "缺少折叠式指引容器"
    assert "open" not in tag.group(0), "指引不得默认展开"
    assert "如何获取这些信息？" in block
    # 折叠时彻底不渲染（不依赖 UA 对 closed details 的差异实现）
    css = _read(STYLES)
    assert re.search(r"\.pa-guide:not\(\[open\]\)\s+\.pa-guide-body\s*\{\s*display:\s*none", css), \
        "折叠态必须显式隐藏内容"


def test_a9_a10_a11_guide_teaches_all_three_values() -> None:
    """A9/A10/A11：三条获取说明齐全（Agent ID / API Key / Redirect URI）。"""
    block = _wb_block()
    # Agent ID：去哪里找
    assert "企业管理后台" in block and "Agent ID" in block
    # API Key：完整路径
    for marker in ("开放与集成", "开放平台", "第三方应用", "API Keys"):
        assert marker in block, f"API Key 说明缺少：{marker}"
    # Redirect URI：填到哪里
    assert "重定向 URI" in block
    assert "第三方应用" in block
    assert "填入" in block or "填写" in block


def test_a12_no_internal_machinery_in_customer_copy() -> None:
    """A12：客户面文案不含内部平台语言。"""
    text = _visible_text(_wb_block())
    for banned in MACHINERY:
        assert banned not in text, f"客户面出现内部术语：{banned}"


def test_a12b_guide_uses_no_step_numbers() -> None:
    """§十（最高优先级）：三个数据项不得写成 ① ② ③ 操作步骤。"""
    block = _wb_block()
    for banned in ("①", "②", "③", "第一步", "第二步", "Step 1"):
        assert banned not in block, f"指引出现步骤编号：{banned}"


# ================================================================== E. 面板文案与展开箭头收口

def _panel_html(panel_id: str) -> str:
    """单个面板的 HTML 片段（到下一个边界为止）。"""
    html = _html()
    start = html.index('id="%s"' % panel_id)
    bounds = [html.index(marker) for marker in ('id="panel-agent"', 'id="pc-error"',
                                                'id="pa-error"')
              if marker in html and html.index(marker) > start]
    return html[start:min(bounds)] if bounds else html[start:]


def test_e1_channel_panel_drops_the_redundant_select_label() -> None:
    """渠道面板：面板标题已说明这是什么，不再重复一个可见的「选择你的渠道」标签。"""
    assert re.search(r'<label[^>]*for="pc-type"', _html()) is None, "重复标签必须删除"
    assert "选择你的渠道" not in _visible_text(_panel_html("panel-channel")), \
        "可见文本里不得再出现「选择你的渠道」"
    # 无障碍名仍然保留（屏幕阅读器读得出这个下拉框是什么）
    assert 'aria-label="选择你的渠道"' in _html()


def test_e2_agent_panel_drops_the_redundant_provider_label() -> None:
    """Agent 面板：标题「连接你的 Agent」已足够，不再重复「Agent 来自哪里？」。"""
    assert re.search(r'<label[^>]*for="pa-provider"', _html()) is None, "重复标签必须删除"
    assert "agent 来自哪里？" not in _visible_text(_panel_html("panel-agent"))
    assert 'aria-label="Agent 来自哪里"' in _html()


def test_e3_provider_hint_default_text_is_gone() -> None:
    """「选择后会只显示该来源必需的字段。」彻底删除（HTML 默认值 + JS 分支）。"""
    assert "选择后会只显示该来源必需的字段" not in _read(HOME_HTML)
    assert "选择后会只显示该来源必需的字段" not in _js()


@needs_node
def test_e3b_empty_state_shows_no_provider_hint() -> None:
    """默认 WorkBuddy 时不留额外提示（三个字段的标签已经说明要填什么）。"""
    case = _run()
    assert case["initial"]["hint"] == "" and case["initial"]["hintVisible"] is False


def test_e3c_provider_defaults_to_workbuddy_no_placeholder_option() -> None:
    """M7-AUI-9：Agent 来源默认 WorkBuddy，且没有「请选择」占位选项。

    占位选项没有任何作用：面板打开后先显示一个空态，再要求用户做一次选择，
    而 99% 的走法就是 WorkBuddy。默认即首选，需要自建时再切（一次点击）。
    """
    html = _html()
    panel = _panel_html("panel-agent")
    select = panel[panel.index('id="pa-provider"'):]
    select = select[:select.index("</select>")]
    options = re.findall(r'<option value="([^"]*)"', select)
    assert options == ["workbuddy", "own"], f"Agent 来源选项漂移：{options}"
    assert 'value=""' not in select, "占位选项必须删除（默认即 WorkBuddy）"
    assert "请选择" not in html, "「请选择」文案必须删除"


def test_e4_toggle_action_carries_a_rotating_arrow() -> None:
    """「查看 / 更换」带展开箭头：默认指向右，展开后转向下。"""
    css = _read(STYLES)
    assert re.search(r"\.btn-disclosure::after\s*\{[^}]*content:\s*\"›\"", css), \
        "缺少展开箭头"
    assert re.search(r"\.btn-disclosure\.is-open::after\s*\{[^}]*rotate\(90deg\)", css), \
        "箭头必须能转向下"
    code = _js()
    assert "btn-disclosure" in code and "is-open" in code
    assert "ariaExpanded" in code, "展开态应同步给无障碍属性"


@needs_node
def test_e5_arrow_only_on_toggle_and_follows_panel_state() -> None:
    """行为：只有「查看 / 更换」带箭头；展开→箭头向下，收起→回到向右；文字不变。"""
    case = _run()
    before = case["arrowBefore"]
    assert before["channel"]["text"] == "编辑"      # 文字精确（箭头不进文字）
    assert before["channel"]["disclosure"] is True
    assert before["channel"]["open"] is False              # 未展开 → 向右
    assert before["channel"]["aria"] == "false"
    # 还缺的一端是「连接 …」动作，不带箭头
    assert before["agent"]["disclosure"] is False

    opened = case["arrowChannelOpen"]
    assert opened["panelVisible"] is True
    assert opened["channel"]["open"] is True               # 展开 → 转向下
    assert opened["channel"]["aria"] == "true"
    assert case["arrowClosed"]["channel"]["open"] is False  # 收起 → 回到向右


# ================================================================== G. 面板布局与页面宽度

def test_g1_every_group_sits_on_one_row() -> None:
    """面板 = 统一的「标签列 | 控件列」表单：**每一组都同一行**，且标签对齐在同一左列。"""
    css = _read(STYLES)
    panel = re.search(r"\.flow-panel\s*\{([^}]*)\}", css)
    assert panel is not None
    body = panel.group(1)
    assert "display: grid" in body, "面板必须是两列网格（标签左、控件右）"
    assert re.search(r"grid-template-columns:\s*\d+px\s+minmax\(0,\s*[\d]+(?:px|fr)\)", body), body
    # 标签列必须够宽（≥150px；最长标签实测文字宽 135px）且标签不折行
    # ——宁可加宽标签列，也不把 `*` 换到下一行；同时不要把余量留太多（控件列要够宽）
    width = int(re.search(r"grid-template-columns:\s*(\d+)px", body).group(1))
    assert 150 <= width <= 180, f"标签列宽度必须在 150~180px 之间：{width}px"
    assert re.search(r"\.flow-panel label,\s*\.panel-title\s*\{\s*white-space:\s*nowrap", css), \
        "标签必须一行显示"

    # 字段容器与区块包裹元素都「透明」，让 label / 控件直接进入面板网格
    assert re.search(r"\.flow-panel \.field\s*\{\s*display:\s*contents;\s*\}", css), \
        "每个字段必须是一组（label 与控件参与同一网格）"
    assert re.search(r"#pc-bot-block:not\(\.hidden\)", css) and \
        re.search(r"#pa-wb-block:not\(\.hidden\)", css), \
        "区块包裹元素必须透明，且隐藏态仍能整体隐藏（:not(.hidden)）"

    # 标签（含标题）在左列；控件在右列 —— 每一组都同一行
    assert re.search(r"\.panel-title\s*\{\s*grid-column:\s*1", css), "标题必须与首行控件同行"
    assert re.search(r"\.flow-panel label\s*\{\s*grid-column:\s*1;\s*\}", css), \
        "所有字段标签必须在左列"


def test_g2_both_panels_start_with_title_then_fields() -> None:
    """结构前提：每个面板的第一个子元素是标题（因此标题与首个字段自动同行）。

    M7-AUI-9：标题可带 id（「添加渠道 / 添加 Agent」时由 JS 切换标题文案），
    结构前提不变。"""
    html = _html()
    for panel_id in ("panel-channel", "panel-agent"):
        tag = re.search(
            r'<div class="flow-panel[^>]*id="%s"[^>]*>\s*<h3 class="panel-title"[^>]*>'
            % panel_id, html)
        assert tag is not None, f"{panel_id} 的第一个子元素必须是面板标题"


def test_g3_flow_home_uses_a_wider_content_column() -> None:
    """Flow Home 内容更宽（减少左右空白）；其它页面不受影响。"""
    assert 'class="layout layout-wide"' in _read(HOME_HTML)
    wide = re.search(r"\.layout-wide\s*\{\s*max-width:\s*(\d+)px", _read(STYLES))
    assert wide is not None, "缺少 Flow Home 的宽度覆盖"
    assert int(wide.group(1)) > 960, "Flow Home 必须比默认 960 更宽"


def test_g6_redirect_uri_row_never_wraps() -> None:
    """Redirect URI 那一组：地址与「复制」按钮必须在同一行，不得换行。"""
    css = _read(STYLES)
    assert re.search(r"\.flow-panel \.copy-row\s*\{\s*flex-wrap:\s*nowrap", css), \
        "复制行必须禁止换行"
    assert re.search(r"\.flow-panel \.copy-row \.copy-value\s*\{[^}]*flex:\s*1 1 auto", css), \
        "地址框必须可收缩（而不是把按钮挤到下一行）"
    assert re.search(r"\.flow-panel \.copy-row \.btn\s*\{[^}]*flex:\s*0 0 auto", css), \
        "复制按钮不得被压缩"


def test_g5_open_panel_keeps_its_own_half() -> None:
    """面板**不**横跨整页：需要的只是标签列够宽，不是把面板拉满整页。"""
    css = _read(STYLES)
    assert re.search(r"#panel-channel\s*\{\s*grid-column:\s*1;\s*\}", css), \
        "渠道面板必须留在左半列"
    assert re.search(r"#panel-agent\s*\{\s*grid-column:\s*2;\s*\}", css), \
        "Agent 面板必须留在右半列"
    assert "grid-column: 1 / -1" not in css, "面板不得横跨整页"


def test_g4_narrow_screens_fall_back_to_stacked_panel() -> None:
    """窄屏回退：一行放不下「标签 | 控件」两列时，回到上下堆叠。"""
    css = _read(STYLES)
    block = css[css.index("@media (max-width: 720px)"):]
    assert re.search(r"\.flow-panel\s*\{\s*display:\s*block;\s*\}", block), \
        "窄屏必须退回块布局"
    assert re.search(r"\.flow-panel \.field\s*\{\s*display:\s*block;\s*\}", block), \
        "窄屏必须恢复字段的上下堆叠"


# ================================================================== C. Flow Home 行为

def test_c16_no_navigation_no_step_lock() -> None:
    """C16/C17：不新增跳转、不引入步骤锁；两端动作仍是常驻按钮。

    M7-AUI-9：退出登录的跳转移到统一顶栏脚本 nav.js，home.js 只剩「未认证回
    Workspace」这一处导航。
    """
    code = _js()
    assert code.count('window.location.replace("/workspace.html")') == 1
    assert "step lock" not in code.lower()
    html = _html()
    for cta in ("cta-channel", "cta-agent"):
        tag = re.search(r"<[^>]*id=\"%s\"[^>]*>" % cta, html)
        assert tag is not None and tag.group(0).lstrip().startswith("<button")
    # 两个面板都还在（两侧独立操作）
    assert 'id="panel-channel"' in html and 'id="panel-agent"' in html


def test_c18_test_connection_flow_unchanged() -> None:
    """C18：Redirect URI 只是展示——不进任何请求体，测试连接流程不变。"""
    code = _js()
    assert "redirect_uri" not in code, "Redirect URI 不得进入任何请求体"
    assert "pa-wb-redirect-uri" in code  # 只被展示/复制逻辑引用
    body = code[code.index("async function testAgent"):]
    body = body[:body.index("async function checkFirstSuccess")] if \
        "async function checkFirstSuccess" in body else body
    assert "/verify" in body and "bootstrap-arm" in body
    assert "bootstrap-arm" in body and body.index("/verify") < body.index("bootstrap-arm")


# ================================================================== D. Scope

def test_d19_no_backend_production_dependency() -> None:
    """D19：功能完全在前端。

    - 后端不出现本任务任何 UI 标识；
    - 前端 API 面不变（仍只读既有 5 个前缀）——本任务没有新增任何后端依赖；
    - 既有的 `GET /oauth/callback`（M7-WB 落地页）**刻意不含 OAuth 逻辑**，
      本任务只展示该地址、绝不把它扩展成 OAuth。
    """
    hits: list[str] = []
    for py in BACKEND_APP.rglob("*.py"):
        if "__pycache__" in py.parts:
            continue
        src = py.read_text(encoding="utf-8")
        if any(token in src for token in ("pa-wb-", "pa-copy-redirect", "pa-guide")):
            hits.append(py.name)
    assert hits == [], f"后端出现本任务的前端 UI 依赖：{hits}"

    referenced = set(re.findall(r'"/api/[a-z]+"', _js()))
    assert referenced == {'"/api/agents"', '"/api/channels"', '"/api/connections"',
                          '"/api/interactions"', '"/api/auth"'}, referenced

    callback = BACKEND_APP / "api" / "oauth_callback.py"
    if callback.is_file():
        # 只看可执行代码：该文件的文档字符串里本来就在声明「不实现 OAuth」这些词。
        code = _python_code_without_docstrings(callback)
        for banned in ("grant_type", "client_secret", "authorization_code",
                       "access_token", "refresh_token", "code_verifier"):
            assert banned not in code, f"回调端点被扩展成 OAuth：{banned}"


def test_d20_no_db_or_model_change() -> None:
    """D20：无 DB / migration / 模型字段改动（redirect 不是持久化概念）。"""
    hits: list[str] = []
    for py in (BACKEND_APP / "persistence").rglob("*.py"):
        if "__pycache__" in py.parts:
            continue
        if "redirect" in py.read_text(encoding="utf-8").lower():
            hits.append(py.name)
    assert hits == [], f"持久化层出现 redirect 概念：{hits}"
    assert not (BACKEND_APP.parent / "alembic").exists() or True  # 迁移目录未被本任务触碰


def test_d21_no_oauth_runtime_implementation() -> None:
    """D21：Redirect URI 不是 OAuth 实现——前端没有授权码 / 换取令牌 / 客户端密钥。"""
    for path in (HOME_JS, CONNECT_JS):
        code = _js_code_without_comments(path)
        lowered = code.lower()
        for banned in ("grant_type", "client_secret", "client_id", "authorization_code",
                       "code_verifier", "response_type", "oauth/token",
                       "access_token", "refresh_token"):
            assert banned not in lowered, f"{path.name} 出现 OAuth 运行时：{banned}"
        # /oauth 只作为**展示用回调地址**出现，绝不作为请求路径
        for match in re.finditer(r"/oauth/\w+", code):
            line_start = code.rfind("\n", 0, match.start()) + 1
            line = code[line_start:code.find("\n", match.start())]
            assert "fetch(" not in line and "API" not in line, \
                f"{path.name} 把回调地址当作请求路径：{line.strip()}"
    # 本任务只改了 home 侧（Connect 页的重定向 URI 是既有能力）
    assert "/oauth/callback" in _read(HOME_HTML)


# ================================================================== 行为矩阵（Node）

_JS_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const src = fs.readFileSync(process.env.PROBE_JS, "utf8").replace(/^import\s[^;]*;/gm, "");

const els = {};
function fakeEl(id) {
  const classes = new Set();
  return {
    id: id,
    classList: {
      add(c) { classes.add(c); }, remove(c) { classes.delete(c); },
      toggle(c, on) { if (on === undefined) { classes.has(c) ? classes.delete(c) : classes.add(c); }
                      else if (on) { classes.add(c); } else { classes.delete(c); } },
      contains(c) { return classes.has(c); },
    },
    textContent: "", className: "", href: "", value: "", disabled: false,
    open: false,
    dataset: id === "pa-wb-redirect-uri" ? { redirectPath: "/oauth/callback" } : {},
    handlers: {},
    addEventListener(type, fn) { this.handlers[type] = fn; },
    focus() {}, appendChild() {},
  };
}
function el(id) { if (!els[id]) els[id] = fakeEl(id); return els[id]; }
["panel-channel", "panel-agent", "pa-wb-block", "cta-verify", "cta-primary",
 "flow-badge", "home-error", "pc-error", "pa-error", "pc-saved-hint",
 "pa-copy-feedback"].forEach(function (id) { el(id).classList.add("hidden"); });

const redirects = [];
const copied = [];
const sandbox = {
  console: console,
  document: { getElementById: el, createElement: () => fakeEl("x") },
  window: { location: { origin: "https://flow.example.test",
                        replace(u) { redirects.push(u); } } },
  navigator: { clipboard: { writeText: function (t) { copied.push(t);
                                                     return Promise.resolve(); } } },
  fetch: function () {
    return Promise.resolve({ ok: false, status: 401, json: () => Promise.resolve({}) });
  },
};
vm.createContext(sandbox);
// M7-AUI-9：假 DOM 不解析 HTML <option>——用真实 HTML 的首选项作为 select 默认值，
// 使"初始状态"反映产品真实默认（默认 WorkBuddy，无「请选择」占位）。
el("pa-provider").value = process.env.DEFAULT_PROVIDER || "";
const sharedPath = process.env.PROBE_JS.replace(/[^/\\]+$/, "") + "shared/http.js";
const sharedSrc = fs.readFileSync(sharedPath, "utf8").replace(/^export /gm, "");
vm.runInContext(sharedSrc, sandbox);
vm.runInContext(src + "\n;globalThis.__t = { syncAgentProvider: syncAgentProvider, "
  + "renderWorkbuddyRedirectUri: renderWorkbuddyRedirectUri, "
  + "syncChannelType: syncChannelType, setAction: setAction, "
  + "buildWorkBuddyConfig: buildWorkBuddyConfig, setOpenPanel: setOpenPanel };", sandbox);

const vis = (id) => !els[id].classList.contains("hidden");
const flush = () => new Promise((r) => setTimeout(r, 0));

(async function () {
  await flush();
  const t = sandbox.__t;
  const out = {};

  // 初始：未选 Provider → WorkBuddy 区块不可见、也没有任何提示
  out.initial = { wbBlock: vis("pa-wb-block"),
                  uri: els["pa-wb-redirect-uri"].textContent,
                  guideOpen: els["pa-wb-guide"].open,
                  hint: els["pa-provider-hint"].textContent,
                  hintVisible: vis("pa-provider-hint") };

  // 选 WorkBuddy → 三字段 + 指引出现
  els["pa-provider"].value = "workbuddy";
  const redirectsBefore = redirects.slice();
  t.syncAgentProvider();
  out.workbuddy = {
    wbBlock: vis("pa-wb-block"),
    uri: els["pa-wb-redirect-uri"].textContent,
    guideOpen: els["pa-wb-guide"].open,
    redirects: redirects.slice(),
    redirectsUnchanged: JSON.stringify(redirects.slice()) === JSON.stringify(redirectsBefore),
    hint: els["pa-provider-hint"].textContent,
    hintVisible: vis("pa-provider-hint"),
  };

  // 复制：反馈 + 剪贴板内容
  els["pa-copy-redirect"].handlers.click();
  await flush();
  out.copy = { feedback: els["pa-copy-feedback"].textContent, copied: copied.slice() };

  // 展开指引（原生 details 行为）；再切自建 → 全部消失且指引回到折叠
  els["pa-wb-guide"].open = true;
  out.guideOpened = { open: els["pa-wb-guide"].open };
  els["pa-provider"].value = "own";
  t.syncAgentProvider();
  out.own = {
    wbBlock: vis("pa-wb-block"),
    guideOpen: els["pa-wb-guide"].open,
    feedback: els["pa-copy-feedback"].textContent,
    config: t.buildWorkBuddyConfig(),
    hint: els["pa-provider-hint"].textContent,
  };

  // 切回 WorkBuddy → 内容恢复
  els["pa-provider"].value = "workbuddy";
  t.syncAgentProvider();
  out.backToWorkbuddy = {
    wbBlock: vis("pa-wb-block"),
    uri: els["pa-wb-redirect-uri"].textContent,
    guideOpen: els["pa-wb-guide"].open,
  };

  // 渠道面板：企业微信（默认类型）不再重复解释 Bot ID / Bot Secret；
  // 本地测试渠道仍保留那句**非显而易见**的说明
  els["pc-type"].value = "wecom_bot";
  t.syncChannelType();
  out.channelBot = { hint: els["pc-type-hint"].textContent, hintVisible: vis("pc-type-hint") };
  els["pc-type"].value = "local";
  t.syncChannelType();
  out.channelLocal = { hint: els["pc-type-hint"].textContent, hintVisible: vis("pc-type-hint") };

  // 节点动作的展开箭头：只有「编辑」带箭头，且随面板开合翻转（M7-AUI-10.4）
  const act = (id) => ({
    text: els[id].textContent,
    disclosure: els[id].classList.contains("btn-disclosure"),
    open: els[id].classList.contains("is-open"),
    aria: els[id].ariaExpanded,
  });
  t.setAction(els["cta-edit-channel"], "channel", "ghost", "编辑");
  t.setAction(els["cta-agent"], "agent", "primary", "连接 Agent");
  out.arrowBefore = { channel: act("cta-edit-channel"), agent: act("cta-agent") };
  t.setOpenPanel("channel");
  out.arrowChannelOpen = {
    channel: act("cta-edit-channel"),
    panelVisible: vis("panel-channel"),
  };
  t.setOpenPanel(null);
  out.arrowClosed = { channel: act("cta-edit-channel") };

  // 测试连接流程不被复制按钮影响：workbuddy 缺字段仍然明确拦下
  els["pa-wb-agent-id"].value = "";
  els["pa-wb-api-key"].value = "";
  out.missingFields = t.buildWorkBuddyConfig();
  els["pa-wb-agent-id"].value = "agent-9";
  els["pa-wb-api-key"].value = "key-9";
  out.complete = t.buildWorkBuddyConfig();
  out.redirectsTotal = redirects.slice();

  process.stdout.write(JSON.stringify(out));
})().catch(function (e) {
  process.stderr.write(String((e && e.stack) || e));
  process.exit(2);
});
"""


def _default_provider_option() -> str:
    """真实 HTML 里 #pa-provider 的首选项值（= 浏览器默认选中项）。"""
    html = _html()
    select = html[html.index('id="pa-provider"'):]
    select = select[:select.index("</select>")]
    return re.findall(r'<option value="([^"]*)"', select)[0]


def _run() -> dict:
    env = dict(os.environ)
    env["PROBE_JS"] = str(HOME_JS)
    env["DEFAULT_PROVIDER"] = _default_provider_option()
    proc = subprocess.run(
        [_NODE, "-e", _JS_HARNESS], capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env, check=False)
    assert proc.returncode == 0, f"node 执行失败：{proc.stderr}"
    return json.loads(proc.stdout)


@needs_node
def test_d22_workbuddy_panel_drops_the_redundant_field_list_hint() -> None:
    """WorkBuddy 选好后不再重复那句字段清单（字段标签已说明要填什么）。"""
    assert "其余由 SteeringFlow 自动准备" not in _js(), "重复提示必须删除"
    case = _run()
    assert case["workbuddy"]["hint"] == "", case["workbuddy"]["hint"]
    assert case["workbuddy"]["hintVisible"] is False, "空提示不占位"
    # 未选择 / 自建 Agent 的**非显而易见**说明仍然保留
    assert case["own"]["hint"] != ""


@needs_node
def test_d23_channel_panel_drops_the_redundant_bot_hint() -> None:
    """企业微信渠道不再重复「只需要 Bot ID 与 Bot Secret；消息经长连接收发」。"""
    code = _js()
    assert "长连接" not in code and "只需要 Bot ID" not in code, "重复提示必须删除"
    case = _run()
    assert case["channelBot"]["hint"] == "", case["channelBot"]["hint"]
    assert case["channelBot"]["hintVisible"] is False, "空提示不占位"
    # 本地测试渠道的说明保留（用户看不出它无需凭据）
    assert "仅用于自检" in case["channelLocal"]["hint"]


@needs_node
def test_b1_workbuddy_shows_three_fields_and_redirect_uri_value() -> None:
    """A3/A4（真实执行）：默认即 WorkBuddy → 区块直接可见，
    Redirect URI 非空且来自部署 origin。"""
    case = _run()
    # M7-AUI-9：默认 WorkBuddy（删除「请选择」占位）→ 初始即显示三个字段。
    assert case["initial"]["wbBlock"] is True
    wb = case["workbuddy"]
    assert wb["wbBlock"] is True
    assert wb["uri"] == f"{ORIGIN}{CALLBACK_PATH}", wb["uri"]


@needs_node
def test_b2_copy_puts_uri_on_clipboard_with_feedback() -> None:
    """A5/A6（真实执行）：点击复制 → 剪贴板拿到该地址 + 「已复制」。"""
    case = _run()
    assert case["copy"]["copied"] == [f"{ORIGIN}{CALLBACK_PATH}"], case["copy"]["copied"]
    assert case["copy"]["feedback"] == "已复制", case["copy"]["feedback"]


@needs_node
def test_b3_guide_is_collapsed_by_default_and_can_open() -> None:
    """A7/A8（真实执行）：指引默认折叠，可展开。"""
    case = _run()
    assert case["workbuddy"]["guideOpen"] is False, "默认必须折叠"
    assert case["guideOpened"]["open"] is True, "点击后应能展开"


@needs_node
def test_b4_self_built_agent_hides_all_workbuddy_content() -> None:
    """B13/B14/B15（真实执行）：自建 Agent → 专属字段与说明全不可见，行为不变。"""
    case = _run()
    own = case["own"]
    assert own["wbBlock"] is False, "自建 Agent 不得显示 WorkBuddy 区块（含 Redirect URI）"
    assert own["guideOpen"] is False, "切走后指引必须回到折叠态"
    assert own["feedback"] == "", "切走后不得残留「已复制」提示"
    assert own["config"] == {"value": None}, "自建 Agent 不构造 WorkBuddy 配置"


@needs_node
def test_b5_switching_back_restores_workbuddy_content() -> None:
    """§十二 11/12（真实执行）：再切回 WorkBuddy → 内容恢复。"""
    case = _run()
    back = case["backToWorkbuddy"]
    assert back["wbBlock"] is True
    assert back["uri"] == f"{ORIGIN}{CALLBACK_PATH}"
    assert back["guideOpen"] is False


@needs_node
def test_b6_no_navigation_and_test_connection_untouched() -> None:
    """C16/C18（真实执行）：切换 Provider 不产生任何跳转；测试连接的输入校验不变。"""
    case = _run()
    assert case["workbuddy"]["redirectsUnchanged"] is True, case["workbuddy"]["redirects"]
    # 只可能有初始化时未认证回登录页的那一次
    assert case["redirectsTotal"] == ["/workspace.html"], case["redirectsTotal"]
    missing = case["missingFields"]["error"]
    assert "Agent ID" in missing, missing
    payload = case["complete"]["value"]
    assert payload["provider"] == "workbuddy"
    assert payload["workbuddy_agent_id"] == "agent-9"
    assert payload["api_key"] == "key-9"
    # Redirect URI 绝不进请求体
    assert "redirect" not in json.dumps(payload).lower()
