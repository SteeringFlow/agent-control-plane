# M6.6-C4 / CP4-2 — First Success Cross-Boundary Audit

> 前序：
> - `M6.6-C3 Bootstrap Arm` — PASS / CLOSED（真实 PG Closure）。
> - `M6.6-C4 / CP4-1 Session Concurrency` — PASS / CLOSED（`find_or_create` 收敛并发）。
>
> 本轮：对 **CP3 Bootstrap + CP4-1 Session** 合并后的完整 First Success 路径做**只读交叉边界审计**。
> **只读**：唯一新增物是本文件；未改动任何代码 / migration / 测试 / ADR。

---

## 1. Scope

审计 **Bootstrap、Identity、Grant、Session、Interaction、Worker、Agent execution、Delivery、replay、reclaim**
之间的**交叉边界**，而非重新审计任一组件内部。

聚焦单一不变量：

```
Verify Connection → Bootstrap Arm → trusted inbound → Identity/Grant
→ Session → Interaction → Worker claim → Agent execution
→ processing=succeeded → Delivery → delivery=delivered → First Success
```

在并发 / crash / retry 下，上述链是否仍保持正确语义。

## 2. First Success Definition（冻结）

First Success = 同一 `interaction` 行同时满足：

```
processing_status == succeeded
AND
delivery_status   == delivered
```

- **派生状态，不持久化**：`tests/test_cloud_0_1_2_workspace_golden_path_gate.py::test_first_success_is_derived_not_persisted`
  （G6）断言：全仓无 `FirstSuccess` / `OnboardingState` / `ActivationEvent` / `UserJourney`
  模型或表。**禁止新增**。
- 不得把 `processing=succeeded` 单独等同于 First Success；不得把 `delivery=delivered` 单独等同于 First Success。

## 3. Current End-to-End Flow

事务边界还原（每个 phase 一次独立短事务，见 `interaction_recording.py:168-169`）：

```
Intake（ChannelInboundService.accept_message）
  ① Trust Gate（CP-2 段 1：路由类型）          channel_inbound.py:283
  ② Adapter.parse_message（段 2：验签/解密）    channel_inbound.py:284
  ③ 可信 TenantScope（验签后才建）             channel_inbound.py:288
  ④ create_pending → PENDING + received(COMMIT) interaction_recording.py:257
        └─ 同 (channel, external_interaction_id) 已存在 ⇒ ACK+STOP（去重） channel_inbound.py:293-295
HTTP 200（ACK）
   ↓（响应之后，异步）
Worker.process(interaction_id)
  ⑤ claim_processing → PROCESSING (CAS, COMMIT) worker.py:82
  ⑥ Bootstrap.ensure（仅当有有效 Arm）         worker.py:90-91
  ⑦ RuntimeCoordinator.execute(inbound)         coordinator.py:93
       ├─ IdentityResolution（只读）            coordinator.py:100
       ├─ AccessAuthorization（只读）          coordinator.py:111
       ├─ Routing（只读）                      coordinator.py:117
       ├─ SessionResolution（只读 → REUSE/CREATE） coordinator.py:121
       ├─ SessionCreation（仅 CREATE_NEW）       coordinator.py:128
       ├─ AgentDispatch（外部 I/O）             coordinator.py:151
       └─ Delivery（外部 I/O）                  coordinator.py:171
  ⑧ record_success（processing=SUCCEEDED + delivery=DELIVERED + 同 COMMIT）
                                             interaction_recording.py:305-343
```

关键事实：**⑥ Bootstrap 在 ⑤ claim 之后、⑦ execute 之前**（CP3-F 冻结）。

## 4. Bootstrap → Interaction

- **Q1：Bootstrap 相对 Interaction 创建的顺序？**
  Interaction（PENDING）在 Intake ④ 创建并提交；Bootstrap 在 Worker ⑥ 执行——即
  **Interaction 创建在前，Bootstrap 在后**，且 Bootstrap 在 claim 之后。
- **Q2：Bootstrap 已建立 Grant，但 Interaction 随后失败？**
  Grant 在 Bootstrap 自己的短事务（⑥）已提交；Interaction 失败只是该行终态
  （processing=failed）。Grant **持久保留**。下一条消息 → 新 Interaction（独立）
  → Bootstrap.ensure 见 Identity+Grant 已存在 → `ALREADY` no-op（bootstrap.py:85-87）。
  **不产生第二 Grant**。
