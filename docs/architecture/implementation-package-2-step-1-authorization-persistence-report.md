# Package 2 / Step 1 — Authorization Persistence Implementation Report

> 状态：**已实现 / 已提交**。范围严格限定为授权状态**持久化层**。
> 上游：Gate 2-A、Package 2-B、Package 2-C、Step 0（均已冻结）。
>
> ```
> commit   18d76d4  feat(authorization): persist conversation-to-connection grants
> files    23 changed, +1196 / -30
> ```

---

## 1. 修改文件清单（23）

### 新增 Production（6）

| 文件 | 内容 |
|---|---|
| `backend/alembic/versions/0007_connection_conversations_and_backfill.py` | 原子迁移：supporting UNIQUE + 建表 + 索引 + RLS + 回填 + 自检 |
| `backend/app/domain/connection_conversation.py` | 领域实体（frozen dataclass，**不携带 tenant_id**，与其余实体一致） |
| `backend/app/persistence/models/connection_conversation.py` | ORM（四元组 UNIQUE + 复合 FK，无 `ondelete`） |
| `backend/app/persistence/mappers/connection_conversation.py` | Mapper（**无** `apply_to_orm`——行不可变） |
| `backend/app/persistence/repositories/connection_conversation.py` | SQLAlchemy 适配器（tenant 自有列 stamp + 读取过滤） |
| `backend/app/repositories/connection_conversation.py` | Port（**3** 个方法） |

### 修改 Production（5）

| 文件 | 变更 |
|---|---|
| `app/persistence/models/connection.py` | + supporting `UNIQUE(tenant_id, channel_id, id)`（业务唯一性**不动**） |
| `app/persistence/models/__init__.py` | 注册新模型 + `__all__`；docstring 11 → 12 张表 |
| `app/domain/__init__.py` | 导出新实体 |
| `app/repositories/unit_of_work.py` | UoW **Port** + `connection_conversations` 属性 |
| `app/persistence/unit_of_work.py` | UoW 实现：构造 / 清空该仓储 |

### 测试（12）

```
新增  tests/test_package_2_step_1_authorization_persistence.py   （28 项）
更新  test_models.py                             metadata 表集合 + 计数 11→12
更新  test_database.py                           metadata 表集合
更新  test_cloud_0_1_1_b_tenant_isolation_dod.py metadata 表集合
更新  test_cloud_0_1_1_a_postgres_alembic_foundation.py  PG 表集合（PG-gated）
更新  test_step14_2_2_fresh_sqlite_schema_init.py         SQLite 期望表集合
更新  test_step12_9_4_12_attempt_fence.py                表集合
更新  test_step5_7_closure.py                    5 组文件清单 + schema no-drift 表集合
更新  test_unit_of_work.py                       UoW 实例属性面
更新  test_external_identity_decoupling_package_1.py    迁移链 head 0006 → 0007
更新  test_step10_6_1_agent_selection_semantics_audit.py ★ 语义修订（见 §9）
更新  test_step6_7_management_closure.py        ★ token 假阳性 allowlist（见 §9）
```

---

## 2. Migration 0007 — diff 摘要

```
revision      = "0007"
down_revision = "0006"

upgrade()   单事务内按序：
  1. op.create_unique_constraint("uq_connections_tenant_channel_id", "connections",
                                 ["tenant_id", "channel_id", "id"])
  2. op.create_table("connection_conversations", ...)   ← PK + 四元组 UNIQUE + 3 个 FK
  3. op.create_index("ix_connection_conversations_tenant_id", ...)
     op.create_index("ix_connection_conversations_connection_id", ...)
  4. [PG] ENABLE + FORCE ROW LEVEL SECURITY + CREATE POLICY tenant_isolation
  5. _backfill()                                        ← 按租户 set_config 后再读再写
  6.    └ 每租户自检：expected == actual，不等即 raise

downgrade() drops policy → indexes → table → supporting UNIQUE
             （**有损**：丢弃全部授权行；docstring 明写，不得当作安全回滚）
```

