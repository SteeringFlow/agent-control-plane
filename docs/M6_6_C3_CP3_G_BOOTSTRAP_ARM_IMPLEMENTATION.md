# M6.6-C3-CP3-G — Bootstrap Arm Implementation Report

> 性质：**实现报告**（代码 / 迁移 / 测试已落地；本文件是记录与 Gate 自评）。
> 上游：ADR-12（语义授权）、ADR-11（补签）、
> 　　　`docs/M6_6_C3_CP3_D_…`（O-1~O-5）、`docs/M6_6_C3_CP3_E_GUARD_CHANGE_MANIFEST.md`、
> 　　　`docs/M6_6_C3_CP3_F_…`（实现设计）
>
> **结论：PASS**（PostgreSQL 闭环已由 `docs/M6_6_C3_CP3_G_PG_CLOSURE_AUDIT.md` 闭合）。
> SQLite 全量回归 **2306 passed / 34 skipped / 0 failed**；
> 启用 PostgreSQL（临时容器）后全量 **2338 passed / 6 skipped / 0 failed**，
> 官方数据库证据 A/B/C **全 PASS**；
> CP3-G §7.1 遗留的 **G1 / G2 / G3 已闭合**（实现代码零改动）。

---

## 1. 交付清单（逐项映射到 CP3-F）

| CP3-F # | 交付物 | 落地位置 |
|---|---|---|
| 1 | ORM 模型 + 部分唯一索引 | `app/persistence/models/bootstrap_arm.py` |
| 2 | Mapper | `app/persistence/mappers/bootstrap_arm.py` |
| 3 | Repository Port + Adapter | `app/repositories/bootstrap_arm.py` / `app/persistence/repositories/bootstrap_arm.py` |
| 4 | Domain 值对象 | `app/domain/bootstrap_arm.py` |
| 5 | 迁移 0008 | `alembic/versions/0008_bootstrap_arms.py`（`alembic heads` = **0008**） |
| 6 | UoW 暴露 | `app/persistence/unit_of_work.py` + `app/repositories/unit_of_work.py` |
| 7 | Application Service | `app/application/bootstrap_arms.py` |
| 8 | 端点 | `app/api/management/bootstrap_arms.py` + `app/main.py` 装配 |
| 9 | `AuthenticatedUser` 依赖 | `app/api/auth.py::build_authenticated_user_provider` |
| 10 | Bootstrap 组件 | `app/application/bootstrap.py` |
| 11 | Worker 注入 | `app/application/worker.py`（1 行）+ `app/main.py`（1 行） |
| 12 | Adapter `chat_type` | `wecom_bot.py` / `wecom.py` / `local.py` 各 1 处 |
| 13 | 守卫增补 / 升级 | 见 §4 |
| 14 | 测试 | 见 §5 |

**新增能力面（全部经 ADR-12 授权，无额外扩张）**：
`bootstrap_arms` 表 · `bootstrap.py` · `bootstrap_arms.py`（app + api）· `ChatType` 三个常量 ·
`find_or_create`（Identity / Grant 各一个幂等方法）。

---

## 2. 关键实现要点

### 2.1 DB 为权威（O-1）

```python
Index("uq_bootstrap_arms_scope_armed",
      "tenant_id", "channel_id", "connection_id",
      unique=True,
      postgresql_where=text("status = 'armed'"),
      sqlite_where=text("status = 'armed'"))
```

- 同一 scope 至多一条 `armed` 由 **DB** 保证；应用层 supersede（revoke → insert）
  只塑形语义，撞约束时**重试一次**。
- `_revoke_active` **刻意不过滤 `expires_at`**：部分索引覆盖 `status='armed'`
  与过期无关；只 revoke 未过期行会让已过期的旧行永久阻塞新 Arm
  （实现期发现，有专门回归用例 `test_supersede_after_expiry_still_works`）。

### 2.2 时间判据一律 DB 时钟

- `expires_at` = `datetime('now','+N seconds')`（SQLite）/ `now() + interval`（PG）；
- 过期与消费写在**同一条 SQL**（`WHERE … AND expires_at > now()`）⇒ 无 TOCTOU；
- 负间隔显式带符号（`f"{seconds:+d} seconds"`），供「一出生即过期」的测试使用。

### 2.3 原子建立（§15）

```
BEGIN
  CAS consume  → find_or_create Identity(user_id=None) → find_or_create Grant
COMMIT
```
- 二者冲突用 **SAVEPOINT**（`begin_nested`）消化后**回读复用**；
  整体 rollback 会把已消费的 Arm 退回，破坏 one-shot，故不可用。
- CAS 输家：**回滚并关闭事务** → 新事务回读赢家成果 → 命中则 ALREADY；
  未命中则重试 CAS 一次；仍不适用才回落既有 `IDENTITY_RESOLUTION_FAILED`。

### 2.4 三条 Trigger 统一（§12）

```
HTTP  → main.py:384-385 ┐
WS    → main.py:395     ├─ Worker.process() ─ bootstrap.ensure()
Driver→ main.py:354-357 ┘
```
Bootstrap 只在 `Worker.process`（claim 之后、execute 之前）注入一次；
`bootstrap=None` 时行为与注入前一致（有回归用例）。