- **Q3：Interaction 已创建但 Worker 未处理，Arm 是否已 consumed？**
  **否**。`consume` 只在 Worker ⑥ 的 `_attempt` 内发生（bootstrap.py:116）。
  Intake ④ 只建 Interaction，不触及 Arm。故 PENDING 未 claim 时 Arm 仍为 `ARMED`。
- **Q4：Bootstrap 已建立授权，但 Interaction 因 rollback 不存在？**
  不可达：Interaction 在 ④ 已独立提交；⑥ Bootstrap 在 Interaction **之后**运行。
  唯一「Interaction 不存在」的情形是 ④ 自身回滚（未提交）→ 根本不会有 Worker ⑥。

## 5. Arm Consumption

`bootstrap_arm.py:57-84`：`consume` 是 `UPDATE ... WHERE id=arm AND status=armed AND expires_at>now`
的 **CAS**，赢家 `rowcount==1`。

- 输家（rowcount==0）→ `_attempt` 返回 False → `uow.rollback()`（bootstrap.py:124）→
  不开任何授权写入（ADR-12 §10「已消费但无授权」不可达）。
- 赢家在**同一事务**内 `consume + Identity.find_or_create + Grant.find_or_create`
  一并提交（bootstrap.py:115-138）。任一失败 → 整体回滚 → Arm 回 `ARMED` → 可 retry。

## 6. Replay（Arm Consumption × Interaction Replay）

- **Case A：第一条消息中途 crash（Arm consumed 之后）**
  Arm 已 `CONSUMED`（winner 提交）。reclaim 后新 Worker ⑥ 跑 `ensure` → `_established`
  因 Identity+Grant 已存在返回 `True` → `ALREADY`（bootstrap.py:95-96）→ **不再 consume**
  → 不重建 Identity/Grant。✅
- **Case B：第一条消息已产生 Interaction，但消息再次 replay**
  Intake ④ 的去重：`(channel, external_interaction_id)` 已存在 → 返回既有 id，ACK+STOP
  （channel_inbound.py:293-295）。**不新建 Interaction、不跑 Runtime、不触发 Bootstrap**。✅
- **Case C：Worker reclaim 同一 Interaction**
  见 §10。Bootstrap 因 `ALREADY` 安全 no-op。✅

结论：**Bootstrap establishment 是 one-shot（Arm CAS 保证）；Interaction replay 由 Intake
去重拦截，绝不二次建立 Identity/Grant**。

## 7. Identity / Grant / Session 三者收敛

三者各自 `find_or_create`：

- `Identity.find_or_create`（identity.py:42）——键 `(channel_id, external_id)`
- `Grant.find_or_create`（connection_conversation.py:90）——键四元组
- `Session.find_or_create`（CP4-1，persistence/repositories/session.py）——键 `(identity_id, connection_id, external_conversation_id)` + tenant

**两个并发 first messages（A、B）收敛推导**：

1. Bootstrap Arm CAS ⇒ 恰 1 个 consume ⇒ 恰 1 个 Identity + 1 个 Grant（§5/§6）。
2. A、B 同 `(channel, external_identity_id)` ⇒ `IdentityResolution` 解析到**同一个** `identity_id`
   （coordinator.py:100-102）。
3. A、B 同 `(channel, external_conversation_id)` + 同一条 Grant ⇒ Routing 选出**同一个**
   `connection_id`（coordinator.py:117-119，Authorization/Routing 只读、确定性）。
4. Session 键 = `(identity_id, connection_id, conversation)` —— 三者均同 ⇒
   `Session.find_or_create` 必然落到**同一** Session（命中复用 / 并发冲突回读复用，CP4-1）。

⇒ **危险态「Interaction A→Identity1→Session1；Interaction B→Identity1→Session2」不可达**：
   Session2 要求 `connection_id` 或 `identity_id` 不同，而二者均被 Bootstrap+find_or_create 收敛为同一值。
   该结论已被 CP4-1 B13 直接证明（见 §21）。

