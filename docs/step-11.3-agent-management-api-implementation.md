# Step 11.3 — Agent Management API Implementation

> **实现步。** 按 11.2 §8 清单逐项落地，**不新增任何契约外能力**。
> 交付物 = 生产代码 + 本报告 + 验收测试
> （`tests/test_step11_3_agent_management_api.py`，33 项）。
> 最终全量回归：**994 passed**。

---

## 1. 三层实现（严格沿 11.2 冻结契约）

```
HTTP (api/management/agents.py)
  ↓  只做：HTTP 事实提取 + 应答序列化 + Application 错误→状态码映射
Schema (schemas/agents.py)
  ↓  只做：字段形状（类型/必填可选/响应字段面）；不做业务判断
Application (application/agents.py)
  ↓  业务规则唯一裁决点：白名单 / URL / 引用非空 / name 去重
Domain (domain/agent.py) → Repository Port → Persistence Adapter → DB
```

**API 不直接调 Repository、不碰 SQLAlchemy Session**（用户 11.3 红线）；
AST 测试守护 API/Schema 层无 `sqlalchemy` / `app.persistence` 导入、
无业务判断 token（`ALLOWED_` / `urlsplit` / `Literal[`）。

### 1.1 Schema 层（`app/schemas/agents.py`）

- `AgentCreate` / `AgentUpdate`（请求）：形状模型；`credential_ref` 在此出现（写路径）。
- `AgentResponse` / `AgentListResponse`：8 个可见字段
  `id/name/type/status/endpoint/config/created_at/updated_at`；
  **`credential_ref` 被字段面排除**（引用不出）。
- **刻意偏离 11.2 §7 的「schema 枚举 + 服务守卫双重拦截」**：按用户 11.3 指令
  「不要在 API 层重新判断业务规则」，type/status/URL/引用校验全部**单点**
  落在 Application Service（API 只做形状）。非法值依旧 422（经错误映射）。

### 1.2 Application 层（复用 + 收口，非重写）

- `create_agent` / `get_agent` / `update_agent` 编排逻辑**原样复用**；
- 新增政策校验（11.2 冻结）：
  - `ALLOWED_AGENT_TYPES = ("acp",)` —— 对齐 `adapters/agents/` 注册表；
  - `ALLOWED_MANAGEMENT_STATUSES = ("active", "inactive")` ——
    **`error` 输入一律拒绝**（运行态仅 Runtime 写入，§3）；
  - endpoint 最小 URL 校验（scheme http/https + 非空 host；纯字符串解析，
    **刻意不用 urllib**——6.5/6.7/7.3/10.1.4 五个既有边界守护把 `urllib.*`
    视为协议/传输泄漏，最小校验不需要它）；
  - `credential_ref` 提供则必须非空白；`config` 必须为 dict。
- `CreateAgent` 补 `status`（可选，缺省 active）；
- 补 `list_agents()`（Service + Port + Adapter，MVP 无分页/过滤）；
- **修复一个实现中暴露的真实缺口**：`update_agent` 改名无去重守卫
  （11.2 §5.3 要求 PATCH 重名 → 409，原服务只在 create 有守卫）→
  在改名 != 现名时经 `find_by_name` 预检（与 create 同一 TOCTOU 窗口，见 §4）。

### 1.3 Domain / Persistence

- `Agent` 补 `created_at` / `updated_at` 只读审计回显（与 Session 实体同一先例；
  11.2 §5.1 响应契约需要，故合法推进 11.1 审计快照的 7 字段事实）；
- Adapter `create`/`update` 后 `refresh` 取回 server_default/onupdate 时间戳
  （保证 create 返回值与随后 get 值一致）；新增 `list_agents`（按 id 升序）。

### 1.4 HTTP 层（`app/api/management/agents.py` + `main.py` 装配）

- `build_agents_api(service)` 工厂 + 构造注入（与 runtime channels 同一纪律）；
  4 路由：`POST ""`（201）、`GET ""`、`GET /{agent_id}`、`PATCH /{agent_id}`；
  **无 DELETE**（软下线 = PATCH status=inactive）。
