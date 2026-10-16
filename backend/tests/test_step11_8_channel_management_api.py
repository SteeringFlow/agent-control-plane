"""Step 11.8 —— Channel Management API 实现验收（第二条 Management Vertical Slice）。

按 11.7 审计裁决 + 用户 11.8 五条冻结合同，经真实 HTTP 探针（TestClient）锁定：

    HTTP → Schema → Application Service → Domain → Repository → DB

五条冻结合同：
1. Response contract：字段面 == {id,name,type,status,config}；config 经
   sanitize_channel_config() 脱敏（token/encoding_aes_key/corpsecret → ********，
   corp_id/agentid/api_base/timeout_s 原样返回）；
2. Create contract：首次配置允许提交完整 config（含 secret），secret 真实落库；
3. Patch contract：config = load → merge incoming → persist（不是整段替换）——
   只改非密字段时密键保留；
4. Secret 不可通过 GET/List 泄漏（与 Agent「credential_ref write-only」的区别：
   Channel 无 credential_ref，凭据住 config → 读时脱敏）；
5. type 在 15.2.3 收口为受控集合 ALLOWED_CHANNEL_TYPES = ("wecom", "local")
   （第二个 Channel Adapter 出现使 11.7 deferred decision 到期）：Schema 仍是
   自由 str，非法值由 Application Service → 422。

不做（11.8 边界）：DELETE / Health Check / Webhook（api/runtime 边界不混合）/
Routing / Connection / Channel Registry / Credential Vault / Channel Secret 独立表。
"""

import ast
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.api.management.channels import build_channels_api
from app.application.channels import ChannelApplicationService
from app.persistence.models.channel import ChannelModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

BACKEND = Path(__file__).resolve().parents[1]
API_ROUTER = BACKEND / "app" / "api" / "management" / "channels.py"
SCHEMAS = BACKEND / "app" / "schemas" / "channels.py"

_REDACTED = "********"


def _factory(db_engine) -> sessionmaker:
    return sessionmaker(bind=db_engine, class_=Session, expire_on_commit=False)


def _service(db_engine) -> ChannelApplicationService:
    return ChannelApplicationService(
        uow_factory=lambda: SqlAlchemyUnitOfWork(session_factory=_factory(db_engine))
    )


@pytest.fixture()
def client(db_engine):
    """真实 HTTP 探针：test app 装配 build_channels_api(内存库 Service)。"""
    test_app = FastAPI()
    test_app.include_router(build_channels_api(_service(db_engine)))
    return TestClient(test_app)


def _db_get(db_engine, channel_id: int) -> ChannelModel:
    """绕过全部应用层，直接读 DB 行（验证 secret 真实落库 / merge 保留）。"""
    with Session(db_engine) as session:
        row = session.get(ChannelModel, channel_id)
        session.expunge(row)
        return row


# 完整 WeCom config（11.7 §2.2 真实键面）：含入站验签材料 + 出站凭据
_FULL_CONFIG = {
    "token": "wecom-token-secret",
    "encoding_aes_key": "a" * 43,
    "corp_id": "corp-001",
    "corpsecret": "wecom-corpsecret-value",
    "agentid": 1000002,
    "api_base": "https://qyapi.weixin.qq.com",
    "timeout_s": 10,
}

_FULL_PAYLOAD = {
    "name": "wecom-main",
    "type": "wecom",
    "config": dict(_FULL_CONFIG),
}

_RESPONSE_FIELDS = {"id", "name", "type", "status", "config"}
_SECRET_KEYS = {"token", "encoding_aes_key", "corpsecret"}
_VISIBLE_KEYS = {"corp_id", "agentid", "api_base", "timeout_s"}


def _assert_sanitized(config: dict | None) -> None:
    """合同 #1/#4：密键一律脱敏占位，非密键原样可见。"""
    assert config is not None
    for key in _SECRET_KEYS:
        assert config[key] == _REDACTED, f"响应泄漏密键 {key}：{config[key]!r}"
    for key in _VISIBLE_KEYS:
        assert config[key] == _FULL_CONFIG[key]


# ================================================================== 合同 #2：Create