## 8. Worker Claim Boundary

- Worker 逻辑：`process` = `load → claim(CAS) → [Bootstrap] → execute → record`
  （worker.py:79-101）。
- **G1 安全阀**：`claim` 失败立即 return，Runtime **一次都不跑**（worker.py:82-83）。
- Q1：Interaction 被 claim 后 Worker crash —— 此时 Arm 是否已 consumed？
  取决于 crash 点：若 crash 在 ⑤ 之后、⑥ 之前 ⇒ Arm 仍 `ARMED`（reclaim 后 ⑥ 再跑）；
  若 crash 在 ⑥ 提交之后 ⇒ Arm 已 `CONSUMED`。Identity/Grant 只在 ⑥ 提交后存在。
- Q2：stale reclaim 后新 Worker 是否再次执行 Bootstrap？
  **是**（⑥ 在 claim 后、execute 前，每次 `process` 都跑 `ensure`）。
- Q3：再次执行是否因 Arm 已 consumed 而安全 no-op？
  **是**：`_established` 命中 ⇒ `ALREADY`，不 consume（bootstrap.py:85-87, 95-96）。

## 9. Worker Crash（Bootstrap × Worker Crash 组合）

逐项列出（赢家 Arm 提交后 runtime crash）：

| 项 | crash 前（⑥后⑦中） | reclaim+retry 后 |
|---|---|---|
| Identity | 已存在 | 复用（同 id） |
| Grant | 已存在 | 复用（同 id） |
| Arm | `CONSUMED` | `CONSUMED`（不再 consume） |
| Session | 尚未建（在⑦内） | `find_or_create` ⇒ 复用/新建后复用（同 id） |
| Interaction | `PROCESSING` | 经 ⑤ claim → ⑦ retry → record |

⇒ **不会创建第二个 Identity / Grant**（Arm 已 consumed + `_established` 命中 ⇒ ALREADY）。
Agent **会被重新执行**（Worker retry 语义，≠ inbound replay）；Session 复用。✅

## 10. Reclaim

`reclaim_stale_processing(threshold)`（interaction_recording.py:401-413）：`PROCESSING` 孤儿
→ `PENDING`（仅回收、不 claim、不执行）。回收后行重新进入 claim 池，由既有
`claim(CAS) + Worker` 接管。

- Reclaim **不是** inbound replay：它复用同一条 Interaction 记录（同 `id` / 同 `external_interaction_id`），
  仅重启其执行。
- Reclaim 重启后：`Bootstrap.ensure` ⇒ `ALREADY`（见 §8/§9）；`Session.find_or_create` ⇒ 复用；
  Agent **重新执行**（retry）。这与「inbound replay 被 Intake 去重拦截」是两件事，区分清晰。

## 11. Agent Execution Retry

区分两种事件：

- **Worker retry（reclaim）**：同 Interaction 重启 → Agent 重跑（§9/§10）。Identity/Grant/Session
  全复用；Arm 不消费。
- **inbound message replay**：Intake 去重 ⇒ 不新建 Interaction、不跑 Runtime（§6 Case B）。

`Bootstrap` 在 retry 下因 `ALREADY` 安全 no-op；**不会**因 reclaim 再次触发 Arm 消费或二次建立 Identity/Grant。

## 12. Delivery Boundary

`DeliveryExecutor`（delivery.py）只消费 `OutboundResponse`，**不读库、不重路由、不重解析**。

- **Case A：Agent 成功，但 Delivery 失败**
  `record_failure(stage==delivery)` ⇒ `processing=SUCCEEDED + delivery=FAILED`
  （interaction_recording.py:367-369）—— **4.2/5.3 冻结的合法组合**，非 bug。
- **Case B：Delivery retry → delivered**
  当前**无 Delivery retry 机制**（interaction_recording.py:47-49 明确「不做 Retry/Outbox」）。
  若未来新增，承载物是 `response_payload` 快照（interaction_recording.py:40-43），
  **不得**借 retry 重新执行 Agent。
