"""TenantProvisioningService（Cloud-0.1.2.1）—— 实现 app.core.tenant.TenantProvisioner 端口。

职责（唯一）：Cloud Sign Up 时为用户创建其归属租户（Tenant = 用户 Workspace 的
实体化）。这是 `tenants` 表**唯一**的写入入口，运行在 bootstrap 分支
（`app.tenant_id` 未设 → 命中 tenants 的 bootstrap RLS 策略）。

产品化假设（0.1.2.1 要验证的）：**Cloud 用户注册后无需额外「创建 Workspace」步骤**——
一次 `provision` 即产生该用户的租户，用户随即进入自己的 Workspace。

边界（写死，与 Cloud-0.1.2 Gate 一致）：

- 不引入 `workspaces` / `organizations` / `teams` / `memberships` 任何表或域实体；
  `Tenant` 即 ownership / isolation root，`Workspace` 只是其 Cloud UX 表达；
- 不引入 `WorkspaceProvisioner` / `OrganizationProvisioner` 第二套归属创建器——
  本类就是 `TenantProvisioner` 的唯一实现；
- bootstrap 访问用 `uow_factory(None)`（不设 tenant 上下文）；
- 返回 `TenantScope`（Cloud-0.1.1-B 的唯一 tenant 原语），供后续 tenant-scoped UoW 复用。

依赖边界（AST 守护）：不 import SQLAlchemy / FastAPI / api / schemas / adapters /
app.persistence；只依赖 app.core（端口）与 app.repositories（端口）。
"""

from __future__ import annotations

from collections.abc import Callable

from app.core.tenant import TenantScope
from app.repositories.unit_of_work import UnitOfWork


class TenantProvisioningService:
    """`TenantProvisioner` 端口实现（bootstrap 建租户）。"""

    def __init__(
        self, uow_factory: Callable[[int | None], UnitOfWork]
    ) -> None:
        self._uow_factory = uow_factory

    def provision(self, name: str, *, config: dict | None = None) -> TenantScope:
        """创建一行租户（root catalog），返回其 `TenantScope`。

        `name` 是租户 / Workspace 的显示名（Cloud Sign Up 时由注册输入派生，
        无需用户额外填写「Workspace Name」）。
        """
        with self._uow_factory(None) as uow:
            tenant_id = uow.tenants.create(name=name, config=config)
            uow.commit()
        return TenantScope(tenant_id=tenant_id)
