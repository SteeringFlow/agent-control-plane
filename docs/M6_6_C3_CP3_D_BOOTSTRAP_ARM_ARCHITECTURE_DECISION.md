# M6.6-C3-CP3-D — Bootstrap Arm Architecture Decision Gate

> 性质：**READ-ONLY / ARCHITECTURE DECISION ONLY**。
> 未改代码 / DB / migration / 测试 / 配置 / 生产环境；未修改既有 ADR / Package 2-C /
> Gate 2.6 / Cloud 冻结文档；未创建测试数据、未删除数据、未 commit / tag。
> 本轮唯一产出物 = 本文件。
>
> 上游：`docs/M6_6_C3_CP1_…`（语义裁决，CONDITIONAL PASS，前置 = ADR-12）
> 　　　`docs/M6_6_C3_CP2_…`（Trust Gate，PASS / CLOSED）
> 　　　`docs/M6_6_C3_CP3_BOOTSTRAP_ARM_DESIGN_AUDIT.md`（设计审计，CONDITIONAL PASS，遗留 O-1~O-5）
>
> **结论先行：CONDITIONAL PASS。**
> O-1 ~ O-5 **全部裁决完成**，实现所需架构边界**已明确**；
> 唯一未关闭项不是架构问题，而是**文档/立项前置**（ADR-12 签发 + Cloud V0 blocker 记录 +
> ADR-11 补签）。若本轮同时授权签发 ADR-12，判定立即升为 **PASS**。

---

## 1. Executive Summary

### 1.1 O-1 ~ O-5 裁决表

| # | 问题 | 裁决 | 权威层 |
|---|---|---|---|
| **O-1** | 同一 scope 是否允许多个 ARMED | **不允许。C（两者结合），但 DB 是唯一最终权威** —— 部分唯一索引 `UNIQUE(tenant_id, channel_id, connection_id) WHERE status='armed'` + 应用层「同事务 revoke-then-insert」塑形 supersede 语义 | **DB** |
| **O-2** | 是否承认 `bootstrap_arms` 为 Cloud-0.1.x 冻结的显式例外 | **CONDITIONAL 承认**：必须走**既有例外机制**（新增 ADR 编号 + 显式取代关系），不是「绕过冻结」。入 V0 还需一条 **release-blocker 记录**（G5 阻断证据）。**至少需：ADR-12 签发** | ADR-12 |
| **O-3** | Admin 如何显式创建 Arm | **A — 新增专用 management endpoint**：`POST /api/connections/{connection_id}/bootstrap-arm`（Connection 子资源，与既有 `/health` 同形）。复用 `/health`（C）与 PATCH Connection（B）均被否；前端组合（D）在语义上等于伪造 | 管理面 |
| **O-4** | AUTH_REQUIRED=False / OSS 谁可以 Arm | **D'（精确化后的 D）**：initiator **必须**来自一次成功的 `BrowserAuthService.verify(cookie)`；**永不**回退到 anonymous / guest / system / sentinel / 未认证的既有 User。`initiator_user_id` 因此裁定为 **NOT NULL** | 认证边界 |
| **O-5** | 群聊能否消费 Arm | **B — V0 禁止 group conversation 消费 Arm**。群聊正常运行不受任何影响（既有显式 Grant 路径不变）。判别事实必须由 **Adapter 以 metadata 显式携带 `chat_type`**，禁止在 Core 用「conversation ≠ identity」启发式推断 | Adapter |

### 1.2 一句话给实现 Agent

> Arm 是 **Connection 上的一个短时、一次性、由已认证操作者显式开启的验证窗口**；
> 只有**单聊**、且**通过 Trust Gate** 的第一条消息能消费它；
> 消费 = 在**同一个短事务**里 CAS 掉窗口 + find-or-create 一条 `user_id=NULL` 的 External
> Identity + find-or-create 一条**普通** Grant；
> 窗口由 **DB** 保证「同一 scope 至多一条 ARMED」，由 **DB 时钟**保证过期，
> 由 **Arm 行本身**承载审计。

### 1.3 本轮否决的规避路径（O-2 明确要求）

```
❌ Connection.config / Channel.config      —— 不是万能配置容器（connection.py:17-19）
❌ Activity / metadata                     —— append-only，结构上无法表达「消费」（CP-3 §7.3）
❌ 内存 arm                                —— 重启即失、多实例失效、不可审计
❌ hidden flag / DB 直接写入 / 前端态 / localStorage
❌ 复用 /health 探活端点（GET 产生授权状态 = 破坏 GET 幂等）
```

---

## 2. O-1 Decision — ARMED 唯一性

### 2.1 问题

同一 `(tenant_id, channel_id, connection_id)` 是否允许同时存在多条 `status='armed'` 的 Arm？

**裁决：不允许。**（否则一次 arm 动作可兑现 N 次 → one-shot 被稀释，CP-3 §9.3 的
「至多一次 Bootstrap」直接失效。）

### 2.2 三方案比较

| 维度 | **A. PG partial unique index** | **B. 应用层 revoke + insert** | **C. 两者结合** |
|---|---|---|---|
| PostgreSQL production | ✅ 原生支持 | ❌ READ COMMITTED 下两次并发 arm 都可能 insert 成功 | ✅ |
| SQLite 开发/测试 | ✅ 部分索引（3.8+）支持，SQLAlchemy `sqlite_where` | ❌ 同样无保证（写锁串行使其**偶然**正确，不可依赖） | ✅ |
| Alembic 支持 | ⚠️ 需 `op.create_index(..., postgresql_where=...)`；OSS 侧由 ORM `Index(sqlite_where=...)` 承载（`0007:44-46` 的两条路径纪律） | ✅ 无迁移成本 | ⚠️ 同 A，但必须写对两种方言 |
| 事务行为 | 冲突即语句失败 → 事务内可捕获重试 | revoke+insert 同事务 → 回滚则旧 ARMED 完好 | 同左 |
| 并发 Verify | 第二次 insert 撞约束 → 需映射为 supersede（否则 409，UX 差） | ✅ 天然 supersede | ✅ 应用层吃掉常见路径，DB 兜底 |
| 并发消费 | 不受影响（消费是独立的条件 UPDATE） | 不受影响 | 不受影响 |
| rollback | 约束在语句级生效，回滚后一致 | 回滚 → 旧 ARMED 仍在（正确） | 同左 |
| test parity | 需在 SQLite 与 PG **双跑**同一断言 | 单跑即可但语义不可证 | 同 A |
| 既有 DB 约定 | 全仓 `UniqueConstraint` 均为**整表**约束，**无部分索引先例**（`app/persistence/models/*` 扫描确认） | — | 引入首个部分索引 |

