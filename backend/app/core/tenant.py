"""Cloud-0.1.1-B —— Tenant Isolation 上下文与入口端口（实现边界的 Core 层）。

职责边界（与 docs/step-cloud-0.1.1-B-tenant-isolation-implementation-boundary.md 一致）：

- `TenantScope`：**显式**租户隔离上下文（frozen dataclass）。沿调用链显式传入，
  绝不进 contextvars / 模块级全局（红线：禁止任何隐式全局租户上下文）。
- `TenantResolver` / `TenantProvisioner`：入口层**端口**（Protocol）。本步只
  定义契约，**不实现** Browser Auth / Webhook Routing（见边界 §12）。

边界纪律：
- Tenant 是 Control Plane 的 **Data Isolation Context**，不是 Agent Runtime Context；
- 本模块**不 import** SQLAlchemy / FastAPI / persistence / application——它是
  纯契约层（被 API 入口与 persistence 的 UoW 共同引用，不反向依赖任何业务层）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class TenantScope:
    """显式租户隔离上下文。

    唯一载荷是 `tenant_id`（与 8 张 Core Table 的 `tenant_id` 列同域）。它是
    Repository Scope 与 PostgreSQL RLS 的共同输入源，但自身不携带任何业务语义。
    """

    tenant_id: int


# OSS / 本地单租户默认租户（实现边界 §11）：无显式 TenantScope 时，组合根仍把它
# 显式绑定进 UoW 生命周期（tenant 必须显式进入，绝不用隐式全局），但其行为等同
# 隔离前——所有行 tenant_id=1，Repository 过滤与 RLS 均为恒等过滤。
TENANT_LOCAL = 1


def default_tenant_scope() -> TenantScope:
    """OSS / 本地单租户默认作用域。

    Cloud 多租户由入口 `TenantResolver` 解析出具体租户后，以同一个
    `functools.partial(UoW, tenant_id=scope.tenant_id)` 桥绑定（§7/§12）——
    本函数只是 OSS 缺省值，不承担任何 Browser Auth / Webhook 解析。
    """
    return TenantScope(tenant_id=TENANT_LOCAL)


class IngressTenantError(Exception):
    """入口租户解析失败 / 无权限（HTTP 边界映射 403 / 404）。

    刻意不继承任何 Application / Runtime 错误——租户解析发生在 Runtime 之前，
    不进入 Runtime Failure Model。
    """


@runtime_checkable
class TenantResolver(Protocol):
    """入口处把 routing hint + 凭据 解析为 `TenantScope`。

    契约（实现期才落地，本步只锁接口）：
    - 在 UoW 构造**之前**解析（解析不得依赖已 SET LOCAL 的会话）；
    - `routing hint ≠ authentication proof`：tenant 必须取自**已校验**对象
      （如校验签名后的 Channel.tenant_id），绝不取自 URL；
    - 解析失败 / 无权限 → 抛 `IngressTenantError`。
    """

    def resolve(self, request_context: Any) -> TenantScope: ...


@runtime_checkable
class TenantProvisioner(Protocol):
    """唯一被允许写入 `tenants` 表的入口（创建租户 + 初始 admin）。

    必须经 bootstrap RLS 分支（`app.tenant_id` 未设）执行——普通业务事务已
    `SET LOCAL`，受 per-tenant RLS 约束，无权写 `tenants`（边界 §10）。
    """

    def provision(self, name: str, *, config: dict | None = None) -> TenantScope: ...


__all__ = [
    "TenantScope",
    "TENANT_LOCAL",
    "default_tenant_scope",
    "IngressTenantError",
    "TenantResolver",
    "TenantProvisioner",
]
