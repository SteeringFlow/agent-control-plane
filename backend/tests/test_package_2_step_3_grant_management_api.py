"""Package 2 / Step 3 — Grant Management API（B4.2/B4.3/B4.4/B4.5）。

范围：把 Step 1 的授权状态存储正式暴露为 **独立资源** 的管理 API
（URL 形态 = B4.3：`/api/connection-conversations`，不嵌在 Connection 下——
ADR-2 红线：Connection 域不得长成授权对象）+ B4.5 的 known-conversations 投影。

本文件分五段：

  A. Create（201 / 409 / 404 / 422 / 跨租户拒绝）
  B. List（显式授权 only / tenant isolated）
  C. Delete（204 / 404）
  D. ★ Discovery 投影（Session 可见 / 无 Session 不可见 / **绝不产生授权**）
  E. 边界守卫（无 Update / 独立资源 / 投影零写入）

**核心红线（ADR-6）**：

    known conversation  ≠  authorized conversation

Discovery 只让会话**被观察**；观察不产生授权状态。会话获得访问权只有一条路：
显式 Create grant。
"""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.application.access_authorization import AccessAuthorizationExecutor
from app.application.connection_conversations import (
    ConnectionConversationApplicationService,
)
from app.api.management.connection_conversations import (
    build_connection_conversations_api,
)
from app.domain.agent import Agent
from app.domain.channel import Channel
from app.domain.connection import Connection
from app.domain.identity import Identity
from app.domain.session import Session as SessionEntity
from app.domain.user import User
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

BACKEND_DIR = Path(__file__).resolve().parents[1]
APP_DIR = BACKEND_DIR / "app"
SESSION_ADAPTER_PY = APP_DIR / "persistence" / "repositories" / "session.py"
GRANT_SERVICE_PY = APP_DIR / "application" / "connection_conversations.py"


# ------------------------------------------------------------------ 测试底座


def _factory(db_engine) -> sessionmaker:
    return sessionmaker(bind=db_engine, expire_on_commit=False)


def _uow(db_engine, tenant_id=None):
    return lambda: SqlAlchemyUnitOfWork(
        session_factory=_factory(db_engine), tenant_id=tenant_id)


def _seed(db_engine, *, sessions: tuple[str, ...] = (),
          second_channel: bool = False) -> dict:
    """user / channel / agent / identity / active connection（+ 可选 Session 行）。"""
    with _uow(db_engine)() as uow:
        user = uow.users.create(User(name="alice"))
        channel = uow.channels.create(Channel(name="wecom-1", type="wecom"))
        agent = uow.agents.create(Agent(name="agent-a", type="acp"))
        identity = uow.identities.create(Identity(
            user_id=user.id, channel_id=channel.id, external_id="wx_a"))
        conn = uow.connections.create(Connection(
            channel_id=channel.id, agent_id=agent.id, name="path-a",
            status="active"))
        channel2 = conn2 = None
        if second_channel:
            channel2 = uow.channels.create(Channel(name="wecom-2", type="wecom"))
            conn2 = uow.connections.create(Connection(
                channel_id=channel2.id, agent_id=agent.id, name="path-b",
                status="active"))
        for conversation in sessions:
            uow.sessions.create(SessionEntity(
                identity_id=identity.id, channel_id=channel.id,
                connection_id=conn.id,
                external_conversation_id=conversation, status="active"))
        uow.commit()
        return {"channel": channel.id, "agent": agent.id, "connection": conn.id,
                "identity": identity.id,
                "channel2": channel2.id if channel2 else None,
                "connection2": conn2.id if conn2 else None}


def _client(db_engine, tenant_id=None) -> TestClient:
    """Grant Management API 的测试装配（service 直注，不经 tenant_scope_provider）。"""
    service = ConnectionConversationApplicationService(uow_factory=_uow(db_engine, tenant_id))
    app = FastAPI()
    app.include_router(build_connection_conversations_api(service=service))
    return TestClient(app)


