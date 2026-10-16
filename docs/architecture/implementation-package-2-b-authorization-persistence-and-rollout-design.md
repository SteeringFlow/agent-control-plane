# Implementation Package 2-B — Authorization Persistence & Rollout Design

> 状态：**只读设计**。未改生产代码、未改测试、未建 migration、未改 schema、未改冻结契约。
> 范围：**只裁决 Grant persistence 的最终边界**（B1 – B7）。不再审整个 ACP。
> 继承：ADR-1 ~ ADR-10（FROZEN）、Gate 1 / 2 / 2.5 / 2.6 / 3、Package 1（`f43689a`）、
> Gate 2-A（BLOCKER + 最小持久化边界提案 + M1~M7 待修订清单）。
>
> 本文件把 Gate 2-A 的「形状提案」升级为**可直接实现的最终设计**：
> 逐列 / 逐约束 / 逐动作 / 逐回填条件 / 逐文件 NO-CHANGE 清单。

---

## 0. 结论摘要

| # | 问题 | 裁决 |
|---|---|---|
| **B1** | Grant 表最终 schema | 1 张表 **6 列**（`id` + 4 语义列 + 2 时间戳）；1 PK / 1 UNIQUE / **1 复合 FK** / 1 普通 FK / 2 索引；**零 CHECK**（与既有 12 表同纪律） |
| **B2** | Tenant consistency | **由 DB 复合 FK 直接防止**：`(tenant_id, channel_id, connection_id) → connections(tenant_id, channel_id, id)`（需给 `connections` 加 1 个**加性** UNIQUE；已核实不打破任何守卫） |
| **B3** | Connection lifecycle | **阻断式**（无 `ondelete` = NO ACTION）；`status → inactive` **不触碰 grants**；拒绝 cascade / 拒绝 explicit cleanup |
| **B4** | Grant Management API | 从真实 UI 生命周期推导：**Create / Delete / List = 必需**；**Update = 不需要**；**+1 个只读候选会话面**（ADR-10；实现时点待裁） |
| **B5** | Default-deny rollout | migration **0007 内原子完成** schema + RLS + 回填；回填源 = `sessions`；7 个边界条件逐条裁决 |
| **B6** | Multi-Connection semantics | **ALLOW + eligible={C1,C2} → ROUTING_FAILED**，与现有 `if len(connections) != 1` 完全一致（附 4 行真值表） |
| **B7** | Full change inventory | 13 个区域的逐项 NO CHANGE / CHANGE 清单（见 §B7） |

**一句话**：2-B 把「一张表 + 一个复合 FK + 一次原子回填 + 三个管理动作」定为 Package 2 的
全部持久化边界，其余 13 个区域里的 9 个明确 **NO CHANGE**。

---

## B1 — Grant 表最终 schema

### B1.1 表（最终形态）

```
connection_conversations                                  ← 名称理由见 B1.4
─────────────────────────────────────────────────────────
  id                          INTEGER  PK AUTOINCREMENT       NOT NULL
  tenant_id                   INTEGER  FK tenants.id          NOT NULL  idx
  channel_id                  INTEGER  FK channels.id         NOT NULL
  external_conversation_id    VARCHAR(255)                    NOT NULL
  connection_id               INTEGER                         NOT NULL  idx
  created_at                  TIMESTAMPTZ  server_default now()   NOT NULL
  updated_at                  TIMESTAMPTZ  server_default now()   NOT NULL
─────────────────────────────────────────────────────────
  PK      (id)
  UNIQUE  (tenant_id, channel_id, external_conversation_id, connection_id)
          uq_connection_conversations_tenant_channel_conv_conn        (52 chars ≤ 63)
  FK-1    (tenant_id) → tenants(id)                    [与 8 张业务表同纪律]
  FK-2    (channel_id) → channels(id)                  [scope 键的一半]
  FK-3c   (tenant_id, channel_id, connection_id)
              → connections(tenant_id, channel_id, id)  [复合 FK：结构性一致性，见 B2]
  INDEX   ix_connection_conversations_tenant_id         (37 chars)
  INDEX   ix_connection_conversations_connection_id
  CHECK   无（与既有 12 张表零 CHECK 一致）
```

