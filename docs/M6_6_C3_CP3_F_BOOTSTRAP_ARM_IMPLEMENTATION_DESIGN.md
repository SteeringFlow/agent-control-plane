# M6.6-C3-CP3-F — Bootstrap Arm Implementation Design

> 性质：**READ-ONLY 实现设计**。未写 / 未改任何代码、迁移、测试、配置、ADR。
> 本文件只把「已经批准的架构」翻译成「可直接实现的设计」，并在末尾给出
> **Implementation Ready / Not Ready**。
>
> 上游：ADR-12（语义授权）、ADR-11（授权持久化补签）、
> 　　　`docs/M6_6_C3_CP3_D_…`（O-1~O-5 裁决）、
> 　　　`docs/M6_6_C3_CP3_E_GUARD_CHANGE_MANIFEST.md`（守卫修改面）
>
> **结论先行：IMPLEMENTATION READY（有条件）** —— 条件见 §22。

---

## 1. Executive Summary

### 1.1 交付物清单（实现顺序即依赖顺序）

| # | 交付物 | 位置 | 类型 |
|---|---|---|---|
| 1 | `bootstrap_arms` ORM 模型 + 部分唯一索引 | `app/persistence/models/bootstrap_arm.py` | 新增 |
| 2 | Mapper（ORM ⇄ Domain） | `app/persistence/mappers/bootstrap_arm.py` | 新增 |
| 3 | Repository Port + Adapter | `app/repositories/bootstrap_arm.py`、`app/persistence/repositories/bootstrap_arm.py` | 新增 |
| 4 | Domain 值对象 `BootstrapArm` | `app/domain/bootstrap_arm.py` | 新增 |
| 5 | Alembic 迁移 `0008` | `alembic/versions/0008_bootstrap_arms.py` | 新增 |
| 6 | UoW 暴露 `bootstrap_arms` | `app/persistence/unit_of_work.py`、`app/repositories/unit_of_work.py` | 修改 |
| 7 | Application Service（arm / revoke / 读） | `app/application/bootstrap_arms.py` | 新增 |
| 8 | 端点 `POST /api/connections/{id}/bootstrap-arm` | `app/api/management/bootstrap_arms.py` + `main.py` 装配 | 新增 |
| 9 | `AuthenticatedUser` 依赖提供者 | `app/api/auth.py` + `main.py` | 新增（小） |
| 10 | Bootstrap 组件 | `app/application/bootstrap.py` | 新增 |
| 11 | Worker 注入点 | `app/application/worker.py`、`app/main.py:135-136` | 修改（各 1 行） |
| 12 | `chat_type` metadata | `app/adapters/channels/wecom_bot.py`、`wecom.py`、`local.py` | 修改（各 1 处） |
| 13 | 守卫增补 / 升级 | 见 §18 | 修改（增补型） |
| 14 | 测试 | 见 §19 | 新增 |

### 1.2 三条触发路径汇聚于一点（落点的决定性论据）

```
Trigger 1（HTTP webhook）  main.py:384-385  → _worker_for(scope).process(id)
Trigger 1（WS 长连接）     main.py:395      → _worker_for(scope).process(id)
Trigger 2（Driver 回收）   main.py:354-357  → ProductionDriver → Worker.process(id)
```

⇒ **所有执行都经过 `Worker.process`**。因此 Bootstrap 只需在 `Worker.process` 内注入一次，
自动覆盖全部三条路径，且不需要改动任何 Trigger。**这是落点选择的决定性理由。**

---

## 2. `bootstrap_arms` 精确字段

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | Integer PK | autoincrement | — |
| `tenant_id` | Integer | NOT NULL, FK `tenants.id`, **index** | RLS 谓词列；租户归属**自有列**，绝不推导 |
| `channel_id` | Integer | NOT NULL, FK `channels.id` | scope |
| `connection_id` | Integer | NOT NULL | scope（复合 FK 的一部分） |
| `status` | String(32) | NOT NULL, server_default `'armed'` | ∈ {`armed`, `consumed`, `revoked`} |
| `initiator_user_id` | Integer | **NOT NULL**, FK `users.id`（**无 ondelete** = NO ACTION） | O-4：Arm 只在存在真实认证操作者时被创建 ⇒ 不可能为空；无默认值、无 sentinel |
| `expires_at` | DateTime(timezone=True) | NOT NULL | **DB 计算**（`now() + interval`），app 只传间隔 |
| `consumed_at` | DateTime(timezone=True) | NULL | 消费时由 **DB** 写入 |
| `consumed_by_interaction_id` | Integer | NULL，**无 FK** | 避免与 interaction 生命周期互锁 |
| `consumed_external_identity_id` | String(255) | NULL，**无 FK** | + (tenant, channel) ⇒ Identity UNIQUE 键 |
| `consumed_external_conversation_id` | String(255) | NULL，**无 FK** | + (tenant, channel, connection) ⇒ Grant 四元组 UNIQUE 键 |
| `created_at` | DateTime(timezone=True) | NOT NULL, `server_default=func.now()` | = armed_at |
| `updated_at` | DateTime(timezone=True) | NOT NULL, `server_default=func.now()`, `onupdate=func.now()` | 全表纪律 |

