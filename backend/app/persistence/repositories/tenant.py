"""SqlAlchemyTenantRepository（Adapter，实现 app/repositories/tenant.py 端口）。

Cloud-0.1.2.1 —— 「注册即建租户」的唯一写入点（`tenants` root catalog）。

边界（刻意写死）：

- 构造只接收 `session`（**无 tenant_id**）：`tenants` 是 root catalog，不套用
  「按 tenant_id 过滤」的普通 per-tenant RLS；
- 写入发生在 **bootstrap** 分支（register/signup 时 `app.tenant_id` 未设）——
  与 Cloud-0.1.1-B 的 `TenantProvisioner` 契约一致；
- 本层只 flush（拿自增 id），**不 commit**——事务边界属 UoW；
- ORM 行 → tenant_id 的翻译内联在此（`tenants` 不是 Domain Entity，无需 mapper）。
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.persistence.models.tenant import TenantModel


class SqlAlchemyTenantRepository:
    """实现 TenantRepository 协议（create 写入 + list_active_ids bootstrap 只读投影）。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, *, name: str, config: dict | None = None) -> int:
        row = TenantModel(name=name, status="active", config=config)
        self._session.add(row)
        self._session.flush()
        return row.id

    def list_active_ids(self) -> list[int]:
        """活跃租户 id（Cloud-0.1.3.3）：bootstrap 只读，无 tenant 过滤
        （tenants 是 root catalog，B §9 特殊策略；调用方 = 组合根 fan-out 枚举）。"""
        rows = self._session.execute(
            select(TenantModel.id).where(TenantModel.status == "active")
        ).scalars().all()
        return list(rows)
