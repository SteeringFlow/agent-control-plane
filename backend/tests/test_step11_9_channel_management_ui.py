"""Step 11.9 —— Channel Management UI 语义守护。

严格对齐 11.5（Agent UI）的守护模式：静态断言 UI 源码严格遵守 11.7/11.8 冻结
语义 + 用户 11.9 冻结 UI 合同（防 UI 漂移）：

- frontend 落地 channels.html / channels.js（复用 styles.css）；静态托管可达；
- UI 只访问 /api/channels（不越界到 agents / connections / chat 等）；
- 无 DELETE（生命周期 = active ⇄ inactive 软下线）；
- **Secret 哨兵（本步核心冻结合同）**：config 中值仍为 "********" 的敏感键
  表示「已配置」——编辑回填但保存时自动省略（后端 merge 保留真实 secret）；
  改写为新值 = 轮换。绝不把 "********" 当真实值回发；
- type 自由字符串：文本输入框（刻意不做下拉 / type registry）；
- config 不透明 JSON：不预设任何业务键（哨兵逻辑按值匹配，与键名无关）；
- status 纯管理态：表单只提供 active / inactive（Channel 无运行态 error 轴）。

注：不触生产 DB——静态资源可达性 + app.router 注册顺序断言。
"""

from pathlib import Path

from tests.oss_export_boundary import oss_ui_required

from fastapi.testclient import TestClient

from app.main import app

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND = REPO_ROOT / "frontend"

CHANNELS_HTML = FRONTEND / "channels.html"
CHANNELS_JS = FRONTEND / "channels.js"
STYLES = FRONTEND / "styles.css"
INDEX = FRONTEND / "agents.html"
MAIN = Path(__file__).resolve().parents[1] / "app" / "main.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ================================================================== A. 落地事实


def test_a1_channel_ui_files_exist() -> None:
    """Channel Management UI 落地（channels.html + channels.js，复用 styles.css）。"""
    for path in (CHANNELS_HTML, CHANNELS_JS, STYLES):
        assert path.is_file(), f"缺少 {path.name}"


@oss_ui_required()
def test_a2_static_ui_reachable_and_mount_still_last() -> None:
    """真实应用：/channels.html 与 /channels.js 可达；Agent 页（M7-AUI-9 起为
    /agents.html）仍可达；静态 mount 恒最后注册（API 优先），/api/channels 路由在册。"""
    client = TestClient(app)

    page = client.get("/channels.html")
    assert page.status_code == 200
    assert "Channel List" in page.text and "channel-form" in page.text

    js = client.get("/channels.js")
    assert js.status_code == 200
    assert "loadList" in js.text

    agent_page = client.get("/agents.html")
    assert agent_page.status_code == 200
    assert "Agent List" in agent_page.text

    route_types = [type(r).__name__ for r in app.router.routes]
    assert route_types[-1] == "Mount", "静态 mount 必须最后注册（API 优先）"
    api_paths = {getattr(r, "path", "") for r in app.router.routes}
    assert {"/api/channels", "/api/channels/{channel_id}"} <= api_paths


def test_a3_agent_and_channel_pages_are_cross_linked() -> None:
    """两个管理页互链（Agent ⇄ Channel），同一服务、无构建链。

    M7-AUI-8.2：Agent 管理入口改为显式 `/agents.html`——生产 nginx 的
    `location = /` 会 302 到 `/workspace.html`，因此 `/` 不能作为管理页入口。"""
    assert 'href="/channels.html"' in _read(INDEX)
    assert 'href="/agents.html"' in _read(CHANNELS_HTML)
    assert 'href="/"' not in _read(CHANNELS_HTML)


# ================================================================== B. API 面（不越界）


def test_b1_channel_ui_only_talks_to_channels_api() -> None:
    """channels.js 的全部 fetch 目标只允许 /api/channels
    （禁止 agents/connections/users/identities/chat 等——管理页各管各的实体）。"""
    js = _read(CHANNELS_JS)
    assert 'const API = "/api/channels";' in js
    assert "API}/" in js  # 其余路径均由该常量派生
    for banned in ("/api/agents", "/api/connections", "/api/users",
                   "/api/identities", "chat", "websocket"):
        assert banned not in js.lower(), f"Channel UI 出现越界端点/概念：{banned}"


def test_b2_ui_has_no_delete() -> None:
    """无 DELETE：生命周期 = active ⇄ inactive（软下线），UI 不提供删除。"""
    js = _read(CHANNELS_JS)
    assert "DELETE" not in js
    html = _read(CHANNELS_HTML)
    assert "删除" not in html


