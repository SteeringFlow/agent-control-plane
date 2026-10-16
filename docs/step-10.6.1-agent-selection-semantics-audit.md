# Step 10.6.1 — Agent Selection Semantics Audit

> **审计步，生产代码改动：0 行。**
> 交付物 = 本报告 + 审计快照测试
> （`tests/test_step10_6_1_agent_selection_semantics_audit.py`）。
>
> **先不实现多 Agent Routing。** 只回答：当一个 Channel 对应多个 Agent 时，
> Control Plane 凭什么选择 Agent？以及一组围绕 Agent Selection 的语义问题
> —— 每个答案都必须有真实代码 / 真实数据库行为作证。

---

## 0. 范围与不做

**做**：

- 审计 8 个候选选择输入（Channel / Identity / external_conversation_id /
  Session / User / Connection / explicit command / Agent metadata）；
- 回答 6 个语义问题（凭什么选 / Selection 归属 / Conversation 粘性 /
  Session 切换 / Connection 影响 / Connection 单 Agent 性）；
- 记录审计中暴露的新张力（全部只记录，不修）。

**不做（本步明确排除）**：

- 不实现任何 Agent Selection / 多 Agent Routing / 规则引擎；
- 不改 RoutingExecutor / Runtime 执行顺序 / Session 语义 / 任何 schema；
- 不引入 Conversation→Agent 映射表 / AgentSelector / RoutingRule /
  Agent Registry / Load Balancing / 命令语法（换 Agent 指令）；
- 不为「多 Agent 选择依据」拍板——那是产品语义，需要真实用例证据。

---

## 1. 前置架构事实（审计的坐标）

以下事实全部来自冻结契约，是回答语义问题的坐标系：

| # | 冻结事实 | 出处 |
| --- | --- | --- |
| F1 | **Connection = Channel ↔ Agent 的受控访问路径**；agent 只经 Connection 可达 | 7.1 / `domain/connection.py` |
| F2 | Connection 绑定不可变：`UNIQUE(channel_id, agent_id)`，`agent_id` 单一 FK | 7.1 / 4.2 |
| F3 | Routing 的唯一职责 = 选 Connection；`agent_id` 是所选 Connection 的**派生**，不是独立选择 | 8.4 / 9.4 |
| F4 | MVP Routing 唯一选择输入 = `channel_id`；identity / conversation / content 不参与选择 | 8.4 / 9.4 |
| F5 | Fail Closed：Channel 的 active Connection 数 != 1 → `ROUTING_FAILED` | 8.4 / 10.3.1 |
| F6 | Session 隔离键 = `(Identity.id × Connection.id × external_conversation_id)`；actor 是 identity 不是 user | 8.3 / 9.3 |
| F7 | 8.3 会话粘性冻结：既有 conversation 必须沿用其 active session 已绑定的 Connection，不得每消息重选 | 8.3 §4 |
| F8 | 执行顺序 = identity → routing → session_resolution → …：**routing 先于 session resolution**（routing 只需 channel_id） | 9.1 / 9.8.1 |
| F9 | 单 Channel 多 ACTIVE Connection → RoutingFailedError（**Routing Capability Gap**，诚实记录不修） | 10.3.1 §3 |
| F10 | `external_session_id`（Agent 侧会话引用）**不在 Runtime 主链路回流** | 10.3.1 §4 |
| F11 | 10.4.2 Interaction Wrapper 用**弱键** `(identity, conversation)` 查 session 回填 Interaction（省 connection 一维，「有记录的近似」） | `repositories/session.py` / 10.4.2 |
| F12 | Inbound 契约只表达「外部世界说了什么」：无 user/identity/session/agent 归属字段 | 8.1 |
| F13 | `interactions` / `connections` / `identities` 全表冻结；8 表无 conversation→agent 绑定表 | 5.3 |

---

## 2. 候选输入审计

