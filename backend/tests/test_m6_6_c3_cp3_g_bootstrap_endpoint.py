"""M6.6-C3-CP3-G — Bootstrap Arm 端点：认证 / 授权 / supersede / 审计。

被守护的不变量（ADR-12 §15~§16 / CP3-F §15~§16）：

    G5   unauthenticated ⇒ **401**（**不回退** OSS 缺省租户身份，O-4）
         authenticated Tenant Admin ⇒ 201 + supersede
    O-4  initiator_user_id = 已认证操作者，**NOT NULL**，无 anonymous/guest/system/sentinel
    §16  请求体不接受 tenant_id / channel_id / initiator_user_id
         （scope 由服务端从「已认证会话 + Connection」解析）
    §16  跨租户 Connection ⇒ 404（不是越权成功）
"""

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.api.auth import build_authenticated_user_provider
from app.api.management.bootstrap_arms import build_bootstrap_arms_api
from app.application.bootstrap_arms import BootstrapArmApplicationService
from app.application.browser_auth import (
    BrowserAuthService,
    LoginBrowserUser,
    RegisterBrowserUser,
)
from app.core.browser_auth import AuthenticatedUser
from app.core.tenant import TENANT_LOCAL
from app.domain.agent import Agent
from app.domain.channel import Channel
from app.domain.connection import Connection
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

LOGIN = "bootstrap-admin"
PASSWORD = "bootstrap-admin-pw"


def _uow(engine, tenant_id=None):
    return SqlAlchemyUnitOfWork(
        session_factory=sessionmaker(bind=engine, expire_on_commit=False),
        tenant_id=tenant_id,
    )


def _factory(engine, tenant_id=None):
    return lambda: _uow(engine, tenant_id)


def _auth_service(engine) -> BrowserAuthService:
    """BrowserAuthService 的 uow_factory 以 tenant_id 为入参（auth 边界）。"""
    return BrowserAuthService(uow_factory=lambda tid: _uow(engine, tid))


def _registered_admin(engine) -> AuthenticatedUser:
    service = _auth_service(engine)
    try:
        return service.register(RegisterBrowserUser(
            login=LOGIN, password=PASSWORD, tenant_id=TENANT_LOCAL))
    except Exception:
        # 已注册（同一 engine 复用）：直接登录取已认证事实
        return service.login(LoginBrowserUser(
            login=LOGIN, password=PASSWORD)).user


def _seed_connection(engine, tenant_id: int) -> int:
    """在该租户内建 channel + agent + connection，返回 connection_id。"""
    with _uow(engine, tenant_id) as uow:
        channel = uow.channels.create(
            Channel(name=f"ep-ch-{tenant_id}", type="local"))
        agent = uow.agents.create(Agent(name=f"ep-ag-{tenant_id}", type="local"))
        assert channel.id and agent.id
        connection = uow.connections.create(Connection(
            channel_id=channel.id, agent_id=agent.id,
            name=f"ep-path-{tenant_id}"))
        assert connection.id
        uow.commit()
        return connection.id


def _new_tenant(engine) -> int:
    with _uow(engine, None) as uow:
        tenant_id = uow.tenants.create(name="tenant-two")
        uow.commit()
    return tenant_id


def _client(engine, initiator_user_id: int, tenant_id: int = TENANT_LOCAL):
    service = BootstrapArmApplicationService(
        uow_factory=_factory(engine, tenant_id),
        initiator_user_id=initiator_user_id,
    )
    app = FastAPI()
    app.include_router(build_bootstrap_arms_api(service=service))
    return TestClient(app)


# ============================================================ A. 认证（O-4 / G5）


def test_unauthenticated_request_is_401_no_tenant_local_fallback(db_engine) -> None:
    """无 Cookie ⇒ **401**，绝不回退到不可归属的缺省租户身份。"""
    auth_service = _auth_service(db_engine)
    provider = build_authenticated_user_provider(auth_service)

    app = FastAPI()

    @app.post("/probe")
    def probe(user: AuthenticatedUser = Depends(provider)) -> dict:
        return {"user_id": user.user_id, "tenant_id": user.tenant_id}

    client = TestClient(app)
    response = client.post("/probe")

    assert response.status_code == 401


def test_invalid_cookie_is_401(db_engine) -> None:
    auth_service = _auth_service(db_engine)
    provider = build_authenticated_user_provider(auth_service)

    app = FastAPI()

    @app.post("/probe")
    def probe(user: AuthenticatedUser = Depends(provider)) -> dict:
        return {"user_id": user.user_id}

    client = TestClient(app)
    response = client.post("/probe", cookies={"acp_session": "bogus"})

    assert response.status_code == 401