**表级约束**

```
UNIQUE / 部分唯一索引：
    (tenant_id, channel_id, connection_id) WHERE status = 'armed'
复合 FK：
    (tenant_id, channel_id, connection_id) → connections(tenant_id, channel_id, id)
索引：
    ix_bootstrap_arms_tenant_id
RLS：ENABLE + FORCE + tenant_isolation（NULLIF 形态）
```

**明确不建的列**：`agent_id`（派生）、`identity_id` / `grant_id`（会阻断授权撤销且会漂移，
ADR-12 §7）、`revoked_at` / `revoked_by`（V0 不需要）、`challenge_*`（S-2 预留，V0 不建）。

---

## 3. Migration `0008`（PostgreSQL）

```
revision = "0008" ; down_revision = "0007"

upgrade():
  1. op.create_table("bootstrap_arms", …)          # 列同 §2
  2. op.create_index("ix_bootstrap_arms_tenant_id", …)
  3. op.create_index(
         "uq_bootstrap_arms_scope_armed",
         ["tenant_id","channel_id","connection_id"],
         unique=True,
         postgresql_where=sa.text("status = 'armed'"),   # ← O-1 的权威层
         sqlite_where=sa.text("status = 'armed'"),       # ← 双路径同形状
     )
  4. IF postgresql:
         ALTER TABLE bootstrap_arms ENABLE  ROW LEVEL SECURITY
         ALTER TABLE bootstrap_arms FORCE   ROW LEVEL SECURITY
         CREATE POLICY tenant_isolation ON bootstrap_arms
             USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::integer)
             WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::integer)

downgrade(): 有损（丢弃全部 Arm 行）；先 DROP POLICY + DISABLE RLS，再删索引与表。
```

**纪律**：与 `0007` 同形（单事务原子、RLS 用 0005 的 NULLIF 形态、无 backfill —— Arm 表
**不回填**：历史数据里不存在「已 arm」事实，回填等于凭空创造授权窗口）。

---

## 4. SQLite / OSS 与 test parity

```
OSS / 本地：不跑 Alembic，schema 由 Base.metadata.create_all 承担（0007:44-46 纪律）
⇒ ORM 模型必须自带同一条部分唯一索引：
      Index("uq_bootstrap_arms_scope_armed",
            "tenant_id","channel_id","connection_id",
            unique=True,
            postgresql_where=text("status = 'armed'"),
            sqlite_where=text("status = 'armed'"))
```

**Test parity 要求（必须双跑同一断言）**

| 断言 | SQLite（create_all） | PostgreSQL（alembic upgrade head） |
|---|---|---|
| 插入第二条 `armed`（同 scope）必须失败 | ✅ | ✅ |
| 过期 Arm 不可消费 | ✅ | ✅ |
| 消费 CAS 的 `rowcount == 1` 语义 | ✅ | ✅ |
| `expires_at` 与 `now()` 可比（格式/时区） | ✅（需实测，见 §9） | ✅ |

> 方言可行性（CP3-D B-3）在**实现步内**验证；若任一方言不支持部分索引 ⇒
> **停止并回到 Gate**（CP3-D §2.5 禁区），不得降级为纯应用层。

---

## 5. RLS Policy

```
ALTER TABLE bootstrap_arms ENABLE ROW LEVEL SECURITY;
ALTER TABLE bootstrap_arms FORCE  ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON bootstrap_arms
    USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::integer)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::integer);
```

与全表同纪律；第一层防线 = Repository 显式追加 `tenant_id` 过滤，第二层 = RLS。
**新表必须同时满足 DoD 断言**：`tenant_id` 列 + `tenants.id` FK + `ix_bootstrap_arms_tenant_id`
（`test_cloud_0_1_1_b_tenant_isolation_dod.py:59-63`）。

