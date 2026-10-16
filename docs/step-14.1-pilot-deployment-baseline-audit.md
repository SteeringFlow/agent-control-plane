# Step 14.1 — Pilot Deployment Baseline（Implementation Before Audit）

> **纯审计步，生产代码改动：0 行。**
> 目标极窄：把已完成的 ACP MVP 从「开发环境可运行」收敛为「**单实例 Pilot 可部署、
> 可启动、可验证**」。不新建任何架构面。
>
> 本报告同时给出 14.2 的实施清单（P0 阻断项 / P1 加固项），但本步不落地。

---

## 0. 审计结论摘要（先看这个）

| # | 阻断项 | 证据 | 严重度 |
| --- | --- | --- | --- |
| **B1** | **容器内 `FRONTEND_DIR` 路径不存在 → 启动即崩** | `main.py:52` `parents[2]/"frontend"`；compose 只挂 `./backend:/app` → 解析为 `/frontend` | 🔴 P0 |
| **B2** | **生产代码无任何建表入口 → 全新环境所有 API 500** | `database.py:22` 明确「不提供 create_all/init_db」；13.4 实测新库 `no such table: interactions` | 🔴 P0 |
| **B3** | **compose 为开发形态**：无 Dockerfile、启动期 `pip install`、`--reload`、无卷/无健康检查、`8000:8000` 全暴露 | `docker-compose.yml:6-18` | 🔴 P0 |

**结论：当前形态不满足 Pilot Gate。** 三个 P0 全部是「部署外壳」问题，
**不涉及 Runtime / Worker / Driver / Persistence / Domain 任何语义改动**，
修复面完全落在 14.1 定义的范围内。

---

## 1. 逐题审计

### Q1 — 当前启动方式

**链路事实**（`main.py:64-73, 187-197, 201`）：

```
FastAPI(lifespan)
  ├─ 装配：UoW / Adapters / 六 Executor / RuntimeCoordinator
  │        / PersistingCoordinator(phases) / Worker(phases)
  │        / ChannelInboundService(intake=phases)
  └─ lifespan:  yield 前 driver.start()，finally 里 driver.stop()
     └─ StaticFiles mount "/" → frontend/
```

| 检查项 | 结论 |
| --- | --- |
| Driver 是否必然启动 | ✅ 是。`lifespan` 内 `driver.start()`，与应用生命周期绑定（唯一生命周期机制） |
| shutdown 是否正确停止 | ✅ 是。`finally` 中 `driver.stop()`；`stop` 用 `Event.wait` 唤醒（无需等满间隔）、daemon 线程不阻塞退出；**不中断**在途执行，由 Core RECOVER 兜底（`driver.py:104-126`） |
| 开发环境专属启动逻辑 | ❌ 有：`--reload`（compose:10）、启动期 `pip install`（compose:9）、`docker-compose.yml` 中 `ENVIRONMENT=development` |
| `--reload` 对 Driver 的影响 | ⚠️ reload 把进程模型变成 monitor + worker，lifespan 反复执行；**resident Driver 的生命周期与 reload 组合不应作为 Pilot 运行方式**（详见 Q6） |

**结论**：运行时语义正确（无需改），需要清掉的是**启动外壳**（B3）。

### Q2 — Container / Compose

**现状**：

| 资产 | 现状 |
| --- | --- |
| Dockerfile | ❌ **不存在**（全仓库 0 个） |
| `docker-compose.yml` | ✅ 存在，但为开发形态：base image `python:3.14-slim` + 启动期 `pip install -r requirements.txt` + `uvicorn --reload` + bind mount `./backend:/app` + `ports 8000:8000` |
| `.env` / 模板 | ✅ `backend/.env.example`（SAFE TEMPLATE，只含键名）；`backend/.env` 已 gitignore（`.gitignore:25-27`） |
| Kubernetes / Helm | ❌ 不存在（**不需要**） |

**目标形态**（用户给定，本步确认其可行性）：

