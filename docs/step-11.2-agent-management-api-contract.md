# Step 11.2 — Agent Management API Contract & Boundary Design

> **契约设计步，生产代码改动：0 行。**
> 交付物 = 本报告（把 API「是什么」定死）。
> 不写 FastAPI、不写 Pydantic、不动 ORM / Application Service——只冻结契约，
> 落地由后续切片（11.3+）执行。本报告裁决 11.1 留下的 4 个政策缺口：
> `status` 双轴、`credential_ref`「引用入、引用不出」+ PATCH 语义、
> `type` 受控集合、`name/endpoint/config` 约束。

---

## 0. 端点面（冻结）

| Method | Path | 语义 | 状态 |
| --- | --- | --- | --- |
| `POST` | `/api/agents` | 创建 | 冻结 |
| `GET` | `/api/agents` | 列表 | 冻结 |
| `GET` | `/api/agents/{id}` | 取单个 | 冻结 |
| `PATCH` | `/api/agents/{id}` | 部分更新 | 冻结 |

- **`DELETE /api/agents/{id}` → 明确 DROP**（沿用 11.1 裁决）。MVP 不做硬删：
  Agent 被 `connections` 以 FK 引用，硬删需处理引用 + 历史 Session/Interaction。
  软下线 = `PATCH status=inactive`（管理态）。若未来确需合规删除，单列步骤，
  不在 11.2 预设。
- `PATCH` 是**唯一**的写更新动词（幂等的局部修改，符合 6.2 `UpdateAgent`
  「None=不改」语义）；**不使用 `PUT`**（整体重写会逼管理员每次回传全部字段，
  且与「引用不出」的读/写不对称冲突）。

---

## 1. 核心裁决 1 —— `status` 双轴：管理态 vs 运行态

### 1.1 问题定性

`status` 当前是 ORM 裸 `String(32)`，Application Service 的 `UpdateAgent.status`
也是裸字符串、无约束。它实际承载**两条互相独立的轴**，混在一个列里：

```
status
├── management state   （管理员经 API 设定，表达「这个 Agent 是否该被使用」）
│     ├── active       启用 / 软上线
│     └── inactive     停用 / 软下线（替代硬删）
└── runtime state      （Runtime 后端在 dispatch 失败时写入，表达「最近一次调用是否健康」）
      └── error        最近一次 dispatch 失败
```

**不能**因为 ORM 是 `status: str`，就让 API 把包括 `error` 在内的任意字符串
都开放给管理员。否则：
- 管理员可把 `status` 写成 `error`，污染运行态状态机；
- 后端写入的 `error` 可被管理员随意覆盖，运行态不可观测；
- 无法区分「这是谁设的」。

### 1.2 裁决（最小、不破坏现有单列表）

**保持现 ORM 单 `status` 列（11.2 不做 schema 迁移）。API 通过「输入白名单 +
所有权策略」收口：**

| 轴 | 取值 | 谁写 | API 是否接受为输入 |
| --- | --- | --- | --- |
| 管理态 `management` | `active` \| `inactive` | 管理员经 API | ✅ 仅这两个 |
| 运行态 `runtime` | `error` | Runtime dispatch 失败 | ❌ **永不接受**（客户端传 `error` → 422） |

- **API 输入 `status` 受控为 `{active, inactive}`**（枚举 `ManagementStatus`）。
- **`error` 是 Runtime 专有值**：仅由 dispatch 路径在失败时写入 ORM；API 侧
  收到 `status=="error"` 的请求体 → `422 Unprocessable Entity`。
- **读路径**（`GET` 单个/列表）原样返回 ORM 值，可能是 `active` / `inactive` /
  `error` 三者之一——读不裁切，因为 `error` 本身就是要暴露给管理员的运行态信号。
- **恢复规则（唯一允许的「管理覆盖运行态」）**：处于 `error` 的 Agent，管理员
  可 `PATCH status=active` 手动清障（表达「我认为它好了，请重新启用」）。
  这是显式、有意的管理意图；若故障仍在，下一次 dispatch 失败会再次写回 `error`。
  ——除这一处外，API 绝不写入 `error`。

> 升级路线（**不在 11.2 建设**，记后续）：当运行态信号需要扩展（如区分
> `error`/`degraded`、记录 last_error_at），再引入独立的 `runtime_status`
> 列，与 `management` 轴彻底解耦。现在不提前建（10.9「不为未来提前建设」）。

---

## 2. 核心裁决 2 —— `credential_ref`：引用入、引用不出

### 2.1 三句冻结规则（11.1 已立，本步锁定到 API 契约）

| 操作 | `credential_ref` |
| --- | --- |
| `POST` 请求体 | ✅ **可以输入**（让管理员录入引用） |
| `GET /{id}` 响应 | ❌ **永不返回** |
| `GET` 列表响应 | ❌ **永不返回** |