### 2.5 端点（§8~§9）

- 依赖 `AuthenticatedUser`（**不回退** TENANT_LOCAL）⇒ 无会话 = **401**；
- 无请求体 ⇒ 调用方无法声明 tenant / channel / initiator；
- 不校验 `Connection.status`（对齐既有 Grant 创建）⇒ **未新增任何错误类**；
- 重复 Verify = supersede，返回 **201** + DB 生成的 `expires_at`；
- 响应不暴露 Identity / Grant / 授权词汇。

### 2.6 群聊（§14）

`ensure()` 早期判定 `metadata["chat_type"] == "single"`；group / 缺失 ⇒
**不消费、不创建、不报错**（Arm 保持 `armed`）。群聊既有 Grant 路径不受影响
（有专门回归用例）。

---

## 3. 未做的事（范围纪律）

```
❌ UI（Flow Home / Connect / Workspace / Agent / Channel / Identity / Grant / Interaction）
❌ Authorization policy engine / RBAC / challenge / Group Bootstrap / ACP User provisioning
❌ Router / AgentDispatcher / Session domain 语义
❌ Interaction stage enum / failure code taxonomy
❌ 8.1 InboundInteraction 六字段契约 / snapshot schema
❌ 新配置项（TTL 是代码常量 BOOTSTRAP_ARM_TTL_SECONDS）
❌ RUNTIME_EXECUTION_ORDER（一处未改）
```

---

## 4. 守卫变更（增补型，无删改）

### 4.1 CP3-E 清单已列出的

| # | 位置 | 变更 |
|---|---|---|
| G-A | `test_cloud_0_1_1_b_tenant_isolation_dod.py` | 表集合 + 新表的 tenant 三要件断言 |
| G-B | `test_cloud_0_1_1_a_postgres_alembic_foundation.py` | PG 表集合增补 |
| G-C | `test_step8_2_identity_resolution_contract.py` | `EXPECTED_APPLICATION_FILES` + `EXPECTED_IDENTITY_PORT_METHODS` |
| G-D | `test_adr_authorization_and_access_model.py` | **语义升级**：固定 4 文件 ⇒ 全仓扫描 + 允许清单（Identity / Grant 各一份），并新增「Bootstrap 绝不建 ACP User」 |
| G-E | `test_package_2_step_4_authorization_contract_closure.py` | 授权行创建点 = `["bootstrap.py", "connection_conversations.py"]` |
| G-F | `RUNTIME_EXECUTION_ORDER` 全部精确断言 | **零变更** |
| G-G | `test_adr6_no_implicit_grant_on_first_message` | **零变更** |
| G-I | `test_m6_6_c3_cp3_g_bootstrap_arm_persistence.py` | 新增：索引形状（tenant 前导 + 双方言 where）+ SQLite/Gated-PG「第二条 ARMED 必失败」+ revoke→new armed 成功 |
| G-J | `test_package_2_step_4…` conftest / autouse 扫描 | 扩展到 `bootstrap_arms` |

### 4.2 全量回归额外暴露的精确守卫（同样只增补）

`test_database.py` · `test_models.py`（计数 12→13）· `test_step12_9_4_12_attempt_fence.py` ·
`test_step14_2_2_fresh_sqlite_schema_init.py` · `test_step5_7_closure.py`（目录文件面 / 表集合 / Port 面）·
`test_external_identity_decoupling_package_1.py`（迁移 head 0007→0008）·
`test_step6_1_application_boundary.py` · `test_step6_5_management_consistency.py` ·
`test_step6_7_management_closure.py` · `test_step7_1_connection_domain_contract.py` ·
`test_step7_3_connection_management_audit.py` · `test_step8_A_runtime_closure.py`（`inbound_interaction.py` 符号面）·
`test_step9_2_identity_execution.py` · `test_step10_6_1_agent_selection_semantics_audit.py` ·
`test_step11_6 / test_step11_7` · `test_step12_6_runtime_production_composition.py`（Worker kwarg + bootstrap）·
`test_unit_of_work.py`（UoW 属性面）· `test_persistence_smoke.py` ·
`test_package_2_step_1_authorization_persistence.py`（Grant Port 面）·
`test_3b_2_2 / test_3b_3_3 / test_step12_8_2 / test_step15_2_3`（Adapter metadata 与 outbox 面）。

> 全部为「把新事实加入既有精确集合」，**没有**放宽任何一条既有断言的语义。

---

## 5. 测试

新增（CP3-G 定向）：

| 文件 | 覆盖 |
|---|---|
| `test_m6_6_c3_cp3_g_bootstrap_arm_persistence.py` | 建表 / scope / supersede / 过期 / CAS / 追溯字段 / G-I（11 passed + 1 PG-gated skip） |
| `test_m6_6_c3_cp3_g_bootstrap_endpoint.py` | 401（无 cookie / 坏 cookie）/ 201 / 请求体不可声明 scope / supersede / 跨租户 404 / initiator NOT NULL（10 passed） |
| `test_m6_6_c3_cp3_g_bootstrap_runtime.py` | 未 Arm 等价 / 原子建立 / user_id=NULL / 不建 ACP User / 复用 / 群聊拒绝 / 群聊既有 Grant 仍可用 / 三 Adapter chat_type / 并发 / CAS 输家 / 重放 / 过期 / Worker 唯一注入点（16 passed） |

