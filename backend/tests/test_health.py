"""验收 A/B/C：应用可构造、/health 返回 200、Swagger 契约暴露该端点。"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_returns_ok() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_openapi_exposes_health() -> None:
    """C：/docs 由 openapi.json 驱动，契约里必须能看到 /health。"""
    response = client.get("/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/health" in paths
    assert "get" in paths["/health"]


def test_docs_page_is_reachable() -> None:
    response = client.get("/docs")
    assert response.status_code == 200


def test_app_metadata_comes_from_settings() -> None:
    """应用标题来自配置，避免写死造成配置层形同虚设。"""
    assert app.title == "Agent Control Plane"