### 2.3 关键回答：哪一层是最终不变量的权威来源？

> **数据库。**
>
> 应用层「revoke-then-insert」**不能**单独承载该不变量：两个并发 arm 事务在
> READ COMMITTED 下都可能先读到「无 ARMED」再各自 insert —— 没有 DB 约束时结果就是
> 两条 ARMED，而 one-shot 语义随之破裂。应用层的价值只在**把语义塑成 supersede**
> （重复 Verify = 刷新窗口），而不是提供正确性。

因此：

```
DB    = 不变量的唯一权威（不可绕过、可被测试直接断言）
应用层 = 语义塑形（supersede 而非 409）+ 并发下的友好重试
```

### 2.4 裁决（O-1 = C，权威 = DB）

```
1. 建部分唯一索引：
     UNIQUE(tenant_id, channel_id, connection_id) WHERE status = 'armed'
   PG：CREATE UNIQUE INDEX ... WHERE status='armed'
   SQLite：CREATE UNIQUE INDEX ... WHERE status='armed'（3.8+ 支持）
   同一约束以 SQLAlchemy Index(unique=True, postgresql_where=…, sqlite_where=…)
   在 ORM 上表达一次 → OSS(create_all) 与 Cloud(Alembic) 两条路径同形状
   （沿用 0007:44-46「两条路径不分叉」纪律）

2. arm 用例（单事务）：
     ① UPDATE 该 scope 上 status='armed' 的行 → status='revoked'
     ② INSERT 新行 status='armed'
     ③ COMMIT
   若 ② 撞唯一约束（并发双击）→ 重试一次 ①+②；仍撞 → 返回既有冲突语义（不得静默双 ARMED）

3. 消费仍是独立条件 UPDATE（不受本约束影响）：
     WHERE id=? AND status='armed' AND expires_at > now()  → rowcount==1
```

**SQLite 测试如何保持语义一致**：同一条 ORM `Index` 定义被 `Base.metadata.create_all`
（OSS/测试）与 Alembic `op.create_index`（PG）两侧使用；测试断言「插入第二条 ARMED
必须失败」在两种方言下跑**同一段代码**（既有 `test_step6_6_identity_management.py:229-234`
对 UNIQUE 的 IntegrityError 断言即为该风格的先例）。

### 2.5 降级禁区

```
若实现期证明某方言无法表达部分唯一索引：
    ❌ 不得降级为「纯应用层保证」（那等于把安全不变量交给并发时序）
    ❌ 不得改为 UNIQUE(tenant, channel, connection, status)（会阻断历史 CONSUMED/REVOKED 行）
    ⇒ 必须回到本 Gate 重新裁决（登记为 O-1'），不得在实现期自行变通
```

---

## 3. O-2 Decision — Cloud-0.1.x Release Freeze Exception

### 3.1 Current Rule（原文）

| 来源 | 原文要点 |
|---|---|
| `docs/cloud-v0-release-freeze.md:36` | **§1.14 V0 scope：「V0 不接受 §1 之外的任何新范围」** |
| `:54` | 禁止「在冻结范围新增能力、**字段**、**端点**、配置维度、行为分支」 |
| `:75` | §3.2：新功能 / 新端点 / 新配置项 → **不是 release blocker → post-release backlog** |
| `:88-95` | §3.3 判定流程：范围外（§1.14 之外）→ post-release backlog |
| `:98` | 存疑时**默认判为非 blocker** |
| `:71` | §3.1 典型 blocker 含「**Golden Path 阻断**」 |
| `docs/cloud-0.1.5-…:42` | G5：First Success = 真实 Interaction 的 `processing=succeeded × delivery=delivered` |
| `docs/architecture/authorization-and-access-model.md:10-12` | Access Authorization 本身即 §1 之外的**新范围**；「V0 冻结期内不实现；实现步须在**解冻 / 新阶段立项**后进入」 |

### 3.2 Conflict（逐条）

```
bootstrap_arms 新表            → §2「禁止新增字段」+ §3.2（新功能）
POST /api/.../bootstrap-arm    → §2「禁止新增端点」+ §3.2（新端点）
Arm 语义（新的授权来源）        → §1.14「V0 不接受新范围」
ADR-5 推论/红线                → authorization-and-access-model.md:296-297「运行时链路不得
                                 出现任何创建 Identity 的调用」；:302-303「❌ 无首条消息自动建
                                 User / Identity」；:650「❌ 首条消息自动创建 ACP User / Identity」
```

⇒ 按冻结文档**字面默认结论**：Bootstrap Arm 属 post-release backlog。

### 3.3 是否有既有例外机制？**有。**

| 机制 | 证据 |
|---|---|
| **新增 ADR 编号 + 显式取代关系** | `authorization-and-access-model.md:702-703`：「本文件为 FROZEN：**任何修改须以『新增 ADR 编号 + 显式取代关系』的方式提交，不得原地改写既有 ADR 的语义**」 |
| **窄范围 persistence exception 的正式批准** | Package 2-C §1.5（`implementation-package-2-c-…:90-98`）：`connection_conversations` 正是以「V0 唯一的授权状态存储 + 不是 ACL」的窄豁免获批 |
| **第二次走同一出口** | Package 2-C §1.3（`:56-59`）：`0006`（`identities.user_id` 放宽）已走过一次；ADR-11 是**第二次**，「不是例外之外的特例」 |

⇒ 结论：**本设计可以合法进入，但必须走 ADR 机制，不是绕过冻结。**

### 3.4 必须记录的流程债（本轮发现，非本轮修复）

