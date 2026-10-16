"""Step 11.11 —— Connection Management API 实现验收（第三条 Management Slice）。

按 11.10 审计五焦点裁决 + 用户 11.11 冻结合同，经真实 HTTP 探针（TestClient）锁定：

    HTTP → Schema → Application Service → Domain → Repository → DB

冻结合同（刻意不复制 Channel 11.8 之处已标注）：
1. Response contract：字段面 == {id,name,channel_id,agent_id,status,config}；
   **不携带** channel_name / agent_name（列表只返回绑定 ID，名称由 UI 经
   /api/channels、/api/agents 解耦解析）；
2. credential_ref 写-only：Create/Update 可入（引用入），一切响应「不在场」
   排除（引用不出，复制 Agent 护栏——不是 Channel 的 config 脱敏）；
3. config = Access Path 参数 + **响应面敏感键脱敏**（16.1.2 取代 11.11 原
   「不脱敏」决定——P1 真实接入证实 config 携带 runtime 凭据如 bearer_token，
   原样回显即明文泄漏）：敏感键 → ********（键名 strip+lower），非敏感键
   所见即所存；PATCH config 仍 = **整段替换**，但哨兵值写入被 422 拒绝；
4. status 白名单 active/inactive（schema 级）——「error」在合同面无法进入
   （11.10 焦点3：纯管理态，无 runtime 双轴）；
5. immutable binding：channel_id / agent_id 不在 Update 模型且 extra="forbid"
   ——改绑定 = 422（换绑 = 旧 inactive + 新建，不是 PATCH）；
6. 创建仍 existence-only（11.10 焦点1）：Channel/Agent 缺失 → 404，不做
   status 门禁（下拉 active-only 是 UI 过滤）。

不做（11.11 边界）：DELETE / Health Check **状态机** / Webhook / Routing / 换绑。
（Cloud-0.1.2.2.2.1 增加了**即时诊断探针** `GET /{id}/health`——它是派生快照，
不是持久健康子系统；本文件对应断言已同步更新。）
"""

import ast
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.api.management.connections import build_connections_api
from app.application.agents import AgentApplicationService, CreateAgent
from app.application.channels import ChannelApplicationService, CreateChannel
from app.application.connections import ConnectionApplicationService
from app.persistence.models.connection import ConnectionModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

BACKEND = Path(__file__).resolve().parents[1]
API_ROUTER = BACKEND / "app" / "api" / "management" / "connections.py"
SCHEMAS = BACKEND / "app" / "schemas" / "connections.py"

_RESPONSE_FIELDS = {"id", "name", "channel_id", "agent_id", "status", "config"}
_CONFIG = {"timeout_s": 10, "protocol_options": {"mode": "stream"}}
_CRED_REF = "cred_conn_001"


def _factory(db_engine) -> sessionmaker:
    return sessionmaker(bind=db_engine, class_=Session, expire_on_commit=False)


def _uow_factory(db_engine):
    return lambda: SqlAlchemyUnitOfWork(session_factory=_factory(db_engine))


def _service(db_engine) -> ConnectionApplicationService:
    return ConnectionApplicationService(uow_factory=_uow_factory(db_engine))


@pytest.fixture()
def client(db_engine):
    """真实 HTTP 探针：test app 装配 build_connections_api(内存库 Service)。"""
    test_app = FastAPI()
    test_app.include_router(build_connections_api(_service(db_engine)))
    return TestClient(test_app)


def _seed_binding(db_engine, channel_name="wecom-main", agent_name="support-agent") -> tuple[int, int]:
    """经真实 Application 服务播种绑定目标，返回 (channel_id, agent_id)。"""
    uow = _uow_factory(db_engine)
    channel = ChannelApplicationService(uow_factory=uow).create_channel(
        CreateChannel(name=channel_name, type="wecom"))
    agent = AgentApplicationService(uow_factory=uow).create_agent(
        CreateAgent(name=agent_name, type="acp"))  # Agent type 受控集合（11.2 合同 #3）
    return channel.id, agent.id


def _payload(channel_id: int, agent_id: int, /, **over) -> dict:
    body = {
        "channel_id": channel_id,
        "agent_id": agent_id,
        "name": "wecom-main → support-agent",
        "config": dict(_CONFIG),
        "credential_ref": _CRED_REF,
    }
    body.update(over)
    return body


def _create(db_engine, client, **over) -> dict:
    channel_id, agent_id = _seed_binding(db_engine)
    resp = client.post("/api/connections", json=_payload(channel_id, agent_id, **over))
    assert resp.status_code == 201, resp.text
    return resp.json()


def _db_get(db_engine, connection_id: int) -> ConnectionModel:
    """绕过全部应用层，直接读 DB 行（验证 credential_ref 落库 / 绑定不变）。"""
    with Session(db_engine) as session:
        row = session.get(ConnectionModel, connection_id)
        session.expunge(row)
        return row


