# Cloud-0.1.2.2 —— Connect Agent Architecture Gate

> 阶段：Architecture Gate（设计 + 影响审计）。**本次只交付设计裁决 + 机器化护栏，不实现。**
> 方法：只读审计 + 机器化护栏（`tests/test_cloud_0_1_2_2_connect_agent_architecture_gate.py`）。
> 配套契约：ACP V0.1 RC → ACP V0.2 Artifact Contract → Cloud-0.1.1-A/B/C（已 GO 且落地）
> → Cloud-0.1.2（Workspace & Golden Path Gate，GO）→ Cloud-0.1.2.1（Registration → Workspace Entry，已落地）
> → **本 Gate（Connect Agent）**。
>
> **本 Gate 的中心问题（单一裁决标尺）**：
>
> ```
> AuthenticatedUser(user_id, tenant_id)
>   ↓  TenantScope(tenant_id)                 ← 复用 B，唯一 tenant 原语
>   ↓  Agent                                ← 目标身份 / 注册
>   ↓  Connection (Channel ↔ Agent)         ← 受控访问路径（承载凭据）
>   ↓  AgentAdapter                          ← 协议翻译（只从 Connection 取凭据）
>   ↓  Runtime
> ```
>
> 一句话：**Cloud 不要因为「Connect Agent」这个产品动作，就偷偷长出
> 第二套 Agent / Connection / Secret 模型，或把 User 提供的凭据错误安放。**
> 本次把这条红线机器化锁死。

---

## 0. 结论先行（GO / BLOCKED）

**GO。** 对现有代码库做只读审计后确认，5 个待裁决问题的最小安全边界全部可定：

- **① Agent Credential 归属于 Connection**，不归 Agent。运行时 `ACPAdapter` 只从
  `Connection.config` / `Connection.credential_ref` 取凭据（实证：`app/adapters/agents/acp.py`）；
  `Agent.credential_ref` 在 runtime 从未被消费——它是占位，不是凭据载体。
- **② Agent 创建 与 Connection 创建 是两个独立事实**，产品「Connect Agent」单页只是
  UX 编排。Domain/Application 保持两个独立实体、两个独立 Service。
- **③ WorkBuddy = Agent(type="acp")**；Bot ID / Secret / ACP endpoint 全部落在
  `Connection.config`，不造第二套 `WorkBuddy*` / `Bot` / `Integration` 类。
- **④ Credential 不进 ACP DB 的新列**。`connections` / `agents` 都不新增 `secret` /
  `credential` / `api_key` / `token` 列；继续用 `config`（敏感键读时脱敏）+ 可选
  `credential_ref`（opaque 引用，明文永不入库）。不建 Secret 平台。
- **⑤ Connection Status 三轴分离**：Configuration State（`active`/`inactive`，可持久）
  ≠ Runtime Health（派生诊断，不持久、不成状态机）≠ Interaction Outcome
  （`processing_status` / `delivery_status`，已在 `Interaction` 上，Connection 不吸收）。

**一处已知偏差（不影响 GO，列入 follow-up 0.1.2.2.1）**：当前 `AgentApplicationService`
/`ConnectionApplicationService` 在创建时**强制要求**非空白 `credential_ref`。这与 ①（Agent
不承载凭据）及 WorkBuddy 示例（`POST /api/agents` 只传 `name`+`type`）矛盾。该要求应降级为
**可选**（config 已承载真实 secret）。详见 §9。

---

## 1. 链路固定（冻结）

Cloud V0 「Connect Agent」目标链：

```
Browser
  ↓  Browser Authentication (C.1)
  ↓  AuthenticatedUser(user_id, tenant_id)
  ↓  TenantScope(tenant_id)                      ← 复用 B
  ↓  Connect Agent（产品单页）
        ├─ Create Agent(name, type)              ← 身份 / 注册，无 secret
        └─ Create Connection(channel_id, agent_id, config{含凭据})
  ↓  Connection = Channel ↔ Agent 受控访问路径（承载凭据）
  ↓  AgentAdapter.send(request, connection)      ← 只从 connection 取凭据
  ↓  Runtime → Interaction → First Success（派生）
```

核心裁决（与 B/C/0.1.2 一脉相承）：

> **Agent 是「目标身份 / 注册」，Connection 是「到该目标的受控、已认证访问路径」。
> 凭据属于「路径」，不属于「目标」。**

---

## 2. ① Agent Credential 到底属于哪里？（最小安全边界）

### 裁决

**凭据（api_key / token / secret / bearer_token）只属于 `Connection`，不归 `Agent`。**

