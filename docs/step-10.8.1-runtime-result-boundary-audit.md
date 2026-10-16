# Step 10.8.1 — Runtime Result Boundary Audit

> **审计步，生产代码改动：0 行。**
> 交付物 = 本报告 + 审计快照测试
> （`tests/test_step10_8_1_runtime_result_boundary_audit.py`）。
>
> 只回答一个问题：**如果 Runtime 不再只返回 `None`，它最小需要暴露哪些
> 「执行事实」，才能同时解决 10.4~10.7 已多次证明的观察缺口？**
>
> 并裁决：RuntimeResult 应该是 **A. 极薄 execution result**，
> 还是 **B. 包含整个 execution trace 的对象**。

---

## 0. 范围与不做

**做**：盘点 10.4~10.7 被探针证明的观察缺口 → 映射到四个候选事实
（① AgentResponse / ② external_session_id / ③ Session created/reused /
④ Execution & delivery outcome）→ 逐个裁决是否暴露 → 裁决 A/B。

**不做（本步明确排除）**：

- 不改 `RuntimeExecutionCoordinator` Protocol（`execute() -> None` 不动）；
- 不引入任何 RuntimeResult 类型（连薄版也不引入——那是接线步的事）；
- 不写 response_payload、不改 PersistingCoordinator；
- 不重开 Runtime Baseline（本步只是为 10.1.4 §10 六问准备答案素材）。

---

## 1. 被证明的观察缺口 → 四个事实的映射

10.4~10.7 用真实探针证明过的观察缺口，全部收敛到四个候选事实：

| 缺口 | 出处 | 需要的执行事实 |
| --- | --- | --- |
| G-A：`response_payload` 零写入方——Agent 回了什么不可见；Case B 的 Delivery Retry 无承载物 | 10.5.3 / 10.7.1 §1.3 / 10.7.2 B1 | **① AgentResponse** |
| G-B：`external_session_id` 不回流——Agent 侧会话引用无处落地，CP↔Agent 无法跨系统串联 | 10.3.1 §4 / 10.7.2 B4 | **② external_session_id**（搭 ① 便车） |
| G-C：Persistence Observation 靠**弱键**重推导 session 归属（多 Connection 下歧义）；CREATE_NEW Evidence 靠「执行前后 active session 由无到有」**推导**而非直接观察 | 10.6.1 N2 / 10.6.2 B5 / 10.4.2 | **③ Session created/reused（mode + session_id）** |
| G-D：成功路径中间段 Evidence 不可见 | 10.4.2 已知边界 / 10.7.1 Q2 | 部分由 ①（dispatch 成功锚点）+ ③（session 直证）解决；identity/routing 段由 `failure_stage` 反推即可（§2④） |
| G-E：execution outcome / delivery outcome | 10.4.2 已验证 | **已由异常通道承载，无需暴露**（§2④） |

**探针实证的「事实丢失点」**：AgentResponse 的 content 在边界内真实存在并
流到了 Delivery（Test Channel Adapter 能收到 `echo:hello`），但持久化观察层
拿不到它——`response_payload` NULL、`sessions.external_session_id` NULL。
事实不是不存在，是**在 `execute() -> None` 边界上被丢弃**。

---

## 2. 四个事实逐个裁决

### ① AgentResponse —— 必须暴露

- 它是 `response_payload` 的唯一写入源（10.7.2 B1 已裁决快照形状 =
  `{content, external_session_id, metadata}` 三字段原样）；
- 它同时是 **dispatch 段成功的锚点**：持有 AgentResponse = dispatch 必然成功
  ——G-D 中 dispatch 段的 Evidence 由此直接化；
- 形状：**整个 frozen 对象**（三字段），不拆散传递。

### ② external_session_id —— 不单独暴露

- 它是 **AgentResponse 的字段**（8.5 冻结字段面的一员）；
- 它的两个消费用途——响应级快照（写进 response_payload）与未来的
  `sessions.external_session_id` 会话级绑定——都从 ① 取即可；
- 单独再暴露一份 = 对同一事实的第二次表达（与 10.6.2「agent_id 不是
  Session 的第二个选择键」同一纪律：**不重复表达**）。

### ③ Session created/reused —— 必须暴露（mode + session_id 二元组）

- `session_id`：本次执行实际使用的 CP Session（REUSE 或 CREATE 的那条）——
  Persistence Observation 从此**不需要弱键重推导**，10.6.2 B5 证明的
  `MultipleResultsFound` 歧义源被连根消除；
- `session_mode`（REUSE_ACTIVE / CREATE_NEW）：使 CREATE_NEW Evidence 从
  「执行前后快照推导」变为**直接观察**（Evidence = 事实，不推导）；
- 这是四个事实里唯一**今天完全靠推导/近似**获得的——价值最高。

### ④ Execution outcome / Delivery outcome —— 不暴露

- wrapper 今天已经从「**异常 vs 无异常**」完整推导 outcome：无异常 →
  `succeeded × delivered`；`ChannelDeliveryFailedError` → `succeeded × failed`；
  其它 typed error → `failed × pending` + failure 三元组（10.4.2 验证，
  探针复现）；
- 返回值再携带 outcome = 与异常通道**重复表达**，且制造「result 说成功但
  异常也抛了」的不一致面；