> **ADR-11 只有内容冻结，文件从未创建。**
> 证据：Package 2-C `:508`「ADR-11 待签发 ⏳ 唯一剩余的前置」、`:515`「✗ ADR-11 文件本身
> （本 Gate 只冻结其内容）」；CP-1 §2.1 末行记录「`docs/architecture/` 下**无 ADR-11 文件**」。
> 即：`connection_conversations` 已在生产中运行，而其授权它的 ADR 文件**至今不存在**。
> ⇒ 本轮把它登记为必须补签的文档债（§13），但它**不是** Bootstrap 的 blocker
> （否则 Package 2 自身也不成立）。

### 3.5 Proposed Exception（精确措辞建议，供 ADR-12 采用）

```
ADR-12（新增编号，显式补充/取代关系）

1) 对 ADR-5：
   ADR-5 **本体**（不自动创建 ACP User）永久保持，不被取代。
   被取代的是其在「不存在任何显式授权来源」前提下写下的**推论与红线表述**：
     - :296-297「运行时链路不得出现任何创建 Identity 的调用」
       → 补充限定：「……除非该创建发生在**已存在的、由已认证 Tenant Admin 显式开启的
          Bootstrap Arm** 之内，且该 Arm 在同一事务内被原子消费。」
     - :302-303 / :650「❌ 无首条消息自动建 User / Identity」
       → 补充限定：「**自动**的判别标准 = 消息到达时是否存在一条有效 Arm。
          无 Arm ⇒ 行为与今天逐字节一致（Fail Closed）。」
   （CP-1 §3.2/§3.3 已给出该语义辨析与可机器化判别式）

2) 对 Package 2-C §1.5 窄豁免：
   窄豁免从「connection_conversations = V0 唯一的**授权状态**存储」
   扩展为「+ bootstrap_arms = V0 唯一的**授权建立窗口**存储」；
   并声明 bootstrap_arms **不是**授权状态（不参与 find_granted_connection_ids）、
   不是 ACL、不是 Discovery。

3) 对 Cloud V0 冻结：
   若入 V0 → 必须以 release-blocker 记录证成（见下）；
   若不做该记录 → Bootstrap Arm 按 §3.2 进入 post-release backlog（实现在 V0 GA 之后）。
```

### 3.6 Blocker 证成的真实依据（不得编造）

Package 2-C §4（`:204-216`）已原文登记：

```
V0 无法通过 Discovery 授权它
        ↓
除非用户知道其 external_conversation_id 并显式创建 Grant
```

而 Cloud-0.1.5 的 Golden Path（`:15-27`）是
`Sign Up → Connect Agent → Connect Channel → Send WeCom message → First Success`，
**其中没有任何「创建 Identity / Grant」步骤**。
⇒ 在 Package 2 的 default-deny 上线后，零知识用户的**第一条真实消息**无法取得授权，
G5 的 First Success 无路径可达 —— 这正是 §3.1 列举的典型 blocker 形态「**Golden Path 阻断**」。

**但该 blocker 必须由一条显式记录证成**（引用 G5 + Package 2-C §4），
不能由本文件宣称。若项目选择不登记，则 Arm 按 §3.2 归入 post-release backlog。

### 3.7 Required Documentation Change（本轮不执行）

| # | 文档动作 | 性质 |
|---|---|---|
| D-1 | **签发 ADR-12**（新增编号 + 显式取代/补充关系，内容见 §3.5） | **硬前置**（CP-1 已要求） |
| D-2 | 新增一条 **release-blocker 记录**（引用 cloud-v0-release-freeze §3.1 + G5 + Package 2-C §4） | 入 V0 的前置（post-V0 实现则不需要） |
| D-3 | **补签 ADR-11 文件**（既有内容已冻结，文件缺失） | 流程债，建议同批 |
| D-4 | 本文件（CP3-D）作为架构裁决记录 | 已完成 |

### 3.8 O-2 判定

```
CONDITIONAL —— 例外机制存在且可走；不是 BLOCKED。
条件：D-1（ADR-12 签发）为架构生效前置；
      D-2 仅在「要求进入 Cloud V0」时才是前置。
```

---

## 4. O-3 Decision — Bootstrap Arm 操作入口

### 4.1 现有管理面约定（证据）

| 事实 | 证据 |
|---|---|
| 资源面：`POST 201 / GET 200 / PATCH 200 / DELETE 204`；`404/409/422` 由 Application error 映射 | `app/api/management/channels.py:77-85`、`connections.py`、`identities.py` |
| **Connection 子资源先例**：`GET /api/connections/{connection_id}/health` | `app/api/management/connections.py:188-191` |
| **Channel 子资源先例**：`GET /api/channels/{channel_id}/known-conversations` | `app/api/management/connection_conversations.py:141-144` |
| **独立资源先例**：`POST /api/connection-conversations`（Package 2 Step 3） | `connection_conversations.py:89-91` |
| 每请求注入：`TenantScope → service`（`service_provider` 依赖） | `app/main.py:255-313` |
| **不存在**「Verify Connection」端点；`/health` 是 **ACP Agent 探活**（只读 Connection.config + ACPAdapter.probe） | `main.py:269-280`、`connections.py:188-191`、`connections.py:213` 的 `checked_at` |

### 4.2 五方案比较

| 方案 | 判定 | 理由 |
|---|---|---|
| **A. 新增专用 management endpoint** | ✅ **推荐** | 唯一能同时满足「真实 / authenticated / auditable 管理动作」的形态；与既有子资源先例同形 |
| **B. 复用现有 Connection endpoint（PATCH）** | ❌ | Connection 是业务对象；PATCH 语义 = 改 name/status/config。把授权窗口塞进 `config` 直接违反 `connection.py:17-19`（config 不是万能配置容器）与 ADR-2 红线；且无法表达 TTL / 一次性 / initiator |
| **C. 复用 Verify Connection endpoint** | ❌ | **不存在**该端点。与之最接近的 `/health` 是 **GET 探活**：① 探活 ≠ 授权来源（两个语义）；② 让 GET 产生授权状态破坏幂等；③ Cloud-0.1.2.2.2 已把 `/health` 冻结为只读诊断 |
| **D. 前端组合既有 API** | ❌ | 没有任何既有 API 能创建 Arm ⇒ 该方案要么等于「无入口」，要么退化为前端伪造状态（CP-1 明令禁止 hidden flag / 前端态 / localStorage） |
| **E. 其他（DB 直写 / 后台任务）** | ❌ | 不可归属、不可审计，直接违反 CP-1 P4 |

