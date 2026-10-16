# Step 5.1 —— Project Bootstrap & Runtime Skeleton

一句话目标：**把 Agent Control Plane 的后端工程真正跑起来。**

本步不解决业务问题，只解决「工程骨架是否可运行」这一件事。
从这一刻起，项目从「架构设计」进入「可运行的软件系统」。

---

## 1. 最小运行链

```
HTTP
 └─> FastAPI
      └─> app.main
           └─> GET /health
                └─> 200 {"status": "ok"}
```

第一版不碰数据库。理由：我们需要先证明**工程骨架本身能够启动**，
而不是先证明数据库能连通。依赖越少，失败原因越容易定位。

---

## 2. 目录基线（目录 = 架构边界）

```
backend/app/
├── main.py                    应用装配入口（唯一）
├── core/                      跨层基础设施：配置（errors.py 延后）
├── api/                       HTTP 入口
│   ├── health.py              进程级存活探针
│   ├── management/            控制面（空壳）
│   └── runtime/               数据面（空壳）
├── schemas/                   HTTP 契约（Pydantic），仅 api 可依赖
├── application/               用例编排（空壳）
├── domain/                    领域（空壳）
├── repositories/              仓储抽象端口（空壳，无实现）
├── adapters/
│   ├── channels/              入站渠道适配（空壳）
│   └── agents/                出站 Agent 适配（空壳）
└── persistence/
    ├── models/                ORM 模型（空壳，5.3）
    ├── mappers/               ORM <-> 领域映射（空壳）
    └── repositories/          仓储 SQLAlchemy 实现（空壳）
```

**关键取舍：目录存在 ≠ 实现存在。**
除 `core/`、`api/health.py`、`schemas/health.py` 外，其余层本步全部是
只有 `__init__.py` 的空壳，且每个 `__init__.py` 都写明了该边界的职责与禁令。
这样后续每个 Vertical Slice 到来时，落点已经确定，不需要再讨论「放哪儿」。

### 两个需要留档的决定

1. **`health.py` 放在 `api/` 根，而不是 `management/` 或 `runtime/` 下。**
   存活探针不属于任何纵向切片：它是编排层探测「进程是否活着」的平台级入口，
   既不代表控制面可用，也不代表数据面可用。塞进任一子包都会让
   「runtime 挂了但进程活着」的语义变含糊。**该决定可逆**——
   未来若要细粒度探针（如 `/runtime/ready`），在 `runtime/` 下新增即可。
2. **`app/repositories/` 与 `app/persistence/repositories/` 的分工。**
   前者是「需要什么能力」（抽象端口，供 application 依赖，不碰 SQLAlchemy）；
   后者是「怎么落地实现」（SQLAlchemy 具体实现）。
   分开之后，application 不依赖具体存储，换库不必改用例编排。

---

## 3. Application / Domain 保持空壳（5.1.3 控制点）

虽然完整链路是：

```
API -> Application -> Domain -> Repository -> Persistence
```

但 5.1 **不为架构完整而提前实现这些层**。以下对象本步明确不实现：

```
UserService  AgentService  ConnectionService  RoutingService
IdentityResolver  SessionResolver  ...
```

原因很直接：它们属于具体 Vertical Slice，现在没有真实运行场景。
过早实现只会得到「架构越来越漂亮，但代码没有被验证」的结果。

这条纪律已被机器化：`tests/test_import_boundaries.py` 会断言
`application/` 与 `domain/` 下不存在 `*_service.py` 或 `*Service` 类。
等真正进入某个 Slice 时，**删除该断言**而不是绕过它。

---

## 4. Core（4. 配置最小集）

`app/core/config.py` 只暴露三个变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `APP_NAME` | `Agent Control Plane` | 应用名，同时作为 OpenAPI 标题 |
| `DATABASE_URL` | `sqlite:///./agent_control_plane.db` | 5.2 起生效，本步不连库 |
| `ENVIRONMENT` | `development` | 运行环境标识 |

`core/errors.py` **暂不建立**：目前没有任何错误语义需要被统一表达，
等出现第一个跨层错误类型时再补。

刻意暂不引入：Redis、Kafka、Celery、JWT、OAuth、Vault、Kubernetes 相关配置。

---

## 5. 依赖方向（验收 D）

只允许外层依赖内层：

```
api          -> schemas, application, domain, core
application  -> domain, repositories(端口), core
adapters     -> application/domain 端口, core
persistence  -> domain, core, (唯一允许 SQLAlchemy 的业务相关层)
repositories -> domain, core
domain       -> core / 标准库
```

机器化禁令（AST 扫描，`tests/test_import_boundaries.py`）：

| 层 | 禁止 import |
| --- | --- |
| `domain` | `sqlalchemy`、`fastapi`、`starlette`、`app.schemas`、`app.api`、`app.application`、`app.persistence`、`app.adapters`、`app.repositories` |
| `application` | `fastapi`、`starlette`、`sqlalchemy`、`app.api`、`app.persistence`、`app.adapters` |

> 说明：规则用 AST 而非文本匹配。docstring 里常出现「本层不 import SQLAlchemy」
> 这类否定式说明，文本匹配会把这些说明误判成违规。
>
> 未纳入机器校验的约定（留给后续步按需收紧）：
> domain 是否能使用 pydantic 库本身（当前规则只禁止 `app.schemas` 这类 API Schema 反向流入）。

`app/persistence/` 是唯一允许出现 SQLAlchemy 的业务相关层——这是 5.2/5.3 的落点，
本步该目录仍是空壳。

---

## 6. 验收标准

不看代码量，只看事实：

### A. 工程可以启动

```bash
cd backend
uvicorn app.main:app --reload
```

成功启动（无 ImportError、无 Traceback）。

### B. Health Check

```bash
curl http://127.0.0.1:8000/health
```

```json
{"status": "ok"}
```

### C. Swagger 可以打开

浏览器打开 <http://127.0.0.1:8000/docs>，能看到 `/health`。

### D. 导入边界没有反向依赖

```bash
cd backend
python -m pytest -q
```

`tests/test_import_boundaries.py` 全绿即视为守住。

---

## 7. 常用命令

```bash
# 启动（必须在 backend/ 目录下，模块路径是 app.main）
cd backend && uvicorn app.main:app --reload

# 测试
cd backend && python -m pytest -q

# 容器启动（可选）
docker compose up backend
```

---

## 8. 本步刻意不做

- 不建库、不建表、不连数据库（5.2 / 5.3）
- 不实现任何 Service / Resolver / Repository
- 不引入 Redis / Kafka / Celery / JWT / OAuth / Vault / Kubernetes
- 不建立 `core/errors.py`
- 不做前端工程
- 不做认证、租户、日志中间件（等有真实消费方）

---

## 9. 下一步

```
5.2  FastAPI ──> SQLAlchemy ──> SQLite      打通持久化连接
5.3  SQLAlchemy ──> 8 张核心表              落地 persistence/models
5.4  Repository ──> 真实数据库读写           打通端口与实现
之后  Runtime Vertical Slice:
      WeCom ──> Control Plane ──> ACP Agent ──> WeCom
```