### B1.2 逐列必要性证明（不多一列）

| 列 | 为什么必要 | 少它会怎样 |
|---|---|---|
| `id` | 与其他 12 表同形；DELETE 的定位键（B4） | 无法用行 id 删除 → 只能用三元组做路径参数（外部会话键可含特殊字符） |
| `tenant_id` | ① 与 8 张业务表同纪律；② **RLS 需要自有列**（不能经 join 推导，与 `identities` 同一处理） | RLS 第二层防线无法直接成立 |
| `channel_id` | ADR-3 作用域键的一半（会话键只在 Channel 内解释） | 群 `CHAT-G1` 与另一渠道的同名群无法区分 |
| `external_conversation_id` | ADR-3 作用域键的另一半 | 无作用域主语 |
| `connection_id` | ADR-2 / ADR-8 的目标域 | 没有授权对象 |
| `created_at` / `updated_at` | 12 表统一的时间戳纪律（`server_default now()`） | 漂移 |

**明确不需要的列**（ADR-9 / ADR-3 / ADR-1 红线）：

```
❌ status / effect / priority / weight / expires_at / conditions   （纯 allow 关系；deny = 缺行）
❌ identity_id / user_id                                          （ADR-1/ADR-3：作用域不得下沉）
❌ connection 的冗余 agent_id                                      （可经 Connection 派生，违反单一事实源）
❌ conversation 实体外键                                           （ADR-3：Conversation 不是 ACP 实体）
```

### B1.3 Nullability（逐列）

```
id                        NOT NULL   （PK）
tenant_id                 NOT NULL   （RLS 谓词列，绝不可空）
channel_id                NOT NULL   （作用域键；空 = 无意义的授权）
external_conversation_id  NOT NULL   （同上）
connection_id             NOT NULL   （授权目标；空 = 无意义的授权）
created_at / updated_at   NOT NULL
```

**空串不由 DB 拦**：既有 12 张表**零 CHECK 约束**，非空校验的应用层先例是
`_require_non_blank(...)` → `InvalidIdentityInputError` → 422（`app/application/identities.py:81-83`）。
本设计沿用该先例（DB 只兜 `NOT NULL`），并在 B4 的 422 契约中体现。

### B1.4 索引的**最小性论证**（为什么恰好两个）

| 索引 | 服务什么真实查询 | 为什么不是冗余 |
|---|---|---|
| `ix_connection_conversations_tenant_id` | RLS policy 扫描 + 租户作用域过滤 | 与 8 张表的 `ix_<t>_tenant_id` 同纪律（`test_cloud_0_1_1_b_tenant_architecture_gate:195-199` 已把该纪律机器化） |
| `ix_connection_conversations_connection_id` | B4 的 **List** 读路径（按 Connection 列出「它在哪工作」） | 有真实消费者（连接详情页） |

**刻意不加的索引（并说明为什么被唯一约束覆盖）**：

```
✗ (tenant_id, channel_id, external_conversation_id)  ← 授权判定的主查询
    → 已被 UNIQUE 的**前导列**完整覆盖（leftmost prefix）。
      这与 interaction.py 的既有裁决同纪律：
      「复合唯一索引以 channel_id 为前导列，天然服务 channel 作用域访问」
✗ ix_connection_conversations_channel_id
    → 同被 UNIQUE 前导列 (tenant_id, channel_id) 覆盖
```

**结论：能省的索引必须省；只有 `connection_id` 的那条有独立查询路径，其余由 UNIQUE 前导覆盖。**

### B1.5 命名（及其**流程要求**）

建议名：**`connection_conversations`** —— 用产品语言表达「这条 Connection 在这些 Conversation 上工作」
（ADR-10 的 "Where should it work?"），**不含 ACL 词汇**。

