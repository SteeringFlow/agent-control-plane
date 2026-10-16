"""TenantRepository 端口（Cloud-0.1.2.1 —— TenantProvisioner 的持久化端口）。

只描述 `TenantProvisioner`（app.core.tenant）声明的最小持久化能力：在 root catalog
`tenants` 中创建一行。不含实现，不 import SQLAlchemy / persistence。

边界（写死，与 Cloud-0.1.2 Gate 一致）：

- 只提供 `create`：`tenants` 表**唯一**的写入路径归 `TenantProvisioner`
  （bootstrap RLS 分支，`app.tenant_id` 未设）；
- `list_active_ids`（Cloud-0.1.3.3）：活跃租户 id 的 bootstrap 只读投影——
  供组合根 fan-out 枚举 per-tenant Runtime（0.1.3.2 Gate Q1：tenant 枚举归
  组合根关注点）。它发生在任何 per-tenant 作用域**之前**（bootstrap，同
  0.1.3.1 的 Channel 路由投影纪律）；返回 id 列表，不返回 Domain Entity；
- 返回 tenant_id（int），**不返回 Domain Entity**——`tenants` 是 isolation /
  ownership root，不是业务域实体（Domain 全树冻结；无 Workspace / Organization 实体）；
- 本端口不承担 tenant 解析（那是 `TenantResolver`），也不承担隔离（那是 UoW + RLS）。

实现见 app/persistence/repositories/tenant.py（SqlAlchemyTenantRepository）。
"""

from typing import Protocol, runtime_checkable


@runtime_checkable
class TenantRepository(Protocol):
    """`tenants` root catalog 的最小写入端口。"""

    def create(self, *, name: str, config: dict | None = None) -> int:
        """插入一行租户，返回其自增 id（tenant_id）。"""
        ...

    def list_active_ids(self) -> list[int]:
        """活跃租户 id 的 bootstrap 只读投影（Cloud-0.1.3.3 fan-out 枚举源）。"""
        ...
