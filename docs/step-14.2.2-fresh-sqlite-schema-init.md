# Step 14.2.2 — Fresh SQLite Schema Init（关闭 B2）

> 目标：全新环境启动后，SQLite 必须存在完整当前 schema。
> **只解决「新数据库有没有表」，不解决「数据库持久化到哪里」——Volume 是 14.2.3。**

```
Fresh container → SQLite DB → ❌ no such table: interactions
                      ↓（本步）
Fresh container → SQLite DB → 一次性 schema 初始化 → 8 张核心表 → Driver 正常
```

## 1. 实现（改动面严格限定）

| 文件 | 改动 | 归属 |
| --- | --- | --- |
| `backend/app/persistence/database.py` | 新增 `init_database(target_engine=None)`；更新模块 docstring（取消「不提供 create_all/init_db」的旧表述） | `persistence/**` ✅ |
| `backend/app/main.py` | **仅**在 lifespan 内、`driver.start()` 之前调用一次 `init_database()`（import + 3 行） | 启动装配 ✅ |
| `backend/tests/test_step14_2_2_fresh_sqlite_schema_init.py` | 新增 5 项验收 | `tests/**` ✅ |
| `backend/tests/test_persistence_smoke.py` / `test_step5_7_closure.py` | 两处旧守卫按新裁决更新 | `tests/**` ✅ |

**语义定位（按建议实现）**：

```
Fresh DB → initialize schema → start normal application
```

- `init_database()` 用 `Base.metadata.create_all`（**不是** Alembic / migration framework，14.1 拍板）；
- 延迟 `import app.persistence.models`（该包 import 本模块的 `Base`，模块级 import 会循环）完成 8 张表的 metadata 注册；
- `create_all` 默认 `checkfirst=True` → **幂等**，对已有库重复调用安全；
- **明确不**让 Driver「发现 no table 就建表」，也**不**让 Worker / Runtime / Repository 承担建表责任——由测试 `test_init_does_not_live_in_runtime_or_worker` 机器化守卫。

## 2. 验收实测（真实全新环境，未使用既有 `backend/agent_control_plane.db`）

验证方式：`docker compose down` → 重建镜像 → `up -d`。容器可写层**不含任何 DB 文件**，
等价「DB file = absent」的全新环境。

| # | 验收 | 结果 |
| --- | --- | --- |
| **G2.1** | 全新 SQLite 文件创建出 `users / identities / channels / agents / connections / sessions / interactions / activities` **全部 8 张**；容器内 `inspect(engine).get_table_names()` 实际输出 8 项，**无多余业务表** | ✅ |
| **G2.2** | 日志中 `no such table` 出现次数 = **0**，`OperationalError` = **0**；`find_pending` 正常执行（启动日志 `Application startup complete.` 后无异常） | ✅ |
| **G2.3** | Management 读 `GET /api/agents` → 200 空列表；最小合法写入 `POST /api/agents` → **201 Created**（id=1）；再读 → 200 含该记录 | ✅ |
| **G2.4** | `GET /api/interactions?limit=5` → **200** `{"items":[],"total":0,"limit":5}`（不再 500） | ✅ |
| **G2.5** | `pytest -q` → **1467 passed**（2:04），零回归 | ✅ |

## 3. 关于两处旧守卫测试的裁决变更

`test_app_source_never_auto_creates_schema`（5.6）与
`test_a7_..._create_all_only_in_test_harness...`（5.7）原断言
「应用源码不得出现 create_all」——该断言编码的是 5.2/5.7 时代「没有模型可建」的
前提，**已被 14.1 拍板推翻**（MVP 用 `create_all` 做一次性初始化）。

两处已替换为**更精确**的不变量（而非删除守卫）：

- `create_all` **唯一合法位置** = `app/persistence/database.py`（`init_database`）；
- Driver / Worker / Runtime / Coordinator / Repository 中仍**禁止**出现 `create_all`；
- Alembic / migrations / alembic 依赖仍保持 deferred（5.7 原断言的其余部分原样保留）。

## 4. 边界核对（禁止清单）

❌ Alembic / migration framework　❌ PostgreSQL　❌ 新表　❌ 新字段　❌ Runtime 修改
❌ Worker 修改　❌ Driver 修改　❌ Domain 修改　❌ Interaction 状态模型修改
❌ retry/replay　❌ volume　❌ healthcheck　❌ Dockerfile/Compose 加固
——**全部为零**。表集合与 4.2 冻结的 8 张核心表一致（ORM 模型文件数 ≠ 表名数，
按实际 `Base.metadata` 初始化）。

## 5. 遗留（明确属于后续步）

- **持久化**：当前 SQLite 位于容器可写层，**重启容器数据不保留** → **14.2.3**（named volume）；
- `--reload` / 依赖锁定 / 反向代理 / healthcheck → 14.2.3；
- B1（容器布局）已在 **14.2.1** 关闭并在本步复验（`/`、`/interactions.html` 仍 200）。
