"""M7-AUI-0 —— Entry UX Simplification 验收门（只读）。

冻结的产品决策（本文件锁死）：

1. 普通用户只需要理解一件事：**我要进入 SteeringFlow**。
   入口页第一层概念不再是「创建 Workspace」——`进入 SteeringFlow` 是唯一标题。
2. 统一入口默认态 = **继续**（账号 + 密码 → 既有登录能力）。
3. **绝不静默创建账号**：既有 login 端点对「账号不存在 / 密码错误」都返回 401
   （后端刻意不区分，避免账号枚举），前端因此**不猜测**，只在登录失败后提供
   一个必须由用户点击的「创建账号并继续」。
4. 注册仍复用既有 `POST /api/auth/signup`（注册即自动获得 Workspace + 自动登录），
   本任务**零后端改动**。
5. 页面不解释内部实现：无 PBKDF2 / 无存储方式 / 无 Workspace 内部命名 /
   无全局唯一实现 / 无后端验证方式。

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
ENTRY_HTML = FRONTEND / "workspace.html"
ENTRY_JS = FRONTEND / "workspace.js"
STYLES = FRONTEND / "styles.css"

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


def _html() -> str:
    """用户可见面（剥离 HTML 注释——注释里说明「不做」的术语不算暴露）。"""
    return _strip_html_comments(_read(ENTRY_HTML))


def _js() -> str:
    return _strip_js_comments(_read(ENTRY_JS))


def _function_body(code: str, name: str) -> str:
    start = code.index(f"function {name}")
    match = re.search(r"\n(?:async )?function ", code[start + 1:])
    end = start + 1 + match.start() if match else len(code)
    return code[start:end]


def _pane_tag(pane_id: str) -> str:
    """取 `<div id="<pane_id>" ...>` 开标签（判断默认是否 hidden）。"""
    match = re.search(r'<div id="%s"[^>]*>' % pane_id, _html())
    assert match is not None, f"缺少面板：{pane_id}"
    return match.group(0)


# ================================================================== A. 统一入口

def test_a1_entry_title_is_enter_steeringflow() -> None:
    """验收 1/2：第一次访问看到的是「进入 SteeringFlow」统一入口。"""
    html = _html()
    assert "进入 SteeringFlow" in html
    assert "<title>SteeringFlow</title>" in html
    for legacy in ("创建你的 Workspace", "创建 Workspace"):
        assert legacy not in html, f"入口页仍出现旧主操作：{legacy}"


def test_a2_no_create_workspace_primary_action() -> None:
    """验收 3：主操作不是「创建 Workspace」；主按钮 = 继续 / 创建账号并继续。"""
    html = _html()
    login_btn = re.search(r'<button[^>]*id="btn-login"[^>]*>(.*?)</button>', html, re.S)
    signup_btn = re.search(r'<button[^>]*id="btn-signup"[^>]*>(.*?)</button>', html, re.S)
    assert login_btn is not None and signup_btn is not None
    assert login_btn.group(1).strip() == "继续"
    assert signup_btn.group(1).strip() == "创建账号并继续"


def test_a3_default_state_is_continue() -> None:
    """验收 2：默认显示「继续」态；创建账号态默认收起。"""
    assert "hidden" not in _pane_tag("login-pane"), "默认态必须是「继续」"
    assert "hidden" in _pane_tag("signup-pane"), "创建账号态必须默认收起"


def test_a4_no_engineering_explanations() -> None:
    """验收 4/5 + §六：不解释 PBKDF2 / 存储 / Workspace 内部命名 / 全局唯一。"""
    html = _html().lower()
    js = _js().lower()
    for banned in (
        "pbkdf2", "绝不明文", "验证器存储", "全局唯一",
        "workspace 名称", "workspace name", "workspace id", "user id",
    ):
        assert banned not in html, f"入口页出现工程解释：{banned}"
        assert banned not in js, f"入口脚本出现工程解释：{banned}"
    # 用户可见字段只有账号 / 密码，不再有 Login / Password 之外的内部字段
    assert 'name="tenant_id"' not in html


def _input_tag(field_id: str) -> str:
    match = re.search(r'<input[^>]*id="%s"[^>]*>' % field_id, _html(), re.S)
    assert match is not None, f"缺少字段：{field_id}"
    return match.group(0)


def test_a5_fields_are_email_and_password() -> None:
    """M7-AUI-0.2：字段 = 邮箱 + 登录密码 / 设置密码（后端字段名 login/password 不动）。"""
    html = _html()
    for field_id in ('id="l-login"', 'id="l-password"', 'id="f-login"', 'id="f-password"'):
        assert field_id in html, f"缺少字段：{field_id}"
    for label in ("邮箱", "登录密码", "设置密码"):
        assert label in html, f"缺少字段标签：{label}"
    for placeholder in ("请输入你的邮箱地址",
                        "输入你的 SteeringFlow 登录密码",
                        "设置你的 SteeringFlow 登录密码"):
        assert placeholder in html, f"缺少占位提示：{placeholder}"
    # 邮箱字段（不是自由文本登录名）
    assert 'type="email"' in _input_tag("l-login")
    assert 'type="email"' in _input_tag("f-login")
    assert 'type="password"' in _input_tag("l-password")
    # 后端字段名不变（不为翻译而改契约）
    assert 'name="login"' in html and 'name="password"' in html


def test_a6_autofill_attributes_favor_returning_user() -> None:
    """验收 9/11：默认态用 current-password，浏览器自动填充后点「继续」即登录。"""
    html = _html()
    login_pwd = re.search(r'<input[^>]*id="l-password"[^>]*>', html, re.S)
    signup_pwd = re.search(r'<input[^>]*id="f-password"[^>]*>', html, re.S)
    assert login_pwd is not None and signup_pwd is not None
    assert 'autocomplete="current-password"' in login_pwd.group(0)
    assert 'autocomplete="new-password"' in signup_pwd.group(0)


def test_a7_switch_entries_are_in_customer_language() -> None:
    """§七：注册 / 登录切换入口用客户语言（复用既有 toggle，不新建页面）。"""
    html = _html()
    assert "还没有账号？" in html
    assert "已有账号？" in html
    tab_signup = re.search(r'<button[^>]*id="tab-signup"[^>]*>(.*?)</button>', html, re.S)
    tab_login = re.search(r'<button[^>]*id="tab-login"[^>]*>(.*?)</button>', html, re.S)
    assert tab_signup is not None and tab_login is not None
    assert tab_signup.group(1).strip() == "创建账号"
    assert tab_login.group(1).strip() == "登录"
    # 既有 toggle 机制仍在（不新建注册页面）
    assert "showAuthTab" in _js()


# ================================================================== B. 状态机 / 不静默创建

def test_b1_continue_never_creates_account() -> None:
    """§三 明确禁止：点「继续」绝不创建账号（只调既有 login）。"""
    body = _function_body(_js(), "submitLogin")
    assert "`${API}/login`" in body
    assert "${API}/signup" not in body, "「继续」触碰了 signup（会静默创建账号）"
    assert "createAccountFromLogin" not in body, "「继续」不得自动触发创建"


def test_b2_creation_only_on_explicit_confirm() -> None:
    """§三：创建账号只在用户点击确认后发生（唯一入口 = btn-create-from-login）。"""
    code = _js()
    body = _function_body(code, "createAccountFromLogin")
    assert "${API}/signup" in body
    # 只有「创建账号并继续」这一个点击入口会走到创建（不是登录失败的自动动作）
    assert 'btnCreateFromLogin.addEventListener("click", createAccountFromLogin)' in code


def test_b3_create_offer_only_after_failed_login() -> None:
    """§三：确认区默认隐藏，只在一次登录失败后出现；切换状态即收回。"""
    assert 'id="login-create-offer"' in _read(ENTRY_HTML)
    assert "hidden" in re.search(
        r'<div id="login-create-offer"[^>]*>', _html()).group(0)
    js = _js()
    assert "show(loginCreateOffer)" in _function_body(js, "submitLogin")
    assert "hide(loginCreateOffer)" in _function_body(js, "showAuthTab")


def test_b4_errors_are_user_language_only() -> None:
    """M7-AUI-0.3：错误只用用户语言；绝不把 HTTP / detail / 内部异常上屏。"""
    js = _js()
    # 登录失败**不区分**邮箱不存在 / 密码错误（避免泄露账号是否存在）
    assert "邮箱或登录密码不正确。" in js
    assert "这个邮箱已经注册，请直接登录。" in js
    # 不存在任何「邮箱不存在 / 尚未注册」这类会泄露账号存在性的文案
    for leaky in ("邮箱不存在", "尚未注册", "未注册", "不存在这个账号", "账号不存在"):
        assert leaky not in js, f"登录面泄露账号存在性：{leaky}"
    # 不再把后端原始错误串直接上屏
    assert "setLoginError(err.message)" not in js
    assert "setSignupError(err.message)" not in js
    # 错误文案来自固定词表（按状态码映射）
    assert "loginFailureMessage" in js and "signupFailureMessage" in js


def test_b5_no_new_auth_contract() -> None:
    """§八：认证契约一个没改——仍只调既有 4 个 /api/auth 端点。"""
    js = _read(ENTRY_JS)
    assert 'const API = "/api/auth";' in js
    for endpoint in ("${API}/login", "${API}/signup", "${API}/me", "${API}/logout"):
        assert endpoint in js, f"既有端点缺失：{endpoint}"
    lowered = js.lower()
    for banned in ("localstorage", "sessionstorage", "bearer", "authorization",
                   "oauth", "/api/agents", "/api/channels", "/api/connections",
                   "/api/users", "/api/identities"):
        assert banned not in lowered, f"入口脚本引入越界面：{banned}"


def test_b6_success_lands_on_flow_home() -> None:
    """验收 9：登录 / 创建成功后自动进入既有 Flow Home。"""
    assert 'window.location.replace("/home.html")' in _read(ENTRY_JS)


def test_b7_email_format_is_checked_before_every_create() -> None:
    """场景 4：两条创建路径都在发 signup 之前校验邮箱格式。"""
    code = _js()
    assert "isValidEmail" in code
    assert re.search(r"\[\^\\s@\]\+@\[\^\\s@\]\+\\\.\[\^\\s@\]\+", code), \
        "缺少邮箱格式规则（需含 @ 与域名点）"
    for fn in ("submitSignup", "createAccountFromLogin"):
        body = _function_body(code, fn)
        assert "isValidEmail" in body, f"{fn} 未校验邮箱格式"
        assert "请输入有效的邮箱地址。" in body, f"{fn} 缺少无效邮箱提示"
        # 校验发生在 signup 调用**之前**
        assert body.index("isValidEmail") < body.index("${API}/signup"), \
            f"{fn} 的邮箱校验必须早于 signup 调用"


# ================================================================== C. Node 行为验收

_JS_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const src = fs.readFileSync(process.env.PROBE_JS, "utf8").replace(/^import\s[^;]*;/gm, "");

const els = {};
function mkEl() {
  const classes = new Set();
  return {
    classList: {
      add(c) { classes.add(c); },
      remove(c) { classes.delete(c); },
      toggle(c, on) {
        if (on === undefined) { classes.has(c) ? classes.delete(c) : classes.add(c); }
        else if (on) { classes.add(c); } else { classes.delete(c); }
      },
      contains(c) { return classes.has(c); },
    },
    textContent: "", innerHTML: "", value: "",
    handlers: {},
    addEventListener(type, fn) { this.handlers[type] = fn; },
    focus() {},
  };
}
function el(id) { if (!els[id]) els[id] = mkEl(); return els[id]; }

// 复刻 HTML 的初始状态（含 class="hidden"）
["signup-pane", "workspace-section", "nav-logout",
 "login-error", "signup-error", "login-create-offer"].forEach((id) => {
  el(id).classList.add("hidden");
});

let mode = "anon";            // anon | session（模拟 HttpOnly 会话 Cookie 是否生效）
let loginStatus = 200;        // 既有 login 端点返回码
let signupStatus = 201;       // 既有 signup 端点返回码
const calls = [];
const redirects = [];

function json(status, body) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status: status,
    json: () => Promise.resolve(body),
  });
}
const sandbox = {
  console: console,
  document: { getElementById: el, createElement: () => mkEl() },
  window: { location: { replace(u) { redirects.push(u); } } },
  fetch: function (url, options) {
    const method = (options && options.method) || "GET";
    const payload = options && options.body ? JSON.parse(options.body) : null;
    calls.push({ method: method, url: String(url), payload: payload });
    if (method === "POST" && url === "/api/auth/login") {
      if (loginStatus === 200) { mode = "session"; return json(200, { user_id: 1, tenant_id: 2 }); }
      return json(loginStatus, { detail: "authentication failed" });
    }
    if (method === "POST" && url === "/api/auth/signup") {
      if (signupStatus === 201) { mode = "session"; return json(201, { user_id: 1, tenant_id: 2 }); }
      return json(signupStatus, { detail: "login already exists" });
    }
    if (method === "GET" && url === "/api/auth/me") {
      return mode === "session" ? json(200, { user_id: 1, tenant_id: 2 })
                                : json(401, { detail: "authentication required" });
    }
    if (method === "POST" && url === "/api/auth/logout") { mode = "anon"; return json(204, null); }
    return json(404, { detail: "not found" });
  },
};
vm.createContext(sandbox);
const sharedPath = process.env.PROBE_JS.replace(/[^/\\]+$/, "") + "shared/http.js";
const sharedSrc = fs.readFileSync(sharedPath, "utf8").replace(/^export /gm, "");
vm.runInContext(sharedSrc, sandbox);
vm.runInContext(src + "\n;globalThis.__t = { showAuthTab: showAuthTab };", sandbox);

const flush = () => new Promise((r) => setTimeout(r, 0));
const noopEvent = { preventDefault() {} };
function submitContinue() { return els["login-form"].handlers.submit(noopEvent); }
function clickCreate() { return els["btn-create-from-login"].handlers.click(); }
function snapshot() {
  return { calls: calls.slice(), redirects: redirects.slice() };
}
function reset(status, signup) {
  calls.length = 0; redirects.length = 0;
  mode = "anon"; loginStatus = status; signupStatus = signup;
  sandbox.__t.showAuthTab("login");
  els["login-error"].classList.add("hidden");
  els["login-create-offer"].classList.add("hidden");
}

(async function () {
  await flush(); await flush();

  // M7-AUI-0.2：标题随状态切换（登录态 / 注册态）
  const titles = {};
  sandbox.__t.showAuthTab("login");
  titles.login = els["entry-title"].textContent;
  sandbox.__t.showAuthTab("signup");
  titles.signup = els["entry-title"].textContent;
  sandbox.__t.showAuthTab("login");

  // 验收 1/2/3：第一次访问的默认态
  const firstVisit = {
    continueVisible: !els["login-pane"].classList.contains("hidden"),
    createPaneHidden: els["signup-pane"].classList.contains("hidden"),
    offerHidden: els["login-create-offer"].classList.contains("hidden"),
  };

  // 验收 6：已有账号 + 正确密码 → 登录（绝不创建）
  reset(200, 201);
  els["l-login"].value = "alice@steeringflow.test";
  els["l-password"].value = "pw-correct";
  await submitContinue(); await flush();
  const existing = snapshot();

  // 验收 8/9：账号不存在 → 登录失败（不静默创建）→ 显式确认 → 创建并进入
  reset(401, 201);
  els["l-login"].value = "newbie@steeringflow.test";
  els["l-password"].value = "pw-new";
  await submitContinue(); await flush();
  const newAccountFailed = {
    ...snapshot(),
    error: els["login-error"].textContent,
    offerHidden: els["login-create-offer"].classList.contains("hidden"),
  };
  await clickCreate(); await flush();
  const newAccountCreated = { ...snapshot(), error: els["login-error"].textContent };

  // 验收 7：已有账号 + 错误密码 → 用户友好错误；即便点了创建也不会建重
  reset(401, 409);
  els["l-login"].value = "alice@steeringflow.test";
  els["l-password"].value = "pw-wrong";
  await submitContinue(); await flush();
  const wrongPassword = { ...snapshot(), error: els["login-error"].textContent };
  await clickCreate(); await flush();
  const wrongPasswordAfterCreate = {
    ...snapshot(), error: els["login-error"].textContent,
  };

  // 场景 4：点「创建账号并继续」+ 无效邮箱 → 直接提示，绝不发 signup
  reset(401, 201);
  els["l-login"].value = "abc@xxx";
  els["l-password"].value = "pw";
  await submitContinue(); await flush();
  await clickCreate(); await flush();
  const invalidEmail = {
    ...snapshot(), error: els["login-error"].textContent,
  };

  // 场景 5：点「创建账号并继续」+ 已注册邮箱 → 服务端 409 → 提示已注册，不创建
  reset(401, 409);
  els["l-login"].value = "taken@steeringflow.test";
  els["l-password"].value = "pw";
  await submitContinue(); await flush();
  await clickCreate(); await flush();
  const registeredEmail = {
    ...snapshot(), error: els["login-error"].textContent,
  };

  // 验收 10/11：第二次打开（浏览器已自动填充邮箱密码）→ 点「继续」→ 正常登录
  reset(200, 201);
  els["l-login"].value = "alice@steeringflow.test";  // autofill
  els["l-password"].value = "pw-correct";            // autofill
  await submitContinue(); await flush();
  const autofill = snapshot();

  process.stdout.write(JSON.stringify({
    titles: titles,
    firstVisit: firstVisit,
    existing: existing,
    newAccountFailed: newAccountFailed,
    newAccountCreated: newAccountCreated,
    wrongPassword: wrongPassword,
    wrongPasswordAfterCreate: wrongPasswordAfterCreate,
    invalidEmail: invalidEmail,
    registeredEmail: registeredEmail,
    autofill: autofill,
  }));
})().catch(function (e) {
  process.stderr.write(String((e && e.stack) || e));
  process.exit(2);
});
"""


