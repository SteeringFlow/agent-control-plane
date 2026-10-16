# Step 10.8.3 — RuntimeCoordinator Result Integration

> **接线步。** 只做一件事：`RuntimeCoordinator.execute()` 从 `-> None`
> 正式变成 `-> RuntimeExecutionResult`（10.8.2 薄结果契约）。
>
> 生产改动 = 恰好两个文件：`application/runtime.py`（Protocol 签名）+
> `application/coordinator.py`（构造并返回结果）。三个 Executor **零改动**
> （它们本来就返回所需 typed 值：`SessionResolution` / `Session` /
> `AgentResponse`——「必要的类型接线」恰好为零）。

---

## 1. 成功路径（最终形态）

```
execute(inbound)
      │
      ├── 成功
      │      ↓
      │ RuntimeExecutionResult
      │   ├── session_mode    = decision.mode        （REUSE_ACTIVE / CREATE_NEW）
      │   ├── session_id      = control_plane_session_id
      │   │       REUSE_ACTIVE → existing session.id
      │   │       CREATE_NEW   → newly created session.id
      │   │       （二者已统一为 control_plane_session_id，绝不 None——9.8.2 不变量）
      │   └── agent_response  = response             （Dispatch 产出，直接进入 Result）
      │
      └── 失败
             ↓
        原 typed exception（无结果对象）
```

---

## 2. 类型接线严格限幅

### ✅ 允许且已改（恰两文件）

| 文件 | 改动 |
| --- | --- |
| `application/runtime.py` | Protocol `execute` 签名 `-> RuntimeExecutionResult`；docstring 补成功/失败语义；「不引入 RuntimeResult 巨型 DTO」禁令原文保留（薄契约是 10.8.1 A 裁决的唯一例外） |
| `application/coordinator.py` | 末尾构造并 `return RuntimeExecutionResult(session_mode=decision.mode, session_id=control_plane_session_id, agent_response=response)`；docstring 同步；import 面仅 + `app.domain.runtime_execution_result` |

### ❌ 禁改清单（全部锁死，探针 C4 逐项验证）

```
❌ Persistence wrapper（interaction_recording.py —— 仍 -> None，结果被丢弃，10.8.4 接）
❌ InteractionRecord / ActivityRecord
❌ Repository contract / Adapter
❌ Delivery 语义（DeliveryExecutor / OutboundResponse 不动）
❌ RUNTIME_EXECUTION_ORDER（六段顺序原样）
❌ Failure exception model（STAGE_FAILURE_MAP 六段映射原样；失败仍 typed 异常）
❌ API response（channels.py 不认识 Result）
❌ Idempotency / Retry
❌ response_payload（仍零写入方）
❌ sessions.external_session_id（仍从不写）
```

**为什么 wrapper 本步不动**：把结果线程化进 PersistingCoordinator 必然牵出
「response_payload 写什么 / sessions.external_session_id 绑定」——那是 10.8.4
的裁决面。10.8.3 只让 Runtime 诚实返回事实；wrapper 丢弃结果的**过渡态**被
测试显式锁死（`wrapper.execute -> None` + 不引用 Result），防止半接线漂移。

---

## 3. 既有断言翻转（约定内，5 处）

| 测试 | 原断言 | 翻转后 |
| --- | --- | --- |
| `test_step9_1::test_b` | runtime.py roots ≤ {typing, app.domain, app.domain.inbound_interaction} | + `app.domain.runtime_execution_result` |
| `test_step10_3_1::test_d3` | coordinator import 精确白名单（5 项） | + `app.domain.runtime_execution_result`（6 项） |
| `test_step10_7_2::test_b3` | coordinator/runtime `execute() -> None` | coordinator/runtime `-> RuntimeExecutionResult`；wrapper 仍 `-> None`（10.8.4） |
| `test_step10_8_1::test_b1` | runtime.py `execute() -> None` | `-> RuntimeExecutionResult`（10.8.1 预备裁决兑现；巨型 DTO 禁令原文保留） |
| `test_step10_8_2::test_f1` | 零接线（三文件不引用） | coordinator/runtime 已接线；wrapper 仍未接线（10.8.4） |

9.A 守卫全部兼容（coordinator roots 仍以 `app.domain` 开头 / `_call_attrs`
仍只有 `execute` / 类面仍 `{RuntimeCoordinator}` / `control_plane_session_id
is None` 兜底保留）。

---

## 4. 验收（用户 9 条 + 禁改清单 → 测试 12 项）

| # | 验收标准 | 测试 |
| --- | --- | --- |
| 1 | REUSE_ACTIVE 返回正确 session_id（与首条完全相同） | A2 |
| 2 | CREATE_NEW 返回新 session_id（== DB 新建行） | A1 |
| 3 | 两种模式都返回同一个 frozen AgentResponse 对象类型 | A3 |
| 4 | external_session_id 只能通过 agent_response 获取（Result 三字段面） | A4 |
| 5 | 失败仍然走原异常通道（identity / dispatch / delivery 三类） | B1 |
| 6 | RUNTIME_EXECUTION_ORDER 完全不变 | C1 |
| 7 | Coordinator 不因此获得 Repository / ORM / Adapter 依赖 | C2 |
| 8 | Runtime Core 没有出现第二套 Result 类型（全树 *Result == 2） | C3 |
| 9 | 全量回归通过 | 全量 |
| 10 | 禁改清单（wrapper/Record/Repo/Adapter/API/失败模型/幂等） | C4 |
| 11 | Protocol conformance 保持（Coordinator + wrapper 仍符合形状） | C5 |
| 12 | 接线正式化（Protocol 注解 AST 校验）+ 契约成文 | C6 / D1 |

全量回归：904 + 12 = **916 passed**。
