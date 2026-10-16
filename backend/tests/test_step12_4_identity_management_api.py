"""Step 12.4 —— Identity Management API 验收（Runtime 第一站的数据入口）。

按 11.3/11.8/11.11 同一模式，经真实 HTTP 探针（TestClient）锁定：

    HTTP → Schema → Application Service → Domain → Repository → DB

- 创建依赖（12.4 目标）：user_id / channel_id 必须已存在 → 缺失 404；
  (channel_id, external_id) 已占用 → 409；同 external_id 跨 Channel → 201；
- Create：201（7 字段面）；status 由领域默认 active（无 status 入参，
  extra=forbid）；blank external_id / display_name → 422；
  metadata 非 dict → 422；
- List：空列表合法；跨渠道同 external_id 如实陈列、不合并；
- Get：200 / 404；
- Patch：display_name / status / metadata 逐一生效（metadata 提供 = 整段替换）；
  缺省/null = 不修改（12.3 冻结）；PATCH {} → noop 200；
  绑定骨架（user_id / channel_id / external_id）经 extra=forbid 在合同面 422
  （改绑定 = Identity Linking，绝不引入）；
- 边界：API/Schema 层不 import SQLAlchemy/persistence；无 DELETE；
  无自动创建 Identity（user/channel 缺失一律 404，绝不顺手建）。
"""

import ast
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.api.management.identities import build_identities_api
from app.application.identities import IdentityApplicationService
from app.domain.channel import Channel
from app.domain.user import User
from app.persistence.models.identity import IdentityModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

BACKEND = Path(__file__).resolve().parents[1]
API_ROUTER = BACKEND / "app" / "api" / "management" / "identities.py"
SCHEMAS = BACKEND / "app" / "schemas" / "identities.py"


def _factory(db_engine) -> sessionmaker:
    return sessionmaker(bind=db_engine, class_=Session, expire_on_commit=False)


def _service(db_engine) -> IdentityApplicationService:
    return IdentityApplicationService(
        uow_factory=lambda: SqlAlchemyUnitOfWork(session_factory=_factory(db_engine))
    )


def _make_user(db_engine, name: str = "zhang-san") -> int:
    with SqlAlchemyUnitOfWork(session_factory=_factory(db_engine)) as uow:
        user = uow.users.create(User(name=name))
        uow.commit()
        return user.id


def _make_channel(db_engine, name: str = "wecom-main", channel_type: str = "wecom") -> int:
    with SqlAlchemyUnitOfWork(session_factory=_factory(db_engine)) as uow:
        channel = uow.channels.create(Channel(name=name, type=channel_type))
        uow.commit()
        return channel.id


@pytest.fixture()
def client(db_engine):
    """真实 HTTP 探针：test app 装配 build_identities_api(内存库 Service)。"""
    test_app = FastAPI()
    test_app.include_router(build_identities_api(_service(db_engine)))
    return TestClient(test_app)


def _db_count(db_engine) -> int:
    with Session(db_engine) as session:
        return len(session.query(IdentityModel).all())


_RESPONSE_FIELDS = {"id", "user_id", "channel_id", "external_id",
                    "display_name", "status", "metadata"}


def _payload(user_id: int, channel_id: int, **overrides) -> dict:
    base = {
        "user_id": user_id,
        "channel_id": channel_id,
        "external_id": "zhangsan",
        "display_name": "张三",
        "metadata": {"dept": "sales"},
    }
    base.update(overrides)
    return base


# ================================================================== Create


def test_create_201_full_field_face_and_defaults(db_engine, client) -> None:
    user_id = _make_user(db_engine)
    channel_id = _make_channel(db_engine)

    resp = client.post("/api/identities", json=_payload(user_id, channel_id))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert set(body) == _RESPONSE_FIELDS          # 恰好 7 个可见字段
    assert body["user_id"] == user_id
    assert body["channel_id"] == channel_id
    assert body["external_id"] == "zhangsan"
    assert body["display_name"] == "张三"
    assert body["status"] == "active"             # 领域默认（Channel Create 同一先例）
    assert body["metadata"] == {"dept": "sales"}
    assert body["id"] > 0
    assert _db_count(db_engine) == 1


