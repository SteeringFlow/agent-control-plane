"""M5.2 —— Cloud Product Entry 验收（Auth Boundary / Version / Root / Product Entry）。

四个验收块（与 M5.2 指令 §二/§八/§五/§三/§四/§九 一一对应）：

    Gate 1  应用层 Auth Boundary：移除 Nginx BasicAuth 前先证明——
            未登录管理 API 全部 401；signup/login 公开可达；
            me 401→200（登录后）；logout 清会话；登录后管理 API 正常。
    §八     Deployment Provenance：GET /api/version = {version, commit}；
            version ← 根目录 VERSION 文件；commit ← 构建期 GIT_SHA；
            缺失回退 "unknown"；不读 git runtime history。
    §五     Root Entry：`location = /` → 302 /workspace.html（Nginx 模板守卫）。
    §三/四  Product Entry：workspace.html / workspace.js 具备 Sign Up + Login
            同页 toggle；Login 只调既有 POST /api/auth/login。

边界纪律（不新增业务能力 / 不改 domain）：
    * 管理面 401 由**既有** TenantScope 依赖（auth_required=True）裁决；
    * Login 走既有 POST /api/auth/login（opaque 会话 Cookie，无 token storage）；
    * 本文件只验证 M5.2 契约，不重测 Browser Auth 内部实现（见 0.1.1.C 测试）。
"""

import ast
import functools
import os
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.api.auth import build_auth_api, build_tenant_scope_provider
from app.api.management.agents import build_agents_api
from app.api.management.channels import build_channels_api
from app.api.management.connections import build_connections_api
from app.api.management.identities import build_identities_api
from app.api.management.interactions import build_interactions_api
from app.api.management.users import build_users_api
from app.api.version import build_version_api, read_commit, read_version
from app.application.agents import AgentApplicationService
from app.application.browser_auth import BrowserAuthService
from app.application.channels import ChannelApplicationService
from app.application.connections import ConnectionApplicationService
from app.application.identities import IdentityApplicationService
from app.application.interaction_observability import InteractionObservabilityService
from app.application.tenant_provisioning import TenantProvisioningService
from app.application.users import UserApplicationService
from app.core.tenant import TenantScope
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND = REPO_ROOT / "frontend"
NGINX_TEMPLATE = REPO_ROOT / "_deploy" / "nginx" / "acp-pilot-vhost.conf.template"

# Gate 1 必须 401 的管理 API（M5.2 指令 §二 冻结清单）。
MANAGEMENT_APIS = (
    "/api/agents",
    "/api/channels",
    "/api/connections",
    "/api/users",
    "/api/identities",
    "/api/interactions",
)


# ---------------------------------------------------------------- helpers


def _session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


def _uow_factory(engine):
    factory = _session_factory(engine)

    def factory_fn(tenant_id: int | None = None) -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory=factory, tenant_id=tenant_id)

    return factory_fn


def _build_app(engine, *, auth_required: bool = True) -> FastAPI:
    """装配 Gate 1 验收 app：auth router + 六个管理面（tenant-scoped）。

    与组合根 main.create_app 的管理面装配**同构**——区别仅是 UoW 绑定内存库。
    这是「移除 Nginx BasicAuth 是否安全」的直接证据面：管理 API 的 401
    完全由应用层 TenantScope 依赖产生，与边缘 BasicAuth 无关。
    """
    app = FastAPI()
    factory = _uow_factory(engine)
    auth_service = BrowserAuthService(
        uow_factory=factory, provisioner=TenantProvisioningService(uow_factory=factory)
    )
    app.include_router(build_auth_api(auth_service, cookie_secure=False))
    scope_provider = build_tenant_scope_provider(
        auth_service, auth_required=auth_required
    )

    def _scoped(scope: TenantScope = Depends(scope_provider)):
        """请求解析出的 TenantScope → tenant-scoped UoW 工厂（组合根同构）。"""
        return functools.partial(
            SqlAlchemyUnitOfWork,
            session_factory=_session_factory(engine),
            tenant_id=scope.tenant_id,
        )

    def _agents(scope: TenantScope = Depends(scope_provider)):
        return AgentApplicationService(uow_factory=_scoped(scope))

    def _channels(scope: TenantScope = Depends(scope_provider)):
        return ChannelApplicationService(uow_factory=_scoped(scope))

    def _connections(scope: TenantScope = Depends(scope_provider)):
        return ConnectionApplicationService(uow_factory=_scoped(scope))

    def _users(scope: TenantScope = Depends(scope_provider)):
        return UserApplicationService(uow_factory=_scoped(scope))

    def _identities(scope: TenantScope = Depends(scope_provider)):
        return IdentityApplicationService(uow_factory=_scoped(scope))

    def _interactions(scope: TenantScope = Depends(scope_provider)):
        return InteractionObservabilityService(uow_factory=_scoped(scope))

    app.include_router(build_agents_api(service_provider=_agents))
    app.include_router(build_channels_api(service_provider=_channels))
    app.include_router(build_connections_api(service_provider=_connections))
    app.include_router(build_users_api(service_provider=_users))
    app.include_router(build_identities_api(service_provider=_identities))
    app.include_router(build_interactions_api(service_provider=_interactions))
    return app


