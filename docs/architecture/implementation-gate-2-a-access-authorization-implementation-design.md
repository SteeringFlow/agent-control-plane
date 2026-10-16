# Implementation Gate 2-A — Access Authorization Implementation Design

> 状态：**只读审计 / DESIGN REPORT**。未改生产代码、未改测试、未改 migration、未改 schema、
> 未改冻结契约、未创建任何类/表/文件（除本报告）。
> 继承并**不重开**：`authorization-and-access-model.md`（ADR-1 ~ ADR-10，FROZEN）、
> Gate 1、Gate 2、Gate 2.5、Gate 2.6、Gate 3、Implementation Package 1。
>
> 上游状态：`Package 1 = DONE / COMMITTED（f43689a）`；`PG Closure Evidence = DONE`。
> 本 Gate 是 Package 2 的**第一步且仅为设计**。

---

## 0. 结论总表

| 组 | 问题 | 结论 | 判定 |
|---|---|---|---|
| **A** | Runtime 插入点 | **P2 冻结**：Identity Resolution 之后、Routing 之前；落在 `RuntimeCoordinator` 内 | **FROZEN**（Gate 2 §4.2 / ADR-7/8） |
| **B** | 最小输入 | **`(channel_id, external_conversation_id)`**；禁止消费 Identity / User / content / Connection 状态 | **FROZEN**（Gate 2 §2） |
| **C** | V0 ALLOW/DENY 的真实数据来源 | **不存在合法来源**。6 个候选全部被否决（含「恒 ALLOW」placeholder） | **BLOCKER** |
| **D** | 0-table assumption | **不成立，必须推翻**（Gate 2 §6.2 的 0 表结论是**有前提的**，前提已被本次指令否掉） | **BLOCKER / 需 ADR-11** |
| **E** | Conversation × Connection 表达能力 | 现有 **11 张表无一可表达**（逐表否决）；需最小持久化边界 | **BLOCKER** |
| **F** | Unauthorized Failure Contract | 与真实代码**逐字段核对通过**，零新列、零新状态值，仅新增 2 个分类取值 | **PASS** |
| **G** | Routing regression | 「唯一性语义」零改动成立；但「Routing **零改动**」**必须修订** —— 候选集要从 `active(channel)` 收窄为 `active(channel) ∩ granted(conversation)` | **PASS（需修订 1 条 Gate 2 断言）** |
| **H** | 完整 regression matrix | 4 类（3 类继承 Gate 2 + 新增第 4 类「冷启动 default-deny」，**影响面最大**） | **见 §H** |

**一句话**：Gate 2-A 把 Authorization 的**契约面全部锁定并通过**，但发现它的
**唯一真实数据来源不存在**——而 Gate 2 的 0-table 结论正是靠「V0 恒 ALLOW」这个
placeholder 撑起来的，本次指令已明确禁止该 placeholder。因此本 Gate 的输出是
**BLOCKER + 一条最小持久化边界提案**，而不是一份可直接实施的实现设计。

---

## A. Runtime 插入点

### A.1 冻结形态（采纳 Gate 2 §4.2 的 P2）

```
InboundInteraction
      ↓
Identity Resolution        ← 既有，零改动（仍 Fail Closed）
      ↓
★ Access Authorization     ← 新增唯一阶段
      ↓
Routing                    ← 候选集收窄（见 §G）
      ↓
Session Resolution
      ↓
[Session Creation?]
      ↓
Agent Dispatch
      ↓
Delivery
```

### A.2 落点证据（真实代码行）

- 现状顺序：`app/application/coordinator.py:92-104`
  （`self._identity.execute` → `self._routing.execute` → `self._session_resolution.execute`）。
- 顺序常量：`app/application/runtime.py:75-82`（`RUNTIME_EXECUTION_ORDER`，6 段）。
- ADR-7 三条硬约束：Identity 之后 / Session Resolution 之前 / Interaction 仍记录。

### A.3 ✅ 一个重要的 **PASS**：ADR-7 守卫**不需要改**

```python
# tests/test_adr_authorization_and_access_model.py:343-347
src = _read(coordinator.py)
i_identity = src.index("self._identity.execute")
i_routing  = src.index("self._routing.execute")
i_session  = src.index("self._session_resolution.execute")
assert i_identity < i_routing < i_session
```

在 Identity 与 Routing 之间插入 authorization 后，**该断言仍为真**。
守卫是「用顺序基座迫使实现者显式面对 ADR-7」的形态，而本落点**恰好符合**其设计意图。

### A.4 落点必须遵守的既有守卫（Gate 2 §4.4 复核，逐条仍有效）

