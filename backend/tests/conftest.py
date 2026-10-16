"""共享测试常量与 fixture。"""

import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.persistence import models  # noqa: F401  # 导入即注册 8 张表
from app.persistence.database import Base

# 4.2 冻结的 8 张核心表（顺序无意义，集合比较）
CORE_TABLES = (
    "users",
    "identities",
    "channels",
    "agents",
    "connections",
    "sessions",
    "interactions",
    "activities",
)


@pytest.fixture()
def db_engine():
    """内存 SQLite + StaticPool（单连接共享），已建 8 张表。

    PRAGMA foreign_keys=ON 由 database.py 的 connect 监听器自动应用。
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture()
def db(db_engine):
    """绑定到内存库的 Session。"""
    with Session(db_engine) as session:
        yield session


@pytest.fixture()
def isolated_db_engine(tmp_path):
    """**文件型** SQLite（一个 UoW = 一条连接 = 一个隔离事务）。

    适用面 = 把写者跑到**后台线程**、测试线程同时读 DB 的测试
    （目前只有 WeComBot transport：`rx` 收帧 / `exec` 跑 Worker 与持久化）。

    为什么不直接用上面的 `db_engine`（`:memory:` + `StaticPool`）：StaticPool 只维护
    **一条** DBAPI 连接，`check_same_thread=False` 又允许所有线程共用它；于是在
    SQLite 眼里，「不同线程」其实是**同一个连接上的同一个事务**——

        - 读者能读到写者**未提交**的数据 → 脏读 → **假绿**（poll 见 succeeded，
          权威读数却是 processing）；
        - 读者 `with uow()` 退出时的 `session.close()` 会 ROLLBACK，
          连带丢弃写者尚未提交的语句 → **假红**（15s 内连终态都没有）。

    生产 / PostgreSQL / 文件型 SQLite 都是「一个 UoW = 一条连接 = 一个隔离事务」，
    因此**测试数据库的并发事务语义不得弱于生产数据库**——这是 3B-3.3 WIP Closure
    记录的测试基础设施约束，详见证 `docs/architecture/`。

    刻意不用 WAL / 不放大任何等待预算：需要的是**事务隔离**，不是降低冲突概率。
    `timeout` 只是 SQLite 的锁等待（等同连接池排队），不影响任何测试断言语义。
    """
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'acp.db').as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


# ===========================================================================
# B3.3/B3.3.1 —— OSS 导出感知测试边界（函数级收集期跳过）
#
# 契约与分析器见 tests/oss_export_boundary.py（唯一事实源）。语义：
#   - source（完整源仓库）：不跳过任何项——全部测试照常执行；
#   - oss_export（有效导出树，正证据 = OSS_EXPORT_MANIFEST.txt 且无 frontend/）：
#     对每个测试文件做 AST frontend 消费分析，**仅消费 frontend 的测试函数**
#     在收集期跳过（理由显式可见）；同一模块内的 backend 断言继续执行；
#   - unknown（部分检出/矛盾树）：**不跳过**——消费 frontend 的测试照常收集
#     执行，在缺失文件上显式失败（保守契约：绝不静默跳过检查）。
# 函数级标记 oss_export_boundary.oss_ui_required() 保留为冗余防线（语义一致）。
# 分析器异常时保守处理：不跳过（让缺失文件显式失败）并告警。
# ===========================================================================


def pytest_collection_modifyitems(config, items):  # noqa: ANN001, ANN201
    import logging
    from pathlib import Path

    from tests.oss_export_boundary import OSS_EXPORT, consuming_tests_for, repo_provenance

    if repo_provenance() != OSS_EXPORT:
        return
    skip_ui = pytest.mark.skip(
        reason=(
            "A1: 消费 frontend/（Cloud Web Console，初始 OSS 导出排除）；"
            "仅完整源仓库执行"
        )
    )
    consuming_cache: dict[str, frozenset[str]] = {}
    for item in items:
        fspath = str(item.fspath)
        if fspath not in consuming_cache:
            try:
                consuming_cache[fspath] = consuming_tests_for(Path(fspath))
            except Exception as exc:  # 分析失败 = 保守放行（缺失文件会显式失败）
                logging.warning(
                    "oss_export_boundary: frontend 消费分析失败（%s）——该文件"
                    "的测试将照常执行：%s", fspath, exc,
                )
                consuming_cache[fspath] = frozenset()
        func = getattr(item, "function", None)
        if func is not None and func.__name__ in consuming_cache[fspath]:
            item.add_marker(skip_ui)