理由（与 ORM/领域一致）：它是「指向外部 Secret 存储的引用」，不存明文；
读路径隐藏，防止通过引用间接定位/枚举 Secret 存储。

### 2.2 PATCH 语义裁决（11.1 留的待裁决项，本步定死）

- **允许 PATCH 修改 `credential_ref`**——凭据轮换是真实管理需求（Secret 续期/
  换绑），不应冻结为「创建后不可变」。
- **字段名就用 `credential_ref`，不再发明 `credential_ref_update`**：
  请求体里 Create 与 Patch 用**同一个字段名**，避免「同一概念两个名字」的漂移。
- **PATCH「未提及」语义**（与 6.2 `UpdateAgent` 的 `None=不改` 一致）：
  - **缺省（字段不在 body）** → 保留原值，不改动；
  - **显式 `null`** → 视为「未提供」，同样保留原值（本 MVP **不支持清空**，
    即无「unset」语义，沿用 11.1/6.2 的「不支持显式清空」裁决）；
  - **非 null 字符串** → 覆写为新引用。
- **最小校验**：若提供了 `credential_ref`（非 null），必须是**非空字符串**；
  空串/纯空白 → `422`。引用本身对 API 是不透明字符串，API 不解析、不验证其指向。

> 读路径不返回 ≠ 写路径不接收，二者独立。这是「引用入、引用不出」的完整闭环。

---

## 3. 核心裁决 3 —— `type` 受控集合（对齐 adapter registry）

- 现状：`type` 是自由字符串（ORM `String(64)`，服务不校验），但 Routing 要靠
  `type` 选 Agent——自由字符串 → 类型漂移 → Routing 不可靠匹配（11.1 缺口）。
- **裁决**：`type` 收为**受控枚举** `AgentType`，取值对齐 `app/adapters/agents/`
  注册表。当前唯一合法值：`"acp"`（对应 `ACPAdapter`）。
- **Create / PATCH 收口**：传入不在白名单的 `type` → `422`。
- **白名单来源**：以 `app/adapters/agents/` 下已注册的适配器种类为准；新增适配器
  时同步扩展枚举（不在此步预建「动态发现」机制）。

---

## 4. 核心裁决 4 —— 其余字段约束（收口 11.1 缺口）

| 字段 | 约束 | 违规 → |
| --- | --- | --- |
| `name` | 必填、非空；全局唯一（应用级 `find_by_name` 守卫已存在；DB `UNIQUE` 为后续硬兜底，不在 11.2 建） | 空 → `422`；重复 → `409` |
| `endpoint` | 非必缺但 Create 必填非空；最小 URL 校验 = `scheme ∈ {http,https}` + 非空 `host`（不探活、不过度） | 空/非法 URL → `422` |
| `config` | 可选；自由 JSON `dict`（按适配器需要承载 `protocol`/`timeout_s`/`bearer_token` 等），API 不透明、不校验内部 | 非 dict → `422` |
| `id` | 只读；仅响应返回，绝不出现在请求体 | — |
| `created_at` / `updated_at` | 只读；审计可观测，仅响应返回 | — |

> `name` 的 DB `UNIQUE` 约束：11.1 判定为「小缺口 / 真正确定性来源」，但属
> schema 迁移，不在 11.2（0 生产代码）落地；记入 §7 待办，由后续切片随 migration 落实。

---

## 5. 请求 / 响应 Schema 精确定义

### 5.1 `AgentResponse`（读，单个与列表元素共用）

```json
{
  "id": 1,
  "name": "acp-prod",
  "type": "acp",
  "endpoint": "https://agent.example.com/acp",
  "status": "active",            // active | inactive | error（见 §1.2）
  "config": { "protocol": "tencent-acp-sse", "timeout_s": 30 },
  "created_at": "2026-09-04T10:00:00+00:00",
  "updated_at": "2026-09-04T10:00:00+00:00"
}
```
**`credential_ref` 永不出现**（§2.1）。`status` 可为 `error`（运行态信号）。

### 5.2 `POST /api/agents` 请求

```json
{
  "name": "acp-prod",                       // 必填，非空
  "type": "acp",                            // 必填，∈ {acp}
  "endpoint": "https://agent.example.com/acp", // 必填，合法 http/https URL
  "status": "active",                       // 可选，∈ {active,inactive}，默认 active
  "config": { "protocol": "tencent-acp-sse" }, // 可选 dict
  "credential_ref": "vault://agent/acp-prod"   // 可选，写-only，非空串
}
```
- 成功 → `201 Created`，body = `AgentResponse`。
- 失败：重复 `name` → `409`；非法 `type`/`status`/`endpoint`/`credential_ref`/空
  `name` → `422`。

### 5.3 `PATCH /api/agents/{id}` 请求（全字段可选）