---

## 6. Partial Unique Index（O-1 落地）

```
权威层 = DB。应用层 revoke-then-insert 只负责 supersede 语义，不承载不变量。

SQLAlchemy（ORM）
    Index("uq_bootstrap_arms_scope_armed", "tenant_id","channel_id","connection_id",
          unique=True,
          postgresql_where=text("status = 'armed'"),
          sqlite_where=text("status = 'armed'"))

⚠️ 它是 Index，不是 UniqueConstraint
   ⇒ 既有「UNIQUE 必须 tenant 前导」守卫（形态为 `in uc[...]` 成员判定）**看不见它**
   ⇒ 必须新增 G-I 断言（E4 §7），否则安全权威层落在守卫盲区
```

---

## 7. Arm 状态生命周期

```
                      arm(admin)                      supersede(admin)
  UNARMED ────────────────────────► ARMED ───────────────────────────► REVOKED
                                      │  ▲                              （终态）
              消费（CAS 赢 + 建立授权事实）│  │ 事务失败 → 回滚
                                      ▼  │
                                   CONSUMED（终态）

  EXPIRED = 逻辑状态（status 仍为 'armed'，但 expires_at <= now()）
            → 不建状态值、不写行、不删行（审计留存）
```

| 跃迁 | 触发 | 事务 | 可逆 |
|---|---|---|---|
| → ARMED | 端点 arm | 独立短事务（revoke 旧 + insert 新） | 否 |
| ARMED → CONSUMED | §10 消费事务 | **与 Identity+Grant 同一事务** | 否 |
| ARMED → REVOKED | supersede（同一事务内） | 独立短事务 | 否 |
| ARMED → EXPIRED | 时间（谓词） | 无写入 | — |

**不变量**（机器化）：INV-1 同 scope 至多一条 `armed`；INV-2 CONSUMED/REVOKED 是终态；
INV-3 存在 CONSUMED ⇒ 该事务建立了 Identity 与/或 Grant；INV-4 EXPIRED 零写入。

---

## 8. Supersede 事务（重复 Verify）

```
BEGIN（tenant-scoped UoW）
 ① 存在性：uow.connections.get_by_id(connection_id) → None ⇒ ConnectionNotFoundError(404)
    （跨租户在本作用域内等价于不存在，沿用 Grant API 纪律）
 ② 归属：channel_id 取自 Connection（请求体不接受 tenant/channel）
 ③ UPDATE bootstrap_arms
        SET status='revoked', updated_at=now()
      WHERE tenant_id=? AND channel_id=? AND connection_id=? AND status='armed'
 ④ INSERT bootstrap_arms(status='armed', expires_at = now() + interval,
                         initiator_user_id = 认证主体, …)
 ⑤ COMMIT
```

- 撞 `uq_bootstrap_arms_scope_armed`（并发双击）⇒ **重试一次 ③+④**；仍撞 ⇒ 按既有错误面返回
  （不得静默产生两条 ARMED）。
- 回滚 ⇒ 旧 ARMED 完好（正确）。
- **不校验 `Connection.status`**：与既有 Grant 创建完全同形
  （`app/application/connection_conversations.py:74-93` 只校验存在性 + channel 归属 + 唯一性，
  不校验 status）。后续若 Connection 非 active，首条消息自然落到既有 `ROUTING_FAILED`。
  ⇒ 这同时关闭了 CP3-D §5.4 的待定项，且**不新增任何错误类**。

---

## 9. Expiration Predicate

```
判定恒在 SQL 内（与写入同语句，无 TOCTOU）：
    WHERE … AND status = 'armed' AND expires_at > now()

时间源 = DB（12.9.4.11 §4 铁律：app 只传间隔，绝不生成绝对时刻）
写入侧（方言对称，形态复用 _stale_cutoff）：
    sqlite      → datetime('now', '+<ttl> seconds')
    postgresql  → now() + interval <ttl> seconds
```

**TTL 取值**：**代码常量**（建议 `BOOTSTRAP_ARM_TTL_SECONDS = 600`），单事实源。
**不新增配置项** —— Cloud V0 冻结 §3.2 明列「新配置项」非 blocker；且本设计不需要可调。