> ⚠️ **流程要求（延续 Gate 2-A §M5 的纪律）**：
> ADR guard 的黑名单里有 `access_grants` / `authorization_decisions`（表）与
> `AccessGrant` / `GrantModel` / `PermissionGrant`（类）。本设计**不采用"挑一个不在黑名单里的名字"这种规避**，
> 而是要求 **ADR-11 显式记录一条窄豁免**：
> ① 声明该表是 V0 **唯一的**授权状态存储；
> ② 说明它**不是** ACL / 权限表（无 effect / 优先级 / principal；作用域 = conversation；deny = 缺行）；
> ③ 在 ADR guard 中以**显式白名单条目 + 注释**的形式落地。
> 之后 implement 才有权建这张表。

---

## B2 — Tenant consistency

### B2.1 用户问的错配，逐种分析

```
Grant.tenant = A
Grant.channel = A
Grant.connection = B        ← 同一租户内、跨 channel 的错配
```

| 错配类型 | 现状能否出现 | 现有防线 | 是否需要结构性修复 |
|---|---|---|---|
| **跨租户**：grant.tenant=A，connection 属 tenant B | 理论可写 | ① RLS（三表都有）；② tenant-scoped UoW（repository 根本看不见 B 的连接 → 存在性校验必失败） | 值得**同时**由 DB 拦（纵深） |
| **跨 channel（同租户）**：grant.channel=X，connection 属 channel Y | **可以出现** | **无**（RLS 对同租户不生效；`connection_id` 单列 FK 不携带 channel） | ✅ **必须由 DB 拦** |
| **跨租户的 channel**：grant.tenant=A，channel 属 tenant B | 理论可写 | 同跨租户一栏 | 由 composite FK 的 tenant 列一并覆盖 |

**结论：跨 channel 错配是 RLS 无法覆盖的真实空洞**（同租户内），
这就是 B2 要求「DB constraint 直接防止」的正当性来源。

### B2.2 裁决：**采用复合 FK**

```
FK-3c  (tenant_id, channel_id, connection_id)
           → connections(tenant_id, channel_id, id)
```

**一个约束同时消灭两种错配**：grant 的 `tenant_id` 与 `channel_id` 必须与所引用 Connection 的
那两个列**逐字节相同**。

代价（唯一一处）：`connections` 需要一个被引用的唯一键：

```
connections:  + UNIQUE(tenant_id, channel_id, id)   name = uq_connections_tenant_channel_id
```

**为什么这个代价可接受（已实测核对，非推测）**：

| 检查 | 结果 |
|---|---|
| `test_models.py::test_b_connections_schema`（`:144-154`） | 只断言**列清单**与 `uq_connections_tenant_channel_agent_id` 的存在/列集（`uqs = [...]` 后取首个）→ **加约束不影响** |
| `test_step10_6_1::test_a3`（`:190-200`） | 用 `any(...)` 成员判定 → **不影响** |
| `test_cloud_0_1_1_b_tenant_architecture_gate::test_unique_constraints_lead_with_tenant_id`（`:179-192`） | 用 `in uc["connections"]` 成员判定 → **不影响** |
| `test_cloud_0_1_1_b_tenant_isolation_dod::test_dod_schema...`（`:69-72`） | 用 `any(...)` → **不影响** |
| `connections` 列清单 | **不长列**（纯约束）→ 不触及任何列清单断言 |

### B2.3 必须诚实说明的**不可闭合部分**

```
grant 表的结构一致性 ≤ connections 自身的结构一致性
```

`connections.channel_id → channels.id` 是**单列 FK，tenant-blind**：理论上可以存在
「tenant A 的 Connection 挂在 tenant B 的 Channel 上」。grant 表**忠实镜像**它，
不引入新的不一致，但也**无法修好它**。

- 彻底闭合需要再给 `channels` 加 `UNIQUE(tenant_id, id)` 并把 connections 的 FK 升级为复合 FK——
  **那是既有表的既有缺口，不在 Package 2 范围**（本文件明确记录，不顺手改）。
- 现行防线仍是：RLS + tenant-scoped UoW（写路径看不见跨租户行）。

### B2.4 保留 `FK-1 (tenant_id) → tenants.id`

虽然被 FK-3c 语义覆盖，**仍保留**，理由：与 8 张业务表的 `fk_<t>_tenant_id` 命名/形态一致，
且未来守卫若按该模式扫描新表不会漏判。

---