| 守卫 | 约束 | 对设计的实际影响 |
|---|---|---|
| `test_fanout_does_not_change_runtime_execution_logic` | `coordinator.py` 内**不得出现 `"tenant"` 字面量**（注释也不行） | tenant 只能经**注入的 tenant-scoped UoW 工厂**在 executor 内部生效 |
| `test_coordinator_has_no_repository_orm_adapter_identifiers` | 禁止标识符含 `Repository/SqlAlchemy/ORM/Model/Engine/Adapter` | 新阶段只能注入抽象 |
| `test_coordinator_does_not_swallow_stage_errors` | coordinator 内**无 `ast.Try`** | **DENY 必须抛 typed error**，不得 catch |
| `test_coordinator_never_resolves_session_by_user_id` | `coordinator.py` 内不得出现 `"user_id"` | 与 §B「Identity/User 非输入」一致 |
| `test_coordinator_session_branch_has_no_hidden_branch` | 恰 2 个 `decision.mode` 分支 | 新阶段不得引入第三个 mode 分支 |
| `test_outbound_construction_is_minimal` | 恰 1 处 `OutboundResponse(...)`、恰 4 个 kwargs | 不变 |

**结论：A = FROZEN，且落点与全部既有守卫兼容。**

---

## B. 最小输入

### B.1 冻结

```
最小输入集：  (channel_id, external_conversation_id)
判定输出：    ALLOW | DENY
值域（未来）：eligible connection set（V0：≤1，唯一性仍归 Routing）
```

| 候选 | 是否输入 | 依据 |
|---|---|---|
| `external_conversation_id` | **必要**（主语） | ADR-3：Scope = Conversation |
| `channel_id` | **必要**（作用域） | 会话键只在 Channel 内解释（群 `chatid` / 单聊 `userid` 均无全局唯一性） |
| `identity_id` / `external_identity_id` | **禁止消费** | ADR-1 / ADR-3 |
| `user_id` | **禁止消费** | ADR-1 / ADR-5 |
| `content` | **禁止消费** | 否则滑向 ABAC / 内容策略（ADR-9） |
| `connection_id` | **不是输入**，是值域 | ADR-8：判定的**产出**侧 |

### B.2 复核

- 入站契约已把两个键并列携带且**不含内部归属**：
  `app/domain/inbound_interaction.py`（ADR guard `test_adr6_*` 机器锁定其无
  `user_id/identity_id/session_id/connection_id/agent_id/authorized/...`）。
- 群聊会话键稳定：`app/adapters/channels/wecom_bot.py`
  （`chattype=group → external_conversation_id = body.chatid`；`single → from.userid`）。

**结论：B = FROZEN，零额外前置条件。**

---

## C. V0 ALLOW / DENY 的真实数据来源 —— **BLOCKER**

### C.1 问题的准确形式

```
eligible = Permit(channel_id, external_conversation_id)
DENY  ⇔  eligible is empty
ALLOW ⇔  otherwise
```

Gate 2 §3.2 给 `Permit` 的定义是：

> **`Permit` 在 V0 = 全量**（不查任何 deny/grant 记录）→ 判定**恒 ALLOW**；
> 这是 §0 的 **D2**，需 Review 显式确认。
> （`implementation-gate-2-minimal-authorization-design.md:156-157`）

而本次指令明确禁止：

> **特别禁止用「永远 ALLOW」的 placeholder 冒充有效 Authorization。**

**冲突是直接的：** 若 `Permit` 不查任何记录，则 Authorization 阶段
**永远返回 ALLOW**，它是一个「语义位」而非判定——这正是被禁止的形态。

### C.2 六个候选数据源，逐个否决

| # | 候选来源 | 判定 | 否决理由（可追溯） |
|---|---|---|---|
| S1 | **不查任何记录 → 恒 ALLOW** | ❌ | **本次指令明确禁止**（placeholder 冒充授权）。且它使 `UNAUTHORIZED` 永不可达 → 拒绝契约无法被验证 |
| S2 | **无条件 DENY（严格照 ADR-6 字面）** | ❌ | ADR-6 说「缺授权记录 = 不允许」；若一条记录都不存在 → **全部流量 DENY** → 现有 Runtime 全线停摆（见 §H 第 4 类）。这是「把系统关掉」而非「实现授权」 |
| S3 | `connections.config`（JSON 列内塞会话白名单） | ❌ | `app/domain/connection.py:17-18` 冻结：「config … **不是「万能配置容器」**（不得长成 permissions/capabilities/routing_rules/…）」；ADR-2 红线；`test_step7_3…:486 test_j_connection_config_is_not_policy_container` 机器锁定 |
| S4 | `sessions`（已有 `(identity_id, connection_id, external_conversation_id)`） | ❌ | **循环**：Session 由授权**之后**的 Session Creation 建立；用 Session 当授权源 = 首条消息永远未授权。且它是 **identity-scoped**（同一群 G1 里 A/B/C 各有身份）→ 群聊「G1 → C1」无法表达（ADR-3 的核心用例） |
| S5 | `channels.config`（Channel 级白名单） | ❌ | ① 同类「config 当策略容器」反模式；② **Channel 级 ≠ Conversation 级**，等于「该渠道所有会话都放行」→ 使 ADR-3 的会话作用域**空转**；③ 现有 `PATCH /api/channels/{id}` 会**静默变成授权改写端点** |
| S6 | `interactions` / `activities` | ❌ | 两者都只有 `channel_id` + `external_interaction_id`，**既无 conversation 键也无 connection 键**；`activities` 是 append-only Evidence，把审计轨迹当许可源在语义上是反的 |

