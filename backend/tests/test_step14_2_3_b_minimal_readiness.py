"""Step 14.2.3.B —— Minimal Readiness（/health = 存活 + DB 可达性）。

只验证一件事：`GET /health` 真的执行了一次数据库访问（SELECT 1），
且**只**检查数据库——不检查任何外部服务、不引入 metrics / tracing / 新状态。

验收对应：
- health 实际执行 SELECT 1（DB 可达 → 200；不可达 → 503）；
- 响应体不变：`{"status": "ok"}`（不新增字段）；
- 不检查 Agent / Channel / ACP / WeCom / Driver / 外部网络（机器化守卫）。
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.persistence.database import get_db


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client


# ---------------------------------------------------------------- 正常路径


def test_health_returns_ok_with_body_unchanged(client) -> None:
    """DB 可达 → 200，且响应体仍是 {"status": "ok"}（不新增字段/状态）。"""
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_executes_select_1(client, monkeypatch) -> None:
    """关键：/health 必须**真的**执行一次 SELECT 1（不是只返回固定 ok）。"""
    executed: list[str] = []

    class _FakeDbapiConnection:
        def execute(self, sql: str) -> None:  # 记录实际执行的语句
            executed.append(sql)

    class _FakeConnection:
        connection = _FakeDbapiConnection()

    class _FakeSession:
        def connection(self) -> _FakeConnection:
            return _FakeConnection()

    def fake_get_db():
        yield _FakeSession()

    # FastAPI 在路由定义时已捕获依赖对象，故用 dependency_overrides 替换
    app.dependency_overrides[get_db] = fake_get_db
    try:
        response = client.get("/health")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert executed == ["SELECT 1"], f"readiness 应执行一次 SELECT 1，实际：{executed}"


# ---------------------------------------------------------------- 不可达路径


def test_health_returns_503_when_db_unreachable(client, monkeypatch) -> None:
    """DB 不可达 → 503（Docker healthcheck 据此判 unhealthy）。"""
    from fastapi import HTTPException

    def failing_probe(db) -> bool:
        return False

    monkeypatch.setattr("app.api.health._database_is_reachable", failing_probe)

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["detail"] == "database unreachable"
    assert HTTPException is not None  # 503 由标准 HTTPException 表达（非新状态机）


# ---------------------------------------------------------------- 范围守卫


def test_health_does_not_check_external_services() -> None:
    """范围守卫：/health 只查数据库，不检查外部服务/配置/Driver/网络。

    否则 /health 会逐步演化成复杂的系统状态机（14.1 明确禁止）。
    """
    from pathlib import Path

    import ast

    path = Path(__file__).resolve().parents[1] / "app" / "api" / "health.py"
    source = path.read_text(encoding="utf-8")

    # 只看**可执行代码**：先剔除 docstring（docstring 里会列举「刻意不检查 x/y/z」，
    # 直接全文扫描会误伤自己）。
    tree = ast.parse(source, filename=str(path))
    docstrings: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if not (isinstance(node.body, list) and node.body):
            continue
        first = node.body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            docstrings.append(first.value.value)

    code_only = source
    for text in docstrings:
        code_only = code_only.replace(text, "")

    forbidden_markers = [
        "agent",
        "channel",
        "acp",
        "wecom",
        "driver",
        "pending",
        "httpx",
        "requests",
        "socket",
        "urlopen",
    ]
    lowered = code_only.lower()
    offenders = [m for m in forbidden_markers if m in lowered]
    assert not offenders, f"/health 的可执行代码不应检查这些外部关注点：{offenders}"


def test_health_imports_no_sqlalchemy() -> None:
    """分层守卫：api 层不得 import SQLAlchemy（由 import 边界测试兜底，此处聚焦 health）。"""
    import ast
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "app" / "api" / "health.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    offenders = {m for m in imported if m.split(".")[0] == "sqlalchemy"}
    assert not offenders, f"api/health.py 不得 import SQLAlchemy：{offenders}"