## B3 — Connection lifecycle

### B3.1 裁决：**阻断式（block delete）**

```
connection_conversations.connection_id   → FK 无 ondelete 子句 = NO ACTION（阻断式）
```

**与 Package 1 A-2 同构**（`identities.user_id` → users.id 同样阻断式），理由逐条对齐：

| 选项 | 裁决 | 理由 |
|---|---|---|
| **delete blocked**（推荐） | ✅ | 与既有 FK 纪律 + Package 1 先例一致；「保留授权」靠**阻断删除**实现，不靠隐式改写 |
| cascade | ❌ | 删除 Connection 连带删除授权记录 = **隐式授权变化**（且不可逆、无审计痕迹）。明确违反本次要求「不要引入隐式授权变化」 |
| explicit cleanup | ❌ | ① V0 **没有**删除 Connection 的能力（`ConnectionRepository` 无 `delete`；`connections` API 无 DELETE，软下线 = `PATCH status`）→ 无消费者；② 引入第二个写入者/管理动作 |

### B3.2 同一问题的另一半：`status → inactive`

```
Connection 软下线（PATCH status=inactive）
        ↓
Grants 一行都不动（不删、不置空、不软删）
        ↓
下次执行：granted ≠ ∅，但 active ∩ granted = ∅
        ↓
Routing 0 条候选 → ROUTING_FAILED（**不是** UNAUTHORIZED）
```

这是**唯一正确**的语义：授权仍然成立（「这条会话被允许用 C1」是事实），
只是**路径当前不可用**。谎报成 `UNAUTHORIZED` 会把运维引向错误方向。

**推论（写入设计约束）**：`connections.status` 的读写路径**零改动**，
且 grants 表**没有 status 列**（B1.2 已排除）——授权与可运维性彻底正交。

---

## B4 — Grant Management API（从真实 UI / runtime lifecycle 推导）

### B4.1 先看真实生命周期（不假设）

| 真实存在的东西 | 事实 |
|---|---|
| `frontend/connect-agent.html` | 引导式流程：「**一个用户任务，不是 Domain Entity**」= 创建 Agent → 创建 Connection →（可选）探活；页内自述「后端事实始终只有三件：Agent 已创建、Connection 已创建、探针结果」 |
| `frontend/connect-channel.html` | 创建 Channel → 选已有 Agent → `POST /api/connections` 绑定 Access Path → 探针 → 从 `/api/interactions` 推导 First Success |
| `frontend/connections.js` | 只做 `GET /api/connections` + 解耦解析 channel/agent 名称（服务端不 join） |
| 管理面 API | `/api/{agents,channels,connections,identities,users,interactions}` —— **没有任何授权面** |
| ADR-10（FROZEN） | 默认路径必须是 `Agent → Connect → **Where should it work?**` |

**结论**：ADR-10 的「Where should it work?」在真实流程里紧随「Connect」之后，
所以**最小管理动作的本质是：为一条已经存在的 Connection 指定「它在哪些会话上工作」**。

### B4.2 三个**必需**动作 + 一个**不需要**

| 动作 | 必需？ | 论证（不是默认假设） |
|---|---|---|
| **Create**（把会话加进 Connection 的工作范围） | **必需** | default-deny（ADR-6）下，除回填外**没有任何途径**能让新会话获得访问权 → 没有 create，系统只能靠回填工作（新会话永远 DENY） |
| **Delete**（撤销） | **必需** | ADR-9 的模型是「**允许的集合**」——集合必须可增可减；否则配错的授权永久生效，只能改库。且「撤销」是访问控制的**最低**要求 |
| **List**（回显「它在哪工作」） | **必需** | 用户必须看到自己配了什么（ADR-10 的 "Simple by Default"）；且是 Delete 的前置（需要行 id） |
| **Update** | ❌ 不需要 | 行不可变（改 = 删 + 建）；与 `Connection` 绑定不可变同一思想（`domain/connection.py:22-24`） |
| bulk / import / export / priority / effect | ❌ | ADR-9 NON-GOAL |

### B4.3 端点形态（**不**嵌在 Connection 下）