### C.3 判定

```
V0 ALLOW/DENY 的数据来源：  不存在
        ↓
不存在「有效的 Authorization」可实施路径
        ↓
BLOCKER（不是设计缺陷，而是缺失的持久化边界）
```

> 注意：这不是「Gate 2 设计错了」。Gate 2 在 §6.2 里**诚实登记过**这个代价：
> 「**DENY 在 V0 不可达**（§3.2 D2）：因此『拒绝记录』在当前没有生产触发源，
> **只有 Targeted Tests 可以构造**」（`:324-325`）。
> 本次指令相当于 **Review 否决 D2**，于是 Gate 2 §6 的结论随之失效（见 §D）。

---

## D. 0-table assumption 是否成立 —— **推翻**

### D.1 Gate 2 §6.2 的原始结论是**有前提的**

原文（`implementation-gate-2-minimal-authorization-design.md:320-333`）：

```
### 6.2 该结论的**代价**（必须写进 Review 记录，不能隐藏）
1. ALLOW 不被持久化：… 无法回答「某会话何时被允许、依据什么」…
2. DENY 在 V0 不可达（§3.2 D2）：因此「拒绝记录」在当前没有生产触发源 …
3. 一旦需要 T1/T2/T3（Gate 1 §B：会话级 deny / 子集绑定 / 授权关系审计）中任一，
   本结论立即失效，必须升级为 Persistence = YES（新表 + migration）…

Persistence verdict:   0 new tables / 0 new columns / NO migration   ✅（在上述代价被接受的前提下）
```

**结论的成立条件是「代价被接受」。** 代价 #1（ALLOW 不落库）与 #2（DENY 不可达 /
恒 ALLOW）在本次指令下**均不可接受**：

- 代价 #2 被否 → `Permit` 必须有真实来源 → 必须有 grant/deny 记录 →
  这正是 Gate 2 §6.2 代价 #3 自己写下的**升级触发条件 T1/T2**；
- 代价 #1 被否 → 至少要有「哪些会话被允许」的事实来源。

### D.2 而且 ADR-6 **本身就要求**存在显式许可记录

```
Authorization 的默认值是 deny（缺授权记录 = 不允许），允许必须显式成立。
```
（`authorization-and-access-model.md:344`）

「缺授权记录 = 不允许」这句话在语义上**预设了「授权记录」的存在**；
一个永远没有记录的系统，只能表达「全部不允许」。ADR-6 与 Gate 2 §6 的
0-table 结论**在「有效授权」要求下互相矛盾**。

### D.3 结论

```
0-table assumption = 不成立
        ↓
Persistence = YES（新表 + migration）
        ↓
但它与 ADR §八「任何 schema 变更（C2 已裁决：不需要）」直接冲突
        ↓
必须以 ADR-11（新编号 + 显式取代关系）修订
```

ADR 自身的修改纪律（`:702-703`）：

> 本文件为 **FROZEN**：任何修改须以「新增 ADR 编号 + 显式取代关系」的方式提交，
> 不得原地改写既有 ADR 的语义。

**因此 Package 2 的第一步不是写代码，而是 ADR-11。**

---

## E. Conversation × Connection 表达能力（逐表审计）

### E.1 现有 11 张表逐个核对

| 表 | 是否持有 conversation 键 | 是否持有 connection 键 | 能否表达 `G1 → C1` |
|---|---|---|---|
| `users` | ❌ | ❌ | ❌ |
| `identities` | ❌ | ❌ | ❌ |
| `channels` | ❌（只有 `config` JSON） | ❌ | ❌（S5 已否决） |
| `agents` | ❌ | ❌ | ❌ |
| `connections` | ❌ | 自身 | ❌（只有 `config` JSON；S3 已否决） |
| `sessions` | ✅ `external_conversation_id` | ✅ `connection_id` | ⚠️ **形式上能，语义上不能**（identity-scoped + 循环，S4） |
| `interactions` | ❌（只有 message 级键） | ❌ | ❌ |
| `activities` | ❌ | ❌（只有 `session_id`） | ❌ |
| `tenants` | ❌ | ❌ | ❌ |
| `user_credentials` | ❌ | ❌ | ❌ |
| `browser_sessions` | ❌ | ❌ | ❌ |

**唯一「形式上有两个键」的表是 `sessions`，而它恰好是最不能用的那个**
（授权必须发生在 Session 之前 —— ADR-7 硬约束）。

### E.2 判定