**原子性**：表存在但无授权 = 全量 DENY = 服务中断 → 二者必须同一事务。

---

## 3. 最终 DDL（PostgreSQL 实测输出）

### 3.1 `connection_conversations`

```
       column_name        |        data_type         | is_nullable
--------------------------+--------------------------+-------------
 id                       | integer                  | NO
 tenant_id                | integer                  | NO
 channel_id               | integer                  | NO
 external_conversation_id | character varying        | NO
 connection_id            | integer                  | NO
 created_at               | timestamp with time zone | NO
 updated_at               | timestamp with time zone | NO
(7 rows)
```

```
                        conname                        |                   pg_get_constraintdef
-------------------------------------------------------+--------------------------------------------------------------
 connection_conversations_pkey                         | PRIMARY KEY (id)
 fk_connection_conversations_channel_id                | FOREIGN KEY (channel_id) REFERENCES channels(id)
 fk_connection_conversations_tenant_channel_connection | FOREIGN KEY (tenant_id, channel_id, connection_id)
                                                       |   REFERENCES connections(tenant_id, channel_id, id)
 fk_connection_conversations_tenant_id                 | FOREIGN KEY (tenant_id) REFERENCES tenants(id)
 uq_connection_conversations_tenant_channel_conv_conn  | UNIQUE (tenant_id, channel_id, external_conversation_id, connection_id)
(5 rows)
```

```
                      indexname
------------------------------------------------------
 connection_conversations_pkey
 ix_connection_conversations_connection_id
 ix_connection_conversations_tenant_id
 uq_connection_conversations_tenant_channel_conv_conn
(4 rows)
```

→ **索引最小性成立**：除 PK 与 UNIQUE 自带索引外，只多两个；
授权判定主查询 `(tenant_id, channel_id, external_conversation_id)` 由 UNIQUE 的**前导列**覆盖，未重复建。

### 3.2 FK 删除语义（NS ACTION 实测）

```
                        conname                        | confdeltype
-------------------------------------------------------+-------------
 fk_connection_conversations_tenant_channel_connection | a
(1 row)
```

`a` = **NO ACTION**（阻断式）。不是 `n`(SET NULL)、不是 `c`(CASCADE)。

### 3.3 supporting UNIQUE 的证明

```
                conname                 |           pg_get_constraintdef
----------------------------------------+------------------------------------------
 uq_connections_tenant_channel_agent_id | UNIQUE (tenant_id, channel_id, agent_id)
 uq_connections_tenant_channel_id       | UNIQUE (tenant_id, channel_id, id)
(2 rows)
```

→ 业务唯一性 `(tenant_id, channel_id, agent_id)` **仍在**，新约束与之并列；
它的唯一作用是让 §3.1 的复合 FK 有合法 target。

---

## 4. RLS（PostgreSQL 实测）

```
 relrowsecurity | relforcerowsecurity
----------------+---------------------
 t              | t

     polname      |                                       using_expr
------------------+--------------------------------------------------------------------------------
 tenant_isolation | (tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::integer)
```

用 **0005 的 NULLIF 形态**（0005 的 `CORE_TABLES` 循环不覆盖新表 → 0007 直接写出）。

```sql
ALTER TABLE connection_conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE connection_conversations FORCE  ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON connection_conversations
    USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::integer)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::integer);
```

---

## 5. Backfill SQL（实际执行）

```sql
INSERT INTO connection_conversations
    (tenant_id, channel_id, external_conversation_id, connection_id)
SELECT DISTINCT tenant_id, channel_id, external_conversation_id, connection_id
FROM sessions
WHERE tenant_id = :tid                          -- ← 按租户循环时附加
  AND connection_id IS NOT NULL
  AND external_conversation_id IS NOT NULL
  AND NULLIF(TRIM(external_conversation_id), '') IS NOT NULL
ON CONFLICT DO NOTHING;
```