**实现期必须实测（SQLite）**：`server_default=func.now()`（CURRENT_TIMESTAMP，UTC
`YYYY-MM-DD HH:MM:SS`）与 `datetime('now','+N seconds')` 的**字符串格式可比性**；
由 §4 的 parity 断言「过期 Arm 不可消费」覆盖。

---

## 10. Atomic Consume

```
BEGIN（tenant-scoped UoW）
 ① 定位 Arm（同一 scope、status='armed'、expires_at > now()）
    无命中 ⇒ NOT_APPLICABLE，退出（不写任何东西）
 ② 范围校验：inbound.metadata["chat_type"] == "single"
    否/缺失 ⇒ NOT_APPLICABLE，退出（不消费）
 ③ 快速路径：Identity 已存在 且 该 conversation 的 Grant 已存在
    ⇒ ALREADY，退出（**不消费** —— §10 消费条件 ④）
 ④ CAS 消费（单条条件化 UPDATE）：
       UPDATE bootstrap_arms
          SET status='consumed',
              consumed_at = now(),
              consumed_by_interaction_id = :iid,
              consumed_external_identity_id = :eid,
              consumed_external_conversation_id = :cid
        WHERE id = :arm_id
          AND status = 'armed'
          AND expires_at > now()
    → rowcount == 1 ⇒ 取得；rowcount == 0 ⇒ 见 §17 输家路径
 ⑤ find-or-create Identity（user_id = NULL）
 ⑥ find-or-create Grant（普通 ConnectionConversation 行）
COMMIT
```

**顺序要点**：CAS 在写入之前（先取得许可，再写授权）；任一步失败 ⇒ 回滚 ⇒ Arm 回到 ARMED。

---

## 11. Identity + Grant 同事务（find-or-create）

```
Identity：
    find_by_channel_and_external_id(channel_id, external_id)
      → 命中：复用（绝不建第二行；UNIQUE(tenant,channel,external_id)）
      → 未命中：create(Identity(user_id=None, channel_id, external_id))
      → IntegrityError（并发插入）⇒ **回读复用**（M1），不得冒泡为 UNEXPECTED_FAILURE

Grant：
    find_by_binding(channel_id, external_conversation_id, connection_id)
      → 命中：复用
      → 未命中：create(ConnectionConversation(...))
      → IntegrityError ⇒ **回读复用**（M2）

两者与 ④ 的 CAS 在**同一事务**提交 ⇒ 「CONSUMED 且无授权」在状态空间上不可达。
```

**消费条件（CP-3 §9.4）**：本事务**确实建立了** Identity 与/或 Grant 才保持 CONSUMED；
若二者**都已存在**（③ 快速路径）⇒ 不进入消费流程（Arm 保持 ARMED 直至自然过期）。

**`user_id` 恒 NULL**；`IdentityApplicationService` **不得**被 Bootstrap 引用
（该标识符在 ADR-5 守卫的禁止集合内，见 §18 G-D）。

---

## 12. Bootstrap 组件落点 + Worker 调用位置

```
新增：app/application/bootstrap.py
      class BootstrapCoordinator:            # 命名避开 Resolver/Service/Manager
          def __init__(self, *, uow_factory, ttl_seconds=…): …
          def ensure(self, inbound, interaction_id) -> BootstrapOutcome
                     # NOT_APPLICABLE / ALREADY / APPLIED

Worker.process（app/application/worker.py:62-87）内：
      record = self._load(interaction_id)
      if not self._phases.claim_processing(interaction_id): return False
      expected_started_at = self._read_started_at(interaction_id)
      inbound = inbound_from_snapshot(record.request_payload)
      try:
          self._bootstrap.ensure(inbound, interaction_id)   # ★ 新增（默认 None ⇒ skip）
          result = self._runtime.execute(inbound)
      except Exception as exc:
          self._phases.record_failure(...); raise
      …

装配（app/main.py:135-136）：
      worker = Worker(runtime=runtime_coordinator, phases=phases,
                      uow_factory=runtime_uow_factory,
                      bootstrap=BootstrapCoordinator(uow_factory=runtime_uow_factory))
      # TenantComposition 不新增字段（Bootstrap 随 Worker 注入，不是租户级资源）
      # bootstrap 缺省 None ⇒ 未注入时行为与今天逐字节一致（可开关）
```

**位置纪律**：`claim` **之后**、`runtime.execute` **之前**。
（claim 之前 ⇒ 未取得执行权的进程也会改状态；execute 之后 ⇒ 管线已失败。）