```http
POST   /api/connection-conversations
       body  { channel_id, external_conversation_id, connection_id }
       201 → 行 | 409 已存在 | 404 channel/connection 不存在 | 422 非法输入

GET    /api/connection-conversations?connection_id={id}
       200 → { items: [...], total }

DELETE /api/connection-conversations/{id}
       204 | 404
```

**为什么独立资源而不是 `POST /api/connections/{id}/conversations`**：

- ADR-2 红线：`Connection` 域**不得**长成授权对象（guard `_AUTH_FIELD_FRAGMENTS` 含 `grant`，
  会检查 Connection 的字段与方法名）；把授权做成 Connection 的**子资源**会把二者在 API 面重新耦合成一个对象；
- 独立资源可以直接用**行 id** 做 DELETE 定位（外部会话键可能含 `-` / 空格 / 特殊字符，不适合做路径参数）。

### B4.4 状态码（沿用既有先例，不发明）

| 情形 | 码 | 先例 |
|---|---|---|
| 重复授权（同一四元组） | **409** | `DuplicateIdentityError → 409`（`api/management/identities.py:81-82`） |
| channel / connection 不存在 | **404** | `ChannelNotFoundError / UserNotFoundError → 404`（同文件 `:83-85`） |
| 空串 / 非法类型 | **422** | `InvalidIdentityInputError → 422`（同文件 `:86-87`） |
| 未登录 | 401 | 既有 browser auth |
| 租户作用域 | — | 既有 `tenant_scope_provider` 注入 tenant-scoped service（与 identities 同一注入纪律） |

### B4.5 需要一个**只读**候选会话面（ADR-10 的实现前置）

要渲染 "Where should it work?"，用户得有个**可选项**。ACP 没有 Conversation 实体（ADR-3），
但**可以观察**已出现的会话键：

```http
GET /api/channels/{id}/known-conversations
    → DISTINCT external_conversation_id FROM sessions WHERE channel_id = {id}
```

- **ADR-6 合规**：Discovery 允许被观察（"会话可以被观察到、可以被记录，但**不产生**授权状态"）；
  这是纯读投影，不写任何授权状态；
- **不是**授权输入，也**不进**协调器链路；
- **实现时点待裁**：本文件把它标为 **ADR-10 的实现前置**，但允许 V0 先用自由文本输入过渡
  （那时 UX 会差一档）。**这是一个需要 Review 明示的选择，不是默认包含**。

---

## B5 — Default-deny rollout（本次最大的 migration risk）

### B5.1 裁决 R1：schema + RLS + 回填**在 0007 内原子完成**

```
❌ 不允许拆成「先建表 → 稍后回填」两步
```

**理由（硬）**：表存在但没有任何 grant 的**中间态 = 全量 DENY = 服务中断**。
原子性不是偏好，而是正确性要求。

### B5.2 裁决 R2：回填源 = `sessions`

```sql
INSERT INTO connection_conversations
        (tenant_id, channel_id, external_conversation_id, connection_id)
SELECT DISTINCT
        tenant_id, channel_id, external_conversation_id, connection_id
FROM sessions
WHERE connection_id IS NOT NULL
  AND external_conversation_id IS NOT NULL
  AND btrim(external_conversation_id) <> ''
ON CONFLICT DO NOTHING;
```

**为什么是 `sessions`（而不是别的）**：它是**唯一**同时持有三个键的表
（`channel_id` + `external_conversation_id` + `connection_id`，`models/session.py:40-52`），
且其行即「这个会话确实在这条 Access Path 上工作过」的**事实记录**。

### B5.3 七个边界条件，逐条裁决

