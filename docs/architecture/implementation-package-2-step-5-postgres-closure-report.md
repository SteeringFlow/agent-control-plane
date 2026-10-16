# Package 2 / Step 5 — PostgreSQL Closure

> 状态：**完成**。目标是把两条线重新汇合——**「测试通过」与「Cloud V0 数据库证据通过」**。
> 环境：一次性 `postgres:16-alpine`（本机 Docker，`127.0.0.1:55432`，DSN
> `postgresql+psycopg://acp:acp@127.0.0.1:55432/acp`）——**专用可重置库**。

---

## 0. 证据总览（同一 DSN、同一次运行）

| # | 证据线 | 结果 |
|---|---|---|
| 1 | `closure_evidence.py`（A/B/C） | **OVERALL PASS**（修正 6 处 stale 假设后） |
| 2 | PG-gated pytest（全量套件 + 真库） | **2224 passed / 2 skipped / 1 failed** |
| 3 | Migration inspection（链拓扑 + 逐版本） | PASS（测试内） |
| 4 | RLS probe（非超级用户角色实测） | PASS（测试 + closure_evidence 双线） |
| 5 | 最终数据库状态检查 | head `0007` · 13 表 · 10 表 ENABLE+FORCE RLS · 11 策略 · grants=0 |

唯一 failed = `WIP-3B3-3-WS-TIMING`（已登记的 3B-3.3 时序 flaky；本次轮换到
`test_3b_3_3::c3`，隔离复现通过，与本步无关）。
2 skipped = **真实外部服务**门控（`TENCENT_ACP_URL` / `TENCENT_ACP_TOKEN` /
`TENCENT_RUNTIME_ID`），非 PG 相关。

## 1. Migration（真库）

```
0001 → 0002 → 0003 → 0004 → 0005 → 0006 → 0007     单 head · linear · 无 merge
alembic_version = 0007                              upgrade 成功（7 步全绿）
downgrade 0006 → 表与 supporting UNIQUE 均被丢弃    再 upgrade head → 表复活
                 但授权行**不复活**（无对应 Session，不可回填）
                 ↳ 这正是 0007 docstring 登记的「有损」限制
```

## 2. Schema：冻结 DDL 逐项符合

```
列             id / tenant_id / channel_id / external_conversation_id /
               connection_id / created_at / updated_at   （全 NOT NULL，除 id）
禁列           status / effect / priority / agent_id / identity_id / user_id / decision
UNIQUE         uq_connection_conversations_tenant_channel_conv_conn（四元组）
复合 FK        (tenant_id, channel_id, connection_id) → connections(tenant_id, channel_id, id)
               **无 ondelete**（NO ACTION，阻断式；不 cascade）
索引           ix_connection_conversations_tenant_id / _connection_id
supporting     uq_connections_tenant_channel_id（复合 FK 的合法 target）
```

## 3. RLS（真库 + 非超级用户角色）

```
connection_conversations   relrowsecurity = t · relforcerowsecurity = t
策略 tenant_isolation      USING + WITH CHECK 均为 NULLIF(current_setting(...),'') 形态
隔离实测（acp_closure_app 角色）：
    tenant 1 上下文 → 只见 ev_tenantA（看不到 tenant 2 的行）
    tenant 2 上下文 → 只见 ev_tenantB（反向同样不泄漏）
    未设上下文     → 0 行（fail-closed）
```

## 4. Grant integrity（DB 层拒绝，不依赖应用层自觉）

| 场景 | 结果 |
|---|---|
| wrong tenant（跨租户） | IntegrityError（复合 FK） |
| wrong channel（同租户跨 channel） | IntegrityError（复合 FK） |
| wrong connection（不存在） | IntegrityError（FK） |
| **inactive connection** | **允许**（授权与可运维性正交，B3 冻结）→ 后果是 `ROUTING_FAILED` 而非 `UNAUTHORIZED` |
| duplicate grant（同一四元组） | IntegrityError（UNIQUE） |

## 5. Backfill（0006 → seed sessions → 0007）

真实执行路径：`reset → upgrade 0006 → 播种历史 Session → upgrade head → 校验 grants`

| 语义 | 验证结果 |
|---|---|
| 群聊**多成员**同一会话 | 源侧 2 行（不同 identity）→ **收敛为 1 条授权** |
| 多 Connection（历史迁移） | 同一会话 → **2 条授权**（`G1→active` + `G1→idle`） |
| **inactive Connection** | **照常回填**（不按 status 过滤） |
| NULL / 空白会话键 | 跳过（`NULLIF(TRIM(...))`） |
| 无 connection 的 Session | 跳过 |
| **绝不静默 0 行** | 迁移内每租户自检 `期望(DISTINCT eligible) == 实得`；不一致即 raise。测试断言 `3 == expected` 且非空 |
| 空库 / 新部署 | 回填 no-op → 0 授权（ADR-6 正确行为，**不得**用通配授权掩盖） |

