"""Step 5.2 验收测试（事实测试，不看代码量）：

Test A  Engine 初始化（DATABASE_URL -> Engine）
Test B  SessionLocal 能创建 Session
Test C  SELECT 1 真实连通（Engine 与 Session 两条路径）
Test D  Base.metadata 已注册 8 张核心表（5.3 落地后翻转自 5.2 的'为空'断言）
Test E  Engine 确实使用配置值 + SQLite 线程参数只对 sqlite 生效

全部使用 sqlite:///:memory:，不在仓库里留下数据库文件。
"""

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session

from app.core.config import get_settings
from app.persistence.database import (
    Base,
    SessionLocal,
    _engine_kwargs,
    create_database_engine,
    get_db,
)


def test_a_engine_is_created_from_config() -> None:
    engine = create_database_engine()
    assert isinstance(engine, Engine)
    assert str(engine.url) == get_settings().DATABASE_URL


def test_a_engine_accepts_explicit_url() -> None:
    engine = create_database_engine("sqlite:///:memory:")
    assert engine.url.get_backend_name() == "sqlite"


def test_b_session_factory_creates_session() -> None:
    session = SessionLocal()
    try:
        assert isinstance(session, Session)
    finally:
        session.close()


def test_c_engine_executes_select_1() -> None:
    engine = create_database_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT 1")).scalar() == 1


def test_c_session_executes_select_1() -> None:
    engine = create_database_engine("sqlite:///:memory:")
    session = Session(bind=engine)
    try:
        assert session.execute(text("SELECT 1")).scalar() == 1
    finally:
        session.close()


def test_d_metadata_contains_core_tables_plus_tenants() -> None:
    """Base.metadata 已注册 8 张核心业务表 + `tenants` root + Cloud-0.1.1-C 的 2 张 Browser Auth 表。"""
    from tests.conftest import CORE_TABLES

    assert isinstance(Base, type) and issubclass(Base, DeclarativeBase)
    assert set(Base.metadata.tables) == set(CORE_TABLES) | {
        "tenants", "user_credentials", "browser_sessions",
        # Implementation Package 2 / Step 1：授权状态存储（非 4.2 业务表）。
        "connection_conversations",
        # M6.6-C3 / ADR-12：Bootstrap 窗口存储（不是授权状态）。
        "bootstrap_arms"}


def test_e_engine_uses_configured_database_url() -> None:
    """配置入口 -> database engine 的接线必须真实生效。"""
    assert str(SessionLocal.kw["bind"].url) == get_settings().DATABASE_URL


def test_e_sqlite_gets_thread_safe_connect_args() -> None:
    kwargs = _engine_kwargs("sqlite:///./agent_control_plane.db")
    assert kwargs == {"connect_args": {"check_same_thread": False}}


def test_e_non_sqlite_has_no_sqlite_specific_kwargs() -> None:
    kwargs = _engine_kwargs("postgresql+psycopg://user:pass@localhost:5432/acp")
    assert kwargs == {}


def test_get_db_yields_and_closes_session(monkeypatch) -> None:
    """get_db 退出时必须把连接还给连接池（不泄漏连接）。

    Session 惰性建连：必须先执行一条语句，连接才真正被占用。
    模块 engine 的 SQLite 相对路径在 import 时就按当时 CWD 解析完毕，
    测试中 chdir 无法重定向——因此 monkeypatch 替换 SessionLocal，
    绑定一个指向系统临时目录的探针 engine，避免测试在仓库里留 .db 文件。
    顺带验证：SQLAlchemy -> Session -> 真实 SQLite 文件 的链路确实工作。
    """
    import tempfile
    from pathlib import Path

    import app.persistence.database as db_module
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    db_path = Path(tempfile.mkdtemp(prefix="acp-db-test-")) / "probe.db"
    probe_engine = create_engine(f"sqlite:///{db_path}")
    monkeypatch.setattr(
        db_module,
        "SessionLocal",
        sessionmaker(bind=probe_engine, class_=Session, expire_on_commit=False),
    )

    before = probe_engine.pool.checkedout()
    generator = db_module.get_db()
    session = next(generator)
    assert isinstance(session, Session)
    session.execute(text("SELECT 1"))
    assert probe_engine.pool.checkedout() == before + 1
    generator.close()  # 触发 finally: db.close()
    assert probe_engine.pool.checkedout() == before
    assert db_path.exists()  # 真实文件库已建立连接并写入文件头
