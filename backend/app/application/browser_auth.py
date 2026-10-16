"""Browser Authentication Application Service（Cloud-0.1.1-C.1）。

兑现 Gate 裁决的最小闭环：

    register → login → verify(cookie) → AuthenticatedUser → TenantScope

边界（刻意写死，见 docs/step-cloud-0.1.1-C-browser-authentication-gate.md）：

- 只回答 **L1+L2**（操作员身份 + 派生的 tenant_id）；tenant-scoped UoW（L3/L4）
  复用 Cloud-0.1.1-B —— 本服务不实现隔离、不实现授权（无 RBAC / 角色 / 权限）；
- 凭据校验用 `app.core.passwords`（stdlib pbkdf2，无 JWT / 无外部依赖）；
- bootstrap 路径（login / verify / logout）用 `uow_factory(None)`：登录发生在
  tenant 解析之前，且 `users` 受 RLS 保护，故 tenant 直接取自 auth 记录；
- register 需要写 `users`（受 RLS 保护的租户数据），故用 `uow_factory(tenant_id)`
  的 tenant-scoped UoW；
- 事务边界归 UoW（每个用例自持一个 UoW，显式 commit）。

`uow_factory` 以 `tenant_id` 为入参（`Callable[[int | None], UnitOfWork]`）——这是
auth 边界的天然需要：它必须在 bootstrap（None）与具体租户之间选择作用域。

依赖边界（AST 守护）：不 import SQLAlchemy / FastAPI / api / schemas / adapters /
app.persistence；只依赖 app.core、app.repositories（端口）、app.application。
"""

from __future__ import annotations

import base64
import os
import re
from collections.abc import Callable
from dataclasses import dataclass

from app.application.errors import (
    DuplicateBrowserLoginError,
    InvalidBrowserAuthInputError,
)
from app.core.browser_auth import AuthenticatedUser, BrowserAuthError
from app.core.passwords import hash_password, verify_password
from app.core.tenant import TenantProvisioner
from app.domain.user import User
from app.repositories.unit_of_work import UnitOfWork

# 会话令牌字节数：opaque 随机（非 JWT、非签名声明链）。
_SESSION_TOKEN_BYTES = 32


def _new_session_id() -> str:
    """生成 opaque 会话令牌：URL-safe base64 随机字节（无签名、无结构）。"""
    return base64.urlsafe_b64encode(
        os.urandom(_SESSION_TOKEN_BYTES)).rstrip(b"=").decode("ascii")


@dataclass(frozen=True)
class RegisterBrowserUser:
    """注册浏览器用户的输入契约（应用层校验点）。

    `tenant_id` 是新建 User 的归属租户（本地/OSS 走 TENANT_LOCAL）；
    `login` 是全局唯一登录标识，同时作为 User 的显示名（最小实现）。
    """

    login: str
    password: str
    tenant_id: int


@dataclass(frozen=True)
class SignUpBrowserUser:
    """Cloud Sign Up 输入契约（Cloud-0.1.2.1）。

    刻意**只有** login / password：注册即获得一个 Tenant（用户 Workspace 的实体化），
    用户不需要额外经历「创建 Workspace → 填 Workspace Name → Confirm」。

    M7-AUI-0.3：`login` 的产品语义是**邮箱**（User 身份 = Email + password），
    因此注册入口校验邮箱格式；唯一性由 `user_credentials.login` 唯一约束兜底。
    """

    login: str
    password: str


@dataclass(frozen=True)
class LoginBrowserUser:
    """登录输入契约。"""

    login: str
    password: str


@dataclass(frozen=True)
class BrowserSessionGrant:
    """登录成功的输出：opaque session_id + 已认证用户（供 API 写 Cookie）。"""

    session_id: str
    user: AuthenticatedUser