****不加 `status = 'active'`**（Package 2-C §5.2 冻结；V0 的 Session 无 ended 生命周期）。

### 5.1 为什么按租户循环（本次实现的关键加固）

`sessions` 自 0002 起 `ENABLE + FORCE RLS`。若迁移角色是**受 RLS 约束的 owner / 非超级用户**，
未设 `app.tenant_id` 时策略谓词为 `NULL` → 回填会**静默插入 0 行** = 全量 DENY 上线
（Package 2 点名的最大 migration risk）。

因此回填对每个租户先 `SELECT set_config('app.tenant_id', <tid>, true)` 再读再写；
并在每租户后立即自检：

```python
expected = count(DISTINCT eligible sessions WHERE tenant_id = tid)   # 同一 GUC 上下文
actual   = count(*) FROM connection_conversations WHERE tenant_id = tid   # 表刚建，必等于 expected
if actual != expected: raise RuntimeError(...)      # 「静默 0 行」不可能发生
```

### 5.2 回填实证（PG-gated 测试精确断言）

在 0006 上播 6 行 sessions，再升到 0007：

| 源行 | 结果 |
|---|---|
| `(id1, c1, CHAT-G1)` + `(id2, c1, CHAT-G1)`（**群内两成员**） | **收敛为 1 行**：`(1, CHAT-G1, 1)` |
| `(id1, c2=inactive, CHAT-G2)` | **保留**：`(1, CHAT-G2, 2)` |
| `(id1, NULL, CHAT-G3)` | 跳过（`connection_id IS NULL`） |
| `(id1, c1, '')` | 跳过（空白） |
| `(id1, c1, NULL)` | 跳过（NULL） |

```
断言结果：rows == [(1,'CHAT-G1',1), (1,'CHAT-G2',2)]
```

**空库**：0 行 → 全 DENY（新部署必须显式 Create Grant）。

---

## 6. UoW / Repository 边界

```python
class ConnectionConversationRepository(Protocol):     # 恰好 3 个方法
    def create(self, record) -> ConnectionConversation: ...
    def get_by_id(self, id) -> ConnectionConversation | None: ...
    def find_by_binding(self, channel_id, external_conversation_id, connection_id): ...
```

| 决策 | 理由 |
|---|---|
| **无 `update`** | 授权行不可变（改授权 = 删除 + 新建），与 Connection 绑定不可变同一思想 |
| **无 `delete` / `list_*`** | 消费者是 Step 3 的 Delete / List Grants API，到那时再加（不为无消费者预先扩张 Port） |
| **无授权判定查询**（`find_granted_connection_ids`） | 消费者是 Step 2 的 Authorization 执行器；本 Step 只落持久化边界，不提前实现运行时语义 |

UoW：Port 与实现各 +1 属性（`connection_conversations`），
构造注入 / 退出清空与其余 11 个仓储完全同形。

**领域实体不携带 `tenant_id`**（与 Identity / Session / Connection 一致）——
租户归属由 Persistence 层**自有列** stamp（`row.tenant_id = self._tenant_id`），
**绝不**经 `Connection → Tenant` 推导。

---

## 7. Targeted Tests

```
tests/test_package_2_step_1_authorization_persistence.py   28 项（22 SQLite + 6 PG-gated）
```

