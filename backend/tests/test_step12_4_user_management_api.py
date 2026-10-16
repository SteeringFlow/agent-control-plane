"""Step 12.4 —— User Management API 验收（Identity 绑定链的创建依赖起点）。

按 11.3/11.8/11.11 同一模式，经真实 HTTP 探针（TestClient）锁定：

    HTTP → Schema → Application Service → Domain → Repository → DB

- Create：201（id/name/status）；status 由领域默认 active（无 status 入参，
  extra=forbid）；name 不是唯一键——同名 User 合法共存（6.4 冻结）；
- List：空列表合法；Identity 绑定 UI 的 User 下拉数据源；
- Get：200 / 404；
- Patch：name/status 逐一生效；缺省/null = 不修改（12.3 冻结）；
  PATCH {} → noop 200；非 None 空串 → 422；携带 id → 422（immutable）；
- 边界：API/Schema 层不 import SQLAlchemy/persistence、不做业务判断；
  无 DELETE。
"""

import ast
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.api.management.users import build_users_api
from app.application.users import UserApplicationService
from app.persistence.models.user import UserModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

BACKEND = Path(__file__).resolve().parents[1]
API_ROUTER = BACKEND / "app" / "api" / "management" / "users.py"
SCHEMAS = BACKEND / "app" / "schemas" / "users.py"


def _factory(db_engine) -> sessionmaker:
    return sessionmaker(bind=db_engine, class_=Session, expire_on_commit=False)


def _service(db_engine) -> UserApplicationService:
    return UserApplicationService(
        uow_factory=lambda: SqlAlchemyUnitOfWork(session_factory=_factory(db_engine))
    )


@pytest.fixture()
def client(db_engine):
    """真实 HTTP 探针：test app 装配 build_users_api(内存库 Service)。"""
    test_app = FastAPI()
    test_app.include_router(build_users_api(_service(db_engine)))
    return TestClient(test_app)


def _db_count(db_engine) -> int:
    with Session(db_engine) as session:
        return len(session.query(UserModel).all())


_RESPONSE_FIELDS = {"id", "name", "status"}


# ================================================================== Create


