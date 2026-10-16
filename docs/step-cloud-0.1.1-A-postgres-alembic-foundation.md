# Step Cloud-0.1.1-A —— PostgreSQL + Alembic Persistence Foundation

技术基线：OSS V0.x RC `v0.1.0-rc.1` / HEAD `e6ac4e9`。Technical Line = CLOSED / FROZEN。

一句话目标：**把 Cloud 的持久化地基从「单文件 SQLite」升级到「PostgreSQL + Alembic」，
且不动 Runtime 契约、不引入任何多租户概念。**

本 Step **不引入**：tenant_id / tenants / RLS / Browser Auth / Webhook Tenant Routing /
Workspace UI / WorkBuddy / WeCom Cloud implementation / Redis / Kafka / Celery / Vault /
Kubernetes / OAuth Platform（全部留给后续 Step）。

---

## 1. A1 只读审计结论（PostgreSQL 兼容性）

| 层 | 兼容 | 依据 |
| --- | --- | --- |
| Engine 创建 | ✅ | `database.py` URL 驱动；sqlite 参数被 `startswith("sqlite")` 守卫 |
| FK 强制 | ✅ | `PRAGMA foreign_keys` 仅 sqlite（`isinstance` 守卫）；PG 原生强制 |
| ORM 类型 | ✅ | `String/JSON/DateTime/Boolean/FK/UniqueConstraint`（泛型） |
| 时间戳 | ✅ | `server_default=func.now()` → PG `now()` |
| `processing_started_at` | ✅ | `.with_variant(SQLiteDateTime, "sqlite")`；PG 落 `TIMESTAMP WITH TIME ZONE` |
| CAS claim / Attempt Fence | ✅ | 条件化 `UPDATE ... rowcount`，PG 原生支持 |
| JSON 列 | ✅ | `JSON(none_as_null=True)`，PG 原生 |
| **孤儿回收 cutoff** | ❌→✅ | 原 `func.datetime("now", "-N seconds")` 为 **SQLite 专有**——本 Step 修复 |

**唯一功能阻塞点**：`reclaim_stale_processing` 的 SQLite 专有 `datetime()` 函数。

## 2. 关键决策（实施时校准）

**PG cutoff 表达式**：不写死 `make_interval(...)`（需 7 个位置参数，且 `secs=` 关键字
不被 SQLAlchemy `Function` 接受）。改为绑定**相对间隔**：

```python
# _stale_cutoff：按 dialect 选择
sqlite:  func.datetime("now", f"-{seconds} seconds")          # 原样保留，行为不变
其他(PG): func.now() - literal(timedelta(seconds=seconds), type_=Interval)  # now() - :interval
```

- 编译实测：`SELECT now() - %(param_1)s`（单个绑定参数，`timedelta` → PG `interval`）；
- 类型稳定：`timestamptz − interval → timestamptz`；
- **不拼裸 SQL**（既有守卫 `test_g1` 的 `text(` 禁令保持成立）；
- 仍满足 12.9.4.11 §4「cutoff 由 **DB** 计算，app 只传阈值间隔」。

**baseline migration 忠实性**：由 autogenerate 对**空 PostgreSQL**生成后逐字段核对，
只含 4.2 冻结的 **8 张业务表**（无任何额外业务变化）；`alembic check` 实测
**No new upgrade operations detected**（baseline 与当前模型一致）。

**A8 边界**：`init_database()` 的 `create_all` **仅对 SQLite 生效**；Cloud PostgreSQL
由 `alembic upgrade head` 管理 schema（防「create_all 建表 + Alembic 基线」双轨冲突）。

**Alembic env 日志纪律**：`fileConfig(..., disable_existing_loggers=False)`——迁移与应用
同进程运行（pytest / lifespan）时，默认 `True` 会全局禁用应用 logger。

## 3. 变更文件

新增：

- `backend/alembic.ini`（纯 ASCII；URL 由 env.py 解析，环境变量优先）
- `backend/alembic/env.py`
- `backend/alembic/script.py.mako`
- `backend/alembic/versions/0001_baseline_core_tables.py`（8 表 baseline）
- `docker-compose.cloud.yml`（Cloud PG profile：postgres + backend，启动先 `alembic upgrade head`）
- `backend/tests/test_cloud_0_1_1_a_postgres_alembic_foundation.py`
- `docs/step-cloud-0.1.1-A-postgres-alembic-foundation.md`（本文件）

修改（最小面）：

- `backend/app/persistence/repositories/interaction_record.py`（方言感知 cutoff）
- `backend/app/persistence/database.py`（`init_database` sqlite-only 守卫）
- `backend/requirements.txt`（+ `alembic==1.19.1` / `psycopg[binary]==3.3.4`）
- `backend/requirements.lock.txt`（Linux 容器 `pip freeze` 重建）
- `backend/tests/test_step5_7_closure.py`（A7 断言随 Alembic 引入翻转）
- `backend/tests/test_step14_2_3_c1_dependency_reproducibility.py`（直接依赖清单 +2）
- `README.md`（Cloud profile + Alembic 命令）

未改动（证据）：`app/application/*`（六段 Executor / coordinator / runtime / driver /
worker）、`app/domain/*`、`app/repositories/*`、`app/adapters/*`、8 张 ORM、OSS
`docker-compose.yml`。

## 4. 验收结果

| 项 | 结果 |
| --- | --- |
| PostgreSQL fresh DB → `alembic upgrade head` → 8 表 | ✅ 恰 8 业务表 + `alembic_version` |
| UoW 8 实体 round-trip（PG） | ✅ |
| CAS claim / reclaim / Attempt Fence（PG） | ✅ |
| `init_database()` 在 PG 不建表（A8） | ✅ |
| OSS SQLite 全量回归 | ✅ 1690 passed, 5 skipped, 0 failed |
| 全量 + PostgreSQL 回归 | ✅ 1695 passed, 0 failed |
| `alembic check` | ✅ No new upgrade operations detected |
| Runtime contract 语义 | ✅ 零变化（执行顺序 / Result / Agent 契约字段面守卫全绿） |

## 5. 后续（不属于本 Step）

Cloud-0.1.1 后续 Step：tenant_id / tenants / RLS / Browser Auth / Webhook Tenant
Routing / Workspace UI / WorkBuddy·WeCom Cloud implementation。
