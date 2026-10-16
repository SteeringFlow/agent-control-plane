"""Step 11.3 —— Agent Management API 实现验收（第一条 Management Vertical Slice）。

按 11.2 冻结契约 + 用户 11.3 验收清单，经真实 HTTP 探针（TestClient）锁定：

    HTTP → Schema → Application Service → Domain → Repository → DB

- Create：201 全字段回显；credential_ref 可写且响应永不出现；
  credential_ref 是 optional opaque reference（None / "" / 纯空白 → 归一 None，不因之失败）；
  非法 type/endpoint/status(error) → 422；duplicate name → 409；
- List：全部可见字段、不泄漏 credential_ref、空列表合法；
- Get：200 / 404 / 无 credential_ref；
- Patch：六字段逐一生效；credential_ref 缺省/null=不变、""=422、"new"=覆写；
  status=error 拒收（422）；运行态 error 可被管理员 PATCH active 手动清障；
- 边界：API/Schema 层不 import SQLAlchemy/persistence、不做业务判断；
  name 去重是应用级守卫（无 DB UNIQUE）——TOCTOU 竞态事实被记录而非顺手迁移。
"""

from dataclasses import fields
from pathlib import Path

import ast
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.api.management.agents import build_agents_api
from app.application.agents import AgentApplicationService
from app.persistence.models.agent import AgentModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

BACKEND = Path(__file__).resolve().parents[1]
API_ROUTER = BACKEND / "app" / "api" / "management" / "agents.py"
SCHEMAS = BACKEND / "app" / "schemas" / "agents.py"


def _factory(db_engine) -> sessionmaker:
    return sessionmaker(bind=db_engine, class_=Session, expire_on_commit=False)


def _service(db_engine) -> AgentApplicationService:
    return AgentApplicationService(
        uow_factory=lambda: SqlAlchemyUnitOfWork(session_factory=_factory(db_engine))
    )


@pytest.fixture()
def client(db_engine):
    """真实 HTTP 探针：test app 装配 build_agents_api(内存库 Service)。"""
    test_app = FastAPI()
    test_app.include_router(build_agents_api(_service(db_engine)))
    return TestClient(test_app)


def _db_get(db_engine, agent_id: int) -> AgentModel:
    """绕过全部应用层，直接读 DB 行（验证 credential_ref 真实落库/覆写）。"""
    with Session(db_engine) as session:
        row = session.get(AgentModel, agent_id)
        session.expunge(row)
        return row


_FULL_PAYLOAD = {
    "name": "acp-prod",
    "type": "acp",
    "endpoint": "https://agent.example.com/acp",
    "status": "active",
    "config": {"protocol": "tencent-acp-sse", "timeout_s": 30},
    "credential_ref": "vault://agent/acp-prod",
}

_RESPONSE_FIELDS = {
    "id", "name", "type", "status", "endpoint", "config",
    "created_at", "updated_at",
}


# ================================================================== Create


def test_create_201_full_fields_and_credential_ref_write_only(db_engine, client) -> None:
    resp = client.post("/api/agents", json=_FULL_PAYLOAD)
    assert resp.status_code == 201
    body = resp.json()
    assert set(body) == _RESPONSE_FIELDS          # 恰好 8 个可见字段
    assert body["name"] == "acp-prod"
    assert body["type"] == "acp"
    assert body["status"] == "active"
    assert body["endpoint"] == "https://agent.example.com/acp"
    assert body["config"] == {"protocol": "tencent-acp-sse", "timeout_s": 30}
    assert body["id"] > 0
    assert body["created_at"] is not None and body["updated_at"] is not None
    assert "credential_ref" not in body           # 引用不出

    row = _db_get(db_engine, body["id"])
    assert row.credential_ref == "vault://agent/acp-prod"  # 引用入：真实落库


def test_create_credential_ref_optional_normalized(db_engine, client) -> None:
    """credential_ref = optional opaque reference（0.1.2.2.1）：

    None / "" / 纯空白都不再因 credential_ref 本身失败，且一律归一为 None。
    """
    for name, ref in (("ref-none", None), ("ref-empty", ""), ("ref-blank", "   ")):
        payload = {"name": name, "type": "acp"}
        if ref is not None:
            payload["credential_ref"] = ref
        resp = client.post("/api/agents", json=payload)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert "credential_ref" not in body                 # 引用不出
        assert _db_get(db_engine, body["id"]).credential_ref is None  # 归一为 None