# ================================================================== 合同 1：Response 字段面


def test_create_201_field_face_and_defaults(db_engine, client) -> None:
    body = _create(db_engine, client)
    assert set(body) == _RESPONSE_FIELDS          # 恰好 6 个可见字段
    assert body["name"] == "wecom-main → support-agent"
    assert body["status"] == "active"             # 领域默认
    assert body["id"] > 0
    assert body["config"] == _CONFIG
    assert "credential_ref" not in body           # 引用不出
    assert "channel_name" not in body and "agent_name" not in body  # 绑定 ID only


def test_list_returns_binding_ids_only(db_engine, client) -> None:
    _create(db_engine, client)
    channel_id, agent_id = _seed_binding(db_engine, "feishu-main", "ops-agent")
    client.post("/api/connections", json=_payload(channel_id, agent_id, name="feishu → ops"))

    resp = client.get("/api/connections")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert "credential_ref" not in resp.text
    assert "channel_name" not in resp.text and "agent_name" not in resp.text
    for item in body["items"]:
        assert set(item) == _RESPONSE_FIELDS
    assert {i["name"] for i in body["items"]} == {
        "wecom-main → support-agent", "feishu → ops"}


def test_get_existing_200_and_missing_404(db_engine, client) -> None:
    created = _create(db_engine, client)
    resp = client.get(f"/api/connections/{created['id']}")
    assert resp.status_code == 200
    assert set(resp.json()) == _RESPONSE_FIELDS
    assert client.get("/api/connections/99999").status_code == 404


# ================================================================== 合同 2：credential_ref 写-only


def test_credential_ref_persisted_but_never_echoed(db_engine, client) -> None:
    created = _create(db_engine, client)

    row = _db_get(db_engine, created["id"])       # 引用真实落库
    assert row.credential_ref == _CRED_REF

    for resp in (
        client.get(f"/api/connections/{created['id']}"),
        client.get("/api/connections"),
        client.patch(f"/api/connections/{created['id']}", json={"name": "renamed"}),
    ):
        assert resp.status_code == 200
        assert "credential_ref" not in resp.text, "响应泄漏 credential_ref 字段面"
        assert _CRED_REF not in resp.text, "响应泄漏引用值"


def test_patch_credential_ref_rotates_write_only(db_engine, client) -> None:
    """引用可轮换（非空串=覆写）；缺省/null = 保留当前引用（镜像 Agent 语义）。"""
    created = _create(db_engine, client)
    cid = created["id"]

    assert client.patch(f"/api/connections/{cid}",
                        json={"credential_ref": "cred_rotated"}).status_code == 200
    assert _db_get(db_engine, cid).credential_ref == "cred_rotated"

    client.patch(f"/api/connections/{cid}", json={"name": "x"})          # 缺省
    client.patch(f"/api/connections/{cid}", json={"credential_ref": None})  # 显式 null
    assert _db_get(db_engine, cid).credential_ref == "cred_rotated"


# ================================================================== 合同 3：config = Access Path 参数 + 响应面敏感键脱敏（16.1.2）


def test_config_sensitive_keys_masked_in_response(db_engine, client) -> None:
    """16.1.2（取代 11.11 原「不脱敏」合同）：P1 真实接入证实 config 携带
    runtime 凭据（bearer_token），原样回显即明文泄漏——敏感键一律脱敏为
    ********，非敏感键所见即所存。"""
    config = {"endpoint": "http://agent:9000", "bearer_token": "real-secret-1",
              "runtime_id": "rt-1", "timeout_s": 10}
    created = _create(db_engine, client, config=config)
    resp = client.get(f"/api/connections/{created['id']}")
    body = resp.json()
    assert body["config"]["bearer_token"] == "********"
    assert body["config"]["endpoint"] == "http://agent:9000"
    assert body["config"]["runtime_id"] == "rt-1"
    assert body["config"]["timeout_s"] == 10
    assert "real-secret-1" not in resp.text

    # 反向守护：脱敏实现不被静默移除
    schema_src = SCHEMAS.read_text(encoding="utf-8")
    assert "def sanitize_connection_config" in schema_src
    assert "SENSITIVE_CONNECTION_CONFIG_KEYS" in schema_src


def test_patch_config_is_replace_not_merge(db_engine, client) -> None:
    """11.11 与 Channel 11.8 的关键差异：PATCH config = 整段替换，未提供的键不保留。"""
    created = _create(db_engine, client)
    cid = created["id"]

    resp = client.patch(f"/api/connections/{cid}",
                        json={"config": {"timeout_s": 30}})
    assert resp.status_code == 200
    assert resp.json()["config"] == {"timeout_s": 30}   # protocol_options 被替换掉

    row = _db_get(db_engine, cid)
    assert row.config == {"timeout_s": 30}              # 落库同样是替换