def test_valid_cookie_yields_authenticated_operator(db_engine) -> None:
    """有效会话 ⇒ 真实 `AuthenticatedUser`（user_id + tenant_id）。"""
    auth_service = _auth_service(db_engine)
    admin = _registered_admin(db_engine)
    grant = auth_service.login(LoginBrowserUser(login=LOGIN, password=PASSWORD))
    provider = build_authenticated_user_provider(auth_service)

    app = FastAPI()

    @app.post("/probe")
    def probe(user: AuthenticatedUser = Depends(provider)) -> dict:
        return {"user_id": user.user_id, "tenant_id": user.tenant_id}

    client = TestClient(app)
    response = client.post("/probe", cookies={"acp_session": grant.session_id})

    assert response.status_code == 200
    assert response.json()["user_id"] == admin.user_id
    assert response.json()["tenant_id"] == admin.tenant_id


# ============================================================ B. 端点语义（§16）


def test_arm_returns_201_with_expiry(db_engine) -> None:
    admin = _registered_admin(db_engine)
    connection_id = _seed_connection(db_engine, TENANT_LOCAL)
    client = _client(db_engine, admin.user_id)

    response = client.post(f"/api/connections/{connection_id}/bootstrap-arm")

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "armed"
    assert body["connection_id"] == connection_id
    assert "expires_at" in body and body["expires_at"]      # 回显 DB 生成值
    # 不暴露内部授权机制
    assert "identity" not in body and "grant" not in body


def test_request_body_cannot_declare_scope_or_initiator(db_engine) -> None:
    """请求体携带 tenant_id / channel_id / initiator_user_id 一律被忽略。"""
    admin = _registered_admin(db_engine)
    connection_id = _seed_connection(db_engine, TENANT_LOCAL)
    client = _client(db_engine, admin.user_id)

    response = client.post(
        f"/api/connections/{connection_id}/bootstrap-arm",
        json={"tenant_id": 999, "channel_id": 999, "initiator_user_id": 999},
    )

    assert response.status_code == 201

    with _uow(db_engine, TENANT_LOCAL) as uow:
        arm = uow.bootstrap_arms.get_by_id(response.json()["arm_id"])
    assert arm is not None
    assert arm.initiator_user_id == admin.user_id       # 不是 999
    assert arm.connection_id == connection_id


def test_repeated_verify_is_201_supersede(db_engine) -> None:
    """重复 Verify = supersede：201 + 新 Arm；同 scope 至多一条 armed。"""
    admin = _registered_admin(db_engine)
    connection_id = _seed_connection(db_engine, TENANT_LOCAL)
    client = _client(db_engine, admin.user_id)

    first = client.post(f"/api/connections/{connection_id}/bootstrap-arm")
    second = client.post(f"/api/connections/{connection_id}/bootstrap-arm")

    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["arm_id"] != second.json()["arm_id"]

    with _uow(db_engine, TENANT_LOCAL) as uow:
        old = uow.bootstrap_arms.get_by_id(first.json()["arm_id"])
    assert old is not None and old.status == "revoked"


def test_cross_tenant_connection_is_404(db_engine) -> None:
    """跨租户 Connection ⇒ 404（作用域内不存在），不是越权成功。"""
    admin = _registered_admin(db_engine)
    other_tenant = _new_tenant(db_engine)
    other_connection = _seed_connection(db_engine, other_tenant)
    client = _client(db_engine, admin.user_id)          # 绑定 TENANT_LOCAL

    response = client.post(f"/api/connections/{other_connection}/bootstrap-arm")

    assert response.status_code == 404


def test_unknown_connection_is_404(db_engine) -> None:
    admin = _registered_admin(db_engine)
    client = _client(db_engine, admin.user_id)
    assert client.post("/api/connections/999999/bootstrap-arm").status_code == 404


def test_initiator_is_persisted_and_not_null(db_engine) -> None:
    """审计：initiator 必须可回答「谁允许的」。"""
    admin = _registered_admin(db_engine)
    connection_id = _seed_connection(db_engine, TENANT_LOCAL)
    client = _client(db_engine, admin.user_id)

    arm_id = client.post(
        f"/api/connections/{connection_id}/bootstrap-arm").json()["arm_id"]

    with _uow(db_engine, TENANT_LOCAL) as uow:
        arm = uow.bootstrap_arms.get_by_id(arm_id)

    assert arm is not None
    assert arm.initiator_user_id == admin.user_id
    assert arm.initiator_user_id is not None


def test_service_requires_initiator(db_engine) -> None:
    """没有已认证主体 ⇒ 服务**根本无法构造**（O-4 的类型级约束）。"""
    with pytest.raises(TypeError):
        BootstrapArmApplicationService(uow_factory=_factory(db_engine))  # type: ignore[call-arg]