def _grant_count(db_engine) -> int:
    with _uow(db_engine)() as uow:
        return sum(
            len(uow.connection_conversations.list_by_connection(connection_id))
            for connection_id in _all_connection_ids(db_engine))


def _all_connection_ids(db_engine) -> list[int]:
    from sqlalchemy import select
    from sqlalchemy.orm import Session as SASession
    from app.persistence.models.connection import ConnectionModel
    with SASession(db_engine) as s:
        return list(s.execute(select(ConnectionModel.id)).scalars().all())


# ================================================================== A. Create


def test_a1_valid_create_returns_201(db_engine) -> None:
    g = _seed(db_engine)
    resp = _client(db_engine).post("/api/connection-conversations", json={
        "channel_id": g["channel"],
        "external_conversation_id": "conv-1",
        "connection_id": g["connection"],
    })
    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] > 0
    assert body["channel_id"] == g["channel"]
    assert body["external_conversation_id"] == "conv-1"
    assert body["connection_id"] == g["connection"]


def test_a2_duplicate_is_409(db_engine) -> None:
    g = _seed(db_engine)
    client = _client(db_engine)
    payload = {"channel_id": g["channel"], "external_conversation_id": "conv-1",
               "connection_id": g["connection"]}
    assert client.post("/api/connection-conversations", json=payload).status_code == 201
    assert client.post("/api/connection-conversations", json=payload).status_code == 409


def test_a3_missing_channel_or_connection_is_404(db_engine) -> None:
    g = _seed(db_engine)
    client = _client(db_engine)
    resp = client.post("/api/connection-conversations", json={
        "channel_id": 999999, "external_conversation_id": "conv-1",
        "connection_id": g["connection"]})
    assert resp.status_code == 404
    resp = client.post("/api/connection-conversations", json={
        "channel_id": g["channel"], "external_conversation_id": "conv-1",
        "connection_id": 999999})
    assert resp.status_code == 404


def test_a4_invalid_input_is_422(db_engine) -> None:
    g = _seed(db_engine)
    client = _client(db_engine)
    base = {"channel_id": g["channel"], "external_conversation_id": "conv-1",
            "connection_id": g["connection"]}
    # 形状非法（应用层 422）
    for mutate in ({"channel_id": 0}, {"channel_id": -1},
                   {"connection_id": 0}, {"external_conversation_id": ""},
                   {"external_conversation_id": "   "}):
        resp = client.post("/api/connection-conversations",
                           json={**base, **mutate})
        assert resp.status_code == 422, mutate
    # 合同面非法（extra=forbid）
    resp = client.post("/api/connection-conversations", json={**base, "effect": "allow"})
    assert resp.status_code == 422


def test_a5_connection_of_another_channel_is_422(db_engine) -> None:
    """授权三元组必须自洽：connection 存在但不属于该 channel → 422（不是 404）。"""
    g = _seed(db_engine, second_channel=True)
    resp = _client(db_engine).post("/api/connection-conversations", json={
        "channel_id": g["channel"], "external_conversation_id": "conv-1",
        "connection_id": g["connection2"]})
    assert resp.status_code == 422


def test_a6_cross_tenant_is_rejected(db_engine) -> None:
    """跨租户：tenant-2 作用域内 channel/connection **等价于不存在** → 404。

    （DB 层还有第二道防线：复合 FK 拒绝 (tenant, channel, connection) 不一致的行，
    Package 2 / Step 1 已覆盖。）
    """
    g = _seed(db_engine)
    resp = _client(db_engine, tenant_id=2).post("/api/connection-conversations", json={
        "channel_id": g["channel"], "external_conversation_id": "conv-1",
        "connection_id": g["connection"]})
    assert resp.status_code == 404
    assert _grant_count(db_engine) == 0


# ================================================================== B. List


