"""SQLite → PostgreSQL 一次性数据导入（Cloud-0.1.1-B §11）。

把既有 OSS SQLite 的 8 张业务表导入 Cloud PostgreSQL，并统一归到 bootstrap
Tenant #1（`tenant_id = 1`）——因为 Cloud schema 的 `tenant_id` 为 NOT NULL
（由 migration 0002 建立），导入时必须显式赋值。

用法（示例）：

    python -m tools.sqlite_to_pg_import \
        --sqlite sqlite:///./data/agent_control_plane.db \
        --pg postgresql+psycopg://user:pass@localhost:5432/acp

前置：目标 PostgreSQL 已 `alembic upgrade head`（含 0002，tenants#1 已存在）。

纪律：本工具是 **deployment / 迁移一次性产物**，不是运行时组件——不 import
Application / Runtime / Adapter；只做「按 FK 顺序读 SQLite → 写 PG」。
"""

from __future__ import annotations

import argparse

from sqlalchemy import create_engine, insert, select

# 按 FK 依赖序（父表在前）
CORE_TABLES = (
    "tenants",
    "users", "channels", "agents",
    "identities", "connections",
    "sessions", "interactions", "activities",
)

# 需要显式赋值 tenant_id 的业务表（tenants 自身没有 tenant_id 维度）
TENANT_SCOPED_TABLES = (
    "users", "identities", "channels", "agents",
    "connections", "sessions", "interactions", "activities",
)

BOOTSTRAP_TENANT_ID = 1


def _table(engine, name):
    from app.persistence import models  # noqa: F401  注册 metadata
    from app.persistence.database import Base

    return Base.metadata.tables[name]


def import_all(sqlite_url: str, pg_url: str) -> dict[str, int]:
    """把 SQLite 8 张业务表导入 PG（tenant_id=1）。返回各表导入行数。"""
    from app.persistence import models  # noqa: F401

    src = create_engine(sqlite_url)
    dst = create_engine(pg_url)
    counts: dict[str, int] = {}

    with src.connect() as s_conn, dst.begin() as d_conn:
        # tenants：确保 bootstrap #1 存在（schema 已由 0002 建好）
        tenants = _table(dst, "tenants")
        existing = d_conn.execute(
            select(tenants.c.id).where(tenants.c.id == BOOTSTRAP_TENANT_ID)
        ).scalar()
        if existing is None:
            d_conn.execute(insert(tenants).values(
                id=BOOTSTRAP_TENANT_ID, name="default", status="active"))

        for name in TENANT_SCOPED_TABLES:
            table = _table(dst, name)
            rows = s_conn.execute(select(_table(src, name))).mappings().all()
            payload = []
            for row in rows:
                data = dict(row)
                data["tenant_id"] = BOOTSTRAP_TENANT_ID   # 统一归到 Tenant #1
                payload.append(data)
            if payload:
                d_conn.execute(insert(table), payload)
            counts[name] = len(payload)

    src.dispose()
    dst.dispose()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="SQLite → PostgreSQL 一次性导入（tenant_id=1）")
    parser.add_argument("--sqlite", required=True, help="源 SQLite URL")
    parser.add_argument("--pg", required=True, help="目标 PostgreSQL URL（已 alembic upgrade head）")
    args = parser.parse_args()

    counts = import_all(args.sqlite, args.pg)
    for name, n in counts.items():
        print(f"{name}: {n}")


if __name__ == "__main__":
    main()