- `Agent` 承载的是**公开可达的注册信息**：`name` / `type` / `endpoint`（可公开）/ `config`（非凭据
  类配置）。`Agent` **不承载任何 secret**。
- 凭据的两个合法落点（二者选一，互斥演进）：
  1. **`Connection.config`**（V0 默认）：secret 直接存于 config JSON，响应**读时脱敏**
     （`sanitize_connection_config`），写时拒哨兵 `********`；runtime 经 `ACPAdapter`
     直读使用，永不进入任何管理响应。这与 `Channel.config` 承载 `corpsecret`/`token` 是同一纪律。
  2. **`Connection.credential_ref`**（前向兼容）：指向外部 Vault 的 opaque 引用
     （`"vault://..."`）。V0 **不实现** Vault 解析——引用只是占位，明文永不入库。
- **不引入** `SecretManager` / `CredentialVault` / `OAuthCredential` / `CredentialProvider`
  平台。V0 最小安全边界 = 既有「config 内敏感键脱敏 + credential_ref 引用不解析」纪律
  （10.1.5 凭证安全边界 / 16.1.2 config 脱敏）。真正的 Vault 集成是**独立的后续 Gate**，
  不在 V0。

### 实证（readonly 审计）

- `app/adapters/agents/acp.py::_send_tencent_acp_sse`：
  `config = connection.config ... endpoint = config.get("endpoint") ... token = config.get("bearer_token")`
  —— 凭据只来自 `Connection`。
- `app/adapters/agents/acp.py::_compose_acp_request`：
  `if isinstance(connection.credential_ref, str) and connection.credential_ref: headers["X-Acp-Credential-Ref"] = ...`
  —— 也只来自 `Connection`。
- 全代码库 **无任何** `agent.credential_ref` / `request.agent.credential_ref` 的读取。
  → `Agent.credential_ref` 在 runtime 是死字段（占位仪式），应解除其创建期强制要求（§9）。

### 护栏（见 §8 测试清单）

- `test_agent_carries_no_credential_but_optional_reference`：Agent 域/响应不含 secret 字段；
  `credential_ref` 是 optional 引用（不在 `AgentResponse` 字段面）。
- `test_runtime_reads_credential_from_connection_not_agent`：静态确认 `ACPAdapter` 从
  `connection.config` / `connection.credential_ref` 取凭据，且不引用 `agent.credential_ref`。

---

## 3. ② Agent 创建 与 Connection 创建 是否分离？

### 裁决

**分离为两个事实；产品层单页编排，Domain 绝不合二为一。**

- Domain / Application 维持两个独立实体 + 两个独立 Service：
  - `AgentApplicationService.create_agent`：校验 `name`/`type`/`endpoint`/`status`/
    `config`，按 `name` 去重；**不复制** Connection 逻辑。
  - `ConnectionApplicationService.create_connection`：校验 `channel_id` / `agent_id` 存在 +
    `(channel_id, agent_id)` 唯一预检 → 写入。
- 产品「Connect Agent」页面 = 一个 UX 编排：先 `create_agent`（按 name 去重，已存在则复用）
  → 再 `create_connection`（`channel_id` + `agent_id` + 含凭据的 `config`）。**两条写、两个事实**
  在 UI 后被顺序提交，但 Domain 层绝不把它们合并成 `ConnectAgentEntity`。
- **不变式保持**：`Connection.(channel_id, agent_id)` 为 immutable binding（改绑定 = 删旧 +
  建新），UNIQUE 约束仍为并发硬保证。

### 护栏

- `test_agent_and_connection_are_separate_facts`：`Agent` / `Connection` 为独立域类；
  `AgentApplicationService` 与 `ConnectionApplicationService` 均存在且互补（前者不持有 channel，
  后者不持有 agent 名/type）。
- `test_connection_create_requires_pre_existing_agent`：`create_connection` 在 agent 不存在时抛
  `AgentNotFoundError`（即「先有 Agent 再有 Connection」，两事实顺序依赖成立）。

---

## 4. ③ WorkBuddy 应该如何进入这个模型？

### 裁决

**WorkBuddy = `Agent(type="acp")`；Bot ID / Secret / ACP endpoint 全部落在 `Connection.config`。**
不造 `WorkBuddyAgent` / `WorkBuddyIntegration` / `Bot` / `CloudWorkBuddy` 第二套类。

映射（与 `examples/wecom-workbuddy/README.md` 完全一致）：