# ================================================================== C. 冻结 UI 合同：Secret 哨兵


def test_c1_secret_placeholder_sentinel_semantics() -> None:
    """核心冻结合同：「******** 表示已配置，不是真实 secret」。

    - 哨兵常量唯一定义；
    - 编辑模式保存时值仍为哨兵的键被省略（后端 merge 保留真实 secret）；
    - 占位语义文案（已配置 / 保留 / 轮换）在 HTML hint 中可见。
    """
    js = _read(CHANNELS_JS)
    assert 'const SECRET_PLACEHOLDER = "********";' in js
    # 编辑模式省略未修改的占位键（→ 后端 merge 保留）
    assert "if (value === SECRET_PLACEHOLDER) continue;" in js
    # 哨兵按值匹配、与键名无关（不预设敏感键集合——那是 schemas 层的冻结事实）
    html = _read(CHANNELS_HTML)
    assert "已配置" in html
    assert "保留" in html
    assert "轮换" in html


def test_c2_edit_backfills_sanitized_config_but_never_resends_placeholder() -> None:
    """编辑回填的是 GET 返回的脱敏 config（含 ******** 标记）；提交前经过
    buildConfigPayload 的哨兵过滤——绝不把 ******** 当真实值回发后端
    （那会把真实 secret 覆盖成占位符）。"""
    js = _read(CHANNELS_JS)
    assert "fConfig.value = channel.config ? JSON.stringify(channel.config, null, 2) : \"\";" in js
    # 提交路径必须经过哨兵过滤函数（buildConfigPayload），而非直接使用文本框原始值
    assert "buildConfigPayload(editingId !== null)" in js
    assert "if (!configResult.omit) payload.config = configResult.value;" in js


# ================================================================== D. status 纯管理态 / type 自由字符串


def test_d1_status_form_only_management_states() -> None:
    """表单 status 选项恒为 {active, inactive}（create）或
    {不修改, active, inactive}（edit）；Channel 无运行态 error 轴。"""
    js = _read(CHANNELS_JS)
    assert js.count('for (const value of ["active", "inactive"])') == 2
    assert 'option.value = "error"' not in js
    html = _read(CHANNELS_HTML)
    assert '<option value="error"' not in html


def test_d2_type_is_free_text_input_no_registry() -> None:
    """type 为自由文本输入框（用户 11.9：UI 可以暂时允许字符串输入，
    不为 UI 好看提前制造 type registry）；HTML 不硬编码任何 type 选项。"""
    html = _read(CHANNELS_HTML)
    assert '<input type="text" id="f-type"' in html
    assert '<select id="f-type"' not in html
    assert '<option value="wecom"' not in html
    assert '<option value="feishu"' not in html


# ================================================================== E. config 不透明（不预设键）


def test_e3_config_is_opaque_json_no_invented_keys() -> None:
    """config 以原始 JSON 编辑（不透明 dict）；channels.js 不预设任何业务键——
    敏感键集合的唯一事实来源在 schemas 层（11.8），UI 哨兵逻辑按值匹配。"""
    js = _read(CHANNELS_JS)
    assert "JSON.parse" in js and "JSON.stringify" in js
    for invented in ("token", "encoding_aes_key", "corpsecret", "corp_id",
                     "agentid", "api_base", "timeout_s"):
        assert f'"{invented}"' not in js, f"UI 预设了 config 业务键：{invented}"


# ================================================================== F. 克制边界（不做清单）


def test_f_no_forbidden_features() -> None:
    """用户 11.9 不做清单在 UI 源码中零出现：Registry / Marketplace / Vault /
    Credential Management Center / Health Check / Connection / Routing /
    Webhook 配置中心 / 多租户 / RBAC / Network Graph。

    11.12 翻转：channels.html 顶部导航允许出现「Connection Management」链接
    （三页互链，属页面导航而非功能越界）——剔除导航行后再断言。"""
    for path in (CHANNELS_HTML, CHANNELS_JS):
        src = _read(path)
        if path == CHANNELS_HTML:
            src = "\n".join(line for line in src.splitlines()
                            if "connections.html" not in line)
        low = src.lower()
        for banned in ("registry", "marketplace", "vault", "rbac",
                       "webhook", "tenant", "health", "routing", "connection"):
            assert banned not in low, f"{path.name} 出现越界概念：{banned}"
