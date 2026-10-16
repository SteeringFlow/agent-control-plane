# Step 14.2.3.B — Minimal Readiness + Healthcheck

> 目标极克制：给 `/health` 增加**唯一一个** readiness 信号（应用进程能否访问数据库），
> 并让 Docker healthcheck 使用它。**不建立监控系统。**

```
GET /health → SELECT 1 → DB reachable → 200
                              ↓不可达   → 503 → Docker healthcheck unhealthy
```

## 1. 实现（改动面）

| 文件 | 改动 |
| --- | --- |
| `backend/app/api/health.py` | 新增 `_database_is_reachable(db)`：经 FastAPI 依赖注入拿 Session，取**底层 DBAPI 连接**执行 `SELECT 1`；不可达 → `503`（标准 `HTTPException`） |
| `docker-compose.yml` | 新增 `healthcheck`（用 Python 标准库 `urllib` 探测 `/health`，**不装 curl/wget**，不新增镜像层）：interval 30s / timeout 5s / retries 3 / start_period 15s |
| `backend/tests/test_step14_2_3_b_minimal_readiness.py` | 新增 5 项验收 |
| `backend/tests/test_persistence_smoke.py` | 更新被本步推翻的旧守卫（见 §3） |

**响应体保持不变**：`{"status": "ok"}`（**不新增字段、不新增状态**）。

**实现约束**：api 层不得 import SQLAlchemy（`test_import_boundaries.py` 锁死），故不做
`text("SELECT 1")` 构造，而是用 Session 的底层 DBAPI 连接执行原语——不引入 ORM 依赖。

**刻意不检查**（机器化守卫 `test_health_does_not_check_external_services`，扫描可执行
代码、剔除 docstring）：Agent / Channel / Connection 是否配置、ACP / WeCom 是否在线、
Driver 是否有 pending / 是否在跑、外部网络是否可达。

## 2. 验收实测

| # | 项 | 结果 |
| --- | --- | --- |
| 1 | Fresh container startup | `docker compose up -d --build` 启动成功 ✅ |
| 2 | `GET /health` | **200** `{"status":"ok"}` ✅ |
| 3 | 实际执行 `SELECT 1` | 用 `dependency_overrides` 注入假 Session，断言实际执行语句 **`== ["SELECT 1"]`** ✅ |
| 4 | Docker healthcheck | `docker compose ps` → **`Up (healthy)`** ✅ |
| 5 | Driver 正常 | `Application startup complete.`；`Traceback`/`no such table`/`OperationalError` 计数 **0** ✅ |
| 6 | `pytest -q` | **1472 passed**（1:49）✅ |
| 7 | Runtime / Worker / Driver 0 改动 | 仅 `api/health.py` + compose + tests + docs ✅ |

补充：容器内 `inspect(engine).get_table_names()` = 8 张表（探针不依赖任何表，
`SELECT 1` 语义成立）。

## 3. 被本步推翻的旧守卫（已更新，非删除）

`test_persistence_smoke.py` 的 5.6 F 节原断言「`/health` 不碰数据库」。该断言编码的是
5.6 时代的前提，**已被 14.1 审计结论推翻**（`/health` 不查库 → 无法证明 DB accessible）。

- `test_health_returns_ok_without_db_touch` → `test_health_returns_ok_and_probes_db`
  （语义：200 + 确实探库）；
- `test_no_db_file_until_first_real_db_operation`：注释说明探针本身会建库，故在断言前
  显式复位 DB 文件，只保留「import 阶段不建库」这一不变量。

## 4. 过程中的两点诚实记录

1. **负向验证（DB 不可达 → 503）在容器层难以安全复现**：容器以 root 运行，
   `chmod 000` 对已打开 fd 无效且 root 绕过权限；而**启动期** DB 损坏会让
   `init_database` 直接失败（容器退出，而非 healthcheck 转 unhealthy）。
   因此 503 路径由**单元级**验收保证（`test_health_returns_503_when_db_unreachable`，
   注入失败探针 → 断言 503 + `detail`），这是当前最可靠的证明方式。
2. 负向尝试过程中，卷内演示数据（`persist-check-agent`）随文件移动丢失；
   已从退出容器的 `/tmp` 取回 DB 并写回卷，随后**重新验证持久化**：
   `POST /api/agents` → 201 → `docker compose restart` → 数据仍在（id=1），
   `/health` 200，容器 `healthy`。持久化不变量本身在 14.2.3.A ④已证明，此处为复验。

## 5. 边界核对（禁止项）

❌ Runtime ❌ Worker ❌ Driver ❌ Persistence schema ❌ 新表 ❌ 新字段 ❌ Retry
❌ Metrics ❌ Prometheus ❌ OpenTelemetry ❌ tracing ❌ SSE/WebSocket ❌ 新状态
——**全部为零**。

## 6. 遗留（14.2.3.C）

依赖版本锁定、`.env` 单一来源、反向代理（暴露面隔离完整形态）、Pilot walk-through。
