# Step 11.1 — Agent Management Vertical Slice Audit

> **审计步，生产代码改动：0 行。**
> 交付物 = 本报告 + 审计测试
> （`tests/test_step11_1_agent_management_vertical_slice_audit.py`）。
>
> 目标：回答「现有 Agent 的 Domain / ORM / Repository / Application Service /
> Schema / API / Frontend 基础，是否已足够支撑第一条 Agent Management 产品闭环？」
> ——并逐项裁决用户给出的检查清单。

---

## 1. 分层现状（逐层清点）

| 层 | 状态 | 事实 |
| --- | --- | --- |
| Agent Domain | ✅ **已存在且功能完整** | `domain/agent.py` frozen dataclass，7 字段 |
| ORM | ✅ **已存在且功能完整** | `persistence/models/agent.py` AgentModel，8 列（含 created_at/updated_at） |
| Repository Port | ✅ **已存在且功能完整** | `repositories/agent.py`：get_by_id / create / update / find_by_name |
| Repository Adapter | ✅ **已存在且功能完整** | `persistence/repositories/agent.py` SqlAlchemyAgentRepository |
| Application Service | ✅ **已存在且功能完整** | `application/agents.py` AgentApplicationService + 输入契约 + 校验 + 错误类型 |
| API Schema（Pydantic） | ❌ **缺失** | 全树无任何 agent 的 pydantic/response 模型 |
| HTTP API（FastAPI 路由） | ❌ **缺失** | `api/management/` 仅 `__init__.py`；`main.py` 只注册 health_router |
| Frontend | ❌ **缺失（占位）** | `frontend/` 仅 README 占位，无任何源码文件 |

**关键发现（与「从零建设」假设相反）**：Agent 的**后端链路**
Domain → ORM → Repository → Application Service **已经是一条完整、可运行、
带校验与去重守卫的闭环**——这不是要新建，而是要被 API + 前端消费。真正
缺失的是 **Schema / HTTP API / Frontend 三层**，以及服务/端口层面的
**List 与 Delete** 两个用例。

---

## 2. 检查清单逐项裁决

### 2.1 哪些已存在 / 空壳 / 缺失

| 项 | 判定 |
| --- | --- |
| Agent Domain | 已存在（完整） |
| ORM | 已存在（完整） |
| Repository（Port+Adapter） | 已存在（完整） |
| Application Service | 已存在（完整：Create/Get/Update + 校验 + DuplicateAgentError/AgentNotFoundError/InvalidAgentInputError） |
| `api/management/` 目录 | 空壳（仅 `__init__.py`，无任何 router/schema） |
| API Schema / HTTP API | 缺失 |
| Frontend | 缺失（占位） |
| List 用例 | 缺失（port/service 均无 `list_agents`） |
| Delete 用例 | 缺失（port/service 均无 `delete_agent`） |

### 2.2 哪些字段需要 API 暴露

`id` / `name` / `type` / `status` / `endpoint` / `config` —— 管理面可读写展示。

### 2.3 哪些字段应该隐藏

`credential_ref` —— **GET 列表/详情一律不出**（见 2.4）。

### 2.4 credential_ref 的 API 语义

- 它是**凭据引用**（指向外部 Secret 存储的标识），领域与 ORM 已承诺
  「只存引用、不存明文」；
- API 契约应延续这一边界：**写入型字段**——Create/Update 请求体**接收**
  `credential_ref`（让管理员录入引用），但**读取型响应隐藏**该字段（防止
  通过引用间接定位 Secret 存储）；
- 即「**引用入、引用不出**」。当前 API 层尚不存在，该契约待建 API 时落实。

### 2.5 Create / Get / List / Update / Delete

- **Create / Get / Update**：已在 Application Service 实现，必要；
- **List**：管理闭环**必要**（无 list 无法在 UI 陈列 / 选择 Agent），当前
  **缺失** → 缺口（建议随 API 切片补 `list_agents`）；
- **Delete**：MVP **不必要**——Agent 被 Connection 以 FK 引用，硬删需处理
  引用与历史 Session/Interaction。建议 MVP 用 `status="inactive"` 软下线
  代替硬删 → **DROP**（与 10.9「不为未来提前建设」一致；若未来确需合规
  删除再单列）。

### 2.6 status 谁可以修改

