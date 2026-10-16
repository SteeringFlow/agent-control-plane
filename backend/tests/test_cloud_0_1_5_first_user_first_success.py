"""Cloud-0.1.5 —— 首次用户 / 首次成功（Golden Path）验收（M7-AUI-8.2 后）。

**M7-AUI-8.2（Advanced Surface Reduction）更新**：旧接入页（connect-agent /
connect-channel）收口为纯兼容跳转页，旧脚本已删除；因此本文件的守卫点全部落在
**仍然活跃的表面**上：

- 入口与金路径：`workspace.html`（唯一下一步）+ `connect.html` / `connect.js`（统一接入）；
- 兼容页：文件存在、仍 200、客户端跳转 `/connect.html`、不加载旧脚本；
- 智能零知识（G1/G3）：金路径文件不出现内部术语；用户可见文案使用真实认知词；
- 失败可行动（G4）：统一接入页的失败仍是「步骤 + 原因 + 下一步 + 重试」，
  并沿用 M7-AUI-8.2 的新规矩：默认用户语言 + `[查看详细信息]` 折叠技术细节；
- 首次成功（G5）：仍由 Interaction 原生双状态派生（执行成功 × 已送达），
  绝不把「配置成功」当成功。
"""

from __future__ import annotations

import re

from tests.oss_export_boundary import oss_ui_required
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND = REPO_ROOT / "frontend"

WORKSPACE_HTML = FRONTEND / "workspace.html"
WORKSPACE_JS = FRONTEND / "workspace.js"
AGENT_HTML = FRONTEND / "connect-agent.html"
CHANNEL_HTML = FRONTEND / "connect-channel.html"
CONNECT_HTML = FRONTEND / "connect.html"
CONNECT_JS = FRONTEND / "connect.js"
MAIN_PY = REPO_ROOT / "backend" / "app" / "main.py"

UNIFIED_CONNECT = "/connect.html"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _strip_js_comments(src: str) -> str:
    """与 0.1.2.3.2 / 0.1.4.1 同一纪律：先剥离注释再看真实标记（注释可合法讨论禁区）。"""
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    src = re.sub(r"//[^\n]*", " ", src)
    return src


def _connect_js() -> str:
    return _strip_js_comments(_read(CONNECT_JS))


def _anchor(html: str, marker: str) -> str:
    """取出含指定 marker 的 <a ...>...</a> 片段。"""
    idx = html.index(marker)
    start = html.rindex("<a", 0, idx)
    end = html.index("</a>", idx) + len("</a>")
    return html[start:end]


# ================================================================== G1. 零知识启动


def test_g1_workspace_exposes_no_tenant_to_user() -> None:
    """G1：Workspace 用户可见 HTML 不出现 'Tenant' 字眼（内部 tenant_id 仅 JS 内部使用）。"""
    html = _read(WORKSPACE_HTML)
    assert "Tenant" not in html, "Workspace 用户可见面不应暴露 Tenant 内部术语"
    # 仍保留内部身份引用（代码层），但不进入展示。
    js = _read(WORKSPACE_JS)
    assert "tenant_id" in js  # 内部身份仍被消费（不向用户展示）


def test_g1_zero_knowledge_terms_absent_from_golden_path() -> None:
    """G1：金路径文件不含需要用户预先理解的内部架构术语。"""
    files = [WORKSPACE_HTML, AGENT_HTML, CHANNEL_HTML, CONNECT_HTML, CONNECT_JS]
    lowered = "".join(_read(f).lower() for f in files)
    for forbidden in ("tenantscope", "webhook routing", "acp protocol"):
        assert forbidden not in lowered, f"金路径出现内部术语：{forbidden}"


# ================================================================== G2. 单一自然下一步


def test_g2_single_primary_cta_always_visible() -> None:
    """G2：Workspace 只有**一个**清晰接入入口（同目标的两个 CTA 已合并）。"""
    html = _read(WORKSPACE_HTML)
    assert 'id="cta-start-connect"' in html
    assert f'href="{UNIFIED_CONNECT}"' in html
    anchor = _anchor(html, 'id="cta-start-connect"')
    assert "hidden" not in anchor, "接入入口应始终可见（唯一自然下一步）"
    assert "btn-primary" in anchor
    # 旧的并列双 CTA 不再存在（两个目标相同，并列只会让人猜）。
    assert 'id="cta-connect-agent"' not in html
    assert 'id="cta-connect-channel"' not in html


def test_g2_workspace_only_talks_to_auth_api() -> None:
    """G2（边界 / 遵守 0.1.2.1 冻结门禁）：Workspace UI 只与 Auth API 对话。"""
    js = _read(WORKSPACE_JS)
    assert "/api/agents" not in js
    assert "/api/channels" not in js
    assert "/api/connections" not in js
    assert "/api/interactions" not in js
    assert "api/auth" in js