def _run_probe() -> dict:
    env = dict(os.environ)
    env["PROBE_JS"] = str(ENTRY_JS)
    proc = subprocess.run(
        [_NODE, "-e", _JS_HARNESS], capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env, check=False)
    assert proc.returncode == 0, f"node 执行失败：{proc.stderr}"
    return json.loads(proc.stdout)


@needs_node
def test_c1_first_visit_shows_unified_entry() -> None:
    """验收 1/2/3（真实执行）：默认「继续」可见、创建态收起、无创建确认区。"""
    out = _run_probe()
    first = out["firstVisit"]
    assert first["continueVisible"] is True
    assert first["createPaneHidden"] is True
    assert first["offerHidden"] is True


@needs_node
def test_c2_existing_account_logs_in_without_creating() -> None:
    """验收 6（真实执行）：已有账号 + 正确密码 → 只登录，绝不调 signup。"""
    out = _run_probe()
    calls = out["existing"]["calls"]
    urls = [c["url"] for c in calls]
    assert urls == ["/api/auth/login", "/api/auth/me"], urls
    assert "/api/auth/signup" not in urls
    assert out["existing"]["redirects"] == ["/home.html"]


@needs_node
def test_c3_unknown_account_offers_explicit_create_then_enters() -> None:
    """验收 8/9（真实执行）：登录失败→明确确认→创建→自动进入 Flow Home。"""
    out = _run_probe()
    failed = out["newAccountFailed"]
    assert [c["url"] for c in failed["calls"]] == ["/api/auth/login"], failed["calls"]
    assert failed["error"] == "邮箱或登录密码不正确。"
    assert failed["offerHidden"] is False, "登录失败后必须提供「创建账号并继续」"
    assert failed["redirects"] == [], "登录失败绝不进入 Flow Home"

    created = out["newAccountCreated"]
    urls = [c["url"] for c in created["calls"]]
    assert urls == ["/api/auth/login", "/api/auth/signup", "/api/auth/me"], urls
    assert created["redirects"] == ["/home.html"], "创建成功后必须自动进入 Flow Home"


