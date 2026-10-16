# Step 10.4.2 — Runtime Interaction Persistence Vertical Slice

> **目标**：让一次完整 Runtime 执行产生一条 Interaction + 一组 Activity，并
> 正确记录成功、执行失败、Delivery 失败——把 10.4.1 的审计结论变成运行时事实。

---

## 1. 实现（10.4.1 裁决的落地形状）

```
ChannelInboundService（零改动）
      ↓ PersistingCoordinator          ← app/application/interaction_recording.py
      │   （RuntimeExecutionCoordinator Protocol 装饰器，10.4.1 §C 裁决挂点）
      ↓ inner RuntimeCoordinator（零改动、零感知）→ 六 Executor（零改动）
```

**新增面（最小）**：

| 层 | 文件 | 内容 |
| --- | --- | --- |
| domain | `interaction_record.py` / `activity_record.py` | 记录域镜像（frozen dataclass）。命名裁决：`InteractionRecord` **≠ 8.1 InboundInteraction**（不落库的 Adapter 输出）；`record` 后缀精确表达 Evidence 语义 |
| Port | `repositories/interaction_record.py` / `activity_record.py` | 生命周期三跃迁（create / mark_processing / record_outcome）+ get_by_id；Activity 只有 `append`（append-only 无 update/delete） |
| Mapper | `persistence/mappers/interaction_record.py` / `activity_record.py` | to_domain / to_orm |
| Adapter | `persistence/repositories/interaction_record.py` / `activity_record.py` | SQLAlchemy 实现（flush 不 commit，事务归 UoW） |
| UoW | Port + 实现 | + `interactions` / `activities` 两仓库（恰 8 仓库，快照守卫防继续扩边） |
| application | `interaction_recording.py` | `PersistingCoordinator` + `STAGE_FAILURE_MAP`（typed error → stage/code/retryable 显式映射） |
| SessionRepository | Port + 实现 | + `find_active_by_identity_and_conversation`（wrapper 最小读路径） |

## 2. Interaction 生命周期（10.4.2 冻结的最小状态机）

```
processing: PENDING ──→ PROCESSING ──→ SUCCEEDED │ FAILED
delivery:   PENDING ─────────────────→ DELIVERED │ FAILED（PENDING = 未到达 Delivery）
```

- PENDING 与 PROCESSING 分属两个事务——PENDING 是**真实持久化过的状态**
  （行为侧由 test_a1 在 inner 执行中观察 PROCESSING 验证）；
- 终态由 `record_outcome` **一次性**落定（状态 + 失败三元组 + session 回填）。

## 3. Evidence 设计（本步最关键的两个裁决）

### 3.1 不给 Coordinator/Executor 加 callback（问题由实现暴露后裁决）

Coordinator 只有 `execute() -> None`（9.1 冻结：不引入 RuntimeResult；
10.3.1 守卫：无 agent-specific 分支）。**证据显示**：如果给六段加
`on_stage_started/completed` 回调，persistence 会重新渗透 Runtime——正是
10.4.1 排除的形状。因此：

- **Executor 不写 Activity**（9.A / 10.3.1 / 10.4.1 全部守卫继续成立，
  test_b8 AST 复核）；
- Evidence 全部由 wrapper 在**执行边界**观察：
  - 失败路径：typed error 类型 → `(failure_stage, failure_code, retryable)`
    显式映射（`STAGE_FAILURE_MAP`），五段失败全覆盖；
  - **session_creation Evidence 由持久化事实推导**：执行前后该
    `(identity, conversation)` 的 active session 由无到有 ⇒ CREATE_NEW
    真实发生 ⇒ 记 `session_created`（stage=session_creation, SUCCEEDED）。
    **REUSE_ACTIVE 不产生任何 session_creation Evidence**——凭空制造的
    success 不是事实（test_b2）；
  - 成功：`interaction_completed`（stage=delivery, SUCCEEDED）。

### 3.2 成功路径的中间段 Evidence：诚实记录为已知边界