def test_create_201_full_config_persisted_response_sanitized(db_engine, client) -> None:
    resp = client.post("/api/channels", json=_FULL_PAYLOAD)
    assert resp.status_code == 201
    body = resp.json()
    assert set(body) == _RESPONSE_FIELDS          # 恰好 5 个可见字段
    assert body["name"] == "wecom-main"
    assert body["type"] == "wecom"
    assert body["status"] == "active"
    assert body["id"] > 0
    _assert_sanitized(body["config"])             # 响应脱敏

    row = _db_get(db_engine, body["id"])          # 合同 #2：secret 真实落库
    assert row.config == _FULL_CONFIG


def test_create_default_status_is_active(client) -> None:
    resp = client.post("/api/channels", json={"name": "c1", "type": "wecom"})
    assert resp.status_code == 201
    assert resp.json()["status"] == "active"
    assert resp.json()["config"] is None


def test_create_without_config_is_legal(client) -> None:
    """config 可省略（先建渠道、后补配置的管理路径）。"""
    resp = client.post("/api/channels", json={"name": "lazy", "type": "wecom"})
    assert resp.status_code == 201
    assert resp.json()["config"] is None


@pytest.mark.parametrize(
    "bad_payload",
    [
        {**_FULL_PAYLOAD, "name": ""},
        {**_FULL_PAYLOAD, "name": "   "},
        {**_FULL_PAYLOAD, "type": ""},
        {**_FULL_PAYLOAD, "type": "  "},
        {**_FULL_PAYLOAD, "config": ["not", "a", "dict"]},
        {**_FULL_PAYLOAD, "config": "not-a-dict"},
    ],
    ids=["empty-name", "blank-name", "empty-type", "blank-type",
         "config-list", "config-str"],
)
def test_create_invalid_input_422(client, bad_payload) -> None:
    resp = client.post("/api/channels", json=bad_payload)
    assert resp.status_code == 422, resp.text


def test_create_duplicate_same_type_name_409(client) -> None:
    assert client.post("/api/channels", json=_FULL_PAYLOAD).status_code == 201
    resp = client.post("/api/channels", json=_FULL_PAYLOAD)
    assert resp.status_code == 409


def test_create_same_name_different_type_allowed(client) -> None:
    """渠道重复性是平台限定的：wecom 与 local 的同名渠道合法共存（15.2.3 受控集合内）。"""
    assert client.post("/api/channels", json=_FULL_PAYLOAD).status_code == 201
    resp = client.post("/api/channels",
                       json={"name": "wecom-main", "type": "local"})
    assert resp.status_code == 201


# ================================================================== 合同 #4：List / Get 不泄漏


def test_list_empty_is_legal(client) -> None:
    resp = client.get("/api/channels")
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "total": 0}


def test_list_returns_all_sanitized_without_secret_leak(client) -> None:
    client.post("/api/channels", json=_FULL_PAYLOAD)
    client.post("/api/channels", json={"name": "local-main", "type": "local"})
    resp = client.get("/api/channels")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    for item in body["items"]:
        assert set(item) == _RESPONSE_FIELDS
        if item["type"] == "wecom":
            _assert_sanitized(item["config"])
    assert {item["name"] for item in body["items"]} == {"wecom-main", "local-main"}