def test_b1_list_returns_explicit_grants_only(db_engine) -> None:
    g = _seed(db_engine, second_channel=True)
    client = _client(db_engine)
    for conversation in ("conv-1", "conv-2"):
        assert client.post("/api/connection-conversations", json={
            "channel_id": g["channel"], "external_conversation_id": conversation,
            "connection_id": g["connection"]}).status_code == 201
    # 另一条 connection 的授权不混入
    assert client.post("/api/connection-conversations", json={
        "channel_id": g["channel2"], "external_conversation_id": "conv-3",
        "connection_id": g["connection2"]}).status_code == 201

    resp = client.get("/api/connection-conversations",
                      params={"connection_id": g["connection"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert {i["external_conversation_id"] for i in body["items"]} == {"conv-1", "conv-2"}


def test_b2_list_is_tenant_isolated(db_engine) -> None:
    g = _seed(db_engine)
    assert _client(db_engine).post("/api/connection-conversations", json={
        "channel_id": g["channel"], "external_conversation_id": "conv-1",
        "connection_id": g["connection"]}).status_code == 201

    # tenant-2 作用域内该 connection 不可见 → 404（而不是泄露 tenant-1 的授权）
    resp = _client(db_engine, tenant_id=2).get(
        "/api/connection-conversations", params={"connection_id": g["connection"]})
    assert resp.status_code == 404


def test_b3_list_rejects_missing_or_invalid_connection(db_engine) -> None:
    client = _client(db_engine)
    assert client.get("/api/connection-conversations",
                      params={"connection_id": 999999}).status_code == 404
    assert client.get("/api/connection-conversations",
                      params={"connection_id": 0}).status_code == 422


# ================================================================== C. Delete


def test_c1_existing_delete_then_404(db_engine) -> None:
    g = _seed(db_engine)
    client = _client(db_engine)
    grant_id = client.post("/api/connection-conversations", json={
        "channel_id": g["channel"], "external_conversation_id": "conv-1",
        "connection_id": g["connection"]}).json()["id"]

    resp = client.delete(f"/api/connection-conversations/{grant_id}")
    assert resp.status_code == 204
    # 撤销生效：List 回空
    body = client.get("/api/connection-conversations",
                      params={"connection_id": g["connection"]}).json()
    assert body["total"] == 0
    # 再删 → 404
    assert client.delete(f"/api/connection-conversations/{grant_id}").status_code == 404


def test_c2_missing_or_invalid_delete(db_engine) -> None:
    client = _client(db_engine)
    assert client.delete("/api/connection-conversations/999999").status_code == 404
    assert client.delete("/api/connection-conversations/0").status_code == 422


def test_c3_cross_tenant_delete_is_404(db_engine) -> None:
    g = _seed(db_engine)
    client = _client(db_engine)
    grant_id = client.post("/api/connection-conversations", json={
        "channel_id": g["channel"], "external_conversation_id": "conv-1",
        "connection_id": g["connection"]}).json()["id"]
    # tenant-2 看不见 tenant-1 的授权行 → 404（删除被租户作用域阻断）
    assert _client(db_engine, tenant_id=2).delete(
        f"/api/connection-conversations/{grant_id}").status_code == 404


# ================================================================== D. ★ Discovery


def test_d1_existing_session_is_visible(db_engine) -> None:
    g = _seed(db_engine, sessions=("conv-1", "conv-2"))
    resp = _client(db_engine).get(
        f"/api/channels/{g['channel']}/known-conversations")
    assert resp.status_code == 200
    body = resp.json()
    assert body["channel_id"] == g["channel"]
    assert body["conversations"] == ["conv-1", "conv-2"]   # 稳定排序、去重


def test_d2_no_session_is_invisible(db_engine) -> None:
    g = _seed(db_engine)
    resp = _client(db_engine).get(
        f"/api/channels/{g['channel']}/known-conversations")
    assert resp.status_code == 200
    assert resp.json()["conversations"] == []


def test_d3_discovery_never_creates_grant(db_engine) -> None:
    """★ ADR-6：观察不产生授权状态——GET 前后授权计数不变。"""
    g = _seed(db_engine, sessions=("conv-1",))
    client = _client(db_engine)
    assert _grant_count(db_engine) == 0

    for _ in range(3):
        resp = client.get(f"/api/channels/{g['channel']}/known-conversations")
        assert resp.status_code == 200
        assert resp.json()["conversations"] == ["conv-1"]

    assert _grant_count(db_engine) == 0, "Discovery 不得创建 Grant"


def test_d4_known_conversation_is_not_authorized(db_engine) -> None:
    """★★ Step 3 核心红线：known conversation ≠ authorized conversation。

    会话已出现（Discovery 可见）但**没有显式 grant** → Runtime 授权**仍然 DENY**。
    「出现在 Discovery 中」绝不等于「已授权」（ADR-6：允许必须显式成立）。
    """
    g = _seed(db_engine, sessions=("conv-known",))
    client = _client(db_engine)

    # Discovery 说「这个会话出现过」
    body = client.get(f"/api/channels/{g['channel']}/known-conversations").json()
    assert body["conversations"] == ["conv-known"]

    # 但授权面仍然 default-deny
    with pytest.raises(Exception) as excinfo:
        AccessAuthorizationExecutor(_uow(db_engine)).execute(
            g["channel"], "conv-known")
    from app.application.errors import AccessAuthorizationDeniedError
    assert isinstance(excinfo.value, AccessAuthorizationDeniedError)

    # 显式 Create grant 之后，同一会话才 ALLOW
    assert client.post("/api/connection-conversations", json={
        "channel_id": g["channel"], "external_conversation_id": "conv-known",
        "connection_id": g["connection"]}).status_code == 201
    auth = AccessAuthorizationExecutor(_uow(db_engine)).execute(
        g["channel"], "conv-known")
    assert auth.granted_connection_ids == frozenset({g["connection"]})


def test_d5_unknown_channel_is_404(db_engine) -> None:
    assert _client(db_engine).get(
        "/api/channels/999999/known-conversations").status_code == 404


# ================================================================== E. 边界守卫


def test_e1_no_update_endpoint() -> None:
    """行不可变（B4.2）：没有任何 PUT / PATCH 路由——改授权 = 删 + 建。"""
    service = ConnectionConversationApplicationService(uow_factory=lambda: None)
    app = FastAPI()
    app.include_router(build_connection_conversations_api(service=service))
    mutating = [r.path for r in app.routes
                if {"PUT", "PATCH"} & getattr(r, "methods", set())]
    assert mutating == [], f"出现变更路由：{mutating}"


def test_e2_independent_resource_not_nested_under_connections() -> None:
    """独立资源边界（B4.3）：授权路由不出现在 /connections/{id}/… 之下。"""
    service = ConnectionConversationApplicationService(uow_factory=lambda: None)
    app = FastAPI()
    app.include_router(build_connection_conversations_api(service=service))
    nested = [r.path for r in app.routes if "/connections/" in r.path]
    assert nested == [], f"授权被嵌进 Connection 子资源：{nested}"


def test_e3_discovery_is_read_only_at_source_level() -> None:
    """源码级：Discovery 投影（Port 方法 + service 方法）零写入调用。"""
    import ast

    def _calls(path: Path, func_name: str) -> set[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == func_name:
                return {n.func.attr for n in ast.walk(node)
                        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        raise AssertionError(f"{path} 未找到 {func_name}")

    assert _calls(SESSION_ADAPTER_PY, "list_known_conversation_ids").isdisjoint(
        {"add", "delete", "flush", "commit", "create"})
    assert _calls(GRANT_SERVICE_PY, "list_known_conversations").isdisjoint(
        {"create", "delete", "commit"})
