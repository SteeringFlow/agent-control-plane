"""Cloud-0.1.2.1 —— Registration → Workspace Entry 验证门。

验证的产品化假设（见 docs/step-cloud-0.1.2.1-registration-workspace-entry.md）：

    Cloud 用户注册之后，**不需要额外「创建 Workspace」**。

    Sign Up → TenantProvisioner → User + Tenant → Browser Session
            → AuthenticatedUser → Workspace UI

本文件机器化 owner 清单 §4，并延续 Cloud-0.1.2 Gate 的红线（无第二套归属模型）。
"""

import dataclasses

from tests.oss_export_boundary import oss_ui_required
import io
import re
import tokenize
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.api.auth import build_auth_api
from app.application.browser_auth import BrowserAuthService, SignUpBrowserUser
from app.application.tenant_provisioning import TenantProvisioningService
from app.core.tenant import TENANT_LOCAL
from app.persistence.models.browser_session import BrowserSessionModel
from app.persistence.models.credential import UserCredentialModel
from app.persistence.models.tenant import TenantModel
from app.persistence.models.user import UserModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND = REPO_ROOT / "frontend"
BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"


# ---------------------------------------------------------------- helpers


def _uow_factory(engine):
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def factory_fn(tenant_id: int | None = None) -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory=factory, tenant_id=tenant_id)

    return factory_fn


def _auth_service(engine) -> BrowserAuthService:
    f = _uow_factory(engine)
    return BrowserAuthService(
        uow_factory=f, provisioner=TenantProvisioningService(uow_factory=f)
    )


def _build_app(engine) -> FastAPI:
    app = FastAPI()
    app.include_router(build_auth_api(_auth_service(engine), cookie_secure=False))
    return app


def _signup(client: TestClient, login: str, password: str):
    return client.post("/api/auth/signup", json={"login": login, "password": password})


# M7-AUI-0.3：注册入口要求邮箱格式（User 身份 = Email + password）。
EMAIL_A = "api-founder@steeringflow.test"
EMAIL_B = "dup@steeringflow.test"


def _strip_js_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    src = re.sub(r"//[^\n]*", " ", src)
    return src


def _strip_html_comments(src: str) -> str:
    return re.sub(r"<!--.*?-->", " ", src, flags=re.S)


def _app_code_identifiers() -> str:
    names: list[str] = []
    for py in APP.rglob("*.py"):
        if "__pycache__" in py.parts:
            continue
        src = py.read_text(encoding="utf-8")
        try:
            toks = tokenize.generate_tokens(io.StringIO(src).readline)
        except Exception:
            continue
        names += [t.string for t in toks if t.type == tokenize.NAME]
    return f" {' '.join(names).lower()} "


# =============================================================== 1 / 2 注册即建租户 + 用户


def test_signup_provisions_new_tenant_without_explicit_tenant(db_engine) -> None:
    """1+2：Sign Up 自动建租户（无需 tenant_id / 无需 Workspace Name），并建 User + 凭据。"""
    service = _auth_service(db_engine)
    # M7-AUI-0.3：注册入口的 login 是邮箱（User 身份 = Email + password）。
    grant = service.signup(
        SignUpBrowserUser(login="founder@steeringflow.test", password="pw"))

    # 自动产生了一个**新**租户（不是 TENANT_LOCAL 兜底）。
    assert grant.user.tenant_id > TENANT_LOCAL

    with Session(db_engine) as session:
        tenant = session.get(TenantModel, grant.user.tenant_id)
        user = session.execute(
            select(UserModel).where(UserModel.name == "founder@steeringflow.test")
        ).scalar_one()
        cred = session.execute(
            select(UserCredentialModel).where(
                UserCredentialModel.login == "founder@steeringflow.test")
        ).scalar_one()

    assert tenant is not None
    assert tenant.name == "founder@steeringflow.test" and tenant.status == "active"
    assert user.tenant_id == grant.user.tenant_id
    assert cred.user_id == user.id and cred.tenant_id == grant.user.tenant_id
    # 凭据绝不明文
    assert cred.verifier != "pw" and "pw" not in cred.verifier


# =============================================================== 3 opaque 会话


def test_signup_creates_opaque_session_and_grant(db_engine) -> None:
    """3：注册即建 opaque 会话（非 JWT），返回可写 Cookie 的 grant。"""
    service = _auth_service(db_engine)
    grant = service.signup(
        SignUpBrowserUser(login="bob@steeringflow.test", password="pw"))

    assert grant.session_id
    assert grant.session_id.count(".") == 0
    assert not grant.session_id.startswith("eyJ")

    with Session(db_engine) as session:
        rows = session.execute(select(BrowserSessionModel)).scalars().all()
    assert len(rows) == 1
    assert rows[0].session_id == grant.session_id
    assert rows[0].user_id == grant.user.user_id
    assert rows[0].tenant_id == grant.user.tenant_id


# =============================================================== 4 无「创建 Workspace」步骤


def test_signup_input_has_no_workspace_step() -> None:
    """4：Sign Up 输入只有 login / password——没有 Workspace Name / Confirm 等额外步骤。"""
    fields = {f.name for f in dataclasses.fields(SignUpBrowserUser)}
    assert fields == {"login", "password"}, fields


# =============================================================== 5 / 6 / 7 API 落地已认证 Workspace


