# ADR — Authorization & Access Model（授权与访问模型）

> 状态：**FROZEN**（自本文件签署起生效）
> 阶段性质：**仅裁决，不实现**。本文件把「Authorization 是什么 / 作用域是什么 /
> 位于 Runtime 何处 / 明确不做什么」一次性锁死，为后续 Access Authorization
> 实现步（Gate → 实现）提供**唯一权威裁决来源**（single source of truth）。
> 命名说明：本文件是一组 ADR 的合集（**ADR-1 ~ ADR-9** + **ADR-10 UX 设计约束**），
> 采用 `docs/architecture/` 归置——它不是某个 Step 的实施记录，而是**跨 Step 的
> 架构裁决**。
> 与 V0 冻结的关系：Access Authorization 属 `cloud-v0-release-freeze.md` §1 之外的
> **新范围**。V0 冻结期内**不实现**；实现步须在解冻 / 新阶段立项后进入，且实现
> 不得违背本文件任何一条红线。

---

## 0. 一句话原则

**授权回答的问题只有一个：这个 Conversation 能不能访问这个 Connection？**

不是「这个人是谁」，不是「这个部门有没有权限」，也不是「消息收到了就该被处理」。
收到消息（Discovery）与允许处理（Authorization）是两件事。

---

## 一、ADR 索引

| # | 决策 | 一句话 |
|---|---|---|
| ADR-1 | ACP User ≠ External Identity | 渠道参与者身份不要求拥有 ACP User |
| ADR-2 | Connection 是 Agent Access Boundary | 维持 `Channel 1:N Connection N:1 Agent`，不重定义 Agent 权限体系 |
| ADR-3 | Authorization 的 MVP 作用域 = Conversation | 判定是 `G1 → C1 = ALLOW`，不是 `User A/B/C → C1` |
| ADR-4 | Channel Native Authorization 不由 ACP 重建 | 不建 `WeComDepartment` / `SlackGroup` / `FeishuDepartment`，不复制渠道组织模型 |
| ADR-5 | Authorization 不自动创建 ACP User | `First Message → Resolve → Access Check`，绝不 `→ Create ACP User` |
| ADR-6 | Discovery ≠ Authorization | 「第一次看见这个 Conversation」不等于「它可以使用 Connection」 |
| ADR-7 | Authorization 位于 Session Resolution 之前 | 未授权请求不进入正常 Agent Session，但 Interaction 仍可记录 |
| ADR-8 | Authorization 先于最终 Routing | Router 只消费**已授权 eligible connections**，且**不因此开发 Multi-Agent Routing** |
| ADR-9 | V0 不做复杂权限系统 | 无 RBAC / ABAC / Policy Engine / 角色层级 / 组织管理 / 策略 DSL |
| ADR-10 | UX 也是设计约束 | **Simple by Default. Powerful When Needed.** |

---

## 二、现状对齐（真实代码盘点，先于裁决）

本节是 ADR 的**事实基础**：先确认「哪些承载面已经存在、哪些不存在」，避免把
已有裁决误当成新设计，也避免把不存在的能力写成「已完成」。

### 2.1 已经存在的承载面（裁决不需要新建）

