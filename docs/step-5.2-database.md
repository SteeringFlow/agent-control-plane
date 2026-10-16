# Step 5.2 —— Database Initialization & SQLAlchemy Setup

一句话目标：**让 Control Plane 拥有一个可靠、可测试、但仍然极简的持久化基础设施。**

本阶段**不创建 8 张业务表**（那是 5.3）。链路严格控制在：

```
FastAPI
   │
   └── Config (core/config.py)
         │  DATABASE_URL
         ▼
     persistence/database.py
         │  Engine / SessionLocal / Base / get_db
         ▼
       SQLite
         ▼
   Session / Engine
```

而不是 `FastAPI → SQLAlchemy → User → Agent → Repository → Runtime`——
后者是 Vertical Slice 以后的事。

---

## 1. 新增内容（仅一个模块）

```
backend/app/persistence/
├── database.py    Engine / SessionLocal / Base / get_db（本步全部内容）
├── models/        空壳（5.3 落地 8 张表）
├── mappers/       空壳
└── repositories/  空壳
```

`persistence/__init__.py` 对外再导出 `Base / SessionLocal / engine / get_db`。

## 2. Database Configuration（5.2.2）

不再另设计一套 DatabaseConfig——`Settings` 已经是配置入口：

```
config.py ──DATABASE_URL──> database.py
```

默认值仍为 `sqlite:///./agent_control_plane.db`。

## 3. Engine（5.2.3）

- 进程级**唯一** engine，模块加载时创建；
- SQLite 需要 `connect_args={"check_same_thread": False}`（FastAPI 依赖在线程池执行），
  该参数**只对 sqlite 生效**（`_engine_kwargs` 按 URL 前缀判断）；
- **不建 Database Factory**：SQLite 就是 MVP 的真实数据库。
  未来换 PostgreSQL 只需让 `DATABASE_URL` 变化，工厂抽象是负资产。

## 4. Session Factory（5.2.4）

`SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)`。

职责只有「创建 Session」。它不做 Repository、不做事务业务逻辑、
不做 Routing、不做 Application Service、不做 Dependency Policy。

`expire_on_commit=False` 是面向 web 请求的默认：commit 后对象在构造响应时仍可读。

## 5. FastAPI Dependency（5.2.5）

`get_db()` 是 FastAPI/Infrastructure 层的依赖：

```
API  ──get_db()──> SQLAlchemy Session   ✅ 允许
Application ──get_db()                  ❌ 禁止（Application 不认识 FastAPI / SQLAlchemy）
```

未来方向：API 拿到 Session 后，把它注入 Application 的仓储端口，
Application 只面对接口。已由 AST 防线锁死（见 §7）。

## 6. Base / Metadata / Migration（5.2.6 / 5.2.7 / 5.2.9）

- `Base(DeclarativeBase)` 已建立，**当前没有任何业务 Model**——这是正常的，
  它只为 5.3 的统一注册点 `Base.metadata` 做准备；
- **暂不引入 Alembic**：现在没有任何 schema 可迁移，提前引入只会得到
  env.py / versions/ / 配置等空转基础设施。等 5.3 的 8 张表真正落地后再决定；
- 也不提供 `create_all()`：没有模型可建。

## 7. Import Boundary（5.2.12，机器化）

`tests/test_import_boundaries.py` 的禁令表更新为：

| 层 | 对 SQLAlchemy | 其它禁令 |
| --- | --- | --- |
| `domain` | ❌ | fastapi / starlette / app.schemas / app.api / application / persistence / adapters |
| `application` | ❌ | fastapi / starlette / app.api / persistence / adapters |
| `repositories`（端口） | ❌ | fastapi / starlette / app.api / schemas / application / persistence / adapters |
| `api` | ❌（只许经 `app.persistence.database.get_db`） | 直接 import `persistence.models / mappers / repositories` |
| `persistence` | ✅ 唯一允许 | — |

即：**Persistence 可以依赖 Repository Interface（实现端口），
Repository Interface 绝不依赖 Persistence。**

## 8. database.py 职责文档（5.2.8）

已写进模块 docstring，便于日后一眼检查：

可以：创建 Engine / 创建 Session Factory / 定义 ORM Base / 提供 DB Session dependency。

不可以：定义业务实体 / 实现 Repository / 实现 Application Service /
执行 Routing / 调用 Agent / 调用 Channel Adapter / 决定业务事务。

## 9. 验收结果（Tests A–E）

`backend/tests/test_database.py`，`python -m pytest -q` **27 passed**：

| Test | 内容 | 结果 |
| --- | --- | --- |
| A | `DATABASE_URL → Engine` 真实创建（含显式 URL 分支） | ✅ |
| B | `SessionLocal()` 能创建 Session | ✅ |
| C | `SELECT 1` 真实连通（Engine 路径 + Session 路径 + 真实 SQLite 文件路径） | ✅ |
| D | `Base.metadata` 存在；users/identities/channels/agents/connections/sessions/interactions/activities **均不存在**（防漂移） | ✅ |
| E | Engine 确实使用配置值；SQLite 线程参数只对 sqlite 生效 | ✅ |
| 附加 | `get_db` yield→close 把连接归还连接池（不泄漏） | ✅ |

冒烟：`uvicorn app.main:app` 启动后 `GET /health` 仍为
`200 {"status":"ok"}`，且**启动过程不创建任何 .db 文件**（engine 惰性建连）。

### 实现期踩过的两个坑（留档）

1. **SQLite 相对路径在 engine 创建时按当时 CWD 解析**，运行中 `chdir` 无法重定向——
   测试因此用 monkeypatch 替换 `SessionLocal` 绑定临时目录探针 engine，
   而不是 chdir 仓库目录。
2. 本机 pytest 的 `tmp_path` fixture 临时根目录有 PermissionError（环境问题），
   测试改用 `tempfile.mkdtemp()` 规避。

## 10. 本步刻意不做

- 不创建任何业务 ORM Model / 业务表（5.3）
- 不实现任何 Repository（5.4）
- 不引入 Alembic / Migration
- 不做连接池调优、读写分离、主从、Schema 多租户（无真实需求）
- 不在 health 检查里探测数据库（health 仍只证明进程存活）

## 11. 下一步

```
5.3  SQLAlchemy ──> 8 张核心表（persistence/models 落地，同时决定是否引入 Alembic）
5.4  Repository ──> 真实数据库读写（app/repositories 端口 ↔ persistence/repositories 实现）
```