```
┌──────────────────────────────┐
│        ACP Pilot（单实例）    │
│  FastAPI                     │
│    ├─ Management             │
│    ├─ Observation            │
│    ├─ WeCom Webhook          │
│    ├─ Worker                 │
│    └─ ProductionDriver       │
│  SQLite ──► Persistent Volume│
└──────────────────────────────┘
```

**可行，但必须先修 B1**：`FRONTEND_DIR = Path(main.py).parents[2] / "frontend"`。
容器布局需保证 `main.py` 位于 `<root>/app/main.py` 且 `<root>/frontend/` 同时存在
（当前 bind mount 只挂 `./backend`，故 `<root>=/app/app`，解析到 `/frontend` 不存在，
`StaticFiles(directory=...)` 在启动期即抛错 → **容器起不来**）。

### Q3 — 数据持久化

**「重启后数据是否还在」——分两层回答**：

| 场景 | 结果 | 说明 |
| --- | --- | --- |
| 当前 compose（bind mount `./backend:/app`） | ✅ 数据在宿主机 `./backend/agent_control_plane.db` | 但 DB 混在源码目录里，且无独立卷，镜像化后会丢失 |
| 镜像化后若无 named volume | ❌ DB gone（ephemeral container fs） | 正是用户担心的 Pilot blocker |

**但真正的 P0 不是"重启丢库"，而是 B2：新库无表。**
生产代码刻意不提供 `create_all/init_db`（`database.py:22`）；13.4 实测中全新
`agent_control_plane.db` 直接导致 `GET /api/interactions` 500
（`no such table: interactions`）。Pilot 从零部署必然踩中。

**方向**：保留 SQLite（**不迁 PostgreSQL**，5.2 裁决 + 14.1 禁止清单），
加一个**显式的一次性 schema 初始化步骤**（仍用 `create_all`，MVP 纪律，
不引入迁移工具链）+ named volume 持久化。

`.gitignore:13-15` 已忽略 `*.db` → DB 不会误提交 ✅（也意味着每个新环境都必须 init）。

### Q4 — 依赖可复现性（"开发机能跑 ≠ 别人机器能跑"）

| 项 | 现状 | Pilot 要求 |
| --- | --- | --- |
| Python 依赖 | `requirements.txt` **全部 `>=`（无 pin）**：fastapi / uvicorn[standard] / pydantic-settings / sqlalchemy / cryptography + pytest / httpx | ⚠️ 需锁定版本（否则不同时间构建得到不同依赖树）；运行时镜像不装 dev 依赖 |
| Python 版本 | compose `python:3.14-slim`；本地 Python 3.14 | ✅ 一致，需在文档固定 |
| 前端静态资源 | 无构建、由 FastAPI 直接托管（`main.py:201`） | ⚠️ 依赖 `frontend/` 随镜像/卷可得（B1） |
| Ollama / 外部 ACP | **不是部署期依赖**：Agent 端点 + `credential_ref` 由 Management 在库内配置（11.2 写-only）；`TENCENT_ACP_*` 仅 live-test 用途（`.env.example:16-19`） | ✅ 无需外部服务 |
| 环境变量 | `.env.example` 模板 ✅；compose 内联 env 与 `.env` **双来源并存** | ⚠️ 需明确单一来源（compose `env_file` 或 env 段二选一） |

**结论**：可复现启动的缺口 = 版本锁定 + 依赖预装进镜像 + 目录布局修正，**无架构改动**。

### Q5 — Secrets / Exposure Boundary

**事实**：ACP 自身 **0 鉴权**（13.5 Q4 已核实：JWT/Bearer/login 的命中项全是 Adapter
出站凭据）。当前 `ports: 8000:8000` 把**三个面一起暴露公网**：

```
Internet ──► :8000 ──► Management（写配置）      ❌ 不可裸暴露
                    ──► Observation（含对话原文 payload） ❌ 不可裸暴露
                    ──► WeCom Webhook（设计上必须公网）  ✅
```

**Pilot 的最小网络边界（不建 Auth 系统）**：