### 4.3 裁决（O-3 = A，Connection 子资源形态）

架构级契约（**不写代码**）：

```
POST /api/connections/{connection_id}/bootstrap-arm
  认证    : 必须呈交有效浏览器会话（O-4）；未认证 → 401（沿用既有 _map_auth_error）
  作用域  : tenant 由认证会话的 tenant_id 决定；channel 由 Connection 决定
            请求体**不接受** tenant_id / channel_id（防 scope 错配；与 Grant API 同纪律）
  输入    : 仅 connection_id（路径参数）
  前置校验: Connection 存在（且在本租户作用域内，否则 404）
  行为    : 同一事务 ① 该 scope 上 ARMED → REVOKED  ② INSERT 新 ARMED
  输出    : 201；回显 arm_id / status / **expires_at**（DB 生成值，app 不计算）
  重复调用: **supersede**（旧窗口失效 + 新窗口开启）；仍返回 201（每次都产生新 Arm 行）
  错误    : 404（connection 不存在/跨租户）/ 401（未认证）/ 403（无认证权威，见 O-4）
  V0 不做 : GET / LIST / PATCH / DELETE（Arm 不是 CRUD 资源，是 Connection 上的一个动作）
```

**为什么是子资源而不是独立资源**：
① Arm 没有独立的 CRUD 生命周期，它是「这条 Connection 上的一个验证窗口动作」；
② 与 `/health` 同形（同为 connection 上的动作品）；
③ 避免引入一个新的「看起来像资源」的名词（ADR-10：默认路径不暴露新授权词汇）。

**UI 文案约束**（沿用 Cloud-0.1.5 G3/G5 纪律）：
端点名是内部 API 名；用户可见文案必须是 **「验证连接 / Verify Connection」**，
**不得**出现 `Arm` / `Bootstrap` / `授权` / `Authorization` 等词汇。

### 4.4 与 O-1 的依赖

O-1（DB 部分唯一索引）是 O-3「重复调用 = supersede」能否被**安全**表达的前提：
没有 DB 约束时，supersede 只是「尽力而为」。二者必须同批实现。

---

## 5. O-4 Decision — AUTH_REQUIRED=False / OSS 语义

### 5.1 现状（证据）

```
app/core/config.py:36-40    AUTH_REQUIRED: bool = False（默认 OSS / 本地单租户）
app/api/auth.py:179-201     无 Cookie 且 auth_required=False → default_tenant_scope()
                            = TenantScope(TENANT_LOCAL)  ← **无任何主体身份**
app/main.py:186-191         tenant_scope_provider(auth_service, auth_required=settings.AUTH_REQUIRED)
app/core/browser_auth.py:31-40  AuthenticatedUser(user_id, tenant_id) —— 已认证主体事实已存在
app/api/auth.py:130-146     GET /api/auth/me + register/login 路由**始终挂载**（不受 AUTH_REQUIRED 影响）
```

⇒ 关键事实：OSS 部署中**操作者仍可注册/登录**并取得 cookie；`AUTH_REQUIRED=False`
只影响「无 cookie 时是否 401」，**不**关闭认证能力本身。

### 5.2 五方案比较

| 方案 | 判定 | 理由 |
|---|---|---|
| **A. Anonymous / NULL initiator** | ❌ 禁止 | CP-1 P4：initiator 是**合法性的组成部分**。NULL initiator ⇒ 无法回答「谁允许的」⇒ Authorization Source 不可归属 ⇒ Bootstrap 退化为「任何能调用 API 的人都能开窗口」 |
| **B. System / sentinel user** | ❌ 禁止 | `domain/identity.py:13` 已禁止 sentinel；sentinel initiator 与「可归属」直接冲突 |
| **C. Existing User without auth**（请求里带 user_id） | ❌ 禁止 | 请求体中的 `user_id` 可任意伪造 ⇒ 等价于匿名，且伪造了一个「看起来可归属」的审计字段（比 A 更危险） |
| **D. Disable Arm** | ✅ **采用（精确化后）** | 无认证权威 ⇒ Arm 完全不可用 |
| **E. 其他** | ❌ | 任何新主体（本地操作员实体）= 新范围，须独立裁决 |

### 5.3 裁决（O-4 = D，精确化为「认证来源」而非「部署开关」）

```
Arm 的 initiator 必须来自一次成功的 BrowserAuthService.verify(cookie)
——即 AuthenticatedUser(user_id, tenant_id)。

❌ 永不回退到：
   - 匿名 / NULL
   - guest / system / sentinel（含 sentinel UUID、user_id=0/-1、固定常量）
   - 请求体携带的 user_id
   - AUTH_REQUIRED=False 时的 TENANT_LOCAL 缺省身份

⇒ 精确语义：判定条件不是「AUTH_REQUIRED 为真」，而是「本次请求是否呈交了有效会话」。
   因此：
     · Cloud（AUTH_REQUIRED=True）：无 cookie → 401（既有行为）
     · OSS 已登录操作者：有 cookie → **可以 Arm**（initiator = 该真实 User）
     · OSS 未登录（无 cookie）：Arm **完全不可用** → 401（不是 TENANT_LOCAL 回退）
```

**实现形态**：Arm 端点**不复用** `tenant_scope_provider` 的 OSS 回退，而是新增一个
「`AuthenticatedUser` 提供者」依赖（与 `build_tenant_scope_provider` 同形，只是**不回退**）。

### 5.4 直接后果（Contract 层）

```
initiator_user_id  NOT NULL  —— 无默认值、无 sentinel、无「系统」兜底
```

（CP-3 §7.4 曾将其列为 nullable 候选；本轮依据 O-4 收紧为 **NOT NULL**：
Arm 只在存在真实认证操作者时才能被创建，因此该列**不可能**为空。）

### 5.5 OSS 未登录时「第一条消息」怎么办？

**与今天完全一致（Fail Closed）**，且 OSS 有既有替代路径：