- **Case C：Delivery retry 是否可能重新执行 Agent？**
  **当前不会**：Delivery 是 coordinator 内的独立外部 I/O（coordinator.py:171），
  `record_success` 已先把 `processing=SUCCEEDED+delivery=DELIVERED` 落定后才返回；
  Delivery 失败走 `record_failure` 且 **Agent 不再被调用**。证据：
  `tests/test_step10_5_3_duplicate_interaction_decision_semantics.py` 在 Delivery 失败时断言
  `agent.calls == 1`（绝不第二次调用 Agent）。✅
- 结论：**Delivery 独立于 processing**；当前实现下 Delivery 失败不重跑 Agent。

## 13. First Success Race

`record_success`（interaction_recording.py:305-318）在**同一事务**内同时写
`processing_status=SUCCEEDED` 与 `delivery_status=DELIVERED`：

```python
uow.interactions.record_outcome(
    ..., processing_status=PROCESSING_SUCCEEDED,
        delivery_status=DELIVERY_DELIVERED, ...)
uow.commit()
```

⇒ **不存在「processing=succeeded 但 delivery=pending」的瞬间**。First Success 判定必须挂在
**同一 interaction 行**的两个列上（定义见 §2），UI/逻辑不得把单列单独判成 First Success。
唯一「处理成功 + 投递未达」是显式的 `delivery=FAILED`（§12 Case A），那不是 First Success，是已知状态。

## 14. Duplicate Inbound

严格区分（§6 Case B 已覆盖）：

- **Duplicate A（完全相同 event / msgid）**：Intake ④ 按 `(channel, external_interaction_id)`
  去重 → 返回既有 Interaction id，ACK+STOP，**不新建 Interaction / 不跑 Runtime / 不触 Session**。
  WeCom 的 `msgid`（external_interaction_id）正是官方排重键（wecom.py:42, wecom_bot.py:47）。
- **Duplicate B（同一 conversation 的两个不同消息）**：两个不同 `external_interaction_id`
  → 两条 Interaction → 经 CP4-1 收敛到**同一 Session**（§7）。

二者不混：`external_interaction_id`（去重键）≠ Session 隔离键 `(identity, connection, conversation)`。

## 15. Concurrent Interaction

CP4-1 已证：2 消息 → 1 Session → 2 Interactions。`Session` 在执行期**无可变态**
（无 counter / 无 status 机 / 无 Agent 内部态；ACP 不持有 Agent 内部 state）。
`record_success` 对 Session 只做两件幂等操作：写 `session_id`（外键引用）+ 可选
`bind_external_session`（None 不写、不覆盖，interaction_recording.py:326-329）。

⇒ 两个 Interaction 共用同一 Session 属**预期且安全**；**不因共享 Session 引入 Session locking**
（ACP 不拥有 Agent internal state，见 Non-Goals）。

## 16. RLS / Tenant Isolation

全链读写均经 tenant-scoped UoW：

- Bootstrap `consume` / `find_active_by_channel` 带 `tenant_id` 过滤（bootstrap_arm.py:94-95）；
- Identity/Grant/Session `find_or_create` 的 reread 走 tenant-scoped repo（CP4-1 同构）；
- Interaction / Activity / Delivery 写入带 UoW 租户上下文（unit_of_work.py:104-108）。

并发 recovery 的 reread **不绕过 RLS**：tenant 由 UoW 作用域承担，四元组回读复用既有过滤。

## 17. Crash Matrix

按真实事务边界逐项（✅= 状态自洽/可恢复；⚠= 预存限制，非本轮引入）：

| Crash 点 | Identity | Grant | Arm | Session | Interaction | 重试行为 |
|---|---|---|---|---|---|---|
| ④前（Trust/解析） | — | — | ARMED | — | — | 无副作用 |
| ④中（create_pending 未提交） | — | — | ARMED | — | 不存在 | 重推（去重未发现）→ 新 PENDING |
| ④后/⑤前（PENDING 未 claim） | — | — | ARMED | — | PENDING | ⚠ 无 claimer 则卡 PENDING（Worker 可用性，预存） |
| ⑤后/⑥前（PROCESSING 未 Bootstrap） | — | — | ARMED | — | PROCESSING | reclaim→⑥重跑 |
| ⑥中（consume+id+grant 未提交） | — | — | ARMED | — | PROCESSING | 回滚→reclaim→⑥重跑 ✅ |
| ⑥后/⑦中（Bootstrap 已提交） | 存在 | 存在 | CONSUMED | 未建 | PROCESSING | reclaim→⑥ALREADY→⑦retry→Session 复用 ✅ |
| ⑦中（Agent 执行） | 存在 | 存在 | CONSUMED | 见 retry | PROCESSING | reclaim→⑦retry（Agent 重跑）✅ |
| ⑧中（record_success 未提交） | 存在 | 存在 | CONSUMED | 存在 | PROCESSING | 回滚→reclaim→新 attempt 重跑 ⚠（见 §18） |
| ⑧后（First Success 达成） | 存在 | 存在 | CONSUMED | 存在 | succeeded+delivered | 完成 ✅ |

