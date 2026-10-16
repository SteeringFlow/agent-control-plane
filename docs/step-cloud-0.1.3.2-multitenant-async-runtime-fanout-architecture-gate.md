# Cloud-0.1.3.2 — Multi-Tenant Async Runtime / Worker Tenant Fan-out（Architecture Gate）

> 阶段：**Architecture Gate（仅裁决，不写实现代码）**。上游：Cloud-0.1.3.1 WeCom Webhook
> Tenant Resolution（CLOSED，入站接纳段已 tenant-scoped）。本步：把「单一 Runtime 组合 →
> per-tenant fan-out」的设计在写代码前裁决锁死。下游：真实多租户 WeCom Webhook 验证 →
> 0.1.3.2 Connect Channel Golden Path。
>
> 方法：只读审计 `Worker` / `Driver` / `PersistingCoordinator` / `RuntimeCoordinator` /
> 四个 persistence 原语 / `main.py` 组合根（证据见 §1）→ 对 10 个 fan-out 问题逐一裁决
> （§2）→ 冻结红线清单（§3，`test_cloud_0_1_3_2_runtime_tenant_fanout_gate.py` 机器化锁定）
> → follow-up（§4）→ **GO**（§5）。
>
> **中心命题**：0.1.3.1 让「入站接纳」tenant-scoped；0.1.3.2 要让「异步执行」——
> `find_pending → claim → Runtime.execute → record_outcome → reclaim` —— 同样 tenant-scoped。
> 关键判断（已用代码证实）：**fan-out 不是 Runtime execution logic 变化，而是 Composition
> 实例化方式的变化**。单一 Runtime 组合 + 每个 tenant 一份 tenant-scoped UoW，由组合根
> 编排 tick 即可；`Worker`/`Driver`/`Coordinator`/`Executor` 代码零改动。

---

## 1. 审计证据（真实代码，非推测）

| 事实 | 证据 | 推论 |
|---|---|---|
| 四个异步原语**已** tenant-scoped | `app/persistence/repositories/interaction_record.py`：`find_pending`(L223-230) / `claim_processing`(L53-67) / `record_outcome`(L115-127) / `reclaim_stale_processing`(L155-175) 均在 `self._tenant_id is not None` 时追加 `tenant_id == self._tenant_id` | fan-out 不需要改任何原语——它们只是被绑到不同的 tenant |
| **Attempt Fence 在存储层落地，与 tenant 无关** | `claim_processing` 是单条条件 `UPDATE ... WHERE InteractionModel.id == interaction_id AND processing_status='pending' [AND tenant_id=:tid]`（L53-67）；`record_outcome` 追加 `processing_status='processing' AND processing_started_at=:expected`（L115-127） | fence 唯一性由**全局 PK `id` + DB 时钟围栏值**保证，不经 tenant 参数；多 Driver/多 Runtime 下仍成立 |
| Worker 完全 tenant-blind | `app/application/worker.py:62` `def process(self, interaction_id: int)` —— 无 tenant 参数；内部只经 `self._uow_factory` / `self._phases` / `self._runtime` | tenant 只能由注入的 uow_factory 携带，不能进 `process` 签名 |
| Driver 无业务状态、不枚举 tenant | `app/application/driver.py`：`PendingSource` 仅 `find_pending` / `reclaim_stale_processing` 两方法（L44-57）；`run_once` 只 `source.find_pending(limit)` → `worker.process(record.id)`（L155-168）；`DriverConfig`（L60-76）只有节奏/批量 | 给 Driver 加 tenant 枚举 = 违反 12.9.4.16.3/16.7（偷偷造第二套状态机） |
| Coordinator 完全 tenant-blind | `app/application/runtime.py:99` `def execute(self, inbound: InboundInteraction)` —— 无 tenant 参数 | Runtime 契约零 tenant（B §14 已证） |
| 组合根已是 tenant 注入的唯一缝 | `app/main.py:72-73` `tenant_uow_factory = partial(SqlAlchemyUnitOfWork, tenant_id=TENANT_LOCAL)`（B §7 桥）；`:226,253` `Worker`/`PersistingCoordinator`/六 Executor 全部经 `uow_factory=runtime_uow_factory` 注入；`:300` `driver = ProductionDriver(source=phases, worker=worker, ...)` —— **无任何 tenant= 关键字** | 当前只实例化 1 份（TENANT_LOCAL）；fan-out = 同构造循环 N 次，租户经 uow_factory 流入，Runtime 内部零感知 |