```
现有 Persistence 无法表达 Conversation → Connection 的授权关系
        ↓
0-table assumption 正式推翻（Gate 2 §6 需被 ADR-11 显式取代）
```

### E.3 `sessions` 的一个**正当用途**：一次性回填源（迁移，不是运行时）

这解决 default-deny 的冷启动问题：

```
既有部署中，凡存在过 Session 的 (channel_id, external_conversation_id, connection_id)
    ⇒ 该组合事实上一直在工作
    ⇒ migration 0007 可将其回填为授权记录
```

**这是显式的数据迁移动作，不是运行时自动授权** —— 不违反 ADR-6
（ADR-6 禁止的是「首次收到消息即写授权」这条**运行时**路径）。

---

## F. Unauthorized Failure Contract

### F.1 冻结契约（ADR-7 + Gate 2 §5）

```
processing_status = failed        （未授权 → 处理不成立）
delivery_status   = pending       （没有出站投递）
failure_stage     = authorization
failure_code      = UNAUTHORIZED
retryable         = false
session_id        = NULL
response_payload  = NULL
+ activities 一条：type=stage_failure, stage=authorization, status=failed
```

### F.2 与真实代码逐字段核对（**全部 PASS**）

| 字段 | 拒绝时的值 | 代码事实 | 核对结论 |
|---|---|---|---|
| `processing_status` | `failed` | `interaction_recording.py:361-362`：`PROCESSING_SUCCEEDED if stage == STAGE_DELIVERY else PROCESSING_FAILED` | ✅ `authorization` ≠ `delivery` → `failed`（**零改动**） |
| `delivery_status` | `pending` | 同处 `:363-364`：`DELIVERY_FAILED if stage == STAGE_DELIVERY else DELIVERY_PENDING` | ✅ `pending`（**零改动**） |
| `session_id` | `NULL` | `:366-369`：仅当 `stage in SESSION_LINKED_STAGES` 才查 Session；`SESSION_LINKED_STAGES = (STAGE_AGENT_DISPATCH, STAGE_DELIVERY)`（`:115`） | ✅ 不查、不建 Session（**零改动**） |
| `failure_stage` / `failure_code` | `authorization` / `UNAUTHORIZED` | `:356-357`：`STAGE_FAILURE_MAP.get(type(exc), (None, UNEXPECTED_FAILURE_CODE, False))` | ⚠️ **必须注册**，否则退化为 `UNEXPECTED_FAILURE`（不诚实） |
| `retryable` | `false` | `:360`：`bool(getattr(exc, "retryable", retryable))` | ✅ 映射表给 `False` 即可 |
| `response_payload` | `NULL` | `:351`：失败路径「响应事实不存在或未被返回，不可伪造」 | ✅ |
| Evidence | `activities` 一条 | 既有 `ACTIVITY_STAGE_FAILURE` + `metadata={failure_code, retryable}` | ✅ 与既有六段失败同形 |
| 拒绝如何被记录 | — | `worker.py:78-84`：`except Exception as exc: record_failure(...); raise` | ✅ **Worker 零改动**，typed error 走既有通道 |

### F.3 「不得自行新增状态」的遵守核对

- `processing_status` / `delivery_status` 的**取值集合完全不变**（既有常量）；
- **不新增 `status` 列**、不合成双状态（ADR guard `test_adr7_rejection_is_recordable_without_new_status_column:354-367` 机器锁定）；
- 新增的**只有 2 个分类取值**：`STAGE_AUTHORIZATION = "authorization"`（`domain/activity_record.py:18-23` 同形）与 `UNAUTHORIZED` 码，均落在既有列（`String(32)` / `String(64)`）内。

**结论：F = PASS，零 schema 变更、零新状态值、Worker 零改动。**

---

## G. Routing regression

### G.1 ADR-8 的硬要求

> `Authorization → Eligible Connections → Router`
> 推论：**Router 的输入集合 = 已授权 eligible connections；Router 不得自行扩大候选集。**
> 红线：❌ 不得把「授权」实现成 Router 内部的一个过滤器

### G.2 冲突点（必须显式裁决）

Gate 2 §7 第 8 项声称：

> **Routing**：授权**不消费 Connection 状态、不做唯一性裁决** →
> `0 条 / >1 条 → ROUTING_FAILED`、`agent_id` 派生自被选 Connection 全部不变
> → 锁定测试 `test_step9_4::*` 零更新

该声明在 **D2（恒 ALLOW）** 下成立。一旦 `Permit` 有真实来源，它就**不成立**：

- 若授权返回「该会话被允许的 connection 集合」，而 Router 仍只看
  `find_active_by_channel(channel_id)`，则 Router **自行扩大了候选集** → 违反 ADR-8；
- 若授权集合里有 C2 而 channel 的 active 只有 C1，Router 会选中 C1 → **授权被绕过**。

### G.3 推荐裁决（同时保住 Gate 2 §3.2 与 ADR-8）

把「授权」与「选择」在**数据上**切开：