| 事实 | 代码位置 | 与哪条 ADR 相关 |
|---|---|---|
| 入站契约从入口就把**会话**与**身份**分成两个字段 | `app/domain/inbound_interaction.py`：`external_conversation_id` / `external_identity_id` 并列 | ADR-1 / ADR-3 |
| 入站契约**不含**内部归属（无 `user_id` / `identity_id` / `session_id` / `connection_id` / `agent_id`） | 同上（显式冻结：Adapter 没有机会提前固化内部归属） | ADR-1 / ADR-6 |
| 群聊已按官方 wire 接纳，群会话键稳定 | `app/adapters/channels/wecom_bot.py`：`chattype=group → external_conversation_id = body.chatid`；`single → from.userid` | ADR-3（群聊天然成立的前提） |
| `Connection` = Channel ↔ Agent 受控访问路径；唯一约束 `UNIQUE(tenant_id, channel_id, agent_id)` | `app/domain/connection.py` / `app/persistence/models/connection.py` | ADR-2 |
| `Connection` 域**已明确预留**：「不负责判断『谁有权限用 Agent』……Authorization / Policy / Capability 以后单独做」 | `app/domain/connection.py` 冻结契约 | ADR-2 |
| `Session` 隔离键 = `Identity.id × Connection.id × external_conversation_id`；`external_conversation_id` 已是 `sessions` 一等列 | `app/domain/session.py` / `app/persistence/models/session.py` | ADR-3 |
| Routing 当前 = `channel_id → 唯一 active Connection`；0 条或 >1 条 → `ROUTING_FAILED`（Fail Closed）；`agent_id` 派生自所选 Connection | `app/application/routing.py` | ADR-8 |
| Runtime 执行顺序（实现） | `app/application/coordinator.py`：Identity Resolution → Routing → Session Resolution →（REUSE / CREATE_NEW）→ Agent Dispatch → Delivery | ADR-7 / ADR-8 |
| Interaction 冻结状态面 = **双状态 + 失败三元组** | `processing_status × delivery_status` + `failure_stage / failure_code / retryable`（4.2 / 10.4.2 冻结；**无**单一 `status` 列） | ADR-7（记录未授权的方式） |

### 2.2 明确不存在（本 ADR 不是「补充已有能力」，而是「新能力的前置裁决」）

```
不存在  Authorization 组件 / 授权决策对象 / 授权接口
不存在  权限实体：Role / Permission / Principal / Policy / Scope / Effect / ACL
不存在  组织镜像：Department / Organization / Group
不存在  Conversation 实体（Conversation 目前只是 channel-side 会话键，ACP 不拥有会话实体）
不存在  多 Agent 路由（RoutingRule / Weight / Priority / LoadBalancer / RoundRobin）
```

> 现状复核：`app/` 全量 AST 类名扫描确认上述实体均**不存在**；`RBAC / SSO /
> Organization` 等词只出现在**拒斥性 docstring**（如 `core/browser_auth.py`「明确不做
> OAuth Platform / SSO / RBAC / Organization / SCIM」）——这正是本 ADR 要延续的纪律。

---

## ADR-1：ACP User ≠ External Identity

**决策**

```
ACP User
    =  SteeringFlow 管理账号（管理面主体）

External Identity
    =  Channel participant identity（渠道侧参与者身份）
```

因此：**External Identity 不要求拥有 ACP User。**

**理由**

管理面主体与渠道参与者是两个世界的概念。把二者绑死会让「渠道侧有人说话了」直接
等价于「管理面多了一个账号」，这是管理面被运行时流量污染，且会迫使授权依赖账号体系。

**推论（实现约束）**

- 既有承载面：`Identity`（`channel_id + external_id` 唯一键）与 `User` 是两个领域实体，
  `Identity.user_id` 是**管理面显式建立**的绑定（Step 6.6 Identity Management），
  不是运行时产生的。
- 授权判定**不得**要求 `Identity` 或 `User` 存在（详见 §六 C1）。
- 未绑定的渠道参与者 = 「没有 Identity 行」，而不是「有 Identity 但没有 User」。

**红线**

- ❌ 不得为「让授权通过」而补建 Identity / User。
- ❌ 不得把 `Identity` 与 `User` 合并、也不得做自动跨渠道身份合并（Linking 需显式管理动作）。

---

## ADR-2：Connection 是 Agent Access Boundary

**决策**

继续保持现有关系，不新增绑定实体：

```
Channel 1 ───── N Connection N ───── 1 Agent

Connection
    ↓
Agent

Connection 表示：一个 Channel 上的一个 Agent runtime endpoint。
```

**Authorization 不直接重新定义 Agent 权限体系。**

**理由**

