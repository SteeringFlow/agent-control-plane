# Cloud-0.1.2.3 —— Connect Agent Golden Path Architecture Gate

> 阶段：Architecture Gate（设计 + 影响审计）。**本次只交付设计裁决 + 机器化护栏，不实现。**
> 方法：只读审计 + 机器化护栏（`tests/test_cloud_0_1_2_3_connect_agent_golden_path_architecture_gate.py`）。
> 配套契约：ACP V0.1 RC → ACP V0.2 Artifact Contract → Cloud-0.1.1-A/B/C（已 GO 且落地）
> → Cloud-0.1.2（Workspace & Golden Path Gate，GO）→ Cloud-0.1.2.1（Registration → Workspace Entry，已落地）
> → Cloud-0.1.2.2（Connect Agent Architecture Gate，GO：凭据归 Connection / 两事实分离 / WorkBuddy=Agent(acp) / 不新增 secret 列 / 三轴分离）
> → Cloud-0.1.2.2.1（credential_ref 可选对齐，已落地）
> → Cloud-0.1.2.2.2（Connection Health Gate，GO：派生诊断非持久状态机）
> → Cloud-0.1.2.2.2.1（Connection Health Probe 实现，已落地）
> → **本 Gate（Connect Agent Golden Path：编排既有 Core 能力 + 裁决事务边界 / 孤儿 Agent 语义）**。
>
> **本 Gate 的中心问题（单一裁决标尺）**：
>
> ```
> Connect Agent（产品单页）
>      ↓
>   Create Agent              ← POST /api/agents            （独立事务，已 commit）
>      ↓
>   Create Connection         ← POST /api/connections       （独立事务，已 commit）
>      ↓
>   Health Probe（可选）       ← GET  /api/connections/{id}/health  （即时快照，不落库）
>      ↓
>   Connected（瞬时 UI 状态，派生）
> ```
>
> 一句话：**Cloud 不要因为「Connect Agent」这个产品闭环，就偷偷长出第二套编排实体 /
> 事务管理器 / onboarding 状态机 / 原子 connect 端点。四个基础事实（Agent /
> Connection / 可选 credential_ref / config）已由 0.1.2.2 裁决并验证，本 Gate 只审计
> 「用户操作流程如何编排既有 Core 能力」以及「部分成功后的孤儿 Agent 如何被产品语义接纳」。**

---

## 0. 结论先行（GO / BLOCKED）与 9 问裁决

**GO。** 对现有代码库做只读审计后确认，产品闭环可由**纯前端编排 + 既有端点**完成，且
「部分成功」作为结构化事实被接纳。9 个待裁决问题的最小安全边界全部可定：

| # | 问题 | 裁决 |
|---|---|---|
| Q1 | 单页 UI 是否只是 orchestration？ | **是**。仅编排 `POST /api/agents` → `POST /api/connections` →（可选）`GET /{id}/health`；不新增后端实体 / 端点 / 事务管理器。 |
| Q2 | 创建 Agent 成功、Connection 失败时如何处理？ | **接纳孤儿 Agent，不回滚**。两写是两个独立事务，结构性无法原子；失败 = 用户可见的产品状态，而非需自动回滚的错误。 |
| Q3 | 是否允许「只有 Agent 没有 Connection」的中间事实？ | **允许，且是预期态**。Agent = 注册身份；Connection = 到渠道的受控路径；domain 本就不要求 Agent 必有 Connection。 |
| Q4 | Health Probe 是否必须由用户主动点击？ | **否，非强制**。成功后 UI 可自动探活作为即时反馈；用户也可手动重探。探针是**建议性**的，绝不成为 Connection 创建的门禁。 |
| Q5 | Connected 是否只是 Probe 的瞬时 UI 状态？ | **是**。`Connected` 由最近一次 `GET /{id}/health` 快照（`reachable=true`）派生，不落库；持久事实仍为 `Connection.status=active`。 |
| Q6 | 失败是否允许 retry？ | **允许，= 既有端点的重新提交**。重 `POST /api/connections`（重复绑定 → 409，前端判为已连）；重 `GET /{id}/health`（建议性）。不新增 retry 框架 / 重试循环。 |
| Q7 | 是否需要 delete/rollback？ | **否，不新增回滚 / Saga**。`UnitOfWork` 已显式禁止 Saga/Outbox/分布式事务。清理 = 复用既有软下线（`PATCH status=inactive`）；硬删除是独立 follow-up（F-2），非事务机制。 |
| Q8 | 是否新增任何 onboarding 状态？ | **否**。与 0.1.2 G6 / 0.1.2.2.2 一致：无 `onboarding_state` / `connect_status` / `provisioning` / `first_success` 字段或表。 |
| Q9 | 是否触碰已有 Agent/Connection API 合同？ | **否**。`POST /api/agents`、`POST /api/connections`、`GET /{id}/health`、`PATCH /api/agents/{id}` 全部按 11.3 / 11.11 / 0.1.2.2.2.1 冻结合同复用，无合同变更、无新端点、无新字段。 |

