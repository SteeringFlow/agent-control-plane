"""M6 UX Closure —— P1 Minimal Fix 验收（只读静态断言 + 真实应用可达探针）。

对应首次用户旅程审计确认的 P1（不含 P0 / P2 / Advanced 区）：

P1-1  First Success 用户可见文案移除内部 Interaction ID（connect.js / home.js）。
P1-2  旧工程师接入页 connect-agent.html / connect-channel.html 统一收口 /connect.html。
P1-3  workspace.html 两个旧接入 CTA 统一指向 /connect.html。

范围纪律（与任务一致）：不修改 backend / Runtime / Verify / Bootstrap Arm / DB /
migration / Channel·Agent·Connection domain；本文件只做只读断言。旧页面文件保留
（不删除），仅新增客户端重定向。

判定逻辑不变：First Success 仍由 processing=succeeded × delivery=delivered 派生。
"""

from __future__ import annotations

import re

from tests.oss_export_boundary import oss_ui_required
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND = REPO_ROOT / "frontend"

CONNECT_JS = FRONTEND / "connect.js"
HOME_JS = FRONTEND / "home.js"
CONNECT_HTML = FRONTEND / "connect.html"
AGENT_HTML = FRONTEND / "connect-agent.html"
CHANNEL_HTML = FRONTEND / "connect-channel.html"
WORKSPACE_HTML = FRONTEND / "workspace.html"
WORKSPACE_JS = FRONTEND / "workspace.js"

# 统一 Connect Journey（P1 收口目标）。
UNIFIED_CONNECT = "/connect.html"

# First Success 用户可见文案所在的活跃旅程表面（connect-channel.js 已被 P1-2 重定向，
# interactions.js 属 Advanced 观测面——两者均不在本次用户可见文案范围）。
_FIRST_SUCCESS_SURFACES = (CONNECT_JS, HOME_JS)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _anchor(html: str, marker: str) -> str:
    """取出含指定 marker 的 <a ...>...</a> 片段。"""
    idx = html.index(marker)
    start = html.rindex("<a", 0, idx)
    end = html.index("</a>", idx) + len("</a>")
    return html[start:end]


def _redirect_target(html: str) -> str | None:
    """页面声明的重定向目标（meta refresh 或 window.location.replace）。"""
    m = re.search(r'http-equiv="refresh"\s+content="0;\s*url=([^"\s]+)"', html, re.I)
    if m:
        return m.group(1)
    m = re.search(r'window\.location\.replace\("([^"]+)"\)', html)
    if m:
        return m.group(1)
    return None


# ================================================================== P1-1


def test_p1_1_first_success_copy_has_no_internal_interaction_id() -> None:
    """P1-1：活跃旅程的 First Success 文案不再出现内部 Interaction ID。"""
    for path in _FIRST_SUCCESS_SURFACES:
        src = _read(path)
        assert "Interaction #" not in src, f"{path.name} 仍含内部 Interaction #"
        assert "${ok.id}" not in src, f"{path.name} 仍把内部 id 拼进用户文案"


def test_p1_1_first_success_copy_is_user_language() -> None:
    """P1-1：First Success 用用户语言表达真实产品结果（渠道已接到 Agent + 首条回复）。"""
    for path in _FIRST_SUCCESS_SURFACES:
        src = _read(path)
        assert "已成功接到你的 Agent" in src, f"{path.name} 缺少用户语言成功语义"
        # 保留既有 First Success 语义锚点（与 M6.6-F4-A T5 一致）。
        assert "成功送达" in src, f"{path.name} 丢失既有成功语义锚点"


def test_p1_1_first_success_still_derives_from_dual_status() -> None:
    """P1-1：判定逻辑不变——仍由 processing=succeeded × delivery=delivered 派生。"""
    for path in _FIRST_SUCCESS_SURFACES:
        src = _read(path)
        assert "processing_status === PROCESSING_SUCCEEDED" in src
        assert "delivery_status === DELIVERY_DELIVERED" in src


def test_p1_1_success_copy_carries_no_internal_entity_terms() -> None:
    """P1-1：成功文案所在源码不出现审计冻结的内部实体术语（用户可见面）。"""
    for path in _FIRST_SUCCESS_SURFACES:
        src = _read(path)
        for banned in ("Interaction ID", "Connection ID", "Session ID"):
            assert banned not in src, f"{path.name} 出现内部实体术语：{banned}"


# ================================================================== P1-2


def test_p1_2_legacy_pages_are_kept_not_deleted() -> None:
    """P1-2：旧页面文件保留（不删除），只是统一收口。"""
    assert AGENT_HTML.is_file()
    assert CHANNEL_HTML.is_file()


def test_p1_2_connect_agent_redirects_to_unified_connect() -> None:
    """P1-2：connect-agent.html → /connect.html。"""
    assert _redirect_target(_read(AGENT_HTML)) == UNIFIED_CONNECT


def test_p1_2_connect_channel_redirects_to_unified_connect() -> None:
    """P1-2：connect-channel.html → /connect.html。"""
    assert _redirect_target(_read(CHANNEL_HTML)) == UNIFIED_CONNECT


def test_p1_2_unified_connect_target_exists_and_is_served() -> None:
    """P1-2：收口目标 /connect.html 存在且可被应用服务。"""
    assert CONNECT_HTML.is_file()
    assert TestClient(app).get(UNIFIED_CONNECT).status_code == 200


@oss_ui_required()
def test_p1_2_legacy_pages_still_served_by_app() -> None:
    """P1-2：旧 URL 仍可达（重定向在客户端生效，避免 404 / 破坏书签）。"""
    client = TestClient(app)
    for path in ("/connect-agent.html", "/connect-channel.html"):
        assert client.get(path).status_code == 200, path


# ================================================================== P1-3


def test_p1_3_workspace_old_ctas_point_to_unified_connect() -> None:
    """P1-3 + M7-AUI-8.2：workspace.html 的接入入口统一指向 /connect.html。

    原先两个 CTA（Connect Agent / Connect Channel）目标相同，并列只会让用户猜
    哪一个是正确入口——M7-AUI-8.2 合并为一个「开始接入 →」。"""
    html = _read(WORKSPACE_HTML)
    assert f'href="{UNIFIED_CONNECT}"' in _anchor(html, 'id="cta-start-connect"')


def test_p1_3_workspace_cta_ids_intact() -> None:
    """P1-3 + M7-AUI-8.2：Workspace 只有一个接入入口 id（旧的并列双 id 已合并）。"""
    html = _read(WORKSPACE_HTML)
    assert 'id="cta-start-connect"' in html
    assert 'id="cta-connect-agent"' not in html
    assert 'id="cta-connect-channel"' not in html


def test_p1_3_workspace_still_only_talks_to_auth_api() -> None:
    """P1-3：不改 Workspace 认证 / 加载 / 导航逻辑（仍只调 auth API）。"""
    js = _read(WORKSPACE_JS)
    assert 'const API = "/api/auth";' in js
    for banned in ("/api/agents", "/api/channels", "/api/connections",
                   "/api/users", "/api/identities", "/api/interactions"):
        assert banned not in js, f"Workspace UI 越界调用：{banned}"