- 结论：**异常通道本身就是执行事实通道**，④ 留在异常里。

---

## 3. A vs B 裁决：极薄 execution result

### 裁决：A —— 一个极薄的 execution result

```
RuntimeExecutionResult（frozen，恰好三个字段）
    session_mode:   str               # REUSE_ACTIVE / CREATE_NEW
    session_id:     int               # 本次执行实际使用的 CP Session
    agent_response: AgentResponse     # 成功返回时必非 None（Dispatch 成功才可能返回）
```

没有别的字段。**被证明的缺口只需要两个新事实（①③），② 搭车，④ 不需要。**

### 为什么不是 B（整个 execution trace）

1. **双份事实源**：IdentityResolution / RoutingResult / SessionResolution
   已经是各 Executor 的内部 typed 契约；把它们整体装进 result 外泄，同一
   事实在两处存放，迟早漂移；
2. **观察层耦合全部内部**：PersistingCoordinator 是唯一消费者，却拿到全部
   内部细节——此后任何 Executor 契约演进都波及观察层；
3. **诱导制造 Evidence**：全 trace 会诱惑 wrapper 逐段写成功 Evidence——
   违背 10.4.2 冻结的「Evidence = 事实，不凭空制造」；被证明的缺口并不需要
   identity/routing 段的成功 Evidence（`failure_stage` 反推足够）；
4. **9.1 冻结反对的正是巨型 DTO**（原文「不引入 RuntimeResult 巨型 DTO」）；
   B 是它的直译，A（三字段）与该冻结的精神一致。

### 命名注记

9.1 拒绝的名字是 `RuntimeResult`（巨型 DTO 语境）。未来若引入薄 result，
命名应避开该裁决的混淆面（如 `RuntimeExecutionResult`）——记录在案。

---

## 4. 治理门槛与接线点

- **改 `RuntimeExecutionCoordinator` Protocol（`-> None` → `-> result`）=
  Runtime Core 修改** → 必须先过 **10.1.4 §10 Freeze Rule 六问**。本审计
  给出六问的预备答案素材：
  1. 现有契约为何无法满足？——`execute() -> None` 使观察层拿不到任何执行
     事实（探针实证：事实在边界上被丢弃）；
  2. 真实测试暴露还是新需求？——真实测试暴露（response_payload NULL /
     弱键歧义 / session 行 external_session_id NULL 均为探针实证）；
  3. 能否在 Application/Adapter 层解决？——不能（wrapper 拿不到
     AgentResponse，10.7.2 B3 实证）；
  4. 是否破坏 Failure Boundary？——不（失败仍走 typed 异常，④ 不动）；
  5. 是否破坏 Session Boundary？——不（session 事实只是被观察，归属决策
     仍在 Routing/Resolution）；
  6. 是否重开 Runtime Baseline？——接线步裁决（本步不拍板）。
- **统一接线点**：10.7.1 Q2（中间段 Evidence）+ 10.7.2 B3（谁来写
  response_payload）+ 本步 = 未来**同一次**边界修订；
- **开放点（记录）**：RuntimeResult 只覆盖成功路径（失败走异常、无返回值）
  ——失败路径的 session 归属今天仍走弱键（MVP 单 Connection 下无歧义）；
  彻底消除需 typed error 携带上下文，等真实证据触发。

---

## 验收（探针钉住的事实）

| # | 条件 | 测试 |
| --- | --- | --- |
| 1 | ① wrapper 观察不到 AgentResponse（边界丢弃实证的前半） | A1 |
| 2 | ② external_session_id 是 AgentResponse 字段；会话级绑定主链路从不写 | A2 |
| 3 | ③ session 事实今天靠弱键重推导 + 前后快照推导 | A3 |
| 4 | ④ 异常通道已完整承载 outcome（三态探针）→ 无需暴露 | A4 |
| 5 | 事实丢失点实证：content 流到了 Delivery、却没到观察层 | A5 |
| 6 | 9.1 冻结拒绝的是巨型 DTO；修改 Protocol 受 10.1.4 Freeze Rule 约束 | B1 |
| 7 | A 形状（三字段）与 B 方案的裁决已成文 | B2 |
| 8 | 内部 typed 契约存在但不外泄（B 方案 = 双份事实源的实证） | B3 |
| 9 | 契约成文 | C1 |

---

## 一句话版

| 问题 | 答案 |
| --- | --- |
| 最小需暴露哪些执行事实？ | 恰好两个新事实：**① AgentResponse**（含 ② external_session_id，不单独暴露）+ **③ Session created/reused（mode + session_id）**；**④ outcome 不暴露**（异常通道已承载） |
| 为什么是这三个字段？ | response_payload 的写入源（①）+ 消除弱键歧义/CREATE_NEW Evidence 直证（③）——10.4~10.7 被证明的缺口恰好被它们覆盖，多一个都是重复表达 |
| A 还是 B？ | **A 极薄 execution result**（session_mode + session_id + agent_response）；B = 双份事实源 + 观察层全面耦合 + 诱导制造 Evidence + 9.1 冻结明确反对 |
| 现在就引入吗？ | 不——本步只审计；引入 = Runtime Core 修改，须过 10.1.4 §10 六问（预备答案素材已备） |