**核心裁决（本次 Gate 的灵魂）**——见 §3：孤儿 Agent 不是 bug，是 Golden Path 第一个
**显式事务边界**；我们把它变成用户可见、可恢复的产品语义，而不是藏在一个伪原子操作背后。

**两处已知偏差（follow-up，不阻塞 GO）**：
- **F-1（合同澄清，非阻塞）**：`AgentApplicationService.create_agent` 当前**强制 `endpoint` 非空白**；而 WorkBuddy 配方里 ACP endpoint 实际落在 `Connection.config`。Golden Path 的「Connect WorkBuddy」配方需决定 Agent 携带什么 `endpoint`（要么前端把 ACP endpoint 同步传给 Agent.endpoint，要么后续把 Agent.endpoint 降级为可选）——属合同澄清，不影响本 Gate 安全边界。
- **F-2（可选清理，非阻塞）**：当前 Agent / Connection 均无硬 `DELETE` 端点（设计上"软下线 = `PATCH status=inactive`"）。若产品希望用户彻底移除孤儿 Agent，是**独立的窄 follow-up（加 DELETE 端点）**，明确**不是**事务/回滚机制，不在本 Gate 范围。

---

## 1. 链路固定（冻结）

Cloud V0 「Connect Agent」目标链（与 0.1.2.2 §1 一脉相承，本次聚焦「编排」与「部分成功」）：

```
Browser
  ↓  Browser Authentication (C.1, L1+L2)
  ↓  AuthenticatedUser(user_id, tenant_id)
  ↓  TenantScope(tenant_id)                      ← 复用 B
  ↓  Connect Agent（产品单页，纯前端编排）
        ├─ POST /api/agents        {name, type, endpoint, config?}
        │       ↓ 201  → agent_id
        ├─ POST /api/connections   {channel_id, agent_id, name, config{含凭据}}
        │       ↓ 201  → connection_id   （可能 404/409/422 → 部分成功）
        └─ GET  /api/connections/{id}/health   （可选 · 建议性 · 不落库）
                ↓ 200 {reachable, checked_at, detail, probe}
  ↓  Connection = Channel ↔ Agent 受控访问路径（承载凭据）
  ↓  AgentAdapter.send(request, connection)      ← 只从 connection 取凭据
  ↓  Runtime → Interaction → First Success（派生）
```

核心裁决：

> **「Connect Agent」是既有两个独立事实（Agent、Connection）的 UX 编排，
> 不是一个新事务、不是新实体、不是新端点。** 部分成功（Agent 成、Connection 败）
> 是结构化事实，必须由产品语义显式接纳，而非被自动回滚掩盖。

---

## 2. Q1 — 单页 UI 是否只是 orchestration？

### 裁决

**是。Connect Agent 单页 = 前端对既有端点的顺序编排，后端零新增。**

- 前端只调用三个既有端点：`POST /api/agents`（11.3 冻结）、`POST /api/connections`
  （11.11 冻结）、`GET /api/connections/{id}/health`（0.1.2.2.2.1 实现）。
- **不新增**任何后端实体（`ConnectAgent` / `GoldenPath`）、**不新增**任何后端端点
  （`POST /api/connect` / `connect_agent`）、**不新增**任何事务编排器
  （`ConnectService` / `ConnectController` / `ConnectWorkflow`）。
- 这把 0.1.2.2 的红线和 0.1.2 的红线的交集锁死：不因「产品闭环」而长出第二套
  Agent/Connection 模型（0.1.2.2），也不因「onboarding」而长出第二套 workspace /
  onboarding 模型（0.1.2 G6）。

### 实证（readonly 审计）

- `app/api/management/agents.py` / `connections.py`：端点面严格为
  `POST/GET/GET{id}/PATCH` + `GET {id}/health`；**无任何** `connect*` 路由或方法。
- `app/application/agents.py` / `connections.py`：`create_agent` / `create_connection`
  各自独立存在，**不存在** `create_connect_agent` / `connect()` 之类跨事实编排方法。