`Connection` 已经是「受控访问路径」的正确抽象：绑定不可变、唯一约束
`UNIQUE(tenant_id, channel_id, agent_id)`、`credential_ref` 只存引用、`config` 明确
禁止长成「万能配置容器」。授权应当**消费**这个边界，而不是**重新定义**它。

**推论（实现约束）**

- 授权的最小输出是「这条 Conversation 允许访问哪些（eligible）Connection」，
  而不是「Agent 有哪些权限」。
- Agent 侧的能力/权限体系**不在本 ADR 范围内**，也不因引入授权而变更。

**红线**

- ❌ `Connection` 域不得新增 `permission` / `role` / `policy` / `principal` / `scope`
  等授权字段，也不得新增 `authorize()` / `allow()` / `deny()` 等方法。
- ❌ 不得新增 `WorkspaceChannel` / `WorkspaceAgent` / `Integration` / `Bot` 等第二套绑定实体。

---

## ADR-3：Authorization 的 MVP 作用域

**决策**

```
External Conversation
        ↓
      Access
        ↓
   Connection
```

即：**Conversation 是 MVP 默认的 Authorization Scope。**

```
G1 → C1 = ALLOW
```

而不是：

```
User A → C1
User B → C1
User C → C1
```

因此群聊天然成立：

```
G1
├── A
├── B
└── C
     │
     ▼
    C1
     │
     ▼
   Agent X
```

**理由**

- 渠道侧的**会话**（私聊 / 群聊）才是「谁在和这个 Agent 对话」的稳定单位；逐人授权
  在群聊里既不成立（成员动态变化），也不是 MVP 的真实需求。
- 群聊会话语义已在 3B-3.3 落地（`chatid` → `external_conversation_id`），
  所以「群 → Connection」在承载面已经可表达，无需任何新实体。

**推论（实现约束）**

- 授权判定输入 = `(channel_id, external_conversation_id)`；判定输出 = 允许访问的
  Connection 集合（V0 见 ADR-8：至多一条 active）。
- `Conversation` 在 ACP 中**不是一个实体**，只是 channel-side 会话键；不得为了授权
  而新建 Conversation 表 / 实体。

**红线**

- ❌ MVP 不得把授权作用域下沉到 `User` / `Identity`（那不是 V0 的模型）。
- ❌ 不得引入「会话成员列表」作为授权输入（那会立刻变成 ADR-4 禁止的组织/成员管理）。

---

## ADR-4：Channel Native Authorization 不由 ACP 重建

**决策**

**Channel Native Authorization —— 如果存在，由 Channel 自己负责。**

ACP 不创建：

```
WeComDepartment
SlackGroup
FeishuDepartment
...
```

也**不复制** Channel 的组织权限模型。

ACP 只处理：

```
Conversation
      ↓
Connection
```

之间的 Agent access。

**理由**

渠道的组织结构、成员关系、部门权限是**渠道平台的事实**，ACP 没有权威来源，
复制一份必然过期、必然不一致、必然变成第二套组织模型。ACP 的权威只覆盖
「Channel ↔ Connection ↔ Agent」这条自有边界。

**推论（实现约束）**

- 若未来需要「按部门/分组授权」，正确做法是**在渠道侧或其上游**表达，并把结果以
  ACP 能消费的最小形式（会话键 / 连接键）投影进来；不得在 ACP 内建组织树。
- 渠道侧组织信息若需要展示，只能作为**只读透传**（若有），不得成为授权判定源。

**红线**

- ❌ 不新增 `Department` / `Organization` / `Group` / `GroupMember` 等实体或表。
- ❌ 不实现「组织权限继承」「部门成员同步」。

---

## ADR-5：Authorization 不自动创建 ACP User

**决策**

必须明确禁止这种逻辑：

```
First Message
    ↓
Unknown External Identity
    ↓
Create ACP User          ❌
    ↓
Authorize
```

正确：

```
First Message
    ↓
Resolve External Identity
    ↓
Access Check
    ↓
ALLOW / DENY
```

**External Identity 可以一直没有 ACP User。**

**理由**