```json
{
  "name": "acp-prod-2",        // 可选；提供则非空 + 唯一
  "type": "acp",               // 可选；提供则 ∈ {acp}
  "endpoint": "https://...",   // 可选；提供则合法 URL
  "status": "inactive",        // 可选；提供则 ∈ {active,inactive}（拒绝 error）
  "config": { "timeout_s": 60 }, // 可选；提供则整体替换 dict
  "credential_ref": "vault://..." // 可选；写-only。缺省/null=不改；非空串=覆写
}
```
- **局部更新语义**：任何字段缺省 = 不改；只有显式提供的字段被覆写（对齐
  `UpdateAgent`）。
- 成功 → `200`，body = `AgentResponse`；目标不存在 → `404`；`name` 冲突 → `409`；
  非法值（含 `status=error`、`type` 越界、非法 URL、空白 `credential_ref`）→ `422`。

### 5.4 `GET /api/agents` 列表响应

```json
{
  "items": [ /* AgentResponse ... */ ],
  "total": 12
}
```
MVP 不做分页/过滤（11.1 判定无真实消费者、不过度建设）；`items` 中每一项同样
**不含 `credential_ref`**。

---

## 6. 错误码映射（Application → HTTP）

| Application 错误 | 触发 | HTTP |
| --- | --- | --- |
| `DuplicateAgentError` | 同名创建 / PATCH 改重名 | `409 Conflict` |
| `AgentNotFoundError` | `GET`/`PATCH` 目标 id 不存在 | `404 Not Found` |
| `InvalidAgentInputError` | 空字段 / `type` 越界 / `status=error` / 非法 URL / 空白 `credential_ref` / `config` 非 dict | `422 Unprocessable Entity` |

错误体统一形如 `{ "detail": "<message>", "field": "<可选字段名>" }`（具体 shape
由 11.3 实现定，这里只冻结「语义→码」映射）。

---

## 7. 边界守护（API 层不得越界）

- API 层（11.3 的 `api/management/agents.py` + schemas）**只**依赖
  `AgentApplicationService` 与其输入契约（`CreateAgent`/`UpdateAgent`）；
  **不** import ORM / SQLAlchemy / persistence 实现 / adapter。
- **绝不**在响应里序列化 `credential_ref`（schema 层面从响应模型排除）。
- **绝不**接受客户端写入 `error`（schema 枚举 + 服务守卫双重拦截）。
- 请求的 `id`/`created_at`/`updated_at` 由 schema 标记为只读/排除输入。
- 不新造错误类型：沿用 `DuplicateAgentError` / `AgentNotFoundError` /
  `InvalidAgentInputError`，由 API 层映射到 HTTP 码（§6）。

---

## 8. 待后续步骤落地清单（11.2 不建）

| 项 | 类型 | 落点 |
| --- | --- | --- |
| Pydantic 请求/响应 Schema + 4 个 FastAPI 路由 | 生产代码 | 11.3 |
| `status` 输入枚举 `{active,inactive}` + 拒绝 `error` 的校验 | 生产代码 | 11.3 |
| `type` 受控枚举 `AgentType={acp}` + 校验 | 生产代码 | 11.3 |
| `endpoint` 最小 URL 校验 | 生产代码 | 11.3（必要时下沉到 `application/agents.py` 校验） |
| 响应模型排除 `credential_ref` | 生产代码 | 11.3 |
| 错误映射（App→HTTP 409/404/422） | 生产代码 | 11.3 |
| 后端补 `list_agents`（port/service/repository） | 生产代码 | 随 11.3 |
| `agents.name` DB `UNIQUE` 约束（migration） | 生产代码 | 后续独立切片 |
| `runtime_status` 独立列（如需扩展运行态信号） | 生产代码 | 未来按需，**不预建** |

---

## 9. 验收（→ 11.3 实现 + 测试）

| # | 条件（契约已冻结，待 11.3 实现满足） |
| --- | --- |
| 1 | 端点面 = `POST/GET /api/agents`、`GET/PATCH /api/agents/{id}`；无 `DELETE` |
| 2 | `status` API 输入仅 `{active,inactive}`；`error` 拒收（422） |
| 3 | `credential_ref`：Create 可入；Get/List **永不**返回；Patch 同名字段、缺省/null=不改、非空=覆写 |
| 4 | `type` 受控 `{acp}`；越界 → 422 |
| 5 | `name` 重复 → 409；空/非法 → 422 |
| 6 | `endpoint` 最小 URL 校验（http/https + host）；非法 → 422 |
| 7 | `config` 可选 dict；非 dict → 422 |
| 8 | 响应含 `id/name/type/endpoint/status/config/created_at/updated_at`，**不含** `credential_ref` |
| 9 | 错误映射：Duplicate→409 / NotFound→404 / InvalidInput→422 |
| 10 | API 层不 import ORM/persistence/adapter；只读字段不出现在请求契约 |
