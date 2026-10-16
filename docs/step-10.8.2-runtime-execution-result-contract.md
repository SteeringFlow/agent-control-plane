# Step 10.8.2 — Runtime Execution Result Contract Design

> **契约定义步。** 生产代码改动 = 恰好一个新 domain 契约文件 +
> domain 导出面一行——**不接线、不实现**。
>
> 交付物：
> `app/domain/runtime_execution_result.py::RuntimeExecutionResult`（frozen 三字段）
> + 契约测试（`tests/test_step10_8_2_runtime_execution_result_contract.py`）+ 本文档。
>
> 只解决一个问题：把 10.8.1 已裁决的三个事实，正式定义成一个**最小、稳定**
> 的 Domain contract，把 contract **定死**。

---

## 1. 契约（冻结在 `app/domain/runtime_execution_result.py`）

```
RuntimeExecutionResult（frozen，恰好三个字段）
│
├── session_mode     REUSE_ACTIVE / CREATE_NEW   ← 复用 8.3 常量
│
├── session_id       int                          ← 本次执行实际使用的 CP Session
│
└── agent_response   AgentResponse（8.5 frozen）  ← 直接复用
                        ├── content
                        ├── external_session_id
                        └── metadata
```

命名刻意避开 9.1 冻结拒绝的 `RuntimeResult`（巨型 DTO 语境）——本契约是其
反面：10.8.1 A 裁决的**极薄 execution result**（10.8.1 §3「命名注记」兑现）。

---

## 2. 八项契约裁决（逐项回答审计问题）

| # | 审计问题 | 裁决 | 理由 |
| --- | --- | --- | --- |
| 1 | session_mode 用现有类型还是新建极薄 enum？ | **复用 8.3 既有常量 `REUSE_ACTIVE` / `CREATE_NEW`（str），不新建 Enum** | 单一事实源（identity 级复用）；项目状态语义全部是 str 常量（processing_status / delivery_status / SessionResolution.mode），Enum 会引入第二种表达风格与第二份常量源 |
| 2 | session_id 用 int 还是 domain identifier？ | **int** | 项目无 identifier wrapper 类型（identity_id / connection_id / control_plane_session_id 同为 int）；引入 wrapper = 无证据的风格漂移 |
| 3 | agent_response 是否直接复用现有 frozen AgentResponse？ | **直接复用**（注解即该类本身，不复制不包装不重定义） | 8.5 已冻结；10.7.2 裁决的 response_payload 快照形状就是它的字段面——第二个响应类型 = 重复表达 |
| 4 | 三字段是否全部只读 / frozen？ | **是**（`frozen=True`） | 契约事实不可变；与全部 8.x 值对象同纪律 |
| 5 | 成功路径下三字段的不变量？ | `session_id` 必填（运行时语义 > 0）；`session_mode` 恰好两值；`agent_response` **必非 None** | 本契约只表达「成功执行」——失败走 typed 异常通道（10.8.1 裁决 ④：异常通道本身就是执行事实通道） |
| 6 | 是否允许 agent_response is None？ | **不允许**：无默认值、必填 | 与 `SessionResolution.existing_session_id` 的「CREATE_NEW 时 None」不同——result 的 session 恒存在（本次执行必然实际使用了一个 CP Session）；None 会把「失败」偷运进成功契约 |
| 7 | session_id 与 session_mode 的组合约束？ | mode 只决定 session_id 的**解释**（REUSE = 复用既有 active session / CREATE = 本次新建），不产生额外组合状态；**不存在「CREATE_NEW 且 session_id 为空」的合法组合** | 那是不可能状态——决议失败走 `SESSION_RESOLUTION_FAILED` 异常，不产生结果对象 |
| 8 | 允许未来扩展字段，还是明确禁止未经审计扩展？ | **明确禁止**：字段面恰好三个，机器化锁定（字段面相等断言）；任何新增字段（逐段事实 / 时间戳 / outcome）必须重开本契约审计 | 9.1「不引入 RuntimeResult 巨型 DTO」在结果对象上的延续；字段面断言让「偷偷加字段」直接红测试 |

不变量的执行点：domain 值对象保持 8.x 纯净风格（plain frozen，不内嵌校验，
RoutingResult / SessionResolution / AgentResponse 同风格）；生产者（Coordinator）
在未来接线步 **Fail Closed 校验**（镜像 `DispatchExecutor._valid_response`
模式）。

---

## 3. 边界：本步刻意不做

- **不接线**：不改 `RuntimeExecutionCoordinator` Protocol（`execute() 仍 -> None`）、
  不改 RuntimeCoordinator / PersistingCoordinator / ChannelInboundService——
  接线 = Runtime Core 修改，须过 **10.1.4 §10 Freeze Rule 六问**（预备答案
  素材见 docs/step-10.8.1 §4）；
- **不携带 outcome**（10.8.1 裁决 ④）/ 逐段事实 / 时间戳；
- 失败路径无结果对象（异常传播时 result 不存在）——失败路径的 session 归属
  仍走既有弱键读路径（开放点记录在 10.8.1 §4）；
- 不实现任何读路径 / response_payload 写入（接线步一起裁决）。

---

## 4. 验收（8 条 → 测试 9 项）

| # | 条件 | 测试 |
| --- | --- | --- |
| 1 | 字段面恰好三字段、全部必填无默认、类型注解冻结 | A1 |
| 2 | 三字段 frozen 只读 | B1 |
| 3 | session_mode 复用 8.3 常量（identity 相等）、无 Enum 类 | C1 |
| 4 | agent_response 注解即 AgentResponse 本身（frozen） | D1 |
| 5 | 两种 mode 合法构造、只读语义成立 | E1 |
| 6 | agent_response 必填（缺参 TypeError）+ 「None 不允许」成文 | E2 |
| 7 | 零接线：coordinator / runtime / wrapper 不引用；execute() -> None；执行顺序不变 | F1 |
| 8 | domain 纯净 imports + 导出面 + 契约成文 | G1 / H1 |

全量回归：895 + 9 = **904 passed**。
