"""Cloud-0.1.1-C.1 —— Browser Authentication 最小实现验证门。

覆盖 owner 给定的验证清单（逐条机器化）：

1. 注册 / 创建最小 Browser User
2. credential 存储不进入 users
3. 登录成功建立 opaque browser session
4. Cookie 属性正确
5. session → user 正确解析
6. user → tenant 正确产生 TenantScope
7. 请求进入既有 tenant-scoped UoW
8. A tenant 无法访问 B tenant
9. 未认证 → 401
10. 不涉及授权的 403 语义不被混入
11. OSS TENANT_LOCAL fallback 行为不变
12. Runtime / Agent / SDK / Worker / Driver 零改动
13. B 的 RLS/UoW 实现 零重写

参考：docs/step-cloud-0.1.1-C-browser-authentication-gate.md
      docs/step-cloud-0.1.1-C.1（本步实现契约）
"""

import functools
import inspect
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.api.auth import build_auth_api, build_tenant_scope_provider
from app.api.management.users import build_users_api
from app.application.browser_auth import (
    BrowserAuthService,
    LoginBrowserUser,
    RegisterBrowserUser,
)
from app.application.users import UserApplicationService
from app.core.browser_auth import BrowserAuthError
from app.core.tenant import TENANT_LOCAL, TenantScope, default_tenant_scope
from app.persistence.database import Base, _engine_kwargs
from app.persistence.models.browser_session import BrowserSessionModel
from app.persistence.models.credential import UserCredentialModel
from app.persistence.models.tenant import TenantModel
from app.persistence.models.user import UserModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"
SDK = BACKEND / "agent_runtime_sdk"


# ---------------------------------------------------------------- helpers


def _session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


def _uow_factory(engine):
    factory = _session_factory(engine)

    def factory_fn(tenant_id: int | None = None) -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory=factory, tenant_id=tenant_id)

    return factory_fn


def _auth_service(engine) -> BrowserAuthService:
    return BrowserAuthService(uow_factory=_uow_factory(engine))


def _ensure_tenant(engine, tenant_id: int, name: str = "t") -> None:
    with Session(engine) as session:
        if session.get(TenantModel, tenant_id) is None:
            session.add(TenantModel(id=tenant_id, name=name, status="active"))
            session.commit()


def _build_app(engine, *, auth_required: bool) -> FastAPI:
    """装配一个最小测试 app：auth router + users 管理面（tenant-scoped）。"""
    app = FastAPI()
    service = _auth_service(engine)
    app.include_router(build_auth_api(service, cookie_secure=False))
    provider = build_tenant_scope_provider(service, auth_required=auth_required)

    def _users_service(
        scope: TenantScope = Depends(provider),
    ) -> UserApplicationService:
        return UserApplicationService(uow_factory=functools.partial(
            SqlAlchemyUnitOfWork,
            session_factory=_session_factory(engine),
            tenant_id=scope.tenant_id,
        ))

    app.include_router(build_users_api(service_provider=_users_service))
    return app


def _register(client, login, password, tenant_id=1):
    return client.post("/api/auth/register", json={
        "login": login, "password": password, "tenant_id": tenant_id})


def _login(client, login, password):
    return client.post("/api/auth/login", json={"login": login, "password": password})


# =============================================================== 1 / 2 注册 / 凭据不入 users


def test_register_creates_user_and_credential_separately(db_engine) -> None:
    """1+2：注册创建 User + 凭据；凭据**只**落 user_credentials，绝不进 users。"""
    service = _auth_service(db_engine)
    result = service.register(RegisterBrowserUser(
        login="alice", password="s3cret-pw", tenant_id=1))

    with Session(db_engine) as session:
        user = session.execute(
            select(UserModel).where(UserModel.name == "alice")).scalar_one()
        creds = session.execute(select(UserCredentialModel)).scalars().all()

    assert user.id == result.user_id
    assert user.tenant_id == 1
    assert len(creds) == 1
    assert creds[0].login == "alice"
    assert creds[0].user_id == result.user_id
    # 验证器不是明文；users 表无任何凭据列
    assert creds[0].verifier != "s3cret-pw"
    assert "s3cret-pw" not in creds[0].verifier
    users_cols = {c.name for c in UserModel.__table__.columns}
    assert not (users_cols & {"password", "password_hash", "secret",
                              "credential", "token", "verifier"})