**顺带发现的一条真实约束**：`sessions` 的 `UNIQUE(tenant, identity, connection, conversation)`
意味着「同一会话多行」只能由**不同成员**产生——而群聊的形态正是如此。回填的
DISTINCT 因此有真实的收敛对象（不是假想场景）。

## 6. Runtime（真库）

```
Identity.user_id = NULL + 有效 grant  →  全链 ALLOW（Resolution→Authz→Routing→
                                          Session→Dispatch→Delivery），且授权通过后
                                          身份**仍不**被补建绑定
no grant                              →  AccessAuthorizationDeniedError
                                          + 冻结五元组落库：
                                          failed / pending / authorization /
                                          UNAUTHORIZED / retryable=false /
                                          session_id=NULL / response_payload=NULL
```

## 7. Management API（真库）

```
POST   → 201；重复 → 409
GET    → 200（仅该 connection 的显式授权）
DELETE → 204；再删 → 404
GET known-conversations → 200（Session 出现即可见）
```

**核心红线（真库）**：会话已出现在 known-conversations 中，但**没有** grant →
Runtime 仍 `UNAUTHORIZED`，且授权表保持 0 行（Discovery 不产生授权状态）。

## 8. `tools/closure_evidence.py`：6 处 stale 修正（**只对齐 schema 演进，不新增口径**）

Step 4 登记了 3 处；实跑后确认共 **6 处**，全部是「假设与当前 Cloud V0 不符」：

| # | 位置 | 原假设 | 实况（head 0007） |
|---|---|---|---|
| 1 | `evidence_a` head | 硬编码 `rev == "0002"` | 改为**从迁移目录动态取 head**（同一修法见 `test_cloud_0_1_1_a`）——硬编码的 evidence 会长期静默过期 |
| 2 | `evidence_a` 表集合 | 8 业务表 + tenants | + `user_credentials` / `browser_sessions`（0003）/ `connection_conversations`（0007） |
| 3 | `evidence_a` RLS 集合 | 「8 业务表 + tenants 均有 FORCE RLS」 | RLS 集合 = 8 业务表 + tenants + `connection_conversations`（10）；并**反向断言** Browser Auth 两表**刻意无 RLS**（auth 边界表，非租户业务数据） |
| 4 | `evidence_a` 策略数 | `n_pol == 9` | `== len(RLS_TABLES) + 1`（= 11：RLS 表各 1 + channels_bootstrap） |
| 5 | **`evidence_c` unset 上下文** | 期望**抛 DataError**（0002 形态 `''::integer`） | 0005 起改为 `NULLIF(current_setting(...,true), '')` → 未设时谓词为 NULL → **0 行**（不抛错）。两种形态都 fail-closed，但**可观测不同**——旧断言在 head 上必然失败 |
| 6 | **`evidence_c` 隔离断言** | `count == 1`（绝对行数） | 改为**「看得见哪些行」**：`ev_tenantB` 不在 A 的可见集、`ev_tenantA` 不在 B 的可见集。绝对行数是对**环境**的假设（库里可能有其它合法数据），不是对 RLS 的断言 |

第 5、6 条**不在 Step 4 的登记清单里**——它们只有真正把脚本对着 head 跑一次才会
暴露。这正是「不要只跑 pytest」的价值：`closure_evidence.py` 是**唯一**一条不经过
测试套件、直接对数据库断言证据的线，因此它的过期不会被任何 pytest 结果掩盖。

## 9. 最终状态判定

```
Migration 0001→0007 · 单 head · linear          ✅ PASS
Schema 冻结 DDL（含复合 FK · 无 ondelete）        ✅ PASS
RLS ENABLE + FORCE + 租户隔离实测                ✅ PASS
Grant integrity（5 类）                          ✅ PASS
Backfill（6 条语义 + 不静默 0 行）                ✅ PASS
Runtime ALLOW / UNAUTHORIZED（真库）             ✅ PASS
Management API + known ≠ authorized（真库）      ✅ PASS
closure_evidence.py                              ✅ OVERALL PASS
最终 DB 状态检查                                  ✅ head 0007 · 13 表 · 10 RLS · 11 策略
```

## 10. 环境说明（可清理）

本次使用的一次性容器：

```powershell
docker rm -f acp-pg-step5      # 用完即弃；数据不保留
```

（镜像 `postgres:16-alpine` 本机已有；`postgres:16` 因网络中断未拉取成功，未使用。）

## 11. 路线图

```
Step 5  PostgreSQL Closure      ✅ 本步
Step 6  Full Regression         ⏳ 下一步（SQLite + PG 双线已各跑过一次）
Step 7  Real WeCom E2E          ⏳ Package 2 最终验收
```
