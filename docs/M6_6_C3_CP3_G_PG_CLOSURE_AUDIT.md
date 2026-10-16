# M6.6-C3-CP3-G-PG — PostgreSQL Closure Verification（闭环审计）

> 性质：**验证 Gate + 只读闭环审计**。不是新开发阶段。
> 范围：临时 PostgreSQL → 执行 0008 → 跑 CP3-G persistence / PostgreSQL foundation →
> 验证 partial unique index → 验证 RLS → 全绿则只读收口。
> **未做 UI，未做 CP4。**
>
> **结论：PASS。**
> CP3-G §7.1 未闭合的 **G1 / G2 / G3** 全部闭合；
> 官方数据库证据脚本三段 **A/B/C 全 PASS**；
> 全量回归（含全部 PG-gated 用例）**2338 passed / 6 skipped / 0 failed**。

---

## 1. 环境（本机 Docker Desktop，一次性）

```
镜像      postgres:17-alpine
容器      acp-cp3g-pg（临时，验证后已删除）
地址      127.0.0.1:55432 → 5432     （刻意避开既有 5432 上的 fixture-pg）
凭据      acp / acp，库 acp（POSTGRES_USER = superuser，仅用于迁移与 seed）
DSN       CLOUD_TEST_DATABASE_URL=postgresql+psycopg://acp:acp@127.0.0.1:55432/acp
```

**未触碰任何既存容器**（`fixture-pg` / `wgtea-ceo-v2-*` / `tmp-live-clone-*` 全程保持运行）。
验证完成后容器已 `docker rm -f`，`backend/.pytest_tmp` 已清理。

---

## 2. 执行记录与证据

### 2.1 CP3-G 持久化（含 G1 / G2 / G3 / G-I 的 PG 侧）

```
$ CLOUD_TEST_DATABASE_URL=… python -m pytest \
      tests/test_m6_6_c3_cp3_g_bootstrap_arm_persistence.py -q
16 passed
```

PG 用例（新增/升级后全部实跑，不再是 skip）：

| 用例 | 验证 | 结果 |
|---|---|---|
| `test_g1_migration_0008_creates_table_and_partial_index_postgres` | `alembic upgrade head` 真实执行；表存在；`pg_indexes.indexdef` 含 `UNIQUE INDEX` 且列序 tenant→channel→connection，**且含 `WHERE … armed`**；`pg_class.relrowsecurity/relforcerowsecurity` 双 True | ✅ |
| `test_g_i_second_armed_same_scope_fails_postgres` | 同 scope 第二条 ARMED → `IntegrityError`（DB 拒绝） | ✅ |
| `test_g_i_revoked_then_new_armed_succeeds_postgres` | REVOKED 旧行不阻止新 ARMED（supersede 可用） | ✅ |
| `test_g3_rls_isolation_and_fail_closed_postgres` | **非 superuser 角色**实测：本租户 1 行 / 其它租户 0 行 / **未 SET LOCAL 0 行**（fail-closed）；Repository 显式过滤同向 | ✅ |
| `test_expired_and_consume_semantics_parity_postgres` | 过期谓词 + CAS 赢家/输家 + 追溯字段，与 SQLite 逐条一致 | ✅ |

### 2.2 PostgreSQL foundation / tenant isolation DoD / Package 2 Step 5

```
$ python -m pytest tests/test_cloud_0_1_1_a_postgres_alembic_foundation.py \
                 tests/test_cloud_0_1_1_b_tenant_isolation_dod.py \
                 tests/test_package_2_step_5_postgres_closure.py \
                 tests/test_m6_6_c3_cp3_g_bootstrap_arm_persistence.py -q
55 passed
```

### 2.3 官方数据库证据（`tools/closure_evidence.py`）

```
=== Evidence A: Migration 0001 → head ===   PASS
  alembic head = 0008（动态取自迁移目录）
  表集合 = 8 业务表 + tenants + alembic_version + browser_auth(2)
           + connection_conversations + bootstrap_arms          diff = set()
  ENABLE+FORCE RLS：… 'connection_conversations': (True, True),
                       'bootstrap_arms': (True, True)           ✅ 新表已纳入
  RLS policies 已建 = 12（RLS 表 11 + channels_bootstrap）
=== Evidence B: SET LOCAL 事务级隔离 ===     PASS
  Txn A 上下文 = '1'；Txn B 不继承 A 的上下文（已清理）
=== Evidence C: RLS 跨租户隔离（非 superuser 应用角色）===  PASS
  Tenant A 看不到 Tenant B；反向亦然；unset tenant 上下文 ⇒ 0 行（fail-closed）
  unset 上下文可读 tenants（bootstrap 策略）

OVERALL: PASS
```

> `tools/closure_evidence.py` 已同步把 `bootstrap_arms` 纳入
> `BOOTSTRAP_TABLES` → `RLS_TABLES` 与表集合期望（**增补**，不放宽任何既有断言）。
> 该工具同时是「0008 的 RLS 必须在**真实 PG** 上可见」的长期守卫。

### 2.4 全量回归（PG 已启用）