| 候选输入 | 当前是否参与选择 | 当前存在形态 | 审计结论 |
| --- | --- | --- | --- |
| **Channel** | ✅ 唯一选择输入 | `InboundInteraction.channel_id` → `find_active_by_channel` | 已用；结构性事实（消息来自哪个受控渠道） |
| **Identity** | ❌ | 8.2 解析产物 `Identity.id`（channel-scoped）；Routing 不消费 | 身份维度候选。天然 **Channel 作用域内**的第二选择信号（同一 User 在另一 Channel 的 Identity 与此无关） |
| **external_conversation_id** | ❌（不参与 Routing） | Inbound 字段；参与 Session 隔离键 | 会话维度候选。**粘性锚点**：conversation 无法脱离 channel 解释，也无法脱离 identity（8.3 双边界） |
| **Session** | ❌（Routing 先于 Session） | `sessions` 行携带 `connection_id + agent_id` | 不是选择输入，但 active session 是「这条 conversation 已绑定哪条 Connection」的**唯一现存事实**（F7 粘性的载体） |
| **User** | ❌ | Runtime 只有 Identity（actor=identity_id，契约无 user_id 字段，8.3） | 不可作为 Runtime 选择信号。按 User 选 Agent = 经 Identity 间接表达；User 级跨渠道选择与 8.3 Channel 边界冲突，禁止隐含跨越 |
| **Connection** | ❌（它是被选对象） | Routing 的输出 / Session 键的一维 | **不是选择输入**。唯一例外：既有 active session 的 connection 作为粘性**约束**（F7）——那是「已选事实约束再选」，不是候选输入 |
| **explicit command** | ❌ 不存在 | 全树无命令语法；content 从 Routing 到 Dispatch 原样透传，无人解析为控制信号 | 用户显式换 Agent 的指令若真实需要，是**全新信号**（需要在 Inbound/契约层定义），当前没有任何承载 |
| **Agent metadata** | ❌ 无选择语义 | Agent 有 name/type/endpoint/status/config/credential_ref；Connection.config 只允许连接配置（7.1） | 静态元数据目前**不是**任何选择维度的输入；要用它选 Agent 必须先冻结新的选择维度（语义未定义） |

**第一层结论（Q1 的地基）**：

> 今天 CP「选 Agent」的唯一凭据是 `channel_id`——而且不直接选 Agent，是选
> Connection，Agent 是派生（F3/F4）。8 个候选中只有 Channel 在用；其余全部
> 要么无选择语义、要么被纪律排除、要么身份被 channel 作用域限制。

---

## 3. Q1：当 Channel 对应多个 Agent 时，凭什么选择？

**当前答案：没有答案——这是 10.3.1 记录的 Capability Gap（F9）**：Channel 有
2 条 active Connection 时 Routing 直接 `ROUTING_FAILED`（Fail Closed，不猜）。
多 Agent 选择依据目前不存在，也不应为了存在而存在。

**候选信号如何分层**（若真实用例需要，按外部世界事实的层次选，不猜）：

```
外部世界事实（全部必须在 Channel scope 内解释 —— 10.5.x 身份纪律）
  ├── 结构事实：Channel                     ← 已用（选 Channel 的 Access Path）
  ├── 身份事实：Identity（channel-scoped）   ← 第二候选（按人分流）
  ├── 会话事实：conversation / active session ← 粘性候选（续聊绑定原 Agent）
  ├── 用户意图：explicit command            ← 尚无任何承载（未来产品信号）
  └── 静态元数据：Agent metadata            ← 无冻结语义（未来若用须先定义）
```

注意：这里每一项都不是「选 Agent」本身——它们的语义结果都必须是**收窄到一条
Connection**（因为 Agent 只经 Connection 可达，F1/F2）。「选择 Agent」在 CP
里是一个被禁止的悬空概念：**没有 Connection 就没有 Agent 可达路径**。

---

## 4. Q2：Selection 属于 Routing，还是 Selection → Routing 的输入？

### 4.1 语义上的裁决

> **不存在独立于 Routing 的「Agent Selection」。**
> 「选 Agent」在当前模型中 ≡「选 Connection」：Agent 是 Connection 的派生
> （F3），8.4 冻结 `RoutingResult{connection_id, agent_id}` 且禁止
> 「选了 C 却派给别的 Agent」。因此 **Agent Selection 属于 Routing**——
> 具体地说，Routing 选择了 Agent Access Path，agent 只是该选择的投影。

### 4.2 那「Selection → Routing 的输入」在什么意义上成立？

如果未来 Routing 需要按 conversation / identity 选，那正确形态是：**这些事实
成为 Routing 的输入**，而不是在 Routing 之前另设一个「Selection」组件去预选
Connection。理由：

- 若 Routing 前有一个独立 Selection stage，会出现**两个选择器**（Selection 选
  Connection + Routing 再选 Connection）——违反 8.4「Connection 在 Routing 中
  被选择」的单一裁决点，也让 Fail Closed 数量判定分裂；
- 任何「预选结果」本质就是 Routing 输出的一半（connection_id），前置组件会
  重复 RoutingResult 的形状。

**所以（语义层裁决）**：