# =============================================================== Gate 1 — Auth Boundary


def test_gate1_unauthenticated_management_apis_all_401(db_engine) -> None:
    """未登录：六个管理 API 全部 401（应用层裁决，无需边缘 BasicAuth）。"""
    client = TestClient(_build_app(db_engine, auth_required=True))
    for path in MANAGEMENT_APIS:
        resp = client.get(path)
        assert resp.status_code == 401, f"未登录 {path} 应 401，实际 {resp.status_code}"


def test_gate1_signup_and_login_publicly_reachable(db_engine) -> None:
    """signup / login 公开可达（未登录也 201 / 200，无 401 前置）。"""
    client = TestClient(_build_app(db_engine, auth_required=True))

    # M7-AUI-0.3：注册入口的 login 是邮箱（User 身份 = Email + password）。
    signup = client.post(
        "/api/auth/signup",
        json={"login": "m52-founder@steeringflow.test", "password": "pw"})
    assert signup.status_code == 201

    # login = 既有账号（signup 已建 user/tenant）走同一公开面。
    login = client.post(
        "/api/auth/login",
        json={"login": "m52-founder@steeringflow.test", "password": "pw"})
    assert login.status_code == 200


def test_gate1_me_logout_session_lifecycle(db_engine) -> None:
    """/api/auth/me 未登录 401 → 登录后 200 → logout 204 → 再次 401。"""
    client = TestClient(_build_app(db_engine, auth_required=True))
    assert client.get("/api/auth/me").status_code == 401

    signup = client.post(
        "/api/auth/signup",
        json={"login": "lifecycle@steeringflow.test", "password": "pw"})
    assert signup.status_code == 201
    tenant_id = signup.json()["tenant_id"]
    user_id = signup.json()["user_id"]

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json() == {"user_id": user_id, "tenant_id": tenant_id}

    assert client.post("/api/auth/logout").status_code == 204
    assert client.get("/api/auth/me").status_code == 401


def test_gate1_authenticated_management_api_allowed(db_engine) -> None:
    """登录后：管理 API 正常（200）——证明 401 只挡未登录，不挡产品用户。"""
    client = TestClient(_build_app(db_engine, auth_required=True))
    signup = client.post(
        "/api/auth/signup",
        json={"login": "ops-user@steeringflow.test", "password": "pw"})
    assert signup.status_code == 201

    for path in MANAGEMENT_APIS:
        resp = client.get(path)
        assert resp.status_code == 200, f"登录后 {path} 应 200，实际 {resp.status_code}"


# =============================================================== §八 — Version


def test_version_endpoint_returns_version_and_commit(monkeypatch) -> None:
    """GET /api/version → {version, commit}；version ← 根目录 VERSION 文件。"""
    monkeypatch.setenv("GIT_SHA", "ea2487962ae2fbd37df81d36db81196b5f498037")
    app = FastAPI()
    app.include_router(build_version_api())
    client = TestClient(app)

    resp = client.get("/api/version")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "version": read_version(),
        "commit": "ea2487962ae2fbd37df81d36db81196b5f498037",
    }
    # version 事实源 = 仓库根 VERSION 文件（非 OpenAPI 元数据）。
    assert body["version"] == (REPO_ROOT / "VERSION").read_text(
        encoding="utf-8").strip()


def test_version_commit_falls_back_to_unknown_without_git_sha(monkeypatch) -> None:
    """GIT_SHA 缺失 → commit == "unknown"（绝不读 git runtime history）。"""
    monkeypatch.delenv("GIT_SHA", raising=False)
    assert read_commit() == "unknown"

    app = FastAPI()
    app.include_router(build_version_api())
    body = TestClient(app).get("/api/version").json()
    assert body["commit"] == "unknown"


def test_version_version_file_missing_falls_back_to_unknown(monkeypatch) -> None:
    """VERSION 文件缺失 → version == "unknown"（read_version 不抛错）。"""
    from app.api import version as version_module

    monkeypatch.setattr(
        version_module, "VERSION_FILE", Path("<nonexistent>") / "VERSION")
    assert version_module.read_version() == "unknown"