```
管理面显式创建 Identity（user_id 必填，既有 API）
      +
管理面显式创建 Grant（已知会话键，既有 API）
      ⇒ 第一条消息正常成功
```

⇒ Arm 在 OSS 未登录场景不可用 **不是能力缺口**，而是「没有可归属主体就不产生授权来源」
这一安全语义的必然结果。

---

## 6. O-5 Decision — Group Chat Bootstrap

### 6.1 现状（证据）

| 事实 | 证据 |
|---|---|
| 群聊会话键 = `chatid`；单聊 = `from.userid` | `app/adapters/channels/wecom_bot.py:45-62`（docstring）、`:393-400`（实现） |
| **`chattype` 不进 `InboundInteraction`**（契约恒 6 字段） | `app/domain/inbound_interaction.py:57-66`；`wecom_bot.py:408-415` 只把 `bot_reply` 放进 metadata |
| 协议知识**只停留在 Adapter 层** | `wecom_bot.py:116-120`：「`req_id` 只作为 transport metadata 流转，不新增任何契约字段」 |
| **`wecom`（HTTP webhook）只支持 `chattype=single`，群聊 → None** | `app/adapters/channels/wecom.py:36-37`「只支持 text 单聊」、`:365-366`、`:379-380` |
| 授权作用域 = Conversation；群成员共用同一条授权；Grant 无有效期 | ADR-3（`:175-188`、`models/connection_conversation.py:36-39`） |

⇒ 群聊 Bootstrap 只可能发生在 **`wecom_bot`**；`wecom` 天然不可能（adapter 已 ignore）。

### 6.2 四方案比较

| 方案 | 判定 | 理由 |
|---|---|---|
| **A. 允许群聊消费 Arm** | ❌ | Arm 语义 = 「下一个未知会话获得一次机会」。群聊下该会话 = **整个群**：一次 arm 可能把一个管理员从未预期、成员集合未知且**动态变化**的群，授出一条**无有效期**（Grant 禁止 `expires_at`）的长期授权。这最严重地破坏 CP-1 的**精准作用域（P3）**，且事后无自动回收 |
| **B. V0 禁止群聊消费 Arm** | ✅ **推荐** | 把 Arm 的未知量收敛为「下一条**单聊**会话」（= 一个外部身份）；群聊回到既有显式路径（admin 知道 `chatid` 后创建普通 Grant），与今天**逐字节一致**；不引入 temporary/special grant；不改 ADR-3、不改 Grant 模型 |
| **C. 允许 + 额外 challenge** | ❌（V0） | = S-2 加固，CP-3 §15 已定为**独立后续 Gate**；在 V0 引入会同时引入 challenge 状态与 UX 成本，范围失控 |
| **D. 其他** | ❌ | 无需新授权模型（不得引入 temporary Grant，用户明令） |

### 6.3 裁决（O-5 = B）+ 判别事实的来源（关键）

```
V0：Arm 只能被「单聊会话」消费。
群聊（external_conversation_id = chatid）的入站消息：
    · 若该会话已有显式 Grant → 正常运行（不受任何影响）
    · 若没有 → Arm 对该消息 **no-op**（Arm 保持 ARMED，可留给后续单聊消息）
      ⇒ 不消费、不报错、不产生授权（与 CP-3 §11.2「输家/不适用路径」同形）
```

**判别事实必须显式，不得推断**：

```
✅ 由 Adapter 以 channel-originated metadata 携带 chat_type
     （与 BOT_REPLY_METADATA_KEY / response_url 完全同形：
       走既有 metadata 通路，**不改 InboundInteraction 的 6 字段契约面**）
     Bootstrap 判定：metadata["chat_type"] == "single" 才允许消费
     缺失 / 未知 / 非 single → **不允许消费**（不猜、不误判为单聊
       —— 与 inbound_from_callback 对未知 chattype 的 acknowledge-ignore 同纪律）

❌ 禁止在 Core 用「external_conversation_id != external_identity_id ⇒ 群聊」启发式
     理由：那是 wecom_bot 的渠道映射知识，属 Adapter 层
     （wecom_bot.py:116-120 明文：协议知识只停留在本模块）
```

### 6.4 必须同时声明（防止误读）

```
「Bootstrap 不支持 group」  ≠  「ACP 不支持 group」

· 群聊入站、群聊 Session、群聊 Grant、群聊 Agent 执行与投递 —— 全部照旧
· ADR-3 的「群 → Connection」授权模型 —— 一字不改
· 管理员仍可为某个 chatid 显式创建普通 Grant（既有管理面能力，Package 2 Step 3）
· 唯一变化：Arm 这个**新的授权建立窗口**不对群会话开放
```

### 6.5 实现前置（非架构未决）

O-5=B 需要在 Adapter 侧新增一个 metadata key（`chat_type`）。这属
「3B-3.3 仅字段映射」同形的**最小 Adapter 扩展**，需与 ADR-12 一并授权；
**不改 8.1 契约字段面、不改 Domain、不改 Runtime**。

---

## 7. Decision Dependencies

```
        ┌──────────── O-2（例外机制 / ADR-12）───────────┐
        │                                                │
        ▼                                                ▼
   O-1（DB 权威不变量）──────► O-3（操作入口 = supersede 可用）
                                        │
                                        ▼
                                  O-4（initiator 语义 → NOT NULL）
                                        │
                                        ▼
                                  O-5（消费范围 = 单聊）

O-2 ──► migration 实现（0008？）
O-4 ──► runtime 消费路径的 initiator 无关，但 arm 写入路径的 NOT NULL 约束
O-5 ──► WeCom 首条成功语义（单聊可达；群聊需显式 Grant）
```

### 7.1 Implementation Blockers（明确列出）

