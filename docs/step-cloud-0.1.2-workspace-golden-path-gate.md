# Cloud-0.1.2 —— Workspace & Golden Path Architecture Gate

> 阶段：Architecture Gate（设计 + 影响审计）。**本次只交付设计，不实现。**
> 方法：只读审计 + 机器化护栏（`tests/test_cloud_0_1_2_workspace_golden_path_gate.py`）。
> 配套契约：ACP V0.1 RC → ACP V0.2 Artifact Contract → Cloud-0.1.1-A（PG + Alembic + 8 Core Tables）
> → Cloud-0.1.1-B（Tenant Isolation）→ Cloud-0.1.1-C / C.1（Browser Authentication，已 GO 且已落地）
> → **本 Gate（Workspace & Golden Path）**。
>
> **本 Gate 的中心问题（单一裁决标尺）**：
>
> ```
> Browser
>   ↓  Browser Authentication (C, L1+L2) → AuthenticatedUser(user_id, tenant_id)
>   ↓  TenantScope(tenant_id)             ← 复用 B，唯一 tenant 原语
>   ↓  Workspace (UX only)
>   ↓  tenant-scoped Resources (Agent / Channel / Connection)
>   ↓  Runtime → Interaction → First Success
> ```
>
> 一句话：**Cloud 不要因为「Workspace / Onboarding / First Success」这些产品词，就偷偷长出
> 第二套 Workspace / Tenant / Integration / Authorization 模型。** 本次把这条红线机器化锁死。

---

## 0. 结论先行（GO / BLOCKED）

**GO。** 对现有代码库做只读审计后确认：

- **Workspace ≡ Tenant 已成立**（G1）：后端只有 `TenantScope`（`app.core.tenant`）一个 tenant 原语；
  无 `Workspace` 域类、无 `workspaces` 表、无 `workspace_members` / `organizations` / `teams` / `memberships`
  第二套归属模型。`Workspace` 在 `app/**` 真实代码标识符中**零出现**（仅出现在 docstring 的
  「Workspace/Tenant?」表述里，属拒斥性证据，已被护栏剥离）。
- **Sign-up Bootstrap 链路清晰**（G2）：`BrowserAuthService.register → User + 凭据 → login →
  BrowserSession → AuthenticatedUser(user_id, tenant_id)`。`User` 域无 `workspace_id`；
  `AuthenticatedUser` 恰为 `{user_id, tenant_id}`，tenant 是唯一归属维度；唯一被允许写 `tenants`
  的入口是 `TenantProvisioner`（bootstrap 端口），无第二套归属创建器。
- **TenantScope Continuity 成立**（G3）：`AuthenticatedUser.tenant_id → TenantScope(tenant_id) →
  functools.partial(UoW, tenant_id=scope.tenant_id)`（B §7 桥）。无隐式全局 tenant 上下文
  （无 `current_tenant` / `global_tenant`），无第二套 tenant 注入机制。
- **Agent / Channel / Connection 直接复用**（G4）：三实体（`app.domain.*`）即 Cloud onboarding 的
  资源模型；无 `CloudAgent` / `CloudChannel` / `CloudConnection` / `CloudResource` 第二套业务模型。
- **WorkBuddy = Agent / WeCom = Channel / Connection = binding**（G5）：无 `Integration` /
  `Connector` / `IntegrationModel` 抽象层；WeCom / WorkBuddy 落在既有 Channel / Agent 的 `type`
  维度，不新造 Cloud 专属类。
- **First Success 可推导，不持久化**（G6）：无 `first_successes` / `onboarding_states` /
  `activation_events` / `user_journeys` 表或域类。First Success 是「Agent+Channel+Connection+真实
  Interaction 成功」的派生状态，非 ACP Core Domain Entity。
- **Architecture Pollution 以静态护栏锁死**（G7）：no workspace table / no organization / no
  membership / no authorization engine / no cloud runtime fork / no second tenant abstraction。

最终结构（与用户给定目标一致，且已被现有代码兑现）：

```
                 Browser
                    │
                    ▼
          Browser Authentication (C, L1+L2)
                    │
                    ▼
            AuthenticatedUser(user_id, tenant_id)
                    │
                    ▼
              TenantScope(tenant_id)            ← 复用 B，唯一 tenant 原语
                    │
          ┌─────────┴─────────┐
          │                   │
          ▼                   ▼
      Workspace            Resources
      (UX only,           tenant-scoped
       frontend)          Agent / Channel / Connection
                              │
              ┌───────────────┼──────────────┐
              ▼               ▼              ▼
            Agent           Channel      Connection
              │               │              │
              └───────────────┴──────────────┘
                              │
                              ▼
                           Runtime (Core，不变)
                              │
                              ▼
                         Interaction
                              │
                              ▼
                       First Success (派生状态，非实体)
```