**失败处理（不新增 error code）**：
- 不适用（无 Arm / 过期 / 已消费 / scope 不符 / 群聊）⇒ no-op，管线照常 ⇒ 既有
  `IDENTITY_RESOLUTION_FAILED`；
- 事务内 DB 异常 ⇒ 回滚 ⇒ no-op + `LOGGER.exception` ⇒ 管线照常 ⇒ 既有失败码。
  **不得**冒泡成 `UNEXPECTED_FAILURE`（CP-3 §19.2）。
  ⚠️ 代价：生产面仅有日志、无 Evidence 行。这是「不新增 stage/evidence」下的**有意取舍**；
  是否新增管理面 Evidence 属后续独立 Gate。

---

## 13. `chat_type` 从 Adapter 到 Bootstrap 的传递

**通路（零契约改动）**：

```
Adapter 归一化
    → InboundInteraction.metadata["chat_type"]        # 契约仍 6 字段，只是 metadata 多一个键
    → inbound_to_snapshot（**metadata 恒存**）        # interaction_snapshot.py:54-68
    → interactions.request_payload
    → Worker: inbound_from_snapshot（重建 metadata）  # interaction_snapshot.py:71-99
    → inbound.metadata["chat_type"]
    → BootstrapCoordinator.ensure 读取
```

> 关键事实：`metadata` 已在快照里**恒存并重建**（12.9.4.3 收口），
> 因此 `chat_type` **自动往返**，**不需要**改 `InboundInteraction` 字段面、
> **不需要**改 snapshot、不需要新 DB 列。

**Adapter 侧（三处，各一个 metadata 键）**

| Adapter | 处理 |
|---|---|
| `wecom_bot.py` `inbound_from_callback` | 已知 `chattype ∈ {single, group}` ⇒ 写入 `metadata["chat_type"] = chattype`（与既有 `bot_reply` 键并列） |
| `wecom.py` `_inbound_from_robot_json` | 仅 `chattype == "single"` 才继续 ⇒ 写入 `"single"` |
| `local.py` | 显式声明其会话形态（单会话 ⇒ `"single"`）；实现步需先确认 local 的会话语义再落键 |

**纪律**：协议知识只留在 Adapter（与 `bot_reply` / `response_url` 同形）；
**禁止**在 Core 用「`external_conversation_id != external_identity_id` ⇒ 群聊」推断。

**副作用评估**：`chat_type` 会随 12.8.2 的 metadata 合并进入 `OutboundResponse.metadata`；
`reply_context_from` / `_response_url_from` 只取各自键 ⇒ **无影响**。

---

## 14. Group Chat 的拒绝位置

```
拒绝点 = BootstrapCoordinator.ensure 的 ② 步（消费事务**之前**）

    metadata.get("chat_type") == "single"  ⇒ 允许继续
    缺失 / None / 其它值（含 "group"）    ⇒ NOT_APPLICABLE
        · 不消费 Arm（Arm 保持 ARMED，可留给后续单聊消息）
        · 不创建 Identity / Grant
        · 不报错、不新造失败码
        · 该消息继续走管线 ⇒ 无授权时落到既有 IDENTITY_RESOLUTION_FAILED
```

**必须同时声明（防误读）**：群聊的入站、Session、Grant、Agent 执行与投递**全部照旧**；
管理员仍可为 `chatid` 显式创建普通 Grant。变化只有一条：**Arm 不对群会话开放**。

---

## 15. `initiator_user_id` NOT NULL + 端点认证/鉴权

```
认证依赖（新增，与 build_tenant_scope_provider 同形但**不回退**）：
    build_authenticated_user_provider(auth_service)
        cookie → service.verify(proof) → AuthenticatedUser(user_id, tenant_id)
        无 cookie / 无效 cookie ⇒ 401（BrowserAuthError 既有映射）
        ❌ 绝不回退 default_tenant_scope()（那是 O-4 明令禁止的不可归属主体）

Arm 端点：
    依赖 1：AuthenticatedUser（⇒ initiator，且决定 tenant）
    依赖 2：tenant-scoped service provider（⇒ UoW 绑定该 tenant）
    写入：initiator_user_id = authenticated_user.user_id   （NOT NULL，无默认值）
```

**偏差登记**：CP3-D §5.3 曾写「无认证权威 ⇒ 403」。本设计精确化为 **401**：
「无已认证主体」是**认证**问题，不是**授权不足**（403 保留给已认证但越权）。

