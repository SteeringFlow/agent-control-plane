"""M7-WB —— WorkBuddy 重定向 URI 在 Connect 页面的存在性护栏（静态源码断言）。

只锁定用户可见面：Connect 页展示 WorkBuddy 重定向 URI + 复制按钮，
且地址来自部署环境（不硬编码 host）。
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND = REPO_ROOT / "frontend"
CONNECT_HTML = FRONTEND / "connect.html"
CONNECT_JS = FRONTEND / "connect.js"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_t5_connect_page_contains_callback_path() -> None:
    """Test 5：Connect 页包含 /oauth/callback。"""
    assert "/oauth/callback" in _read(CONNECT_HTML)


def test_t6_connect_page_shows_workbuddy_redirect_uri_label() -> None:
    """Test 6：Connect 页包含「WorkBuddy 重定向 URI」。"""
    assert "WorkBuddy 重定向 URI" in _read(CONNECT_HTML)


def test_t7_connect_page_teaches_where_to_paste_the_uri() -> None:
    """Test 7：Connect 页包含「开放平台」「第三方应用」「重定向 URI」引导。"""
    html = _read(CONNECT_HTML)
    assert "开放平台" in html
    assert "第三方应用" in html
    assert "重定向 URI" in html


def test_t8_connect_page_has_copy_button_and_uses_browser_clipboard() -> None:
    """Test 8：存在复制按钮；复制走浏览器 Clipboard API，且地址取自部署环境。"""
    html = _read(CONNECT_HTML)
    assert 'id="btn-copy-redirect"' in html
    assert "复制" in html

    js = _read(CONNECT_JS)
    assert "navigator.clipboard.writeText" in js
    assert "window.location.origin" in js
    # 不硬编码 host：显示值必须来自真实部署环境。
    for hardcoded in ("steeringflow.com", "localhost", "127.0.0.1"):
        assert hardcoded not in js, f"connect.js 硬编码 host：{hardcoded}"
