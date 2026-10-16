"""M7-AUI-10.8.23-C.5 —— ISS-07 Registration Boundary Fix 回归测试。

机器化 C.4 §7.3 回归计划（全部隔离内存 SQLite + 一次性数据，绝不触碰生产）：

1. Cloud 门控：expose_register=False → POST /api/auth/register = 404
2. 无副作用：404 路径不产生 users / user_credentials 行；攻击者凭据无法登录
3. OSS/缺省兼容：缺省（省略参数）与显式 True 保持修复前行为
4. signup 保留：注册即服务端 provision 新租户；调用者无法用 tenant_id 选租户
5. 负向永久锁：Cloud 模式无法向【已存在】租户创建凭据（C.4 §4 R1 的负向化）
6. 接线断言：build_auth_api 签名缺省 True；main.py 传 expose_register=not settings.AUTH_REQUIRED
"""

from __future__ import annotations

import functools
import inspect
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.auth import build_auth_api, build_tenant_scope_provider
from app.api.management.users import build_users_api
from app.application.browser_auth import BrowserAuthService
from app.application.tenant_provisioning import TenantProvisioningService
from app.application.users import UserApplicationService
from app.core.tenant import TenantScope
from app.persistence.database import Base
from app.persistence.models.credential import UserCredentialModel
from app.persistence.models.tenant import TenantModel
from app.persistence.models.user import UserModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

BACKEND = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- helpers


