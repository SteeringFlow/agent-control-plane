"""M7-AUI-8.2 —— Advanced Surface Reduction 验收门。

本步把 M7-AUI-8.1 的产品表面重新分类落地为一次**表面收敛**（不是删除能力）：

- 统一顶栏（Flow Home + 高级 ▾ + 账户），只有一套实现，按用户任务分组；
- 修复 `href="/"` 的 Agent 管理入口（生产 `/` 会 302 到入口页 → 改为 `/agents.html`；
  `index.html` 只作为产品根入口跳转 `/workspace.html`，与 nginx `location = /` 同语义）；
- 逐页字段分层：能力一个不删，只改「默认是否展开 / 给谁看」；
- 修复 Agents 页面渲染崩溃（`endpoint = null` 时把字符串传给 `appendChild`）；
- 先进错误展示：用户语言默认 + `[查看详细信息]` 折叠技术细节；
- 旧接入页瘦身为纯兼容跳转（HTTP 200 + 客户端跳转），旧脚本删除。

静态源码断言 + 真实应用探针（TestClient）+ Node 行为矩阵（直接执行 `agents.js`
的渲染函数，fake DOM 会像浏览器一样对非节点参数抛错）。
"""

from __future__ import annotations

import json

from tests.oss_export_boundary import oss_ui_required
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND = REPO_ROOT / "frontend"

HOME_HTML = FRONTEND / "home.html"
ROOT_ENTRY_HTML = FRONTEND / "index.html"
WORKSPACE_HTML = FRONTEND / "workspace.html"
CONNECT_HTML = FRONTEND / "connect.html"
CONNECT_JS = FRONTEND / "connect.js"
APP_JS = FRONTEND / "agents.js"

# 管理面（全部保留；只是分层与改名）
AGENTS_HTML = FRONTEND / "agents.html"
CHANNELS_HTML = FRONTEND / "channels.html"
CHANNELS_JS = FRONTEND / "channels.js"
CONNECTIONS_HTML = FRONTEND / "connections.html"
CONNECTIONS_JS = FRONTEND / "connections.js"
USERS_HTML = FRONTEND / "users.html"
IDENTITIES_HTML = FRONTEND / "identities.html"
RUNS_HTML = FRONTEND / "interactions.html"

LEGACY_AGENT_HTML = FRONTEND / "connect-agent.html"
LEGACY_CHANNEL_HTML = FRONTEND / "connect-channel.html"
LEGACY_AGENT_JS = FRONTEND / "connect-agent.js"
LEGACY_CHANNEL_JS = FRONTEND / "connect-channel.js"

STYLES = FRONTEND / "styles.css"

# 统一顶栏的页面集合（home 另有「账户」菜单；connect 是客户面但同一套结构）
NAV_PAGES = (HOME_HTML, AGENTS_HTML, CHANNELS_HTML, CONNECTIONS_HTML,
             USERS_HTML, IDENTITIES_HTML, RUNS_HTML, CONNECT_HTML)

# 菜单应完全一致（顺序即分组顺序）
EXPECTED_NAV = [
    "/channels.html", "/connections.html", "/agents.html",
    "/interactions.html", "/identities.html", "/users.html",
]
EXPECTED_GROUPS = ["接入配置", "运行与排障", "成员"]

_NODE = shutil.which("node")
needs_node = pytest.mark.skipif(
    _NODE is None, reason="node 不可用（渲染回归需要 Node）")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _strip_html_comments(src: str) -> str:
    return re.sub(r"<!--.*?-->", " ", src, flags=re.S)


def _strip_js_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    src = re.sub(r"//[^\n]*", " ", src)
    return src


def _nav_panel(html: str) -> str:
    panel = html[html.index('id="nav-advanced-panel"'):]
    return panel[:panel.index("</details>")]


# ================================================================== A. 统一导航