def test_patch_config_omitted_and_null_leave_unchanged(db_engine, client) -> None:
    """缺省 / 显式 null 都 = 不修改 config。"""
    created = _create(db_engine, client)
    cid = created["id"]

    client.patch(f"/api/connections/{cid}", json={"name": "renamed"})
    assert _db_get(db_engine, cid).config == _CONFIG

    client.patch(f"/api/connections/{cid}", json={"config": None})
    assert _db_get(db_engine, cid).config == _CONFIG


# ================================================================== 合同 4：status 纯管理态白名单


def test_patch_status_active_inactive_roundtrip(db_engine, client) -> None:
    """软下线 / 重新启用（无 DELETE；无 error 清障动作——error 根本进不来）。"""
    created = _create(db_engine, client)
    cid = created["id"]

    assert client.patch(f"/api/connections/{cid}",
                        json={"status": "inactive"}).status_code == 200
    assert _db_get(db_engine, cid).status == "inactive"

    assert client.patch(f"/api/connections/{cid}",
                        json={"status": "active"}).status_code == 200
    assert _db_get(db_engine, cid).status == "active"


@pytest.mark.parametrize("bad_status", ["error", "failed", "ERROR", "Active", "   ", ""])
def test_patch_status_whitelist_rejects_non_management_state(db_engine, client, bad_status) -> None:
    """11.10 焦点3 冻结：error 等任何非管理态值在 schema 层 422，且 DB 不被污染。"""
    created = _create(db_engine, client)
    cid = created["id"]

    resp = client.patch(f"/api/connections/{cid}", json={"status": bad_status})
    assert resp.status_code == 422, f"status={bad_status!r} → {resp.status_code}"
    assert _db_get(db_engine, cid).status == "active"


def test_list_includes_inactive_connections(db_engine, client) -> None:
    """管理面列举含 inactive（与 runtime find_active_by_channel 互不替代）。"""
    _create(db_engine, client)
    channel_id, agent_id = _seed_binding(db_engine, "feishu-main", "ops-agent")
    second = client.post("/api/connections",
                         json=_payload(channel_id, agent_id, name="feishu → ops")).json()
    client.patch(f"/api/connections/{second['id']}", json={"status": "inactive"})

    body = client.get("/api/connections").json()
    assert body["total"] == 2                          # inactive 仍在管理列表中
    assert {i["status"] for i in body["items"]} == {"active", "inactive"}


# ================================================================== 合同 5：immutable binding


def test_patch_binding_is_rejected_422_not_silently_ignored(db_engine, client) -> None:
    """改绑定在合同面直接 422（extra=forbid），绝不被静默忽略；DB 绑定不变。"""
    channel_id, agent_id = _seed_binding(db_engine)
    resp = client.post("/api/connections", json=_payload(channel_id, agent_id))
    assert resp.status_code == 201, resp.text
    cid = resp.json()["id"]

    for bad in ({"channel_id": channel_id + 999}, {"agent_id": agent_id + 999}):
        resp = client.patch(f"/api/connections/{cid}", json=bad)
        assert resp.status_code == 422, f"{bad} → {resp.status_code}"

    row = _db_get(db_engine, cid)
    assert (row.channel_id, row.agent_id) == (channel_id, agent_id)


def test_create_extra_fields_rejected_422(db_engine, client) -> None:
    """Create 同样 extra=forbid：创建期不接受 status（领域默认 active）等越界字段。"""
    channel_id, agent_id = _seed_binding(db_engine)
    resp = client.post("/api/connections",
                       json=_payload(channel_id, agent_id, status="inactive"))
    assert resp.status_code == 422


# ================================================================== 合同 6：existence-only 创建 + 去重


def test_create_unknown_channel_or_agent_404(client) -> None:
    """Channel/Agent 只校验存在（existence-only，11.10 焦点1：不做 status 门禁）。"""
    resp = client.post("/api/connections", json=_payload(99999, 99999))
    assert resp.status_code == 404                     # Channel 预检在前

    # 也可用「真实存在的一端 + 不存在的另一端」验证两侧预检独立生效：
    resp2 = client.post("/api/connections",
                        json=_payload(99999, 1, name="ghost-binding"))
    assert resp2.status_code == 404


