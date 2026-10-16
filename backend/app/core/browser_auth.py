"""Cloud-0.1.1-C —— Browser Authentication 上下文与端口（最小闭环）。

本模块兑现 Gate 裁决的问题链：

    Browser
      ↓  L1  Cookie（opaque session id）
    BrowserAuthenticator.verify()
      ↓  L2  Who is this user?
    AuthenticatedUser(user_id, tenant_id)
      ↓  L3  Which Workspace/Tenant?（复用 app.core.tenant）
    TenantScope(tenant_id)
      ↓  L4  What resources?（复用 Cloud-0.1.1-B 的两层隔离）
    existing tenant-scoped UoW

边界纪律（与 docs/step-cloud-0.1.1-C-browser-authentication-gate.md 一致）：

- 本模块只承载 **L1 + L2**（人类操作员身份识别）。L3/L4 复用 B 已冻结的
  `TenantScope` 与 tenant-scoped UoW —— 本模块**不**重建隔离、**不**实现授权。
- 明确**不做**：OAuth Platform / SSO / RBAC / Organization / SCIM / Enterprise IAM /
  JWT complexity（Gate §3 七大禁区）。
- 纯契约层：**不 import** SQLAlchemy / FastAPI / persistence / application —— 被
  API 入口与 application 层共同引用，不反向依赖任何业务层。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class AuthenticatedUser:
    """Browser Auth 的输出（L2）：一个已认证的人类操作员。

    仅携带身份事实（`user_id` + `tenant_id`）。`tenant_id` 与
    `app.core.tenant.TenantScope.tenant_id` 同域，是 L3 的直接输入源。
    """

    user_id: int
    tenant_id: int


class BrowserAuthError(Exception):
    """Browser 认证失败（HTTP 边界映射 **401 Unauthorized**）。

    刻意**不**映射 403：403 保留给「已认证但无权限 / tenant 解析失败」的语义
    （WeCom 入站验签用的就是 403）。Browser Auth 只产生 401，绝不混入授权语义。
    """


@dataclass(frozen=True)
class CredentialRecord:
    """`user_credentials` 的读取投影（登录校验用）。

    携带 `tenant_id` 是刻意的：登录发生在 **tenant 解析之前**（bootstrap），
    此时不能读受 RLS 保护的 `users` 表来推断租户，故 tenant 由凭据行直接给出。
    """

    user_id: int
    tenant_id: int
    login: str
    verifier: str


@dataclass(frozen=True)
class SessionRecord:
    """`browser_sessions` 的读取投影。

    同样携带 `tenant_id`：session 解析在 tenant 解析之前（bootstrap），
    tenant 必须由 session 行直接给出，不能回头读 `users`。
    """

    session_id: str
    user_id: int
    tenant_id: int


@runtime_checkable
class BrowserAuthenticator(Protocol):
    """把浏览器呈交的凭据（opaque session cookie）解析为 `AuthenticatedUser`。

    契约（实现期落地，本模块只锁接口）：
    - 成功 → `AuthenticatedUser`；
    - 失败 / 未知 / 缺失 → 抛 `BrowserAuthError`（映射 401）。
    """

    def verify(self, proof: str) -> AuthenticatedUser: ...


__all__ = [
    "AuthenticatedUser",
    "BrowserAuthError",
    "BrowserAuthenticator",
    "CredentialRecord",
    "SessionRecord",
]