def test_a1_single_nav_implementation_on_every_page() -> None:
    """所有页面使用同一套顶栏结构；旧的两套实现（.nav-advanced）已消除。"""
    for path in NAV_PAGES:
        html = _strip_html_comments(_read(path))
        assert 'id="nav-advanced"' in html, f"{path.name} 缺少「高级」菜单"
        assert 'id="nav-advanced-panel"' in html, f"{path.name} 缺少「高级」面板"
        assert "nav-menu" in html, f"{path.name} 未使用统一菜单实现"
        assert "nav-advanced-items" not in html, \
            f"{path.name} 仍是旧的第二套导航实现"
        assert "nav-advanced\"" not in html.replace('id="nav-advanced"', ""), \
            f"{path.name} 仍带有旧导航样式类"
    # 旧样式类彻底下线（只有一套实现）
    assert ".nav-advanced-items" not in _read(STYLES)


def test_a2_advanced_menu_is_grouped_and_identical() -> None:
    """「高级」菜单在所有页面上分组一致、条目一致、顺序一致。"""
    for path in NAV_PAGES:
        html = _strip_html_comments(_read(path))
        panel = _nav_panel(html)
        links = re.findall(r'href="([^"]+)"', panel)
        assert links == EXPECTED_NAV, f"{path.name} 菜单漂移：{links}"
        for group in EXPECTED_GROUPS:
            assert group in panel, f"{path.name} 缺少分组：{group}"
        # 菜单文案是用户任务语言，不再是 Control Plane 实体名。
        for banned in ("Interaction Observability", ">Agents<", ">Channels<",
                       ">Connections<", ">Users<", ">Identities<"):
            assert banned not in panel, f"{path.name} 菜单仍是实体名：{banned}"


def test_a3_agent_entry_is_explicit_agents_html_never_root() -> None:
    """Agent 管理入口必须是 /agents.html：生产 `/` 会 302 到入口页，不能当管理入口。

    M7-AUI-9：Agent 管理页从 `index.html` 更名为 `agents.html`（文件名必须表达
    职责）；`index.html` 退化为纯产品根入口，不再承载任何列表 / 表单 / 脚本。
    """
    for path in NAV_PAGES:
        html = _strip_html_comments(_read(path))
        panel = _nav_panel(html)
        assert 'href="/agents.html"' in panel, f"{path.name} 缺少 Agent 管理入口"
        assert 'href="/index.html"' not in panel, f"{path.name} 仍指向根入口页"
        assert 'href="/"' not in panel, f"{path.name} 仍用 / 作为 Agent 入口"
        # 全页也不应出现裸 `/` 链接（避免回到「点进去被送回 Flow Home」）。
        assert 'href="/"' not in html, f"{path.name} 仍存在裸 / 链接"


def test_a4_flow_home_link_present_and_collapsed() -> None:
    """每个页面都能回 Flow Home；「高级」默认折叠（details 未加 open）。"""
    for path in NAV_PAGES:
        html = _strip_html_comments(_read(path))
        assert 'href="/home.html"' in html, f"{path.name} 缺少回 Flow Home 的入口"
        assert "nav-menu-panel" in html
        m = re.search(r'<details class="nav-menu" id="nav-advanced"[^>]*>', html)
        assert m is not None, f"{path.name} 的「高级」菜单结构异常"
        assert " open" not in m.group(0), f"{path.name} 的「高级」默认应为折叠"


def test_a4b_home_read_never_hangs_silently() -> None:
    """Flow Home 读取挂住时必须转为可操作错误（M7-AUI-9）。

    此前：任一请求挂住 → 界面永久停在「正在读取你的连接…」（该文案还是 home.html
    的初始静态文本），用户得不到任何反馈，也无法自助恢复。
    M7-AUI-9 模块化：超时实现在共享层（shared/http.js），home.js 负责措辞与重试出口。
    """
    shared = _strip_js_comments(_read(FRONTEND / "shared" / "http.js"))
    assert "REQUEST_TIMEOUT_MS" in shared, "共享层缺少请求超时上限"
    assert "AbortController" in shared and "controller.abort()" in shared, "缺少可中断的请求"
    js = _strip_js_comments(_read(FRONTEND / "home.js"))
    assert "读取超时" in js, "缺少超时文案"
    # 失败只上屏用户语言；原始技术细节不再拼进提示里
    assert "读取连接信息失败 ——" not in js
    html = _read(HOME_HTML)
    assert 'id="home-retry"' in html, "缺少「重试」出口"