- 全 `app` 标识符扫描：无 `ConnectAgent` / `GoldenPath` / `ConnectController` /
  `ConnectService` / `ConnectWorkflow` / `Provisioning` / `Onboarding` 类。

### 护栏

- `test_connect_agent_is_pure_frontend_orchestration`：Q1/Q9 —— 全 app 无越界编排类 /
  方法；`build_agents_api` / `build_connections_api` 是唯一组合根；无 `connect_agent` 路由。

---

## 3. Q2 / Q3 / Q7 — 部分成功后的孤儿 Agent（本次 Gate 核心）

### 裁决：接纳孤儿 Agent，不回滚；不做 Saga

这是整个 Golden Path 第一个真正的事务边界 / 产品语义问题。审计代码后确认：

1. **两写是两个独立事务，结构性无法原子。**
   - `AgentApplicationService.create_agent`（agents.py L161）`with self._uow_factory() as uow: … uow.commit()`
     —— 自开事务、自提交。
   - `ConnectionApplicationService.create_connection`（connections.py L135）`with self._uow_factory() as uow: … uow.commit()`
     —— 自开事务、自提交。
   - 两者经各自的 `uow_factory` 进入独立 Session；Agent 提交后，Connection 失败**无法**回滚 Agent。
2. **`UnitOfWork` 已显式禁止事务抽象。** 其模块 docstring（unit_of_work.py L23-25）
   明文拒绝 `TransactionManager / TransactionService / 分布式事务 / Saga / Outbox /
   Event Sourcing / CQRS / Async / Retry 框架`。引入 Saga/补偿事务 = 推翻既有架构纪律。
3. **Agent 无 Connection 在 domain 本就合法。** `Connection` 是独立实体，经
   `UNIQUE(channel_id, agent_id)` 自证；domain / schema 没有任何「Agent 必有 Connection」
   的约束。Agent = 注册身份，Connection = 到渠道的受控路径，二者本就分离（0.1.2.2 ②）。

**因此裁决：**

- **孤儿 Agent 是预期的产品状态，不是错误。** 流程不自动回滚 Agent。
- 前端在 Connection 失败时展示：**「Agent ✓ 已创建；Connection ✗ 失败（原因：404/409/422）」**，
  把事务边界**显式、用户可见**地呈现。
- **恢复只复用既有端点（无任何新子系统）：**
  - **重试 Connection**：用已有 `agent_id` 重 `POST /api/connections`。重复绑定 →
    `DuplicateConnectionError` → 409（前端判「已连接」），属幂等重提交，**不是**新 retry 框架。
  - **搁置孤儿 Agent**：`PATCH /api/agents/{id}` `status=inactive`（既有软下线纪律；
    无硬 DELETE 是设计选择，见 F-2）。

**绝不做：**

- ❌ 在 `create_connection` 失败时「自动删除刚创建的 Agent」（补偿事务 / 隐式回滚）。
- ❌ 引入 `Saga` / `Outbox` / `TransactionManager` / 补偿 handler。
- ❌ 造一个原子的 `POST /api/connect`（把两事实合成一写）——那会破坏 0.1.2.2 ② 的两事实分离。

### 护栏

- `test_two_fact_creation_are_separate_no_saga`：Q2/Q3 —— 全 app 无 `TransactionManager` /
  `Saga` / `Outbox` / `Compensating` / `DistributedTransaction` 类；`create_agent` /
  `create_connection` 各自 `with self._uow_factory() as uow` 自提交；`connections.py` 不调用 `create_agent`。
- `test_orphan_agent_recovery_via_existing_endpoints_no_rollback`：Q2/Q3/Q7 —— 软下线存在
  （`update_agent` 支持 `status="inactive"`，`ALLOWED_MANAGEMENT_STATUSES` 含 `inactive`）；
  重提交路径存在（`DuplicateConnectionError` 可导入 = 409 幂等重提交）；`agents.py` / `connections.py`
  **无** `delete_agent` / `delete_connection` / `rollback_agent` / 补偿方法。

---

## 4. Q4 — Health Probe 是否必须由用户主动点击？

### 裁决：非强制，建议性，绝不成为门禁

- 探针 `GET /api/connections/{id}/health`（0.1.2.2.2.1 已实现）是**即时诊断快照**
  （`ConnectionHealthResponse{connection_id, reachable, checked_at, detail, probe}`），
  **不写库、不改 `Connection.status`、不成状态机**（0.1.2.2.2 冻结）。