def _require_non_blank(value: str | None, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise InvalidBrowserAuthInputError(f"{field} 不能为空")


# 邮箱格式（M7-AUI-0.3）：注册入口的基本形状校验——**只做格式，不验证所有权**
# （验证码 / OTP / SMTP / email_verified 一律不在本阶段实现）。
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _require_email(value: str) -> None:
    """注册入口的邮箱格式校验。

    前端有同一条校验，但**服务端绝不只依赖前端**：绕过前端直接调 API 也必须拦下
    （`InvalidBrowserAuthInputError` → 422）。
    """
    if not isinstance(value, str) or not _EMAIL_RE.match(value.strip()):
        raise InvalidBrowserAuthInputError("邮箱格式不正确")


class BrowserAuthService:
    """Browser Auth 用例编排者（依赖 UoW Port，而非具体 SQLAlchemy 实现）。

    同时实现 `app.core.browser_auth.BrowserAuthenticator` 端口（`verify`）。
    """

    def __init__(
        self,
        uow_factory: Callable[[int | None], UnitOfWork],
        provisioner: TenantProvisioner | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        # Cloud-0.1.2.1：Sign Up 自动建租户（唯一写 tenants 入口 = TenantProvisioner）。
        # 未注入时 register 仍要求显式 tenant_id（OSS / 既有行为不变）。
        self._provisioner = provisioner

    # ------------------------------------------------------------ register

    def register(self, input: RegisterBrowserUser) -> AuthenticatedUser:
        """创建最小 Browser User（User 行 + 凭据行），返回已认证用户。

        - login / password 非空校验（否则 422）；
        - login 全局唯一（否则 409）；
        - User 落 tenant-scoped UoW（repo 显式 stamp tenant_id），凭据落 auth 表。
        """
        _require_non_blank(input.login, "login")
        _require_non_blank(input.password, "password")
        if not isinstance(input.tenant_id, int) or input.tenant_id <= 0:
            raise InvalidBrowserAuthInputError("tenant_id 必须是正整数")

        with self._uow_factory(input.tenant_id) as uow:
            if uow.browser_auth.find_credential_by_login(input.login) is not None:
                raise DuplicateBrowserLoginError(input.login)
            user = uow.users.create(User(name=input.login))
            assert user.id is not None
            uow.browser_auth.create_credential(
                user_id=user.id,
                tenant_id=input.tenant_id,
                login=input.login,
                verifier=hash_password(input.password),
            )
            uow.commit()
            return AuthenticatedUser(user_id=user.id, tenant_id=input.tenant_id)

    # ------------------------------------------------------------ sign up

    def signup(self, input: SignUpBrowserUser) -> BrowserSessionGrant:
        """Cloud Sign Up（0.1.2.1）：注册 → 建租户 → 建用户 → 建会话 → 已认证。

        产品化假设：**注册后用户的 Workspace 已经存在，无需额外「创建 Workspace」**。
        因此本用例把「provision tenant」「create user + credential」「create browser
        session」串成一次原子产品动作，直接返回可写 Cookie 的会话授权。
        """
        _require_non_blank(input.login, "login")
        _require_non_blank(input.password, "password")
        # M7-AUI-0.3：邮箱格式（服务端独立校验，不依赖前端）。
        _require_email(input.login)
        if self._provisioner is None:
            raise InvalidBrowserAuthInputError(
                "未配置 TenantProvisioner，无法在注册时自动建租户"
            )
        # 0) 预检 login 唯一性（**在 provision 之前**）：避免重复注册失败时
        #    在 tenants 留下孤儿租户。register 内仍会二次校验（DB 唯一约束兜底）。
        with self._uow_factory(None) as uow:
            if uow.browser_auth.find_credential_by_login(input.login) is not None:
                raise DuplicateBrowserLoginError(input.login)
        # 1) 建租户（bootstrap；tenant 名由注册输入派生，用户无需另填 Workspace Name）。
        scope = self._provisioner.provision(input.login)
        # 2) 在该租户内建用户 + 凭据（复用显式租户路径，与 register 同一套语义）。
        user = self.register(RegisterBrowserUser(
            login=input.login, password=input.password, tenant_id=scope.tenant_id))
        # 3) 建 opaque 浏览器会话（注册即登录，直达 Workspace）。
        with self._uow_factory(None) as uow:
            session_id = _new_session_id()
            uow.browser_auth.create_session(
                session_id=session_id, user_id=user.user_id, tenant_id=user.tenant_id)
            uow.commit()
        return BrowserSessionGrant(session_id=session_id, user=user)

    # ------------------------------------------------------------ login

    def login(self, input: LoginBrowserUser) -> BrowserSessionGrant:
        """校验凭据并建立 opaque 会话。

        未知 login 与错误口令一律抛 `BrowserAuthError`（不区分，避免枚举）。
        """
        _require_non_blank(input.login, "login")
        _require_non_blank(input.password, "password")

        with self._uow_factory(None) as uow:
            credential = uow.browser_auth.find_credential_by_login(input.login)
            if credential is None or not verify_password(
                input.password, credential.verifier
            ):
                raise BrowserAuthError("invalid credentials")
            session_id = _new_session_id()
            uow.browser_auth.create_session(
                session_id=session_id,
                user_id=credential.user_id,
                tenant_id=credential.tenant_id,
            )
            uow.commit()
            return BrowserSessionGrant(
                session_id=session_id,
                user=AuthenticatedUser(
                    user_id=credential.user_id, tenant_id=credential.tenant_id
                ),
            )

    # ------------------------------------------------------------ session → user → tenant

    def verify(self, proof: str) -> AuthenticatedUser:
        """`BrowserAuthenticator` 端口实现：cookie 值 → AuthenticatedUser。"""
        return self.authenticate_session(proof)

    def authenticate_session(self, session_id: str) -> AuthenticatedUser:
        """把 opaque session_id 求值为 `AuthenticatedUser`（含 tenant_id）。

        未知 / 缺失 → `BrowserAuthError`（401）。

        注：方法名刻意避开 "resolve"，以不与 Runtime 的 Identity / Session
        Resolution 语义混淆（那些由既有反偷渡审计守护）。
        """
        if not isinstance(session_id, str) or not session_id:
            raise BrowserAuthError("missing session")
        with self._uow_factory(None) as uow:
            record = uow.browser_auth.find_session(session_id)
        if record is None:
            raise BrowserAuthError("unknown session")
        return AuthenticatedUser(user_id=record.user_id, tenant_id=record.tenant_id)

    # ------------------------------------------------------------ logout

    def logout(self, session_id: str) -> None:
        """删除会话（不存在为 no-op）。"""
        if not isinstance(session_id, str) or not session_id:
            return
        with self._uow_factory(None) as uow:
            uow.browser_auth.delete_session(session_id)
            uow.commit()
