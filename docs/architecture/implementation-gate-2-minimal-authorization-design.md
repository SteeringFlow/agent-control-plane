# Implementation Gate 2 — Minimal Authorization Design（最小授权设计）

> 性质：**只读设计**。未改生产代码、未改测试、未改冻结契约、未建 migration、
> 未创建 AuthorizationService / Policy 类、未改 `user_id`、未改 Runtime order。
> 输入：`docs/architecture/authorization-and-access-model.md`（ADR-1 ~ ADR-10，FROZEN）
>      `docs/architecture/implementation-gate-1-authorization-impact-audit.md`（Gate 1 审计）
> 产出：本文件（唯一产出）+ 其中的设计图与决策表。

```
阶段状态
  Authorization Architecture   G — PASS / FREEZE
  Read-only Impact Audit                          DONE（Gate 1）
  Minimal Authorization Design                    ← 本文件（Gate 2）
  Review                                          NOT STARTED
  Implementation                                  NOT STARTED
  Targeted Tests / Full Regression                NOT STARTED
  Real Channel Integration                        NOT STARTED
  Freeze                                          NOT STARTED
```

---

## 0. 结论摘要

| # | 问题 | 结论 |
|---|---|---|
| 1 | 无 ACP User 的外部参与者如何表示？ | **不表示**（不建 Identity row）。`identities.user_id NOT NULL` **不是授权层的 blocker**；真正的链式约束是 **`sessions.identity_id NOT NULL`** + `SessionCreationExecutor` 要求 `identity_id > 0`。V0 结论：未绑定参与者**不进 Runtime 执行**（在既有 Identity Resolution 处 STOP），**不改 schema** |
| 2 | Authorization 的必要输入？ | **`channel_id` + `external_conversation_id`** 两项。**Identity/User 不是输入**（ADR-1/ADR-3）；**Connection 不是输入**，是判定的**目标域**（eligible set） |
| 3 | Decision | 只有 **ALLOW / DENY**。无 Policy / Role / Permission / Scope Engine / Rule Engine |
| 4 | Runtime 落点 | 推荐 **P2（执行期）**，具体为 **Identity Resolution 之后、Routing 之前**；拒绝 P1、拒绝 P3 |
| 5 | 拒绝如何记录？ | **现有模型可以完整表达**：`processing=failed` + `failure_stage=authorization` + `failure_code=UNAUTHORIZED` + `retryable=false` + `session_id=NULL` + 一条既有 `stage_failure` Evidence。**不新增状态、不新增列**；仅新增 **2 个取值** |
| 6 | Persistence | **0 new tables / 0 new columns / NO migration —— 仍然成立**（附取舍与升级触发条件） |
| 7 | 既有行为 | 11 项逐条给出「为何不受影响」+ 对应锁定测试；其中「顺序/失败映射」属**集合显式更新**（只改集合、不改语义） |

**必须由 Review 裁决的 2 个语义收敛点**（本设计给出推荐 + 理由，未单方面拍板）：

- **D1（未绑定参与者）**：V0 是否接受「未绑定参与者 = 不进入执行（Identity Resolution 处 STOP）」，
  即 **Authorization 不在 V0 扩大可执行人群**。
- **D2（DENY 可达性）**：V0 的 DENY **在实际行为上是不可达的**（许可集合 V0 = 全量）。
  是否接受「结构性授权（语义位 + 未来唯一插点），V0 恒 ALLOW」。

---

## 1. External Identity — 未拥有 ACP User 的外部参与者如何表示？

### 1.1 真实约束链（逐条给出代码事实）

```
外部参与者（有人发消息）
      │
      │  ①  Runtime: IdentityResolutionExecutor
      │      find_by_channel_and_external_id → None
      ▼      或 命中 Identity 但 users.get_by_id → None
  IdentityResolutionFailedError            ← Fail Closed（8.2/9.2 冻结）
      │
      │  ②  即使 Identity 存在：IdentityModel.user_id  NOT NULL (FK users.id)
      │     → 一条 Identity 行必然指向一个 User
      ▼
  Identity.user_id  ──┐
                      │  ③  SessionModel.identity_id  NOT NULL (FK identities.id)
                      │     且 SessionCreationExecutor 要求 identity_id > 0
                      ▼
             会话 / 执行都必须有 Identity
```