- **Connection 创建成功（201）即成立**，与探针结论无关。`reachable=false` 不撤销 Connection。
- 产品建议：
  - 成功后 UI **可自动**调一次 health 作为即时反馈（"Connected" / "Unreachable" 徽标）。
  - 用户**也可手动**点「检查连接」重探（建议性）。
  - 探针 503（未装配）/ 不可达 → 前端提示，**不阻断**已创建的 Connection。

### 护栏

- `test_health_probe_is_advisory_not_a_creation_gate`：Q4/Q5 —— `connections.py` 中
  `create_connection` 路由**不**引用 `probe(`（探针只在 health 路由调用）；`ConnectionHealthResponse`
  可导入；`Connection` 域无 `connected` / `health_status` 字段。

---

## 5. Q5 — Connected 是否只是 Probe 的瞬时 UI 状态？

### 裁决：是，派生、瞬时、不持久

- `Connected` 徽标 = 最近一次 `GET /{id}/health` 快照的 `reachable=true` 视图态。
- 持久事实只有 `Connection.status ∈ {active, inactive}`（配置态，11.11 冻结）。
- 这与 0.1.2.2.2 的三轴分离一脉相承：运行健康（Runtime Health）= 派生诊断，永不落在
  Connection 上。`checked_at` 由请求时刻生成，绝不落库。

### 护栏

- `test_connected_is_transient_probe_state_not_persisted`：Q5 —— `AgentProbeSnapshot`
  含 `reachable`/`probe`；`ConnectionHealthResponse` 含 `reachable`/`checked_at`；
  `Connection` 域无 `connected` 字段；`Connection.status` 默认 `active`，
  `CONNECTION_MANAGEMENT_STATUSES == {"active","inactive"}`。

---

## 6. Q6 — 失败是否允许 retry？

### 裁决：允许，= 既有端点的重新提交

- **Connection 创建失败 → 重试** = 重 `POST /api/connections`（同 `agent_id`/`channel_id`）。
  重复绑定 → `DuplicateConnectionError` → 409，前端可判「已连接」，属安全幂等重提交。
- **Health 不可达 → 重探** = 重 `GET /{id}/health`，建议性、不影响 Connection。
- **不新增** retry 循环 / `RetryPolicy` / `RetryManager` / 指数退避框架。重试是用户/前端
  发起的「再提交一次」，不是自动重试机器。

### 护栏

- `test_retry_is_resubmission_via_existing_endpoints`：Q6 —— `DuplicateConnectionError`
  可导入（=409 幂等重提交信号）；`get_connection_health` 路由存在（可重探）；
  全 app 无 `RetryManager` / `RetryPolicy` 类。

---

## 7. Q8 — 是否新增任何 onboarding 状态？

### 裁决：否

与 0.1.2 G6（First Success 派生）及 0.1.2.2.2（Health 派生）完全一致：

- `Agent` 域字段：`name/type/id/endpoint/status/config/credential_ref/created_at/updated_at`
  —— 无 `onboarding_state` / `connected` / `provisioning` / `connect_status` / `first_success`。
- `Connection` 域字段：`channel_id/agent_id/name/id/status/config/credential_ref/
  created_at/updated_at` —— 同上，无此类字段。
- 无 `onboarding_states` / `connect_statuses` / `provisioning` 表或域类。
- 「已连接」由 health 探针派生（§5），「首次成功」由 Interaction 派生（G6）——两者都不是实体。

### 护栏

- `test_no_onboarding_state_or_connected_field`：Q8 —— `Agent` / `Connection` 域无
  `onboarding_state`/`connected`/`provisioning`/`connect_status`/`first_success` 字段；
  全 app 无 `OnboardingState` / `ConnectStatus` / `Provisioning` 类。

---

## 8. Q9 — 是否触碰已有 Agent/Connection API 合同？

### 裁决：否，完全复用冻结合同

Golden Path 仅消费以下**已冻结**端点，无任何合同变更、无新端点、无新字段：

| 端点 | 合同源 | 本 Gate 用法 |
|---|---|---|
| `POST /api/agents` | 11.3 冻结 | 创建 Agent（name/type/endpoint/config?） |
| `POST /api/connections` | 11.11 冻结 | 创建 Connection（channel_id/agent_id/name/config） |
| `GET /api/connections/{id}/health` | 0.1.2.2.2.1 实现 | 可选即时诊断 |
| `PATCH /api/agents/{id}` | 11.3 冻结 | 孤儿 Agent 软下线（status=inactive） |