identity_resolution / routing / session_resolution / agent_dispatch 的**逐段
成功 Evidence** 需要 stage 级回调或结构化回流——wrapper 观察不到段间边界。
本步**不为成功段制造未经观察的 Evidence**（Evidence = 事实）。真实需要时
走专门裁决（callback / RuntimeResult——后者需解冻 9.1，本步不动）。

## 4. Failure Recording（验收核心）

| 场景 | processing | delivery | failure_stage | failure_code | retryable | session_id |
| --- | --- | --- | --- | --- | --- | --- |
| Identity 失败 | FAILED | PENDING | identity_resolution | IDENTITY_RESOLUTION_FAILED | False | NULL |
| Routing 失败 | FAILED | PENDING | routing | ROUTING_FAILED | False | NULL |
| Session 失败 | FAILED | PENDING | session_resolution | SESSION_RESOLUTION_FAILED | False | NULL |
| Dispatch 失败 | FAILED | PENDING | agent_dispatch | AGENT_DISPATCH_FAILED | **True** | **回填** |
| AgentResponse 无效 | FAILED | PENDING | agent_dispatch | AGENT_RESPONSE_INVALID | False | 回填 |
| **Delivery 失败** | **SUCCEEDED** | **FAILED** | delivery | CHANNEL_DELIVERY_FAILED | True | 回填 |

- **Delivery failure ≠ Processing failure**（4.2 冻结的合法组合）——test_b6；
- retryable 语义：transport/timeout 类 True；契约/配置/身份类 False；
- SessionCreationExecutor 失败复用 `SessionResolutionFailedError`（9.7）→
  其失败 Evidence 归 stage=session_resolution（错误码不可区分，诚实映射）；
- 错误记录后**原样 re-raise**——webhook 5xx 传播行为与 9.8.2 冻结一致。

## 5. Schema 修正案（实现驱动的 4.2 演进）

实现立即暴露：`interactions.session_id` / `activities.session_id` 为 NOT NULL
时，**Session 建立之前的失败**（identity/routing）无法被记录——与验收要求
直接冲突。裁决：

- 两列 → `nullable=True`（语义：前段失败无 Session 可挂；成功路径与到达
  Dispatch 的失败路径由 wrapper 回填）；
- **Alembic 触发条件达成**（5.3 预言的「第一个需要演进已有库 schema 的真实
  变更」）——记录在案，迁移工具链留真实部署 Step（MVP 仍 create_all）；
- SessionRepository 新读方法是有记录的近似（隔离键弱化为二键）——MVP 语义
  下 (identity, conversation) 唯一确定 active session（8.4 单 Connection 冻结）；
  单 Channel 多 Connection 落地时收紧。

## 6. 验收（10.4.2 §八 17 条 → tests/test_step10_4_2_interaction_persistence.py）

| # | 条件 | 测试 |
| --- | --- | --- |
| 1 | 成功 Interaction 能持久化 | B1 |
| 2/3 | processing_status / delivery_status 正确 | B1/B3/B6 |
| 4 | failure_stage/code/retryable 正确 | B3-B6 |
| 5 | REUSE_ACTIVE 不生成虚假 session_creation Activity | B2 |
| 6 | CREATE_NEW 有 session_creation Evidence | B1 |
| 7 | Agent Dispatch failure 被记录 | B5 |
| 8/9 | Delivery failure 被记录且 ≠ processing failure | B6 |
| 10 | Interaction 与 Session FK 正确 | B1/B5/B6（NULL 语义见 B3） |
| 11 | Activity append-only | B7 |
| 12/13 | RuntimeCoordinator / Executor 不增加 persistence 依赖 | B8（AST）+ 9.A/10.3.1/10.4.1 既有守卫 |
| 14 | API 不直接操作 Repository | 既有 import boundary 守卫（service 零改动） |
| 15 | 无新队列/Outbox/Event Bus/Audit Framework | 10.3.1 D7 + 10.4.1 c3（恰 8 仓库） |
| 16/17 | 全量回归 / Worktree clean | **782 passed**（771 + 11）/ ✅ |

另：A1（生命周期 PENDING→PROCESSING 可观察）、A2（映射表完整性）、B9
（wrapper 装配进 ChannelInboundService，service 零改动）。