@needs_node
def test_c4_wrong_password_is_user_friendly_and_never_creates() -> None:
    """验收 7（真实执行）：已有账号 + 错误密码 → 用户语言错误；点创建也不建重。"""
    out = _run_probe()
    wrong = out["wrongPassword"]
    assert [c["url"] for c in wrong["calls"]] == ["/api/auth/login"]
    assert wrong["error"] == "邮箱或登录密码不正确。"
    assert wrong["redirects"] == []

    after = out["wrongPasswordAfterCreate"]
    # 409 = 这个邮箱已注册 → 明确告知「已注册，请直接登录」，不创建、不进入
    assert [c["url"] for c in after["calls"]] == [
        "/api/auth/login", "/api/auth/signup"], after["calls"]
    assert after["error"] == "这个邮箱已经注册，请直接登录。"
    assert after["redirects"] == []


@needs_node
def test_c6_invalid_email_is_blocked_before_signup() -> None:
    """场景 4（真实执行）：无效邮箱 → 提示「请输入有效的邮箱地址。」，绝不发 signup。"""
    out = _run_probe()
    invalid = out["invalidEmail"]
    # 只发了那次失败的 login；创建请求根本没发出去
    assert [c["url"] for c in invalid["calls"]] == ["/api/auth/login"], invalid["calls"]
    assert invalid["error"] == "请输入有效的邮箱地址。"
    assert invalid["redirects"] == []