def _make_engine():
    """隔离内存库（StaticPool 单连接共享）；create_all 自动种子 Tenant #1。"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


def _seed_customer_tenant(engine, tenant_id: int = 2) -> None:
    """种子一个『既有客户租户』+ 其既有用户（一次性数据，模拟生产 8 租户场景）。"""
    with sessionmaker(bind=engine)() as s:
        if s.get(TenantModel, tenant_id) is None:
            s.add(TenantModel(id=tenant_id, name=f"customer-{tenant_id}", status="active"))
        s.add(UserModel(name="real-operator", tenant_id=tenant_id))
        s.commit()


def _build_app(engine, *, expose_register=None, with_provisioner=False, auth_required=True):
    """按测试需要装配最小 app（与既有 C.1 测试同一生产代码路径）。

    expose_register=None → 不传参（走缺省 True，验证缺省兼容）。
    """
    sf = sessionmaker(bind=engine, expire_on_commit=False)

    def factory_fn(tenant_id: int | None = None) -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory=sf, tenant_id=tenant_id)

    service = BrowserAuthService(
        uow_factory=factory_fn,
        provisioner=TenantProvisioningService(uow_factory=factory_fn)
        if with_provisioner
        else None,
    )
    app = FastAPI()
    kwargs = {"cookie_secure": False}
    if expose_register is not None:
        kwargs["expose_register"] = expose_register
    app.include_router(build_auth_api(service, **kwargs))
    provider = build_tenant_scope_provider(service, auth_required=auth_required)

    def _users_service(
        scope: TenantScope = Depends(provider),
    ) -> UserApplicationService:
        return UserApplicationService(uow_factory=functools.partial(
            SqlAlchemyUnitOfWork, session_factory=sf, tenant_id=scope.tenant_id))

    app.include_router(build_users_api(service_provider=_users_service))
    return TestClient(app), sf


def _counts(sf):
    """(users 行数, user_credentials 行数)——副作用断言用。"""
    with sf() as s:
        return (
            s.scalar(select(func.count()).select_from(UserModel)),
            s.scalar(select(func.count()).select_from(UserCredentialModel)),
        )


# ---------------------------------------------------------------- 1. Cloud 门控


def test_cloud_route_disabled_returns_404():
    engine = _make_engine()
    _seed_customer_tenant(engine, tenant_id=2)
    client, _sf = _build_app(engine, expose_register=False)
    r = client.post(
        "/api/auth/register",
        json={"login": "attacker@evil.test", "password": "pw123456", "tenant_id": 2},
    )
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------- 2. 无副作用


def test_cloud_route_disabled_no_side_effects():
    engine = _make_engine()
    _seed_customer_tenant(engine, tenant_id=2)
    client, sf = _build_app(engine, expose_register=False)
    before = _counts(sf)
    r = client.post(
        "/api/auth/register",
        json={"login": "attacker@evil.test", "password": "pw123456", "tenant_id": 2},
    )
    assert r.status_code == 404
    assert _counts(sf) == before, "被禁用路由不得产生 users/user_credentials 行"
    # 攻击者凭据未创建 → 登录必败
    r = client.post(
        "/api/auth/login", json={"login": "attacker@evil.test", "password": "pw123456"}
    )
    assert r.status_code == 401
    # 无会话 → me 401
    r = client.get("/api/auth/me")
    assert r.status_code == 401


# ---------------------------------------------------------------- 3. OSS/缺省兼容


def test_default_and_explicit_true_preserve_register():
    # 缺省（省略 expose_register）→ 行为与修复前一致
    engine = _make_engine()
    client, _sf = _build_app(engine)
    r = client.post(
        "/api/auth/register",
        json={"login": "oss-user@example.test", "password": "pw123456", "tenant_id": 1},
    )
    assert r.status_code == 201, r.text
    assert r.json() == {"user_id": r.json()["user_id"], "tenant_id": 1}
    # 显式 True → 同样可用
    engine2 = _make_engine()
    client2, _sf2 = _build_app(engine2, expose_register=True)
    r2 = client2.post(
        "/api/auth/register",
        json={"login": "oss-user2@example.test", "password": "pw123456", "tenant_id": 1},
    )
    assert r2.status_code == 201, r2.text


# ---------------------------------------------------------------- 4. signup 保留


def test_signup_provisions_new_tenant_and_rejects_tenant_selection():
    engine = _make_engine()
    _seed_customer_tenant(engine, tenant_id=2)
    client, _sf = _build_app(engine, with_provisioner=True, auth_required=True)
    # 注册即建【新】租户（不是调用者可指定的 1 / 2）
    r = client.post(
        "/api/auth/signup", json={"login": "fresh@example.test", "password": "pw123456"}
    )
    assert r.status_code == 201, r.text
    new_tenant = r.json()["tenant_id"]
    assert new_tenant not in (1, 2)
    # 会话解析为该新租户
    r = client.get("/api/auth/me")
    assert r.status_code == 200
    assert r.json()["tenant_id"] == new_tenant
    # 调用者无法用 tenant_id 选租户（extra="forbid" → 422）
    r = client.post(
        "/api/auth/signup",
        json={"login": "u2@example.test", "password": "pw123456", "tenant_id": 2},
    )
    assert r.status_code == 422
    # signup 在 Cloud 门控（expose_register=False）下仍然可用
    client2, _sf2 = _build_app(engine, expose_register=False, with_provisioner=True)
    r = client2.post(
        "/api/auth/signup", json={"login": "cloud@example.test", "password": "pw123456"}
    )
    assert r.status_code == 201, r.text


# ---------------------------------------------------------------- 5. 负向永久锁


def test_negative_lock_cloud_cannot_join_existing_tenant():
    """C.4 §4 R1 的永久负向化：Cloud 模式（expose_register=False）下，向既有
    租户 2 注册凭据被 404 拒绝，且全程零 users/user_credentials/sessions 副作用。
    仅隔离内存库——绝不针对生产。"""
    engine = _make_engine()
    _seed_customer_tenant(engine, tenant_id=2)
    client, sf = _build_app(engine, expose_register=False, with_provisioner=True)
    before = _counts(sf)
    r = client.post(
        "/api/auth/register",
        json={"login": "attacker@evil.test", "password": "pw123456", "tenant_id": 2},
    )
    assert r.status_code == 404
    assert _counts(sf) == before
    r = client.post(
        "/api/auth/login", json={"login": "attacker@evil.test", "password": "pw123456"}
    )
    assert r.status_code == 401  # 无法获得租户 2 的会话
    # 既有租户成员名单不受污染
    with sf() as s:
        names = [n for (n,) in s.execute(select(UserModel.name)).all()]
    assert names == ["real-operator"]


# ---------------------------------------------------------------- 6. 接线断言


def test_composition_root_and_signature_wiring():
    # build_auth_api 签名：expose_register 存在且缺省 True（OSS/测试兼容）
    sig = inspect.signature(build_auth_api)
    assert "expose_register" in sig.parameters
    assert sig.parameters["expose_register"].default is True
    # auth.py：路由注册被 expose_register 门控
    auth_src = (BACKEND / "app" / "api" / "auth.py").read_text(encoding="utf-8")
    assert "if expose_register:" in auth_src
    # 组合根：Cloud（AUTH_REQUIRED=True）→ expose_register=False
    main_src = (BACKEND / "app" / "main.py").read_text(encoding="utf-8")
    assert "expose_register=not settings.AUTH_REQUIRED" in main_src