**结论（B §8 末已预写）**：*"Worker 必须在 tenant-scoped UoW 下运行（Cloud 下按租户 fan-out：每租户一个处理循环）。Worker / Driver 的代码逻辑不变，仅其 `uow_factory` 被绑到具体 tenant。fan-out 具体实现细节留给实现期，但不变量写死：每次 `find_pending`/`claim`/`reclaim` 都在 `SET LOCAL app.tenant_id = 该行 tenant` 的事务内执行。"* —— 本 Gate 把"实现期的不变量"裁决为具体形态。

---

## 2. 十个 fan-out 问题裁决

### Q1. Driver 如何发现有哪些 tenant 需要处理？
**裁决：Tenant 枚举是组合根 / Provisioning 关注点，绝不是 Driver 关注点。**
Driver 永不枚举 tenant。组合根持有「活跃 tenant 集合」（OSS 下 = `{TENANT_LOCAL}`；Cloud 下取自 `tenants` 目录，经 bootstrap 无租户读，与 0.1.3.1 的 Channel 路由投影同机制），由一个**薄编排器**（新增，位于组合根）遍历该集合、tick 每个 tenant-Runtime。Driver 本身保持单租户：它调 `source.find_pending()`，而该 source 的 UoW 已绑 tenant，自然只回该租户行。这样 Driver 的冻结形状（只认 `PendingSource` + `worker`，12.9.4.16.7）不被破坏。
> 演进余地：编排器可周期性从 `tenants` 目录刷新集合，使新开通租户无需重启即被处理；但枚举永远在组合根，不在 Driver。

### Q2. 是否允许一个 Driver 进程 fan-out 全部 tenant？
**裁决：允许，且默认就是「一个进程 + 一个编排器 + N 个 tenant-Runtime」。** 但"fan-out 全部 tenant"的实现方式是**组合根持有 N 个 tenant-blind Runtime 实例**，而非把 Driver 变成 tenant-aware。进程拓扑（1 进程 vs N 进程）是部署关注点，与正确性正交——Attempt Fence 在任一种拓扑下都成立（见 Q6）。首版默认单进程单编排器（贴合"单一 Runtime 组合 → per-tenant fan-out"，也保留现有单 Driver 线程模型）；多进程只是同一组合代码的横向扩展，无需新机制。

### Q3. tenant 是否应该成为 Worker execution context？
**裁决：绝对不应该。** Tenant 不进 `Worker.process` 签名、不做成 Worker/Driver/Coordinator/Executor 的 execution-context 字段或参数。Tenant 纯粹驻留在 UoW/基础设施层（一个已绑 tenant 的 session 像 DB 连接一样是"环境"而非"参数"）。`Worker.process(interaction_id)` 保持不变，它拿到的已是 tenant-scoped UoW。这正是对 B §14「Runtime diff = ZERO semantic change」保证的兑现，也是本 Gate 最重要的红线。

### Q4. find_pending / reclaim / CAS 的租户边界如何保持？
**裁决：已经保持，零改动。** 证据见 §1 表格——四个原语在 `self._tenant_id is not None` 时**已**各自追加 `tenant_id == self._tenant_id`。因为每个 tenant-Runtime 使用 tenant-scoped UoW，这些原语天然只作用于该租户行。fan-out 只是"用不同 tenant binding 实例化同一套原语"，不变量由现有代码 + B §16.8 `test_async_paths_tenant_scoped` 已锁定。