1. 容器只绑定 `127.0.0.1:8000`（不直接对外），由反向代理（Nginx/Caddy）对外；
2. 反代只放行 webhook 相关路径（`GET/POST /api/channels/{id}/wecom/webhook`）；
3. 对 `/`（Management+Observation UI）与 `/api/...` 施加 **Basic Auth 或 IP allowlist**
   （一行配置，非权限模型）；
4. 凭据存放纪律已存在且正确：Channel `config` 含 token / encoding_aes_key /
   corpsecret（UI 回显 `********`，11.9），Agent `credential_ref` 写-only（11.2），
   `backend/.env` gitignore（10.1.5）✅ —— **不需要新机制，只需要不破坏它**。

**明确不做**：JWT / RBAC / 用户体系 / 新权限模型（14.1 禁止清单 + 13.5 拍板）。

### Q6 — `--reload` 清除

**必须清除。** 事实依据：

- `ProductionDriver` 是 **resident 生命周期组件**（daemon 线程，挂在 lifespan，
  `driver.py:26-28` 明示「不另造一套生命周期机制」）；
- `--reload` 引入 monitor + worker 双层进程，文件监听触发的重启会反复执行 lifespan
  → resident 组件的生命周期被进程重启反复打断/重建，与「执行权由 CAS 唯一裁决」的
  可靠性模型叠加出**不必要的进程不确定性**；
- reload 依赖源码目录监听，在 bind mount/容器中既慢又不必要。

**Pilot 启动方式**：`uvicorn app.main:app --host 0.0.0.0 --port 8000`（**单 worker、无 reload**）。

### Q7 — Health / Startup Verification（克制）

**现状**：`GET /health` 存在，返回 `{"status":"ok"}`；`health.py:17` docstring 明示
「**不检查数据库等外部依赖**」。

对照 Gate 五项：

| Gate 项 | 现状能否证明 | 结论 |
| --- | --- | --- |
| Container starts | ✅ `docker compose up` 退出码 + 日志 | 无需新东西 |
| FastAPI ready | ✅ `/health` | 现状足够 |
| **DB accessible** | ❌ **`/health` 不查库，无法证明** | 需要**极小** readiness 探针（一次 `SELECT 1`） |
| Driver alive | ⚠️ 无端点 | 建议：启动日志断言 + 真实交互验证，**不做指标端点** |
| Webhook reachable | ⚠️ 无端点 | 用 WeCom 握手（GET 验签）或 curl 验证，**不做探测框架** |

**审计决定（而非预设）**：需要**一个**极小的 DB readiness 探针——可以是扩展
`/health`（增加 `db` 字段）或新增 `/health/ready`，**不是 Prometheus / Grafana /
OpenTelemetry / tracing**（14.1 禁止清单）。其余四项靠日志与一次真实交互证明，
**不新增任何可观测性基础设施**。

### Q8 — Pilot Walk-through（12 步）

当前**步骤 1–7、9–12 均已具备**（Management 五页 + Runtime + Observation 已验收），
但 12 步链条上**缺第 0 步与第 8 步的可操作性文档**：

| # | 步骤 | 现状 | 缺口 |
| --- | --- | --- | --- |
| 1–2 | 准备环境 / 配置 env | 部分 | `.env.example` ✅；需补「从模板复制 + 必填项说明」 |
| 3 | `docker compose up` | ❌ | B1/B2/B3 未修则起不来或起来即 500 |
| **0（缺）** | **初始化数据库 schema** | ❌ | **B2：必须显式 init 步骤** |
| 4 | 打开 Management | ⚠️ | 需暴露面说明（Q5）+ 访问地址 |
| 5 | 配置 Agent | ✅ | endpoint + type(acp) + credential_ref |
| 6 | 配置 Channel | ✅ | type=wecom + config：`token`/`encoding_aes_key`/`corp_id`(+`corpsecret`/`agentid`/可选 `receiveid`)（10.2.1/10.2.2） |
| 7 | 创建 Connection | ✅ | channel × agent 绑定（11.11） |
| 8 | 配置 WeCom 回调 | ⚠️ | 需写明回调 URL 形态 `POST /api/channels/{id}/wecom/webhook` 与握手验签 |
| 9–10 | 发消息 → 得到回复 | ✅ | 真实 wire 已冒烟（12.8.6） |
| 11–12 | Observation 查看执行故事 | ✅ | 13.4 已验收 |