**CP3+CP4-1 组合未引入任何新交叉边界缺陷**：所有「半成功」要么是原子事务不可达（⑥），
要么是预存的 Worker/reclaim 行为（④后无 claimer、⑧回滚重跑），与 Bootstrap/Session 无关。

## 18. Partial Success States

按真实边界枚举「半成功」候选，判定是否 bug：

- **Identity exists, Grant missing**：稳态不可达。Bootstrap 的 Identity+Grant 同事务提交
  （bootstrap.py:115-138）；二者只经 Bootstrap 建立。**
  理论退化态**（Arm 已 consumed 但 Grant 缺失——因原子性实际不可达）下：
  `_established` 返回 False → `_attempt` 再试 `consume` 必 False（Arm 已 consumed）→
  `NOT_APPLICABLE` → 不建立 → runtime 的 `AccessAuthorization` DENY（无 Grant）→ 消息失败。
  属 fail-closed，且实际不可达。
- **Grant exists, Identity missing**：同上不可达（同事务）。
- **Arm consumed, Identity/Grant missing**：**不可达**（原子事务，ADR-12 §10）。
- **Session exists, Interaction missing**：**不可达**——Session 在 ⑦ 内创建，Interaction 在 ④ 已提交；
  Session 存在 ⇒ Interaction 必存在。
- **Interaction succeeded, Delivery 不一致**：显式合法态 `processing=succeeded + delivery=failed`
  （§12 Case A），非 bug；且 First Success 要求两者齐全（§2/§13）。

## 19. Local First Success

`tests/test_step15_2_3_local_first_success.py::test_e1_local_first_success_full_chain`：
POST local 消息 → ACK → Worker → 六段 Runtime → `processing=succeeded + delivery=delivered`，
`session_id` 非空、Identity/Session 真实物化、未绕过核心模型。Local 路径可经**管理面预置**
Identity/Grant（非 Bootstrap）抵达 First Success，与 Bootstrap 路径正交。

## 20. Real WeCom First Success

信任顺序（CP-2，先于 Bootstrap）：

```
HTTP 路由声明 expected_channel_type（段 1）   channels.py / channel_inbound.py:283
        ↓
Adapter 验签/订阅连接（段 2）                  channel_inbound.py:284
        ↓
可信 TenantScope（验签后）                     channel_inbound.py:288
        ↓
create_pending → Worker → claim → Bootstrap → runtime → delivery
```

- **不存在「untrusted webhook → Bootstrap」**：Trust Gate 在 Intake ④ 即拦截（不匹配/验签失败
  → 不产 Interaction，更不进 Worker）。WeCom `wecom_bot` 单聊 `conversation == identity`
  （wecom_bot.py:49），与 `wecom.py` 同形，Session 隔离键语义一致。
- E2E 证据：`tests/test_3b_2_2_wecom_bot_websocket_transport.py` 断言 WeCom E2E 完整成功
  （`processing=succeeded + delivery=delivered`，session 非空）；其 Delivery 失败变体断言
  `processing=succeeded + delivery=failed + retryable=False`（应用层明确拒绝重试）。

## 21. Existing Evidence