### Q5. 一个 tenant 的异常是否可能阻塞其他 tenant？
**裁决：不会——前提是编排器对每个 tenant 的 tick 做隔离。** 现有 `Driver.run_once` 已逐条隔离 `worker.process` 异常（12.9.4.16.5，循环不死的纪律）；fan-out 新增的要求是**编排器层面对每个 tenant-Runtime 的 `run_once` 包 try/except**，使一个租户的失败不连累其他租户。这是**唯一需要新增的错误边界代码，且只落在组合根（编排器），不进 Runtime**（OSS 单租户 N=1 时无额外代码）。

### Q6. 多 tenant + 多 Driver 时 Attempt Fence 是否仍然成立？
**裁决：成立，可证明。** Fence 在存储层强制：`claim_processing` 是单条条件 `UPDATE WHERE id=:id AND status='pending' [AND tenant_id=:tid]`，`record_outcome` 再加 `processing_status='processing' AND processing_started_at=:expected`。围栏唯一性由 `(id, DB 围栏值)` 决定，**独立于存在多少个 Driver / Runtime**。两条不同进程"抢同一条 attempt"在物理上不可能——interaction 只属一个 tenant，而每个 tenant-Runtime 只看得见自己的行（RLS 第一/二层 + repo 过滤），跨租户行集天然不相交。⇒ 每租户的「恰好一次」与今日**逐字节同构**（同一份单租户组合的不同实例），跨租户笛卡尔积由行集不相交平凡隔离。test_step12_6 的「恰好一次」组合锁须重述为「per-tenant 恰好一次」——但因每个 tenant-Runtime 是同一单租户组合的忠实实例，证明直接继承。

### Q7. TENANT_LOCAL 在 Cloud Runtime 中最终应该消失在哪里、保留在哪里？
**裁决：**
- **消失于**：组合根里「把 `runtime_uow_factory = partial(UoW, tenant_id=TENANT_LOCAL)` 一路灌进整个 Runtime」的硬编码。必须被「按活跃 tenant 集合生成 per-tenant factory」取代。
- **保留为**：(a) **OSS / 单租户默认常数**——当只有 1 个租户时，编排器实例化恰好 1 个 tenant-Runtime（绑 TENANT_LOCAL），与今日逐字节一致；(b) **bootstrap 无租户读的回退**（0.1.3.1 的 Channel 路由投影、Q1 的 tenant 集合读取都发生在任何 tenant scope 之外）；(c) ORM/server 默认 `tenant_id=1`。
- **铁律**：TENANT_LOCAL 是**常数**，绝不是在 Cloud Runtime 逻辑里"流动"的 current-tenant。Worker 在 Cloud 下永远不会把 TENANT_LOCAL 当作"当前租户"——它只见到其 UoW 所绑的那个 tenant。

### Q8. 是否需要 tenant-aware Driver，还是 tenant-aware pickup？
**裁决：两者都不对——因为它们把 tenant 放错了层。** 正确形态 = **组合根 tenant-aware + Driver tenant-blind + UoW tenant-scoped**，三个同时成立，结果是 pickup 天然 tenant-aware 而 Runtime 零改动：
- 不要给 Driver 加 tenant 感知（违反其冻结形状 + 12.9.4.16）；
- 不要给 `find_pending` 加"跨租户返回 + 逐行解析 tenant"的形态（那会把 tenant 塞进 pickup 结果集 / Worker 参数）；
- 而是组合根创建 N 个 tenant-blind Runtime 实例，每个经其已绑 tenant 的 UoW **只 pickup 自己租户的行**。"pickup 是 tenant-aware"这一性质由 UoW binding 自然涌现，而非由 pickup 调用里的任何 tenant 参数。⇒ tenant-aware 组合根 + tenant-blind Driver + tenant-scoped UoW = 零 Runtime 改动即得目标性质。