**结论**：Walk-through 的**内容全部存在**，缺的是「把它写成未参与开发者可执行」的
文档 + 第 0 步 init。**这恰恰说明：14.1 的目标只是部署外壳 + 文档。**

---

## 2. 明确禁止事项（14.1 写入，14.2 严格遵守）

❌ PostgreSQL migration　❌ Kubernetes / Helm　❌ CI/CD platform
❌ JWT / RBAC / 用户体系　❌ Retry / Replay　❌ Metrics / Prometheus / Grafana
❌ Distributed tracing / OpenTelemetry　❌ Message Queue　❌ Redis
❌ Multi-instance orchestration　❌ Second Channel　❌ Second Agent
❌ 迁移工具链（Alembic 等）　❌ 改动 Runtime / Worker / Driver / Persistence 语义
❌ 新增领域状态 / 新 DB 字段 / 新表

**允许的最小生产改动面（14.2）**：
新增 `Dockerfile`、改 `docker-compose.yml`、新增一次性 DB 初始化入口/脚本、
极小 readiness 探针（可选）、`requirements` 运行时锁定、`.env` 模板与部署文档。

---

## 3. 14.2 实施清单（P0 / P1）

**P0（否则 Gate 无法通过）**：

1. **B1 容器目录布局**：镜像/卷保证 `main.py` 在 `<root>/app/main.py` 且
   `<root>/frontend/` 存在（COPY 整个仓库或调整挂载），使 `FRONTEND_DIR` 解析正确；
2. **B2 Schema 初始化**：提供一次性 init（仍 `create_all`，MVP 纪律），并写入
   Walk-through 第 0 步；
3. **B3 Compose/Dockerfile 加固**：新增 Dockerfile（依赖预装、无 dev 依赖）、compose
   去 `--reload`、去启动期 `pip install`、**named volume 挂 SQLite**、绑定
   `127.0.0.1`、加 `healthcheck`。

**P1（Gate 可选/体验）**：

4. 极小 DB readiness 探针（扩展 `/health` 或 `/health/ready`）；
5. `requirements` 运行时版本锁定 + dev 依赖分离；
6. `.env` 单一来源 + 必填项说明；
7. 反代/暴露面配置样例（Basic Auth 或 IP allowlist，仅 webhook 公网）；
8. Pilot Walk-through 文档（含第 0 步 init、WeCom 回调 URL、Observation 验收）。

---

## 4. 最终验收 Gate（14.1 定义，14.2 验证）

> **在一台干净机器上，从零完成部署：容器启动后，使用真实 WeCom → ACP → Agent →
> Delivery 链路完成一次交互，并能够在 Observation 中看到完整执行故事；
> 重启容器后历史数据仍存在。**

分解的可验证子项：

| # | 子项 | 判定 |
| --- | --- | --- |
| G1 | 干净机器 `git clone` + 按文档 `docker compose up` 能启动（无手动改代码） | 容器 Running |
| G2 | 启动后 FastAPI 就绪 + **DB 可访问**（无 `no such table`） | readiness 探针 / API 200 |
| G3 | Driver alive（PENDING 能被排空） | 一次真实交互成功 |
| G4 | 真实 WeCom 消息 → ACK → Agent 执行 → 投递成功（用户收到回复） | 真实 wire |
| G5 | Observation 中该 Interaction 的执行故事完整（双状态 / 时间线 / payload） | 13.4 UI |
| G6 | `docker compose restart` 后历史 Interaction/Activity/Management 数据仍在 | named volume |
| G7 | Management / Observation **不裸暴露公网**（仅 webhook 对外） | 反代/绑定配置 |

**Gate 通过即意味着：ACP 具备第一个外部 Pilot 的可部署基线。**
此后不再做基础设施，进入 Pilot / External Traction（13.5 §4 已拍板）。