def test_create_201_field_face_and_default_status(client) -> None:
    resp = client.post("/api/users", json={"name": "zhang-san"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert set(body) == _RESPONSE_FIELDS          # 恰好 3 个可见字段
    assert body["name"] == "zhang-san"
    assert body["status"] == "active"             # 领域默认（Channel Create 同一先例）
    assert body["id"] > 0


def test_create_same_name_users_coexist(client) -> None:
    """name 不是唯一键：同名 User 合法共存（唯一性归 Identity，6.4 冻结）。"""
    assert client.post("/api/users", json={"name": "张伟"}).status_code == 201
    assert client.post("/api/users", json={"name": "张伟"}).status_code == 201
    assert _db_count_by_name(client, "张伟") == 2


def _db_count_by_name(client, name: str) -> int:
    items = client.get("/api/users").json()["items"]
    return sum(1 for item in items if item["name"] == name)


@pytest.mark.parametrize(
    "bad_payload",
    [
        {"name": ""},                       # name 空
        {"name": "   "},                    # name 纯空白
        {"name": "x", "status": "active"},  # Create 无 status（领域默认 active）
        {"name": "x", "id": 1},             # id 只读（immutable，合同面 422）
        {},                                 # 缺必填 name
    ],
    ids=["empty-name", "blank-name", "status-not-creatable", "id-not-creatable",
         "missing-name"],
)
def test_create_invalid_input_422(client, bad_payload) -> None:
    resp = client.post("/api/users", json=bad_payload)
    assert resp.status_code == 422, resp.text
    assert _db_count_count_guard(client) == 0


def _db_count_count_guard(client) -> int:
    return client.get("/api/users").json()["total"]


# ================================================================== List


def test_list_empty_is_legal(client) -> None:
    resp = client.get("/api/users")
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "total": 0}


def test_list_returns_all_users_with_field_face(client) -> None:
    client.post("/api/users", json={"name": "zhang-san"})
    client.post("/api/users", json={"name": "li-si"})
    resp = client.get("/api/users")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert len(body["items"]) == 2
    for item in body["items"]:
        assert set(item) == _RESPONSE_FIELDS
    assert {item["name"] for item in body["items"]} == {"zhang-san", "li-si"}


# ================================================================== Get


def test_get_existing_200(client) -> None:
    created = client.post("/api/users", json={"name": "zhang-san"}).json()
    resp = client.get(f"/api/users/{created['id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == _RESPONSE_FIELDS
    assert body["id"] == created["id"]
    assert body["name"] == "zhang-san"


def test_get_missing_404(client) -> None:
    resp = client.get("/api/users/99999")
    assert resp.status_code == 404


# ================================================================== Patch


def test_patch_each_field_persists_and_omitted_stays(db_engine, client) -> None:
    user_id = client.post("/api/users", json={"name": "zhang-san"}).json()["id"]

    resp = client.patch(f"/api/users/{user_id}", json={"name": "zhang-san-v2"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "zhang-san-v2"
    assert body["status"] == "active"           # 未提及 → 不变

    resp = client.patch(f"/api/users/{user_id}", json={"status": "disabled"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "disabled"
    assert body["name"] == "zhang-san-v2"       # 前一次改名保留

    # 跨请求重新读取：改动真实落库
    assert client.get(f"/api/users/{user_id}").json() == body


def test_patch_null_means_no_change(client) -> None:
    """显式 null = 不修改（12.3 冻结；本 MVP 无清空语义）。"""
    user_id = client.post("/api/users", json={"name": "zhang-san"}).json()["id"]
    resp = client.patch(f"/api/users/{user_id}", json={"name": None, "status": None})
    assert resp.status_code == 200
    assert resp.json()["name"] == "zhang-san"
    assert resp.json()["status"] == "active"


def test_patch_empty_body_is_noop_200(client) -> None:
    """PATCH {} → noop（12.3 冻结，与 11.8 Channel 空 body = noop 一致）。"""
    user_id = client.post("/api/users", json={"name": "zhang-san"}).json()["id"]
    resp = client.patch(f"/api/users/{user_id}", json={})
    assert resp.status_code == 200
    assert resp.json()["name"] == "zhang-san"
    assert resp.json()["status"] == "active"


@pytest.mark.parametrize(
    "bad_payload",
    [
        {"name": ""},            # 非 None 空串仍拒绝
        {"name": "   "},
        {"status": ""},
        {"status": "  "},
        {"id": 2},               # immutable：id 不可经 PATCH 修改
    ],
    ids=["empty-name", "blank-name", "empty-status", "blank-status", "id-immutable"],
)
def test_patch_bad_values_422(client, bad_payload) -> None:
    user_id = client.post("/api/users", json={"name": "zhang-san"}).json()["id"]
    resp = client.patch(f"/api/users/{user_id}", json=bad_payload)
    assert resp.status_code == 422, f"{bad_payload} → {resp.status_code}"
    # 失败后状态不变
    assert client.get(f"/api/users/{user_id}").json()["name"] == "zhang-san"


def test_patch_missing_404(client) -> None:
    resp = client.patch("/api/users/99999", json={"name": "ghost"})
    assert resp.status_code == 404


# ================================================================== 边界守护


def test_api_and_schema_layers_have_no_orm_or_business_rules() -> None:
    """AST 守护：API/Schema 层不 import SQLAlchemy / app.persistence。"""
    for path in (API_ROUTER, SCHEMAS):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = {node.module or ""}
            else:
                continue
            for name in names:
                assert not name.startswith("sqlalchemy"), f"{path.name} import {name}"
                assert not name.startswith("app.persistence"), \
                    f"{path.name} import {name}"


def test_production_app_exposes_users_api() -> None:
    """main.py 组合根已装配 /api/users（openapi 契约可见；不触库）。"""
    from app.main import app

    client = TestClient(app)
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    paths = resp.json()["paths"]
    assert "/api/users" in paths
    assert "post" in paths["/api/users"] and "get" in paths["/api/users"]
    assert "/api/users/{user_id}" in paths
    assert "get" in paths["/api/users/{user_id}"] \
        and "patch" in paths["/api/users/{user_id}"]
    # 无 DELETE（管理态变更 = PATCH status）
    assert "delete" not in paths["/api/users/{user_id}"]