| # | 项 | 是否阻塞开工 | 说明 |
|---|---|---|---|
| **B-1** | **ADR-12 签发** | ✅ **阻塞** | CP-1 §1：签发前该来源「admissible but **not yet lawful**」；且 ADR-5 红线字面冲突必须书面补充 |
| **B-2** | **release-blocker 记录** | ⚠️ **仅在「入 Cloud V0」时阻塞** | 若安排在 V0 GA 之后实现，则不需要（走 post-release backlog 即可） |
| **B-3** | O-1 部分唯一索引的方言可行性确认 | ✅ 阻塞（migration 步） | PG / SQLite 双跑验证；不可行须回 Gate |
| **B-4** | O-5 的 Adapter metadata 扩展授权 | ✅ 阻塞（消费判定） | 与 ADR-12 同批授权即可，无独立架构未决 |
| B-5 | ADR-11 文件补签 | ❌ 不阻塞 | 流程债，建议同批 |

**除 B-1~B-4 外，无其他卡实现的未决项。** O-1 / O-3 / O-4 / O-5 的**架构内容**本轮已冻结。

---

## 8. Final Bootstrap Arm Contract

### 8.1 字段逐项裁决

| 字段 | 分类 | 约束 | 理由 |
|---|---|---|---|
| `id` | Required | PK | — |
| `tenant_id` | **Required** | NOT NULL, FK `tenants.id`, 索引, RLS 谓词列 | 全表纪律；绝不从 Connection 推导（`connection_conversation.py:10-11`） |
| `channel_id` | **Required** | NOT NULL, FK `channels.id` | scope 组成 + 复合 FK |
| `connection_id` | **Required** | NOT NULL | scope 组成 + 复合 FK |
| `initiator_user_id` | **Required** | **NOT NULL**, FK `users.id`（NO ACTION） | O-4 裁决：Arm 只在有真实认证操作者时存在 ⇒ 不可能为空 |
| `status` | **Required** | NOT NULL, ∈ {`armed`, `consumed`, `revoked`} | 生命周期 |
| `armed_at`（`created_at`） | **Required** | NOT NULL, **DB 生成** `server_default=func.now()` | 全表纪律；app 不生成时间 |
| `expires_at` | **Required** | NOT NULL, **DB 计算**（`now() + interval`） | CP-3 §8；app 只传间隔 |
| `consumed_at` | **Required**（nullable until consumed） | 消费时由 **DB** 写入 | 未消费 = NULL |
| `consumed_by_interaction_id` | **Required**（nullable until consumed） | int，**无 FK** | 追溯「被哪条 Interaction 消费」；不加 FK 以免与 Interaction 生命周期互锁 |
| `consumed_external_identity_id` | **Required**（nullable until consumed） | str，**无 FK** | 与 (tenant, channel) 一起 = Identity 的 UNIQUE 键 ⇒ 可精确反查「创建了哪条 Identity」 |
| `consumed_external_conversation_id` | **Required**（nullable until consumed） | str，**无 FK** | 与 (tenant, channel, connection) 一起 = Grant 的四元组 UNIQUE 键 ⇒ 可精确反查「创建了哪条 Grant」 |
| `updated_at` | **Required** | NOT NULL, DB 生成 | 全表纪律 |
| `agent_id` | **Derived** → **Rejected 列** | — | Connection 绑定不可变 ⇒ (tenant, channel, connection) ⇒ agent 唯一确定；存 = 第二个事实源 |
| `identity_id` | **Rejected** | — | 见 §8.2 |
| `grant_id` | **Rejected** | — | 见 §8.2 |
| `revoked_at` / `revoked_by` | **Optional / Deferred** | V0 不建 | supersede 已由「新行 + 旧行 `status=revoked` + `updated_at`」表达；若未来需要「谁撤销的」再单独裁决 |
| `challenge_hash` / `challenge_algorithm` | **Deferred / Reserved** | V0 不建 | S-2 加固的预留扩展位（CP-3 §15.4） |
| `external_conversation_id`（arm 时预填） | **Rejected** | — | Arm 的语义就是「下一条**未知**会话」；预填会话键 = 退化成普通 Grant 创建 |

### 8.2 `identity_id` / `grant_id` 是否真的不应该 FK？—— **是，且不建列。**

两条独立理由：

```
① 若加 FK（本仓风格 = NO ACTION）：
     删除（撤销）那条 Grant 会被 Arm 行阻断
     ⇒ 违反 ADR-9「允许的集合必须可增可减」/「授权必须可撤销」的既有裁决
   （不加 FK 的裸 id 也不行：授权可被删除后重建，id 会变化 ⇒ 裸 id 会漂移/指错）

② 用两个外部裸键反查反而更稳定：
     Identity = UNIQUE(tenant_id, channel_id, external_id)      （models/identity.py:30-35）
     Grant    = UNIQUE(tenant_id, channel_id, external_conversation_id, connection_id)
                                          （models/connection_conversation.py:59-63）
   两者都是「外部世界的 ID 只在自己的 Channel Scope 内解释」纪律的一等键
   （interaction_record.py:19-28）
```

⇒ **结论：不建 `identity_id` / `grant_id` 列；审计答案由两个裸键 + 既有 UNIQUE 反查给出。**

### 8.3 约束集（冻结）

```
UNIQUE(tenant_id, channel_id, connection_id) WHERE status='armed'   ← O-1（部分唯一）
FK tenant_id  → tenants.id
FK channel_id → channels.id
FK initiator_user_id → users.id（NO ACTION）
复合 FK (tenant_id, channel_id, connection_id) → connections(tenant_id, channel_id, id)
索引 ix_bootstrap_arms_tenant_id
RLS：ENABLE + FORCE + tenant_isolation（NULLIF 形态，与 0007:176-183 同形）
```

### 8.4 组件边界（沿用 CP-3 §18，本轮确认不变）

```
唯一运行时创建点 = app/application/bootstrap.py
  允许写：uow.bootstrap_arms.* / uow.identities.create / uow.connection_conversations.create
  白名单外的 Executor（identity_resolution / access_authorization / routing /
  session_resolution / session_creation / agent_dispatch / delivery / coordinator）
  ⇒ 零创建调用（ADR-5 守卫扩展 G-1~G-5）
RUNTIME_EXECUTION_ORDER 不变（7 段）；不新增 STAGE_* / failure_code
```

---

## 9. Lifecycle（冻结）