@needs_node
def test_c7_registered_email_is_rejected_by_server() -> None:
    """场景 5（真实执行）：已注册邮箱 → 服务端 409 →「这个邮箱已经注册，请直接登录。」"""
    out = _run_probe()
    registered = out["registeredEmail"]
    urls = [c["url"] for c in registered["calls"]]
    assert urls == ["/api/auth/login", "/api/auth/signup"], urls
    assert registered["error"] == "这个邮箱已经注册，请直接登录。"
    assert registered["redirects"] == []


@needs_node
def test_c5_autofill_second_visit_never_recreates_workspace() -> None:
    """验收 10/11（真实执行）：浏览器自动填充后点「继续」→ 正常登录，不走创建。"""
    out = _run_probe()
    autofill = out["autofill"]
    urls = [c["url"] for c in autofill["calls"]]
    assert urls == ["/api/auth/login", "/api/auth/me"], urls
    assert "/api/auth/signup" not in urls, "自动填充场景绝不再走创建"
    assert autofill["redirects"] == ["/home.html"]


# ================================================================== D. M7-AUI-0.2 产品语义

def test_d1_entry_title_follows_state() -> None:
    """登录态标题 = 进入 SteeringFlow；注册态标题 = 创建 SteeringFlow 账号。"""
    html = _html()
    assert '<h2 id="entry-title">进入 SteeringFlow</h2>' in html
    js = _js()
    assert "创建 SteeringFlow 账号" in js
    assert "进入 SteeringFlow" in js