自动开户把「外部流量」直接变成「管理面数据」，是不可逆的副作用；也把授权逻辑偷偷
变成账号供给（provisioning）逻辑。现有 Runtime 已经遵守这条纪律：
`IdentityResolutionExecutor` 明确「**绝不创建** Identity / User，绝不猜测」，未知身份
直接 Fail Closed（`IdentityResolutionFailedError`）。

**推论（实现约束）**

- 运行时链路（Identity Resolution / Routing / Session / Dispatch）**不得**出现任何
  创建 `Identity` / `User` 的调用。
- 授权结论不得依赖 `User` 是否存在（§六 C1）。

**红线**

- ❌ 无「首条消息自动建 User / Identity」。
- ❌ 无 `IdentityLinker` / `IdentityMatcher` / 自动 linking / fuzzy matching。

---

## ADR-6：Discovery ≠ Authorization

**决策**

这是本次设计非常重要的新原则：

```
Conversation Discovery
        ↓
"我们第一次看见这个 Conversation。"

Authorization
        ↓
"这个 Conversation 能不能使用这个 Connection？"
```

所以：

```
Unknown Conversation
        ↓
Discover
        ↓
NOT automatically authorized
```

**不能因为「Bot 收到了消息」就自动变成「Agent 可以处理消息」。**

**理由**

收到的能力（transport 可达）与允许处理（access decision）是两种不同性质的事实。
把二者合并会让「能被投递」等价于「被授权」——任何能触达 Bot 的会话都会自动获得
Agent 访问权，授权边界形同虚设。

**推论（实现约束）**

- Discovery 可以发生（会话可以被观察到、可以被记录），但**不产生**授权状态。
- 授权的默认值是 **deny**（缺授权记录 = 不允许），允许必须显式成立。

**红线**

- ❌ 不得存在「首次收到消息即写入授权」的代码路径。
- ❌ 不得把「Routing 成功选中了 Connection」当作授权结论（Routing 是选择，不是许可）。

---

## ADR-7：Authorization 位于 Session Resolution 之前

**决策**

最终 Runtime 语义（建议形态）：

```
Channel
   ↓
Channel Adapter
   ↓
InboundInteraction
   ↓
Identity Resolution
   ↓
Access Authorization
   ↓
Session Resolution
   ↓
Router
   ↓
Connection
   ↓
Agent Dispatcher
   ↓
Agent
   ↓
Delivery
```

**理由**

**未授权请求不应该进入正常 Agent Session。**

但 **Interaction 仍然可以记录**，例如：

```
Interaction
   processing_status = failed
   failure_stage      = authorization
   failure_code       = UNAUTHORIZED
   retryable          = false
```

这样 Observation 仍然可以看到发生了什么。

> 措辞对齐：本决策原始表述为 `status = rejected / reason = unauthorized`。现有
> Interaction 的冻结状态面是**双状态 + 失败三元组**（4.2 / 10.4.2），**没有**单一
> `status` 列。落地时必须映射到既有词汇（见 §六 C3），**不得**新增 `status` 列、
> 也不得把双状态合成单一状态。

**顺序约束（本 ADR 冻结的是先后关系，不是图中每个方框的绝对位置）**

- C1：Authorization **在** Identity Resolution **之后**；
- C2：Authorization **在** Session Resolution **之前**（硬约束）；
- C3：Authorization **在** 最终 Connection 选择（Router）**之前**（ADR-8）；
- C4：被拒绝的请求**仍产生 Interaction 记录**，且不产生 Session。

> 图中 `Router → Connection` 位于 Session Resolution 之后沿用了 8.4 的表述；现有实现的
> Routing 在 Session Resolution **之前**（`coordinator.py`：Routing「必须先于
> Session」）。本 ADR 不重新裁决 Router 与 Session 的历史位置，只冻结上述 C1~C4；
> 实现落点见 §六 C4。

**红线**

- ❌ 不得让未授权请求创建 Session、调用 Agent、或产生 outbound delivery。
- ❌ 不得因为「拒绝」而不记录 Interaction（Observation 不能被授权逻辑吃掉）。

