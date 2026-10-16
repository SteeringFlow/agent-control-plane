"""Step 14.2.2 —— Fresh SQLite Schema Init（一次性 schema 初始化）。

只验证一件事：**DB 文件不存在时，启动期的一次性初始化能让它获得当前 schema**，
且不越界。

验收对应：
- G2.1 全新 SQLite 文件 → 8 张核心表全部存在；且**不多建**任何业务表
      （4.2 冻结的领域 schema 不因初始化而改变）；
- G2.2 初始化后 Driver 的 `find_pending` 不再撞 "no such table"
      （本文件用真实 phase API 直接调用 `find_pending` 证明表已可用）；
- G2.5 回归由 `pytest -q` 全量保证（本文件不重复覆盖既有功能）。

边界（14.2.2 冻结）：
- ✅ `Base.metadata.create_all`（不是 Alembic / migration framework）；
- ❌ 不新增表 / 字段 / 领域 schema；不改 Runtime / Worker / Driver / Domain；
- ❌ 初始化职责不落入 Driver / Worker / Repository（本文件同时守卫这条分工）。
"""

from pathlib import Path

import pytest
from sqlalchemy import inspect

from app.persistence.database import create_database_engine, init_database

# 4.2 冻结的 8 张核心业务表 + Cloud-0.1.1-B 的 tenants root catalog
# + Cloud-0.1.1-C 的 2 张 Browser Auth 表
# + Implementation Package 2 / Step 1 的 connection_conversations（授权状态存储）。
# （8 张业务表仍是 4.2 冻结集合，不因 tenant / auth / authorization 而增减。）
EXPECTED_TABLES = {
    "users",
    "identities",
    "channels",
    "agents",
    "connections",
    "sessions",
    "interactions",
    "activities",
    "tenants",
    "user_credentials",
    "browser_sessions",
    "connection_conversations",
    # M6.6-C3 / ADR-12 迁移 0008：Bootstrap 窗口存储（不是授权状态）。
    "bootstrap_arms",
}


@pytest.fixture()
def fresh_db():
    """一个**尚不存在**的全新 SQLite 文件（关键：模拟 DB file = absent）。

    刻意不用 `tmp_path`——本环境的系统 TEMP 目录对 pytest 的 basetemp 有权限
    限制；这里改用测试目录下的自建临时目录（*.db 已被 .gitignore 忽略）。
    """
    import shutil
    import tempfile

    workdir = Path(tempfile.mkdtemp(prefix="_14_2_2_", dir=Path(__file__).parent))
    db_path = workdir / "fresh_14_2_2.db"
    assert not db_path.exists(), "测试前提：DB 文件必须不存在"
    engine = create_database_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        yield engine, db_path
    finally:
        engine.dispose()
        shutil.rmtree(workdir, ignore_errors=True)


def _table_names(engine) -> set[str]:
    return set(inspect(engine).get_table_names())


# ---------------------------------------------------------------- G2.1 全新库建表


def test_fresh_db_gets_all_core_tables(fresh_db):
    """G2.1：全新 SQLite 文件 → 8 张核心表全部存在。"""
    engine, db_path = fresh_db

    init_database(engine)

    assert db_path.exists(), "初始化应创建出 SQLite 文件"
    assert EXPECTED_TABLES <= _table_names(engine)


def test_fresh_db_has_no_extra_business_tables(fresh_db):
    """G2.1（反向）：初始化**没有**偷偷增加业务表。"""
    engine, _ = fresh_db

    init_database(engine)

    created = _table_names(engine)
    # SQLite 可能带 sqlite_sequence（自增计数器），不属于业务表
    business_tables = created - {"sqlite_sequence"}
    assert business_tables == EXPECTED_TABLES, (
        f"业务表集合必须与 4.2 冻结一致；多出={business_tables - EXPECTED_TABLES}，"
        f"缺少={EXPECTED_TABLES - business_tables}"
    )


def test_init_is_idempotent(fresh_db):
    """初始化幂等：重复调用安全（create_all checkfirst=True），不改写既有数据。"""
    engine, _ = fresh_db

    init_database(engine)
    init_database(engine)

    assert EXPECTED_TABLES <= _table_names(engine)

    # 幂等的更强证据：初始化后写入一行，再次初始化后该行仍在
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO channels (name, type, status, config) "
                "VALUES ('keep-me', 'wecom', 'active', '{}')"
            )
        )
    init_database(engine)
    with engine.connect() as conn:
        kept = conn.execute(
            text("SELECT name FROM channels WHERE name = 'keep-me'")
        ).scalar()
    assert kept == "keep-me"


# ---------------------------------------------------------------- G2.2 初始化后表可用


def test_phase_api_find_pending_works_after_init(fresh_db):
    """G2.2：初始化后 `find_pending` 正常执行（不再 no such table）。

    直接用真实 phase API（PersistingCoordinator）走一次 DISCOVER——
    这是 Driver 会调用的同入口；此处只证明 schema 已就位，不改 Driver 语义。
    """
    engine, _ = fresh_db
    init_database(engine)

    from sqlalchemy.orm import sessionmaker

    from app.application.interaction_recording import PersistingCoordinator
    from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def uow_factory():
        return SqlAlchemyUnitOfWork(session_factory=factory)

    phases = PersistingCoordinator(inner=None, uow_factory=uow_factory)  # type: ignore[arg-type]

    assert phases.find_pending(limit=10) == [], "空库应返回空候选，而非抛 schema 错误"


# ---------------------------------------------------------------- 分工守卫


def test_init_does_not_live_in_runtime_or_worker():
    """分工守卫：schema 初始化**不**由 Driver / Worker / Runtime 承担。

    它们的代码面不应出现 create_all / DDL 调用——初始化是 deployment concern。
    """
    from pathlib import Path

    backend_dir = Path(__file__).resolve().parents[1]
    guarded = [
        backend_dir / "app" / "application" / "driver.py",
        backend_dir / "app" / "application" / "worker.py",
        backend_dir / "app" / "application" / "runtime.py",
        backend_dir / "app" / "application" / "coordinator.py",
        backend_dir / "app" / "application" / "interaction_recording.py",
    ]
    for path in guarded:
        source = path.read_text(encoding="utf-8")
        assert "create_all" not in source, (
            f"{path.name} 不应承担 schema 初始化（deployment concern）"
        )