---

## 1. Golden Path 固定（冻结）

Cloud V0 的目标链：

```
Browser
  ↓  Browser Authentication (C)
  ↓  AuthenticatedUser
  ↓  Tenant (== Workspace)
  ↓  Connect Agent   (WorkBuddy = Agent)
  ↓  Connect Channel (WeCom = Channel)
  ↓  Connection      (Channel ↔ Agent binding)
  ↓  First Success   (派生)
  ↓  Runtime
```

本 Gate 的核心裁决（与 B/C 一脉相承）：

> **Workspace 是 Cloud 对 Tenant 的产品化表达，而不是新增一个 Domain Entity。**
> 关系应是 `AuthenticatedUser → Tenant → Workspace UI`，
> 绝不应是 `User → Workspace → Tenant → Agent` 的三层实体。

---

## 2. G1 — Workspace ≡ Tenant（无第二套 Workspace ownership model）

| 检查项 | 现状（审计证据） | 护栏 |
|---|---|---|
| `Workspace` 域类 | 不存在 | `test_no_workspace_domain_entity_or_table` |
| `workspaces` / `workspace_members` 表 | 不存在（仅 11 张表：8 Core + `tenants` + `user_credentials` + `browser_sessions`） | 同上 |
| `organizations` / `teams` / `memberships` 表或域类 | 不存在 | 同上 |
| tenant 身份原语唯一 | 仅 `TenantScope(tenant_id)` | `test_single_tenant_primitive_only` |

**裁决**：`Tenant = ownership / isolation root`；`Workspace = Cloud UX abstraction`。
0.1.2 不创建 `workspaces` / `workspace_members` / `organizations` / `teams` 任何表。
若未来商业验证证明「多 Workspace 单 Tenant」有必要，须**单独开 Gate**，而不是顺手加表。

---

## 3. G2 — Sign-up Bootstrap（真实数据链）

C.1 已落地的链路：

```
register(login, password, tenant_id)
   → User(name=login) 落 tenant-scoped UoW
   → user_credentials(verifier=pbkdf2)
   → 返回 AuthenticatedUser(user_id, tenant_id)
        ↓
login(login, password)
   → 校验 user_credentials.verifier
   → browser_sessions(session_id=opaque, user_id, tenant_id)
   → 返回 BrowserSessionGrant
        ↓
verify(session_id) / authenticate_session
   → 查 browser_sessions → AuthenticatedUser(user_id, tenant_id)
        ↓
build_tenant_scope_provider
   → TenantScope(tenant_id=user.tenant_id)   ← L3 复用 B
```

| 检查项 | 现状 | 护栏 |
|---|---|---|
| `AuthenticatedUser` 字段 | 恰为 `{user_id, tenant_id}` | `test_authenticated_user_carries_tenant_id` |
| `User` 域无 `workspace_id` | `User(name, id, status)`，无 workspace 字段 | `test_register_binds_to_tenant_not_workspace` |
| 唯一 tenant 创建入口 | `TenantProvisioner`（bootstrap 端口） | `test_tenant_provisioner_is_only_tenant_creator` |
| `BrowserAuthService` 三段闭环 | `register` / `login` / `authenticate_session` / `verify` 齐备 | `test_register_binds_to_tenant_not_workspace` |

**冻结决策（产品侧）**：Cloud V0 的 Sign Up = 自动获得一个 Workspace（即一个 Tenant）。
实现期若要做「注册即建租户」，必须**复用现有 `TenantProvisioner` 端口**，绝不能引入
`WorkspaceProvisioner` / `OrganizationProvisioner` 第二套归属创建器。本 Gate 锁死这条边界。

---

## 4. G3 — TenantScope Continuity（不建第二套 tenant 注入机制）

| 检查项 | 现状 | 护栏 |
|---|---|---|
| 无隐式全局 tenant 上下文 | 无 `current_tenant` / `global_tenant` / `set_current_tenant` | `test_no_implicit_global_tenant_context` |
| `AuthenticatedUser.tenant_id → TenantScope` | `build_tenant_scope_provider` 产出 `TenantScope`（B 原语） | `test_auth_flow_reuses_tenant_scope_not_second_injection` |
| 无 Browser 专属 tenant 解析原语 | 无 `BrowserTenantScope` / `WorkspaceScope` | `test_single_tenant_primitive_only` |
| OSS/本地回退保留 | `TENANT_LOCAL == 1`，`default_tenant_scope()` 可用 | `test_no_cloud_runtime_fork` |

`app/api/auth.py` 的 `build_tenant_scope_provider` 是唯一的浏览器面 tenant 解析依赖；
`main.py` 经 `functools.partial(SqlAlchemyUnitOfWork, tenant_id=scope.tenant_id)`（B §7 桥）
把解析结果绑成 tenant-scoped UoW。**不存在第二套 tenant 注入机制**。