def test_create_duplicate_binding_409_and_other_pairs_allowed(db_engine, client) -> None:
    """UNIQUE(channel_id, agent_id)：同对 409；共享一端的不同对合法。"""
    channel_id, agent_id = _seed_binding(db_engine)
    assert client.post("/api/connections",
                       json=_payload(channel_id, agent_id)).status_code == 201
    assert client.post("/api/connections",
                       json=_payload(channel_id, agent_id)).status_code == 409

    # 同 Channel 接另一个 Agent、同 Agent 接另一个 Channel：均合法
    other_agent = AgentApplicationService(uow_factory=_uow_factory(db_engine)).create_agent(
        CreateAgent(name="second-agent", type="acp"))
    assert client.post("/api/connections",
                       json=_payload(channel_id, other_agent.id, name="pair-2")
                       ).status_code == 201


@pytest.mark.parametrize(
    "bad_over",
    [
        {"name": ""},
        {"name": "   "},
        {"channel_id": 0},
        {"channel_id": -1},
        {"agent_id": 0},
        {"config": ["not", "a", "dict"]},
        {"config": "not-a-dict"},
    ],
    ids=["empty-name", "blank-name", "zero-channel", "neg-channel", "zero-agent",
         "config-list", "config-str"],
)
def test_create_invalid_input_422(db_engine, client, bad_over) -> None:
    channel_id, agent_id = _seed_binding(db_engine)
    resp = client.post("/api/connections", json=_payload(channel_id, agent_id, **bad_over))
    assert resp.status_code == 422, f"{bad_over} → {resp.status_code}"


def test_create_credential_ref_optional_normalized(db_engine, client) -> None:
    """credential_ref = optional opaque reference（0.1.2.2.1）：

    None / "" / 纯空白都不再因 credential_ref 本身失败，且一律归一为 None。
    真实凭据仍由 config 承载（响应脱敏见 §合同 3）。
    """
    for i, ref in enumerate((None, "", "   ")):
        # 每个用例独立绑定：UNIQUE(channel_id, agent_id) 要求不同 pair
        channel_id, agent_id = _seed_binding(
            db_engine, channel_name=f"cred-ch-{i}", agent_name=f"cred-ag-{i}")
        resp = client.post("/api/connections",
                           json=_payload(channel_id, agent_id, name=f"cred-ref-{i}",
                                         credential_ref=ref))
        assert resp.status_code == 201, resp.text
        assert _db_get(db_engine, resp.json()["id"]).credential_ref is None


def test_patch_invalid_input_422_and_missing_404(db_engine, client) -> None:
    created = _create(db_engine, client)
    cid = created["id"]
    for bad in ({"name": ""}, {"config": "nope"}, {"credential_ref": "  "}):
        assert client.patch(f"/api/connections/{cid}", json=bad).status_code == 422

    assert client.patch("/api/connections/99999", json={"name": "ghost"}).status_code == 404


def test_patch_empty_body_is_noop_200(db_engine, client) -> None:
    created = _create(db_engine, client)
    resp = client.patch(f"/api/connections/{created['id']}", json={})
    assert resp.status_code == 200
    assert _db_get(db_engine, created["id"]).config == _CONFIG
    assert _db_get(db_engine, created["id"]).credential_ref == _CRED_REF


# ================================================================== 边界守护


def test_no_delete_endpoint(client) -> None:
    """不做 DELETE（软下线 = PATCH status；DROP 裁决维持）。"""
    assert client.delete("/api/connections/1").status_code in (405, 404)


def test_api_and_schema_layers_have_no_orm_imports() -> None:
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


def test_production_app_exposes_connections_api() -> None:
    """main.py 组合根已装配 /api/connections（openapi 契约可见；不触库）。

    ConnectionResponse 的 openapi 字段面 == 6 个可见字段（无 credential_ref、
    无 channel_name/agent_name）；无 DELETE、无 webhook/routing 混入。
    """
    from app.main import app

    client = TestClient(app)
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    doc = resp.json()
    paths = doc["paths"]
    assert "/api/connections" in paths
    assert "post" in paths["/api/connections"] and "get" in paths["/api/connections"]
    assert "/api/connections/{connection_id}" in paths
    assert "get" in paths["/api/connections/{connection_id}"] \
        and "patch" in paths["/api/connections/{connection_id}"]
    assert "delete" not in paths["/api/connections/{connection_id}"]

    response_schema = doc["components"]["schemas"]["ConnectionResponse"]
    assert set(response_schema["properties"]) == _RESPONSE_FIELDS
    assert "credential_ref" not in response_schema["properties"]
    assert "channel_name" not in response_schema["properties"]
    assert "agent_name" not in response_schema["properties"]

    # 管理面与 runtime 边界：connections 无 webhook / routing 子路径。
    # Cloud-0.1.2.2.2.1：即时诊断探针 GET /{id}/health 已登记（派生快照，非持久子系统）。
    assert not any("/webhook" in p or "/routing" in p
                   for p in paths if p.startswith("/api/connections"))
    assert "/api/connections/{connection_id}/health" in paths