def test_a5_account_menu_on_every_topbar_page() -> None:
    """账户菜单不随页面切换消失（M7-AUI-9）。

    账户属于会话，不属于某个页面：用户从 Flow Home 进入任一高级页后仍必须能
    退出登录。此前它只存在于 Flow Home，切页即消失。
    """
    for path in NAV_PAGES:
        html = _strip_html_comments(_read(path))
        assert 'id="nav-account"' in html, f"{path.name} 缺少「账户」菜单"
        assert 'id="nav-logout"' in html, f"{path.name} 缺少退出登录入口"
        m = re.search(r'<details class="nav-menu" id="nav-account"[^>]*>', html)
        assert m is not None, f"{path.name} 的「账户」菜单结构异常"
        assert " open" not in m.group(0), f"{path.name} 的「账户」默认应为折叠"


def test_a6_logout_has_a_single_shared_implementation() -> None:
    """退出登录只有一套实现：`nav.js` 模块（所有带顶栏的页面经 import 装配）。

    M7-AUI-9 模块化：页面脚本以 `import "/nav.js"` 装配统一顶栏（不再各自挂
    第二个 <script>）；工作区页例外——它的「退出登录」由 workspace.js 处理，
    不引用 nav.js，避免同一元素被绑两次。
    """
    nav_js = _read(FRONTEND / "nav.js")
    assert "/api/auth/logout" in nav_js
    assert 'window.location.replace("/workspace.html")' in nav_js
    for path in NAV_PAGES:
        js_name = {
            HOME_HTML: "home.js", AGENTS_HTML: "agents.js",
            CHANNELS_HTML: "channels.js", CONNECTIONS_HTML: "connections.js",
            USERS_HTML: "users.js", IDENTITIES_HTML: "identities.js",
            RUNS_HTML: "interactions.js", CONNECT_HTML: "connect.js",
        }[path]
        src = _strip_js_comments(_read(FRONTEND / js_name))
        assert 'import "/nav.js"' in src, f"{js_name} 未装配统一顶栏模块"
        html = _read(path)
        assert f'src="{js_name}"' in html, f"{path.name} 未加载页面模块"
    # 页面脚本不再各自实现退出登录
    for name in ("home.js", "agents.js", "channels.js", "connections.js",
                 "users.js", "identities.js", "interactions.js", "connect.js"):
        js = _strip_js_comments(_read(FRONTEND / name))
        assert "auth/logout" not in js, f"{name} 仍自带退出登录实现"
    # 工作区页保持自己的实现（不引用 nav.js）
    workspace_js = _strip_js_comments(_read(FRONTEND / "workspace.js"))
    assert 'import "/nav.js"' not in workspace_js
    workspace = _read(WORKSPACE_HTML)
    assert 'id="nav-logout"' in workspace


# ================================================================== B. Agents 渲染回归（endpoint 可为空）


_APP_JS_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const src = fs.readFileSync(process.env.PROBE_JS, "utf8").replace(/^import\s[^;]*;/gm, "");

const els = {};
function fakeNode(tag) {
  const node = {
    nodeType: 1,
    tagName: tag || "div",
    children: [],
    className: "",
    textContent: "",
    title: "",
    value: "",
    placeholder: "",
    disabled: false,
    classList: {
      add() {}, remove() {}, toggle() {}, contains() { return false; },
    },
    // 像浏览器一样：非节点参数直接抛错（这正是被修复的缺陷形态）。
    appendChild(child) {
      if (child === null || child === undefined || typeof child !== "object") {
        throw new Error("Node.appendChild: Argument 1 is not an object.");
      }
      this.children.push(child);
      return child;
    },
    append(...kids) { for (const k of kids) this.appendChild(k); },
    addEventListener() {}, focus() {}, reset() {},
  };
  return node;
}
function el(id) { if (!els[id]) els[id] = fakeNode("div"); return els[id]; }

