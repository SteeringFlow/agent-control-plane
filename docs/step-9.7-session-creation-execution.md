# Step 9.7 — Session Creation Execution & Runtime State Materialization

> 把 9.3 的 `CREATE_NEW` 从**决策**变成真正的 Session Persistence。
> **不是重新设计 Session**——只补最小写路径，让 9.3 的决策能物化为真实 CP Session，
> 并把它的 id 交给后续 Dispatch（→ `AgentRequest.control_plane_session_id`）。

## 为什么现在才做（Evidence-first）

完整链条真正跑起来后，若 `CREATE_NEW` 却拿不到 `control_plane_session_id`，
9.5 的 `AgentRequest.control_plane_session_id` 就无法产生。这不是「未来可能需
要」，而是 First E2E 的**实际前置条件**——因此现在补 Session 写路径。

## 交付物（最小写路径）

| 文件 | 新增 |
| --- | --- |
| `app/repositories/session.py`（Port） | `create(session) -> Session`（读路径 find_active 保留） |
| `app/persistence/repositories/session.py`（Adapter） | `create`（flush，不 commit） |
| `app/persistence/mappers/session.py` | `to_orm`（读路径 to_domain 保留；不做 update） |
| `app/application/session_creation.py` | `SessionCreationExecutor` |

不创建 SessionManager / SessionLifecycleService / SessionService / SessionFactory /
ConversationManager。

## 字段来源（避免自己 Routing）

```
identity_id / external_conversation_id    ← 9.3 Session Resolution
connection_id                             ← 9.4 RoutingResult（已选定 Connection）
channel_id                                ← 已确定对象（Identity/Connection），调用方传入
agent_id                                  ← 必须来自 RoutingResult.agent_id（已选定
                                            Connection）——绝不自己找 Agent
external_session_id / metadata            ← 可选
```

## 幂等性（evidence-first）

检查结果：`sessions` 表当前**没有**覆盖隔离键
`(identity_id, connection_id, external_conversation_id)` 的唯一约束（5.3 冻结）。
第一版让数据库约束与事务承担 MVP 能承担的职责（`find_active → create` 的事务顺序）；
**不引入**分布式锁 / 事务框架 / Event Sourcing / Saga / Redis。真实并发重复若出现，
由真实测试暴露后再决定是否加约束——测试里保留「表无唯一约束」的观测断言。

## 链条闭环（9.7 的核心证明）

```
CREATE_NEW（9.3 决策）
    ↓ SessionCreationExecutor（create）
    ↓ 真实 CP Session（id）
    ↓ Routing（9.4）
    ↓ Dispatch（9.5）AgentRequest.control_plane_session_id = created.id
```

`tests/test_step9_7_session_creation_execution.py::test_c_create_new_materializes_id_for_dispatch`
在单测里串起 9.3 → 9.7 → 9.5，证明物化后的 id 确实成为 Dispatch 的 CP Session id。

## Fail Closed

输入不合法或持久化失败 → 复用 Session Resolution 阶段失败语义
`SessionResolutionFailedError`（拿不到 CP Session = 决议无法落地 → STOP）。

## 验收

`tests/test_step9_7_session_creation_execution.py`（20 项）：create 返回带 id 的真实
Session（落库）/ 物化后 find_active 命中 → REUSE_ACTIVE / agent_id 与
RoutingResult.agent_id 一致 / Creation 不 import Connection·Agent 仓储、不再选路 /
CREATE_NEW → CP Session id → Dispatch（链条闭环）/ Repository 只有 find_active+create /
Mapper 只有 to_domain+to_orm（无 apply_to_orm）/ 6 类非法输入 Fail Closed /
表无唯一约束（观测断言）/ 无 Manager·Service·Factory / mutation（越层 import、
选路调用）。

全仓 `pytest -q` = 528 passed，lint 0。
