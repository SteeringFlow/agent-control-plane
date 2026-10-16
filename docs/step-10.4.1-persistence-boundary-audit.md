# Step 10.4.1 — Persistence Boundary Audit

> **审计步，生产代码改动：0 行。** 交付物 = 本报告 + 审计状态快照测试 8 项
> （`tests/test_step10_4_1_persistence_boundary_audit.py`，全量 771 passed）。

## 1. 现状盘点（Interaction / Activity 全链八层）

| 层 | Interaction | Activity |
| --- | --- | --- |
| **ORM**（`persistence/models/`） | ✅ `InteractionModel`（13 列，4.2 冻结字段全齐；docstring 明示双状态不合成） | ✅ `ActivityModel`（8 列；append-only：无 updated_at；status NOT NULL 无默认） |
| **Schema** | ✅ 已注册 `Base.metadata`（8 张表）；`session_id` FK（连接级 PRAGMA foreign_keys=ON 兜底）；无合成 `status` 列（test_models 显式断言） | ✅ `interaction_id` FK **nullable**（允许会话级活动）/ `session_id` FK |
| **Domain 实体** | ❌ 无 `app/domain/interaction.py`——8.1 冻结：`InboundInteraction`（Adapter 归一化输出，不落库）**不是** Interaction ORM 的领域镜像 | ❌ 无 |
| **Mapper** | ❌ `persistence/mappers/` 仅 6 实体（user/identity/channel/agent/connection/session） | ❌ |
| **Repository Port**（`app/repositories/`） | ❌ 无 | ❌ |
| **Repository Adapter**（`persistence/repositories/`） | ❌ 无 | ❌ |
| **UoW**（Port + 实现） | ❌ 只暴露 6 仓库（users/identities/channels/agents/connections/sessions） | ❌ |
| **Runtime executor**（六段 + Coordinator + channel_inbound） | ✅ **零接触**（全部只经 UoW Port 的 6 仓库；Dispatch/Delivery 均不写库——9.5/9.6 冻结） | ✅ 零接触 |
| **Migration** | ⚠️ 无 Alembic（5.3 显式决策：触发条件 = 第一次真实 schema 演进或首次真实部署；现用 `create_all`） | 同左 |
| **表内数据** | ⚠️ Runtime 主链路**当前不产生任何行**——schema ready、write path 缺失（8 张表中唯一没有读写路径的两张） | 同左 |

---

## 2. 问题 A：Interaction 字段是否足够？

**结论：足够。** 冻结语义逐一对照：

| 冻结语义 | ORM 列 | 判定 |
| --- | --- | --- |
| `processing_status` | `String(32) NOT NULL DEFAULT 'pending'` | ✅ 独立状态，与 delivery 不合成（「Agent 已成功 → 渠道投递失败」合法组合，模型 docstring + schema 测试双冻结） |
| `delivery_status` | `String(32) NOT NULL DEFAULT 'pending'` | ✅ |
| `failure_stage` | `String(32) NULL` | ✅ 与六段执行序对应（identity_resolution / routing / session_resolution / session_creation / agent_dispatch / delivery——候选名全部 ≤32 字符，审计快照 a3）；NULL = 未失败 |
| `failure_code` | `String(64) NULL` | ✅ 承载 typed error code——审计快照 a2 证明 application 层**全部** SCREAMING_SNAKE code ≤64 字符（IDENTITY_RESOLUTION_FAILED / ROUTING_FAILED / SESSION_RESOLUTION_FAILED / AGENT_DISPATCH_FAILED / AGENT_RESPONSE_INVALID / CHANNEL_DELIVERY_FAILED / ...） |
| `retryable` | `Boolean NOT NULL DEFAULT False` | ✅ |

额外确认：`external_interaction_id`（MsgId）列已存在——未来入站幂等去重的落点
已备好；`direction` 列已冻结（MVP 单一 interaction 行记录一次入站请求全生命
周期，direction='inbound'）。

**缺口（记录，不在本步修）**：状态枚举**取值集**（pending/running/succeeded/
failed；failure_stage 取值集）docs 无明文——留写路径 Step 以 domain 常量冻结
（DB 层保持 String + 应用层校验，与 5.3「Schema 只声明」一致）。

## 3. 问题 B：Activity 字段是否足够？

**结论：足够。**