---

## 5. G4 — Agent / Channel / Connection 复用（无 Cloud 专属实体）

| 检查项 | 现状 | 护栏 |
|---|---|---|
| 三实体存在且为资源模型 | `Agent(name,type,...)` / `Channel(name,type,...)` / `Connection(channel_id,agent_id,...)` | `test_core_three_entities_exist_and_reused` |
| 无 Cloud 专属业务模型 | 无 `CloudAgent` / `CloudChannel` / `CloudConnection` / `CloudResource` | `test_no_cloud_specific_resource_entities` |

**裁决**：Cloud UI 做的是「Create / Configure existing Agent / Channel / Connection」，
Cloud 是 ACP Core 的 Managed Control Surface，而非另一套业务模型。

---

## 6. G5 — WorkBuddy / WeCom（无 Integration 抽象）

| 检查项 | 现状 | 护栏 |
|---|---|---|
| 无 Integration 抽象层 | 无 `Integration` / `Connector` / `IntegrationModel` 类 | `test_no_integration_abstraction` |
| Connection 已是绑定 | `Connection.channel_id + agent_id` 即为 Channel↔Agent 受控访问路径 | `test_no_integration_abstraction` |
| WeCom / WorkBuddy 落在 type 维度 | 无 `CloudWeCom` / `CloudWorkBuddy` / `WeComIntegration` 类 | `test_no_second_channel_or_agent_fork_for_wecom_workbuddy` |

**冻结**：Golden Path = `Connect WorkBuddy → 创建/配置 Agent`；`Connect WeCom → 创建/配置 Channel`；
`Connect → Connection`。不要因为 Cloud Onboarding 再造一层 Integration。

---

## 7. G6 — First Success（派生状态，非持久化模型）

| 检查项 | 现状 | 护栏 |
|---|---|---|
| 无 First Success 持久化模型 | 无 `FirstSuccess` / `OnboardingState` / `ActivationEvent` / `UserJourney` 类 | `test_first_success_is_derived_not_persisted` |
| 无对应表 | 无 `first_successes` / `onboarding_states` / `activation_events` / `user_journeys` | `test_first_success_is_derived_not_persisted` |

**裁决**：First Success 由现有事实推导：

```
Agent exists
+ Channel exists
+ Connection exists
+ Real Interaction succeeded
+ Delivery succeeded
        ↓
First Success achieved
```

它是 Cloud V0 的产品目标 / 派生状态，不是 ACP Core Domain Entity。第一阶段**不**引入任何
onboarding 持久化实体。

---

## 8. G7 — Architecture Pollution（机器化锁死）

下列红线以 `test_*` 静态护栏机器锁定，一旦触碰即红灯：

| 禁区 | 为何不引入（V0 不需要 / 已被替代） |
|---|---|
| **workspace table / Workspace 域** | Workspace ≡ Tenant，是 UX 抽象，不进后端域/持久层。 |
| **organization / team / membership** | `tenant` 已是数据隔离与归属根目录（B 的 `tenants` root catalog）。 |
| **authorization engine / RBAC** | 「能访问哪些资源」由租户边界（B）统一回答，无角色 / 无 per-resource ACL。 |
| **Integration / Connector 抽象** | 复用 Core 的 Agent / Channel / Connection；不造第二套集成层。 |
| **cloud runtime fork** | Runtime 仍经既有 Core 执行（Worker / Driver / Coordinator），不为 Cloud 另造。 |
| **second tenant abstraction** | tenant 原语唯一 = `TenantScope`；不新建 `BrowserTenant` / `WorkspaceTenant`。 |

**判据（铁律）**：若 backend 真实代码标识符中出现了 `workspace` / `organization` / `membership` /
`authorizationengine` / `rbac` / `integration`（作为类）/ `cloudruntime` / `cloudagent` /
`cloudchannel` / `firstsuccess` 等符号，即判定为越界。

---

## 9. NOT TOUCHED（冻结边界，与 B/C 一致并扩展）

| 类别 | 文件 / 范围 | 理由 |
|---|---|---|
| **Domain 全树** | `app/domain/**`（含 `user.py` / `agent.py` / `channel.py` / `connection.py`） | 不新增 Workspace / Organization / Team / Membership / Integration 域实体。 |
| **Tenant Isolation** | `app/core/tenant.py`、`app/persistence/unit_of_work.py`、0002 | 复用 B，不重写。 |
| **Browser Authentication** | `app/core/browser_auth.py`、`app/application/browser_auth.py`、`app/api/auth.py`、0003 | 复用 C/C.1，不重写。 |
| **Runtime 契约 / Application / Adapters / SDK / Worker / Driver** | 同 B/C | 经 tenant-scoped UoW 获得隔离，逻辑与签名不变。 |
| **8 Core Table + tenants + user_credentials + browser_sessions schema** | B/C 已建 | 本 Gate 无 schema 变更（不新增任何表）。 |