| # | 问题 | 裁决 | 理由 |
|---|---|---|---|
| 1 | 回填源 | `sessions` | 唯一持有三键的表（见上） |
| 2 | 回填条件 | `connection_id` / `external_conversation_id` **非空且非空白** | 两列在 `sessions` 里**允许 NULL**（`session.py:43-52`，4.2 未冻结 nullability）→ 必须显式过滤，否则产生无意义授权 |
| 3 | **是否所有历史 Session 都算** | ✅ **全部算，不按 `status` 过滤** | ① 实测：`sessions` 目前**无关闭路径**（只有 `create` / `find_active` / `bind_external_session`），`status` 实际恒为 `'active'`；② 过滤会丢失历史事实。**但 ADR-11 必须记录这个假设**——未来引入 `ended` 状态时，回填语义需重新裁决（否则静默漂移） |
| 4 | **多个历史 Connection** | ✅ 全部保留 → 同一会话可能有多条 grant（C1 旧 / C2 新） | 由 B6 的 `eligible = active ∩ granted` 自然处理；若两条都 active → `ROUTING_FAILED`（既有语义，Fail Closed，不静默选一个） |
| 5 | **inactive Connections** | ✅ **照常回填**（不按 `connections.status` 过滤） | 保留历史事实；结果是 `granted ≠ ∅` 但 `active ∩ granted = ∅` → **`ROUTING_FAILED`**（比 `UNAUTHORIZED` 更准确：曾授权、路径不可用）。若过滤掉则谎报成「未授权」 |
| 6 | **重复授权** | `DISTINCT` + `ON CONFLICT DO NOTHING` | 群聊下**同一会话会有多行 sessions**（每个成员一个 identity → 一条 session）→ 例如 G1 有 3 名成员就有 3 行同三元组；必须收敛为 1 行。唯一约束是最终兜底 |
| 7 | **无 Session 的 conversation** | ❌ **不回填** → rollout 后 = DENY | ① `interactions` **没有 conversation 键**（只有 message 级 `external_interaction_id`）→ 技术上无法回填；② 语义上「从未建立过 Session」= 从未进入过执行 → DENY 是诚实的 |

### B5.4 裁决 R4：空库 / 新部署

```
空库 → 回填 no-op → 全 DENY → 必须先在管理面创建授权（B4 的 Create）
```

这是 **ADR-6 的正确行为**，且与 ADR-10 的引导路径一致：用户先 Connect，再回答
"Where should it work?"。**不得**为了「开箱即用」引入通配授权——
那等于让 Gate 2 的恒 ALLOW placeholder 借回填复活。

### B5.5 回填的 tenant 正确性

- `tenant_id` 逐行复制自 `sessions.tenant_id`（不是 `current_setting`）；
- 三列一致性由 B2 的复合 FK 在 INSERT 时**逐行校验** → 任何不一致的行会让 migration **失败**
  （宁可失败也不写入不一致数据）；
- migration 以 owner/superuser 身份运行（RLS 不适用），与 0002~0006 同。

---

## B6 — Multi-Connection semantics

### B6.1 用户给出的例子 —— **确认一致**

```
Conversation G1
   ├── Grant → C1
   └── Grant → C2

Authorization : ALLOW          （判定不看 Connection 数量 —— Gate 2 §3.2）
eligible      = {C1, C2}
Routing       : ROUTING_FAILED （候选 != 1）
```

### B6.2 与现有代码的一致性证明

`app/application/routing.py:65-66`：

```python
if len(connections) != 1:
    raise RoutingFailedError(channel_id)
```

收窄后的候选集 = `active(channel) ∩ granted(conversation)`，**该判定一字不改**。
契约文本「唯一 active Connection / 否则 Fail Closed」保持字面成立
（`test_adr8_no_multi_agent_routing_expansion` 要求 `"if len(connections) != 1" in src` 与
`RoutingFailedError` 存在 → **两者都保留**）。

### B6.3 完整真值表（授权 × 路由）

| `granted` | `active ∩ granted` | Authorization | Routing | `failure_code` |
|---|---|---|---|---|
| ∅（无任何 grant） | — | **DENY**（不进 Routing） | 不执行 | `UNAUTHORIZED` |
| ≠ ∅ | ∅（grant 指向的连接都不 active） | ALLOW | 候选 0 → fail | `ROUTING_FAILED` |
| ≠ ∅ | 恰好 1 条 | ALLOW | 选中；`agent_id` 派生 | — |
| ≠ ∅ | ≥ 2 条 | ALLOW | 候选 >1 → fail | `ROUTING_FAILED` |