| 边界 | 既有测试证据 |
|---|---|
| Bootstrap 并发收敛 | `test_m6_6_c3_cp3_g_bootstrap_runtime.py`：并发首条消息 ⇒ 1 Identity + 1 Grant + 2 Interaction（均 succeeded） |
| Session 并发收敛 | `test_m6_6_c4_cp4_1_session_concurrency.py::B13`：并发首条消息 ⇒ 1 Identity + 1 Grant + 1 Session + 2 Interaction（同 session、均 succeeded）；B1~B12 覆盖复用/隔离/replay/reclaim |
| First Success 派生不持久化 | `test_cloud_0_1_2_workspace_golden_path_gate.py::test_first_success_is_derived_not_persisted`（G6） |
| Local First Success | `test_step15_2_3_local_first_success.py::test_e1_*` |
| WeCom E2E First Success | `test_3b_2_2_wecom_bot_websocket_transport.py` |
| Worker attempt fence / reclaim | `test_step12_9_4_12_attempt_fence.py`（claim CAS、reclaim、ownership 丢失 REJECTED） |
| Delivery 失败不重跑 Agent | `test_step10_5_3_duplicate_interaction_decision_semantics.py`：`delivery=FAILED` ⇒ `agent.calls == 1` |
| Duplicate 去重 | `test_step10_5_3_*`：`(channel, external_interaction_id)` 已存在 ⇒ ACK+STOP |

## 22. Evidence Gaps

- **显式「reclaim 重启后 Session 复用且 Identity/Grant 计数不变」的专用断言**目前分散在
  CP3-G（Identity/Grant 计数）+ CP4-1 B13（Session 计数）+ attempt_fence（reclaim 机制）三处，
  缺一个把「reclaim（非首次 inbound）↔ Identity/Grant/Session 零增长」放在同一条断言里的测试。
  **登记为 evidence gap，不当作实现 bug**（既有证据已分别覆盖各侧面）。
- **⑧ 中回滚重跑的「双 Delivery」**：预存 attempt-fence 限制（见 §17 ⚠、§18），无专门交叉测试；
  非 CP3/CP4 引入。
- **真实 PG 下 Bootstrap 与 Session 双重竞争的端到端并发**：CP4-1 B4/B4b 覆盖 Session 侧 PG 行为，
  CP3-G 覆盖 Bootstrap 侧 PG 行为，但二者**同一事务流**的 PG 并发（env 门控）尚无单一集成测试。

## 23. Findings

1. Bootstrap 与 Session 的并发收敛**互不冲突**：Bootstrap 用 Arm CAS 收敛 Identity/Grant，
   Session 用唯一约束 + `find_or_create` 收敛 Session，二者键空间正交、收敛时机错开
   （Bootstrap 在 claim 后、Session 在 runtime 内），组合后恰收敛到
   `1 Identity + 1 Grant + 1 Session + 2 Interaction`（§7，B13 实测）。
2. **无新架构矛盾**：所有 Executor 只读、Bootstrap 原子、record_success 原子、Intake 去重、
   Trust Gate 在前——交叉语义自洽。
3. **Delivery 与 processing 解耦**已成立且不重跑 Agent（§12，实测 `agent.calls==1`）。
4. 唯一「半成功」风险均为**预存** Worker/reclaim 行为（④后无 claimer、⑧回滚重跑），
   与 CP3/CP4 无关，本轮**不解决**（Non-Goals）。

## 24. Non-Goals

本轮**不解决**（只登记观察，不实现）：

- Session lifecycle / expiration
- Agent memory / context / internal state（ACP 不持有）
- multi-agent coordination / Group Bootstrap
- new authorization model / delivery protocol / Evidence model / failure taxonomy
- UI redesign
- 新增事务 / 锁 / 状态机 / 新 Evidence 来「让 First Success 更可靠」——本轮纪律是验证现有架构
  是否足够，而非重新设计。

## 25. Final Gate

```
PASS
```

CP3 Bootstrap + CP4-1 Session Concurrency 合并后的 First Success 链路**无新的架构问题**：
交叉边界（Bootstrap×Interaction×Worker×Session×Delivery×replay×reclaim）语义自洽，
并发/崩溃/重试下不出现跨租户误复用、不出现第二 Identity/Grant/Session、不出现
`processing=succeeded` 单态误判的 First Success。仅有的「半成功」候选或为原子事务不可达、
或为预存的 Worker/reclaim 限制（已诚实登记，不在本轮范围）。