def test_get_existing_200_sanitized(db_engine, client) -> None:
    created = client.post("/api/channels", json=_FULL_PAYLOAD).json()
    resp = client.get(f"/api/channels/{created['id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == _RESPONSE_FIELDS
    _assert_sanitized(body["config"])


def test_get_missing_404(client) -> None:
    resp = client.get("/api/channels/99999")
    assert resp.status_code == 404


# ================================================================== 合同 #3：Patch merge


def test_patch_config_merge_preserves_secret_keys(db_engine, client) -> None:
    """冻结合同 #3 的核心：PATCH 只改非密字段 → config 中密键保留（merge 非替换）。"""
    channel_id = client.post("/api/channels", json=_FULL_PAYLOAD).json()["id"]

    resp = client.patch(f"/api/channels/{channel_id}",
                        json={"config": {"agentid": 1000003}})
    assert resp.status_code == 200
    config = resp.json()["config"]
    for key in _SECRET_KEYS:
        assert config[key] == _REDACTED, f"响应泄漏密键 {key}"
    assert config["corp_id"] == "corp-001"                  # 未提供的非密键保留
    assert config["agentid"] == 1000003                     # 提供的键覆写

    row = _db_get(db_engine, channel_id)                    # 未提供的密键保留
    assert row.config["token"] == "wecom-token-secret"
    assert row.config["encoding_aes_key"] == "a" * 43
    assert row.config["corpsecret"] == "wecom-corpsecret-value"
    assert row.config["corp_id"] == "corp-001"
    assert row.config["agentid"] == 1000003


def test_patch_can_overwrite_secret_key_explicitly(db_engine, client) -> None:
    """merge 语义下密键也可被显式覆写（管理员轮换 secret 的合法路径）。"""
    channel_id = client.post("/api/channels", json=_FULL_PAYLOAD).json()["id"]

    resp = client.patch(f"/api/channels/{channel_id}",
                        json={"config": {"corpsecret": "rotated-secret"}})
    assert resp.status_code == 200
    assert resp.json()["config"]["corpsecret"] == _REDACTED

    row = _db_get(db_engine, channel_id)
    assert row.config["corpsecret"] == "rotated-secret"      # 覆写落库
    assert row.config["token"] == "wecom-token-secret"       # 其它密键不受影响


def test_patch_name_type_status_persist(db_engine, client) -> None:
    channel_id = client.post("/api/channels", json=_FULL_PAYLOAD).json()["id"]

    resp = client.patch(f"/api/channels/{channel_id}", json={
        "name": "wecom-support",
        "status": "inactive",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "wecom-support"
    assert body["status"] == "inactive"
    assert body["type"] == "wecom"               # 未提及 → 不变
    _assert_sanitized(body["config"])            # config 未提及 → 原样（脱敏回显）

    refetched = client.get(f"/api/channels/{channel_id}").json()
    assert refetched == body                     # 改动真实落库


def test_patch_config_omitted_and_null_leave_unchanged(db_engine, client) -> None:
    """缺省 / 显式 null 都 = 不修改 config。"""
    channel_id = client.post("/api/channels", json=_FULL_PAYLOAD).json()["id"]

    client.patch(f"/api/channels/{channel_id}", json={"name": "renamed"})
    assert _db_get(db_engine, channel_id).config == _FULL_CONFIG

    client.patch(f"/api/channels/{channel_id}", json={"config": None})
    assert _db_get(db_engine, channel_id).config == _FULL_CONFIG


def test_patch_merge_on_channel_without_config(db_engine, client) -> None:
    """原 config 为空时 merge = 直接建立提供的键。"""
    channel_id = client.post(
        "/api/channels", json={"name": "lazy", "type": "wecom"}).json()["id"]

    resp = client.patch(f"/api/channels/{channel_id}",
                        json={"config": {"corp_id": "corp-9"}})
    assert resp.status_code == 200
    assert resp.json()["config"] == {"corp_id": "corp-9"}


def test_patch_bad_values_422(client) -> None:
    channel_id = client.post("/api/channels", json=_FULL_PAYLOAD).json()["id"]
    for bad in (
        {"name": ""},
        {"type": ""},
        {"status": "   "},
        {"config": "not-a-dict"},
        {"config": ["nope"]},
    ):
        resp = client.patch(f"/api/channels/{channel_id}", json=bad)
        assert resp.status_code == 422, f"{bad} → {resp.status_code}"


def test_patch_duplicate_type_name_409(client) -> None:
    """改名 / 重分类撞已有 (type, name) → 409（与 Agent 11.2 §5.3 同一守卫）。"""
    client.post("/api/channels", json=_FULL_PAYLOAD)
    other_id = client.post(
        "/api/channels", json={"name": "wecom-second", "type": "wecom"}).json()["id"]
    resp = client.patch(f"/api/channels/{other_id}", json={"name": "wecom-main"})
    assert resp.status_code == 409

    # 重分类同样受 (type, name) 守卫
    third_id = client.post(
        "/api/channels", json={"name": "wecom-main", "type": "local"}).json()["id"]
    resp = client.patch(f"/api/channels/{third_id}", json={"type": "wecom"})
    assert resp.status_code == 409


def test_patch_self_type_name_not_duplicate(client) -> None:
    """PATCH 未实际改变 (type, name)（仅改其它字段）不触发 409；改自身同名也合法。"""
    channel_id = client.post("/api/channels", json=_FULL_PAYLOAD).json()["id"]
    assert client.patch(
        f"/api/channels/{channel_id}", json={"name": "wecom-main"}).status_code == 200
    assert client.patch(
        f"/api/channels/{channel_id}",
        json={"name": "wecom-main", "type": "wecom"}).status_code == 200


def test_patch_missing_404(client) -> None:
    resp = client.patch("/api/channels/99999", json={"name": "ghost"})
    assert resp.status_code == 404


def test_patch_empty_body_is_noop_200(db_engine, client) -> None:
    channel_id = client.post("/api/channels", json=_FULL_PAYLOAD).json()["id"]
    resp = client.patch(f"/api/channels/{channel_id}", json={})
    assert resp.status_code == 200
    assert resp.json()["name"] == "wecom-main"
    assert _db_get(db_engine, channel_id).config == _FULL_CONFIG


# ================================================================== 合同 #5（15.2.3 修订）：type 受控集合


def test_unlisted_channel_type_rejected_422(client) -> None:
    """15.2.3 合同 #5 修订：type 受控集合 = ("wecom", "local")——未实现 Adapter
    的类型（如 console/feishu/dingtalk）→ 422，而不是 201 active。

    Schema 层仍是自由 str，受控集合的唯一裁决点在 Application Service
    （API/Schema 不重复实现业务规则）；OpenAPI 以 description 文档化取值。"""
    resp = client.post("/api/channels", json={"name": "future-ch", "type": "console"})
    assert resp.status_code == 422

    ok = client.post("/api/channels", json={"name": "local-ch", "type": "local"})
    assert ok.status_code == 201
    assert ok.json()["type"] == "local"

    schema_src = SCHEMAS.read_text(encoding="utf-8")
    assert "Enum" not in schema_src and "enum" not in schema_src
    router_src = API_ROUTER.read_text(encoding="utf-8")
    for token in ("ALLOWED_", "Literal["):
        assert token not in router_src


# ================================================================== 边界守护


def test_api_and_schema_layers_have_no_business_rules_or_orm() -> None:
    """AST 守护：API/Schema 层不 import SQLAlchemy / app.persistence；
    API 层不做业务判断（无白名单 / 无 URL 解析——业务规则单一裁决点在服务层）。"""
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
    router_src = API_ROUTER.read_text(encoding="utf-8")
    for token in ("ALLOWED_", "urlsplit", "urlparse"):
        assert token not in router_src, f"API 层出现业务判断：{token}"


def test_no_delete_endpoint_anywhere(client) -> None:
    """不做 DELETE（软下线 = PATCH status；DROP 裁决维持）。"""
    assert client.delete("/api/channels/1").status_code in (405, 404)


# ================================================================== 生产装配


def test_production_app_exposes_channels_api() -> None:
    """main.py 组合根已装配 /api/channels（openapi 契约可见；不触库）。"""
    from app.main import app

    client = TestClient(app)
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    paths = resp.json()["paths"]
    assert "/api/channels" in paths
    assert "post" in paths["/api/channels"] and "get" in paths["/api/channels"]
    assert "/api/channels/{channel_id}" in paths
    assert "get" in paths["/api/channels/{channel_id}"] \
        and "patch" in paths["/api/channels/{channel_id}"]
    # 无 DELETE；管理面与 runtime 入站 webhook 边界互不混合
    #（12.6 翻转：runtime webhook 已在生产组合根装配 —— 在场合法，
    # 但仍由独立 router（api/runtime）承载，管理面 /api/channels 本身依旧无 DELETE）
    assert "delete" not in paths["/api/channels/{channel_id}"]
