"""Step 11.5 —— Agent Management UI 语义守护。

静态断言 UI 源码严格遵守 11.2 / 11.4 冻结语义（防 UI 漂移）：
- frontend 真实落地（agents.html / agents.js / styles.css），main.py 静态托管；
- UI 只访问 /api/agents（不越界到 Connection / Channel / chat 等）；
- 无 DELETE（生命周期 = active ⇄ inactive 软下线）；
- credential_ref 写-only：编辑绝不回显当前值（「留空 = 保留当前引用」）；
- status 表单只提供管理态 active/inactive，error 只展示不可选；
- type 受控下拉（当前仅 acp），不做自由文本输入；
- config 不透明 JSON，UI 不预设任何业务字段。

注：不触生产 DB——API 优先级用 app.router 注册顺序断言（mount 最后注册），
UI 可达性只请求静态资源。
"""

from pathlib import Path

from tests.oss_export_boundary import oss_ui_required

from fastapi.testclient import TestClient

from app.main import app

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND = REPO_ROOT / "frontend"

INDEX = FRONTEND / "agents.html"
APP_JS = FRONTEND / "agents.js"
STYLES = FRONTEND / "styles.css"
MAIN = Path(__file__).resolve().parents[1] / "app" / "main.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ================================================================== A. 落地事实


def test_a1_frontend_files_exist() -> None:
    """Agent Management UI 三件套真实存在。"""
    for path in (INDEX, APP_JS, STYLES):
        assert path.is_file(), f"缺少 {path.name}"


def test_a2_main_serves_frontend_statically() -> None:
    """组合根静态托管 frontend/（同源访问 /api；无 CORS、无构建链、无新基础设施）。"""
    src = _read(MAIN)
    assert "StaticFiles" in src
    assert "app.mount" in src
    assert "FRONTEND_DIR" in src


@oss_ui_required()
def test_a3_static_ui_reachable_and_api_precedence() -> None:
    """真实应用：/agents.html 与静态资源可达；API 路由先于 catch-all mount 注册
    （mount 恒为最后一个路由 → /api、/docs、/openapi.json、/health 不被吞掉）。

    M7-AUI-9：Agent 管理页由 `/`（原 index.html）更名为 `/agents.html`；
    `/` 现在是产品根入口（跳转 /workspace.html），不再直出管理页。
    """
    client = TestClient(app)

    root = client.get("/")
    assert root.status_code == 200
    assert "url=/workspace.html" in root.text   # 根入口 = 产品入口
    assert "Agent List" not in root.text and "agent-form" not in root.text

    page = client.get("/agents.html")
    assert page.status_code == 200
    assert "Agent List" in page.text and "agent-form" in page.text

    js = client.get("/agents.js")
    assert js.status_code == 200
    assert "loadList" in js.text

    css = client.get("/styles.css")
    assert css.status_code == 200
    assert "badge-error" in css.text

    route_types = [type(r).__name__ for r in app.router.routes]
    assert "Mount" in route_types, "main.py 未挂载静态 frontend"
    assert route_types[-1] == "Mount", "静态 mount 必须最后注册（API 优先）"
    api_paths = {getattr(r, "path", "") for r in app.router.routes}
    assert {"/api/agents", "/api/agents/{agent_id}"} <= api_paths


# ================================================================== B. API 面（不越界）


def test_b1_ui_only_talks_to_agents_api() -> None:
    """UI 的全部 fetch 目标只允许 /api/agents（禁止 Channel/Connection/chat 等）。

    11.5 明确不做：Connection/Channel/Routing/Session UI、Network Graph、
    Health Check、Agent Test Chat、Capability、Permissions、Deployment。
    """
    js = _read(APP_JS)
    assert 'const API = "/api/agents";' in js
    assert "API}/" in js  # 其余路径均由该常量派生
    for banned in ("/api/channels", "/api/connections", "/api/users",
                   "/api/identities", "chat", "websocket"):
        assert banned not in js.lower(), f"UI 出现越界端点/概念：{banned}"


def test_b2_ui_has_no_delete() -> None:
    """无 DELETE：生命周期 = active ⇄ inactive（软下线），UI 不提供删除。"""
    js = _read(APP_JS)
    assert "DELETE" not in js          # 无 HTTP DELETE 方法调用
    assert "method:" not in js or '"DELETE"' not in js
    html = _read(INDEX)
    assert "删除" not in html


# ================================================================== C. credential_ref 写-only


def test_c1_credential_ref_never_echoed() -> None:
    """credential_ref 写-only（11.2 §2 / 11.4 实证）：编辑表单绝不回显当前值；
    占位语义必须是「留空 = 保留当前引用」；绝不出现 Current Credential Ref。"""
    js = _read(APP_JS)
    # openEdit 中 credential 输入恒为空串（绝不从 agent 数据赋值）
    assert 'fCredential.value = "";' in js
    assert 'fCredential.placeholder = "留空 = 保留当前引用";' in js
    assert "fCredential.value = agent" not in js
    html = _read(INDEX)
    assert "留空 = 保留当前引用" in html
    assert "Current Credential Ref" not in html


# ================================================================== D. status 双轴


def test_d1_status_form_never_offers_error() -> None:
    """表单 status 选项恒为管理态 {active, inactive}（create 模式）或
    {不修改, active, inactive}（edit 模式）；error 只展示、绝不可选。"""
    js = _read(APP_JS)
    # create 与 edit 两个模式共用同一管理态白名单循环生成选项
    assert js.count('for (const value of ["active", "inactive"])') == 2
    assert 'option.value = "error"' not in js
    html = _read(INDEX)
    assert '<option value="error"' not in html


def test_d2_error_visible_and_recoverable_via_activate() -> None:
    """error 徽章样式存在（运行态信号可见）；列表对非 active 提供唯一「启用」动作
    （inactive → active 常规切换；error → active 为唯一的管理覆盖运行态）。"""
    css = _read(STYLES)
    assert ".badge-error" in css
    js = _read(APP_JS)
    assert 'actionButton("启用"' in js
    assert 'setStatus(agent.id, "active")' in js
    # 「不修改」缺省项：编辑 error 态 Agent 时保存不会静默改写运行态
    assert 'keep.textContent = "（不修改）";' in js


# ================================================================== E. type 受控 / config 不透明


def test_e1_type_is_controlled_select() -> None:
    """type 为受控下拉（当前 acp / local），不是自由文本输入框。

    M7-AUI-8.2 字段分层：标签使用客户语言（外部 Agent / 本地测试），
    值仍是受控集合 `acp` / `local`——内部形态代码不再当标签上屏。"""
    html = _read(INDEX)
    assert '<select id="f-type"' in html
    assert '<option value="acp">外部 Agent</option>' in html
    assert '<option value="local">本地测试</option>' in html
    assert '<input type="text" id="f-type"' not in html
    assert ">acp<" not in html, "内部形态代码不应作为标签展示"


def test_e2_config_is_opaque_json() -> None:
    """config 以原始 JSON 编辑（不透明 dict）；UI 不预设任何业务字段——
    timeout/model/transport/headers 等由真实 Agent 类型需求驱动。"""
    js = _read(APP_JS)
    assert "JSON.parse" in js and "JSON.stringify" in js
    for invented in ("timeout_s", "protocol", "model", "transport", "headers"):
        # 以带引号字面量匹配（排除 fetch options.headers 等无关命中）
        assert f'"{invented}"' not in js, f"UI 预设了 config 业务字段：{invented}"