### Q9. 如何保证 OSS 单租户 Runtime 完全不发生语义漂移？
**裁决：因为 fan-out 纯粹是组合根的一个循环。** OSS（恰好 1 个租户 = TENANT_LOCAL）下，编排器实例化**恰好 1 个** tenant-Runtime 并 tick 它 = 与今日单 Runtime 完全一致。`Worker`/`Driver`/`Coordinator`/`Executor`/`phases` 代码路径逐字节相同（无任何 `if tenant` 分支）。唯一新增代码是编排器循环，N=1 时即单轮循环、零开销。全量 **1908 项现有测试（单租户 SQLite）必须零改动保持绿**——这是硬 DoD。B §14 证明 #5 已保证单租户 = 恒等变换。

### Q10. 最重要的：哪些东西绝对不能为了多租户而进入 Runtime Domain？
**裁决（冻结红线，机器化锁定于 `test_cloud_0_1_3_2_runtime_tenant_fanout_gate.py`）：**
1. `tenant_id` / `TenantScope` **不得**作为任何 Runtime Domain/Application 代码的参数、属性、字段：`Worker.process(interaction_id)` 不变；`RuntimeCoordinator.execute(inbound)` 不变；六 Executor 不变；`PersistingCoordinator` 不变；`Driver` 不变；
2. `find_pending` / `claim_processing` / `record_outcome` / `reclaim_stale_processing` **不得**新增 tenant 参数——tenant 只来自 UoW binding，绝不来自传入参数；
3. **不得**给域实体加 `tenant_id` 字段：`InteractionRecord` / `Interaction` / `Attempt` / `Execution` / 任何 task/queue 实体保持 tenant-free（B §13.2 / §14）；
4. Runtime 语义**不得**出现任何对 tenant 的分支（无 `if tenant is X`、无 per-tenant 策略选择、无"遍历全部 tenant"的域/Driver 逻辑）；
5. **不得**新建第二套 TenantResolver / 隔离机制——复用 B 的 `TenantScope` + UoW 工厂桥；
6. 不修改 WeCom Auth / Browser Auth / Agent / Connection / Runtime semantics——与 0.1.3.1 冻结一致；
7. Tenant 只许存在于：(1) UoW/基础设施（session binding）；(2) 组合根 fan-out（tick 哪个 tenant-Runtime）；(3) bootstrap 路由投影（0.1.3.1）。

---

## 3. 冻结红线 / 机器化锁定（`test_cloud_0_1_3_2_runtime_tenant_fanout_gate.py`）

> 本 Gate 不写实现代码；只写**锁结构**的测试（对现有代码即绿，实现期若越线即红）。

| 锁 | 断言 | 对应裁决 |
|---|---|---|
| L1 Worker tenant-blind | `worker.py` 的 `process` 签名无 `tenant` 参数；模块不 import `TenantScope`/`tenant_id`；源码无 `tenant` 形参 | Q3 / Q10.1 |
| L2 Driver 无 tenant 枚举 | `driver.py`：`ProductionDriver`/`DriverConfig`/`PendingSource` 无 `tenant` 属性/参数；`run_once` 内 `find_pending`/`worker.process` 调用无 tenant 关键字；`DriverConfig` 字段仅节奏/批量 | Q1 / Q8 / Q10.4 |
| L3 Coordinator tenant-blind | `runtime.py` 的 `execute(self, inbound)` 无 tenant 参数 | Q3 / Q10.1 |
| L4 原语已 tenant-scoped | `interaction_record.py` 中 `find_pending`/`claim_processing`/`record_outcome`/`reclaim_stale_processing` 均在 `self._tenant_id is not None` 分支追加 `tenant_id == self._tenant_id`（AST 扫描） | Q4 |
| L5 Fence 与 tenant 无关 | `claim_processing` 以 `InteractionModel.id == interaction_id` 为 PK 锚（围栏唯一性由 id + DB 时钟值，不经 tenant 参数） | Q6 |
| L6 tenant 只经 uow_factory 注入 | `main.py`：构造 `Worker`/`PersistingCoordinator`/`RuntimeCoordinator`/六 `Executor`/`ProductionDriver` 均**无** `tenant=` 关键字；tenant 经 `partial(SqlAlchemyUnitOfWork, tenant_id=...)` 桥流入（B §7） | Q3 / Q7 / Q10.7 |
| L7 行为：per-tenant 隔离 | 内存 SQLite：两个 tenant-scoped UoW 各自 `find_pending` 仅见本租户行；`claim_processing` 跨租户互不影响 | Q4 / Q6 |
| L8 无隐式全局租户上下文 | `app/application/{worker,driver,runtime,interaction_recording}.py` 不出现 `contextvars` / 模块级 tenant 全局（复用 B §16 `test_no_implicit_global_tenant_context`） | Q10.7 |

