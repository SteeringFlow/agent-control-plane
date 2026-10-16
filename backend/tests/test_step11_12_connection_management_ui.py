"""Step 11.12 —— Connection Management UI 实现验收。

按 11.10 五焦点裁决 + 11.11 API 合同 + 用户 11.12 指令，静态源码断言 +
真实应用探针（TestClient，不触生产 DB）锁定：

- frontend 落地 connections.html / connections.js（复用 styles.css）；
  三页互链（Agent ⇄ Channel ⇄ Connection）；
- **解耦解析（11.11 冻结）**：列表只返回绑定 ID——Channel/Agent 列由 UI 经
  /api/channels、/api/agents 解析（id→实体 Map），服务端绝不 join；
- **active-only 下拉（11.10 焦点1）**：新建时 Channel/Agent 下拉仅列 active
  （inactive 与 error Agent 均不进入）；
- **immutable binding（11.10 焦点4）**：编辑态绑定为只读文本，channel_id /
  agent_id **根本不进入 Update payload**（后端 extra="forbid" 是第二道防线）；
- **credential_ref 写-only**：编辑绝不回显当前引用值；留空 = 保留，填写 = 轮换；
- **config 响应面敏感键脱敏（16.1.2）+ 整段替换**：回填的是脱敏响应
  （敏感键 = 哨兵）；未修改则省略；有修改但含哨兵值 → 拒绝提交（哨兵按值
  匹配、与键名无关，绝不把哨兵当真实值回发后端）；
- status 纯管理态：创建固定 active（disabled），编辑仅 active/inactive；
- 用户 11.12 不做清单零出现：Health / Connection Test / Routing / Failover /
  Retry / Capability / Permission / Network Graph / Secret Vault / Delete。
"""

from pathlib import Path

from tests.oss_export_boundary import oss_ui_required

from fastapi.testclient import TestClient

from app.main import app

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND = REPO_ROOT / "frontend"

CONNECTIONS_HTML = FRONTEND / "connections.html"
CONNECTIONS_JS = FRONTEND / "connections.js"
STYLES = FRONTEND / "styles.css"
INDEX = FRONTEND / "agents.html"
CHANNELS_HTML = FRONTEND / "channels.html"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ================================================================== A. 落地事实


def test_a1_connection_ui_files_exist() -> None:
    """Connection Management UI 落地（connections.html + connections.js，复用 styles.css）。"""
    for path in (CONNECTIONS_HTML, CONNECTIONS_JS, STYLES):
        assert path.is_file(), f"缺少 {path.name}"


@oss_ui_required()
def test_a2_static_ui_reachable_and_mount_still_last() -> None:
    """真实应用：/connections.html 与 /connections.js 可达；Agent/Channel 页仍可达；
    静态 mount 恒最后注册（API 优先），/api/connections 路由在册。"""
    client = TestClient(app)

    page = client.get("/connections.html")
    assert page.status_code == 200
    assert "Connection List" in page.text and "connection-form" in page.text

    assert client.get("/connections.js").status_code == 200
    assert client.get("/channels.html").status_code == 200
    assert client.get("/").status_code == 200

    route_types = [type(r).__name__ for r in app.router.routes]
    assert "Mount" in route_types, "main.py 未挂载静态 frontend"
    assert route_types[-1] == "Mount", "静态 mount 必须最后注册（API 优先）"
    api_paths = {getattr(r, "path", "") for r in app.router.routes}
    assert "/api/connections" in api_paths


def test_a3_three_management_pages_are_cross_linked() -> None:
    """三页互链（Agent ⇄ Channel ⇄ Connection），同一服务、无构建链。

    M7-AUI-8.2：Agent 管理入口改为显式 `/agents.html`（`/` 在生产会被 302 到
    入口页，故不再作为管理页入口）。"""
    assert 'href="/channels.html"' in _read(INDEX)
    assert 'href="/connections.html"' in _read(INDEX)
    assert 'href="/connections.html"' in _read(CHANNELS_HTML)
    html = _read(CONNECTIONS_HTML)
    assert 'href="/agents.html"' in html
    assert 'href="/channels.html"' in html
    assert 'href="/"' not in html


# ================================================================== B. API 面（解耦解析 + 不越界）


def test_b1_resolves_names_via_channels_and_agents_api_only() -> None:
    """connections.js 的 fetch 目标恰好 = /api/connections + /api/channels +
    /api/agents（解耦解析合同：列表只返回绑定 ID，名称由 UI 解析；
    禁止 users/identities/chat/websocket 等越界端点）。"""
    js = _read(CONNECTIONS_JS)
    assert 'const API = "/api/connections";' in js
    assert 'const CHANNELS_API = "/api/channels";' in js
    assert 'const AGENTS_API = "/api/agents";' in js
    for banned in ("/api/users", "/api/identities", "chat", "websocket"):
        assert banned not in js.lower(), f"Connection UI 出现越界端点/概念：{banned}"


def test_b2_ui_has_no_delete() -> None:
    """无 Delete：生命周期 = active ⇄ inactive 软下线（列表提供停用/启用）。"""
    js = _read(CONNECTIONS_JS)
    assert "DELETE" not in js
    assert "删除" not in _read(CONNECTIONS_HTML)