**这张表是 Package 2 的语义核心**：它把「许可」与「选择」彻底正交化——
Authorization 永不裁决数量（Gate 2 §3.2），Routing 永不放宽唯一性（ADR-8）。

### B6.4 明确不做

```
❌ 授权阶段的「多条即 DENY」          （那是把唯一性裁决塞进授权）
❌ 路由阶段的优先级 / weight / fallback（ADR-8 红线）
❌ 按 grant 的创建时间选「最新的一条」 （隐式选择 = Multi-Agent Routing 偷渡）
```

---

## B7 — Full Package 2 change inventory

| 区域 | 变更 | 明细 |
|---|---|---|
| **Schema** | **CHANGE ×2** | ① 新表 `connection_conversations`（B1）；② `connections` **加性** UNIQUE `uq_connections_tenant_channel_id`（B2）。**既有 12 表的列/类型/nullability 一律 NO CHANGE** |
| **Migration** | **CHANGE ×1** | 新增 `0007`（revises `0006`）：建表 + 索引 + 复合 FK + `ENABLE/FORCE ROW LEVEL SECURITY` + `tenant_isolation` 策略（用 0005 的 `NULLIF(current_setting('app.tenant_id', true), '')::integer` 表达式，因为 0005 的 `CORE_TABLES` 循环不会覆盖新表）+ 回填（B5）。`0001`~`0006` **NO CHANGE** |
| **Domain（既有实体）** | **NO CHANGE** | `Connection` / `Session` / `Identity` / `User` / `Channel` / `Agent` 全部不动（ADR-2 红线：Connection 不得新增授权字段或 `authorize()/allow()/deny()`） |
| **Domain（新增）** | **CHANGE ×1** | 新增一个值对象（承载 4 个语义列 + id），命名避开 guard 黑名单（`AccessGrant` / `GrantModel` / `PermissionGrant` / `AuthorizationDecision`） |
| **Application** | **CHANGE ×4** | ① 新增授权执行器（输入 2 键 → granted 集合；命名避开 `Service/Manager/Orchestrator`）；② `coordinator.py` 插入唯一阶段（Identity 后 / Routing 前）；③ `routing.py` 候选集收窄（唯一性判定不变）；④ `interaction_recording.py` 的 `STAGE_FAILURE_MAP` 注册新 error → `(authorization, UNAUTHORIZED, False)` |
| **Port** | **CHANGE ×1** | 新增 repository port（读：按 `(channel_id, external_conversation_id)` 取 granted；写：create / delete / list）+ `UnitOfWork` 暴露新 repo = **第 12 个属性** |
| **Adapter** | **CHANGE ×2** | 新增 SqlAlchemy repository 实现 + mapper（`to_domain` / `to_orm`） |
| **Router** | **CHANGE（收窄）** | 「唯一性语义 + `agent_id` 派生」**NO CHANGE**；候选集来源从 `active(channel)` 变为 `active(channel) ∩ granted(conversation)`。Gate 2 §7 第 8 项「Routing 零改动」**需按 Gate 2-A §M4 修订** |
| **Coordinator** | **CHANGE ×1** | 新增阶段调用 + 把 granted 集合传给 Routing；**NO CHANGE**：不出现 `tenant` / `user_id` 字面量、无 `ast.Try`、不新增第三个 `decision.mode` 分支、`OutboundResponse(...)` 仍恰 1 处 4 kwargs |
| **Activity** | **CHANGE ×1** | `app/domain/activity_record.py` 新增 `STAGE_AUTHORIZATION = "authorization"`（既有 `STAGE_*` 与之并列，风格同形） |
| **Errors** | **CHANGE ×1** | 新增 typed error（`retryable=False`）；**NO CHANGE**：不新增 `status` 列、不合成双状态、不新增错误码以外的概念 |
| **API** | **CHANGE ×4** | ① `app/api/management/` 新路由（3 端点，B4.3）；② `app/schemas/` 新请求/响应模型；③ `app/main.py` 组合根注入（tenant-scoped）；④（待裁）候选会话只读面（B4.5）。**NO CHANGE**：既有 6 个管理路由与错误映射表 |
| **Tests** | **CHANGE ×3 类** | ① **必破**：表集合 exact-set 断言 ≥2 处（`test_cloud_0_1_1_b_tenant_isolation_dod.py:55-56`、`test_cloud_0_1_1_a_postgres_alembic_foundation.py:240-241`）→ 加入新表名；② **集合显式更新**：`test_step9_8_1::CANONICAL`（含测试名里的 "six"）、`test_cloud_0_1_1_a::test_runtime_execution_order_frozen`、`test_step9_1`（18 处）、`test_step9_A`、`test_step10_1_4`、`test_step9_8_2`、`test_step10_8_2/3/4`、`test_step10_6_1`、`test_step10_7_2`、`test_step10_4_2`（failure map 覆盖）；③ **fixture 面**：既有 runtime 成功路径需 seed 授权（Gate 2-A §H 第 4 类；11 个文件已确认断言成功终态，55 个文件触及 Runtime 链条，需逐一枚举）。**新增**：Targeted Tests 7 项（Gate 2-A §第 5 类）+ 本设计的 schema/回填/生命周期测试 |
| **Architecture guards** | **CHANGE ×5** | ① `test_adr6_no_implicit_grant_on_first_message`（禁 `AuthorizationGrant`/`AuthorizationDecision` 类；守卫自述「实现步到来时本断言应按 ADR 演进更新」`:331`）；② `test_adr9_no_authorization_module_in_domain_layer`（domain 模块名黑名单）；③ `_FORBIDDEN_PERMISSION_TABLES` 的**窄豁免**（B1.5）；④ 新增「授权表唯一性」守卫（确保 V0 只有这**一张**授权状态表）；⑤ 新增「deny 不落库」守卫（防止 effect/status 列 creep）。**全 `ADR-11` 显式授权后才可改** |