| WorkBuddy 概念 | 落点 | 字段 |
|---|---|---|
| WorkBuddy Bot ID | `Connection.config` | `runtime_id` |
| WorkBuddy Secret | `Connection.config` | `bearer_token`（脱敏） |
| ACP endpoint | `Connection.config` | `endpoint` |
| 协议 | `Connection.config` | `protocol="tencent-acp-sse"` |
| 逻辑身份 | `Agent` | `{name="workbuddy", type="acp"}`（无 secret） |

即三者都是「访问路径参数」，归属 `Connection`（Access Path），而非 `Agent`（目标身份）。
这把 ① 的裁决落到了具体配方上，且**不新增任何 Cloud 专属类**（与 0.1.2 G5 同一纪律）。

### 护栏

- `test_workbuddy_lands_as_agent_type_acp`：`ALLOWED_AGENT_TYPES` 含 `"acp"`；
  无 `WorkBuddyAgent` / `WorkBuddyIntegration` / `Bot` / `CloudWorkBuddy` 类。
- `test_no_second_agent_or_connection_fork_for_workbuddy`：backend 真实代码标识符中无
  `workbuddyagent` / `workbuddyintegration` / `botcredential` 等越界符号。

---

## 5. ④ Credential 是否进入 ACP DB？

### 裁决

**不新增任何 secret 列。** 这是本次 Gate 的关键否决项之一。

- `agents` / `connections` 表的列面保持冻结：`id / name / type / endpoint / status / config /
  credential_ref / created_at / updated_at / tenant_id`（见 `test_models.py` 锁死的列序）。
  **不得** 新增 `secret` / `credential` / `api_key` / `token` 列。
- 现有最小边界已足够：
  - `config` JSON 承载 secret，读时脱敏（`sanitize_connection_config` 敏感键 → `********`，
    写时拒哨兵）；
  - 可选 `credential_ref` 存 opaque 引用，明文永不落库（10.1.5 凭证安全边界）。
- **不因「Connect Agent」就退化成「connection 加 secret 字段」**——那会制造第二套凭据面，
  且相比 config 脱敏毫无收益。真正的 Vault 列（如未来需要 KMS 引用）属于独立 Gate。
- 这与 0.1.2 G7「不建第二套模型」、「不引入 Secret Manager」完全一致。

### 护栏

- `test_no_secret_plaintext_column_in_agent_or_connection`：遍历 `AgentModel` /
  `ConnectionModel` 列名，无 `secret` / `password` / `token` / `api_key` 明文列（仅允许
  `credential_ref`）。
- `test_connection_config_sensitive_keys_masked_on_read`：`sanitize_connection_config` 将
  `bearer_token`/`api_key`/`secret` 等脱敏、非敏感键（`endpoint`/`runtime_id`/`timeout_s`）原样返回。
- `test_credential_ref_is_write_only_on_connection`：`ConnectionResponse` 字段面不含
  `credential_ref`（引用不出）。

---

## 6. ⑤ Connection Status（三轴分离）

### 裁决

**区分三件不同的事，绝不把它们揉进一个 Connection 状态机：**

```
Configuration State  (配置态)   → Connection.status ∈ {active, inactive}
                                 纯管理态；管理员开关；schema 级白名单，无 "error"
Runtime Health       (运行健康) → Connected / Disconnected / Invalid Credential / Unreachable
                                 派生诊断；按需探测；不持久化；不成状态机
Interaction Outcome  (交互结果) → processing_status / delivery_status
                                 已在 interactions 行；Connection 不吸收
```

- **Configuration State**：沿用既有 `Connection.status`（schema `CONNECTION_MANAGEMENT_STATUSES
  == {"active","inactive"}`）。这是**唯一被持久化**的 Connection 状态。不变。
- **Runtime Health**：**不持久化、不做成状态机**。它是「此刻能否连上 / 能否认证」的瞬时诊断，
  应经 `GET /api/connections/{id}/health` 之类端点按需探测（由 `AgentAdapter` 探活，
  `AgentDispatchFailedError` 已覆盖 transport/auth 失败），返回后即弃。这复用 0.1.2 G6
  「First Success 派生不持久化」的同一架构纪律。
- **Interaction Outcome**：`processing_status` / `delivery_status` 已经是 `InteractionRecord`
  的双轴（4.2 冻结），且**刻意不合成**。`Connection` 不得吸收任何交互结果。

**绝不做**：把 Connection 做成一个大状态机（`connected`/`invalid_credential`/`unreachable`/
`error` 全塞进一个 `status` 或新 `health` 列）。那样既破坏「配置态 ≠ 运行态」纪律，又重蹈
我们冻结 `processing_status`/`delivery_status` 时明确反对的合成错误。