---

## 6. Gate 自评（G1~G20）

| # | 项 | 结论 | 依据 |
|---|---|---|---|
| G1 | 0008 migration | ✅ | PG 真实 `upgrade head` + `pg_indexes` 定义断言（见 PG 闭环审计） |
| G2 | PG / SQLite parity | ✅ | 双方言各自实跑同一组不变量 |
| G3 | RLS | ✅ | 非 superuser 角色实测：跨租户不可见 + unset 上下文 fail-closed |
| G4 | 部分唯一索引 DB 强制 | ✅ | SQLite **与 PG** 实测「第二条 ARMED 失败」 |
| G5 | 端点认证 | ✅ | 401 / 201 / initiator NOT NULL |
| G6 | supersede | ✅ | 旧 revoked + 新 armed；至多一条 armed（双方言） |
| G5 | 端点认证 | ✅ | 401 / 201 / initiator NOT NULL |
| G6 | supersede | ✅ | 旧 revoked + 新 armed；至多一条 armed |
| G7 | expiry | ✅ | 过期不消费、不写状态、不新码 |
| G8 | group rejection | ✅ | 不消费 / 不建 Identity / 不建 Grant / Arm 保持 armed |
| G9 | Identity `user_id=NULL` | ✅ | 且 `user_id != admin`；users 计数不增 |
| G10 | Identity + Grant 原子性 | ✅ | 同事务；回滚 ⇒ Arm 回到 armed |
| G11 | CAS winner/loser | ✅ | 赢家唯一；输家回读复用（不回落失败） |
| G12 | replay | ✅ | claim 失败 ⇒ 不执行；计数不变 |
| G13 | 三 Adapter `chat_type` | ✅ | wecom_bot(实际) / wecom(single) / local(single) |
| G14 | Worker-only injection | ✅ | 静态断言 + 未注入时行为一致 |
| G15 | unarmed path unchanged | ✅ | `IDENTITY_RESOLUTION_FAILED`（既有失败码） |
| G16 | G-D 全仓守卫 | ✅ | 允许清单外零创建 |
| G17 | G-I 索引守卫 | ✅（SQLite）/ gated（PG） | |
| G18 | targeted tests | ✅ | 37 passed |
| G19 | full regression | ✅ | **2306 passed / 34 skipped / 0 failed** |
| G20 | scope integrity | ✅ | 无 UI / 无新配置 / 无新 stage / 无新 failure_code / 无新授权抽象 |

---

## 7. Final Gate

```
PASS
```

### 7.1 PostgreSQL 闭环（原 CONDITIONAL 的唯一来源，现已闭合）

CP3-F §22.2 的 **C-1** 要求「PG + SQLite 双方言 parity 测试全绿」。
本次以本机 Docker 起临时 PostgreSQL（`postgres:17-alpine`，`127.0.0.1:55432`，
验证后已删除）完成闭环：

```
G1  0008 migration        → alembic upgrade head 真实执行 + pg_indexes 定义断言   ✅
G2  PG / SQLite parity    → 同一组不变量双方言各自实跑                            ✅
G3  RLS                   → 非 superuser 角色：跨租户不可见 + unset 上下文为 0 行  ✅
G-I partial unique index  → SQLite + PG 双向「第二条 ARMED 必失败」               ✅
官方证据 tools/closure_evidence.py → A / B / C 全 PASS（bootstrap_arms RLS=True,True）✅
全量回归（PG 启用）        → 2338 passed / 6 skipped / 0 failed                    ✅
```

**实现代码零改动**通过 PG 验证（本轮只修了 2 处**守卫硬编码 head**）。

完整证据与命令见 → `docs/M6_6_C3_CP3_G_PG_CLOSURE_AUDIT.md`。

### 7.2 已知、且**不在本轮范围**的相邻缺陷

并发首条消息中，第二条可能落 `SESSION_RESOLUTION_FAILED` —— 这是**既有 M3 缺陷**
（Session 唯一约束冲突未回读复用），CP-1 §16.1 已将其归入 **CP-4-1**。
Bootstrap 侧不变量（至多一次授权建立）已由 `test_concurrent_first_messages_same_conversation`
守护；M3 的修复不在 CP3-G 范围，已在该用例中显式登记说明。

### 7.3 排期纪律

本实现**不**借用已关闭的 `RB-C3-01`（Bootstrap Authorization Source）自动升级为
Cloud V0 blocker；其排期仍按 `docs/cloud-v0-release-blocker-bootstrap-authorization-source.md`
§4.2 处理：默认 post-release backlog，入 V0 需**独立的** blocker 裁定。

---

## 8. 本轮未做

```
❌ 未改任何 UI 文件
❌ 未新增配置项 / Runtime stage / failure_code / Evidence type
❌ 未新增 Authorization 抽象
❌ 未 commit / tag（变更仅在工作区）
```