const sandbox = {
  console: console,
  document: {
    getElementById: el,
    createElement: (t) => fakeNode(t),
    createTextNode: (t) => ({ nodeType: 3, textContent: String(t) }),
  },
  window: { location: { replace() {} } },
  fetch: function () {
    return Promise.resolve({ ok: false, status: 401, json: () => Promise.resolve({}) });
  },
};
vm.createContext(sandbox);
const sharedPath = process.env.PROBE_JS.replace(/[^/\\]+$/, "") + "shared/http.js";
const sharedSrc = fs.readFileSync(sharedPath, "utf8").replace(/^export /gm, "");
vm.runInContext(sharedSrc, sandbox);
vm.runInContext(src + "\n;globalThis.__t = { renderRow: renderRow, renderList: renderList };", sandbox);

const t = sandbox.__t;
const out = {};
try {
  const rowNull = t.renderRow({ id: 1, name: "workbuddy-agent", type: "acp",
                                endpoint: null, status: "active" });
  out.nullOk = true;
  out.nullTexts = rowNull.children.map((td) => td.textContent);
  out.nullTypeClass = rowNull.children[1].children[0].className;
  out.nullTypeText = rowNull.children[1].children[0].textContent;
  out.nullStatusClass = rowNull.children[2].children[0].className;
} catch (e) {
  out.nullOk = false;
  out.nullError = String(e && e.message);
}
try {
  const rowUrl = t.renderRow({ id: 2, name: "acp-prod", type: "acp",
                               endpoint: "https://agent.example.com/acp",
                               status: "error" });
  out.urlOk = true;
  out.urlTitle = rowUrl.title;
  out.urlStatusText = rowUrl.children[2].children[0].textContent;
} catch (e) {
  out.urlOk = false;
  out.urlError = String(e && e.message);
}
process.stdout.write(JSON.stringify(out));
"""


def _run_app_js_matrix() -> dict:
    env = dict(os.environ)
    env["PROBE_JS"] = str(APP_JS)
    proc = subprocess.run(
        [_NODE, "-e", _APP_JS_HARNESS], capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env, check=False)
    assert proc.returncode == 0, f"node 执行失败：{proc.stderr}"
    return json.loads(proc.stdout)


@needs_node
def test_b1_render_row_accepts_null_endpoint_without_crashing() -> None:
    """真实运行时缺陷回归：`endpoint = null` 是合法状态，渲染不得抛错。

    缺陷形态：把 `"—"`（字符串）传给 `appendChild` →
    `Node.appendChild: Argument 1 is not an object.`
    """
    out = _run_app_js_matrix()
    assert out["nullOk"] is True, f"endpoint=null 渲染崩溃：{out.get('nullError')}"
    assert "workbuddy-agent" in out["nullTexts"]
    # 状态格仍是节点（badge），类型格显示客户语言——两者都用 textContent，不 appendChild 字符串。
    assert "badge-active" in out["nullStatusClass"]
    assert out["nullTypeClass"] == "type-tag"
    assert out["nullTypeText"] == "外部 Agent"


@needs_node
def test_b2_render_row_accepts_string_endpoint() -> None:
    """`endpoint` 为真实地址时渲染正常（技术详情进 title，不上主视觉）。"""
    out = _run_app_js_matrix()
    assert out["urlOk"] is True, f"endpoint=string 渲染崩溃：{out.get('urlError')}"
    assert "https://agent.example.com/acp" in out["urlTitle"]
    # 运行态 error 显示用户语言，不把内部信号当标签
    assert out["urlStatusText"] == "暂时不可用"


def test_b3_agents_page_hides_endpoint_column_and_folds_advanced_fields() -> None:
    """字段分层：地址列不再当主视觉；地址 / 凭据引用 / 配置收进「高级配置」。"""
    html = _strip_html_comments(_read(AGENTS_HTML))
    assert "高级配置" in html
    for marker in ("f-endpoint", "f-credential-ref", "f-config"):
        assert f'id="{marker}"' in html, f"高级配置里缺少 {marker}（能力不得删除）"
    # 列表不再有「地址」列（可选描述性元数据）
    head = html[html.index("<thead>"):html.index("</thead>")]
    assert "地址" not in head
    assert "接入方式" in head and "状态" in head
    # 客户语言：不把 acp / local 当标签
    assert '<option value="acp">外部 Agent</option>' in html
    assert '<option value="local">本地测试</option>' in html


# ================================================================== C. 页面与标题（全部保留）


@oss_ui_required()
def test_c1_all_management_pages_still_served() -> None:
    client = TestClient(app)
    for path in ("/agents.html", "/channels.html", "/connections.html",
                 "/users.html", "/identities.html", "/interactions.html"):
        assert client.get(path).status_code == 200, path
    assert client.get("/agents.js").status_code == 200
    # M7-AUI-9 模块化：共享 HTTP 层可被静态托管（import 解析依赖它可达）
    shared = client.get("/shared/http.js")
    assert shared.status_code == 200
    assert "export async function request" in shared.text


def test_c1b_root_entry_is_product_entry_not_agent_management() -> None:
    """`index.html` = 产品根入口：跳转到 workspace，不得残留任何管理面内容。

    本地 `/` 由 StaticFiles(html=True) 默认返回 index.html，因此必须与生产
    nginx 的 `location = / → 302 /workspace.html` 保持一致——否则本地直出管理页。
    """
    html = _strip_html_comments(_read(ROOT_ENTRY_HTML))
    assert "url=/workspace.html" in html
    assert "/workspace.html" in html
    for banned in ("agent-form", "agent-tbody", "Agent List",
                   "<script src=", "api/agents"):
        assert banned not in html, f"根入口页仍承载管理面内容：{banned}"
    for banned in ("/agents.html", "/channels.html", "/connections.html",
                   "/users.html", "/identities.html", "/interactions.html"):
        assert banned not in html, f"根入口页不应暴露管理入口：{banned}"
    # 本地 `/` 由 StaticFiles(html=True) 默认返回 index.html → 必须与生产一致。
    served = TestClient(app).get("/")
    assert served.status_code == 200
    assert "url=/workspace.html" in served.text
    assert "Agent List" not in served.text


def test_c1c_no_stale_references_to_old_names() -> None:
    """全前端不再引用旧文件名（`app.js` / 指向 `index.html` 的链接）。"""
    for path in sorted(FRONTEND.glob("*.html")) + sorted(FRONTEND.glob("*.js")):
        src = _strip_html_comments(path.read_text(encoding="utf-8"))
        if path.suffix == ".js":
            src = _strip_js_comments(src)
        assert '"app.js"' not in src and "'app.js'" not in src, \
            f"{path.name} 仍引用旧脚本名 app.js"
        assert "/index.html" not in src, f"{path.name} 仍链接根入口页 /index.html"


def test_c2_page_titles_use_task_language() -> None:
    expectations = {
        AGENTS_HTML: "Agent 管理",
        CHANNELS_HTML: "渠道接入",
        CONNECTIONS_HTML: "访问通路",
        USERS_HTML: "成员管理",
        IDENTITIES_HTML: "外部身份",
        RUNS_HTML: "运行记录",
    }
    for path, title in expectations.items():
        html = _read(path)
        assert f"<title>SteeringFlow — {title}</title>" in html, \
            f"{path.name} 页面标题未使用任务语言：{title}"
        assert title in html


def test_c3_page_forms_intact() -> None:
    """页面表单骨架不变（能力保留的证据）。"""
    assert 'id="agent-form"' in _read(AGENTS_HTML)
    assert 'id="channel-form"' in _read(CHANNELS_HTML)
    assert 'id="connection-form"' in _read(CONNECTIONS_HTML)
    assert 'id="user-form"' in _read(USERS_HTML)
    assert 'id="identity-form"' in _read(IDENTITIES_HTML)
    assert 'id="interaction-tbody"' in _read(RUNS_HTML)


# ================================================================== D. 能力保留（分层 ≠ 删除）


def test_d1_channel_advanced_modes_and_sentinel_kept() -> None:
    """渠道高级接入模式仍可用：free-text 接入模式 + 原始配置 + 哨兵语义。"""
    html = _read(CHANNELS_HTML)
    assert '<input type="text" id="f-type"' in html      # 不引入 type 下拉
    assert "接入模式" in html
    assert "高级配置（原始配置）" in html
    assert "********" in html                             # 哨兵语义仍被说明
    js = _strip_js_comments(_read(CHANNELS_JS))
    assert "SECRET_PLACEHOLDER" in js
    assert 'if (value === SECRET_PLACEHOLDER) continue;' in js
    assert "buildConfigPayload(editingId !== null)" in js


def test_d2_connection_enable_disable_kept() -> None:
    """通路启停能力保留（Home 只能建立通路，停用仍只在这里）。"""
    js = _strip_js_comments(_read(CONNECTIONS_JS))
    assert "toggleStatus" in js
    assert '"停用"' in js and '"启用"' in js
    html = _read(CONNECTIONS_HTML)
    assert "高级配置（访问参数 / 凭据引用）" in html
    for marker in ("f-config", "f-cred"):
        assert f'id="{marker}"' in html


def test_d3_run_records_field_layering() -> None:
    """运行记录：列表用中文事实列；内部细节进「技术详情」；双状态不合成。"""
    html = _read(RUNS_HTML)
    head = html[html.index("<thead>"):html.index("</thead>")]
    for label in ("执行状态", "送达状态", "失败环节", "失败原因", "时间", "Channel"):
        assert label in head, f"运行记录列表缺少列：{label}"
    assert "技术详情" in html
    assert "入站消息快照" in html and "回复快照" in html
    js = _strip_js_comments(_read(FRONTEND / "interactions.js"))
    assert "STATUS_LABELS" in js
    assert 'textContent = wireLabel(STATUS_LABELS, status)' in js
    # 双状态仍是两枚独立 chip（不合成）
    assert "detailStatusRow.append" in js
    # 只读面：本页仍只有 GET
    assert "requestGet" in js
    assert '"POST"' not in js and '"PATCH"' not in js


def test_d4_workbuddy_onboarding_unaffected() -> None:
    """WorkBuddy 接入链路不受表面收敛影响（Flow Home + 统一接入页）。"""
    home = _read(HOME_HTML)
    for marker in ("pa-wb-agent-id", "pa-wb-api-key", "pa-wb-redirect-uri",
                   'data-redirect-path="/oauth/callback"'):
        assert marker in home, f"Flow Home 缺少 WorkBuddy 元素：{marker}"
    connect = _read(CONNECT_HTML)
    assert 'id="workbuddy-block"' in connect
    assert 'id="f-wb-agent-id"' in connect and 'id="f-wb-api-key"' in connect
    assert 'id="wb-redirect-uri"' in connect
    assert 'data-redirect-path="/oauth/callback"' in connect
    assert 'id="btn-copy-redirect"' in connect


# ================================================================== E. 客户面语言


def test_e1_customer_surfaces_have_no_internal_terms() -> None:
    """普通客户页面（Flow Home / 入口 / 统一接入）不出现内部 machinery 术语。"""
    banned = ("identity", "session_id", "channel_id", "interaction",
              "adapter", "credential", "runtime", "bootstrap", "grant")
    for path in (HOME_HTML, WORKSPACE_HTML, CONNECT_HTML):
        src = _read(path)
        # 菜单/CTA 的 href 是管理页入口（合法的导航），先剥离再判定用词。
        stripped = re.sub(r'href="[^"]*"', "", src).lower()
        for term in banned:
            assert term not in stripped, f"{path.name} 客户面出现内部术语：{term}"
        # 内部形态代码不作为可见标签（值属性可以保留，但不能显示出来）。
        assert ">acp<" not in src, f"{path.name} 把 acp 当标签展示"
        assert ">local<" not in src, f"{path.name} 把 local 当标签展示"


def test_e2_connect_page_drops_platform_self_description() -> None:
    """Connect 页不再出现 Advanced / Adapter Registry 这类平台自述。"""
    html = _strip_html_comments(_read(CONNECT_HTML))
    assert "Advanced" not in html
    assert "Adapter Registry" not in html
    assert "如需其它企业微信接入方式" in html     # 客户语言指路


def test_e3_advanced_badge_removed_from_agents_page() -> None:
    html = _read(AGENTS_HTML)
    assert "badge-advanced" not in html
    assert "Adapter Registry" not in html
    assert "Advanced" not in html


# ================================================================== F. 错误展示：用户语言 + 技术详情折叠


def test_f1_management_pages_expose_user_language_error_plus_detail_fold() -> None:
    for path in (AGENTS_HTML, CHANNELS_HTML, CONNECTIONS_HTML, USERS_HTML,
                 IDENTITIES_HTML, RUNS_HTML):
        html = _read(path)
        assert "查看详细信息" in html, f"{path.name} 缺少技术详情折叠入口"
        assert "error-detail" in html


def test_f2_scripts_map_http_status_to_user_language() -> None:
    """HTTP 状态 → 用户语言的映射：状态记录在共享层，文案在页面本地（409 各异）。"""
    shared = _strip_js_comments(_read(FRONTEND / "shared" / "http.js"))
    assert "err.status = response.status" in shared, "共享层未记录 HTTP 状态"
    for name in ("agents.js", "channels.js", "connections.js", "users.js",
                 "identities.js", "interactions.js"):
        js = _strip_js_comments(_read(FRONTEND / name))
        assert "function userMessage" in js, f"{name} 缺少用户语言错误映射"
        # 技术细节仍然保留（放进折叠区，而不是删除）
        assert "err.message" in js


def test_f2b_every_script_has_request_timeout() -> None:
    """请求超时兜底收敛到共享模块；页面脚本必须经 import 使用它（M7-AUI-9 模块化）。

    任何一个请求挂住都不允许界面永久停在加载态；失败只上屏用户语言，
    具体是哪个请求挂了打进 console 供排障。
    """
    shared = _strip_js_comments(_read(FRONTEND / "shared" / "http.js"))
    assert "REQUEST_TIMEOUT_MS" in shared, "共享层缺少请求超时上限"
    assert "AbortController" in shared and "controller.abort()" in shared, \
        "共享层请求不可中断"
    assert "console.error" in shared, "共享层缺少排障日志"
    # 页面脚本一律经 import 使用（不再自带网络实现副本）
    for name in ("home.js", "nav.js", "workspace.js", "connect.js",
                 "agents.js", "channels.js", "connections.js",
                 "users.js", "identities.js", "interactions.js"):
        js = _strip_js_comments(_read(FRONTEND / name))
        assert 'from "/shared/http.js"' in js, f"{name} 未接入共享 HTTP 层"
        assert "async function request(" not in js, \
            f"{name} 仍自带 request 实现（应使用共享模块）"
    # 超时 / 断网是用户语言，不是技术堆栈
    for name in ("agents.js", "channels.js", "connections.js",
                 "users.js", "identities.js", "interactions.js", "connect.js"):
        js = _strip_js_comments(_read(FRONTEND / name))
        assert "读取超时" in js, f"{name} 缺少超时文案"
        assert "网络连接失败" in js, f"{name} 缺少断网文案"
    # Flow Home 的读取失败同样只说用户语言（有自己的措辞表）
    home_js = _strip_js_comments(_read(FRONTEND / "home.js"))
    assert "读取超时" in home_js and "暂时无法读取你的连接" in home_js


_TOP_LEVEL_CONST = re.compile(r"^const\s+([A-Za-z_$][\w$]*)", re.M)


def test_f2c_no_const_collision_between_same_page_scripts() -> None:
    """同页脚本顶层 const 不重名（M7-AUI-9 模块化后的纵深防御）。

    历史：经典脚本时代，nav.js 与页面脚本重复声明 `const REQUEST_TIMEOUT_MS`
    曾导致 SyntaxError、整页脚本失效（Flow Home 卡死在静态加载文案）。
    ES module 化后每个脚本有独立模块作用域，该故障模式已结构性消除；
    本断言保留为命名卫生检查（跨文件重名会让排障与源码检索混淆）。
    """
    page_scripts = {
        HOME_HTML: ("nav.js", "home.js"),
        AGENTS_HTML: ("nav.js", "agents.js"),
        CHANNELS_HTML: ("nav.js", "channels.js"),
        CONNECTIONS_HTML: ("nav.js", "connections.js"),
        USERS_HTML: ("nav.js", "users.js"),
        IDENTITIES_HTML: ("nav.js", "identities.js"),
        RUNS_HTML: ("nav.js", "interactions.js"),
        CONNECT_HTML: ("nav.js", "connect.js"),
        WORKSPACE_HTML: ("workspace.js",),
    }
    for page, scripts in page_scripts.items():
        declared: dict[str, str] = {}
        for name in scripts:
            src = _strip_js_comments(_read(FRONTEND / name))
            for const in _TOP_LEVEL_CONST.findall(src):
                assert const not in declared, (
                    f"{page.name}: {declared[const]} 与 {name} 重复声明顶层 const {const}")
                declared[const] = name


def test_f3_connect_page_failures_keep_technical_detail_available() -> None:
    js = _strip_js_comments(_read(CONNECT_JS))
    assert "function reportFailure" in js
    assert "function userMessage" in js
    html = _read(CONNECT_HTML)
    assert 'id="result-error-detail"' in html
    assert "查看详细信息" in html


# ================================================================== G. 兼容页 / 死元素修正


@oss_ui_required()
def test_g1_legacy_pages_still_served_and_redirect() -> None:
    client = TestClient(app)
    for path in ("/connect-agent.html", "/connect-channel.html"):
        page = client.get(path)
        assert page.status_code == 200, path
        assert "url=/connect.html" in page.text, path


def test_g2_legacy_pages_no_longer_load_legacy_scripts() -> None:
    """旧页面不再加载旧脚本（脚本文件本身已删除）。"""
    assert not LEGACY_AGENT_JS.exists()
    assert not LEGACY_CHANNEL_JS.exists()
    for path in (LEGACY_AGENT_HTML, LEGACY_CHANNEL_HTML):
        html = _read(path)
        assert "<script src=" not in html, f"{path.name} 仍加载脚本"
        assert "connect-agent.js" not in html
        assert "connect-channel.js" not in html


def test_g3_cta_primary_is_home_only_and_load_bearing() -> None:
    """`#cta-primary` 只属于 Flow Home（状态 E 的「把它们连接起来」），是活元素。

    M7-AUI-8 的只读审计曾把它误记为 connect.html 的死元素；实际它位于 home.html
    且被 home.js 使用，因此**不能删除**——这里把这个事实锁住，避免后人按错误结论清理。
    """
    assert 'id="cta-primary"' not in _read(CONNECT_HTML)
    assert 'id="cta-primary"' in _read(HOME_HTML)
    assert "cta-primary" in _read(FRONTEND / "home.js")


def test_g4_workspace_single_entry_and_no_management_links() -> None:
    html = _read(WORKSPACE_HTML)
    assert 'id="cta-start-connect"' in html
    assert 'href="/connect.html"' in html
    assert 'id="cta-connect-agent"' not in html
    assert 'id="cta-connect-channel"' not in html
    for banned in ("/agents.html", "/channels.html", "/connections.html",
                   "/users.html", "/identities.html", "/interactions.html"):
        assert banned not in html, f"Workspace 不应暴露管理入口：{banned}"