---

## ADR-8：Authorization 先于最终 Routing

**决策**

```
Authorization
       ↓
Eligible Connections
       ↓
Router
```

而不是：

```
Router
   ↓
Connection
   ↓
Authorization
```

当前 V0：

```
Conversation
      ↓
one active Connection
```

所以实际上 Router 几乎没有复杂选择。

未来：

```
Conversation
      ↓
Authorized Connections
      ↓
Router
 ┌────┼────┐
 C1   C2   C3
 │    │    │
 A1   A2   A3
```

才开始产生真正的 Multi-Agent Routing。

**因此：这次 Authorization 改造不应该顺便开发 Multi-Agent Routing。**

**理由**

若 Router 先于 Authorization，Router 的选择域就是「全部 Connection」，授权只能在事后
否决一次已经做出的选择——这既让未授权路径进入选择逻辑，也让「选择」与「许可」
的职责倒流。正确方向是：**先收窄（授权），再选择（路由）**。

**推论（实现约束）**

- Router 的输入集合 = 已授权 eligible connections；Router 不得自行扩大候选集。
- V0 保持现状：`channel` 的 active Connection 数量 `!= 1` → `ROUTING_FAILED`（Fail Closed），
  不引入 weight / priority / fallback / load balancing。

**红线**

- ❌ 不得引入 `RoutingRule` / `RoutingPolicy` / `Weight` / `Priority` / `LoadBalancer` /
  `RoundRobin` / `Fallback`。
- ❌ 不得把「授权」实现成 Router 内部的一个过滤器（那是把两个职责塞进一个组件）。

---

## ADR-9：V0 不做复杂权限系统

**决策**

明确 NON-GOAL（V0 全部不做）：

```
❌ RBAC
❌ ABAC
❌ Policy Engine
❌ Role hierarchy（角色层级）
❌ Identity overrides（身份覆盖）
❌ Group management（分组管理）
❌ Department management（部门管理）
❌ Permission inheritance（权限继承）
❌ Custom policy DSL（自定义策略语言）
```

**我们只解决：`Conversation 是否可以访问 Connection`。**

**理由**

现有需求只需要一个二值访问判定，作用域是会话。引入策略语言 / 角色模型 / 继承规则
会立刻产生「策略本身的生命周期、版本、冲突消解、可视化」一整条新业务线——这是
V0 之外的范围，且没有真实业务证据支持。

**推论（实现约束）**

- 授权模型的最小形态：`(channel_id, external_conversation_id) → eligible connection set`，
  结果只有「允许的集合」，没有 effect / 优先级 / 条件组合。
- 未来若真实需求出现，应在**独立阶段**重新裁决（本 ADR 不预留可扩展点、不设计 DSL）。

**红线**

- ❌ 不新增 `RBAC` / `ABAC` / `PolicyEngine` / `PolicyModel` / `PolicyRule` / `Role` /
  `Permission` / `Principal` / `RoleHierarchy` / `PermissionInheritance` / `ACL` /
  `CustomPolicyDSL` 等类或表。
- ❌ 不新增 `roles` / `permissions` / `policies` / `departments` / `organizations` /
  `groups` 等表。

---

## ADR-10（UX 设计约束）：Simple by Default. Powerful When Needed.

**决策（冻结为设计约束）**

**Simple by Default. Powerful When Needed.**

用户默认看到的是：

```
Agent
   ↓
Connect
   ↓
Where should it work?
```

而不是：

```
Authorization
Policy
Principal
Resource
Scope
Effect
```

高级能力未来通过 **Progressive Disclosure** 暴露。

**理由**

用户的心智模型是「让这个 Agent 在哪里工作」，不是「配置一条访问控制策略」。
把授权词汇（Policy / Principal / Resource / Scope / Effect）暴露在默认路径上，等于把
实现模型当成产品模型。

**推论（实现约束）**