# ================================================================== C. 冻结合同：解耦解析 / active-only / immutable binding / 写-only


def test_c1_create_dropdowns_are_active_only() -> None:
    """11.10 焦点1 冻结：新建下拉仅列 active——共享过滤 helper 同时服务
    Channel 与 Agent 两个下拉（inactive 与 error Agent 均不进入）。"""
    js = _read(CONNECTIONS_JS)
    assert 'entity.status !== "active"' in js          # 共享的 active-only 过滤
    assert "fillSelectActiveOnly(fChannel" in js
    assert "fillSelectActiveOnly(fAgent" in js
    # 编辑态不渲染绑定下拉（只读文本），因此不存在「为编辑回填下拉选中值」的逻辑
    assert "fChannel.value =" not in js
    assert "fAgent.value =" not in js


def test_c2_binding_never_enters_update_payload() -> None:
    """11.10 焦点4 冻结：Create 带 channel_id/agent_id（唯一入口）；
    Update payload 构造处不出现绑定字段（后端 extra="forbid" 是第二道防线）。"""
    js = _read(CONNECTIONS_JS)
    # 绑定字段字面量在全文件恰好出现一次——只在创建 payload
    assert js.count('"channel_id"') == 1
    assert js.count('"agent_id"') == 1
    assert '"channel_id": Number(fChannel.value)' in js
    # 编辑分支 payload 只从 { name } 起步，仅追加可变字段
    assert "const payload = { name };" in js


def test_c3_credential_ref_is_write_only() -> None:
    """credential_ref 写-only：编辑绝不回显当前引用值；留空 = 保留，填写 = 轮换。"""
    js = _read(CONNECTIONS_JS)
    assert "connection.credential_ref" not in js       # 无回显逻辑
    assert 'fCred.value = "";' in js                   # 编辑态显式清空
    html = _read(CONNECTIONS_HTML)
    assert "留空 = 保留当前引用" in html
    assert "响应永不回显" in html


def test_c4_edit_shows_binding_as_readonly_text() -> None:
    """编辑态绑定为只读展示（immutable binding 的 UI 形态），并提示换绑路径。"""
    js = _read(CONNECTIONS_JS)
    assert "fChannelReadonly.value = bindingLabel(" in js
    assert "fAgentReadonly.value = bindingLabel(" in js
    html = _read(CONNECTIONS_HTML)
    assert '<input type="text" id="f-channel-readonly" readonly>' in html
    assert '<input type="text" id="f-agent-readonly" readonly>' in html
    assert "绑定不可变" in html


# ================================================================== D. status / config 冻结合同


def test_d1_status_form_only_management_states() -> None:
    """status 纯管理态：编辑仅 {不修改, active, inactive}；创建固定 active
    （disabled，后端创建合同无 status 字段）。error 不出现于任何表单选项。"""
    js = _read(CONNECTIONS_JS)
    assert js.count('for (const value of ["active", "inactive"])') == 1
    assert 'option.value = "error"' not in js
    html = _read(CONNECTIONS_HTML)
    assert '<option value="error"' not in html


def test_d2_config_masked_sentinel_guarded_full_replace() -> None:
    """16.1.2：响应 config 敏感键 = 脱敏哨兵；UI 哨兵逻辑按值匹配、与键名
    无关——未修改（与回填原文一致）→ 省略；有修改但含哨兵 → 拒绝提交
    （整段替换会把哨兵当真实值落库）。"""
    js = _read(CONNECTIONS_JS)
    assert 'const REDACTED = "********";' in js        # 哨兵常量唯一定义
    assert "value === REDACTED" in js                  # 按值匹配（与键名无关）
    assert "SECRET_PLACEHOLDER" not in js
    assert "JSON.parse" in js and "JSON.stringify" in js
    assert "originalConfigJson" in js                  # 未修改省略（避免无谓整段替换）
    html = _read(CONNECTIONS_HTML)
    assert "整段替换" in html


def test_d3_config_no_invented_keys() -> None:
    """config 是不透明 JSON：connections.js 不预设任何业务键
    （Access Path 参数语义归用户，UI 只做 JSON 对象校验）。"""
    js = _read(CONNECTIONS_JS)
    for invented in ("token", "encoding_aes_key", "corpsecret", "corp_id",
                     "agentid", "api_base", "timeout_s"):
        assert f'"{invented}"' not in js, f"UI 预设了 config 业务键：{invented}"


# ================================================================== F. 用户 11.12 不做清单（零出现）


def test_f_no_forbidden_features() -> None:
    """不做清单在 Connection UI 源码中零出现：Connection Health / Connection
    Test / Routing / Failover / Retry / Capability / Permission / Network
    Graph / Secret Vault / Delete。"""
    for path in (CONNECTIONS_HTML, CONNECTIONS_JS):
        src = _read(path).lower()
        for banned in ("health", "failover", "routing", "retry", "vault",
                       "rbac", "webhook", "capability", "permission",
                       "graph", "ping", "delete"):
            assert banned not in src, f"{path.name} 出现越界概念：{banned}"
    # 中文「删除」只约束页面文案（js 注释可提及「无硬删除」语义，见 11.9 b2 先例）
    assert "删除" not in _read(CONNECTIONS_HTML)