```
Authorization 只读 grants（不读 Connection 状态）：
    granted(channel, conversation) ⊇ ∅            ← 唯一输入来源

DENY  ⇔  granted(channel, conversation) = ∅       ← ADR-6 的「缺记录 = 不允许」

Router 的候选集 = active(channel) ∩ granted(channel, conversation)
    0 条或 >1 条 → ROUTING_FAILED                 ← 唯一性语义**零改动**
```

好处：

| 冻结面 | 是否保住 |
|---|---|
| Gate 2 §3.2「判定不消费 Connection 状态」 | ✅ **保住**（授权只看 grants） |
| Gate 2 §7 第 8 项「`0/>1 → ROUTING_FAILED`、`agent_id` 派生不变」 | ✅ **保住** |
| ADR-8「Router 只消费 eligible、不自行扩大候选集」 | ✅ 满足 |
| 「授权成 Router 内部过滤器」红线 | ✅ 未违反（授权在 Router 之前独立成阶段） |
| **「Routing 零改动」** | ❌ **必须修订**：候选集来源变了（见下） |

**`granted 非空但 active ∩ granted = ∅` 的语义**：这是**路由/配置失败**（授权成立、
路径不可用），报 `ROUTING_FAILED`，**不是** `UNAUTHORIZED`。理由：把「路径不可用」
谎报成「未授权」会让运维查错方向。

### G.4 因此需要修订的 Gate 2 断言

```
Gate 2 §7 第 8 项「Routing 零改动」
    → 修订为「Routing 的**唯一性语义与 agent_id 派生**零改动；
       候选集从 active(channel) 收窄为 active(channel) ∩ granted(conversation)」
```

受影响的锁定测试（`RoutingExecutor.execute(channel_id)` 的调用点）：
`test_step9_4_routing_execution.py`（8 处）、`test_step10_3_1_routing_isolation.py`、
`test_adr8_no_multi_agent_routing_expansion`（要求 `"if len(connections) != 1" in src`
与 `RoutingFailedError in src` —— **该形状应保留，过滤后再判数量**）。

---

## H. 完整 regression matrix

### 第 1 类：行为**必须完全不变**（继承 Gate 2 §7，11 项，复核后仍成立）

| # | 行为 | 机理 | 代表锁定测试 |
|---|---|---|---|
| 1 | WeCom single | Adapter 与接纳路径零改动 | `test_3b_3_3::test_a2_group_mapping_*` |
| 2 | WeCom group | `chattype=group → chatid` 不变 | `test_3b_3_3::test_c1..c4_group_session_matrix_*` |
| 3 | Identity resolution | 零改动（仍 Fail Closed）——Package 1 后的 D3 语义保持 | `test_step9_2::test_b_*`、`test_step8_2::test_a_failure_code_frozen` |
| 4 | Session reuse | 授权在其之前；三维隔离键不变 | `test_step9_3::*`、`test_step10_6_2::test_a1_*` |
| 5 | Tenant isolation | 授权**不接触 tenant**；coordinator 仍不含 `tenant` 字面量 | `test_cloud_0_1_1_b_tenant_fanout::test_fanout_does_not_change_runtime_execution_logic` |
| 6 | Channel authentication | Adapter 验签零改动 | `test_cloud_0_1_3_1::test_b1_*` |
| 7 | Connection resolution | `find_active_by_channel` 语义不变 | `test_step11_11_*`、`test_step7_3_*` |
| 8 | Routing **唯一性** | `0/>1 → ROUTING_FAILED`、`agent_id` 派生不变 | `test_step9_4::test_a_*`、`test_adr8_no_multi_agent_routing_expansion` |
| 9 | Delivery | 拒绝不进 Delivery；双状态语义不变 | `test_step9_6::test_d_*`、`test_step8_6::test_f_*` |
| 10 | Duplicate callback | 接纳路径（幂等 → ACK+STOP）零改动 | `test_step12_9_4_9::test_b_*`、`test_step10_5_3::test_d1_*` |
| 11 | Worker retry / reclaim | 拒绝 = 终态 `failed`（非 PROCESSING）→ 不进 reclaim；`retryable=false` | `test_step12_9_4_12::test_c1_*`、`test_step12_9_4_17::test_3/test_5` |

### 第 2 类：**集合显式更新**（不是回归，是有序变更）

| 测试 | 更新什么 | 不得改变 |
|---|---|---|
| `test_step9_8_1::CANONICAL`（`:43-44`）+ `test_order_is_canonical_six_stages`（`:97`） | 6 段 → 7 段，加入 `authorization`；**测试名里的 "six" 需一并修正** | 既有六段**相对顺序** |
| `test_cloud_0_1_1_a::test_runtime_execution_order_frozen` | 常量元组 +1 | 「单一源」纪律 |
| `test_step9_1_runtime_execution.py`（18 处引用） | 顺序集合 | 边界纪律 |
| `test_step9_A`、`test_step10_1_4`、`test_step9_8_2`、`test_step10_8_2/3/4`、`test_step10_6_1`、`test_step10_7_2` | spy trace / 调用序列 +1 项 | 每段实参形状；REUSE 仍跳过 session_creation |
| `test_step10_4_2::test_a2_failure_map_covers_all_runtime_typed_errors` | 覆盖新 typed error | 既有条目不改 |
| `test_step10_1_4::test_six_failure_stages_map_to_distinct_typed_errors` | 6 → 7 | 「每阶段只抛自己的错」 |