| 约束 | 位置 | 性质 |
|---|---|---|
| 运行时 Fail Closed | `app/application/identity_resolution.py`（两处 raise） | **策略**（可裁决） |
| `identities.user_id` NOT NULL | `app/persistence/models/identity.py` | **Schema**（不可动，见 §6） |
| `sessions.identity_id` NOT NULL | `app/persistence/models/session.py` | **Schema**（不可动） |
| `SessionCreationExecutor` 要求 `identity_id > 0` | `app/application/session_creation.py` | **契约 + 测试冻结** |
| 仅管理面可创建 Identity | `app/application/identities.py`（6.6）+ ADR-5 禁止运行时创建 | **冻结** |

### 1.2 `identities.user_id NOT NULL` 是否真的是 blocker？

**分层回答（这是本设计最容易搞错的一点）**：

- **对 Authorization 而言：不是 blocker。** Authorization 的输入是
  `(channel_id, external_conversation_id)`（ADR-1/ADR-3），**完全不消费 Identity/User**。
  因此「未拥有 ACP User」不会让授权判定失败——它**根本不需要** Identity。
- **对「未绑定参与者能否被执行」而言：它是链上的一环，但不是最后一环。**
  即使放宽 `user_id`，仍然会撞上 **`sessions.identity_id NOT NULL`**：
  Control Plane Session 的隔离键第一维就是 `Identity.id`（8.3 冻结）——
  **没有 Identity 就没有 Session，没有 Session 就没有 `control_plane_session_id`，
  就无法 Dispatch**（`AgentRequest.control_plane_session_id` 是必填）。

**结论：`identities.user_id` 不是授权层的 blocker；真正的 blocker 是
「Session 是 identity-scoped 的」这一既有冻结语义。** 而改它需要放宽
`sessions.identity_id`（新 schema + 改 8.3 隔离键 + 改群聊会话矩阵测试），
与「0 表 / 0 列 / NO migration」直接冲突。

### 1.3 三条可选路线（评审用决策表）

| 路线 | 做法 | Schema | 冻结契约 | 对本步要求的满足 | 结论 |
|---|---|---|---|---|---|
| **R-A（推荐）** | 未绑定参与者**不进入执行**：仍在 Identity Resolution 处 Fail Closed；Authorization 只在**已绑定**流量上做会话级判定 | **0 改动** | **0 改动** | 满足 0/0/NO | ✅ |
| R-B | 身份解析对未绑定者「非阻断」，让授权先判 | 需要放宽 `sessions.identity_id`（否则 Session 建不出来） | 改 8.2/9.2（失败码冻结）+ 8.3（隔离键）+ 群聊矩阵测试 | ❌ 破坏 §6 与 §7 | ❌ 非最小 |
| R-C | 把 Session 改为 conversation-scoped（actor=会话） | 新列/新唯一键 | 改 8.3 + 全部 session/群聊测试 | ❌ 破坏 §6 与 §7 | ❌ 非本次范围 |

**推荐 R-A，理由**：它把 ADR-1 的承诺**精确化**为「**授权不以 Identity/User 为输入，
且系统不为授权创建 Identity/User**」，而**不**承诺「未绑定参与者可被处理」——
后者是 Session 语义问题，属另一个 Gate。

> **必须在 Review 中明确写下的语义边界**（否则会造成误解）：
> ADR-1 ≠「匿名参与者可以对话」。ADR-1 约束的是**授权的判定输入**与**禁止自动开户**。

### 1.4 不改 schema 时，Session / Audit 语义如何保持？

| 语义 | 不改 schema 如何保持 |
|---|---|
| **Session** | 完全不参与授权。授权位于 Session Resolution **之前**；未授权 → 不进入 Session Resolution，因此**不产生 Session**（也不会出现 `identity_id` 缺失的行）。执行路径上 Session 仍要求 `Identity.id`（既有语义，零变化） |
| **Audit** | 拒绝**不需要 Session 也能被完整记录**：`interactions.session_id` 可空是 10.4.2 已确立的 schema 修正案（「Identity/Routing 等 Session 建立之前的失败也要被记录」）；拒绝路径的 `session_id = NULL`（见 §5 的 `SESSION_LINKED_STAGES` 事实） |
| **事实保全** | 入站事实（含 `external_conversation_id` / `external_identity_id`）已在 `request_payload` 快照中；拒绝时 `response_payload` 保持 NULL（不伪造响应） |