def test_version_endpoint_has_no_auth_dependency() -> None:
    """分层守卫：/api/version 无认证依赖（纯构建事实，不含 secret 面）。"""
    source = (Path(__file__).resolve().parents[1] / "app" / "api" / "version.py"
              ).read_text(encoding="utf-8")
    tree = ast.parse(source, filename="version.py")
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    offenders = {m for m in imported if m.startswith(
        ("app.api.auth", "app.application.browser_auth", "app.core.security"))}
    assert not offenders, f"/api/version 不得引入认证依赖：{offenders}"


# =============================================================== §五 — Root Entry


def test_root_entry_redirect_rule_in_nginx_template() -> None:
    """Nginx 模板：`location = /` → 302 /workspace.html（Cloud Product Entry）。"""
    template = NGINX_TEMPLATE.read_text(encoding="utf-8")
    assert "location = / {" in template
    assert "return 302 /workspace.html;" in template


def test_root_entry_nginx_boundary_contract() -> None:
    """Nginx 模板边界契约（M5.2 §六/§七 冻结）：

    * 产品兜底块 `location /` **不挂** auth_basic（产品面授权归应用层）；
    * webhook 仍为无 auth_basic 的 PUBLIC 直通；
    * /health 仍 404；
    * 运维面 /docs /redoc /openapi.json 保留专属 BasicAuth location。
    """
    template = NGINX_TEMPLATE.read_text(encoding="utf-8")

    # 兜底块不挂 BasicAuth：最后一个 `location / {` 块内无 auth_basic。
    fallback_start = template.rindex("location / {")
    fallback_body = template[fallback_start:]
    assert "auth_basic" not in fallback_body, "产品兜底块不得挂 auth_basic"

    # /health 仍 404；webhook 直通；运维面专属 BasicAuth。
    assert "location = /health {" in template
    health_body = template[template.index("location = /health {"):
                           template.index("location = /health {") + 120]
    assert "return 404;" in health_body
    assert "wecom/webhook$" in template
    assert "docs|redoc|openapi" in template


def test_dockerfile_and_compose_inject_git_sha() -> None:
    """Deployment provenance 装配：Dockerfile ARG/ENV GIT_SHA + COPY VERSION；
    compose build args 注入 GIT_SHA（部署时经宿主 shell 传当前 commit）。"""
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "ARG GIT_SHA=" in dockerfile
    assert "ENV GIT_SHA=${GIT_SHA}" in dockerfile
    assert "COPY VERSION /app/VERSION" in dockerfile

    compose = (REPO_ROOT / "docker-compose.production.yml").read_text(encoding="utf-8")
    assert "GIT_SHA" in compose


# =============================================================== §三/§四 — Product Entry


def test_workspace_html_has_signup_and_login_toggle() -> None:
    """workspace.html：Sign Up + Login 同页 toggle（不引新框架 / 不改业务功能）。"""
    html = (FRONTEND / "workspace.html").read_text(encoding="utf-8")
    # Sign Up 保留
    assert 'id="signup-form"' in html
    assert 'id="f-login"' in html and 'id="f-password"' in html
    # Login 新增
    assert 'id="login-form"' in html
    assert 'id="l-login"' in html and 'id="l-password"' in html
    # toggle 存在
    assert 'id="tab-signup"' in html and 'id="tab-login"' in html
    assert 'id="signup-pane"' in html and 'id="login-pane"' in html


def test_workspace_js_login_uses_existing_login_endpoint() -> None:
    """workspace.js：Login 只调既有 POST /api/auth/login + 复用 /api/auth/me。"""
    js = (FRONTEND / "workspace.js").read_text(encoding="utf-8")
    assert '`${API}/login`' in js
    assert "submitLogin" in js
    assert "showAuthTab" in js
    # 无 token storage / localStorage / JWT 引入。
    lowered = js.lower()
    for banned in ("localstorage", "sessionstorage", "bearer", "authorization"):
        assert banned not in lowered, f"workspace.js 引入了被禁的凭据面：{banned}"
    # 不调管理 API（Workspace UI 只谈 auth API，延续 0.1.2.1 冻结）。
    for banned in ("/api/agents", "/api/channels", "/api/connections"):
        assert banned not in js


def test_connect_pages_are_compat_redirects() -> None:
    """旧 Connect Agent / Connect Channel 页面的冻结职责（M7-AUI-8.2）。

    这两个页面已收口为**兼容跳转页**：文件继续存在、继续 200、客户端立刻跳转到
    统一接入页 `/connect.html`（`/connect.html` 自身提供返回 Flow Home 的入口）。
    因此本测试守护的是「不 404 / 不破坏书签」这一条，而不再是页面内的 workspace 链接。
    """
    for name in ("connect-agent.html", "connect-channel.html"):
        path = FRONTEND / name
        assert path.is_file(), f"{name} 必须保留（书签不 404）"
        html = path.read_text(encoding="utf-8")
        assert "url=/connect.html" in html, f"{name} 应保留统一接入页跳转"