```
Selection 属于 Routing：选择逻辑 + 选择输入都长在 Routing 上；
「Selection → Routing 的输入」若成立，指的是 会话/身份事实 成为 Routing 的
输入（扩展它的选择键），而不是新增一个前置选择 stage。
```

### 4.3 但有一个真实的执行顺序张力（记录，不修）

8.3 会话粘性（F7）说：既有 conversation 必须沿用 active session 绑定的
Connection。而执行顺序（F8）是 routing **先于** session resolution，Routing
只看 channel_id——**Routing 在决策时并不知道这条 conversation 之前绑定在哪**。

- 单 Channel 单 Connection 下无矛盾（选择唯一）；
- 单 Channel 多 Connection 下，粘性要求「选之前知道会话事实」，与当前顺序
  冲突。未来若落地，需要：选择判定前的一次只读会话事实查询（如 F11 的弱键
  读路径的收紧版），或 conversation 绑定的一等化。**这是 10.6.x 设计裁决的
  输入，本步不选边。**

---

## 5. Q3：同一 Conversation 是否必须始终绑定同一个 Agent？

### 现状事实

- 没有 conversation→agent 的一等绑定（无表、无字段，F13）；
- Session 键含 Connection（F6），会话粘性要求沿原 Connection（F7）。

### 审计结论

> **「同一 conversation 绑定同一 Agent」不是一条被显式冻结的规则，而是两条
> 既有纪律共同产生的效果**：会话粘性（续聊沿原 Connection）× Session 键含
> Connection（换 Connection = 新会话空间）。效果是：**只要该 conversation 的
> active session 存在，继续对话就只能走原 Connection（= 原 Agent）；要换
> Agent 必然新开 Session，旧上下文断开。**

因此：

- 「必须始终绑定」语义上不成立为硬约束——**切换是被允许的，但代价是新会话**
  （上下文重置），而不是在同一会话内换 Agent；
- conversation→agent 的粘性目前由 **session→connection 绑定**间接表达，没有
  独立的 conversation 归属实体。

### 连带发现的潜在陷阱（记录）

10.4.2 的 Interaction Wrapper 用弱键 `(identity, conversation)` 查 active
session 回填 Interaction（F11，省 connection 一维）。若同一 (identity,
conversation) 因换 Agent 产生了**两条 active session**（旧 Connection + 新
Connection），该弱键查询会 `MultipleResultsFound`（实现是 `scalar_one_or_none`）。
今天不可达（MVP 单 active Connection/Channel 不会造出两条），多 Agent 落地时
是**必须先收紧的歧义点**——Port docstring 已预告「未来需随真实语义收紧」。

---

## 6. Q4：用户切换 Agent → Session 是否切换？

**结论：是，必然切换（新 Session），且 Agent 侧上下文也断。**

推理链（全部来自冻结事实）：

1. Agent 只经 Connection 可达（F1/F2）→ 切换 Agent 的唯一途径 = 换 Connection；
2. Connection 是 Session 隔离键的一维（F6）→ 换了 Connection，
   `find_active(identity, new_connection, conversation)` 必 miss → **CREATE_NEW**；
3. 旧 Session 仍是 active（sessions 无切换/关闭协议、无状态翻转机制）——
   产生一条**平行的旧会话**，仅靠隔离键永远查不到（因为要带新 connection_id）；
4. `external_session_id` 不在 Runtime 主链路回流（F10）→ Agent 侧的会话引用
   不随 CP 会话迁移 → **Agent 侧上下文同样无法续接**，切换 = 两端都重置。

所以「用户切换 Agent」在语义上是**整段对话在新 Access Path 上重新开始**，
不是「同一会话换个 Agent 服务」。

---

## 7. Q5：Agent Selection 是否影响 Connection？

**结论：因果方向反了——不是选 Agent 影响 Connection，而是选 Connection
决定 Agent。**

- Connection 是 Agent 的载体与唯一入口（F1）；`agent_id` 是 Routing 输出对
  Connection 的投影（F3）；
- 因此「选择 Agent」没有独立于 Connection 的影响力；任何未来的多 Agent 选择
  机制（按身份 / conversation / 命令），其**语义输出必须是 Connection**，否则
  Dispatch 无路可达（9.5：Dispatch 只消费 RoutingResult 的 connection_id）。

**正确的表述**：

> Agent Selection 是 Connection Selection 的别名；影响方向是
> 「Connection（被选）→ 决定 Agent（派生）」，反过来不成立。

---

## 8. Q6：一个 Connection 是否仍只能代表一个 Agent？

**结论：是，继续冻结。**