```
                    arm（已认证 Admin，经管理面端点）
   UNARMED ───────────────────────────────────────────► ARMED
                                                          │
        ┌─────────────────────────────────────────────────┼──────────────┐
        │ 消费（单聊 + Trust Gate PASS + CAS 赢 + 建立授权事实）        │
        ▼                                                  │             │
    CONSUMED（终态）                    expires_at ≤ now()  │    supersede / 撤销
                                                           ▼             ▼
                                                     EXPIRED（逻辑）   REVOKED（终态）
```

### 9.1 状态名与理由

| 状态 | 是否入库 | 说明 |
|---|---|---|
| `armed` | ✅ | 唯一可被消费的状态 |
| `consumed` | ✅ 终态 | 与 Identity/Grant 的建立**同事务**产生，不可回退 |
| `revoked` | ✅ 终态 | supersede 或显式撤销 |
| **`expired`（逻辑）** | ❌ **不建状态值** | 过期由 `expires_at ≤ now()` 谓词判定；不写状态、不删行。理由：① 避免后台清扫任务；② 判定与消费同 SQL ⇒ 无 TOCTOU；③ 审计留存（CP-3 §13） |

### 9.2 不变量

```
INV-1  同一 (tenant_id, channel_id, connection_id) 至多一条 status='armed'   ← DB 保证（O-1）
INV-2  CONSUMED / REVOKED 是终态，不可回到 ARMED
INV-3  存在 CONSUMED 行 ⇒ 该事务确实建立了 Identity 与/或 Grant（CP-3 §9.4）
INV-4  EXPIRED 不产生任何写入；零 Identity / 零 Grant
INV-5  未通过 Trust Gate ⇒ 不进入本状态机（CP-2）
```

---

## 10. Transaction Semantics（冻结）

### 10.1 Arm 写入（管理面）

```
BEGIN（tenant-scoped UoW）
  ① UPDATE 该 scope 上 status='armed' → 'revoked'
  ② INSERT status='armed', expires_at = now() + interval, initiator_user_id = 认证主体
COMMIT
（撞 O-1 唯一约束 → 重试一次 ①+②）
```

### 10.2 Arm 消费 + Identity + Grant（**同一短事务**）—— 确认 CP-3 推荐

```
BEGIN
  ├─ 校验 Arm（同一 scope / status='armed' / expires_at > now()）
  ├─ 校验消费范围：chat_type == 'single'（O-5）
  ├─ CAS 消费：UPDATE ... SET status='consumed', consumed_at=now(),
  │             consumed_by_interaction_id=:iid, consumed_external_*=:keys
  │            WHERE id=:arm_id AND status='armed' AND expires_at > now()
  │            → rowcount==0 ⇒ 走「不适用/输家」路径（回读复用；不消费、不新建）
  ├─ find-or-create Identity（user_id = NULL；冲突 → 回读复用）
  ├─ find-or-create Grant（普通 ConnectionConversation；冲突 → 回读复用）
COMMIT
（事务外）Identity Resolution → Authorization → Routing → Session → Agent → Delivery
```

### 10.3 Agent 失败 —— 确认不回滚授权事实

```
Arm CONSUMED + Identity + Grant 已 COMMIT
        ↓
Agent / Delivery 失败
        ↓
按既有映射落 AGENT_DISPATCH_FAILED / CHANNEL_DELIVERY_FAILED（retryable=True）
        ↓
授权事实**保持**（不回滚、不补偿、不删 Grant）
        ↓
下一条消息：Grant 已存在 ⇒ 直接成功，无需重新 arm
```

**必须写入实现文档的诚实表述**（CP-3 §10.3）：
Arm 保证「**授权已建立**」，**不**保证「首条消息必然成功」
（First Success = `processing=succeeded × delivery=delivered`，Cloud-0.1.5 `:42`）。

---

## 11. Security Invariants（可机器化）

```
SI-1  无 Arm ⇒ 行为与今天逐字节一致（Fail Closed / IDENTITY_RESOLUTION_FAILED）
SI-2  Trust Gate FAIL ⇒ 零 Interaction / 零 Identity / 零 Grant / 不入 Bootstrap（CP-2）
SI-3  无认证操作者 ⇒ 不能 Arm（O-4）；initiator_user_id NOT NULL，无 sentinel
SI-4  同一 scope 至多一条 ARMED（O-1，DB 权威）
SI-5  消费与授权建立同事务 ⇒ 「CONSUMED 且无授权」不可达
SI-6  一次 Arm 至多兑现一次（CAS rowcount==1）
SI-7  TTL 由 DB 时钟判定，且与消费同 SQL（无 TOCTOU、跨实例一致）
SI-8  Bootstrap 创建的 Identity 恒 user_id = NULL（绝不绑定 Tenant Admin）
SI-9  Bootstrap 只创建**普通** Grant（无特殊类型、无有效期、无 status）
SI-10 群会话不消费 Arm（O-5）；群聊其余能力不变
SI-11 Arm 只在 bootstrap.py 一个模块内被写/被消费（ADR-5 守卫 G-1~G-5）
SI-12 不得经 Connection.config / Activity / metadata / 内存 / 前端态 表达 Arm（O-2）
SI-13 Arm 表不参与 find_granted_connection_ids（不是授权状态）
```

---

## 12. CP-4 Boundary（本轮不实现）

CP-4 必须验证（10 项，逐条对应）：

| # | 验证项 | 期望 |
|---|---|---|
| 1 | concurrent first messages | 恰好 1 条 Identity + 1 条 Grant；N 条 interaction 全部到达终态（无 `SESSION_RESOLUTION_FAILED`） |
| 2 | Arm CAS | 赢家 1 个；输家回读复用后继续（不报 `UNEXPECTED_FAILURE`） |
| 3 | Identity find-or-create | `IntegrityError` → 回读复用（M1）；`user_id` 恒 NULL |
| 4 | Grant find-or-create | `IntegrityError` → 回读复用（M2）；普通 Grant |
| 5 | Session conflict read-back | 唯一约束冲突 → `find_active` 回读（M3，既有缺陷） |
| 6 | duplicate external message | 重放同 `external_interaction_id` ⇒ 不二次 Bootstrap、不二次 Agent |
| 7 | worker reclaim | stale reclaim 后重放 ⇒ 不新增 Identity / Grant / Arm 消费 |
| 8 | multi-worker | 同进程多线程并发 ⇒ 仍至多一次 |
| 9 | multi-instance | 两个独立 UoW 并发 ⇒ DB 串行化；至少以双 UoW 模拟 |
| 10 | transaction rollback | Identity/Grant 失败 ⇒ 事务回滚 ⇒ Arm **回到 ARMED**（可 retry） |