**OSS 未登录** ⇒ Arm 端点不可用（401）；OSS 已登录 ⇒ 可用（initiator = 该真实 User）。
未登录时的替代路径仍是既有「管理面显式创建 Identity + Grant」。

---

## 16. Endpoint Contract

```
POST /api/connections/{connection_id}/bootstrap-arm
```

| 项 | 设计 |
|---|---|
| **认证** | 有效浏览器会话（§15）；无 ⇒ 401 |
| **输入** | 仅路径 `connection_id`。**不接受** `tenant_id` / `channel_id` / `ttl` / `conversation`（防 scope 错配与窗口操纵） |
| **前置** | Connection 存在且在本租户作用域内（跨租户 = 404）。**不校验 status**（§8） |
| **行为** | supersede：旧 ARMED → REVOKED，插入新 ARMED（同一事务） |
| **响应** | **201**；`{ arm_id, connection_id, status, expires_at }`<br>`expires_at` = **DB 生成值**（app 不计算、不回显间隔）；无 secret；不回显 initiator |
| **重复调用** | **201 + supersede**（每次都产生一条新 Arm 行，旧行 REVOKED 保留审计） |
| **错误映射** | `ConnectionNotFoundError` → 404；`BrowserAuthError` → 401；其余 ApplicationError → 500（沿用 `channels.py:77-85` 同形映射） |
| **V0 不做** | GET / LIST / PATCH / DELETE（Arm 不是 CRUD 资源） |
| **用户可见文案** | 「验证连接 / Verify Connection」；**不得**出现 Arm / Bootstrap / 授权 / Tenant 等词（Cloud-0.1.5 G3） |

**不新增 ApplicationError**：全部复用既有错误类（§8）。

---

## 17. Concurrency / Replay / Reclaim

| 场景 | 行为 |
|---|---|
| **两条首条消息并发** | 两条 `ensure` 各自开事务；CAS 条件 UPDATE 由 DB 串行化 ⇒ **恰好一条 rowcount==1** |
| **赢家** | 消费 + Identity + Grant 同事务 COMMIT ⇒ 正常管线 |
| **输家** | rowcount==0 ⇒ **ROLLBACK（必须关掉该事务）** ⇒ **新开事务回读** Identity/Grant<br>· 命中 ⇒ ALREADY ⇒ 照常继续管线（**不报错**）<br>· 未命中（赢家已回滚）⇒ **重试 CAS 一次**<br>· 仍不适用 ⇒ 回落既有 `IDENTITY_RESOLUTION_FAILED` |
| **并发 arm（双击）** | 部分唯一索引兜底；应用层重试一次（§8） |
| **重推（同 `external_interaction_id`）** | intake 12.9.4.9 幂等回查 ⇒ 不产生第二条 interaction ⇒ 无第二次 Bootstrap |
| **Worker 崩溃 + reclaim** | Arm 已 CONSUMED、Identity/Grant 已 COMMIT ⇒ 重执行时 `ensure` 无 ARMED 且授权已存在 ⇒ NOT_APPLICABLE ⇒ 正常管线，**不产生第二行** |
| **Bootstrap 事务中崩溃** | 回滚 ⇒ Arm 仍 ARMED ⇒ 下一次执行可兑现 |
| **多实例** | DB CAS 裁决（PG 行级串行化；SQLite 单写者）⇒ 与单实例同语义；不依赖进程内锁 |

> 输家路径的关键细节：**必须在回读前回滚并关闭事务**（READ COMMITTED 下同一事务内
> 读不到赢家未提交的写入），否则会出现「明明已授权却被打成失败」的伪失败。

---

## 18. 既有 Guard 的具体修改面（E4 清单落地）