- 默认路径不得出现授权术语；「在哪里工作」是产品语言，授权是内部机制。
- 高级能力（若未来出现）必须经 Progressive Disclosure 进入，且不得改变默认路径语义。

**红线**

- ❌ 默认引导流程中不得出现 Policy / Principal / Resource / Scope / Effect / ACL 等词汇。

---

## 五、落地前必须裁决的真实张力（CONFLICTS）

以下四条**不是新裁决**，而是上述 ADR 与现有代码/既有冻结契约之间**已经存在的张力**。
实现步必须先显式裁决，不得静默绕过。

### C1. 身份解析目前是 Fail-Closed，而授权是 Conversation-scoped

- 现状：`IdentityResolutionExecutor` 要求 `(channel_id, external_id)` 命中 `Identity`
  且其 `User` 存在，否则 `IdentityResolutionFailedError`（STOP）；且 Runtime **绝不创建**。
- 张力：ADR-3 把授权作用域定为 Conversation（`G1 → C1`），ADR-1 说 External Identity
  不要求拥有 ACP User。若 Identity Resolution 是授权的前置条件，则群里的未绑定成员
  一发消息就在身份解析处 STOP，**永远到不了** `G1 → C1 = ALLOW`。
- 冻结口径（由 ADR-1 + ADR-3 + ADR-7 推论得出，实现步不得违背）：
  **Authorization 的判定输入只有 `(channel_id, external_conversation_id)`；
  Identity / User 的解析结果既不是判定输入，也不是判定前置条件。** Identity Resolution
  仍会执行（用于记录与可观测），但其失败**不得**成为授权结论，也不得阻断
  Conversation-scoped 授权判定。
- 实现步需明确：未绑定参与者的解析结果如何表达（建议：标记为 `unresolved`，
  不写 Identity / User），以及该情况下 Interaction 的 `failure_stage` 是否仍为 `null`。

### C2. `Identity.user_id` 当前 NOT NULL，与 ADR-1 的落点

- 现状：`IdentityModel.user_id` = `ForeignKey("users.id"), nullable=False`。
- 张力：ADR-1 说「External Identity 不要求拥有 ACP User」，字面上与 NOT NULL 冲突。
- 建议口径（实现步确认）：ADR-1 指的是**渠道侧参与者**（可能连 `Identity` 行都没有），
  不是「必须存在一条 `user_id` 为 NULL 的 Identity 行」。
  → **未绑定参与者 = 没有 Identity 行**；已建立的 Identity 仍保持必须有 User。
  → 因此 `identities.user_id` **不需要**放宽；本 ADR 不要求 schema 变更。
  若实现步发现确有「已绑定渠道身份但无管理账号」的需求，须单独裁决（不在本 ADR 授权范围内）。

### C3. 「rejected / unauthorized」与冻结状态词汇的映射

- 现状：Interaction 无 `status` 列；冻结面为 `processing_status × delivery_status`
  + `failure_stage / failure_code / retryable`；且既有纪律是「typed error →
  (stage, code, retryable) 显式映射」（`app/application/interaction_recording.py`）。
- 落地映射（示例，字面量由实现步按既有 `STAGE_FAILURE_MAP` 纪律确定）：

```
processing_status = failed        （未授权 → 处理不成立）
delivery_status   = pending       （没有出站投递）
failure_stage     = authorization
failure_code      = UNAUTHORIZED
retryable         = false         （契约/许可类不可重试）
```

- 硬约束：**不得**新增 `status` 列；**不得**把双状态合成单一状态；**不得**为拒绝路径
  伪造 `succeeded` / `delivered`。

### C4. 顺序落点：Authorization 放在 Identity 与 Routing 之间

- 现状：实现顺序为 Identity Resolution → Routing → Session Resolution
  （`coordinator.py` 明确「Routing 决定 Access Path（必须先于 Session）」）；
  而 8.4 契约文档描述 Routing 在 Session Resolution **之后**
  （`app/domain/routing_result.py` docstring）——**文档与实现存在表述差异**。