def _visible_text() -> str:
    """用户真正能读到的文字（去掉标签——section id 等内部命名不算暴露）。"""
    return re.sub(r"<[^>]+>", " ", _html()).lower()


def test_d2_no_workspace_or_security_internals_on_screen() -> None:
    """明确禁止：创建 Workspace / Workspace 名称 / PBKDF2 / 数据库安全实现 /
    「登录名同时作为 Workspace 名称」/ 任何内部认证术语。"""
    text = _visible_text()
    for banned in ("workspace", "pbkdf2", "明文", "哈希", "数据库", "加密",
                   "jwt", "cookie", "session", "凭据", "验证器", "tenant"):
        assert banned not in text, f"入口页出现禁止术语：{banned}"


def test_d3_js_shows_no_internal_auth_terms() -> None:
    """脚本里上屏的文案也不含内部认证实现术语（注释已剥离）。"""
    js = _js().lower()
    for banned in ("pbkdf2", "明文", "哈希", "数据库", "jwt", "cookie",
                   "workspace 名称", "登录名"):
        assert banned not in js, f"入口脚本上屏了内部术语：{banned}"


@needs_node
def test_d4_title_switches_at_runtime() -> None:
    """真实执行：切换状态时标题随之改变（用户一眼知道自己在登录还是创建账号）。"""
    out = _run_probe()
    assert out["titles"]["login"] == "进入 SteeringFlow"
    assert out["titles"]["signup"] == "创建 SteeringFlow 账号"