---

## 2. Authorization Input — 到底哪些是必要输入？

| 候选 | 是否必要输入 | 依据 | 在 V0 判定中的角色 |
|---|---|---|---|
| **Conversation**（`external_conversation_id`） | **必要** | ADR-3：Authorization Scope = Conversation | 判定的**主语** |
| **Channel**（`channel_id`） | **必要（由 Conversation 键隐含）** | 会话键只在 Channel 内解释；`external_conversation_id` 本身无全局唯一性（群 `chatid` / 单聊 `userid` 都只在 Channel Scope 内有意义） | 判定的**作用域** |
| **Connection**（`connection_id`） | **不是输入** | ADR-8：Authorization → Eligible Connections → Router（Connection 是**产出**侧） | 判定的**值域**（eligible set），V0 不消费其状态（见 D2） |
| **Identity**（`identity_id` / `identity.external_id`） | **不是输入**（**禁止消费**） | ADR-1 / ADR-3：External Identity 不要求拥有 ACP User | 仅作为**可观测事实**（Identity Resolution 仍在其之前执行并留下 Evidence） |
| **User**（`user_id`） | **不是输入**（**禁止消费**） | 同上；且 ADR-5 禁止自动开户 | 无 |
| **content**（消息正文） | **不是输入** | 内容不得参与访问判定（否则滑向 ABAC / 内容策略） | 无 |

```
最小输入集：  (channel_id, external_conversation_id)
判定输出：    ALLOW | DENY
值域（未来）：eligible connection set（V0：≤1，且唯一性仍归 Routing）
```

> **红线**：判定实现**不得**读取 `identity_id` / `user_id` / `external_identity_id` / `content`。
> 这与 Gate 1 §C1 的推论一致：「授权不得以身份解析成功为前置」。

---

## 3. Authorization Decision — 只定义最小结果

### 3.1 结果集（**仅两个**）

```
ALLOW   →  继续既有管线（Routing → Session Resolution → …）
DENY    →  停止管线（不 Routing、不 Session、不 Dispatch、不 Delivery），记拒绝事实
```

### 3.2 V0 判定式（确定性、无规则引擎）

```
eligible = Permit( channel_id, external_conversation_id )        # 许可集合
DENY  ⇔  eligible is empty
ALLOW ⇔  otherwise
```

- **`Permit` 在 V0 = 全量**（不查任何 deny/grant 记录）→ 判定**恒 ALLOW**；
  这是 §0 的 **D2**，需 Review 显式确认。
- 判定**不消费 Connection 状态**：`Connection` 的存在性与唯一性**仍完全归 Routing**
  （0 条 / >1 条 → `RoutingFailedError`，既有冻结语义与测试**零变化**）——
  这是保护 §7「Connection resolution / Routing」回归的关键设计选择。
- 判定**不做唯一性裁决**：|eligible| 为 1 或多条都不是授权的事（选择归 Router）。

### 3.3 明确不设计（写死边界）

```
❌ Policy / PolicyEngine / PolicyRule / PolicyDSL
❌ Role / Permission / Principal / Resource / Scope / Effect / ACL
❌ Rule Engine / 优先级 / 条件组合 / 继承
❌ 内容 / 属性 / 风险评分参与判定
❌ 会话许可集合的**存储形态**（这属未来 ADR；V0 无记录）
❌ 任何形式的 AuthorizationService / Manager / Orchestrator 命名与分层设计
```

> 本 Gate 只冻结「判定输入 + 结果 + 落点 + 记录语义」。**判定规则的来源**
> （未来 permit/deny 从哪来）**留给未来 ADR**，本文件不设计。

---

## 4. Runtime Placement — P1 / P2 / P3 对比与推荐

### 4.1 三候选对比（基于 Gate 1 的真实两阶段结构）

