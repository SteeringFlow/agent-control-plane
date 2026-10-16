# M6.6-C3-CP3-E — Guard Change Manifest（精确守卫预期变化清单）

> 性质：**记录清单，不改测试**。
> 目的：CP3-F / CP-4 实现阶段必然引入 `bootstrap_arms` 表、`app/application/bootstrap.py`、
> 以及 Arm 端点。以下既有**精确断言**会因此失败或失去证明力。
> 本清单说明**哪些必须同步调整、为什么、以及调整形态**；实现阶段**不允许**
> 用「删断言 / 放宽断言」的方式让测试变绿。
>
> 关联：ADR-12 §11（机器化守卫要求，强制项 E-1 ~ E-5）。
> 本文件取代并扩展 `docs/M6_6_C3_CP3_D_…md` §14.1 的简表。

---

## 0. 总览

| # | 守卫 | 变化类型 | 是否允许改断言语义 |
|---|---|---|---|
| G-A | `test_cloud_0_1_1_b_tenant_isolation_dod` 表集合 | **增补型**（必破） | 只增补集合，不得放宽单表断言 |
| G-B | `test_cloud_0_1_1_a::test_fresh_pg_after_upgrade` | **增补型**（必破） | 同上 |
| G-C | `test_step8_2::EXPECTED_APPLICATION_FILES` | **增补型**（必破） | 只增补文件面 |
| G-D | **`test_adr5_runtime_never_creates_acp_user_or_identity`** | **语义升级**（核心） | ✅ 必须升级（见 §5） |
| G-E | `test_package_2_step_4` 授权行唯一创建点 | **增补型**（必破） | 增补 `bootstrap.py` 进允许清单 |
| G-F | `RUNTIME_EXECUTION_ORDER` 精确元组断言（≥5 处） | **零变更** | ❌ 一处都不改 |
| G-G | `test_adr6_no_implicit_grant_on_first_message` | **零变更** | ❌ 不删不改 |
| G-H | `test_package_2_step_4` 契约条目表 | **只增补** | ❌ 不得删改既有条目（meta-guard） |
| G-I | 「UNIQUE 必须 tenant 前导」守卫 | **新增断言**（易漏） | 见 §7 ⚠️ |
| G-J | `conftest` / autouse 不得预置授权 | **沿用，扩展对象** | 见 §8 |

---

## 1. G-A / G-B — 精确表集合（必破）

**断言**：

```
tests/test_cloud_0_1_1_b_tenant_isolation_dod.py:44-58
    CORE_TABLES = ("users","identities","channels","agents",
                   "connections","sessions","interactions","activities")
    assert set(Base.metadata.tables) == set(CORE_TABLES) | {
        "tenants","user_credentials","browser_sessions","connection_conversations"}
    # 并对每张表断言 tenant_id / tenants.id FK / ix_<t>_tenant_id
```

**为何必破**：新增 `bootstrap_arms` ⇒ 等值断言失败。

**同步调整形态（增补，不放宽）**：
- 把 `bootstrap_arms` 加入集合；
- 新表**同时满足**该断言的单表三要件（`tenant_id` 列 + `tenants.id` FK + `ix_bootstrap_arms_tenant_id`）
  —— 这是 RLS/隔离纪律，不是可选项；
- `test_cloud_0_1_1_a::test_fresh_pg_after_upgrade`（PG-gated，精确表集合）同批处理。

---

## 2. G-C — Application 文件面（必破）

**断言**：

```
tests/test_step8_2_identity_resolution_contract.py:46-57（精确等值列表）
tests/test_step8_2_identity_resolution_contract.py:252-257
    app_files = sorted(...glob("application/*.py"))
    assert app_files == EXPECTED_APPLICATION_FILES
```

**为何必破**：新增 `app/application/bootstrap.py`。

**同步调整**：把 `bootstrap` 加入列表（按字母序）。**不得**改成 `>=` / `issubset`。

---

## 3. G-E — 授权行唯一创建点（必破，且是 G-D 的形态依据）

**断言**：

```
tests/test_package_2_step_4_authorization_contract_closure.py:324-329
    creators = [p.name for p in APP_DIR.rglob("*.py")
                if "connection_conversations.create" in p.read_text()]
    assert creators == ["connection_conversations.py"]
```

**为何必破**：`bootstrap.py` 也会写授权行。

**同步调整**：

```
assert creators == ["bootstrap.py", "connection_conversations.py"]
```

> **关键**：这条守卫证明本仓**已经**采用「**全仓扫描 + 显式允许清单**」表达「唯一创建点」。
> 它就是 ADR-12 E-1 要求 ADR-5 守卫升级所应复用的**既有形态**（不是新发明）。

---

## 4. G-D — ADR-5 守卫（**语义升级**，本清单最重要项）

### 4.1 现状

```
tests/test_adr_authorization_and_access_model.py:276-289
    _PROVISIONING_IDENTIFIERS = frozenset({
        "create_user","create_identity","ensure_identity","upsert_identity",
        "provision_identity","auto_provision",
        "UserApplicationService","IdentityApplicationService",
        "IdentityLinker","IdentityMatcher","IdentityResolver","IdentityMerger", ...})
    _RUNTIME_CHAIN_FILES = (
        application/identity_resolution.py,
        application/routing.py,
        application/coordinator.py,
        application/runtime.py)
    # 只扫描这 4 个文件
```

### 4.2 为什么「固定 4 文件」已经不够