- 错误映射（唯一映射点，11.2 §6）：
  `DuplicateAgentError→409`、`AgentNotFoundError→404`、`InvalidAgentInputError→422`。
- `main.py` 组合根：`AgentApplicationService(uow_factory=SqlAlchemyUnitOfWork)`
  + `include_router(build_agents_api(...))`；测试注入内存库实例，不经此路径。

---

## 2. status 双轴落实（11.2 §1.2）

| 场景 | 行为 | 测试 |
| --- | --- | --- |
| Create/PATCH 输入 `active`/`inactive` | 201/200 | ✅ |
| Create/PATCH 输入 `error`/`disabled`/任意值 | **422** | ✅ |
| Runtime dispatch 失败 → 写 `error`（测试用直接 DB 写模拟 Runtime 专有写路径） | — | ✅ |
| GET 读到 `error` | 原样返回（读不裁切，运行态信号可见） | ✅ |
| 管理员 PATCH `error → active` | 200 手动清障（唯一的管理覆盖运行态） | ✅ |

## 3. credential_ref「引用入、引用不出」落实

| 操作 | 行为 | 验证方式 |
| --- | --- | --- |
| POST 可入 | 落库 `vault://agent/acp-prod` | 绕过应用层直读 DB 行 |
| GET 单个 / 列表 | 响应字段面无 `credential_ref` | 逐字段断言（含列表每项） |
| PATCH 缺省 | 不变 | DB 行值不变 |
| PATCH 显式 `null` | 不变（无清空语义） | DB 行值不变 |
| PATCH `""` / 纯空白 | 422 | HTTP 探针 |
| PATCH `"new-lease"` | 覆写 | DB 行值更新 |

## 4. name 唯一性——记录事实，不偷偷迁移（用户 11.3 指令）

- 409 语义已实现（create + PATCH 改名两条路径）；
- **TOCTOU 竞态窗口真实存在并被测试记录**：绕过服务层直接写库可插入同名行
  （无 `IntegrityError` → 证明无 DB UNIQUE）；
- **未做 migration**——除非真实运行环境证明需要 DB 级唯一，才单列步骤
  （与 10.9「不为未来提前建设」一致）。

## 5. 验收对照（用户 11.3 清单 → 测试）

| 验收项 | 结果 |
| --- | --- |
| Create：201 全字段、credential_ref 可写、响应不含它 | ✅ |
| Create：非法 type / endpoint / status=error → 422；duplicate → 409 | ✅ |
| List：全部可见字段、不泄漏 credential_ref、空列表合法 | ✅ |
| Get：200 / 404 / 不返回 credential_ref | ✅ |
| Patch：六字段逐一生效；credential_ref 缺省/null=不变、""=422、"new"=覆写 | ✅ |
| Patch：status=error → 422；error 态管理员 PATCH active → active | ✅ |
| 不做 Frontend | ✅（`frontend/` 保持占位，11.1 B3 事实未动） |
| 真实 Vertical Slice：HTTP→Schema→Application→Domain→Repository→DB | ✅（TestClient 真实探针） |
| 生产装配契约可见（openapi 含 4 端点、无 delete） | ✅ |

## 6. 受事实推进而更新的既有审计/守护测试

11.1（快照审计）、5.6/5.7、6.1/6.5/6.7、7.1/7.3 中的结构断言按新事实更新：
port 方法面 +`list_agents`、service 用例面 +`list_agents`、domain 纯度
+`datetime`、api/management 非空壳（只许 `agents.py`）、
schemas 文件面 +`agents.py`、政策缺口断言从「缺口存在」翻转为「已收口」。
**守护强度未降低**：API/Schema 无 ORM、Application 无 urllib、Adapter 不 commit、
Management API 不依赖 persistence 等边界全部保留。

## 7. 不做（与 11.2 §8 一致）

`agents.name` DB UNIQUE migration、runtime_status 独立列、分页/过滤、
Delete、Frontend、动态 adapter type 发现。