### 第 3 类：生成侧必修（Gate 2 §5.3 的「诚实性要求」）

```
app/domain/activity_record.py        + STAGE_AUTHORIZATION = "authorization"
app/application/interaction_recording.py
                                     + STAGE_FAILURE_MAP[<new error>] = (STAGE_AUTHORIZATION, "UNAUTHORIZED", False)
app/application/runtime.py           + RUNTIME_EXECUTION_ORDER 插入 "authorization"
```
→ 缺失任一项，拒绝都会落成 `failure_stage=NULL / code=UNEXPECTED_FAILURE`（不诚实）。

### 第 4 类：**新增 —— 冷启动 default-deny（本次审计的最大风险面）**

ADR-6 要求 default-deny；一旦 `Permit` 有真实来源且**既有部署没有任何授权记录**：

```
既有 E2E 成功路径 → 全部转为 DENY（UNAUTHORIZED）
```

已量化的影响面：

| 指标 | 数量 | 说明 |
|---|---|---|
| 触及 Runtime 链条的测试文件 | **55** | `RoutingExecutor` / `SessionCreationExecutor` / `RuntimeCoordinator` / `PersistingCoordinator` |
| **断言成功终态常量**的文件 | **11** | `PROCESSING_SUCCEEDED` / `DELIVERY_DELIVERED`（`test_3b_2_2`、`test_cloud_0_1_4_1`、`test_cloud_0_1_5`、`test_step10_5_3`、`test_step12_9_4_*`、`test_step15_2_3` 等） |
| 其余约 44 个 | 间接断言成功 | 通过回复内容 / 结果对象断言（`test_step9_8_3`、`test_step10_1_4*`、`test_step10_2_2`、`test_step10_3_1_multi_agent_e2e`、`test_step12_6`、`test_step15_5_2`、`test_3b_3_3` …） |

**这是必须在 Review 中裁决的 rollout 问题**，两个候选（本 Gate 不裁决）：

```
候选 A：migration 0007 从既有 sessions 回填授权（见 §E.3）
        → 新部署空库 = 全 DENY（正确，但需管理面先建授权）
候选 B：引入「显式 bootstrap 授权」的管理动作（UX: Where should it work?）
        → 与 ADR-10 一致，但仍需回填解决既有部署
```

并且明确：**55 个文件的 fixture 面枚举**是 Package 2-B 的第一个任务
（本 Gate 只给出数量级，未逐一分类，避免在未裁决 rollout 前做无用功）。

### 第 5 类：Targeted Tests（实现步新增，本 Gate 不写）

- DENY 路径：不建 Session / 不调 Agent / 不 Delivery；
- 拒绝落库形状（五元组 + Evidence）；
- 判定**不消费** Identity / User / content（可用 Fake 判定源构造）；
- 群聊 `G1 → C1 = ALLOW`（一个群、多成员、单 Connection）；
- 未授权 conversation 与已授权 conversation 在同 channel 的行为差异；
- `granted 非空但 active ∩ granted = ∅` → `ROUTING_FAILED`（不是 `UNAUTHORIZED`）；
- Worker 拒绝后不进 reclaim。

---

## 9. 最小持久化边界提案（**本 Gate 不实施**）

形状（名称必须由 ADR-11 裁决，此处只给**必要性证明**）：

```
<conversation access relation>          ← 一张表，表达「允许」
├── id                                  ← 与其他表同形
├── tenant_id      FK tenants.id   NOT NULL   ← 直接租户归属（RLS 第二层防线，不靠 join）
├── channel_id     FK channels.id  NOT NULL   ← conversation 键的 scope（ADR-3）
├── external_conversation_id  String(255) NOT NULL
├── connection_id  FK connections.id NOT NULL ← 授权目标（ADR-2 / ADR-8）
├── created_at / updated_at
└── UNIQUE(tenant_id, channel_id, external_conversation_id, connection_id)
```

**最小性证明（每列都必要、且不多一列）**

| 元素 | 为什么必要 |
|---|---|
| `channel_id` + `external_conversation_id` | ADR-3 的作用域键（会话键只在 Channel 内解释） |
| `connection_id` | ADR-2/ADR-8 的目标域 |
| `tenant_id` | 与 8 张业务表同纪律；RLS 需要**自有列**，不能经 join 推导 |
| 四元组 UNIQUE | 授权幂等（重复授予不产生第二行） |

**明确不需要的（ADR-9 红线）**