# ================================================================== E. M7-AUI-0.2 视觉收口


def _entry_css() -> str:
    css = _read(STYLES)
    return css[css.index("M7-AUI-0.2 Entry 视觉收口"):]


def test_e1_all_field_types_share_one_box_model() -> None:
    """邮箱 / 密码 / 文本 / 下拉同一宽度（不再只覆盖 type="text"）。"""
    css = _read(STYLES)
    assert re.search(r"\.field input,\s*\.field select,", css), \
        "字段盒模型必须覆盖所有 input 类型（含 password / email）"


def test_e2_entry_card_is_not_full_width() -> None:
    """页面内容宽度合理：入口卡片窄而居中，不横向铺满 960。

    契约守的是「窄 + 居中」：`max-width` 收口 + 左右 auto 居中。
    垂直间距是可调项（原 40px，M7-AUI-9 起为 48px），故只断言 auto 居中与
    间距为正数，不再冻结具体像素值。
    """
    block = _entry_css()
    match = re.search(r"#signup-section\s*\{[^}]*max-width:\s*(\d+)px", block)
    assert match is not None, "入口卡片缺少宽度收口"
    assert int(match.group(1)) <= 480, f"入口卡片过宽：{match.group(1)}px"
    margin = re.search(r"#signup-section\s*\{[^}]*margin:\s*(\d+)px\s+auto", block)
    assert margin is not None, "入口卡片必须左右居中（margin: Npx auto）"
    assert int(margin.group(1)) > 0, "入口卡片垂直间距必须为正"