| | **P1 Ingress** | **P2 Execution（推荐）** | **P3 Both** |
|---|---|---|---|
| 落点 | `ChannelInboundService.accept_message`（`create_pending` 之前） | `RuntimeCoordinator.execute`：**Identity 之后、Routing 之前** | 两处 |
| 未授权请求是否落库 | 需在“不落库”与“造一条 rejected 行”之间二选一 | **落库**（既有 Interaction 行 → 终态 failed） | 落库 + 前置拦截 |
| 触碰的冻结语义 | **10.2.1**（入站异常不进 Runtime Failure Model）+ **12.9.4.6**（ACK ≠ 执行）+ **12.9.4.9**（重复一律 ACK+STOP） | `RUNTIME_EXECUTION_ORDER` + `STAGE_*` + `STAGE_FAILURE_MAP`（**集合显式更新**，语义不变） | 叠加两者 |
| ADR-6（Discovery ≠ Authorization） | 紧张：把「许可」塞进「接收」 | 自洽：发现照常记录，许可在执行期判定 | 自洽 |
| ADR-7（未授权不进 Session） | 满足（更早） | 满足 | 满足 |
| ADR-8（Authorization 先于 Routing） | 满足 | 满足 | 满足 |
| 拒绝可观测性 | 差（要么无行，要么非规范行） | **好**（复用既有双状态 + 三元组 + Evidence） | 好 |
| 重复/幂等语义 | 受污染（ACK 路径被判定逻辑侵入） | **零影响**（接纳路径零改动） | 受污染 |
| 维护成本 | 低 | 低 | **双份判定点 = 双份真相源** |
| 结论 | ❌ 拒绝 | ✅ **推荐** | ❌ 拒绝 |

### 4.2 推荐：**P2**，具体顺序

```
InboundInteraction
      ↓
Identity Resolution        ← 既有，零改动（仍 Fail Closed）
      ↓
★ Access Authorization     ← 新增唯一阶段（判定见 §3；输入见 §2）
      ↓
Routing                    ← 既有，零改动（唯一性仍归它）
      ↓
Session Resolution
      ↓
[Session Creation?]
      ↓
Agent Dispatch
      ↓
Delivery
```

**为什么落在这里**：

1. 满足 ADR-7 的三条硬约束（Identity 之后 / Session Resolution 之前 / Interaction 仍记录）；
2. 满足 ADR-8（在 Routing 之前，Router 只消费已许可集合）；
3. **接纳路径（ACK / 验签 / 幂等 / tenant 解析）零改动** → Webhook、重复回调、
   群聊映射、RLS 全部不动；
4. 拒绝路径**复用**既有 `record_failure` 机制（§5），不需要新的记录设施；
5. Worker / Driver 零改动（`Worker.process` 已对 Runtime 的 typed error 统一
   `record_failure` 并上抛 —— 拒绝走同一条既有通道）。

**为什么拒绝 P1**：会在「接收边界」引入访问判定，必须重新定义 ACK 语义
（200 还是 403？未授权是否落库？是否入队？），直接冲突于 10.2.1 / 12.9.4.6 / 12.9.4.9
三条已冻结裁决——这不是「最小」，而是「把两个边界重新谈一遍」。

**为什么拒绝 P3**：两个判定点 = 两个真相源；ingress 判定不会带来 P2 之外的任何额外
保证（P2 已保证不产生 Session / 不调用 Agent / 不投递），却要付出污染 ACK 语义的代价。

### 4.3 该落点带来的「冻结面显式更新」清单（只改集合，不改语义）

| 冻结面 | 变化 | 性质 |
|---|---|---|
| `application/runtime.py::RUNTIME_EXECUTION_ORDER` | 插入 `authorization` 条目 | 集合更新 |
| `domain/activity_record.py::STAGE_*` | 新增 `STAGE_AUTHORIZATION = "authorization"` | 取值新增 |
| `application/interaction_recording.py::STAGE_FAILURE_MAP` | 注册新 typed error → (`authorization`, `UNAUTHORIZED`, `False`) | 单一源扩展 |
| `SESSION_LINKED_STAGES` | **不变**（不含 authorization → 拒绝路径 `session_id=NULL`） | 无变化 |
| 顺序/调用顺序断言测试 | 集合更新（见 §7 第 2 类） | 集合更新 |

### 4.4 落点必须遵守的既有守卫（设计约束，非建议）