| # | 文件 / 断言 | 修改 |
|---|---|---|
| **G-A** | `test_cloud_0_1_1_b_tenant_isolation_dod.py:44-58` 表集合 | **增补** `bootstrap_arms`；新表须满足 `tenant_id` + `tenants.id` FK + `ix_bootstrap_arms_tenant_id` |
| **G-B** | `test_cloud_0_1_1_a::test_fresh_pg_after_upgrade` | 同上（PG-gated） |
| **G-C** | `test_step8_2_identity_resolution_contract.py:46-57` `EXPECTED_APPLICATION_FILES` | **增补** `bootstrap`（按序）；断言保持等值 |
| **G-D** | `test_adr_authorization_and_access_model.py:276-298` | **语义升级**：`_RUNTIME_CHAIN_FILES` 固定 4 文件 ⇒ **全仓扫描 + 显式允许清单**<br>允许清单（恰好）：Identity 创建 ∈ {`identities.py`, `bootstrap.py`}；Grant 创建 ∈ {`connection_conversations.py`, `bootstrap.py`}<br>`_PROVISIONING_IDENTIFIERS` **不变** ⇒ `bootstrap.py` **不得**出现 `create_identity` / `IdentityApplicationService` 等标识符（命名须避开：<code>ensure_bootstrap</code> / <code>_ensure_identity</code> 亦在禁止集合内，需另择名） |
| **G-E** | `test_package_2_step_4_…:324-329` 授权行唯一创建点 | `assert creators == ["bootstrap.py", "connection_conversations.py"]` |
| **G-F** | `RUNTIME_EXECUTION_ORDER` 全部精确元组断言（≥5 处） | **零变更** |
| **G-G** | `test_adr6_no_implicit_grant_on_first_message` | **零变更**（不删不改） |
| **G-H** | `test_package_2_step_4_…` 契约条目表 | **只增补**（meta-guard） |
| **G-I** | 「UNIQUE 必须 tenant 前导」守卫 | **新增**：断言存在 `unique=True` 的 `Index`、前导 `tenant_id`、`where status='armed'`；SQLite/PG 双跑「第二条 ARMED 必失败」 |
| **G-J** | `test_package_2_step_4_…:222-238` conftest / autouse 禁令 | **扩展**扫描对象到 `bootstrap_arms` / Arm 标识符（Arm 夹具必须显式、非 autouse） |

---

## 19. 测试

### 19.1 必须保持**完全不变**（回归基线）

```
RUNTIME_EXECUTION_ORDER 的全部精确元组断言（≥5 处）
test_adr5_identity_resolution_still_fails_closed
test_adr6_no_implicit_grant_on_first_message
test_b7_executor_reads_only_grants（授权执行器只读）
test_step9_* 六段/七段顺序与 Coordinator 纯度
test_cloud_0_1_5_*（G1~G5，含「不为 Gate 新增端点/术语」相关断言）
test_step12_9_*（claim / fence / reclaim 语义）
Package 2 全套（授权持久化、运行时授权、Grant 管理、守卫闭包）
```

### 19.2 必须**增补**（不是修改语义）

见 §18 的 G-A / G-B / G-C / G-E / G-H / G-J，以及 G-D（升级）/ G-I（新增）。

### 19.3 必须**新增**（CP-4 边界，ADR-12 §11）

```
Arm 生命周期：armed → consumed / revoked / expired；终态不可逆
O-1：同 scope 第二条 ARMED 插入失败（SQLite + PG）
O-4：无 cookie ⇒ 401；initiator_user_id 无 NULL 行；不得出现 sentinel
O-5：chat_type=group ⇒ Arm 保持 ARMED、零授权写入；群聊既有 Grant 不受影响
消费原子性：事务回滚 ⇒ Arm 回到 ARMED；COMMIT ⇒ CONSUMED 且 Identity/Grant 存在
并发：N 条首条消息 ⇒ 恰好 1 Identity + 1 Grant，且全部 interaction 到达终态
输家路径：回读命中 ⇒ 继续成功（不出现伪失败）
重放 / reclaim：不产生第二行、不二次 Agent
失败回落：无 Arm ⇒ IDENTITY_RESOLUTION_FAILED（不得为 UNEXPECTED_FAILURE）
Identity：user_id 恒 NULL；不创建 ACP User
Grant：普通行、无新列、可撤销（DELETE 不被 FK 阻断）
Guard：全仓扫描 ⇒ 创建点集合 == 允许清单（清单外即失败）
```

---

## 20. 是否需要新的 error code —— **不需要**

| 层 | 结论 | 理由 |
|---|---|---|
| Runtime `STAGE_*` | **不新增** | Bootstrap 不在管线内；`RUNTIME_EXECUTION_ORDER` 一处不改 |
| `failure_code` | **不新增** | 全部回落既有：`IDENTITY_RESOLUTION_FAILED` / `UNAUTHORIZED` / `ROUTING_FAILED` / `SESSION_RESOLUTION_FAILED` / `AGENT_DISPATCH_FAILED` / `CHANNEL_DELIVERY_FAILED` |
| Runtime `ApplicationError` | **不新增** | Bootstrap 失败 ⇒ 回滚 + no-op ⇒ 管线自有失败码 |
| 管理面 `ApplicationError` | **不新增** | Arm 端点复用 `ConnectionNotFoundError`（404）+ HTTP 401；不校验 status ⇒ 无需 422 |
| HTTP 状态码 | 沿用既有面 | 201 / 401 / 404 / 500 |