| 组 | 覆盖 |
|---|---|
| A1 Schema | 列清单逐字 / nullability / PK / 四元组 UNIQUE / 复合 FK target 三列 / FK 无 ondelete / **索引恰好两个** / 无 ACL 列 / supporting UNIQUE 存在且业务唯一性仍在 |
| A2 Domain·Mapper | frozen dataclass 形状 / **无 tenant_id** / mapper round-trip |
| A3 Repository | Port 方法面恰为 3 / UoW 暴露并清空 / create+读回 / **重复绑定被 UNIQUE 拒绝** / 租户作用域第一层防线（tenant B 读不到 tenant A） |
| A4 复合 FK | **同租户跨 channel → 拒绝** / **跨租户 → 拒绝** |
| A5 生命周期 | status 变化**不触碰授权** / **删除被引用的 Connection 被阻断** / 空库无授权 / **群授权是 conversation-scoped 而非 identity-scoped** |
| B PG-gated | head=0007 / DDL 事实（NOT NULL + FK delete rule + supporting UNIQUE）/ RLS enabled+forced+NULLIF / 回填精确结果 / 不合格行跳过 / 空库回填 0 |

**结果**：

```
本地（SQLite）    22 passed, 6 skipped
启用 PG           28 passed, 0 skipped
```

---

## 8. 全量回归

| 环境 | 结果 |
|---|---|
| 本地（无 PG） | **2135 passed, 13 skipped, 0 failed** |
| **启用 PostgreSQL** | **2145 passed, 2 skipped, 1 failed** |

唯一失败：`test_3b_3_3::test_c3_group_session_matrix_case_c_different_user`
—— **已知的 3B-3.3 时序 flake**（`WIP-3B3-3-WS-TIMING`，本会话早前已用 pre-Package-1 基线证明其既有性）。
本轮再次确认：**单跑 0.36s 通过**；Step 1 未触碰 Runtime 任何一行。

**PostgreSQL 实测**：迁移链 `0006 → 0007` 在 fresh PG 上成功应用；
§3/§4/§5 的全部 DDL / RLS / 回填断言均取自**真实 PostgreSQL**（非模拟）。

---

## 9. Guard 影响面（问题点名的「意外影响」）

**实际改动 11 个测试文件**，其中**只有 2 个**在我的预登记清单里 → **预登记低估了 9 个**。

### 9.1 exact-set / 文件清单类（9，纯集合更新）

| 文件 | 断言 |
|---|---|
| `test_models.py` | metadata 表集合 + `count == 11` → **12**（测试名一并改为 `..._is_twelve`） |
| `test_database.py` | metadata 表集合 |
| `test_cloud_0_1_1_b_tenant_isolation_dod.py` | metadata 表集合 |
| `test_cloud_0_1_1_a_postgres_alembic_foundation.py` | PG 升到 head 后的表集合（PG-gated） |
| `test_step14_2_2_fresh_sqlite_schema_init.py` | SQLite `EXPECTED_TABLES` |
| `test_step12_9_4_12_attempt_fence.py` | 表集合 |
| `test_step5_7_closure.py` | **5 组文件清单**（domain / repositories / mappers / persistence-repositories / models）+ `test_a7_schema_no_drift` 表集合 |
| `test_unit_of_work.py` | `set(vars(uow))` 精确属性面 |
| `test_external_identity_decoupling_package_1.py` | 迁移链 head 断言 0006 → **0007**（我自己的守卫，按设计拦住 head 推进） |

### 9.2 ★ 两处**语义性**修订（不是计数更新）

**（a）`test_step10_6_1::test_a5_no_conversation_to_agent_first_class_binding`**

该守卫断言「无 conversation→agent 一等绑定」并枚举冻结表集合。
新表是 Conversation → **Connection**，**不是** conversation→Agent：
Agent 仍只能由被选中的 Connection 派生（`agent_id` 是 Routing 的产物）。

处理方式（**显式修订，不静默**）：
- `FROZEN_TABLES` 加入新表，并写明「本表是 Package 2-C §1.5 的窄范围例外，
  **不是**本守卫禁止的 conversation→Agent 绑定」；
- 并把**新表面积纳入 token 扫描**（`domain/connection_conversation.py`、
  `repositories/connection_conversation.py`、`persistence/repositories/...`）
  → 红线在新代码上继续被强制。

**（b）`test_step6_7::test_i_no_identity_linking_capability`**