def test_signup_via_api_lands_authenticated_in_workspace(db_engine) -> None:
    """5+6：API Sign Up → 201 + Cookie；GET /session 证明已进入自己的 Workspace。"""
    client = TestClient(_build_app(db_engine))

    resp = _signup(client, EMAIL_A, "pw")
    assert resp.status_code == 201
    body = resp.json()
    assert body["tenant_id"] > TENANT_LOCAL

    set_cookie = resp.headers["set-cookie"].lower()
    assert "acp_session=" in set_cookie
    assert "httponly" in set_cookie
    assert "samesite=strict" in set_cookie
    assert "path=/" in set_cookie

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json() == {"user_id": body["user_id"], "tenant_id": body["tenant_id"]}

    # 注销 → 会话消失，/me 端点 401
    assert client.post("/api/auth/logout").status_code == 204
    assert client.get("/api/auth/me").status_code == 401


def test_me_endpoint_requires_authentication(db_engine) -> None:
    """7：无 Cookie → GET /api/auth/me → 401（不猜测、不兜底）。"""
    client = TestClient(_build_app(db_engine))
    assert client.get("/api/auth/me").status_code == 401


# =============================================================== 8 重复注册不留孤儿租户


def test_signup_duplicate_login_conflict_and_no_orphan_tenant(db_engine) -> None:
    """8：重复 login → 409，且**不**在 tenants 留下孤儿租户（provision 前预检）。"""
    client = TestClient(_build_app(db_engine))
    assert _signup(client, EMAIL_B, "p1").status_code == 201

    with Session(db_engine) as session:
        before = len(session.execute(select(TenantModel)).scalars().all())

    assert _signup(client, EMAIL_B, "p2").status_code == 409

    with Session(db_engine) as session:
        after = len(session.execute(select(TenantModel)).scalars().all())
    assert after == before, "重复注册不应留下孤儿租户"


# =============================================================== 9 tenants 唯一写入口


def test_tenant_provisioner_is_only_tenants_writer() -> None:
    """9：`tenants` 唯一写入路径 = TenantProvisioner（TenantProvisioningService）。

    全 backend 真实源码中，`uow.tenants.create(` 只允许出现在 tenant_provisioning.py。
    """
    service = TenantProvisioningService(uow_factory=_uow_factory_stub)
    assert hasattr(service, "provision")

    hits: list[str] = []
    for py in APP.rglob("*.py"):
        if "__pycache__" in py.parts:
            continue
        if ".tenants.create(" in py.read_text(encoding="utf-8"):
            hits.append(py.name)
    assert hits == ["tenant_provisioning.py"], f"tenants 写入点越界：{hits}"


def _uow_factory_stub(tenant_id: int | None = None):  # pragma: no cover - 仅供类型检查
    raise NotImplementedError


# =============================================================== 10-12 Workspace UI


def test_workspace_ui_has_single_connect_agent_cta() -> None:
    """10：Workspace UI 证明「已进入自己的 Workspace」，唯一核心 CTA = Connect Agent。"""
    html = (FRONTEND / "workspace.html").read_text(encoding="utf-8")
    assert "Connect Agent" in html
    # M7-AUI-8.2：并列的两个接入 CTA（目标相同）合并为一个「开始接入 →」。
    assert 'id="cta-start-connect"' in html
    # Sign Up 只有 login + password 两个输入（无 Workspace Name / 无 tenant_id 手填）
    assert 'id="f-login"' in html and 'id="f-password"' in html
    assert "workspace name" not in html.lower()
    assert 'name="tenant_id"' not in html


def test_workspace_ui_excludes_forbidden_surfaces() -> None:
    """11：0.1.2.1 明确不做的表面一个都不出现（注释已剥离，只看真实标记）。"""
    html = _strip_html_comments(
        (FRONTEND / "workspace.html").read_text(encoding="utf-8"))
    js = _strip_js_comments(
        (FRONTEND / "workspace.js").read_text(encoding="utf-8"))
    text = f"{html}\n{js}".lower()
    for banned in (
        "settings", "rename", "member", "invite", "rbac",
        "billing", "dashboard", "profile", "organization",
    ):
        assert banned not in text, f"Workspace UI 出现越界表面：{banned}"


def test_workspace_ui_only_talks_to_auth_api() -> None:
    """12：Workspace UI 只经 /api/auth（无 Dashboard / 管理面越界端点）。"""
    js = (FRONTEND / "workspace.js").read_text(encoding="utf-8")
    assert 'const API = "/api/auth";' in js
    for banned in (
        "/api/agents", "/api/channels", "/api/connections",
        "/api/users", "/api/identities", "/api/interactions",
    ):
        assert banned not in js, f"Workspace UI 越界调用：{banned}"


# =============================================================== 13 无 Workspace 持久化


def test_no_workspace_persistence_introduced() -> None:
    """13：不引入 Workspace 持久化 / 第二套归属模型（延续 Cloud-0.1.2 Gate 红线）。"""
    from app.persistence import models as _models_pkg  # noqa: F401
    from app.persistence.database import Base

    tables = set(Base.metadata.tables.keys())
    assert "workspaces" not in tables
    assert "workspace_members" not in tables
    assert "organizations" not in tables

    # backend 真实代码标识符中不得出现 workspace（Workspace 只是 UX 抽象）。
    assert " workspace " not in _app_code_identifiers()


# =============================================================== 14 静态可达


@oss_ui_required()
def test_workspace_page_is_served_by_app() -> None:
    """14：Workspace UI 由组合根静态托管且可达（同源、无构建链）。"""
    from app.main import app

    client = TestClient(app)
    page = client.get("/workspace.html")
    assert page.status_code == 200
    assert "Connect Agent" in page.text

    js = client.get("/workspace.js")
    assert js.status_code == 200
    assert 'const API = "/api/auth";' in js.text