---

## 21. 明确不做（范围红线）

```
❌ Group Bootstrap（O-5 已禁止群消费 Arm，不因任何理由开放）
❌ Challenge / 验证协议（S-2 留独立 Gate）
❌ Bootstrap history UI / 管理页面
❌ Identity discovery system
❌ 自动 Identity provisioning（无 Arm 时永远不创建）
❌ 新 RBAC / 角色 / 权限体系
❌ 新 Authorization policy engine
❌ 新 Interaction stage / 新 failure_code
❌ Agent 侧改动（SDK / Agent Runtime 契约）
❌ 新配置项（TTL 用代码常量）
❌ 第二张授权状态表（bootstrap_arms 不参与 granted 判定）
```

唯一目标：**Admin 显式验证连接 → 产生受限 Arm → 下一条可信单聊消息完成
Identity + Grant 建立 → 进入现有 7 段管线。**

---

## 22. Implementation Ready / Not Ready

```
IMPLEMENTATION READY（有条件）
```

### 22.1 为什么 Ready

- 语义授权已到位：**ADR-12 已签发**（关闭 CP-1 U-1，并顺带关闭 U-2/U-3/U-4）；
- 架构选择已闭合：CP3-D 的 O-1~O-5 全部裁决，CP3-F 未发现任何新的架构未决项；
- 守卫修改面已逐条落到文件与行号（§18），且区分「增补 / 升级 / 零变更」；
- 落点唯一且覆盖全部三条 Trigger（§1.2），改动面极小（Worker 1 行 + main.py 1 行 + 3 个 Adapter 各 1 处）；
- **不需要新 error code / 新 stage / 新配置项 / 新授权表**（§20、§21）。

### 22.2 Ready 的条件（合并前必须为真）

| # | 条件 | 性质 |
|---|---|---|
| **C-1** | PG + SQLite **双方言 parity 测试全绿**（部分唯一索引、过期谓词、`rowcount` 语义） | 实现期验证（方言不成立 ⇒ 回 Gate，不得降级） |
| **C-2** | 不新增任何 Runtime stage / failure_code / 配置项；`RUNTIME_EXECUTION_ORDER` 断言零变更 | 纪律 |
| **C-3** | 三个 Adapter 全部显式声明 `chat_type`；未声明 ⇒ 不消费（不得用启发式推断） | 纪律 |
| **C-4** | G-D 升级为「全仓扫描 + 允许清单」后，`bootstrap.py` 是**唯一**新增运行时创建点；清单外零创建 | 纪律（ADR-12 E-1） |
| **C-5** | 实现物按 **post-release backlog** 排期（ADR-12 §12）；若要求入 Cloud V0 ⇒ 需独立的 blocker 裁定，**不得**借用 `RB-C3-01` | 排期（不阻塞设计） |

### 22.3 建议实现顺序

```
① 模型 + 迁移 0008 + 部分唯一索引 + RLS（先跑 C-1 的方言验证）
② Port / Adapter / Mapper + UoW 暴露
③ Application Service（arm/supersede/读）
④ 端点 + AuthenticatedUser 提供者（O-4，401 语义）
⑤ 三个 Adapter 的 chat_type（C-3）
⑥ bootstrap.py + Worker/main.py 注入（各 1 行）
⑦ 守卫增补与 G-D 升级（C-4）
⑧ CP-4 验证：并发 / find-or-create / Session 回读 / 重放 / reclaim / 多实例
```

---

## 23. 本轮未做（只读声明）

```
❌ 未创建任何 .py / 迁移 / 测试 / schema / 配置
❌ 未修改 main.py / worker.py / adapters / 任何既有文件
❌ 未修改 ADR-11 / ADR-12 / Package 2-C / Gate 2.6 / Cloud 冻结文档
❌ 未执行任何写库操作、未部署、未 commit / tag
```

**本文件为实现设计产出；按 §22.2 的 C-1~C-5 达成后即可进入 CP3-F 实现。**
