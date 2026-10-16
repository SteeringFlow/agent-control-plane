"""持久化基础设施（Step 5.2）：Engine / SessionLocal / Base / get_db。

本模块是全工程唯一组装 SQLAlchemy 的入口。职责边界（5.2.8，刻意写死）：

可以：
- 创建 Engine（唯一实例，来自 core.config.DATABASE_URL）
- 创建 Session Factory（SessionLocal，只负责"创建 Session"这一件事）
- 定义 ORM Base（DeclarativeBase 声明基类，5.2 无任何业务模型）
- 提供 FastAPI 的 DB Session dependency（get_db）

不可以：
- 定义业务实体（User / Agent / Channel ... 属 5.3 的 persistence/models）
- 实现 Repository（实现属于 persistence/repositories，接口属于 app/repositories）
- 实现 Application Service
- 执行 Routing
- 调用 Agent / Channel Adapter
- 决定业务事务边界（事务属于未来的 Application 层）

同样刻意不做的：
- 不引入 Alembic / 任何 migration framework——14.1 已拍板：MVP 用
  `create_all` 做一次性初始化，迁移工具链是「真实部署 Step」才评估的事，
  不提前增加基础设施。
- 不建 Database Factory 抽象——SQLite 就是 MVP 的真实数据库，
  将来换 PostgreSQL 只需改 DATABASE_URL，不需要多一层工厂。

**Step 14.2.2 补充**：本模块提供 `init_database()`——**一次性 schema 初始化**。
它是 **deployment / startup concern**，由应用启动装配（api/main.py 的 lifespan）
在「进入正常运行之前」调用一次；**绝不是** Driver / Worker / Repository / Runtime
的职责（它们只读写既有表，绝不「发现没表就建表」）。详见该函数 docstring。

依赖方向：persistence 是唯一允许 import SQLAlchemy 的业务相关层；
api 只允许经由 get_db 获取 Session，不允许直接 import SQLAlchemy
（由 tests/test_import_boundaries.py 机器化守护）。
"""

from collections.abc import Iterator
import sqlite3

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings


class Base(DeclarativeBase):
    """ORM 声明基类。

    Step 5.3 起：8 张核心表 ORM（persistence/models/）统一注册到
    Base.metadata；仍不在此定义任何业务实体。
    """


@event.listens_for(Engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, connection_record) -> None:
    """SQLite 默认不启用外键约束（PRAGMA foreign_keys 默认 OFF）。

    外键完整性是 schema 的一部分，必须在连接级开启；否则 5.3 的
    FK 约束在 SQLite 上形同虚设。对所有 sqlite 连接生效，
    其它后端（PostgreSQL 外键默认强制）不受影响。
    """
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def _engine_kwargs(database_url: str) -> dict:
    """按 URL 形态补充 Engine 参数。

    SQLite 有"同线程访问"约束，而 FastAPI 的依赖在线程池中执行，
    因此开发环境需要 check_same_thread=False。
    只对 sqlite 生效，其它后端（未来 PostgreSQL）不受影响。
    """
    kwargs: dict = {}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return kwargs


def create_database_engine(database_url: str | None = None) -> Engine:
    """创建 Engine。默认使用配置入口（core.config）里的 DATABASE_URL。"""
    url = database_url or get_settings().DATABASE_URL
    return create_engine(url, **_engine_kwargs(url))


# 进程级唯一 Engine 与 Session Factory。
# 注意：engine 惰性建连——import 本模块不会创建 SQLite 文件或发起连接。
engine: Engine = create_database_engine()

SessionLocal = sessionmaker(
    bind=engine,
    class_=Session,
    expire_on_commit=False,  # commit 后对象仍可用：web 请求内提交后还要构造响应
)


def get_db() -> Iterator[Session]:
    """FastAPI DB dependency。

    归属：FastAPI / Infrastructure——它让 HTTP 层拿到 Session，
    但不允许 Application 层反向 import 本模块（Application 不认识 FastAPI，
    也不认识 SQLAlchemy，未来由 API 把 Session 注入 Application 的端口）。
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_database(target_engine: Engine | None = None) -> None:
    """一次性 schema 初始化（Step 14.2.2）：让**全新** SQLite 文件获得当前 schema。

    定位：**deployment / startup concern**——由应用启动装配（api/main.py 的
    lifespan）在「进入正常运行之前」调用一次，顺序是

        Fresh DB → initialize schema → start normal application

    刻意**不**让以下任何一方承担建表责任（14.2.2 冻结的分工）：

    - ❌ Driver：它只 DISCOVER / TRIGGER / RECOVER，不解释 "no such table"；
    - ❌ Worker / Runtime / Runtime Coordinator：执行路径，不碰 DDL；
    - ❌ Repository / Application Service：它们假设 schema 已存在。

    语义细节：

    - 用 `Base.metadata.create_all`（**不是** Alembic / migration framework，
      14.1 已拍板）；
    - `create_all` 默认 `checkfirst=True`，因此**幂等**——已存在的表不会被重建
      或改写，对已有库重复调用是安全的；
    - 只创建 8 张核心表（4.2 冻结的 Persistence Model），**不新增表、
      不新增字段、不改领域 schema**；
    - 延迟 import `app.persistence.models`——该包 import 本模块的 `Base`，
      模块级 import 会形成循环，故在函数内导入以完成 metadata 注册。

    **Cloud-0.1.1-A 边界（A8）**：`create_all` 仅作为 **local SQLite** 的
    development convenience。非 SQLite 后端（Cloud PostgreSQL）的 schema 由
    **Alembic** 管理（`alembic upgrade head`），本函数对其**直接返回、不建表**
    ——防止 PG 上出现「create_all 建表 + Alembic 基线」双轨冲突。
    """
    import app.persistence.models  # noqa: F401  延迟导入：注册 8 张表到 Base.metadata

    bind = target_engine if target_engine is not None else engine
    if bind.dialect.name != "sqlite":
        return  # Cloud PostgreSQL：schema 归 Alembic（A8）
    Base.metadata.create_all(bind=bind)