def test_g2_workspace_links_no_management_pages() -> None:
    """G2：Workspace 不把用户扔进一排管理页——不链接管理类页面。"""
    html = _read(WORKSPACE_HTML)
    for banned in ("/agents.html", "/channels.html", "/connections.html",
                   "/users.html", "/identities.html", "/interactions.html",
                   "dashboard", "billing", "members", "settings"):
        assert banned not in html.lower(), f"Workspace 不应暴露管理入口：{banned}"


def test_g2_legacy_pages_only_redirect() -> None:
    """G2：旧接入页不再有 gate / 引导逻辑，只做兼容跳转。"""
    for path in (AGENT_HTML, CHANNEL_HTML):
        src = _read(path)
        assert f"url={UNIFIED_CONNECT}" in src
        assert "gate-section" not in src
        assert "先 Connect Agent" not in src


# ================================================================== G3. 配置符合真实用户认知


def test_g3_no_internal_terms_in_user_facing_labels() -> None:
    """G3：金路径用户可见面无 TenantScope / Connection config / AgentAdapter /
    Runtime / Adapter 等内部术语。"""
    display_files = [WORKSPACE_HTML, AGENT_HTML, CHANNEL_HTML, CONNECT_HTML]
    lowered = "".join(_read(f).lower() for f in display_files)
    for forbidden in ("tenantscope", "connection config", "connection configuration",
                      "agentadapter", "runtime", "adapter"):
        assert forbidden not in lowered, f"用户可见面出现内部术语：{forbidden}"


def test_g3_labels_use_real_user_vocabulary() -> None:
    """G3：统一接入页使用真实认知词（Bot ID / Bot Secret / WorkBuddy Agent ID / API Key）。"""
    connect = _read(CONNECT_HTML)
    assert "Bot ID" in connect
    assert "Bot Secret" in connect
    assert "WorkBuddy Agent ID" in connect
    assert "API Key" in connect
    # ② Agent 区域的用户词仍在（客户语言，不是内部 Type / Endpoint 术语）。
    assert "Agent 名称" in connect


# ================================================================== G4. 失败可行动


def test_g4_failure_is_actionable_on_unified_connect() -> None:
    """G4：统一接入页失败必含「步骤 + 原因 + 下一步 + 重试」，无裸 Error 500。"""
    js = _connect_js()
    assert 'markStep(step, "fail"' in js          # 步骤标记失败
    assert "setAlert(resultError" in js           # 原因可见（用户语言）
    assert "reportFailure" in js                  # M7-AUI-8.2：用户语言 + 技术详情折叠
    assert "部分成功" in js or "重试" in js        # 可行动（重提交既有端点）
    assert 'btnRetry.addEventListener("click"' in js
    assert "Error 500" not in js


def test_g4_no_new_error_state_machine() -> None:
    """G4：不为可行动性引入新的错误状态机 / 错误枚举体系。"""
    js = (_connect_js() + _read(WORKSPACE_JS)).lower()
    for forbidden in ("errorstate", "failurestate", "error_state_machine",
                      "failure_state_machine", "errormachine"):
        assert forbidden not in js, f"出现新的错误状态机概念：{forbidden}"


def test_g4_backend_no_new_error_state_machine() -> None:
    """G4（反向复核）：Backend 不新增错误状态机类承载 First-User 失败。"""
    main_src = _read(MAIN_PY)
    assert "/api/integrations" not in main_src
    assert "/api/onboarding" not in main_src


# ================================================================== G5. 首次成功足够明确


def test_g5_first_success_derived_from_real_interaction_dual_status() -> None:
    """G5：First Success 由真实 Interaction 原生双状态推导，绝不把配置成功当作成功。"""
    js = _connect_js()
    assert 'const PROCESSING_SUCCEEDED = "succeeded";' in js
    assert 'const DELIVERY_DELIVERED = "delivered";' in js
    assert "processing_status === PROCESSING_SUCCEEDED" in js
    assert "delivery_status === DELIVERY_DELIVERED" in js
    assert "Channel 创建成功" not in js


def test_g5_success_copy_is_user_language() -> None:
    """G5：成功文案表达真实结果（消息已送达并收到回复），不暴露内部 ID。"""
    js = _connect_js()
    assert "首次成功" in js
    assert "消息已成功送达并收到回复" in js
    for banned in ("Interaction #", "Connection ID", "Session ID"):
        assert banned not in js, f"接入页成功文案出现内部实体术语：{banned}"


# ================================================================== 总闸：金路径可达且静态挂载顺序不变


@oss_ui_required()
def test_z_reachability_and_mount_order() -> None:
    """真实应用：金路径页面可达；静态 mount 恒最后（API 优先）。"""
    client = TestClient(app)
    for path in ("/workspace.html", "/workspace.js", "/connect.html", "/connect.js",
                 "/connect-agent.html", "/connect-channel.html"):
        assert client.get(path).status_code == 200, path
    route_types = [type(r).__name__ for r in app.router.routes]
    assert route_types[-1] == "Mount", "静态 mount 必须最后注册（API 优先）"