该守卫用 token 扫描（含 `bind`），`find_by_binding` 命中 token → 假阳性。
**没有改名规避**（那正是 Gate 2-A §M5/M6 禁止的做法），而是沿用本文件既有先例
（10.8.4 对 `bind_external_session` 的处理），新增一条**带理由的显式放行**：

```python
# Package 2 / Step 1 精确放行：`find_by_binding` 按 (channel_id,
# external_conversation_id, connection_id) 查一条 Conversation → Connection 授权——
# 它绑定的是「会话键 ↔ 访问路径」，完全不触碰 Identity / User，
# 既不做身份合并、也不做跨渠道匹配，因此**不是** Identity Linking 能力。
CONVERSATION_ACCESS_BINDING_ALLOWLIST = { ... }
```

### 9.3 未动的既有 stale（**已存在**，非本步引入）

`backend/tools/closure_evidence.py`（不是 pytest 测试，是 PG Closure 工具）中
`evidence_a` 硬编码 `head == "0002"`、`n_pol == 9`、精确表集合——
它**自 0003 起就已不一致**（现 head 0007）。本步使它多一处不一致，
但**未修改**（修它属于 PG Closure 专项，不在 Step 1 范围）。**建议在 Package 2 的
PG closure 步一并校正。**

---

## 10. 是否修改任何 3B-3.3 WIP

```
❌ 未修改
```

`git status` 证据（Step 1 提交后仍原样）：

```
 M backend/app/adapters/channels/wecom_bot.py
 M backend/tests/test_3b_2_2_wecom_bot_websocket_transport.py
?? backend/tests/test_3b_3_3_wecom_bot_group_chat.py
```

三者**均未进入本次暂存**（提交前已做边界扫描：`wecom|3b_|docs/` 命中为空）。

---

## 11. 本次未做（严格按 Step 1 边界）

```
❌ Authorization 运行时执行器        ❌ Coordinator 接线（第 7 依赖）
❌ Router 候选集收窄                ❌ 管理面 API / schemas / main 注入
❌ known-conversations 只读面        ❌ 前端
❌ WeCom adapter                     ❌ Session / Identity 语义
❌ Delivery / Worker                 ❌ 任何 3B-3.3 WIP 文件
❌ 未实现 Step 2/3 才需要的 Port 方法（delete / list / 授权判定查询）
```

---

## 12. Git

```
commit  18d76d4  feat(authorization): persist conversation-to-connection grants
parent  e8b12e4  docs(architecture): freeze authorization architecture and gates
diff    23 files changed, 1196 insertions(+), 30 deletions(-)
```

**未提交但仍在工作树**（等你裁决是否单独提交）：

```
docs/architecture/implementation-gate-2-a-access-authorization-implementation-design.md
docs/architecture/implementation-package-2-b-authorization-persistence-and-rollout-design.md
docs/architecture/implementation-package-2-c-authorization-contract-and-boundary-freeze.md
docs/architecture/implementation-package-2-step-0-fixture-semantic-classification.md
（+ 本报告）
```

> ⚠️ **连贯性提示**：`18d76d4` 的代码与 docstring 引用了上述 4 份未跟踪文档
> （Gate 2-A / 2-B / 2-C / Step 0）。与 Package 1 之后的情形相同，
> 建议紧接一个 `docs(architecture): ...` 提交把它们入库。

---

## 13. Step 1 完成判定

```
✅ Schema / Migration 0007 / RLS / 回填 / 导出链 / repository+UoW 边界 / targeted tests
✅ PostgreSQL 实测（DDL、RLS、回填、复合 FK 拒绝语义）
✅ 全量回归零红灯（除既有 WIP flake）
✅ 3B-3.3 WIP 零改动
✅ git 提交 18d76d4
⛔ Step 1 到此为止 —— 未实现 Authorization Runtime（等 Step 2）
```