def test_e3_controls_share_one_height() -> None:
    """表单控件统一高度：邮箱 / 密码 / 按钮同一条规则。"""
    block = _entry_css()
    match = re.search(
        r"#signup-section \.field input,\s*#signup-section \.btn\s*\{([^}]*)\}", block)
    assert match is not None, "缺少统一高度规则"
    assert "height:" in match.group(1)


def test_e4_buttons_share_one_size() -> None:
    """按钮尺寸统一：主按钮与次级按钮同一尺寸。"""
    block = _entry_css()
    # 只匹配独立成规则的 `#signup-section .btn {}`（排除与 input 合并的那条规则）
    match = re.search(r"(?<!,)\n#signup-section \.btn\s*\{([^}]*)\}", block)
    assert match is not None, "缺少按钮尺寸规则"
    body = match.group(1)
    assert "min-width" in body and "padding" in body, body


def test_e5_title_fields_links_form_a_hierarchy() -> None:
    """层级：标题 > 字段 > 按钮 > 注册/登录链接。"""
    block = _entry_css()
    title = re.search(r"#entry-title\s*\{\s*font-size:\s*(\d+)px", block)
    label = re.search(r"#signup-section \.field label\s*\{\s*font-size:\s*(\d+)px", block)
    assert title is not None and label is not None
    assert int(title.group(1)) > int(label.group(1)), "标题必须比字段标签更显著"


# ================================================================== F. M7-AUI-0.3 后端校验

def _auth_service(engine):
    """用内存库装配 BrowserAuthService（与既有 Cloud auth 测试同一装配方式）。"""
    from sqlalchemy.orm import sessionmaker

    from app.application.browser_auth import BrowserAuthService
    from app.application.tenant_provisioning import TenantProvisioningService
    from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def uow_factory(tenant_id=None):
        return SqlAlchemyUnitOfWork(session_factory=factory, tenant_id=tenant_id)

    return BrowserAuthService(
        uow_factory=uow_factory,
        provisioner=TenantProvisioningService(uow_factory=uow_factory),
    )


def test_f1_signup_rejects_invalid_email_server_side(db_engine) -> None:
    """场景 4（后端）：注册入口拒绝无效邮箱——服务端不依赖前端校验。"""
    from app.application.browser_auth import SignUpBrowserUser
    from app.application.errors import InvalidBrowserAuthInputError

    service = _auth_service(db_engine)
    for bad in ("abc", "abc@", "abc@xxx", "abc@.com", "@x.com", "a b@x.com"):
        with pytest.raises(InvalidBrowserAuthInputError):
            service.signup(SignUpBrowserUser(login=bad, password="pw"))


def test_f2_signup_accepts_valid_email_and_provisions_default_workspace(db_engine) -> None:
    """场景 6（后端）：合法邮箱 → 创建账号 → 自动获得默认 Workspace。"""
    from app.application.browser_auth import SignUpBrowserUser
    from app.core.tenant import TENANT_LOCAL

    service = _auth_service(db_engine)
    grant = service.signup(
        SignUpBrowserUser(login="new@steeringflow.test", password="pw"))
    assert grant.user.tenant_id > TENANT_LOCAL
    assert grant.session_id