---

## 4. Follow-up（不阻塞 GO，均为实现期落地项）

| # | 项 | 说明 |
|---|---|---|
| F1 | 组合根编排器（per-tenant 实例化 + tick 循环） | 唯一新增的生产代码，且只落在 `main.py`（组合根）；OSS N=1 时退化成今日单 Runtime。**0.1.3.3 实现修订**：落地为 B §19.3 已冻结契约 `TenantFanoutDriver(tenant_source, driver_factory)`（每租户各一套 Driver 线程，start/stop 对称）——与"单编排器 tick"满足同一组不变量（忠实实例 + per-tenant 隔离 + 组合根生命周期），且遵守更早冻结的 B 契约规格测试 |
| F2 | 每 tenant-Runtime 的 `run_once` 编排器层 try/except 隔离 | 满足 Q5，仅组合根错误边界。**实现修订**：per-tenant 独立 Driver 线程 + Driver 自身异常纪律（12.9.4.16.5），隔离是结构性的 |
| F3 | tenant 集合来源（启动枚举 / 周期刷新 `tenants` 目录） | 组合根关注点，bootstrap 无租户读（与 0.1.3.1 同机制）。**实现修订**：lifespan 启动时枚举（`uow.tenants.list_active_ids`）；运行期新租户先由 Trigger 1（webhook 的惰性 Worker 构建）覆盖，durable Driver 于重启后补全 |
| F4 | test_step12_6 组合锁重述为「per-tenant 恰好一次」 | 因实例化 N 份，组合锁的"单一 Runtime"断言需表达为"每个 tenant-Runtime 是同一组合忠实实例"（构造点恰好一次） |
| F5 | PG RLS bootstrap 读 | `find_pending`/tenant 枚举在 PG 上受 RLS 约束（fail-closed），按 B §9 备选 `SECURITY DEFINER` 或 bootstrap 分支 |
| F6 | 可选：进程拓扑扩展（N 进程） | 同组合代码横向扩展，无新机制；首版不做 |

---

## 5. GO

**GO。** 中心命题已裁决锁死：**fan-out = 同一份 Runtime 组合按 tenant 实例化 N 次，每份绑 tenant-scoped UoW，由组合根薄编排器 tick**；Runtime Domain（Worker/Driver/Coordinator/六 Executor/PersistingCoordinator）保持**证明级 tenant-blind**（§1 代码实证 + §3 机器锁定）。

因此：
- **Attempt Fence / 恰好一次** 由存储层 PK + DB 时钟围栏保证，跨多 tenant + 多 Driver 仍成立（Q6）；
- **OSS 单租户零语义漂移** 由"fan-out 是组合根循环、N=1 时退化为今日单 Runtime"保证（Q9），1908 项现有测试硬 DoD；
- **Tenant 不进任何 Runtime 域** 由 §3 的 8 条机器锁守住（Q10）；
- WeCom Auth / Browser Auth / Agent / Connection / Runtime semantics 全部不触碰（Q10.6，与 0.1.3.1 一致）。

唯一新增生产代码是组合根的**编排器**（F1/F2/F3），不进 Runtime。下一步进入"真实多租户 WeCom Webhook 验证"——把本 Gate 的 per-tenant 实例化落到 `main.py`，并跑通「PENDING → Worker → Agent → AgentResponse → Delivery」跨租户闭环。