| 守卫（真实测试） | 对本设计的约束 |
|---|---|
| `test_fanout_does_not_change_runtime_execution_logic`：`"tenant" not in coordinator.py.lower()` | 新阶段在 coordinator 中**不得出现 "tenant" 字面量**（连注释也不行）；tenant 作用域只能经**注入的 UoW 工厂**在 executor 内部生效 |
| `test_coordinator_has_no_repository_orm_adapter_identifiers`：禁止标识符含 `Repository/SqlAlchemy/ORM/Model/Engine/Adapter` | 新阶段的依赖只能是注入的抽象，命名不得含上述子串 |
| `test_coordinator_does_not_swallow_stage_errors`：coordinator 内**无 `ast.Try`** | 拒绝必须**抛 typed error**，不得在 coordinator 里 catch |
| `test_coordinator_never_resolves_session_by_user_id`：`"user_id" not in coordinator.py` | 同上：coordinator 内不得出现 `user_id` 字面量（与 §2「Identity/User 非输入」一致） |
| `test_coordinator_session_branch_has_no_hidden_branch`：恰 2 个 `decision.mode` If | 新阶段不得引入第三个 `decision.mode` 分支 |
| `test_outbound_construction_is_minimal`：恰 1 处 `OutboundResponse(...)`、恰 4 个 kwargs | 不变 |
| 10.4.1 / 10.4.2 / 10.5.1 / 10.5.2「Runtime 零 persistence 接触」 | 新阶段实现**不得 import** `app.persistence` / `sqlalchemy` / interaction 记录仓库 |

---

## 5. Interaction / Activity Semantics — 未授权请求如何进入现有体系

### 5.1 结论：**现有模型可以表达，不需要新增状态**

依据真实代码（`application/interaction_recording.py::record_failure`）：

```python
stage, code, retryable = STAGE_FAILURE_MAP.get(type(exc), (None, UNEXPECTED_FAILURE_CODE, False))
retryable = bool(getattr(exc, "retryable", retryable))
processing = PROCESSING_SUCCEEDED if stage == STAGE_DELIVERY else PROCESSING_FAILED
delivery   = DELIVERY_FAILED     if stage == STAGE_DELIVERY else DELIVERY_PENDING
session_id = None unless stage in SESSION_LINKED_STAGES   # = (agent_dispatch, delivery)
→ record_outcome(processing, delivery, failure_stage, failure_code, retryable, session_id)
→ ActivityRecord(ACTIVITY_STAGE_FAILURE, stage=stage, status="failed",
                 message=str(exc), metadata={failure_code, retryable},
                 interaction_id, session_id)
```

### 5.2 拒绝路径的逐字段落点（**全部复用既有列/取值域**）

| 字段 | 拒绝时的值 | 是否已有 | 说明 |
|---|---|---|---|
| `processing_status` | `failed` | ✅ 既有取值 | `PROCESSING_FAILED` 已存在 |
| `delivery_status` | `pending` | ✅ 既有取值 | 无出站投递 |
| `failure_stage` | `authorization` | ⚠️ **新取值**（列 `String(32)` 既有） | 13 字符，长度充足 |
| `failure_code` | `UNAUTHORIZED` | ⚠️ **新取值**（列 `String(64)` 既有） | 12 字符；必须在 `STAGE_FAILURE_MAP` 注册（否则退化为 `UNEXPECTED_FAILURE`，即**不诚实**） |
| `retryable` | `false` | ✅ 既有列 | 许可类不可重试（与 `ROUTING_FAILED`/`IDENTITY_RESOLUTION_FAILED` 同档） |
| `session_id` | `NULL` | ✅ 既有（nullable） | `authorization` 不在 `SESSION_LINKED_STAGES` → **不会查 Session、不会建 Session** |
| `response_payload` | `NULL` | ✅ 既有语义 | 无响应事实，不伪造 |
| Evidence | `activities` 一条：`type=stage_failure`、`stage=authorization`、`status=failed`、`metadata={failure_code, retryable}` | ✅ 既有形态 | 与既有六段失败 Evidence **同形** |

### 5.3 「不能自行新增状态」的遵守说明

- **不新增状态列**（不新增 `status`，不合成双状态）；
- **不新增状态取值**：`processing_status` / `delivery_status` 的取值集合**完全不变**；
- **新增的只有 2 个「阶段/错误码取值」**，它们落在既有列与既有常量集合中，
  且属「事件分类词汇」而非「状态机状态」；