| 需求 | ORM 列 | 判定 |
| --- | --- | --- |
| `stage` | `String(32) NULL` | ✅ |
| `status` | `String(32) NOT NULL 无默认` | ✅ 显式事实由写入方声明（不发明语义默认） |
| `message` | `Text NULL` | ✅ |
| `metadata` | JSON NULL（ORM 属性名 `extra_metadata`，SQLAlchemy 保留名约定） | ✅ |

加分项：`type`（String(64) NOT NULL）区分活动类型；`interaction_id` nullable
支持会话级活动；append-only（无 updated_at、ORM 不提供更新机制）符合
Evidence / Execution Record 定位——**不是 Audit Platform**。

## 4. 问题 C：Persistence 应该挂在哪个边界？（核心裁决）

### 4.1 候选排除

| 候选 | 判定 | 理由 |
| --- | --- | --- |
| **Runtime Coordinator 内** | ❌ | 9.8.2 冻结：不 import 任何 Repository / persistence，无状态、无 uow 注入；10.3.1 D 层守卫（import 精确白名单 + 无 agent-specific 分支）刚把它钉死。加持久化 = 直接破坏 |
| **六个 Executor 内各自写** | ❌ | 「执行 + 记账」职责混淆；失败记录恰恰发生在 executor **抛 typed error 的时刻**，自己记录自己的失败需要 try/except 包住自己；dispatch 失败后 delivery 不会跑——谁记录 delivery=pending/FAILED？职责碎片化、事务边界破碎 |
| **API 层（FastAPI endpoint）** | ❌ | Runtime 不止一个未来入口（webhook / Management API / 未来 Worker）；挂 API 层 = 第二个入口开始漏记 |

### 4.2 裁决：挂在 Runtime 调用边界（ChannelInboundService ↔ Coordinator 之间）

```
ChannelAdapter（Adapter 层）
      ↓ InboundInteraction
ChannelInboundService                    ← 持久化边界挂点
      ↓ ①  persistence-aware wrapper（实现 RuntimeExecutionCoordinator Protocol）
      ↓     · interaction 行：入站即建（pending/pending）
      ↓     · 成功：processing=SUCCEEDED + delivery=SUCCEEDED
      ↓     · typed error：映射 failure_stage / failure_code / retryable
      ↓       （六段 stage ↔ Runtime typed errors 的纯函数映射，归 application 层）
      ↓     · activity 追加：生命周期事件（append-only）
      ↓ ② 内部调用既有 RuntimeCoordinator（零改动、零感知）
RuntimeCoordinator → 六 Executor（不变）
```

**关键性质**：
- 实现形状 = `RuntimeExecutionCoordinator` Protocol 的**装饰器**——对调用方
  透明，未来出现第二个入口时提升为组合根通用组件；
- `RuntimeCoordinator` 与六 Executor **零改动**：10.3.1 的 Multi-Agent
  Isolation Guard 与 9.8.2 全部守卫继续成立；
- delivery 失败（dispatch 成功后 `CHANNEL_DELIVERY_FAILED`）自然落到
  `processing=succeeded + delivery=failed`——正是 4.2 冻结的合法组合；
- `retryable` 语义由错误类别决定（transport/timeout 类 vs 契约/配置类），
  映射表是显式纯函数，不做自动重试。

### 4.3 写路径 Step（下一 Step）需要新增的最小面

```
app/repositories/interaction.py        InteractionRepository Port（append/update 状态）
app/repositories/activity.py          ActivityRepository Port（append-only）
persistence/mappers/interaction.py    to_orm（Interaction 无 domain 实体，仅记录行？
                                       —— 写路径 Step 裁决：记录模型用行快照还是轻量 domain 对象）
persistence/repositories/interaction.py / activity.py
UoW Port + 实现                        扩 interactions / activities 两属性（审计快照 c3 随真实扩边更新）
application/interaction_recording.py   typed error → (stage, code, retryable) 纯函数 + wrapper
```

## 5. 验收

| 项 | 结果 |
| --- | --- |
| 审计快照测试 | 8 passed（a1-a3 / b1 / c1-c4） |
| 全量回归 | **771 passed**（763 + 8） |
| 生产代码改动 | **0 行** |
| Runtime 边界快照 | c1/c2：九个 Runtime 侧文件零 persistence 接触；c3：UoW 未被偷偷扩边；c4：domain 无 Interaction/Activity 实体 |