def test_f3_duplicate_email_creates_no_second_user_or_tenant(db_engine) -> None:
    """场景 5（后端）：已注册邮箱 → Duplicate 冲突，且不产生第二个 User / Tenant。"""
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from app.application.browser_auth import SignUpBrowserUser
    from app.application.errors import DuplicateBrowserLoginError
    from app.persistence.models.tenant import TenantModel
    from app.persistence.models.user import UserModel

    service = _auth_service(db_engine)
    service.signup(
        SignUpBrowserUser(login="taken@steeringflow.test", password="p1"))

    with Session(db_engine) as session:
        before_tenants = len(session.execute(select(TenantModel)).scalars().all())
        before_users = len(session.execute(select(UserModel)).scalars().all())

    with pytest.raises(DuplicateBrowserLoginError):
        service.signup(
            SignUpBrowserUser(login="taken@steeringflow.test", password="p2"))

    with Session(db_engine) as session:
        after_tenants = len(session.execute(select(TenantModel)).scalars().all())
        after_users = len(session.execute(select(UserModel)).scalars().all())
    assert after_tenants == before_tenants, "重复注册不得留下孤儿租户"
    assert after_users == before_users, "重复注册不得创建第二个 User"


def test_f4_login_does_not_distinguish_unknown_email_and_wrong_password(db_engine) -> None:
    """场景 2/3（后端）：未注册邮箱 与 密码错误 走**同一**失败（不泄露存在性）。"""
    from app.application.browser_auth import (
        LoginBrowserUser,
        SignUpBrowserUser,
    )
    from app.core.browser_auth import BrowserAuthError

    service = _auth_service(db_engine)
    service.signup(
        SignUpBrowserUser(login="known@steeringflow.test", password="right-pw"))

    wrong_password = pytest.raises(
        BrowserAuthError,
        service.login,
        LoginBrowserUser(login="known@steeringflow.test", password="bad-pw"),
    ).value
    unknown_email = pytest.raises(
        BrowserAuthError,
        service.login,
        LoginBrowserUser(login="nobody@steeringflow.test", password="right-pw"),
    ).value
    # 同一异常类型 + 同一消息：无法通过登录接口区分「邮箱不存在」与「密码错误」
    assert type(wrong_password) is type(unknown_email)
    assert str(wrong_password) == str(unknown_email)


def test_f5_login_with_registered_email_succeeds(db_engine) -> None:
    """场景 1（后端）：已注册邮箱 + 正确密码 → 登录成功。"""
    from app.application.browser_auth import (
        LoginBrowserUser,
        SignUpBrowserUser,
    )

    service = _auth_service(db_engine)
    service.signup(
        SignUpBrowserUser(login="returning@steeringflow.test", password="pw"))
    grant = service.login(
        LoginBrowserUser(login="returning@steeringflow.test", password="pw"))
    assert grant.session_id and grant.user.tenant_id


def test_f6_explicit_tenant_register_path_is_untouched(db_engine) -> None:
    """范围边界：邮箱格式校验只加在 Cloud 注册入口（signup）；
    既有「显式租户 register」路径行为不变。"""
    from app.application.browser_auth import RegisterBrowserUser

    service = _auth_service(db_engine)
    user = service.register(RegisterBrowserUser(
        login="legacy-admin", password="pw", tenant_id=1))
    assert user.user_id and user.tenant_id == 1


def test_f7_no_email_ownership_features_introduced() -> None:
    """「暂不实现邮箱验证」：代码里不得出现验证码 / 一次性口令 / 邮件发送 /
    email_verified 之类的标识符（注释里的说明不算）。"""
    import io
    import tokenize

    banned = {
        "smtp", "smtplib", "sendmail", "send_mail", "mailer", "mail_template",
        "otp", "totp", "email_verified", "email_verification", "verify_email",
        "verification_code", "verification_token", "confirmation_code",
        "magic_link", "mail_queue",
    }
    backend = REPO_ROOT / "backend" / "app"
    hits: list[str] = []
    for py in backend.rglob("*.py"):
        if "__pycache__" in py.parts:
            continue
        try:
            tokens = tokenize.generate_tokens(
                io.StringIO(py.read_text(encoding="utf-8")).readline)
        except Exception:  # pragma: no cover - 解析失败跳过
            continue
        names = {t.string.lower() for t in tokens if t.type == tokenize.NAME}
        found = names & banned
        if found:
            hits.append(f"{py.name}:{sorted(found)}")
    assert hits == [], f"引入了邮箱所有权验证能力：{hits}"