### B7.1 明确 NO CHANGE 的区域（汇总，便于实现步对照）

```
✅ 既有 12 张表的列 / 类型 / nullability
✅ 0001 – 0006 全部迁移文件
✅ Connection / Session / Identity / User / Channel / Agent 域实体
✅ connections.status 的读写路径（B3.2）
✅ Session 三维隔离键与 REUSE/CREATE_NEW 判定
✅ Identity Resolution（Package 1 的 D3 语义）
✅ Tenant / RLS 模型（除新表自身的策略外）
✅ Delivery / AgentDispatch / SessionCreation 执行器
✅ Worker / Driver / Persistence phase API
✅ 既有 6 个管理面路由与错误→状态码映射表
✅ ADR-1 ~ ADR-10 的语义（仅按 M1~M7 显式修订条款，不改语义）
```

---

## 8. 进入 implementation 前必须关闭的剩余项

| # | 项 | 归属 |
|---|---|---|
| 1 | **ADR-11 签发**（显式取代 M1/M2/M3/M4 + 授权 M5/M6 + 记录 B1.5 的窄豁免） | Review |
| 2 | `connections` 是否接受**加性** UNIQUE（B2.2） | Review（代价已实测为零守卫影响） |
| 3 | 独立资源端点形态 vs 子资源（B4.3） | Review |
| 4 | 候选会话只读面（B4.5）是否进 V0 | Review |
| 5 | 回填「不按 status 过滤」的假设登记（B5.3 #3） | ADR-11 记录 |
| 6 | 55 个 runtime 测试文件的 fixture 面枚举 | Package 2 实现步第一步 |

---

## 9. 本次未做（只读声明）

```
❌ 未改生产代码 / 未改测试 / 未建 migration / 未改 schema / 未改冻结契约
❌ 未创建表 / 未创建类 / 未创建 port / 未创建 API
❌ 未改 ADR guard（M5/M6 仅登记，等 ADR-11 授权）
❌ 未执行回填（仅给出 SQL 与条件裁决）
❌ 未裁决第 8 节的 6 项（属 Review）
❌ 未用通配授权 / 恒 ALLOW 让 rollout「看起来能跑」
```

**本文件为设计产出，不构成实现批准。** 下一步：**ADR-11** → 之后 Package 2 实现
（migration 0007 → port/model → 阶段 → coordinator/routing → 管理面 → tests）。