def test_create_endpoint_optional_normalized(db_engine, client) -> None:
    """endpoint = optional descriptive metadata（0.1.2.3.1.1）：

    None / "" / 纯空白都不再因 endpoint 本身失败，且一律归一为 None——完全镜像
    credential_ref 的 create 侧归一化纪律。endpoint 是描述性元数据，非访问地址；
    PATCH（update）合同不变（`""` 是否清除属独立合同问题）。
    """
    for name, ep in (("ep-none", None), ("ep-empty", ""), ("ep-blank", "   ")):
        payload = {"name": name, "type": "acp"}
        if ep is not None:
            payload["endpoint"] = ep
        resp = client.post("/api/agents", json=payload)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["endpoint"] is None                        # 归一为 None（可见字段）
        assert _db_get(db_engine, body["id"]).endpoint is None


def test_create_endpoint_nonblank_preserved_and_still_validated(client) -> None:
    """非空 endpoint 原样保留；非法 URL 仍 422（归一化不削弱 URL 校验）。"""
    resp = client.post("/api/agents", json={
        "name": "ep-url", "type": "acp", "endpoint": "https://agent.example.com/acp"})
    assert resp.status_code == 201
    assert resp.json()["endpoint"] == "https://agent.example.com/acp"

    bad = client.post("/api/agents", json={
        "name": "ep-bad", "type": "acp", "endpoint": "not-a-url"})
    assert bad.status_code == 422


def test_create_default_status_is_active(client) -> None:
    resp = client.post("/api/agents", json={"name": "a1", "type": "acp"})
    assert resp.status_code == 201
    assert resp.json()["status"] == "active"


def test_create_explicit_inactive_allowed(client) -> None:
    resp = client.post("/api/agents", json={"name": "a2", "type": "acp",
                                            "status": "inactive"})
    assert resp.status_code == 201
    assert resp.json()["status"] == "inactive"


@pytest.mark.parametrize(
    "bad_payload",
    [
        {**_FULL_PAYLOAD, "type": "bogus"},                 # type 越界
        {**_FULL_PAYLOAD, "type": ""},                      # type 空
        {**_FULL_PAYLOAD, "endpoint": "not-a-url"},         # 无 scheme/host
        {**_FULL_PAYLOAD, "endpoint": "ftp://x.example"},   # scheme 越界
        {**_FULL_PAYLOAD, "endpoint": "https://"},          # 缺 host
        {**_FULL_PAYLOAD, "status": "error"},               # 运行态拒收（11.2 §1.2）
        {**_FULL_PAYLOAD, "status": "disabled"},            # 非管理态
        {**_FULL_PAYLOAD, "name": ""},                      # name 空
        {**_FULL_PAYLOAD, "name": "   "},                   # name 纯空白
        {**_FULL_PAYLOAD, "config": ["not", "a", "dict"]},  # config 非 dict
    ],
    ids=[
        "bad-type", "empty-type", "endpoint-no-scheme", "endpoint-ftp-scheme",
        "endpoint-no-host", "status-error", "status-disabled", "empty-name",
        "blank-name", "config-not-dict",
    ],
)
def test_create_invalid_input_422(client, bad_payload) -> None:
    resp = client.post("/api/agents", json=bad_payload)
    assert resp.status_code == 422, resp.text


def test_create_duplicate_name_409(client) -> None:
    assert client.post("/api/agents", json=_FULL_PAYLOAD).status_code == 201
    resp = client.post("/api/agents", json=_FULL_PAYLOAD)
    assert resp.status_code == 409


def test_create_duplicate_rolls_back_nothing(db_engine, client) -> None:
    """409 后事务回滚：不残留第二次的半成品行。"""
    client.post("/api/agents", json=_FULL_PAYLOAD)
    client.post("/api/agents", json={**_FULL_PAYLOAD,
                                     "endpoint": "https://other.example"})
    with Session(db_engine) as session:
        count = len(session.query(AgentModel).all())
    assert count == 1


# ================================================================== List


def test_list_empty_is_legal(client) -> None:
    resp = client.get("/api/agents")
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "total": 0}