- **诚实性要求**：若不在 `STAGE_FAILURE_MAP` 注册新错误类型，失败会落成
  `failure_stage=NULL + failure_code=UNEXPECTED_FAILURE`——这会**丢失「这是授权拒绝」这一事实**。
  因此注册新错误类型是**必需**的，而不是可选的。

### 5.4 明确不做

```
❌ 不新增 interactions / activities 列
❌ 不新增 processing/delivery 状态取值
❌ 不新增「拒绝响应」投递（V0 不回消息给渠道；是否回「未授权」提示属产品决策，未裁决）
❌ 不把拒绝写入 Workspace / Solution / Agent 侧
```

---

## 6. Persistence — 0 tables / 0 columns / NO migration 是否仍成立

### 6.1 结论：**仍然成立**

| 需求 | 既有承载 | 结论 |
|---|---|---|
| 记录一次**拒绝** | `interactions`（行已存在）+ `activities`（append-only Evidence） | 0 新表 / 0 新列 |
| 表达「拒绝原因」 | `failure_stage` + `failure_code` + `retryable`（既有三元组） | 0 新列 |
| 表达「无 Session」 | `interactions.session_id` 可空（10.4.2 已确立） | 0 新列 |
| 表达「租户作用域」 | tenant-scoped UoW（既有） | 0 新列 |

### 6.2 该结论的**代价**（必须写进 Review 记录，不能隐藏）

1. **ALLOW 不被持久化**：判定结果不是实体，成功路径不产生任何「已授权」记录
   → 无法回答「某会话何时被允许、依据什么」。这是 V0 明确取舍（ADR-9：V0 不做复杂权限系统）。
2. **DENY 在 V0 不可达**（§3.2 D2）：因此「拒绝记录」在当前没有生产触发源，
   只有 Targeted Tests 可以构造。
3. 一旦需要 T1/T2/T3（Gate 1 §B：会话级 deny / 子集绑定 / 授权关系审计）中任一，
   **本结论立即失效**，必须升级为 `Persistence = YES`（新表 + migration），
   并重新走 Audit → Design → Review。

```
Persistence verdict:   0 new tables / 0 new columns / NO migration   ✅（在上述代价被接受的前提下）
Upgrade trigger:       T1 会话级 deny ｜ T2 N>1 连接子集绑定 ｜ T3 授权关系持久审计
```

---

## 7. Existing Behavior Regression — 逐项证明

### 第 1 类：行为**必须完全不变**（11 项）