```
$ CLOUD_TEST_DATABASE_URL=… python -m pytest -q
2338 passed, 6 skipped
（对照：无 PG 时 = 2306 passed, 34 skipped ⇒ 28 条 PG-gated 用例转实跑并全绿）
```

剩余 6 条 skip **全部为外部凭据/环境门控，与 CP3-G 无关**：

| 数量 | 门控 | 说明 |
|---|---|---|
| 4 | `ACP_REAL_WECOM_BOT_ID/SECRET` | 真实 WeCom E2E（需真凭据） |
| 1 | `ACP_TEST_PG_URL` | WeCom RLS bootstrap 回归第二层 —— **已单独补跑：5 passed** |
| 1 | 内部源仓库不可用时跳过 | 历史 evidence BASELINE-001 baseline |

---

## 3. G1 / G2 / G3 闭合判定

| CP3-G Gate | 原状态 | 现状态 | 证据 |
|---|---|---|---|
| **G1** 0008 migration | ⚠️ 未闭合 | ✅ **PASS** | `alembic upgrade head` 真实执行 + `pg_indexes` 定义断言 + Evidence A |
| **G2** PG / SQLite parity | ⚠️ 部分 | ✅ **PASS** | 同一组不变量（第二条 ARMED 失败 / supersede / 过期 / CAS）双方言各自实跑 |
| **G3** RLS | ⚠️ 未执行 | ✅ **PASS** | 非 superuser 角色：跨租户不可见 + unset 上下文 fail-closed + `relforcerowsecurity=True` |

---

## 4. 本轮发现并修复的问题（2 处，均为守卫增补，非语义放宽）

| # | 位置 | 问题 | 处置 |
|---|---|---|---|
| 1 | `tests/test_package_2_step_5_postgres_closure.py::test_migration_chain_is_linear_with_single_head` | 硬编码 head `0007` | 更新为 `0008` 并把链断言扩展为 8 元（链仍单根线性） |
| 2 | `tests/test_package_2_step_1_authorization_persistence.py::test_b1_migration_head_is_0007` | 硬编码 head `0007` | 语义修正为「0007 在链上 + head 为当前 head」，并断言 head == 0008 |

> 两处都是这条历史教训的同类复发（守卫硬编码 head 会随迁移推进静默失效）。
> 与 `tools/closure_evidence.py` 的 `_migration_head()` 动态取法保持一致。

**实现代码零改动**：本轮没有修改任何 `app/**` 或 `alembic/**` 文件——
PG 侧一次通过，说明 CP3-G 的 SQL 与方言分支（`now()` / `now() + interval`、
部分唯一索引的 `postgresql_where`、RLS 的 NULLIF 形态）实现正确。

---

## 5. 一条必须固化的证据纪律（已落到用例里）

```
PostgreSQL **superuser 会绕过 RLS**。
⇒ RLS 证据必须在 rolsuper=false 且 rolbypassrls=false 的角色下采集，
   否则断言的是空气（本轮首次运行即因此暴露：tenant 2 看到了 tenant 1 的行）。

G3 用例现在显式创建 cp3g_rls_app（NOSUPERUSER NOBYPASSRLS）并断言
current_user 的两个角色属性均为 false 后，才执行隔离断言。
生产对应角色 = acp_app（docker-compose.production.yml + _deploy/postgres/init/10_app_role.sh）。
```

---

## 6. Final Gate（CP3-G 整体）

```
PASS
```

CP3-G 全部 G1~G20 判定（PG 侧补齐后）：

```
G1  0008 migration              ✅ 真实 PG upgrade head
G2  PG / SQLite parity          ✅ 双方言各自实跑
G3  RLS                         ✅ 非 superuser 角色实测 + FORCE 已启用
G4  partial unique index 强制    ✅ SQLite + PG 双向
G5  端点认证                     ✅（SQLite 侧，端点不依赖 PG）
G6  supersede                   ✅ SQLite + PG
G7  expiry                      ✅ SQLite + PG
G8  group rejection             ✅
G9  Identity user_id=NULL       ✅
G10 Identity + Grant 原子性      ✅
G11 CAS winner/loser            ✅ SQLite + PG
G12 replay                      ✅
G13 三 Adapter chat_type         ✅
G14 Worker-only injection       ✅
G15 unarmed path unchanged      ✅
G16 G-D 全仓守卫                 ✅
G17 G-I 索引守卫                 ✅ 双方言
G18 targeted tests              ✅
G19 full regression             ✅ 2338 passed / 0 failed（PG 启用）
G20 scope integrity             ✅ 无 UI / 无新配置 / 无新 stage / 无新 failure_code
```

**CP3-G 可以收口。** 后续按既定顺序：Implementation verification → 是否纳入 Cloud V0
（沿用 `RB-C3-01` §4.2：默认 post-release backlog，入 V0 需独立 blocker 裁定）→ 再谈 UX。

---

## 7. 本轮未做

```
❌ 未做 UI（任何前端文件零改动）
❌ 未做 CP4（M3 Session 冲突回读仍待 CP-4-1）
❌ 未修改任何生产代码 / 迁移（PG 侧一次通过）
❌ 未触碰既存 Docker 容器；临时容器与临时目录已清理
❌ 未 commit / tag
```