def test_list_returns_all_visible_fields_without_credential_ref(client) -> None:
    client.post("/api/agents", json=_FULL_PAYLOAD)
    client.post("/api/agents", json={**_FULL_PAYLOAD, "name": "acp-second",
                                     "endpoint": "https://second.example"})
    resp = client.get("/api/agents")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert len(body["items"]) == 2
    for item in body["items"]:
        assert set(item) == _RESPONSE_FIELDS
        assert "credential_ref" not in item     # 列表同样引用不出
    assert {item["name"] for item in body["items"]} == {"acp-prod", "acp-second"}


# ================================================================== Get


def test_get_existing_200_full_fields_no_credential_ref(db_engine, client) -> None:
    created = client.post("/api/agents", json=_FULL_PAYLOAD).json()
    resp = client.get(f"/api/agents/{created['id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == _RESPONSE_FIELDS
    assert body["id"] == created["id"]
    assert "credential_ref" not in body


def test_get_missing_404(client) -> None:
    resp = client.get("/api/agents/99999")
    assert resp.status_code == 404


# ================================================================== Patch


def test_patch_each_field_persists(db_engine, client) -> None:
    agent_id = client.post("/api/agents", json=_FULL_PAYLOAD).json()["id"]

    resp = client.patch(f"/api/agents/{agent_id}", json={
        "name": "acp-prod-v2",
        "endpoint": "https://new.example.com/acp",
        "status": "inactive",
        "config": {"timeout_s": 60},
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "acp-prod-v2"
    assert body["endpoint"] == "https://new.example.com/acp"
    assert body["status"] == "inactive"
    # M7-AUI-10.2：config 更新 = merge（提供的键覆写、未提供的保留——
    # 镜像 Channel 11.8 合同 #3；原 protocol 键保留，timeout_s 被覆写）
    assert body["config"] == {"protocol": "tencent-acp-sse", "timeout_s": 60}
    assert body["type"] == "acp"                # 未提及 → 不变

    # 跨请求重新读取：改动真实落库
    refetched = client.get(f"/api/agents/{agent_id}").json()
    assert refetched == body


def test_patch_credential_ref_omitted_and_null_leave_unchanged(db_engine, client) -> None:
    """缺省 / 显式 null 都 = 不修改（本 MVP 无清空语义，11.2 §2.2）。"""
    agent_id = client.post("/api/agents", json=_FULL_PAYLOAD).json()["id"]

    client.patch(f"/api/agents/{agent_id}", json={"name": "renamed"})       # 缺省
    assert _db_get(db_engine, agent_id).credential_ref == "vault://agent/acp-prod"

    client.patch(f"/api/agents/{agent_id}", json={"credential_ref": None})  # 显式 null
    assert _db_get(db_engine, agent_id).credential_ref == "vault://agent/acp-prod"


def test_patch_credential_ref_blank_rejected_overwrite_works(db_engine, client) -> None:
    agent_id = client.post("/api/agents", json=_FULL_PAYLOAD).json()["id"]

    resp = client.patch(f"/api/agents/{agent_id}", json={"credential_ref": ""})
    assert resp.status_code == 422

    resp = client.patch(f"/api/agents/{agent_id}",
                        json={"credential_ref": "vault://agent/new-lease"})
    assert resp.status_code == 200
    assert _db_get(db_engine, agent_id).credential_ref == "vault://agent/new-lease"  # 覆写


def test_patch_bad_values_422(client) -> None:
    agent_id = client.post("/api/agents", json=_FULL_PAYLOAD).json()["id"]
    for bad in (
        {"type": "bogus"},
        {"status": "error"},
        {"status": "disabled"},
        {"endpoint": "not-a-url"},
        {"name": ""},
        {"config": "not-a-dict"},
    ):
        resp = client.patch(f"/api/agents/{agent_id}", json=bad)
        assert resp.status_code == 422, f"{bad} → {resp.status_code}"


def test_patch_duplicate_name_409(client) -> None:
    client.post("/api/agents", json=_FULL_PAYLOAD)
    other_id = client.post(
        "/api/agents",
        json={**_FULL_PAYLOAD, "name": "acp-second",
              "endpoint": "https://second.example"},
    ).json()["id"]
    resp = client.patch(f"/api/agents/{other_id}", json={"name": "acp-prod"})
    assert resp.status_code == 409


def test_patch_missing_404(client) -> None:
    resp = client.patch("/api/agents/99999", json={"name": "ghost"})
    assert resp.status_code == 404


def test_patch_empty_body_is_noop_200(client) -> None:
    agent_id = client.post("/api/agents", json=_FULL_PAYLOAD).json()["id"]
    resp = client.patch(f"/api/agents/{agent_id}", json={})
    assert resp.status_code == 200
    assert resp.json()["name"] == "acp-prod"


# ============================================== status 双轴（11.2 §1.2 落实）


def test_runtime_error_state_is_visible_and_admin_can_recover(db_engine, client) -> None:
    """运行态 error 由 Runtime 写入（此处用直接 DB 写模拟 Runtime 专有写路径）：
    读路径不裁切（GET 可见 error）；管理员 PATCH active 手动清障合法；
    客户端 PATCH error 永远 422。"""
    agent_id = client.post("/api/agents", json=_FULL_PAYLOAD).json()["id"]

    # Runtime dispatch 失败 → 写 error（模拟 Runtime 后端写入）
    with Session(db_engine) as session:
        row = session.get(AgentModel, agent_id)
        row.status = "error"
        session.commit()

    # 读不裁切：error 是要暴露给管理员的运行态信号
    assert client.get(f"/api/agents/{agent_id}").json()["status"] == "error"

    # 客户端试图写 error → 422
    resp = client.patch(f"/api/agents/{agent_id}", json={"status": "error"})
    assert resp.status_code == 422

    # 管理员手动清障：error → active（唯一的「管理覆盖运行态」）
    resp = client.patch(f"/api/agents/{agent_id}", json={"status": "active"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "active"


# ================================================================== 边界守护


def test_api_and_schema_layers_have_no_business_rules_or_orm() -> None:
    """AST 守护：API/Schema 层不 import SQLAlchemy / app.persistence，
    且不做业务判断（无白名单 / 无 URL 解析——业务规则单一裁决点在服务层）。"""
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
    for token in ("ALLOWED_", "urlsplit", "urlparse", "Literal["):
        assert token not in router_src, f"API 层出现业务判断：{token}"


def test_name_uniqueness_is_application_guard_no_db_unique(db_engine, client) -> None:
    """记录事实（用户 11.3 指令）：name 去重是应用级守卫，DB 无 UNIQUE——
    绕过服务层直接写库可插入同名行（TOCTOU 竞态窗口真实存在）。
    migration 不在 11.3 偷偷扩大；除非真实环境证明需要 DB 级唯一。"""
    from sqlalchemy import select

    client.post("/api/agents", json=_FULL_PAYLOAD)
    with Session(db_engine) as session:
        session.add(AgentModel(name="acp-prod", type="acp", status="active"))
        session.commit()  # 无 IntegrityError → 证明无 DB UNIQUE
        dupes = session.execute(
            select(AgentModel).where(AgentModel.name == "acp-prod")
        ).scalars().all()
    assert len(dupes) == 2


def test_domain_audit_timestamps_are_read_only_echo(db_engine, client) -> None:
    """created_at/updated_at 是 DB 落库时间戳回显（与 Session 实体同一先例）。"""
    from app.domain.agent import Agent

    assert {f.name for f in fields(Agent)} == {
        "id", "name", "type", "endpoint", "status", "config", "credential_ref",
        "created_at", "updated_at"}
    body = client.post("/api/agents", json=_FULL_PAYLOAD).json()
    row = _db_get(db_engine, body["id"])
    assert row.created_at is not None and row.updated_at is not None


# ================================================================== 生产装配


def test_production_app_exposes_agents_api() -> None:
    """main.py 组合根已装配 /api/agents（openapi 契约可见；不触库）。"""
    from app.main import app

    client = TestClient(app)
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    paths = resp.json()["paths"]
    assert "/api/agents" in paths
    assert "post" in paths["/api/agents"] and "get" in paths["/api/agents"]
    assert "/api/agents/{agent_id}" in paths
    assert "get" in paths["/api/agents/{agent_id}"] \
        and "patch" in paths["/api/agents/{agent_id}"]
    # 无 DELETE（软下线 = PATCH status=inactive，11.2 §0）
    assert "delete" not in paths["/api/agents/{agent_id}"]