```
❌ status / effect / priority / expires_at / conditions   （纯 allow 集合；deny = 无记录）
❌ identity_id / user_id                                  （ADR-1/ADR-3：作用域不得下沉）
❌ conversation 实体表                                     （ADR-3 红线；只存裸键列）
❌ role / permission / policy / principal / scope / acl 下的任何列或表
```

**`deny` 不需要存储**：ADR-6「缺授权记录 = 不允许」意味着 deny 由**缺失**表达 ——
这让模型保持「一个纯 allow 关系」，而不是 effect/优先级系统。

**写入路径**（谁创建授权）—— 需 ADR-11 + 管理面裁决，最小候选：
应用层新增一个 service + repository port + 独立 API 路由；
**不得**在 `Connection` 域新增字段或 `authorize()/allow()/deny()` 方法
（ADR-2 红线 + `_AUTH_FIELD_FRAGMENTS` 含 `grant`，机器锁定）。

---

## 10. 需要**显式修订**的冻结面清单（不得绕过）

| # | 冻结物 | 现在写着什么 | 必须怎么改 |
|---|---|---|---|
| M1 | **ADR §八**「任何 schema 变更（C2 已裁决：不需要）」 | 禁止 schema 变更 | **ADR-11 显式取代**（新表因授权数据源必需） |
| M2 | **Gate 2 §6**「0 new tables / NO migration ✅」 | 0 表结论 | 显式取代；引用其 §6.2 代价 #3 的升级触发作为依据 |
| M3 | **Gate 2 §3.2 D2**「Permit = 全量 → 恒 ALLOW」 | placeholder | Review 否决 D2；`Permit` 改为读授权关系 |
| M4 | **Gate 2 §7 第 8 项**「Routing 零改动」 | 零改动 | 修订为「唯一性语义零改动，候选集收窄」（§G.4） |
| M5 | ADR guard `_FORBIDDEN_PERMISSION_TABLES`（含 `access_grants`）与 `_FORBIDDEN_PERMISSION_CLASSES`（含 `AccessGrant`/`GrantModel`/`PermissionGrant`） | 禁止这些名字 | **由 ADR-11 显式修订白名单**，而不是悄悄选一个不在清单里的名字 |
| M6 | ADR guard `test_adr9_no_authorization_module_in_domain_layer`（domain 不得有 `authorization.py` 等） | 禁 module stem | 同上：**显式修订**，不是靠改名规避 |
| M7 | ADR guard `test_adr6_no_implicit_grant_on_first_message`（禁 `AuthorizationGrant`/`AuthorizationDecision` 类） | 禁类名 | 该守卫**自己写了**「实现步到来时本断言应按 ADR 演进更新」（`:331`）→ 按 ADR-11 更新 |

> **纪律声明**：M5/M6 是本次审计最容易被「技术性规避」的两处。
> 本项目在 Package 1 已确立先例（`User | None` 让 8.2 守卫报红 → **同步修改守卫并写清理由**，
> 而不是改写法让守卫恰好通过）。这里沿用同一纪律：**修改守卫必须由 ADR-11 授权并写明理由。**

---

## 11. 状态列表

### FROZEN（本 Gate 未改动，仅继承并复核）

```
Runtime 插入点 = P2（Identity 之后 / Routing 之前）        A
最小输入 = (channel_id, external_conversation_id)          B
判定结果 = ALLOW | DENY（无 Effect / 优先级 / 条件）        ADR-9
拒绝契约 = failed/pending/authorization/UNAUTHORIZED/false/NULL + 1 条 Evidence   F
顺序硬约束 = C1 Identity 之后 / C2 Session Resolution 之前 / C3 Routing 之前 / C4 拒绝仍记录  ADR-7
D3（Package 1）：Identity 命中即 RESOLVED，User 缺失不影响    已实现
```

### PASS

```
A  落点与 6 条既有 coordinator 守卫全部兼容；ADR-7 的 index 顺序守卫无需修改
B  两个输入键已在入站契约与 sessions 各为一等字段（ADR guard 已锁定）
F  拒绝契约与真实代码逐字段核对通过：0 新列 / 0 新状态值 / Worker 零改动
G  路由「唯一性语义 + agent_id 派生」可零改动保住（候选集收窄即可）
E.3  sessions 可作为既有部署的一次性授权回填源（迁移，不是运行时授权）
```

### BLOCKER

```
C  V0 ALLOW/DENY 的**真实数据来源不存在**（6 个候选全否决，含被禁的恒 ALLOW）
D  0-table assumption **不成立**（推翻）；Gate 2 §6 的结论有前提且前提已被否
E  现有 Persistence **无法表达** Conversation → Connection 授权关系（E.1 逐表否决）
→ 需要 ADR-11（新编号 + 显式取代 M1/M2/M3/M4/M5/M6/M7）才能进入实现
```

### NOT STARTED

