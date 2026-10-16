"""Cloud-0.1.2.3.2 —— Connect Agent Golden Path UI 验收（M7-AUI-8.2 后）。

**M7-AUI-8.2（Advanced Surface Reduction）更新**：旧 Connect Agent 页面收口为
**兼容页**——`connect-agent.html` 继续存在、继续由应用托管（HTTP 200），并在
客户端立刻跳转到统一接入页 `/connect.html`；旧脚本 `connect-agent.js` 已删除
（页面不再加载任何脚本），旧流程 body 与内部术语全部移除。

因此本文件的契约变成三段：

1. **兼容页的三条硬事实**（旧 URL 不 404、不破坏书签、不再暴露旧流程）：
   文件存在 / 可服务（200）/ 客户端跳转到 `/connect.html` / 不加载旧脚本；
2. **编排语义的守卫点迁移**：只编排既有端点、无越界概念、无 Saga / 回滚、
   `endpoint` 可选——这些语义现在由**统一接入页**承载，
   守卫点在 `test_m7_aui_1` / `test_m7_aui_6` / `test_m7_aui_7` / `test_cloud_m6_3`；
   本文件保留「无新后端编排实体」与「统一接入页的端点面」两条抽查；
3. **入口与文档**：Workspace 单一入口指向 `/connect.html`；README 继续记录兼容页。

扫描纪律同 0.1.2.1 / 11.12：**先剥离注释**再看真实标记（注释可合法讨论禁区）。
"""

from __future__ import annotations

import ast

from tests.oss_export_boundary import oss_ui_required
import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND = REPO_ROOT / "frontend"
BACKEND_APP = Path(__file__).resolve().parents[1] / "app"

# 统一接入页（活跃的 Golden Path 表面）
CONNECT_HTML = FRONTEND / "connect.html"
CONNECT_JS = FRONTEND / "connect.js"
# 兼容页（M7-AUI-8.2：只保留跳转；旧脚本已删除）
LEGACY_HTML = FRONTEND / "connect-agent.html"
LEGACY_JS = FRONTEND / "connect-agent.js"
WORKSPACE_HTML = FRONTEND / "workspace.html"
README = FRONTEND / "README.md"

UNIFIED_CONNECT = "/connect.html"

_FORBIDDEN_CLASSES = frozenset({
    "ConnectAgentService", "ConnectAgent", "ConnectService", "ConnectWorkflow",
    "OnboardingState", "Onboarding", "Provisioning", "Saga", "Outbox",
    "Compensating", "TransactionManager", "RetryManager", "RetryPolicy",
})


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _strip_js_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    src = re.sub(r"//[^\n]*", " ", src)
    return src


def _js_code() -> str:
    """统一接入页 connect.js 的**去注释**源码（真实标记面）。"""
    return _strip_js_comments(_read(CONNECT_JS))


def _app_class_names() -> set[str]:
    names: set[str] = set()
    for py in BACKEND_APP.rglob("*.py"):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                names.add(node.name)
    return names


# ================================================================== A. 兼容页 / 静态可达


def test_a1_legacy_page_exists_and_legacy_script_is_gone() -> None:
    """兼容页文件仍在（书签不 404）；旧脚本已删除（页面不再加载旧流程逻辑）。"""
    assert LEGACY_HTML.is_file(), "旧接入页文件必须保留（只保留跳转）"
    assert not LEGACY_JS.exists(), "旧接入页脚本应已删除（M7-AUI-8.2 死代码清理）"


@oss_ui_required()
def test_a2_page_is_served_and_redirects_to_unified_connect() -> None:
    """旧 URL 仍 200（客户端跳转在浏览器侧生效），且不再加载旧脚本。"""
    client = TestClient(app)
    page = client.get("/connect-agent.html")
    assert page.status_code == 200
    assert f"url={UNIFIED_CONNECT}" in page.text
    assert 'window.location.replace("/connect-agent.js")' not in page.text
    assert "connect-agent.js" not in page.text, "兼容页不应再引用旧脚本"
    # 静态 mount 恒最后（API 优先），/api/connections 路由在册。
    route_types = [type(r).__name__ for r in app.router.routes]
    assert route_types[-1] == "Mount"
    api_paths = {getattr(r, "path", "") for r in app.router.routes}
    assert "/api/connections" in api_paths and "/api/agents" in api_paths


def test_a3_workspace_cta_points_to_unified_connect() -> None:
    """Workspace 的单一入口指向统一接入页（M7-AUI-8.2 合并两个同目标 CTA）。"""
    html = _read(WORKSPACE_HTML)
    assert 'id="cta-start-connect"' in html
    assert f'href="{UNIFIED_CONNECT}"' in html
    # 旧的并列双 CTA 已合并，不再存在。
    assert 'id="cta-connect-agent"' not in html
    assert 'id="cta-connect-channel"' not in html


def test_a4_readme_documents_the_page() -> None:
    assert "connect-agent.html" in _read(README)


# ================================================================== B. 纯编排（无新后端面）


def test_b1_no_new_backend_orchestration_entity() -> None:
    assert not (_app_class_names() & _FORBIDDEN_CLASSES)


def test_b2_unified_connect_only_uses_existing_endpoints() -> None:
    """统一接入页只编排既有端点；无越界概念、无新端点。"""
    code = _js_code()
    assert 'const AGENTS_API = "/api/agents";' in code
    assert 'const CHANNELS_API = "/api/channels";' in code
    assert 'const CONNECTIONS_API = "/api/connections";' in code
    referenced = set(re.findall(r'"/api/[a-z]+"', code))
    assert referenced == {'"/api/agents"', '"/api/channels"',
                          '"/api/connections"', '"/api/interactions"'}, referenced
    for banned in ('"/api/users"', '"/api/identities"', '"/api/connect"'):
        assert banned not in code, f"Golden Path UI 出现越界端点：{banned}"
    for banned in ("chat", "websocket"):
        assert banned not in code.lower(), f"Golden Path UI 出现越界概念：{banned}"


def test_b3_no_rollback_or_onboarding_machinery_on_unified_connect() -> None:
    """绝不自动回滚 / 不引入 onboarding、provisioning 状态机（沿用 0.1.2.3 红线）。"""
    code = _js_code()
    lowered = code.lower()
    for banned in ("rollback", "saga", "compensat", "transactionmanager",
                   "onboarding", "provisioning", "connect_status"):
        assert banned not in lowered, f"接入页出现回滚 / onboarding 机制：{banned}"


def test_b4_connect_page_keeps_verify_then_enable_order() -> None:
    """接通顺序铁律：`/verify` 通过后才 `bootstrap-arm`（失败即 STOP）。"""
    code = _js_code()
    assert "/verify" in code and "bootstrap-arm" in code
    assert code.index("/verify") < code.index("bootstrap-arm")


# ================================================================== C. 兼容页不再暴露内部面


def test_c1_legacy_page_has_no_internal_terms() -> None:
    """兼容页不再暴露渠道 ID / 内部动作名 / 配置术语 / 旧管理入口。"""
    html = _read(LEGACY_HTML)
    lowered = html.lower()
    for banned in ("channel_id", "corpsecret", "encodingaeskey",
                   "connection.config", "webhook", "identity", "interaction",
                   "endpoint", "adapter", "runtime"):
        assert banned not in lowered, f"兼容页仍暴露内部术语：{banned}"
    # 不再把用户引向管理页（旧页层的「管理 Agent / 先创建 Channel」入口已移除）。
    for banned in ("/agents.html", "/channels.html", "/interactions.html"):
        assert banned not in html, f"兼容页仍暴露旧入口：{banned}"