不新增字段（如 `connect_status` / `onboarding_state`）、不新增端点（如 `POST /api/connect`）、
不修改契约校验语义。F-1（Agent.endpoint 是否强制）是**合同澄清**而非合同破坏，且不影响 GO。

### 护栏

- `test_golden_path_reuses_existing_api_contracts`：Q9 —— `AgentCreate` / `ConnectionCreate` /
  `ConnectionHealthResponse` 可导入且字段面不变；`build_agents_api` / `build_connections_api`
  为唯一组合根；management API 无 `connect*` 路由。

---

## 9. 中央裁决收口：事务边界 = 用户可见的产品语义

本次 Gate 把 0.1.2.2 的「两事实分离」推进到「两事实的**顺序提交 + 部分成功接纳**」：

```
Agent  (事务 A · 已提交)          ← 注册身份（可独立存在）
Connection (事务 B · 可能失败)     ← 受控访问路径（依赖已存在的 Agent）
        ↓ 部分成功 → 孤儿 Agent = 预期产品态（显式展示 + 重试/搁置恢复）
Health Probe (即时快照 · 不落库)   ← 建议性，非门禁
Connected (UI 派生态)              ← 由最近一次快照 reachable=true 得出，不持久
```

**铁律**：Golden Path 不引入任何让「部分成功」隐形的机制（自动回滚 / 原子端点 / Saga）。
部分成功是 Cloud 产品语义的第一课——它诚实、可恢复、且复用既有 CRUD。

---

## 10. 机器化护栏（Verification Gate）

新增 `tests/test_cloud_0_1_2_3_connect_agent_golden_path_architecture_gate.py`
（**静态 + 行为护栏，当前即可绿灯**，锁死 Q1–Q9 与中央裁决）：

1. `test_connect_agent_is_pure_frontend_orchestration`：Q1/Q9 —— 全 app 无越界编排类
   （`ConnectAgent`/`GoldenPath`/`ConnectController`/`ConnectService`/`ConnectWorkflow`/
   `Provisioning`/`Onboarding`）；management API 无 `connect_agent` 路由；组合根仅为
   `build_agents_api` / `build_connections_api`。
2. `test_no_onboarding_state_or_connected_field`：Q8 —— `Agent`/`Connection` 域无
   `onboarding_state`/`connected`/`provisioning`/`connect_status`/`first_success`；
   全 app 无 `OnboardingState`/`ConnectStatus`/`Provisioning` 类。
3. `test_two_fact_creation_are_separate_no_saga`：Q2/Q3 —— 全 app 无 `TransactionManager`/
   `Saga`/`Outbox`/`Compensating`/`DistributedTransaction`；`create_agent`/`create_connection`
   各自 `with self._uow_factory() as uow` 自提交；`connections.py` 不调用 `create_agent`。
4. `test_orphan_agent_recovery_via_existing_endpoints_no_rollback`：Q2/Q3/Q7 —— 软下线存在
   （`update_agent` 支持 `status="inactive"`，`ALLOWED_MANAGEMENT_STATUSES` 含 `inactive`）；
   重提交信号 `DuplicateConnectionError` 可导入；`agents.py`/`connections.py` 无
   `delete_agent`/`delete_connection`/`rollback_agent`/补偿方法。
5. `test_health_probe_is_advisory_not_a_creation_gate`：Q4/Q5 —— `connections.py` 中
   `create_connection` 路由不引用 `probe(`；`ConnectionHealthResponse` 可导入；
   `Connection` 域无 `connected`/`health_status`。
6. `test_connected_is_transient_probe_state_not_persisted`：Q5 —— `AgentProbeSnapshot`
   含 `reachable`/`probe`；`ConnectionHealthResponse` 含 `reachable`/`checked_at`；
   `Connection` 域无 `connected`；`Connection.status` 默认 `active`，
   `CONNECTION_MANAGEMENT_STATUSES == {"active","inactive"}`。
7. `test_retry_is_resubmission_via_existing_endpoints`：Q6 —— `DuplicateConnectionError`
   可导入；`get_connection_health` 路由存在；全 app 无 `RetryManager`/`RetryPolicy`。
8. `test_golden_path_reuses_existing_api_contracts`：Q9 —— `AgentCreate`/`ConnectionCreate`/
   `ConnectionHealthResponse` 可导入且字段面不变；management API 无 `connect*` 路由。

进入实现期时，本护栏保持绿；并按演进纪律，在「Connect Agent 前端编排」「孤儿 Agent 展示/
恢复 UI」落地后，把对应的「实现后不变量」（如前端双步进度、409 重提交处理）补入该测试。