### 护栏

- `test_connection_status_is_pure_management_state`：`CONNECTION_MANAGEMENT_STATUSES ==
  {"active","inactive"}`；`Connection.status` 默认 `"active"`；无 `error`/`connected`/
  `unreachable` 等运行态混入持久模型。
- `test_runtime_health_is_derived_not_persisted`：`Connection` 域**无** `health` /
  `runtime_health` / `last_health` 字段；运行健康不构成持久状态机。
- `test_interaction_outcome_lives_on_interaction_not_connection`：`InteractionRecord` 含
  `processing_status` / `delivery_status`；`Connection` 不含这两者。

---

## 7. 最终结构（与用户给定目标一致，且已被现有代码兑现）

```
                 Browser
                    │
                    ▼
          Browser Authentication (C.1, L1+L2)
                    │
                    ▼
            AuthenticatedUser(user_id, tenant_id)
                    │
                    ▼
              TenantScope(tenant_id)            ← 复用 B，唯一 tenant 原语
                    │
          ┌─────────┴─────────┐
          ▼                   ▼
      Workspace            Resources
      (UX only)           tenant-scoped
       frontend           Agent / Channel / Connection
                              │
            ┌─────────────────┼─────────────────┐
            ▼                 ▼                 ▼
          Agent             Channel          Connection ◄── 凭据落点
        (身份/注册)        (渠道)          (Channel↔Agent 访问路径)
        name/type           name/type        channel_id+agent_id(immutable)
        endpoint(公开)      config(脱敏)     config(脱敏, 含 secret)
        无 secret          无 secret         credential_ref(可选引用)
                              │
                              ▼
                        AgentAdapter  ── 只从 Connection 取凭据 ──► Runtime
                              │
                              ▼
                         Interaction (processing_status / delivery_status)
                              │
                              ▼
                       First Success (派生状态，非实体)
```

---

## 8. 机器化护栏（Verification Gate）

新增 `tests/test_cloud_0_1_2_2_connect_agent_architecture_gate.py`（**静态 + 行为护栏，
当前即可绿灯**，锁死 ①–⑤）：

1. `test_agent_and_connection_are_separate_facts`：② —— `Agent`/`Connection` 独立域类；
   `AgentApplicationService` 与 `ConnectionApplicationService` 均存在且职责互补。
2. `test_connection_create_requires_pre_existing_agent`：② —— `create_connection` 在 agent
   缺失时抛 `AgentNotFoundError`（两事实顺序依赖）。
3. `test_agent_carries_no_credential_but_optional_reference`：① —— Agent 域无 `secret`/
   `api_key`/`token`/`password`/`bearer_token` 字段；`credential_ref` 为 optional 引用且
   不在 `AgentResponse` 字段面（写-only）。
4. `test_runtime_reads_credential_from_connection_not_agent`：① —— 静态确认 `ACPAdapter`
   从 `connection.config` / `connection.credential_ref` 取凭据，且不引用 `agent.credential_ref`。
5. `test_workbuddy_lands_as_agent_type_acp`：③ —— `ALLOWED_AGENT_TYPES` 含 `"acp"`；
   无 `WorkBuddyAgent` / `Bot` / `WorkBuddyIntegration` 类。
6. `test_no_second_agent_or_connection_fork_for_workbuddy`：③ —— backend 真实代码标识符无
   `workbuddyagent` / `workbuddyintegration` / `botcredential` 等越界符号。
7. `test_no_secret_plaintext_column_in_agent_or_connection`：④ —— `AgentModel` /
   `ConnectionModel` 列名无 `secret`/`password`/`token`/`api_key` 明文列（仅 `credential_ref`）。
8. `test_connection_config_sensitive_keys_masked_on_read`：④ —— `sanitize_connection_config`
   脱敏敏感键、保留非敏感键。
9. `test_credential_ref_is_write_only_on_connection`：④ —— `ConnectionResponse` 字段面不含
   `credential_ref`。
10. `test_connection_status_is_pure_management_state`：⑤ —— `CONNECTION_MANAGEMENT_STATUSES
    == {"active","inactive"}`；`Connection.status` 默认 `active`，无运行态混入。
11. `test_runtime_health_is_derived_not_persisted`：⑤ —— `Connection` 域无 `health`/
    `runtime_health`/`last_health` 字段。
12. `test_interaction_outcome_lives_on_interaction_not_connection`：⑤ —— `InteractionRecord`
    含 `processing_status`/`delivery_status`；`Connection` 不含。
