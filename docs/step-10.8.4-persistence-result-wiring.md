# Step 10.8.4 — Persistence Result Wiring

> **接线收口步。** 解决从 10.4.1 遗留至今的两个实际缺口：
> **response_payload 零写入方** 与 **sessions.external_session_id 从不写**。
>
> 生产改动（最小面）：
> - `application/interaction_recording.py`（PersistingCoordinator：接住 Result、
>   写 response_payload、用 Result.session_id、绑定会话映射、删除前后快照推导）；
> - `repositories/session.py` + `persistence/repositories/session.py`
>   （Session Port/Adapter + `bind_external_session`——唯一写入点，非通用 update）；
> - `repositories/interaction_record.py` + `persistence/repositories/interaction_record.py`
>   （`record_outcome` + `response_payload` 参数——终态一次性落定的自然扩展）。

---

## 1. 数据流（本步之后）

```
RuntimeExecutionResult
        │
   ┌────┼────────────────┐
   ▼            ▼         ▼
agent_response  session_id  session_mode
   │              │            │
   ▼              ▼            ▼
response_payload  interaction.session_id   session_created Evidence
   │              │            （直接观察，非推导）
   │              ▼
   │         Session（不产生新的：REUSE 复用同一条）
   ▼
sessions.external_session_id（会话映射绑定）
```

---

## 2. 四件事（用户指定，全部落地）

| # | 事项 | 落地 |
| --- | --- | --- |
| 1 | PersistingCoordinator 接住 RuntimeExecutionResult | `execute()` 捕获 inner 返回值，**原样返回**（观察层不改执行） |
| 2 | agent_response 原样映射到 interaction.response_payload | `record_outcome(response_payload={"content", "external_session_id", "metadata"})`——10.7.2 B 裁决的三字段原样快照 |
| 3 | 使用 Result 的 session_id，**不再由 wrapper** 自己重新推导 | 成功路径删除「执行前后快照推导」（`session_existed_before` 整体移除）；session_created Evidence 由 `result.session_mode` **直接观察** |
| 4 | agent_response.external_session_id 更新对应 Session 的会话映射 | 新增 `SessionRepository.bind_external_session(session_id, external_session_id)`（唯一写入点）；None 不写入、不清空既有映射 |

## 3. 重新验证（用户两组路径）

| 路径 | 验证 |
| --- | --- |
| **CREATE_NEW** | session_id 直接来自 Result（== DB 新建行）→ external_session_id 落 Session → response_payload 落 Interaction → session_created Evidence 直证（B1） |
| **REUSE_ACTIVE** | session_id 直接来自 Result（== 首条，复用）→ 不产生新 Session（DB 仍 1 条）→ response_payload 落 Interaction → 零虚假 session_created Evidence（B2） |
| 映射保护 | Agent 后续不返回 external_session_id（None）→ 不清空既有映射；response_payload 照常落地（B3） |

---

## 4. 诚实修订：Case B 的 response_payload 仍为 NULL

失败路径**没有** RuntimeExecutionResult（异常传播，10.8.3 冻结的失败模型），
因此：

- dispatch 前失败（failed × pending）→ response_payload = NULL（响应从未诞生）；
- **Case B（succeeded × failed）→ response_payload = NULL**
  ——这是对 **10.7.2 B2 状态矩阵**的诚实修订（原「Case B 有值」一行作废）：
  完整成功才落响应快照。

由此，**10.5.3「Delivery Retry 无承载物」的修订版结论**：

> response_payload 承载物已对**完整成功**的交互成立；但对 **Case B**
> （恰恰是需要重投的那一类）**尚未成立**——失败路径要拿到响应事实，需要
> typed error 携带上下文或其它未来接线裁决（开放点，与 10.8.1 §4 同源）。
> 「具备 retry 所需数据」≠「现在实现 retry」。

## 5. 边界（用户明确守住，本步不做）

❌ RetryService ❌ Replay API ❌ Processing lease ❌ In-flight lock
❌ Response dedup state ❌ Delivery queue ❌ Outbox ❌ Idempotency
——**有承载数据 ≠ 实现 Retry**（C1 符号级扫描锁死；docstring 里的
「不做 Xxx」说明文字不算偷渡）。

同时保持不变：RUNTIME_EXECUTION_ORDER / Protocol 签名 / 失败模型
（STAGE_FAILURE_MAP）/ Delivery 语义 / Runtime 侧不写库（coordinator 仍
不认识 external_session_id / response_payload——写库只归 wrapper）。

---

## 6. 既有断言翻转（约定内，14 处）

| 测试 | 原断言 → 翻转后 |
| --- | --- |
| `test_step10_4_2::a1` | 桩 inner 返回 None → 返回合法 RuntimeExecutionResult（Runtime 边界已变更） |
| `test_step9_3::g` / `test_step9_7::d` | Session Port/Adapter 方法面 3 → 4（+ bind_external_session） |
| `test_step10_5_1::a4` | response_payload 全树 3 文件零写入方 → 6 文件、wrapper 成写入方（缺口关闭） |
| `test_step10_5_3::f3` | Delivery Retry 无承载物 → 承载物已现（完整成功）；仍无 Retry 入口（符号级） |
| `test_step10_7_1::a3` | Agent 回了什么不可见 → 成功路径可查（缺口关闭） |
| `test_step10_7_2::b1` | 三态全 NULL → 完整成功有值（矩阵修订）；b3：wrapper 未接线 → 已接线且仍不经 AgentResponse 类型；b4：会话级从不写 → wrapper 绑定（Coordinator 仍不写） |
| `test_step10_8_1::a2/a3/a5/b3` | 事实丢弃实证 → 已落地；快照推导 → 已删除；弱键仅剩失败路径 |
| `test_step10_8_2::f1` | wrapper 未引用 Result → 已接线 |
| `test_step10_8_3::c4` | response_payload/external_session_id 留给 10.8.4 → 已由 wrapper 写入（Runtime 侧分工保持） |
| `test_step6_7::test_i` | "bind" token → 精确放行 bind_external_session（Agent 会话引用绑定 ≠ Identity Linking） |
| `test_step8_1::test_d` | interaction/session 命名面 → + bind_external_session（Port/Adapter） |

---

## 7. 验收（→ 测试 13 项）

| # | 条件 | 测试 |
| --- | --- | --- |
| 1 | wrapper 接住并原样返回 Result | A1 |
| 2 | agent_response 原样快照落 response_payload | A2 |
| 3 | session_id 来自 Result（不再推导） | A3 / C3 |
| 4 | external_session_id 绑定 Session | A4 |
| 5 | CREATE_NEW 全链验证 + Evidence 直证 | B1 |
| 6 | REUSE 全链验证 + 零虚假 Evidence | B2 |
| 7 | None 不清空既有映射 | B3 |
| 8 | 无 Retry/幂等基础设施（符号级） | C1 |
| 9 | 失败模型 / Delivery 语义不变；dispatch 前失败 payload NULL | C2 |
| 10 | Case B payload 仍 NULL（诚实修订成文） | C4 |
| 11 | Runtime Core / 边界零改动 | C5 |
| 12 | 契约成文 | D1 |
| 13 | 全量回归 | 全量 |

全量回归：916 + 13 = **929 passed**。
