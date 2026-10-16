"""M7-WB —— WorkBuddy 重定向 URI 端点最小护栏（只读断言，不触生产 DB）。

锁定一件事：`GET /oauth/callback` 是一个**公开、稳定、无 OAuth 逻辑**的落地页。

不测 OAuth（不存在）；不测任何认证/授权语义（本端点不参与它们）。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

CALLBACK_PATH = "/oauth/callback"


def test_t1_callback_returns_200() -> None:
    """Test 1：GET /oauth/callback 返回 200。"""
    assert TestClient(app).get(CALLBACK_PATH).status_code == 200


def test_t2_callback_requires_no_login() -> None:
    """Test 2：不要求登录——无 cookie / 无凭据即可访问（第三方平台跳转即此状态）。"""
    resp = TestClient(app).get(CALLBACK_PATH)
    assert resp.status_code == 200
    assert resp.status_code not in (401, 403)


def test_t3_callback_tolerates_query_parameters() -> None:
    """Test 3：带 ?code=...&state=... 也正常返回（不解释、不处理）。"""
    resp = TestClient(app).get(f"{CALLBACK_PATH}?code=test&state=test")
    assert resp.status_code == 200


def test_t4_callback_never_leaks_internal_information() -> None:
    """Test 4：响应不包含 tenant / connection / 内部 id / 凭据 / 堆栈等内部信息。"""
    body = TestClient(app).get(CALLBACK_PATH).text.lower()
    for leaked in (
        "tenant_id",
        "connection_id",
        "runtime_id",
        "api_key",
        "secret",
        "traceback",
    ):
        assert leaked not in body, f"落地页泄漏内部信息：{leaked}"