```
它证明的是：  「这 4 个文件里没有自动开户标识」
它**不能**证明：「整个 runtime 只有被允许的位置才能创建 Identity」

反例（CP-1 §9.4 已点名）：
    把 Bootstrap 实现为 application/bootstrap.py
      → 该文件不在 _RUNTIME_CHAIN_FILES 内
      → 守卫**通过**
      → 但 ADR-5 的实质（运行时零创建）已被违反
      → 这是**守卫规避**，不是**架构合规**

另一处同型漏洞：
    「application/ 新增第 5 个文件」本身也不会触发任何断言
      → 只有 test_step8_2 的文件面等值断言会发现（那是边界断言，不是创建点断言）
```

**因此这不是测试小修小补，而是 ADR-5 守卫语义本身的升级。**

### 4.3 升级后的语义（ADR-12 E-1 强制）

```
从： 扫描固定 N 个文件 ⇒ 断言零命中
改为：全仓扫描（app/ 下，排除 api/management/** 与 tests/）
      ⇒ 断言「创建点集合 == 显式允许清单」

允许清单（**恰好**）：
    Identity 创建 ∈ { application/identities.py（管理面）, application/bootstrap.py }
    Grant    创建 ∈ { application/connection_conversations.py（管理面）,
                      application/bootstrap.py }

清单外出现任何创建 ⇒ 失败。
新增第 2 个运行时创建点 ⇒ 必须回到 ADR Gate，不得就地加白名单。
```

**形态依据**：`test_package_2_step_4…:324-329`（§3）已用同一形态表达「授权行唯一创建点」。

### 4.4 顺带需要保留的原断言

```
✅ test_adr5_identity_resolution_still_fails_closed（:301-307）
   —— identity_resolution.py 仍必须 raise IdentityResolutionFailedError、仍零 {create,add,save,link,upsert}
   —— **一字不改**（Bootstrap 不得改变该文件的 Fail Closed 语义）
```

---

## 5. G-F — `RUNTIME_EXECUTION_ORDER` 精确元组断言（**零变更**）

```
被 ≥5 处精确元组断言锁定，例如：
    test_cloud_0_1_1_b_tenant_isolation_dod.py:148
    test_cloud_0_1_1_b_tenant_fanout.py:87
    test_cloud_0_1_1_a::test_runtime_execution_order_frozen:133
    test_step10_8_4…:386
    test_step10_1_4…:286
    test_step9_8_1::CANONICAL + test_order_is_canonical_six_stages
```

**规则：一处都不改。**
这是「Bootstrap 不成为第八个 Runtime Stage」的**机器化保证**；
任何对这些断言的放宽都等于放弃了 ADR-12 §9 的不变量。

---

## 6. G-G / G-H — 既有条目只增不删

```
G-G  test_adr6_no_implicit_grant_on_first_message
     ❌ 不删、不改其断言（其 docstring 已预留演进说明）
     ✅ Arm 前置条件的表达放在新增的 E-2 守卫里（ADR-12 §11）

G-H  test_package_2_step_4_authorization_contract_closure.py 的契约条目表
     ✅ 只增补新条目；❌ 不得删除或重命名既有条目（该表本身是 meta-guard）
```

---

## 7. G-I ⚠️ 「UNIQUE 必须 tenant 前导」守卫 —— 新增，易漏

**问题**：O-1（CP3-D §2）要求的是**部分唯一索引**：

```
UNIQUE(tenant_id, channel_id, connection_id) WHERE status='armed'
```

在 SQLAlchemy 中它是 **`Index(..., unique=True, postgresql_where=…, sqlite_where=…)`**，
而**不是** `UniqueConstraint`。既有的「UNIQUE 约束必须 tenant 前导」守卫
（`test_cloud_0_1_1_b_tenant_architecture_gate::test_unique_constraints_lead_with_tenant_id`，
形态为 `in uc["connections"]` 的成员判定）**看不见 `Index`**。

**后果**：O-1 的 DB 不变量（同一 scope 至多一条 ARMED）是**全设计的安全权威层**，
却可能**不在任何既有守卫的视野内** —— 与 §4.2 的「守卫盲区」同型问题。

**必须新增**：

```
① 断言 bootstrap_arms 上存在一条 unique=True 的 Index，
   其列前导为 tenant_id，且 where 条件为 status='armed'
② SQLite 与 PostgreSQL 双跑同一断言（O-1 的 test parity 要求）
③ 断言「插入第二条 ARMED 必须失败」（IntegrityError），两种方言同断言
```

---

## 8. G-J — conftest / autouse 不得预置授权（扩展对象）

**断言**：

```
tests/test_package_2_step_4_authorization_contract_closure.py:222-238
    conftest 不得出现 "connection_conversations"
    任何 autouse fixture 的 body 不得出现 "connection_conversations"
```

**为何相关**：Bootstrap 测试需要「已 Arm」夹具。若为了省事用 `autouse` 预置 Arm/Grant，
会使「无 Arm ⇒ Fail Closed」这条回归基线在其它测试中被静默绕过（与授权预置同一性质）。

**同步调整**：把 `bootstrap_arms` / Arm 相关标识符加入同一禁止扫描；
Arm 夹具必须是**显式、非 autouse** 的。

---

## 9. 实现阶段的禁止事项

```
❌ 为了让测试变绿而删除 / 放宽任何既有精确断言
❌ 用「改等值断言为子集断言」消化新表 / 新文件
❌ 把创建代码放在扫描清单之外以通过守卫（CP-1 §9.4 明令的规避）
❌ 就地给允许清单加第 2 个运行时创建点（须回 ADR Gate）
❌ 修改 RUNTIME_EXECUTION_ORDER 的任何一处精确断言
```

---

## 10. 本文件未做的事

```
❌ 未修改任何测试文件（本轮只记录清单）
❌ 未修改任何 ADR / 冻结文档
❌ 未实现 migration / 端点 / 组件
```
