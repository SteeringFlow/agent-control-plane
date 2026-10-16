"""Cloud-0.1.4.1 —— Connect Channel Golden Path UI 验收（M7-AUI-8.2 后）。

**M7-AUI-8.2（Advanced Surface Reduction）更新**：旧 Connect Channel 页面收口为
**兼容页**——`connect-channel.html` 继续存在、继续由应用托管（HTTP 200），并在
客户端立刻跳转到统一接入页 `/connect.html`；旧脚本 `connect-channel.js` 已删除
（页面不再加载任何脚本），旧流程 body 与内部术语（渠道 ID / 回调配置术语 / 旧管理入口）
全部移除。

契约因此分成三段：

1. **兼容页三条硬事实**：文件存在 / 可服务（200）/ 客户端跳转到 `/connect.html` /
   不加载旧脚本、不再暴露内部术语与旧管理入口；
2. **零后端面（红线 1/2）**：组合根没有 `/api/integrations`、`/api/workspace`、
   `/api/connect-channel` 之类的聚合端点——这条**与页面瘦身无关**，继续守住；
3. **迁移说明**：旧的 Golden-Path 行为契约（两个配置面分离 / Secret 绝不回填 /
   Agent 只选不建 / 回调地址仅路由定位）随旧页面一起退出，因为这些能力现在由
   `channels.html`（高级接入）与 `connect.html`（统一接入）承载，
   其守卫点是 `test_step11_9` / `test_m7_aui_6` / `test_m7_aui_7` / `test_cloud_m6_3`。
"""

from __future__ import annotations

import re

from tests.oss_export_boundary import oss_ui_required
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND = REPO_ROOT / "frontend"

LEGACY_HTML = FRONTEND / "connect-channel.html"
LEGACY_JS = FRONTEND / "connect-channel.js"
CONNECT_HTML = FRONTEND / "connect.html"
CONNECT_JS = FRONTEND / "connect.js"
STYLES = FRONTEND / "styles.css"
WORKSPACE_HTML = FRONTEND / "workspace.html"
MAIN_PY = REPO_ROOT / "backend" / "app" / "main.py"

UNIFIED_CONNECT = "/connect.html"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ================================================================== A. 兼容页 / 静态可达


def test_a1_legacy_page_exists_and_legacy_script_is_gone() -> None:
    """兼容页与共用样式仍在（书签不 404）；旧脚本已删除（不再加载旧流程逻辑）。"""
    assert LEGACY_HTML.is_file(), "旧接入页文件必须保留（只保留跳转）"
    assert STYLES.is_file()
    assert not LEGACY_JS.exists(), "旧接入页脚本应已删除（M7-AUI-8.2 死代码清理）"


@oss_ui_required()
def test_a2_page_is_served_and_redirects_to_unified_connect() -> None:
    """旧 URL 仍 200，客户端跳转到统一接入页；静态 mount 恒最后（API 优先）。"""
    client = TestClient(app)
    page = client.get("/connect-channel.html")
    assert page.status_code == 200
    assert f"url={UNIFIED_CONNECT}" in page.text
    assert "connect-channel.js" not in page.text, "兼容页不应再引用旧脚本"
    assert client.get("/workspace.html").status_code == 200
    assert client.get("/connect.html").status_code == 200

    route_types = [type(r).__name__ for r in app.router.routes]
    assert route_types[-1] == "Mount", "静态 mount 必须最后注册（API 优先）"
    api_paths = {getattr(r, "path", "") for r in app.router.routes}
    for existing in ("/api/channels", "/api/connections", "/api/agents",
                     "/api/interactions"):
        assert existing in api_paths


def test_a3_workspace_has_single_connect_entry() -> None:
    """Workspace 只有一个清晰接入入口（同目标的两个 CTA 已合并）。"""
    workspace = _read(WORKSPACE_HTML)
    assert f'href="{UNIFIED_CONNECT}"' in workspace
    assert 'id="cta-start-connect"' in workspace
    # 红线 1 边界：workspace 仍不做 Dashboard / Billing / Members
    for banned in ("dashboard", "billing", "members", "invite"):
        assert banned not in workspace.lower()


# ================================================================== B. 红线 1/2：Backend = 0 changes、不创建 Integration


def test_b1_no_aggregated_or_new_endpoints() -> None:
    """红线 2：不创建 Integration——组合根无聚合端点（Backend = 0 changes 反向复核）。"""
    main_src = _read(MAIN_PY)
    for banned in ("/api/integrations", "/api/workspace", "/api/connect-channel"):
        assert banned not in main_src, f"组合根出现聚合端点：{banned}"

    for path in (LEGACY_HTML, CONNECT_HTML):
        src = _read(path)
        for banned in ("/api/integrations", "/api/workspace", "/api/connect-channel",
                       "Integration"):
            assert banned not in src, f"{path.name} 出现越界概念：{banned}"


def test_b2_unified_connect_uses_only_existing_endpoints() -> None:
    """统一接入页的端点面 = 既有 4 个前缀（无新增 / 无聚合）。"""
    js = re.sub(r"/\*.*?\*/", " ", _read(CONNECT_JS), flags=re.S)
    referenced = set(re.findall(r'"/api/[a-z]+"', js))
    assert referenced == {'"/api/agents"', '"/api/channels"',
                          '"/api/connections"', '"/api/interactions"'}, referenced


# ================================================================== C. 兼容页不再暴露内部面


def test_c1_legacy_page_has_no_internal_terms_or_old_entries() -> None:
    """兼容页不再暴露回调配置术语 / 渠道 ID / 旧管理入口。"""
    html = _read(LEGACY_HTML)
    lowered = html.lower()
    for banned in ("channel_id", "corpsecret", "token", "encodingaeskey",
                   "webhook", "connection.config", "identity", "interaction",
                   "endpoint", "adapter", "runtime"):
        assert banned not in lowered, f"兼容页仍暴露内部术语：{banned}"
    for banned in ("/agents.html", "/channels.html", "/interactions.html",
                   "/connect-agent.html"):
        assert banned not in html, f"兼容页仍暴露旧入口：{banned}"