13. `test_no_secret_manager_or_credential_vault_platform`：④/全局 —— backend 真实代码无
    `SecretManager`/`CredentialVault`/`OAuthCredential`/`CredentialProvider` 类（不建第二套凭据平台）。

进入实现期时，本护栏保持绿；并按演进纪律，在 0.1.2.2.1 落地后，把「Agent 创建不再强制
`credential_ref`」「health 探测端点」等实现后不变量补入该测试。

---

## 9. 已知偏差与 follow-up（0.1.2.2.1，不阻塞 GO）

| # | 偏差 | 影响 | 修复（0.1.2.2.1） |
|---|---|---|---|
| D1 | `AgentApplicationService.create_agent` / `update_agent` 强制 `credential_ref` 非空白 | 与 ①（Agent 不承载凭据）矛盾；使 WorkBuddy 示例 `POST /api/agents` 只传 `name`+`type` 时返回 422 | 删除两处 `_require_non_blank(input.credential_ref, ...)`；`credential_ref` 降级为可选引用；更新 `test_step11_3_agent_management_api.py` 中 `empty-credential-ref`/`blank-credential-ref` 两参数用例（改为允许缺省） |
| D2 | `ConnectionApplicationService.create_connection` 同样强制 `credential_ref` 非空白 | secret 已由 `config.bearer_token` 承载，强制 `credential_ref` 是双写且使 WorkBuddy 示例的 Connection 创建（无 `credential_ref`）返回 422 | 同上，删除该强制；`config` 为凭据主载体时 `credential_ref` 可选；更新对应 Connection 测试 |
| D3 | 尚无 Runtime Health 探测端点 | ⑤ 的「派生诊断」目前无端点承载，仅在 dispatch 失败路径体现 | 实现期加 `GET /api/connections/{id}/health`（由 `AgentAdapter` 探活，返回即弃，不落库）；不入本 Gate 范围 |

D1/D2 是**验证严格度偏差**，不改变安全边界（secret 仍不进 Agent、仍不进响应）；修复后
WorkBuddy 示例链路即端到端可用。D3 是产品层派生诊断，本 Gate 仅裁决「不持久化、不成状态机」。

---

## 10. NOT TOUCHED（冻结边界，与 B/C/0.1.2 一致并扩展）

| 类别 | 文件 / 范围 | 理由 |
|---|---|---|
| **Domain 全树** | `app/domain/**`（`agent.py`/`connection.py`/`interaction_record.py` …） | 不新增 Secret / WorkBuddy / Bot 域实体；`Connection` 三轴分离纪律本就成立。 |
| **Tenant Isolation** | `app/core/tenant.py`、`app/persistence/unit_of_work.py`、0002 | 复用 B，不重写。 |
| **Browser Authentication** | `app/core/browser_auth.py`、`app/application/browser_auth.py`、`app/api/auth.py`、0003 | 复用 C/C.1，不重写。 |
| **Runtime 契约 / Adapter** | `app/application/*`、`app/adapters/*`、`agent_runtime_sdk/**`、Worker/Driver | 经 tenant-scoped UoW 获得隔离，逻辑与签名不变；`ACPAdapter` 仅从 Connection 取凭据。 |
| **8 Core Table + tenants + user_credentials + browser_sessions + 现有 agents/connections 列面** | B/C/11.x 已建 | 本 Gate **无 schema 变更**（不新增任何 secret 列，§5）。 |
| **config 脱敏纪律** | `app/schemas/connections.py` / `channels.py` 的 `sanitize_*` | 沿用 16.1.2，不重写。 |

---

## 11. GO / 边界闭合

**GO。** 5 个待裁决问题全部给出最小安全边界，且能证明：

- **① 凭据归属 Connection**：运行时实证 `ACPAdapter` 只从 `Connection` 取凭据；Agent 不承载 secret。
- **② 两事实分离**：Agent / Connection 独立实体 + 独立 Service；Connection 创建依赖已存在 Agent。
- **③ WorkBuddy = Agent(type=acp)**：Bot ID/Secret/Endpoint 落 `Connection.config`；无第二套类。
- **④ 不新增 secret 列**：沿用 config 脱敏 + 可选 credential_ref；不建 Secret 平台。
- **⑤ 三轴分离**：Configuration State 可持久、Runtime Health 派生不持久、Interaction Outcome 在
  Interaction 上。

机器化护栏（§8）当前即绿灯，锁死 ①–⑤ 与「不建第二套凭据平台」。唯一偏差 D1/D2/D3 已列入
follow-up 0.1.2.2.1，**不阻塞 GO**——它们不改变安全边界，仅调整验证严格度与补充派生诊断端点。