def test_create_minimal_payload_optional_fields_default(db_engine, client) -> None:
    """display_name / metadata 可选；缺省 → None。"""
    user_id = _make_user(db_engine)
    channel_id = _make_channel(db_engine)
    resp = client.post("/api/identities", json={
        "user_id": user_id, "channel_id": channel_id, "external_id": "zhangsan"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["display_name"] is None
    assert body["metadata"] is None


# ------------------------------------------------ 创建依赖（12.4 核心）


def test_create_missing_user_404_no_auto_creation(db_engine, client) -> None:
    """user_id 不存在 → 404；绝不自动创建 User/Identity（❌ 自动创建 Identity）。"""
    channel_id = _make_channel(db_engine)
    resp = client.post("/api/identities", json=_payload(99999, channel_id))
    assert resp.status_code == 404
    assert _db_count(db_engine) == 0


def test_create_missing_channel_404_no_auto_creation(db_engine, client) -> None:
    """channel_id 不存在 → 404；绝不自动创建 Channel/Identity。"""
    user_id = _make_user(db_engine)
    resp = client.post("/api/identities", json=_payload(user_id, 99999))
    assert resp.status_code == 404
    assert _db_count(db_engine) == 0


def test_creation_order_user_and_channel_first_then_identity(db_engine, client) -> None:
    """创建顺序：User / Channel 先存在，Identity 随后（User API 装配见 users 切片测试）。"""
    user_id = _make_user(db_engine)
    channel_id = _make_channel(db_engine)
    resp = client.post("/api/identities", json=_payload(user_id, channel_id))
    assert resp.status_code == 201
    assert resp.json()["user_id"] == user_id


# ------------------------------------------------ 唯一键 (channel_id, external_id)


def test_create_duplicate_same_channel_409(db_engine, client) -> None:
    """UNIQUE(channel_id, external_id)：同渠道同外部身份 → 409，零残留。"""
    user_id = _make_user(db_engine)
    channel_id = _make_channel(db_engine)
    assert client.post(
        "/api/identities", json=_payload(user_id, channel_id)).status_code == 201
    resp = client.post("/api/identities", json=_payload(user_id, channel_id))
    assert resp.status_code == 409
    assert _db_count(db_engine) == 1


def test_create_same_external_id_across_channels_201(db_engine, client) -> None:
    """WeCom/zhangsan 与 Feishu/zhangsan 是两个 Identity（不合并、不 linking）。"""
    user1 = _make_user(db_engine, "zhang-san")
    user2 = _make_user(db_engine, "li-si")
    wecom = _make_channel(db_engine, "wecom-main", "wecom")
    feishu = _make_channel(db_engine, "feishu-main", "feishu")

    a = client.post("/api/identities", json=_payload(user1, wecom))
    b = client.post("/api/identities", json=_payload(user2, feishu))
    assert a.status_code == 201 and b.status_code == 201
    assert a.json()["id"] != b.json()["id"]


@pytest.mark.parametrize(
    "bad_overrides",
    [
        {"external_id": ""},
        {"external_id": "   "},
        {"display_name": ""},
        {"display_name": "   "},
        {"metadata": ["not", "a", "dict"]},
        {"metadata": "not-a-dict"},
        {"status": "inactive"},           # Create 无 status（领域默认 active）
        {"external_id": 123},             # 形状校验（external_id 必须 str）
    ],
    ids=["empty-external", "blank-external", "empty-display", "blank-display",
         "metadata-list", "metadata-str", "status-not-creatable", "bad-external-id"],
)
def test_create_invalid_input_422(db_engine, client, bad_overrides) -> None:
    user_id = _make_user(db_engine)
    channel_id = _make_channel(db_engine)
    resp = client.post(
        "/api/identities", json=_payload(user_id, channel_id, **bad_overrides))
    assert resp.status_code == 422, resp.text
    assert _db_count(db_engine) == 0


# ================================================================== List


def test_list_empty_is_legal(client) -> None:
    resp = client.get("/api/identities")
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "total": 0}


def test_list_returns_all_without_merging_cross_channel(db_engine, client) -> None:
    """跨渠道同 external_id 的两条 Identity 如实陈列（不合并）。"""
    user1 = _make_user(db_engine, "zhang-san")
    user2 = _make_user(db_engine, "li-si")
    wecom = _make_channel(db_engine, "wecom-main", "wecom")
    feishu = _make_channel(db_engine, "feishu-main", "feishu")
    client.post("/api/identities", json=_payload(user1, wecom))
    client.post("/api/identities", json=_payload(user2, feishu))

    resp = client.get("/api/identities")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    for item in body["items"]:
        assert set(item) == _RESPONSE_FIELDS
        assert item["external_id"] == "zhangsan"
    assert {item["channel_id"] for item in body["items"]} == {wecom, feishu}


# ================================================================== Get


def test_get_existing_200(db_engine, client) -> None:
    user_id = _make_user(db_engine)
    channel_id = _make_channel(db_engine)
    created = client.post(
        "/api/identities", json=_payload(user_id, channel_id)).json()
    resp = client.get(f"/api/identities/{created['id']}")
    assert resp.status_code == 200
    assert set(resp.json()) == _RESPONSE_FIELDS
    assert resp.json()["id"] == created["id"]


def test_get_missing_404(client) -> None:
    resp = client.get("/api/identities/99999")
    assert resp.status_code == 404


# ================================================================== Patch


def test_patch_each_field_persists_and_binding_stays(db_engine, client) -> None:
    user_id = _make_user(db_engine)
    channel_id = _make_channel(db_engine)
    identity_id = client.post(
        "/api/identities", json=_payload(user_id, channel_id)).json()["id"]

    resp = client.patch(f"/api/identities/{identity_id}", json={
        "display_name": "张伟", "status": "disabled", "metadata": {"dept": "ops"}})
    assert resp.status_code == 200
    body = resp.json()
    assert body["display_name"] == "张伟"
    assert body["status"] == "disabled"
    assert body["metadata"] == {"dept": "ops"}      # 提供 = 整段替换
    # 绑定骨架不变
    assert body["user_id"] == user_id
    assert body["channel_id"] == channel_id
    assert body["external_id"] == "zhangsan"

    assert client.get(f"/api/identities/{identity_id}").json() == body


def test_patch_partial_keeps_omitted_fields(db_engine, client) -> None:
    user_id = _make_user(db_engine)
    channel_id = _make_channel(db_engine)
    identity_id = client.post(
        "/api/identities", json=_payload(user_id, channel_id)).json()["id"]

    resp = client.patch(
        f"/api/identities/{identity_id}", json={"display_name": "张伟"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["display_name"] == "张伟"
    assert body["status"] == "active"               # 未提及 → 不变
    assert body["metadata"] == {"dept": "sales"}


def test_patch_null_means_no_change(db_engine, client) -> None:
    """显式 null = 不修改（12.3 冻结；metadata=null 是保留，不是清空）。"""
    user_id = _make_user(db_engine)
    channel_id = _make_channel(db_engine)
    identity_id = client.post(
        "/api/identities", json=_payload(user_id, channel_id)).json()["id"]

    resp = client.patch(f"/api/identities/{identity_id}", json={
        "display_name": None, "status": None, "metadata": None})
    assert resp.status_code == 200
    body = resp.json()
    assert body["display_name"] == "张三"
    assert body["status"] == "active"
    assert body["metadata"] == {"dept": "sales"}


def test_patch_empty_body_is_noop_200(db_engine, client) -> None:
    """PATCH {} → noop（12.3 冻结，与 11.8 Channel 空 body = noop 一致）。"""
    user_id = _make_user(db_engine)
    channel_id = _make_channel(db_engine)
    identity_id = client.post(
        "/api/identities", json=_payload(user_id, channel_id)).json()["id"]

    resp = client.patch(f"/api/identities/{identity_id}", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["display_name"] == "张三"
    assert body["status"] == "active"
    assert body["metadata"] == {"dept": "sales"}


@pytest.mark.parametrize(
    "bad_payload",
    [
        {"display_name": ""},
        {"display_name": "   "},
        {"status": ""},
        {"metadata": "not-a-dict"},
        {"metadata": ["list"]},
        {"user_id": 99999},               # immutable binding：合同面 422
        {"channel_id": 99999},            # immutable binding：合同面 422
        {"external_id": "someone-else"},  # immutable binding：合同面 422
    ],
    ids=["empty-display", "blank-display", "empty-status", "metadata-str",
         "metadata-list", "user-id-immutable", "channel-id-immutable",
         "external-id-immutable"],
)
def test_patch_bad_values_422(db_engine, client, bad_payload) -> None:
    user_id = _make_user(db_engine)
    channel_id = _make_channel(db_engine)
    identity_id = client.post(
        "/api/identities", json=_payload(user_id, channel_id)).json()["id"]

    resp = client.patch(f"/api/identities/{identity_id}", json=bad_payload)
    assert resp.status_code == 422, f"{bad_payload} → {resp.status_code}"
    # 失败后状态不变（绑定骨架尤其不变——无 Identity Linking 借道）
    current = client.get(f"/api/identities/{identity_id}").json()
    assert current["user_id"] == user_id
    assert current["channel_id"] == channel_id
    assert current["external_id"] == "zhangsan"


def test_patch_missing_404(client) -> None:
    resp = client.patch("/api/identities/99999", json={"display_name": "ghost"})
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


def test_production_app_exposes_identities_api() -> None:
    """main.py 组合根已装配 /api/identities（openapi 契约可见；不触库）。"""
    from app.main import app

    client = TestClient(app)
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    paths = resp.json()["paths"]
    assert "/api/identities" in paths
    assert "post" in paths["/api/identities"] and "get" in paths["/api/identities"]
    assert "/api/identities/{identity_id}" in paths
    assert "get" in paths["/api/identities/{identity_id}"] \
        and "patch" in paths["/api/identities/{identity_id}"]
    # 无 DELETE；且 Runtime 数据面（/api/channels/{id}/wecom/webhook）不在管理 openapi 契约中
    assert "delete" not in paths["/api/identities/{identity_id}"]