def test_register_rejects_duplicate_login(db_engine) -> None:
    service = _auth_service(db_engine)
    service.register(RegisterBrowserUser(login="dup", password="p1", tenant_id=1))
    from app.application.errors import DuplicateBrowserLoginError
    with pytest.raises(DuplicateBrowserLoginError):
        service.register(RegisterBrowserUser(login="dup", password="p2", tenant_id=1))


# =============================================================== 3 / 4 会话 / Cookie


def test_login_creates_opaque_browser_session(db_engine) -> None:
    """3：登录成功建立 opaque 会话；令牌非 JWT（无签名声明链结构）。"""
    service = _auth_service(db_engine)
    service.register(RegisterBrowserUser(login="bob", password="pw", tenant_id=1))

    grant = service.login(LoginBrowserUser(login="bob", password="pw"))
    assert grant.session_id
    # opaque：不是 JWT（JWT 是 header.payload.signature 三段）
    assert grant.session_id.count(".") == 0
    assert not grant.session_id.startswith("eyJ")

    with Session(db_engine) as session:
        rows = session.execute(select(BrowserSessionModel)).scalars().all()
    assert len(rows) == 1
    assert rows[0].session_id == grant.session_id
    assert rows[0].user_id == grant.user.user_id
    assert rows[0].tenant_id == 1


def test_login_failure_raises_auth_error(db_engine) -> None:
    service = _auth_service(db_engine)
    service.register(RegisterBrowserUser(login="carol", password="right", tenant_id=1))
    with pytest.raises(BrowserAuthError):
        service.login(LoginBrowserUser(login="carol", password="wrong"))
    with pytest.raises(BrowserAuthError):
        service.login(LoginBrowserUser(login="nobody", password="right"))


def test_login_sets_cookie_attributes(db_engine) -> None:
    """4：Cookie 属性正确（HttpOnly + SameSite=Strict + Path=/）。"""
    client = TestClient(_build_app(db_engine, auth_required=True))
    assert _register(client, "cookie-user", "pw", 1).status_code == 201
    resp = _login(client, "cookie-user", "pw")
    assert resp.status_code == 200
    set_cookie = resp.headers["set-cookie"].lower()
    assert "acp_session=" in set_cookie
    assert "httponly" in set_cookie
    assert "samesite=strict" in set_cookie
    assert "path=/" in set_cookie


def test_logout_clears_session_and_cookie(db_engine) -> None:
    client = TestClient(_build_app(db_engine, auth_required=True))
    _register(client, "logout-user", "pw", 1)
    _login(client, "logout-user", "pw")
    resp = client.post("/api/auth/logout")
    assert resp.status_code == 204
    with Session(db_engine) as session:
        assert session.execute(select(BrowserSessionModel)).scalars().all() == []


# =============================================================== 5 / 6 session → user → tenant


def test_verify_resolves_session_to_user_and_tenant(db_engine) -> None:
    """5+6：session → user 正确解析 → 产生携带 tenant_id 的 AuthenticatedUser。"""
    service = _auth_service(db_engine)
    registered = service.register(RegisterBrowserUser(
        login="dave", password="pw", tenant_id=1))
    grant = service.login(LoginBrowserUser(login="dave", password="pw"))

    authenticated = service.verify(grant.session_id)
    assert authenticated.user_id == registered.user_id
    assert authenticated.tenant_id == registered.tenant_id == 1

    with pytest.raises(BrowserAuthError):
        service.verify("not-a-real-session")