---

## 11. 已知偏差与 follow-up（不阻塞 GO）

| # | 偏差 | 影响 | 修复（后续 sub-step） |
|---|---|---|---|
| F-1 | `create_agent` 强制 `endpoint` 非空白；WorkBuddy 配方里 ACP endpoint 落在 `Connection.config` | Golden Path「Connect WorkBuddy」需明确 Agent.endpoint 取值 | 实现期决定：前端把 ACP endpoint 同步传给 Agent.endpoint，或后续把 `Agent.endpoint` 降级为可选（独立裁决）。属合同澄清，不改本 Gate 安全边界。 |
| F-2 | Agent / Connection 均无硬 `DELETE` 端点（设计上软下线 = `PATCH status=inactive`） | 孤儿 Agent 当前只能「搁置」不能「彻底删除」 | 若产品要求硬删除，是**独立窄 follow-up（加 DELETE 端点）**，明确非事务/回滚机制，不在本 Gate。 |

F-1/F-2 均不改变安全边界：凭据仍只归 Connection、两事实仍分离、Health 仍派生不持久、
无第二套 onboarding 状态、无第二套事务模型。

---

## 12. NOT TOUCHED（冻结边界，与 B/C/0.1.2/0.1.2.2 一致并扩展）

| 类别 | 文件 / 范围 | 理由 |
|---|---|---|
| **Domain 全树** | `app/domain/**`（`agent.py`/`connection.py`/…） | 不新增 `ConnectAgent` / `OnboardingState` / `Connected` 域实体；两事实分离纪律本就成立。 |
| **Application 两 Service** | `app/application/agents.py`、`connections.py` | 不合并成 `ConnectAgentService`；不新增 `connect()` 跨事实编排方法；不新增 Saga/回滚。 |
| **Tenant Isolation** | `app/core/tenant.py`、`unit_of_work.py`、0002 | 复用 B，不重写。 |
| **Browser Authentication** | `app/core/browser_auth.py`、`app/api/auth.py`、0003 | 复用 C/C.1，不重写。 |
| **Runtime 契约 / Adapter** | `app/adapters/*`、`agent_runtime_sdk/**`、Worker/Driver | 经 tenant-scoped UoW 隔离，逻辑与签名不变；`ACPAdapter.probe` 仅从 Connection 取凭据。 |
| **8 Core Table + tenants + user_credentials + browser_sessions + agents/connections 列面** | 已建 | 本 Gate **无 schema 变更**（不新增 onboarding/connected/health 列）。 |
| **config 脱敏纪律** | `sanitize_*` | 沿用 16.1.2 / 0.1.2.2 ④，不重写。 |
| **Health Probe 实现** | `GET /{id}/health`、`ACPAdapter.probe`、`AgentProbeSnapshot` | 0.1.2.2.2.1 已落地，本 Gate 仅复用，不改写。 |

---

## 13. GO / 边界闭合

**GO。** 9 个待裁决问题全部给出最小安全边界，且能证明：

- **Q1 纯编排**：Connect Agent 单页只调既有端点；后端零新增实体/端点/事务器。
- **Q2/Q3 接纳孤儿 Agent**：两写是两个独立事务（各自 `uow_factory`+`commit`），结构性无法原子；
  `UnitOfWork` 已禁止 Saga/Outbox；孤儿 Agent 是预期产品态，显式展示 + 重试/搁置恢复。
- **Q4 探针非强制**：health 是建议性即时快照，绝不成为 Connection 创建的门禁。
- **Q5 Connected 瞬时派生**：由最近一次 `GET /{id}/health` 快照派生，不落库；持久事实仍是
  `Connection.status=active`。
- **Q6 重试 = 重提交**：复用 `POST /api/connections`（409 幂等）/ `GET /{id}/health`；无 retry 框架。
- **Q7 无 delete/rollback**：不引入回滚/Saga；清理用既有软下线；硬删除是独立 F-2。
- **Q8 无 onboarding 状态**：无 `onboarding_state`/`connected`/`provisioning` 字段或表。
- **Q9 不碰合同**：完全复用 11.3 / 11.11 / 0.1.2.2.2.1 冻结端点。

机器化护栏（§10）当前即绿灯，锁死 Q1–Q9 与「不建第二套编排/事务/onboarding 模型」。
已知偏差 F-1/F-2 已列入 follow-up，**不阻塞 GO**——它们不改变安全边界，仅属合同澄清与
可选的独立清理端点。
