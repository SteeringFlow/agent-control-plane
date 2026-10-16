"""Alembic environment（Cloud-0.1.1-A）。

唯一职责：把 `app.persistence` 的 ORM metadata 暴露给 Alembic，并解析目标连接
URL。刻意不做：

- 不 import / 不触碰 Runtime、Application、Adapter（schema 与执行语义无关）；
- 不在本文件创建或初始化 schema（建表是 migration 的职责）；
- 不引入任何 tenant / RLS / auth 概念（留给后续 Step）。

URL 解析优先级：环境变量 ``DATABASE_URL`` > ``alembic.ini`` 的
``sqlalchemy.url``。Cloud profile 经环境变量指向 PostgreSQL；本地可显式覆盖。

`target_metadata = Base.metadata`：`import app.persistence.models` 会完成 8 张
核心表的注册（与 `database.init_database` 同一注册路径），保证 baseline migration
真实反映当前 8 表 schema，而不是引入 incidental metadata 差异。
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# 导入即注册 8 张核心表到 Base.metadata（与运行时同一条注册路径）。
import app.persistence.models  # noqa: F401
from app.persistence.database import Base

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers=False：迁移运行**不得**全局禁用既有 logger。
    # 默认 True 会连带禁用应用 logger（如 app.application.driver），破坏同一进程
    # 内的日志捕获（pytest caplog）——这是把 migration 与应用共进程运行时的标准陷阱。
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    """解析目标连接 URL（环境变量优先，其次 alembic.ini）。"""
    url = os.environ.get("DATABASE_URL") or config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError(
            "Alembic 需要目标数据库 URL：请设置环境变量 DATABASE_URL "
            "（或 alembic.ini 的 sqlalchemy.url）。")
    return url


def run_migrations_offline() -> None:
    """离线模式：只渲染 SQL，不建立连接。"""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：建立真实连接并执行迁移。"""
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