def test_provider_derives_tenant_scope_from_session(db_engine) -> None:
    """6：tenant 解析依赖把 Cookie 转成 TenantScope（L3 复用 app.core.tenant）。"""
    service = _auth_service(db_engine)
    service.register(RegisterBrowserUser(login="erin", password="pw", tenant_id=1))
    grant = service.login(LoginBrowserUser(login="erin", password="pw"))

    app = FastAPI()
    provider = build_tenant_scope_provider(service, auth_required=True)

    @app.get("/whoami")
    def whoami(scope: TenantScope = Depends(provider)) -> dict:
        return {"tenant_id": scope.tenant_id}

    client = TestClient(app)
    client.cookies.set("acp_session", grant.session_id)
    assert client.get("/whoami").json() == {"tenant_id": 1}


# =============================================================== 7 / 8 进入 tenant-scoped UoW + 隔离


def test_request_enters_existing_tenant_scoped_uow_and_isolates(db_engine) -> None:
    """7+8：请求经 TenantScope 进入 B 的 tenant-scoped UoW；A tenant 看不到 B tenant 数据。"""
    _ensure_tenant(db_engine, 1, "tenant-1")
    _ensure_tenant(db_engine, 2, "tenant-2")
    client = TestClient(_build_app(db_engine, auth_required=True))

    # —— tenant 1：注册 + 登录 + 创建一条 tenant-1 数据 ——
    _register(client, "t1user", "pw", 1)
    assert _login(client, "t1user", "pw").status_code == 200
    assert client.post("/api/users", json={"name": "in-tenant-1"}).status_code == 201
    t1_items = client.get("/api/users").json()["items"]
    assert any(u["name"] == "in-tenant-1" for u in t1_items)

    # —— 切换到 tenant 2 ——
    client.post("/api/auth/logout")
    _register(client, "t2user", "pw", 2)
    assert _login(client, "t2user", "pw").status_code == 200
    t2_items = client.get("/api/users").json()["items"]

    # tenant 2 看不到 tenant 1 的数据（进入的是 tenant-scoped UoW）
    assert not any(u["name"] == "in-tenant-1" for u in t2_items)
    # tenant 2 只看到自己的注册用户
    assert any(u["name"] == "t2user" for u in t2_items)


# =============================================================== 9 / 10 401 vs 403


def test_unauthenticated_returns_401_when_auth_required(db_engine) -> None:
    """9：Cloud（auth_required）无 Cookie → 401。"""
    client = TestClient(_build_app(db_engine, auth_required=True))
    assert client.get("/api/users").status_code == 401
    # 无效 Cookie 同样 401
    client.cookies.set("acp_session", "bogus")
    assert client.get("/api/users").status_code == 401


def test_bad_credentials_return_401_not_403(db_engine) -> None:
    """10：凭证失败 → 401（绝不 403）；403 授权语义不混入 Browser Auth。"""
    client = TestClient(_build_app(db_engine, auth_required=True))
    assert _login(client, "ghost", "pw").status_code == 401
    # 源码级：auth 模块绝不产生 403（status_code=403）
    src = (APP / "api" / "auth.py").read_text(encoding="utf-8")
    assert "status_code=403" not in src


def test_wecom_403_semantics_still_distinct(db_engine) -> None:
    """10：WeCom 入站验签的 403（InboundSignatureError）语义保持独立、未被混入。"""
    src = (APP / "api" / "runtime" / "channels.py").read_text(encoding="utf-8")
    assert "InboundSignatureError" in src and "403" in src


# =============================================================== 11 OSS fallback


def test_oss_local_fallback_uses_tenant_local(db_engine) -> None:
    """11：OSS / 本地单租户（no auth）无 Cookie → TENANT_LOCAL 回退，行为不变。"""
    assert TENANT_LOCAL == 1
    assert default_tenant_scope().tenant_id == TENANT_LOCAL
    client = TestClient(_build_app(db_engine, auth_required=False))
    # 无 Cookie 也能进入（回退 TENANT_LOCAL）
    assert client.get("/api/users").status_code == 200
    assert client.post("/api/users", json={"name": "oss-user"}).status_code == 201
    # 回退路径写入的行归属 TENANT_LOCAL
    with Session(db_engine) as session:
        row = session.execute(
            select(UserModel).where(UserModel.name == "oss-user")).scalar_one()
    assert row.tenant_id == TENANT_LOCAL


