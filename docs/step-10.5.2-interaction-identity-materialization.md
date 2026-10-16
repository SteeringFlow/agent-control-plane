# Step 10.5.2 — Interaction Identity Materialization

> 目标只有一个：把 **Channel + External Interaction ID** 从 JSON 内的事实，
> 提升成 Interaction 的一等持久化身份。验收 14 条，测试 16 项。

---

## 1. 物化前后

**Before（10.4.2 / 10.5.1 审计后的状态）**：

```
Interaction
├── id
├── session_id
├── external_interaction_id        ← 仅一列，可空、无索引、无唯一
└── request_payload.channel_id     ← scope 只活在 JSON 快照里
```

**After（本步）**：

```
Interaction
├── id
├── channel_id                     ← 一等持久化身份（FK → channels.id, NOT NULL）
├── session_id
├── external_interaction_id
└── request_payload                ← 仍保留原始事件（含 channel_id 快照）
```

并建立：**`UNIQUE(channel_id, external_interaction_id)`**
（`uq_interactions_channel_external_id`）。

---

## 2. 设计结果：统一身份纪律

这是本步最漂亮的结果——外部事件身份与系统既有身份模型完全同构：

| 实体 | 身份 | 既有约束 |
| --- | --- | --- |
| Identity | `channel_id + external_id` | `uq_identities_channel_external_id`（6.6） |
| **Interaction** | **`channel_id + external_interaction_id`** | **`uq_interactions_channel_external_id`（本步）** |
| Connection | `channel_id + agent_id` | `uq_connections_channel_agent_id`（7.1） |

统一纪律：

> **所有外部世界产生的 ID，都必须在自己的 Channel Scope 内解释。**

这是 Control Plane 的一条核心设计原则。此后 `Interaction` 的身份模型与
`Identity` 逐字对齐——Repository 读路径也刻意取同名
`find_by_channel_and_external_id(channel_id, external_interaction_id)`。

---

## 3. 改动面（最小）

| 层 | 文件 | 内容 |
| --- | --- | --- |
| domain | `interaction_record.py` | + `channel_id`（记录域镜像；创建时必填，ORM NOT NULL 兜底） |
| ORM | `persistence/models/interaction.py` | + `channel_id`（FK → channels.id，NOT NULL）+ `UNIQUE(channel_id, external_interaction_id)` |
| Port | `repositories/interaction_record.py` | + `find_by_channel_and_external_id`（精确 identity 读回） |
| Adapter | `persistence/repositories/interaction_record.py` | 同上实现（UoW 已暴露 interactions，无需扩边） |
| Mapper | `persistence/mappers/interaction_record.py` | channel_id 双向映射 |
| application | `interaction_recording.py` | `_start` 写入 `channel_id=inbound.channel_id`（1 行） |

**零改动**：Runtime Coordinator、六 Executor、`RuntimeExecutionCoordinator` Port、
`ChannelInboundService`、两个 Channel/Agent Adapter（wecom / acp）、
`request_payload`（仍保留原始事件，含 channel_id 快照）。

---

## 4. Schema 语义裁决

1. **`channel_id` NOT NULL**：Interaction 从创建第一刻起就必然属于某个 Channel
   （`InboundInteraction.channel_id` 是必填 int）。与 `session_id` 可空的理由不同——
   Session 是执行过程中才建立的，Channel 是事件本身的属性。
2. **`external_interaction_id` 保持可空**：身份的另一半可以缺席；SQL 唯一约束
   对 NULL 不去重，「无外部 ID 的行不参与去重」的语义恰好自然成立，无需特判。
3. **不单独为 `channel_id` 建索引**：复合唯一索引以 `channel_id` 为前导列，
   天然服务 Channel 作用域访问（identities / connections 同纪律）——
   「必要索引」= 恰好一个新索引。
4. **Schema 演进记录**：这是 5.3 预言、10.4.2 触发的 Alembic 条件的第二次
   真实演进。已部署库需要迁移（加列 + 回填 + 加唯一约束）；MVP 仍用
   create_all，迁移工具链仍留真实部署 Step（与 10.4.2 修正案同一处理）。
   语义上：历史行的 channel_id 需从 `request_payload->>'channel_id'` 回填。

---

## 5. 本步明确不做 Duplicate Handling（Idempotency ≠ Retry）