- `UNIQUE(channel_id, agent_id)` + `agent_id` 非空 FK + 绑定不可变（F2 / 7.1）；
- Channel A → Agent X / Agent Y 是两条 Connection；Channel A → Agent X 只能
  有一条（4.2）；
- 多 Agent 于一个 Channel = **多条 Connection**，不可能是「一条 Connection
  代表多个 Agent」。

若未来真实产品出现「一个 Channel 连一个 Agent 集群 / 负载均衡端点」——那将
是 **Connection 语义本身的升级**（如 Connection→endpoint 集合），需要专门
裁决，本步不改变现有冻结。

---

## 9. 审计发现汇总（新张力，全部记录不修）

| # | 发现 | 证据 | 影响 |
| --- | --- | --- | --- |
| N1 | 会话粘性（F7）与执行顺序（F8）的张力：Routing 决策时看不到会话事实 | `RUNTIME_EXECUTION_ORDER`：routing 在 session_resolution 前 | 单 Channel 多 Connection 落地时必须先裁决「粘性事实从哪来」 |
| N2 | Interaction Wrapper 弱键歧义：同 (identity, conversation) 两条 active session 时 `MultipleResultsFound` | `find_active_by_identity_and_conversation` 用 `scalar_one_or_none` | 多 Agent 下换 Agent 会制造两条 active session → 必须先收紧弱键 |
| N3 | 换 Agent = CP 会话 + Agent 会话双重置（external_session_id 不回流） | 10.3.1 §4 / F10 | 「无缝换 Agent」若成为产品诉求，需要 external_session_id 主链路回流 + 会话迁移语义，均为未来裁决 |
| N4 | Capability Gap 依旧：双 ACTIVE → `ROUTING_FAILED`；选择依据完全未定义 | 10.3.1 / 本步 §3 | 这是 10.6.x 的核心产品裁决点，不是技术缺口 |

---

## 10. 验收（快照测试钉住的事实）

| # | 条件 | 测试 |
| --- | --- | --- |
| 1 | Routing 只按 channel_id 选 Connection，不消费 identity/session/conversation/content | A1 |
| 2 | 无 AgentSelector / Selection / RoutingRule / 命令语法等选择组件偷渡 | A2 |
| 3 | 一个 Connection = 一个 Agent（UNIQUE + FK + 绑定不可变 + 标量 agent_id） | A3/A4 |
| 4 | Session 键含 connection_id（换 Connection = 新会话空间） | A4 |
| 5 | 无 conversation→agent 一等绑定表（8 表冻结不变） | A5 |
| 6 | Capability Gap 快照：单 Channel 双 ACTIVE → RoutingFailedError | B1 |
| 7 | 同 conversation + 同 Connection → REUSE_ACTIVE（粘性的事实载体） | B2 |
| 8 | 切换 Connection（Agent）→ CREATE_NEW（旧会话保持 active） | B3 |
| 9 | Agent 只能经 Connection 派生（单一 active 时 result.agent_id = connection.agent_id） | B4 |
| 10 | Wrapper 弱键歧义是真实潜在陷阱（两条 active session → MultipleResultsFound） | B5 |
| 11 | 执行顺序路由先于会话（N1 张力的代码坐标） | B6 |
| 12 | 契约成文 | C1 |

---

## 11. 语义答案一句话版

| 问题 | 答案 |
| --- | --- |
| 凭什么选择 Agent？ | 今天只凭 channel_id，且选的是 Connection（Agent 是派生）；多 Agent 选择依据**尚不存在**（Capability Gap），候选信号须在 Channel scope 内解释 |
| Selection 属于 Routing 还是其输入？ | **属于 Routing**——「选 Agent」≡「选 Connection」；会话/身份事实将来可作为 Routing 的输入扩展，而非前置独立 Selection stage |
| 同一 Conversation 必须始终同一 Agent？ | 不是显式硬规则；由会话粘性 × Session 键含 Connection **共同产生效果**——续聊沿原 Agent，切换 = 新会话（上下文重置） |
| 用户切换 Agent → Session 切换？ | **是，必然新 Session**（Connection 是隔离键一维）；旧 Session 仍 active；Agent 侧上下文也断（external_session_id 不回流） |
| Agent Selection 影响 Connection？ | 方向反了：选 Connection 决定 Agent；Agent Selection 是 Connection Selection 的别名 |
| 一个 Connection 仍只能一个 Agent？ | 是（UNIQUE + 绑定不可变）；多 Agent = 多 Connection；「Connection 代表 Agent 集群」是未来不同的语义 |