# =============================================================== 12 Runtime/Agent/SDK 零改动


def test_runtime_worker_driver_not_coupled_to_browser_auth() -> None:
    """12：Runtime / Worker / Driver / 六段执行不引用 Browser Auth（零改动）。"""
    guarded = [
        APP / "application" / "driver.py",
        APP / "application" / "worker.py",
        APP / "application" / "runtime.py",
        APP / "application" / "coordinator.py",
        APP / "application" / "agent_dispatch.py",
        APP / "application" / "delivery.py",
        APP / "application" / "routing.py",
        APP / "application" / "session_resolution.py",
    ]
    for path in guarded:
        text = path.read_text(encoding="utf-8")
        assert "browser_auth" not in text, f"{path.name} 不应耦合 Browser Auth"
        assert "tenant_scope_provider" not in text, f"{path.name} 不应耦合 tenant 依赖"

    # SDK 完全无关
    for py in SDK.rglob("*.py"):
        assert "browser_auth" not in py.read_text(encoding="utf-8")


# =============================================================== 13 B 的 RLS/UoW 零重写


def test_b_tenant_isolation_implementation_untouched() -> None:
    """13：Cloud-0.1.1-B 的 UoW SET LOCAL + 迁移 0002 的 RLS 保持原样。"""
    uow_src = inspect.getsource(SqlAlchemyUnitOfWork)
    assert "set_config('app.tenant_id', :tid, true)" in uow_src
    assert 'dialect.name == "postgresql"' in uow_src
    # UoW 仍构造全部 8 个 core repo + 新增 browser_auth
    for repo in ("users", "channels", "agents", "identities",
                 "connections", "sessions", "interactions", "activities",
                 "browser_auth"):
        assert f"self.{repo} = " in uow_src

    mig0002 = (BACKEND / "alembic" / "versions" / "0002_tenant_isolation.py")
    assert mig0002.is_file()
    assert "ROW LEVEL SECURITY" in mig0002.read_text(encoding="utf-8")


def test_auth_migration_0003_additive_no_rls() -> None:
    """0002 → 0003：新增 2 张加性 auth 表；不碰 RLS（B 的第二层防线零改动）。"""
    versions = sorted((BACKEND / "alembic" / "versions").glob("*.py"))
    migs = [p for p in versions if p.name != "__init__.py"]
    names = {p.name for p in migs}
    assert any(n.startswith("0003") for n in names), f"缺少 0003 迁移：{names}"

    text = next(p for p in migs if p.name.startswith("0003")).read_text(encoding="utf-8")
    assert 'revision: str = "0003"' in text
    assert 'down_revision: Union[str, None] = "0002"' in text
    assert '"user_credentials"' in text and '"browser_sessions"' in text
    assert "ROW LEVEL SECURITY" not in text, "0003 不应改动 RLS（B 零重写）"


def test_browser_auth_lookup_is_bootstrap_not_tenant_filtered() -> None:
    """13（补充）：auth 表按全局唯一 login / session_id 定位（bootstrap），
    不按 tenant 过滤——真正的数据隔离由后续 tenant-scoped UoW（B）承担。"""
    from app.persistence.repositories.browser_auth import (
        SqlAlchemyBrowserAuthRepository,
    )
    sig = inspect.signature(SqlAlchemyBrowserAuthRepository.__init__)
    assert list(sig.parameters) == ["self", "session"], "auth repo 不应接收 tenant_id"
    src = inspect.getsource(SqlAlchemyBrowserAuthRepository)
    assert "UserCredentialModel.login ==" in src
    assert "BrowserSessionModel.session_id ==" in src
    assert "tenant_id ==" not in src, "auth 查找不应按 tenant 过滤"


def test_uow_pg_connection_pool_justifies_set_local_unchanged() -> None:
    """13（补充）：PG 默认连接池结论不变（证明 B 的 SET LOCAL 仍必需）。"""
    assert _engine_kwargs("postgresql://u:p@localhost/db") == {}