附加（本轮新增的裁决对应验证）：

```
11  O-1  第二条 ARMED 插入必须失败（SQLite + PG 双跑同一断言）
12  O-4  无 cookie ⇒ arm 端点 401；initiator_user_id 无 NULL 行
13  O-5  群聊（chat_type=group）入站 ⇒ Arm 保持 ARMED，零授权写入
14  SI-11 全仓 uow.identities.create / connection_conversations.create 的出现点 == 白名单
```

---

## 13. Required Documentation Changes

| # | 动作 | 阻塞关系 | 本轮是否执行 |
|---|---|---|---|
| D-1 | **签发 ADR-12**（新增编号 + 显式取代/补充关系；内容见 §3.5） | **B-1 硬前置** | ❌（本轮禁改/新增 ADR） |
| D-2 | **release-blocker 记录**（引用 cloud-v0-release-freeze §3.1 + G5 + Package 2-C §4） | B-2（仅入 V0 时） | ❌ |
| D-3 | **补签 ADR-11 文件**（内容已冻结，文件缺失） | B-5（不阻塞） | ❌ |
| D-4 | 本文件（CP3-D 裁决记录） | — | ✅ |
| D-5 | 实现期随迁移同步更新的**精确集合类守卫**（见 §14） | 实现期 | ❌ |

**明确不改**：`cloud-v0-release-freeze.md` 本体、ADR-1~ADR-10 原文、Package 2-C、
Gate 2.6 —— 例外一律以「新增编号 + 显式取代关系」表达（ADR `:702-703` 的法定方式）。

---

## 14. Implementation Preconditions

### 14.1 会被新表 / 新模块**必然打破**的既有精确断言（实现期必须同批增补）

| 守卫 | 位置 | 影响 |
|---|---|---|
| `CORE_TABLES` + `Base.metadata.tables` **精确等值** | `tests/test_cloud_0_1_1_b_tenant_isolation_dod.py:44-58` | 新表 `bootstrap_arms` → 必破，须加入集合 |
| `test_fresh_pg_after_upgrade`（PG-gated，精确表集合） | `test_cloud_0_1_1_a::…` | 同上 |
| `EXPECTED_APPLICATION_FILES`（精确等值） | `test_step8_2::` | 新 application 模块 → 必破 |
| ADR-5 守卫扫描面（固定 4 文件） | `test_adr_authorization_and_access_model.py:284-298` | **必须扩展**（CP-1 §11.4：否则会出现「通过守卫但实质违反 ADR-5」） |
| RLS / tenant 隔离 DoD 的新表谓词 | `0007:176-183` 形态 | 新表必须自带 RLS |

### 14.2 实现顺序建议（不属于本轮裁决，仅供实现 Agent）

```
① D-1（ADR-12 签发）+ D-3（ADR-11 补签）
② ORM 模型 + 0008 迁移（PG RLS + SQLite create_all 两条路径）+ 部分唯一索引方言验证（B-3）
③ Port / Adapter / Mapper + UoW 暴露
④ 管理面端点（O-3）+ AuthenticatedUser 提供者（O-4）
⑤ Adapter metadata 携带 chat_type（O-5 / B-4）
⑥ bootstrap.py（执行边界，Worker 之前）+ Runtime 零改动验证
⑦ M1 / M2 / M3 + Arm CAS + 并发 / 重放 / reclaim / 多实例
⑧ 守卫扩展（G-1~G-5 + SI-1~SI-13 中选可机器化者）
```

---

## 15. Final Gate

```
CONDITIONAL PASS
```

### 15.1 为什么不是 BLOCKED

Bootstrap Arm 在现有 ACP Authorization Model 中**可以安全成立**：

- 授权来源仍是**显式管理动作**（Arm），不是消息本身 ⇒ 与 ADR-6「Discovery ≠ Authorization」一致；
- 创建的仍是 **External Identity**（`user_id=NULL`）+ **普通 Grant** ⇒ 与 ADR-5 本体、ADR-3、ADR-9 一致；
- 冲突只发生在 ADR-5 的**推论/红线字面表述**，而 ADR 文件**自身规定了合法出口**
  （`:702-703`：新增编号 + 显式取代关系）⇒ 有机制、有先例（Package 1 / Package 2 各走过一次）。

### 15.2 为什么不是 PASS

存在 **1 项硬前置 + 3 项实现前置**，其中 B-1 在 CP-1 即被定义为
「签发前该来源 admissible but **not yet lawful**」：

```
B-1  ADR-12 签发                    ← 硬前置（跨 CP-1 / CP-3-D 未变）
B-2  release-blocker 记录           ← 仅在「入 Cloud V0」时前置
B-3  O-1 部分唯一索引方言可行性      ← migration 步前置
B-4  O-5 Adapter metadata 扩展授权   ← 消费判定前置（可与 ADR-12 同批）
```

### 15.3 升为 PASS 的条件

```
① ADR-12 已签发（含 §3.5 的三段取代/补充措辞）
② B-3 / B-4 在实现步随 ADR-12 一并授权
③ （若要求入 V0）D-2 blocker 记录已登记
⇒ 三条齐备即 PASS，可进入 M6.6-C3-CP3-IMPLEMENTATION
```

### 15.4 本轮未做（只读声明）

```
❌ 未创建 ADR-12 / 未补签 ADR-11（本轮禁改既有 ADR、禁新增 ADR）
❌ 未创建 migration / 表 / 端点 / 组件 / 测试
❌ 未修改 Runtime / Identity Resolver / Authorization / Router / Session / Adapter
❌ 未修改 Cloud 冻结文档 / Package 2-C / Gate 2.6
❌ 未创建测试数据、未部署、未 commit / tag
```

**本文件为架构裁决产出，不构成实现批准。**
实现批准以 §15.3 三条为前置。