| # | 行为 | 为何不受影响（机理） | 锁定测试（代表） |
|---|---|---|---|
| 1 | **WeCom single** | 入站映射（`single → from.userid`）在 Adapter；本设计**不碰 Adapter、不碰接纳路径** | `test_3b_3_3_wecom_bot_group_chat.py::test_a2_group_mapping_*`；`test_step9_8_3::test_e2e_*` |
| 2 | **WeCom group** | `chattype=group → chatid` 映射不变；群会话矩阵（复用/换群/换人）不变 | `test_3b_3_3::test_a2b_*` / `test_c1..c4_group_session_matrix_*` / `test_c4_group_end_to_end_reply_and_duplicate_dedup` |
| 3 | **Identity resolution** | **零改动**：仍 Fail Closed（这也正是选 R-A 的原因——策略变更会破坏 8.2/9.2 冻结与测试） | `test_step9_2::test_b_unknown_identity_fails_closed` / `test_b_failure_does_not_create_identity_or_user`；`test_step8_2::test_a_failure_code_frozen` |
| 4 | **Session reuse** | 授权位于其**之前**且 V0 恒 ALLOW → 三维隔离键、REUSE/CREATE_NEW 判定、`find_active` 全部不变 | `test_step9_3::test_a_existing_session_is_reused` / `test_g_find_active_key_is_three_dims`；`test_step10_6_2::test_a1_canonical_key_is_three_dimensions_no_agent_id` |
| 5 | **Tenant isolation** | 判定**不接触 tenant**；tenant 仍只经注入的 tenant-scoped UoW 生效；coordinator 仍不含 `tenant` 字面量（守卫保持） | `test_cloud_0_1_1_b_tenant_fanout::test_fanout_does_not_change_runtime_execution_logic`；`test_cloud_0_1_3_3::test_cross_tenant_execution_impossible_a_to_b` |
| 6 | **Channel authentication** | Adapter 验签 / 解密 / 归一化零改动（Gate 1 §D：Adapter change = NO） | `test_cloud_0_1_3_1::test_b1_bad_signature_rejected_no_row_no_trusted_scope` |
| 7 | **Connection resolution** | `find_active_by_channel` 与 `Connection.status` 语义零改动 | `test_step11_11_connection_management_api.py`；`test_step7_2/7_3` |
| 8 | **Routing** | **授权不消费 Connection 状态、不做唯一性裁决** → `0 条 / >1 条 → ROUTING_FAILED`、`agent_id` 派生自被选 Connection 全部不变（这是 D2/A 方案的关键收益） | `test_step9_4::test_a_zero_active_connections_fails` / `test_a_multiple_active_connections_fail` / `test_b_agent_id_derives_from_selected_connection`；`test_step10_3_1::test_a5_*` |
| 9 | **Delivery** | 拒绝不进入 Delivery；成功路径 Delivery 零改动；双状态语义不变 | `test_step9_6::test_d_delivery_failure_not_processing_failure_frozen`；`test_step8_6::test_f_two_independent_statuses` |
| 10 | **Duplicate callback** | 接纳路径（幂等回查 → ACK+STOP）零改动 | `test_step12_9_4_9::test_b_five_states_duplicate_returns_existing_and_stops`；`test_10_5_3::test_d1_succeeded_terminal_duplicate_is_side_effect_free_conflict` |
| 11 | **Worker retry / reclaim** | 拒绝 = **终态 `processing=failed`**（非 PROCESSING）→ 不进入 reclaim 目标集合；`retryable=false` → 不触发重试语义；claim / attempt fence 零改动 | `test_step12_9_4_12::test_c1_stale_processing_row_is_reclaimed_to_pending` / `test_d1_old_attempt_cannot_write_through_new_attempt`；`test_step12_9_4_17::test_3/test_5` |

### 第 2 类：**集合显式更新**（不是回归，是「有序变更」）

| 测试 | 需要更新什么 | 不得改变什么 |
|---|---|---|
| `test_step9_8_1_execution_ordering.py::test_order_is_canonical_six_stages`（及同文件 `test_routing_precedes_session_in_order` 等） | 阶段集合加入 `authorization` | 既有六段**相对顺序**不变 |
| `test_step10_1_4::test_create_new_path_executes_all_six_stages_in_order` | spy trace 期望序列加一项 | 每段**实参形状**不变 |
| `test_step9_8_2::test_call_order_create_new` / `test_call_order_reuse_active` | 调用序列加一项 | REUSE 仍跳过 `session_creation` |
| `test_step9_1::test_c_execution_ordering_locked`；`test_step9_A::test_order_is_single_source_and_canonical`；`test_cloud_0_1_1_a::test_runtime_execution_order_frozen` | 常量集合 | 「单一源」纪律 |
| `test_step10_4_2::test_a2_failure_map_covers_all_runtime_typed_errors` | 覆盖新 typed error | 既有映射条目不改 |
| `test_step10_1_4::test_six_failure_stages_map_to_distinct_typed_errors` / `test_each_stage_raises_only_its_own_error` | 纳入新阶段 | 「每阶段只抛自己的错」不变 |

### 第 3 类：Targeted Tests（后续阶段新增，本 Gate 不写）

DENY 路径（不建 Session / 不 Dispatch / 不 Delivery）、拒绝落库形状（五元组 + Evidence）、
判定不消费 Identity/User（可实现为「注入恒 ALLOW 的判定源」与「注入恒 DENY 的判定源」两组测试）、
群聊 `G1 → C1 = ALLOW`、Worker 拒绝后不 reclaim。

---

## 8. 设计图

### 8.1 判定位置（改前 / 改后）