完成本步后：

```
(channel_id, MsgId) → UNIQUE
     第一次 create → 成功
     第二次 create → DB conflict（IntegrityError 原样暴露）
```

**本步到此为止，不决定 conflict 后怎么办。**

原因（10.5.1 §Q10 已记录的冲突）：

```
             External Event
                    │
                    ▼
             Interaction Identity   ← 本步物化的就是这一层
                    │
             ┌──────┴──────┐
          Duplicate?       New?
             │
             ▼
       Execution State
             │
       ┌─────┼─────┐
     PENDING PROCESSING TERMINAL
                         │
                    retryable?      ← 真正的难点在这里
```

- 若简单 `if exists: return` → **WeCom 的天然重试机制被 CP 杀死**
  （`FAILED + retryable=True` 的 transport 类失败永远失去自愈通道）；
- 若 `if failed and retryable: execute_again()` → 这又不是传统意义的幂等去重。

**Idempotency ≠ Retry**——这一层语义（Duplicate 判定 × Execution State ×
retryable 的组合矩阵）属 **10.5.3+ 的 Execution semantics 裁决**。本步的
机器约束：

- 写入边界**不做** try/except IntegrityError、不做 find-then-skip；
- Repository **不提供** create-or-get / upsert / find-or-create 语义；
- 无 Retry 执行入口、无 DUPLICATE 状态、无新 idempotency_key、无 Replay API。

`find_by_channel_and_external_id` 只回答「是否已存在」这一事实，
消费方式留待专门裁决。

---

## 6. 验收（14 条 → 测试 16 项）

| # | 条件 | 测试 |
| --- | --- | --- |
| 1 | channel_id 成为 Interaction 一等字段 | A1 |
| 2 | channel_id FK 正确 | A2 |
| 3 | external_interaction_id 继续保留（仍可空） | A1 |
| 4 | (channel_id, external_interaction_id) UNIQUE | A3 |
| 5 | 同 MsgId + 同 Channel 无法产生两条 Interaction | B3（IntegrityError + 1 行 + 1 次副作用） |
| 6 | 同 MsgId + 不同 Channel 可以共存 | B2 |
| 7 | Repository 可以精确查询 identity | C1 / C2 |
| 8 | request_payload 仍保留原始事件 | A5 |
| 9 | Runtime Core 零改动 | D1 |
| 10 | Adapter 零改动 | D2 |
| 11 | 不引入幂等决策 | E1 |
| 12 | 不引入 Retry / Duplicate 状态 / 新 key | E2 |
| 13 | 全量回归 | 801+ 全绿 |
| 14 | Worktree clean | git status |

核心三部曲（用户指定的三个测试）：**B1 Channel A + MsgId 100 → allowed /
B2 Channel B + MsgId 100 → allowed / B3 Channel A + MsgId 100 → conflict**。

附加：A4（索引裁决）、B4（conflict 不翻译不吞）、D3（UoW 无需扩边）、
F1（10.4.2 失败语义回归：Identity 失败记录现在也带 channel 身份）、
G1（契约成文）。

---

## 7. 前置快照翻转（约定内）

10.5.1 审计快照中已被本步关闭的缺口断言按 10.4.1→10.4.2 同一约定**就地翻转**：

| 测试 | 原断言 | 翻转后 |
| --- | --- | --- |
| `test_step10_4_1::a1` | 列集无 channel_id | 含 channel_id |
| `test_models::test_b_interactions_schema` | 列序无 channel_id、无唯一约束 | 含 channel_id + 唯一约束 |
| `test_step10_5_1::B1` | 无唯一约束 | 唯一约束已物化 |
| `test_step10_5_1::B3` | 无 channel_id 列（缺口 G1） | 一等 FK 列（G1 关闭） |
| `test_step10_5_1::D1` | 同 MsgId 双执行（2 行 2 副作用） | 同 MsgId → DB conflict（1 行 1 副作用） |
| `test_step10_5_1::E1/E2` | 同 MsgId 两次执行验证终态不可逆 | 改用不同 MsgId（同 MsgId 已被身份约束） |
| `test_step10_5_1::G1` | in-flight 重推同 MsgId | 改用不同 MsgId（隔离 in-flight 与身份 conflict） |

10.5.1 文档保留为审计史实，顶部已加「状态快照时效」注记。
