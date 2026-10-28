"""P2 Delivery Recovery —— 实现与运维手册（含部署顺序与核验 SQL）。

本文件是**部署前必读**的 runbook：P2 的恢复驱动**默认关闭**，必须按下面的顺序
完成迁移、排空、回填与核验，**另行授权**后才能开启。

## 0. 能力边界（先读这一节）

- P2 提供的是**按失败类别分级的投递恢复**：
  - 「可证明从未尝试过投递」⇒ **允许**重投；
  - 「可证明未送达且明确允许重试」⇒ **允许**重投；
  - 「结果未知」⇒ **永不**自动重投（转人工裁决）。
- P2 **不**提供、也**不**承诺端到端 exactly-once：租约只保证「同一时刻一个在途
  attempt」；旧 attempt 可能仍在外部发送 ⇒ 重复消息风险**存在**。
- 迟到 ACK **不**触发状态升级（本轮未实现 Adapter → 应用层桥接），也**不**触发
  发送；企业微信同一 `req_id` 二次 respond 的幂等行为**待 P4 实测**，本手册
  **不**声称渠道具备幂等保证。

## 1. 状态与围栏（实现口径）

| 字段 | 含义 |
|---|---|
| `delivery_status` | `pending` / `delivering` / `delivered` / `failed` / `unknown` |
| `delivery_attempts` | 投递 attempt **单调计数** = 投递围栏身份（**不用时间戳**） |
| `delivery_started_at` | 投递 attempt 租约起点（DB 时钟） |
| `delivery_protocol` | `0` 历史/未知协议（**禁止自动重投**）/ `1` 新协议 |

- `failed` 在新协议下**严格**代表「可确认未送达」；结果未知（ACK 超时、发送异常）
  一律落 `unknown`。
- `unknown` 与 `delivering` **结构上**不在重投白名单内（由 CAS 条件保证）。
- 租约过期 ⇒ `delivering → unknown`（**绝不**退回 `pending`）。

## 2. 部署顺序（必须按序）

| 步 | 动作 | 前置 | 备注 |
|---|---|---|---|
| S0 | （可选）先上线 P1（phase A 落结果） | — | P1 形态的行 `protocol=0` ⇒ 永不自动重投 |
| S1 | 备份 + `alembic upgrade head`（0008 → 0009，**仅加三列**） | 备份 | 旧代码不引用新列 ⇒ 不会崩 |
| S2 | 部署 P2 代码，**恢复驱动保持关闭**（`RECOVERY_ENABLED=false`） | S1 | phase A 开始写 `protocol=1` |
| S3 | **排空所有旧 Worker**（全部重启到新代码） | S2 | 旧 Worker 产生的行 `protocol=0` ⇒ 兜底不可重投 |
| S4 | 执行一次性回填（tenant-scoped、幂等、**不写 Evidence**） | S3 | `python -m tools.delivery_backfill --tenant-id <t> --database-url <url> --apply` |
| S5 | 执行**核验查询**（见 §3） | S4 | 两类历史候选**分别**检查 |
| S6 | 灰度 + 观测 + 配置确认 | S5 | — |
| S7 | **另行授权**后开启 `RECOVERY_ENABLED=true` | S6 | 本次交付**不**开启 |

## 3. 核验查询（**只读**，由运维执行）

```sql
-- 3.1 历史 pending（投递事实未知）——必须为 0
SELECT count(*) FROM interactions
 WHERE tenant_id = :t AND delivery_protocol = 0
   AND processing_status = 'succeeded' AND delivery_status = 'pending';

-- 3.2 历史 failed & retryable（ACK 超时与「明确未送达」同形）——必须为 0
--     ⚠️ 不可用「retryable=TRUE 的单个过滤条件」代表所有候选已清零：
--     3.1 与 3.2 必须**分别**检查
SELECT count(*) FROM interactions
 WHERE tenant_id = :t AND delivery_protocol = 0
   AND processing_status = 'succeeded'
   AND delivery_status = 'failed' AND retryable = TRUE;

-- 3.3 新协议待投递行（恢复驱动的候选集；应为「已知数量」，非 0）
SELECT delivery_status, count(*) FROM interactions
 WHERE tenant_id = :t AND delivery_protocol = 1
   AND processing_status = 'succeeded' GROUP BY 1;

-- 3.4 结果未知（人工裁决队列）——增长需人工介入
SELECT count(*) FROM interactions
 WHERE tenant_id = :t AND delivery_status = 'unknown';
```

（工具内的等价计数：`tools.delivery_backfill.verification_counts(session, tenant_id)`
——`legacy_pending` 与 `legacy_failed_retryable` **分别**返回 ✓。）

## 4. 回滚限制

| 回滚 | 数据 | 能力 | 结论 |
|---|---|---|---|
| 回滚**代码**（保留迁移与数据） | ✅ 安全（新协议行停在 succeeded/pending|delivering|unknown，旧代码不触碰） | 恢复能力丧失（行永久滞留，需人工） | 可接受 |
| 回滚**迁移**（drop 三列） | ❌ 危险：丢失 `delivery_protocol` ⇒ 历史歧义行会被误读为「可重投」 | — | **禁止**（库中已存在 `protocol=1` 行时） |
| 回滚**回填**（把 unknown 改回原值） | ⚠️ 需人工逐条判断，且**不得**把历史行改回可重投态 | — | 仅人工裁决后进行 |

## 5. 配置项

| 配置 | 默认 | 说明 |
|---|---|---|
| `RECOVERY_ENABLED` | `False` | **默认关闭**；开启需完成 §2 全流程并另行授权 |
| `RECOVERY_POLL_INTERVAL_SECONDS` | `10.0` | 轮次间隔 |
| `RECOVERY_PICKUP_LIMIT` | `10` | 每轮候选上限（有界） |
| `RECOVERY_RECLAIM_EVERY` | `30` | 每多少轮回收一次过期租约 |
| `RECOVERY_LEASE_SECONDS` | `300` | ⚠️ **保守默认值，未经生产实测**（待 P4）：须 > 发送耗时上界 + ACK 窗口 + 余量 |
| `RECOVERY_MAX_ATTEMPTS` | `3` | **总** attempt 次数上限（含首次投递）；达上限不再重投 |

## 6. 待 P4 实测（**不得**当作已验证）

1. 企业微信同一 `req_id` 二次 respond 的行为（幂等？拒绝？双发？）——决定重投风险；
2. ACK 延迟分布（p50/p95/p99）——校准 ACK 窗口与 `RECOVERY_LEASE_SECONDS`；
3. `errcode != 0` 的确切语义（是否可能意为「已消费」＝ 实际已送达）——因此
   `unknown` **不得**被升级为 `failed`。

## 7. 已知限制

- 恢复重发**已能还原 artifact 引用**（P2.1 修复）：`agent_response_from_snapshot()`
  把已落库的 artifact 快照还原为 `Artifact` 领域对象 ⇒ 恢复消息与首次消息
  **逐字段一致**（含引用块渲染）。快照缺失/非法/形状不兼容 ⇒ **Fail Closed**
  （ValueError），**绝不**静默降级成「没有引用的消息」并继续投递。
  ⚠️ 残留：快照重建发生在 claim **之后**（恢复执行器顺序未改），故一条坏快照会
  先消耗一次 attempt（行停在 `delivering`，租约到期后转 `unknown` → 人工裁决）✓ 安全。
- 迟到 ACK 跨进程**不可得**（Adapter 侧观测为进程内存态）⇒ `unknown` 只能靠日志
  与人工裁决；本轮**未**实现应用层桥接。