```
改前                                   改后（新增唯一阶段）
InboundInteraction                     InboundInteraction
      ↓                                      ↓
Identity Resolution                    Identity Resolution      ← 零改动
      ↓                                      ↓
Routing                                ★ Authorization          ← 新增（P2）
      ↓                                      ↓  ALLOW            ↘ DENY
Session Resolution                     Routing                   （停止 + 记录）
      ↓                                      ↓
[Session Creation?]                    Session Resolution
      ↓                                      ↓
Agent Dispatch                         [Session Creation?]
      ↓                                      ↓
Delivery                               Agent Dispatch
                                             ↓
                                       Delivery
```

### 8.2 拒绝路径（复用既有失败通道，零新设施）

```
Authorization → DENY
      │  抛 typed error（不得被 coordinator catch —— 既有守卫禁止 ast.Try）
      ▼
Worker.process 的既有 except 分支
      │  record_failure(interaction_id, inbound, exc, expected_started_at)
      ▼
STAGE_FAILURE_MAP 命中 → (stage="authorization", code="UNAUTHORIZED", retryable=False)
      │
      ├─ interactions.record_outcome(processing=failed, delivery=pending,
      │                              failure_stage=authorization,
      │                              failure_code=UNAUTHORIZED,
      │                              retryable=false, session_id=NULL)
      └─ activities.append(type=stage_failure, stage=authorization, status=failed,
                           message=<reason>, metadata={failure_code, retryable})
      │
      ▼
驱动继续下一条（Driver 不解释异常语义 —— 既有纪律）
```

### 8.3 决策表（Review 需批准的 6 项）

| # | 决策 | 推荐 | 备选 | 影响面 |
|---|---|---|---|---|
| D1 | 未绑定参与者语义 | **R-A**：不进入执行（Identity Resolution 处 STOP），Authorization 不扩大可执行人群 | R-B / R-C（需 schema + 冻结契约变更） | §1 |
| D2 | DENY 可达性 | **接受结构性授权**：V0 许可集合=全量 → 恒 ALLOW；DENY 由测试构造 | 把「0 条 active Connection」改判为 DENY（**会改既有测试期望**，不推荐） | §3、§7 |
| D3 | 判定输入 | `(channel_id, external_conversation_id)`；禁止消费 Identity/User/content/Connection 状态 | — | §2 |
| D4 | 落点 | **P2 = coordinator 内、Identity 之后 / Routing 之前** | P1 / P3 | §4 |
| D5 | 拒绝记录 | 复用双状态 + 三元组 + `stage_failure` Evidence；新增 2 个取值，0 新列 | — | §5 |
| D6 | 持久化 | **0 表 / 0 列 / NO migration** | 升级触发：T1/T2/T3 | §6 |

---

## 9. 本次未做（只读声明）

```
❌ 未改生产代码        ❌ 未改测试            ❌ 未改冻结契约
❌ 未建 migration      ❌ 未新增类/模块/表/列  ❌ 未创建 AuthorizationService / Policy 类
❌ 未改 user_id        ❌ 未改 Runtime order（仅**设计**其集合更新清单）
❌ 未实现 P1/P2/P3 任一落点（仅推荐）
❌ 未裁决 D1/D2（仅给推荐与理由）
```

**本文件为设计产出，不构成实现批准。** 下一步：**Review**（批准 §8.3 六项决策）
→ 之后才进入 Implementation（并同步 Targeted Tests）。

---

## 附：与 Gate 1 审计结论的对应

| Gate 1 结论 | Gate 2 落地 |
|---|---|
| §C1 身份解析 Fail-Closed 与 conversation-scoped 授权的张力 | **D1/R-A**：Authorization 不以身份解析为输入；未绑定者仍在身份解析处 STOP（不改冻结契约） |
| §C3 `rejected/unauthorized` 的映射 | §5：`processing=failed + stage=authorization + code=UNAUTHORIZED + retryable=false` |
| §C4 落点 = Identity 与 Routing 之间 | §4.2 采纳 |
| §B 新表触发 T1/T2/T3 | §6 保留为升级触发条件 |
| §D Adapter 零变更 | §7 第 6 项 + §4.4 守卫 |
| §E 第 2 类「集合显式更新」 | §7 第 2 类逐条列出 |
| §十四 组合既有能力、不造第二套 | §3（不消费 tenant/channel/connection 既有开关的判定权）+ §5（复用既有记录设施） |
| §十五 可能只改策略 | **D1**：本 Gate 结论是 V0 **连策略都不改**（R-A），把策略问题留给未来 ADR |