- **管理态**（`active` / `inactive`）：由**管理员经 API** 设置（软上线/下线）；
- **运行态**（`error` / `disabled` 等）：由 **Runtime 后端**在 dispatch 失败 /
  连接异常时设置，**不应由 API 任意写入**；
- 当前 `UpdateAgent.status` 是**裸字符串、无约束**——可写任意值、无法区分
  「管理态 vs 运行态」→ **缺口**：需限定允许集合 + 区分「谁能改什么」
  （API 仅允许 active/inactive；error 由后端写入）。

### 2.7 type 是否自由字符串

- 当前 `type` 是**自由字符串**（ORM `String(64)`，服务不校验），只有 `acp`
  一种适配器；
- 自由字符串 → 类型漂移 + Routing 无法可靠匹配 → **缺口**：建议收敛为
  **受控集合**（与 `adapters/agents/` registry 对齐的枚举/白名单），禁止
  任意新值。

### 2.8 name 唯一性

- 应用层已有 `find_by_name` 去重守卫（重复抛 `DuplicateAgentError`）；
- 但**无 DB 唯一约束** → 并发 race 可绕过应用守卫 → **小缺口**：补
  `name UNIQUE` 作为真正确定性来源，应用守卫保留为友好错误。

### 2.9 endpoint validation

- 当前仅 `_require_non_blank(endpoint)`（非空），**无格式校验**；
- → **小缺口**：补最小 URL 校验（scheme + host，不探活、不过度）。

### 2.10 真实 frontend consumer

- **否**——`frontend/` 仅占位 README，无任何源码；API 层同样无人消费。
- 这与 10.9 判定的 **G10 WATCH**（「最小读契约已冻结、无真实消费者」）互为
  印证：**管理能力的「生产方（后端）」已就绪，「消费方（API+前端）」缺席**。
  是否 NOW 取决于产品是否立项 Agent Management UI——本步不自称 NOW。

---

## 3. 闭环就绪度结论

```
Agent Management Vertical Slice
   │
   ├── 后端（Domain→ORM→Repository→App Service）：✅ 已就绪
   │     └── 仅缺 List / Delete 用例（List 必要；Delete DROP）
   │
   ├── API Schema / HTTP API：❌ 缺失（空壳目录）
   ├── Frontend：❌ 缺失（占位）
   │
   └── 政策缺口（待 API 切片一并收口）：
        ├── credential_ref：引用入、引用不出
        ├── status：受控集合 + 区分管理态/运行态
        ├── type：受控集合（对齐 adapter registry）
        ├── name：补 DB UNIQUE 约束
        └── endpoint：最小 URL 格式校验
```

**最小 API 切片建议（待消费者立项，不以 11.1 自称 NOW）**：
`api/management/agents.py` 建 Pydantic 请求/响应 Schema + 4 个路由
（POST /agents、GET /agents/{id}、GET /agents 列表、PATCH /agents/{id}），
响应体隐藏 `credential_ref`、暴露其余字段；后端补 `list_agents`；
`status`/`type`/`name`/`endpoint` 的约束在 API+服务层一并落地。该切片即
10.9 中 G10 升级 NOW 时的自然落点。

---

## 4. 验收（→ 测试 12 项）

| # | 条件 | 测试 |
| --- | --- | --- |
| 1 | Agent Domain 字段面 == 7 字段 | A1 |
| 2 | ORM 含 created_at/updated_at（审计可观测） | A2 |
| 3 | Repository Port 方法面 == {get_by_id,create,update,find_by_name} | A3 |
| 4 | Application Service 提供 create/get/update + 输入契约 + 三错误类型 | A4 |
| 5 | `api/management/` 仅 `__init__`（空壳） | B1 |
| 6 | `main.py` 仅注册 health_router（HTTP API 缺失） | B2 |
| 7 | frontend 仅为占位（无真实源码文件） | B3 |
| 8 | List / Delete 用例缺失（port/service 均无） | B4 |
| 9 | credential_ref 为引用字段、无明文；API 层尚不存在（引用入、引用不出契约待落实） | C1 |
| 10 | 政策缺口事实：type 自由字符串 / status 裸字符串无约束 / name 无 DB UNIQUE / endpoint 仅非空 | D1 |
| 11 | 闭环就绪度结论 + 最小 API 切片建议成文 | E1 |
| 12 | 全量回归 | 全量 |