---

## 10. 机器化护栏（Verification Gate）

新增 `tests/test_cloud_0_1_2_workspace_golden_path_gate.py`（**静态护栏，当前即可绿灯**，锁死 G1–G7）：

1. `test_no_workspace_domain_entity_or_table`：G1 —— 无 `Workspace`/`Organization`/`Team`/`Membership` 类；无 `workspaces`/`workspace_members`/`organizations`/`teams`/`memberships` 表。
2. `test_single_tenant_primitive_only`：G1 —— tenant 原语唯一 = `TenantScope(tenant_id)`；无 `browser_tenant`/`workspace_tenant` 第二套。
3. `test_authenticated_user_carries_tenant_id`：G2 —— `AuthenticatedUser` 恰为 `{user_id, tenant_id}`，无 workspace 字段。
4. `test_register_binds_to_tenant_not_workspace`：G2 —— `User` 无 `workspace_id`；`BrowserAuthService` 含 `register`/`login`/`authenticate_session`/`verify` 闭环。
5. `test_tenant_provisioner_is_only_tenant_creator`：G2 —— 唯一写 `tenants` 入口是 `TenantProvisioner`；无 `WorkspaceProvisioner`/`OrganizationProvisioner`。
6. `test_no_implicit_global_tenant_context`：G3 —— 无 `current_tenant`/`global_tenant`/`set_current_tenant` 隐式全局。
7. `test_auth_flow_reuses_tenant_scope_not_second_injection`：G3 —— `build_tenant_scope_provider` 产出 `TenantScope`（B 原语），无 `BrowserTenantScope`/`WorkspaceScope`。
8. `test_core_three_entities_exist_and_reused`：G4 —— Agent/Channel/Connection 存在且为资源模型。
9. `test_no_cloud_specific_resource_entities`：G4 —— 无 `CloudAgent`/`CloudChannel`/`CloudConnection`/`CloudResource`/`CloudIntegration`。
10. `test_no_integration_abstraction`：G5 —— 无 `Integration`/`Connector`/`IntegrationModel` 抽象层；`Connection` 已是绑定。
11. `test_no_second_channel_or_agent_fork_for_wecom_workbuddy`：G5 —— 无 `CloudWeCom`/`CloudWorkBuddy`/`WeComIntegration` 专属分叉。
12. `test_first_success_is_derived_not_persisted`：G6 —— 无 `FirstSuccess`/`OnboardingState`/`ActivationEvent`/`UserJourney` 类或表。
13. `test_no_workspace_or_second_tenant_abstraction`：G7 —— backend 真实代码标识符中无 `workspace`；tenant 概念存在且唯一。
14. `test_no_organization_team_membership_authorization_engine`：G7 —— 无 `organization`/`authorizationengine`/`rbac`/`scim`/`oauth`/`sso`/`jwt` 等越界标识符。
15. `test_no_cloud_runtime_fork`：G7 —— 无 `cloudruntime`/`cloudworker`/`clouddriver`；Runtime 面仍绑定 `TENANT_LOCAL`。
16. `test_workspace_is_ux_abstraction_only`：G7 收口 —— 扫描真实代码标识符，`workspace` 不在其中（仅 docstring 表述，被剥离）。

进入实现期时，本护栏保持绿；并按演进纪律，在「注册即建租户」「Workspace UI 装配」落地后，把
对应的「实现后不变量」（表/端点/派生状态函数）补入该测试。

---

## 11. GO / 边界闭合

**GO。** G1–G7 全部回答，且能证明：

- **Workspace ≡ Tenant**：后端无第二套 Workspace ownership model，tenant 原语唯一。
- **Bootstrap 链路闭合**：`User → 凭据 → Session → AuthenticatedUser(tenant_id) → TenantScope`
  真实成立，唯一 tenant 创建入口是 `TenantProvisioner`。
- **TenantScope Continuity**：`AuthenticatedUser.tenant_id → TenantScope → 既有 tenant-scoped UoW`，
  无隐式全局、无第二套注入。
- **Core 三实体复用**：Agent / Channel / Connection 直接承载 Cloud onboarding，无 Cloud 专属模型。
- **WorkBuddy/WeCom 落到 type 维度**：无 Integration 抽象。
- **First Success 派生**：无新持久化模型。
- **Architecture Pollution 锁死**：workspace / organization / membership / authorization engine /
  cloud runtime fork / second tenant abstraction 以 §10 静态护栏机器锁定。

进入实现期时，按本 Gate 的冻结边界落地「注册即建租户」「Workspace UI」「Golden Path 端点」，
且不得破坏 §9 NOT TOUCHED 与 §8 红线。