```
✗ Authorization 阶段实现（executor / typed error / coordinator 接线）
✗ 授权持久化（表 / migration 0007 / RLS 策略 / repository port / mapper）
✗ 授权写入路径（管理 API；UX「Where should it work?」）
✗ Targeted Tests（第 5 类）
✗ 既有 55 个 runtime 测试文件的 fixture 面枚举与改造（依赖 rollout 裁决）
✗ 真实渠道回归（WeCom → Authorization 端到端）
✗ FREEZE / Package 2 完成声明
```

### Production Changes Required（实现步；本 Gate 不实施）

| # | 文件 | 变更 |
|---|---|---|
| P1 | `app/domain/activity_record.py` | `+ STAGE_AUTHORIZATION = "authorization"` |
| P2 | `app/application/errors.py` | 新增 typed error（**命名不得含 Service/Manager/Orchestrator**；`retryable=False`） |
| P3 | `app/application/<authz>.py` | 新增执行组件（输入 2 键 → granted 集合；`Permit` 读授权关系） |
| P4 | `app/application/interaction_recording.py` | `STAGE_FAILURE_MAP` 注册新 error → `(authorization, UNAUTHORIZED, False)` |
| P5 | `app/application/runtime.py` | `RUNTIME_EXECUTION_ORDER` 插入 `"authorization"` |
| P6 | `app/application/coordinator.py` | 新增唯一阶段（Identity 之后 / Routing 之前）；候选集传给 Routing |
| P7 | `app/application/routing.py` | 候选集 = `active(channel) ∩ granted(conversation)`（唯一性语义不变） |
| P8 | `app/repositories/*.py` + `app/persistence/repositories/*.py` + `app/persistence/mappers/*.py` | 新 port + adapter + mapper |
| P9 | `app/persistence/models/*.py` | 新 ORM 表（含 `tenant_id` + 四元组 UNIQUE + 索引） |
| P10 | `app/api/management/*.py` + `app/schemas/*.py` + `app/main.py` | 授权写入面（形状待 ADR-11/UX 裁决） |

### Migration Required

```
YES —— 新增 1 个 migration（编号 0007，revises 0006）：
  1) create table（含 tenant_id FK / 四元组 UNIQUE / ix_<t>_tenant_id）
  2) PostgreSQL: ENABLE + FORCE ROW LEVEL SECURITY + tenant_isolation policy（与 8 张表同形）
  3) 既有部署回填（从 sessions 推导，见 §E.3）—— 是否包含在本 migration 需 Review 裁决
另：SQLite 路径由 create_all 承担（与 Package 1 / 0006 同处理）。
```

### Tests Required

```
必修（生成侧诚实性，第 3 类）：
  test_step9_8_1（CANONICAL + 名称 "six"）· test_cloud_0_1_1_a::test_runtime_execution_order_frozen
  test_step9_1（18 处）· test_step9_A · test_step10_1_4 · test_step9_8_2 · test_step10_8_2/3/4
  test_step10_6_1 · test_step10_7_2 · test_step10_4_2（failure map 覆盖）
新增守卫（ADR-11 授权范围）：
  M5/M6/M7 三处 ADR guard 白名单的显式修订
新增 Targeted Tests（第 5 类，7 项）
fixture 面（第 4 类）：55 个文件待枚举，11 个已确认断言成功终态
```

---

## 12. 本次未做（只读声明）

```
❌ 未改生产代码 / 未改测试 / 未建 migration / 未改 schema
❌ 未创建任何 Authorization 类 / executor / port / model / 表
❌ 未改 Runtime order / 未改 Routing / 未改 STAGE_* / 未改 STAGE_FAILURE_MAP
❌ 未改 ADR 文档与 ADR guard（M1~M7 仅**登记为待修订**，未动）
❌ 未裁决 rollout（候选 A/B 仅列出，留给 Review）
❌ 未实现任何形式的数据源（包括未实现 §9 的表）
❌ 未用「恒 ALLOW」placeholder 冒充授权（明确否决 S1）
```

---

## 13. 下一步（建议顺序）

```
Gate 2-A（本文件）              ✅ DONE / DESIGN + BLOCKER
        ↓
Gate 2-A Review                 裁决：C/D/E 的 BLOCKER 承认 + rollout 候选 A/B + §9 表形状
        ↓
ADR-11（新增编号，显式取代）      修订 M1~M7；签发最小持久化边界与写入面
        ↓
Package 2-B                    实现：Migration 0007 → port/model → 阶段 → coordinator/routing → 管理面
        ↓
Targeted Tests（第 5 类）+ fixture 面（第 4 类）
        ↓
Authorization regression → Real WeCom regression
```

**关键约束（写给未来的自己）**：在 ADR-11 签发之前，
**不得**以「先放一个恒 ALLOW 的占位、以后再接数据源」的方式推进实现——
那正是本次指令明确禁止的形态，也正好是 Gate 2 §6 的 0 表结论得以成立的**唯一**前提。