- 本 ADR 只冻结新增约束：Authorization 在 Session Resolution 之前且先于最终
  Connection 选择。据此，实现步的自然落点是**在 Identity 解析与 Routing 之间**插入
  授权（兼容现有实现顺序，且满足 ADR-8「Router 只消费 eligible connections」）。
- 实现步需明确：是否顺带修正 8.4 文档与实现的表述差异（属文档对齐，不得借此改动
  Routing 语义）。
- **不得**因为引入 Authorization 而开发 Multi-Agent Routing（ADR-8 已冻结）。

---

## 六、明确禁止（forbidden）

```
❌ 新建 Conversation 实体 / 表（Conversation 只是 channel-side 会话键）
❌ 新建 Authorization 策略体系（RBAC / ABAC / Policy Engine / Role / Permission / ACL）
❌ 新建组织镜像（Department / Organization / Group / GroupMember）
❌ 首条消息自动创建 ACP User / Identity
❌ Identity Linking / 自动跨渠道合并 / fuzzy matching
❌ 把「收到消息」当作授权结论（Discovery ≠ Authorization）
❌ Router 先于 Authorization（授权必须是选择前的收窄）
❌ 为授权新增 Interaction.status 列 / 合成双状态
❌ 未授权请求创建 Session / 调用 Agent / 产生 Delivery
❌ 默认 UX 暴露 Policy / Principal / Resource / Scope / Effect 等授权术语
❌ 借本次改造扩展 Multi-Agent Routing
```

---

## 七、机器化护栏（machine guards）

测试文件：`backend/tests/test_adr_authorization_and_access_model.py`

只读审计 + 机器锁定；**不启动 DB、不做实现**。采用 AST 扫描真实标识符（类名 /
字段名 / 方法名 / 调用名），避免拒斥性 docstring 产生假阳性。

| 组 | 锁定内容 |
|---|---|
| ADR-0 | 本文件存在且 ADR-1 ~ ADR-10 与 UX 约束成文（防漂移） |
| ADR-1 | `User` 与 `Identity` 是两个实体；`User` 不承载渠道身份字段 |
| ADR-2 | `Connection` 未长成授权对象（无授权字段/方法）；`UNIQUE(tenant_id, channel_id, agent_id)` 仍在 |
| ADR-3 | 授权作用域键 `external_conversation_id` 在入站契约与 `sessions` 均为一等字段；群聊 `chatid` 映射仍在 |
| ADR-4 | 无 `Department` / `Organization` / `Group` 等组织镜像类或表 |
| ADR-5 | 运行时链路零创建 `Identity` / `User` 的调用；Identity Resolution 仍 Fail Closed |
| ADR-6 | 入站契约零授权字段；无「首次消息即授权」路径；授权默认 deny 语义成文 |
| ADR-7 | 未授权请求不产生 Session/Agent/Delivery（源码顺序守卫：Identity → Routing → Session Resolution） |
| ADR-8 | 无路由规则引擎类；Routing 仍为「唯一 active Connection / 否则 Fail Closed」 |
| ADR-9 | 无 RBAC / ABAC / PolicyEngine / Role / Permission / Principal / ACL 类与表 |
| ADR-10 | UX 约束成文（Simple by Default / Progressive Disclosure） |

---

## 八、给实现步的边界摘要

```
本 ADR 允许的下一步：
  Gate（审计）→ 实现（Conversation-scoped Access Authorization）
  最小输出：eligible connection set（V0 至多一条 active）

本 ADR 不允许的下一步：
  任何 ADR-9 NON-GOAL 内的能力
  任何 ADR-4 形态的组织/成员建模
  任何 Multi-Agent Routing 扩展
  任何 schema 变更（C2 已裁决：不需要）
  任何「顺手」把授权词汇带进默认 UX 的改动
```

---

> 本文件为 **FROZEN**：任何修改须以「新增 ADR 编号 + 显式取代关系」的方式提交，
> 不得原地改写既有 ADR 的语义。
