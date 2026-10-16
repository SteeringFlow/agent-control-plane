# Implementation Gate 3 — External Identity Contract & Migration Design

> 状态：**DESIGN FREEZE（契约 + 迁移设计）** / Production Code **NOT MODIFIED**
> 性质：允许深入到实现层，产出 **最终 change list**；但不写生产代码、不改测试、不建迁移文件。
> 上游：`implementation-gate-2-6-external-identity-semantic-decision.md`（D1–D7 语义已裁决）
> **本文件不重开 D1–D7 的语义辩论**，只回答「把它落到现有 Core 上具体要动什么、按什么顺序、有什么风险」。
>
> 前置（Gate 2.5 / 2.6 已证明，本文件复核后维持）：
> `identities.tenant_id` 是自有列、RLS 直接按该列判定、Session 只依赖 `identity_id`、
> **全仓 `identity.user_id` 只有一个读取点**。

---

## 0. 结论速览

| 组 | 结论 | 判定 |
|---|---|---|
| **A** Schema | `user_id` NOT NULL → **NULLABLE**；FK **保持阻断式（无 ondelete 子句 = NO ACTION/RESTRICT）**，**不 SET NULL、不 CASCADE**；**零 ORM cascade**（已是结构事实） | 设计冻结 + 1 个新迁移 |
| **B** 契约面 | **7 处**非空假设（domain 1 / application 1 / schema 2 / orm 1 / mapper 2），其中 **只有 3 处必须改** | change list 成形 |
| **C** Resolution | `IdentityResolution.user: User | None`；**User missing ≠ `IDENTITY_RESOLUTION_FAILED`**；**单点改造**（`identity_resolution.py:61`）+ 1 个现有测试语义反转 + **1 个守卫会因注解形状而失败（必须同步改）** | 设计冻结 |
| **D** Management API | **V0 不暴露第二个 API**。现有 `POST /api/identities` 语义固定为 **Create managed binding**（A+B 原子）。**语义拆分 ≠ API 数量增加** | **反膨胀裁决** |
| **E** Session / Runtime | 全链路 **零 `identity.user_id` 依赖**；唯一读取点 = Identity Resolution 内部 | **PASS（结构保证）** |
| **F** Tenant / RLS | `Identity → Tenant` **直接成立**；`Identity → User → Tenant` 不存在 | **PASS + 新增守卫** |
| **G** 自动创建 | 仍明确 NON-GOAL；本 Gate 的 change list **零新增创建路径** | 维持 |

**一句话**：Gate 3 把 2.6 的「语义允许」变成「结构允许 + 合同保守」——
**模型层诚实（可空）、合同层保守（不动 API）、迁移层最小（1 个 alter_column）**。

---

## A. Schema

### A.1 现状事实（逐行列号）

| 事实 | 位置 |
|---|---|
| `user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)` | `app/persistence/models/identity.py:28-30` |
| 迁移 baseline 同样 `nullable=False`，FK **无 `ondelete`** | `alembic/versions/0001_baseline_core_tables.py:84, 93` |
| `sessions.identity_id` FK → `identities.id`，`nullable=False` | `0001:102`；`test_models.py:167` |
| 唯一键 = `uq_identities_tenant_channel_external_id (tenant_id, channel_id, external_id)`（**不含 user_id**） | `models/identity.py:20-25`；`0002:41-43` |
| **全仓零 `relationship()`**（明文纪律） | `persistence/models/__init__.py:9-11`（+ tenant/credential/browser_session 同纪律） |
| **全仓零 `cascade` / `ondelete` / `passive_deletes`** | 全 `backend/app` grep 仅命中 `models/__init__.py` 的**说明性文字** |
| SQLite 外键**全局强制开启**（所有连接，非仅测试） | `persistence/database.py:54-65`：`@event.listens_for(Engine, "connect")` → `PRAGMA foreign_keys=ON` |
| **无 User 删除能力**：`UserRepository` 只有 get/create/update/list | `app/repositories/user.py:22-36` |
| **无 User 删除端点**：users API 无 DELETE（软下线 = PATCH status） | `api/management/users.py:3` |

### A.2 决策 A-1：`user_id` → NULLABLE

**裁决**：`NOT NULL` → `NULLABLE`，ORM 与迁移同步。

**边界（重要）**：本决策**不打开任何生产写入路径**。V0 不存在「创建未绑定 Identity」的动作
（D4 / §D 未裁决），因此**没有任何合法流程会写出 NULL**。F1 提供的是**结构容量**，不是**可达性**。

**为什么仍然必须做**（不是「以后再说」）：

1. **模型不得说谎**：2.6 已冻结 Identity 是一等运行时身份、User 关联可选（D1/D2）。
   若列仍为 NOT NULL，Schema 与冻结语义直接矛盾——这是「文档说一套、结构另一套」的典型来源。
2. **它是解耦的结构前置**：User 生命周期与 Identity 生命周期解耦，必须在**结构上可表达**
   「无 User 的 Identity」；否则未来任何解绑/删除裁决都要先做一次 schema 变更（把最难的一步
   推到最不确定的时刻）。
3. **风险极低**：放宽约束是**纯加性、向后兼容**的——现有写入全部提供 `user_id`，
   不会让任何既有行为失败。（把它和 F2 放同一变更，见 §8 顺序要求。）

### A.3 决策 A-2：FK `ON DELETE` 语义 = **阻断式**

**DB 事实**：`ForeignKey("users.id")` 无 `ondelete` 子句 → PostgreSQL / SQLite 均为
**`NO ACTION`**（阻断式）。迁移 baseline 同样无 `ondelete`（`0001:93`）。

**冻结语义**：

```
只要仍存在 Identity 引用该 User，
删除该 User 必须失败（IntegrityError / 事务回滚）。

不 SET NULL   不 CASCADE
```

**对三个原始问题的直接回答**：

| 问题 | 回答 |
|---|---|
| ON DELETE 到底是什么语义？ | **阻断式（NO ACTION）**：删除被拒绝，不是「自动解绑」也不是「级联清理」 |
| 删除 ACP User 后 Identity 是否保留？ | **保留**——因为**删除根本没发生**（事务回滚，User 与 Identity 同时存续）。「保留」的实现方式是**阻断**，不是 SET NULL |
| 是否自动变成 `user_id = NULL`？ | **不会**。没有任何隐式解绑路径 |
| 是否存在 ORM cascade 会错误删除 Identity？ | **不存在**，且是**结构保证**而非约定（见 A.4） |

**为什么不选 SET NULL**（明确记录被拒选项）：

- SET NULL 会让「删除 User」变成一条**隐式解绑**路径，绕过既有冻结纪律
  （`UpdateIdentity` 无 `user_id`、`IdentityUpdate` `extra=forbid`、改绑定 = 未来的 Identity Linking Slice，
  `app/application/identities.py:67-68`、`api/management/identities.py:30-31`）。
- V0 **根本没有 User 删除能力**（A.1）。SET NULL 是给一把尚不存在的枪装消音器——
  却把「静默改绑定」这条最容易后悔的语义提前写进 DB。

**为什么不选 CASCADE**：

- External Identity 是一等运行时身份且承载会话归属（`sessions.identity_id`）。
  删除一个**管理账号**绝不能连带删除**渠道身份**及其会话历史——那是数据丢失，不是清理。

**「若未来真要删除 User」的三条候选路径（NOT DECIDED，本 Gate 不裁决）**：

```
(a) 显式 Unbind 动作（先解绑 → 再删）        ← 语义最干净，属 Identity Linking Slice
(b) FK 改 SET NULL（删除自动解绑）           ← 与「不静默改绑定」冲突，需显式推翻纪律
(c) 应用层「先解绑再删」的两步编排           ← 是 (a) 的实现形态之一
```

若未来走到这一步，必须**同时**裁决 unbind 语义 + 迁移 + 审计要求。**禁止**在实现步顺手改 `ondelete`。

**是否显式写 `ondelete="RESTRICT"`？—— V0 不写。**

理由是工程性的：`NO ACTION` 与 `RESTRICT` 在无 deferred 约束场景下**行为等同**，但显式化会
改变 FK 的 `ddl` 表达 → 触发 Alembic autogenerate 差异与约束重建。为一个**无行为差异**的字面量
引入迁移风险不划算。改用**守卫**锁死该决策（§F 守卫 g3）。

### A.4 ORM cascade 审查（结论：结构上不可能误删）

| 检查项 | 结果 |
|---|---|
| `relationship()` 出现次数 | **0**（`models/__init__.py:9-11` 明文「只声明 Schema，不写 relationship()」） |
| `cascade=` / `passive_deletes=` | **0** |
| `ondelete=` | **0** |
| ORM 侧删除 User 时的行为 | 无 relationship → SQLAlchemy **不会**去动 `identities` 行 → DB FK 拒绝 → `IntegrityError` |
| 是否存在删除 User 的代码 | **不存在**（端口无 `delete`、端点无 DELETE） |

**判定：PASS，且是结构保证。** 这一项**不需要任何代码改动**，只需要新增一条删除阻断测试（§8 T4）。

### A.5 迁移设计

```
新迁移：backend/alembic/versions/0006_identities_user_id_nullable.py
    revision      = "0006"
    down_revision = "0005"

upgrade():
    op.alter_column("identities", "user_id",
                    existing_type=sa.Integer(), nullable=True)

downgrade():
    op.alter_column("identities", "user_id",
                    existing_type=sa.Integer(), nullable=False)
```

**必须同时改动的位置（否则两条建库路径分叉）**：

| 路径 | 事实源 | 是否需改 |
|---|---|---|
| Cloud / PG | Alembic `upgrade head` | **新增 0006** |
| OSS / SQLite 本地 | `Base.metadata.create_all`（仅 SQLite 生效，A8 已冻结） | **改 ORM**（`models/identity.py:28-30`） |

**不变项（明确冻结）**：

```
✅ 索引 ix_identities_user_id 保留（PG 对 NULL 值同样建索引；删除索引会造成无谓差异）
✅ 唯一键 uq_identities_tenant_channel_external_id 不变（不含 user_id）
✅ identities.channel_id / external_id / tenant_id 全部不动
✅ 0001–0005 一律不回改（既有纪律：baseline 不可变，新变更追加为 0006）
```

**已知限制（诚实记录）**：

- **SQLite 不支持 `ALTER COLUMN`**：0006 只保证 PostgreSQL。SQLite 路径由 `create_all` 承担
  （`test_cloud_0_1_1_a_postgres_alembic_foundation.py:88-92` 已冻结该分工）。
  若未来要对 SQLite 跑迁移，需 `batch_alter_table`——**本步不做**。
- **`downgrade` 是有损不可逆的**：若库中已存在 `user_id IS NULL` 的行，`nullable=False`
  会直接失败。这不是缺陷，而是诚实的「结构容量被使用时不可自动回退」。
  → 迁移 docstring 必须写明这条，禁止把 downgrade 宣传成安全回滚。

### A.6 A 组 change list

| # | 文件 | 改动 | 必需 |
|---|---|---|---|
| A-1 | `app/persistence/models/identity.py:28-30` | `Mapped[int | None]` + `nullable=True`（FK 定义保持无 `ondelete`） | **是** |
| A-2 | `alembic/versions/0006_identities_user_id_nullable.py` | **新增**（含 downgrade 有损说明） | **是** |
| A-3 | `alembic/versions/0001..0005` | **不动** | — |

---

## B. Domain / Mapper / API —— 完整非空假设清单

> 目标：**最终 change list**，不是立即修改。「必需」列表示该处**在 V0 变更包内**是否必须动。

### B.1 `Identity.user_id`（用户点名的三处之一）

| # | 位置 | 现状 | 目标 | 必需 | 理由 |
|---|---|---|---|---|---|
| B-1 | `app/domain/identity.py:30` | `user_id: int` | `user_id: int \| None` | **是** | 与列定义一致；否则 Mapper 把 `None` 塞进非空字段（类型谎言） |
| B-2 | `app/domain/identity.py:28` docstring | `"""某 Channel 上的某个外部身份 → 某个 Control Plane User 的绑定。"""` | 「某 Channel 上的外部身份；**可选**关联到一个 ACP User」 | **是** | 2.6 D1：Identity 不再**定义**为绑定 |
| B-3 | `app/domain/identity.py:5` 图 | `User ──1:N── Identity ──N:1── Channel` | 断言 ACP User 关联为可选 | 建议 | 文档层，防读图误判 |

**注意 B-1 的 fan-out 极小**：`Identity` 是 frozen dataclass，`user_id` 是**必填位置参数**
（`id` 之后）。改成 `int | None` **不移位**任何字段顺序，且全部构造点都显式传 `user_id=`
→ **零构造点破坏**。

### B.2 `IdentityCreate.user_id`

| # | 位置 | 现状 | 目标 | 必需 |
|---|---|---|---|---|
| B-4 | `app/application/identities.py:54`（`CreateIdentity`） | `user_id: int` | **保持 `int`（必填）** | **否** |
| B-5 | `app/application/identities.py:105` | `_require_positive_id(input.user_id, "user_id")` | 保持 | **否** |
| B-6 | `app/application/identities.py:119-120` | `UserNotFoundError` 存在性校验 | 保持 | **否** |
| B-7 | `app/schemas/identities.py:36`（`IdentityCreate`） | `user_id: int` | **保持必填** | **否** |
| B-8 | `app/application/identities.py:46-52` docstring | 「(user_id, channel_id, external_id) 构成绑定骨架」 | 明确「= **Create managed binding**（A+B 原子动作）」 | **是**（措辞） |

**这是本 Gate 最重要的「不改」裁决**（§D 给出完整理由）：管理面的入参契约**零变更**。

### B.3 `IdentityResponse.user_id`

| # | 位置 | 现状 | 目标 | 必需 | 理由 |
|---|---|---|---|---|---|
| B-9 | `app/schemas/identities.py:70` | `user_id: int` | `user_id: int \| None` | **是** | 否则一旦出现 NULL 行，`model_validate` 直接抛 ValidationError → 管理面 500 |
| B-10 | `app/schemas/identities.py:60-65` docstring | 「字段面 == 领域可见字段」 | + 说明 `user_id` 可空 = 未绑定 | 建议 | — |

**B-9 是纯放宽（widening）**：JSON 里 key 始终存在，既有消费方读取非空值时行为不变 →
**无破坏性变更**。

### B.4 其余全链（已核查，多数零变更）

| # | 位置 | 现状 | 目标 | 必需 | 理由 |
|---|---|---|---|---|---|
| B-11 | `persistence/mappers/identity.py:16` | `user_id=row.user_id` | 类型随 ORM 放宽（**逻辑零变更**） | 否 | 透传 |
| B-12 | `persistence/mappers/identity.py:28` | `user_id=entity.user_id` | 同上 | 否 | 透传 |
| B-13 | `persistence/repositories/identity.py` | 无 `user_id` 假设 | **零变更** | 否 | 只按 `id` / `tenant_id` / `channel_id+external_id` 过滤 |
| B-14 | `app/repositories/identity.py`（Port） | 5 个方法 | **零变更、零新方法** | 否 | **关键**：F1/F2 不需要任何新端口方法 |
| B-15 | `api/management/identities.py:14, 26-27, 96-99, 142` | docstring 称「绑定」 | 措辞收敛为 managed binding；代码零变更 | 建议 | — |
| B-16 | `application/identities.py:67-68`（`UpdateIdentity`） | 绑定骨架不可改 | **保持**（改绑定 = Linking，仍不引入） | 否 | 与 A.3「不静默改绑定」一致 |

**B-14 的意义**：`F1`/`F2` **不触碰任何 Repository Port 表面** →
`test_step8_2::test_b_identity_port_face_unchanged`（`:181-184`）、
`test_step9_2::test_i_repository_surface_unchanged`（`:291-307`）、
`test_step7_3::EXPECTED_PORT_METHODS`（`:61-62`）**全部保持 GREEN**。
这是「不扩张抽象」的机器化证据。

### B.5 B 组必需改动汇总（3 + 2 项）

```
必须改（代码）：
    B-1  app/domain/identity.py:30            user_id: int | None
    B-9  app/schemas/identities.py:70         user_id: int | None
    A-1  app/persistence/models/identity.py   nullable=True
必须改（措辞，防读错）：
    B-2  app/domain/identity.py:28
    B-8  app/application/identities.py:46-52
明确不改：
    B-4/5/6/7  CreateIdentity + IdentityCreate.user_id 保持必填
    B-11..B-14 Mapper / Repository / Port 逻辑零变更
```

---

## C. Identity Resolution Contract

### C.1 冻结新结果形状

```
IdentityResolution (frozen dataclass)
├── identity: Identity        （不变）
└── user: User | None         （由 User 改为 User | None）

语义：identity 永远是「谁」的答案；user 是「有没有管理账号」的答案。
      后者缺失不影响前者成立。
```

### C.2 `User missing ≠ IDENTITY_RESOLUTION_FAILED`

```
Identity 命中 + Channel 匹配 + External ID 匹配
        ↓
   RESOLVED（即使 user 为 None）

Identity 未命中
        ↓
   IDENTITY_RESOLUTION_FAILED（Fail Closed，STOP）
```

**关键：这不只是「改个类型」——它删除了一条失败分支。** 逐项影响如下。

### C.3 影响面（error type / docstring / failure stage / tests / contract / callers）

#### (1) 代码：**单点改造**

| 位置 | 现状 | 目标 |
|---|---|---|
| `app/application/identity_resolution.py:61-63` | `user = uow.users.get_by_id(identity.user_id)`；`if user is None: raise IdentityResolutionFailedError(...)` | **移除失败分支**（`user` 允许为 `None` 并通过） |
| `app/domain/identity_resolution.py:59` | `user: User` | `user: User \| None` |

**「单点」是复核结论**：全 `backend/app` 中 `identity.user_id` 的读取**只有这一处**
（另一处 `application/identities.py` 是管理面入参，属 §D）。
因此 F2 的运行时改造半径 = **1 个文件 + 1 行类型 + 1 个 docstring**。

#### (2) error type：**保留，不放宽、不改名**

| 项 | 裁决 |
|---|---|
| `IdentityResolutionFailedError`（`application/errors.py:150-163`） | **保留**——Identity 未命中仍需要它 |
| 新增 `IdentityUserMissingError` / `IdentityNotBoundError` | **不做**（无消费者、语义上不再是失败） |
| 错误构造签名 `(channel_id, external_identity_id)` | **不变** |

**诚实说明**：该类的**存在理由收窄了**（从两种情形收窄为一种），但**不是**废弃。
全仓 **16 个测试文件**引用它（`test_step9_2` 6 处、`test_step9_8_2` 4 处、其余 1–3 处），
其中**只有 1 处断言会被本次语义反转**（见 C.5 T-1），其余断言的是「Identity 未命中 → Fail Closed」，
**与新语义一致，保持 GREEN**。

#### (3) docstring：必须收窄的**确切句子**

| 位置 | 现句 | 修订方向 |
|---|---|---|
| `application/errors.py:153-154` | 「未找到 (channel_id, external_id) 对应的 Identity，**或找到 Identity 但其 user 缺失时抛出**」 | 删除「或找到 Identity 但其 user 缺失时抛出」 |
| `application/identity_resolution.py:26-27` | 「找到 Identity 但 User 缺失 → 同样 Fail Closed」 | 改为「User 缺失**不再**是失败条件（2.6 D3）」 |
| `application/identity_resolution.py:12-16` 图 | `Identity → user_id → User → IdentityResolution(identity, user)` | 标注 user 为可选分支 |
| `domain/identity_resolution.py:52-56` | 「一次成功的 Identity Resolution 结果：…→ Identity → User」 | 明确「User 是可选的**管理关联**，不参与成功判定」 |
| `domain/identity_resolution.py:26-28` | Fail Closed 描述 | 收窄为「未找到 Identity」 |

#### (4) failure stage：**注册表不变，可达性变化**

| 项 | 现状 | 变更后 |
|---|---|---|
| `STAGE_FAILURE_MAP[IdentityResolutionFailedError]` = `(identity_resolution, "IDENTITY_RESOLUTION_FAILED", False)`（`interaction_recording.py:102-104`） | 由 **2 种情形**触发 | 由 **1 种情形**触发 |
| Failed interaction 落 `failure_stage=identity_resolution` | 不变 | 不变 |

**结论**：`failure_stage` / `failure_code` / `retryable` 的**字符串与语义零变更**；
只是其中一条触发路径消失。**不需要改 `STAGE_FAILURE_MAP`，也不需要新增 stage。**

#### (5) 8.2 / 9.2 冻结契约：**定向修订，必须显式**

| 冻结物 | 现措辞 | 修订 |
|---|---|---|
| `domain/identity_resolution.py` 模块 docstring（`8.2` 契约） | 「Identity → User」 | Identity 为主、User 可选 |
| `docs/step-8.2-identity-resolution-contract.md` | 结果 = identity + user | 标注 `user: User \| None` + 成功条件收窄 |
| `docs/step-9.2-identity-execution.md:41-44` | 「找到 Identity 但 User 缺失 → 同样 Fail Closed」 | 明确删除该条（记 2.6 D3 为修订来源） |
| `docs/step-6.6-*` / `step-12.4-*` | Identity = 「绑定」 | 措辞收敛 |

**纪律**：这是对既有冻结契约的**显式、可追溯**修订（引 2.6 D3），**不得静默改字**。

#### (6) 调用方（全量清点）

| 调用方 | 消费什么 | 需改？ |
|---|---|---|
| `application/coordinator.py:92-97` | **仅** `resolved.identity.id`（+ None 守卫） | **零变更** |
| `application/interaction_recording.py:419-426` | `identity.id`（走 `find_by_channel_and_external_id`） | **零变更** |
| `main.py` 组合根 | 注入 `IdentityResolutionExecutor(uow_factory=…)` | **零变更** |
| 测试（见 C.5） | — | 1 处反转 + 1 处守卫 |

**没有任何消费方读取 `resolved.user`。**（全 `app` grep `.user_id` 仅命中 C.3(1) 与 §D 两处。）

#### (7) ⚠️ 一个会被本次变更打破的**守卫**（必须同步改）

```text
tests/test_step8_2_identity_resolution_contract.py:131-140
    test_a_resolution_outcome_shape
        assert names == ["identity", "user"]        ← 不变
        for stmt in anns:
            assert isinstance(stmt.annotation, ast.Name)   ← ★ 会失败
            assert stmt.value is None                     ← 必须保持
```

`User | None` 的注解在 AST 里是 **`BinOp`**（`Name("User") | Constant(None)`），不再是 `ast.Name`。
当前守卫**逐字段硬断言注解必须是单一标识符**。
→ 若不改，**契约守卫会在修改类型的那一刻报红**（这是好现象：守卫抓住了契约变更）。

**改法**：把该断言改为「注解为 `Name` 或 `User | None` 形式的 `BinOp`」，
并**继续保留** `stmt.value is None`（无默认值 = all-or-nothing 纪律不得丢）。

### C.4 C 组 change list

```
代码（单点）：
    C-1  domain/identity_resolution.py:59          user: User | None
    C-2  application/identity_resolution.py:61-63  删除 user-None 失败分支
契约/文档：
    C-3..C-7  上述 5 处 docstring（errors.py / identity_resolution.py / domain + docs）
守卫：
    C-8  tests/test_step8_2_identity_resolution_contract.py:138  注解形状断言放宽（必须同批）
测试：
    C-9  替换 test_step9_2::test_b_identity_without_user_fails_closed（语义反转）
```

### C.5 测试变更（C 组 + A 组的联动）

| # | 测试 | 现状 | 目标 |
|---|---|---|---|
| **T-1** | `test_step9_2::test_b_identity_without_user_fails_closed`（`:173-200`） | 用 **Fake UoW** 模拟 user 缺失，断言**抛错**；docstring 写「FK 使 DB 无法构造」 | **反转为「解析成功且 `user is None`」**，并**改用真实 DB** 构造（A-1 落地后，DB 终于能构造这条行——这是 F1 与 F2 必须同批的直接技术理由） |
| **T-2** | `test_models::test_b_identities_schema`（`:115-131`） | 断言列清单 + `user_id` FK 目标 + `external_id` 非空 | **新增** `assert t.columns["user_id"].nullable` |
| **T-3** | `test_step8_2::test_a_contract_behavior`（`:151-167`） | 用 `user=User(...)` 构造 | **新增** `user=None` 分支断言（形状不变） |
| **T-4** | 新增（A 组） | — | 删除被 Identity 引用的 User → `IntegrityError`（**阻断式**，A.3） |
| **T-5** | 新增（F 组） | — | `identities.user_id` FK **无 `ondelete` / 无 `cascade`** → 锁死「不静默解绑」 |
| **T-6** | 新增（E 组） | — | `Identity(user_id=None)` → 走完 Session / Router / Connection / Agent 链路（`Session.identity_id` 不依赖 User） |
| **T-7** | 新增（守卫） | — | Resolution 成功后 **Repository 写调用数 = 0**（复用 9.2 K 组机制，确保「不因 user 缺失而创建」） |

**不受影响（已核查）**：`test_step6_6`（构造均带 user）、`test_step12_3`、`test_step6_5`、
`test_acp_v0_2_artifact_implementation.py`（**零 alembic 引用**）、
`test_models::test_d_fk_rejects_identity_with_missing_user`（插入期 FK 拒绝，与本次正交）。

---

## D. Management API 语义

### D.1 现状的准确描述

```
POST /api/identities
    ↓  IdentityCreate{user_id 必填, channel_id 必填, external_id 必填}
    ↓  IdentityApplicationService.create_identity
           ① _require_positive_id(user_id)              ← A 的必要性
           ② users.get_by_id(user_id) → 不存在 404       ← **B 的绑定校验**
           ③ channels.get_by_id → 不存在 404
           ④ find_by_channel_and_external_id → 409
           ⑤ identities.create(...)                     ← A 的写入
    ↓  201
```

**准确命名：这个动作实际是 `CreateManagedBinding`，而不是 `CreateExternalIdentity`。**
A（创建身份本体）与 B（建立管理绑定）被**原子地**合在一个用例里。

### D.2 Gate 3 的问题：**V0 是否真的需要第二个 API？**

**裁决：不需要。**

```
V0 不新增：
    ❌ POST /api/identities/unbound         （创建未绑定 Identity）
    ❌ POST /api/identities/{id}/bind       （绑定已有 Identity）
    ❌ DELETE /api/identities/{id}/binding  （解绑）
    ❌ PATCH 解绑（保持 extra=forbid 不可变绑定）
```

现有唯一写入面 = **`POST /api/identities`（Create managed binding）**，语义与入参契约**均不变**。

### D.3 理由（五条，按权重排序）

1. **无消费者。** Runtime **不创建**（ADR-5 / 9.2 源码级守卫）；管理面 UI 的绑定流程天然是
   「选 User + 选 Channel + 填 external_id」。没有任何流程需要「先建一条无主 Identity」。
2. **无策略支撑。** D4 明确**未裁决**「谁有权创建未绑定 Identity」，且授权模型（Gate 2）尚未实现。
   提前暴露写入面 = 提前承诺一个**最难回退**的决策（API 是单向承诺，语义可演进）。
3. **语义拆分已在模型层达成。** 2.6 的 D7 要求「两个语义必须分开」，
   而拆分点已经落在：`Identity.user_id` 可空（模型）+ Resolution 不依赖 User（运行时）+ 措辞区分 A/B（文档）。
   **这三处已足够表达 D7，不需要第三个 HTTP 端点来「证明」拆分。**
4. **API 数量 ≠ 语义数量。** 把两个语义实现为**一个方法的两个阶段**（校验 → 写入）
   是合法的；只要**命名与文档不允许把它们混为一谈**。
5. **成本不对称。** 现在不暴露 API 的代价 = 未来加端点（低）；现在暴露未裁决写入面的代价 =
   未来收窄权限/清理数据/推翻语义（高）。

### D.4 冻结原则（写入 ADR 级约束）

```
语义拆分 ≠ API 数量必须增加

禁止：因为 D7 要求 A/B 分离，就顺手新增两个端点。
允许：语义在模型/运行时/文档层分离，而管理面仍只暴露「原子绑定」这一个写入面。
条件：该写入面必须被显式命名与记录为 Create managed binding（A+B 原子，非两个独立动作）。
```

**反向禁令**（同样冻结）：

```
❌ 不得把「绑定」塞进发现路径（发现即绑定）
❌ 不得把「创建身份」塞进绑定路径（绑定即隐式创建）
❌ 不得让 PATCH 具备改绑定能力（保持 immutable binding，2.6 D7 红线）
❌ 不得引入自动 Linking
```

### D.5 未来若需要独立 A / B，最小增量（**NOT DECIDED**，仅登记）

| 未来动作 | 需要的裁决 | 需要的实现 |
|---|---|---|
| A alone（创建未绑定 Identity） | 谁有权创建（D4）+ 是否需要审计 | 新用例 + 新端点 + 授权检查 |
| Bind（已有 Identity → User） | 是否允许改绑（Linking）+ 是否留旧绑定痕迹 | 新用例 + Port 可能需要 `update` 绑定字段（**当前 Mapper 禁止**，`mappers/identity.py:40-41`） |
| Unbind | 与 User 删除的关系（A.3 三路径） | 同上 |

**本 Gate 不裁决以上任何一项**；它们全部需要在授权模型落地之后、由独立 Gate 处理。

---

## E. Session / Runtime —— 链路复核（结论：PASS）

### E.1 再次锁死（不变项）

```
Session.identity_id                         ← 唯一 Identity 依赖，不变
隔离键 (identity_id, connection_id, external_conversation_id)   ← 不变
Session 不新增 user_id                      ← 不变
Session actor = Identity.id                 ← 不变（8.3）
```

### E.2 全链路逐点验证：有没有隐式 `identity.user_id` 依赖？

| # | 链路节点 | 消费的 Identity 字段 | 触及 `user_id`？ | 证据 |
|---|---|---|---|---|
| 1 | `IdentityResolutionExecutor` | `identity`（+ 可选 `user`） | **是（唯一读取点）** | `application/identity_resolution.py:61` |
| 2 | `RuntimeCoordinator` | `resolved.identity.id` | 否 | `application/coordinator.py:94-97` |
| 3 | `RoutingExecutor` | `channel_id` → connection / agent | 否 | `application/routing.py`（接口输入仅 channel_id） |
| 4 | `SessionResolutionExecutor` | `identity_id` | 否 | `application/session_resolution.py:50-72` |
| 5 | `SessionCreationExecutor` | `identity_id` | 否 | `application/session_creation.py:51-88` |
| 6 | `AgentDispatchExecutor` | `control_plane_session_id` + connection/agent | 否 | `application/agent_dispatch.py` |
| 7 | `sessions` 表 | `identity_id` FK | 否 | `models/session.py`；`test_models.py:159-167` |
| 8 | `interactions` 表 | **无 `identity_id`、无 `user_id`** | 否 | `models/interaction.py:50-91`（只有 `channel_id` + `external_interaction_id`） |
| 9 | `InteractionRecording._find_session` | `identity.id` | 否 | `application/interaction_recording.py:419-426` |
| 10 | `activities` / Observability | session / interaction 维度 | 否 | `models/activity.py` |
| 11 | Runtime API (`api/runtime/`) | inbound 原样 | 否 | 全 `app` `.user_id` grep 未命中 |

### E.3 结论

```
Identity(user_id=NULL) → Session → Interaction → Router → Connection → Agent
全程唯一的 identity.user_id 读取点是 #1（Identity Resolution 内部），
且它在 F2 之后不再参与成功判定。
```

**判定：PASS。** 这是**结构保证**，不是「测试碰巧没覆盖」——
因为 Session / Interaction / Activity 三张表**在 schema 上就没有 user_id 列**，
链路中不存在可依赖它的位置。

**唯一需要新增的验证**：T-6（用 `user_id=None` 的 Identity 真实跑一次链路），
把「结构上没有依赖」升级为「行为上已验证」。

---

## F. Tenant / RLS —— 最终守卫

### F.1 现状：`Identity → Tenant` 直接成立（四条独立证据）

| # | 证据 | 位置 |
|---|---|---|
| 1 | `identities.tenant_id` 是**自有列**（NOT NULL + FK + index） | `models/identity.py:44-47`；`0002` `CORE_TABLES` |
| 2 | RLS 谓词**直接**用该列：`tenant_id = current_setting('app.tenant_id')::integer` | `0002_tenant_isolation.py`；`0005` 同表达式 `ALTER POLICY` |
| 3 | Repository 全部读方法按 `IdentityModel.tenant_id` 过滤，**且从不 join users** | `persistence/repositories/identity.py:23-83` |
| 4 | 创建时 tenant 来自**操作者 TenantScope**，与 `user_id` 无关 | `persistence/repositories/identity.py:36-38`（`row.tenant_id = self._tenant_id`）；`main.py` 注入 `tenant_scope_provider` |

### F.2 必须新增/强化的守卫（机器化）

| # | 守卫 | 目的 |
|---|---|---|
| g1 | `identities` 的**所有**读路径只按 `IdentityModel.tenant_id` 过滤（AST：Repository 内不得出现 `join(UserModel)` / `UserModel`） | 锁死 `Identity → User → Tenant` **不出现** |
| g2 | Identity 创建时 `tenant_id` 只来自 TenantScope（源码守卫：`row.tenant_id = self._tenant_id`，不接受来自 user 的推导） | 同上 |
| g3 | `identities.user_id` FK **无 `ondelete` / 无 cascade**（`fk.ondelete is None`） | 锁死 A.3「不静默解绑」（= T-5） |
| g4 | `identities` 仍**不含**第二个唯一键；唯一键仍为 `(tenant_id, channel_id, external_id)` | 复用 `test_step8_2:187-198`（已存在，保持） |
| g5 | RLS 谓词表达式未被改动（`tenant_id = current_setting(...)`） | 防误改迁移 |
| g6 | `sessions` 表**不新增** `user_id`，三维隔离键约束面不变 | 锁死 §E |

### F.3 红线（冻结）

```
❌ 任何形式的 Identity → User → Tenant 推导
❌ 让 user_id 参与租户判定
❌ 关闭 / 绕过 RLS，或改变 tenant_isolation 谓词列
❌ 用 join(User) 的方式「顺便」补租户过滤
```

---

## G. 自动创建仍然明确 NON-GOAL

**本 Gate 检查了 runtime lifecycle，但没有、也不得顺便实现：**

```
unknown external user
        ↓
auto-create Identity          ← ❌ 仍属 2.6 D4 未裁决项
```

**维持的三道既有守卫**（本次变更**不得削弱**）：

| # | 守卫 | 位置 |
|---|---|---|
| 1 | 运行时模块**零写调用**（AST 扫描 `create` / `update` / `delete`） | `test_step9_2::_boundary_problems`（`:123-129`） |
| 2 | 失败后**计数不变**（Identity/User 各 +0） | `test_step9_2::test_b_failure_does_not_create_identity_or_user`（`:162-170`） |
| 3 | 全仓无「猜 User / linking」能力令牌（fuzzy/guess/fallback/**guest**/link/merge/match/dedup） | `test_step8_2::test_d_no_guessing_or_partial_resolution_capability`（`:247-259`） |

**新增一条（T-7）**：Resolution 成功后**依然零写调用**——
防止本次「允许 user 缺失」被偷渡成「那就顺手补一个」。

**D4 未决项（本 Gate 不回答）**：

```
?  谁有权创建「未绑定 Identity」（管理面显式动作 / 未来 Discovery / 都不给）
?  Discovery 是否需要一个显式触发点
?  未绑定 Identity 的创建是否需要审计要求
```

---

## 8. 落地顺序与变更包（最终 change list）

### 变更包 1 — 合同 + 结构（**必须同一批**）

```
[1] app/persistence/models/identity.py:28-30      user_id → Mapped[int | None], nullable=True
[2] alembic/versions/0006_identities_user_id_nullable.py   （新增；downgrade 有损说明）
[3] app/domain/identity.py:30                     user_id: int | None
[4] app/domain/identity_resolution.py:59          user: User | None
[5] app/application/identity_resolution.py:61-63  删除 user-None 失败分支
[6] app/schemas/identities.py:70                  user_id: int | None
```

**为什么 [1]–[5] 不能拆**（硬性技术约束）：

```
只做 [1]（Schema 可空）不做 [5]  → 中间态：DB 允许 NULL，但运行时仍 Fail Closed
                                   （无功能意义，却已改 schema）
只做 [5]（不再要求 User）不做 [1] → 中间态：解析不要求 User，但 DB NOT NULL
                                   （构造不出来；且 T-1 只能用 Fake UoW 假测）
```

### 变更包 2 — 测试与守卫（与包 1 同批提交）

```
[7]  tests/test_step9_2_identity_execution.py       T-1 语义反转（Fake → 真实 DB）
[8]  tests/test_step8_2_identity_resolution_contract.py:138  T-8 注解形状断言放宽（★ 否则报红）
[9]  tests/test_models.py:115-131                   T-2 + user_id nullable 断言
[10] tests/test_step8_2_...:151-167                 T-3 补 user=None 分支
[11] 新增：T-4 删除阻断 / T-5 FK 无 ondelete / T-6 NULL-user 全链路 / T-7 成功后零写
```

### 变更包 3 — 文档（可同批或紧随）

```
[12] docs/step-8.2-identity-resolution-contract.md
[13] docs/step-9.2-identity-execution.md:41-44
[14] app/application/errors.py:153-154（docstring 收窄）
[15] app/application/identity_resolution.py:12-16, 26-27（图 + Fail Closed 段）
[16] app/domain/identity.py:5, 28（图 + 类 docstring）
[17] app/application/identities.py:46-52（Create managed binding 措辞）
[18] app/api/management/identities.py docstring（绑定语义：A+B 原子）
[19] docs/step-6.6-* / step-12.4-*（Identity 定义措辞）
```

### 变更包 4 — 迁移关闭证据

```
[20] PG：CLOUD_TEST_DATABASE_URL 下 alembic upgrade head → 0006 生效、nullable=True
[21] ★ tests/test_cloud_0_1_1_a_postgres_alembic_foundation.py:245-247
        当前断言 version_num == "0003"，而 head 已是 "0005" → **该 PG-gated 断言已陈旧**，
        本次必须一并修正（改为 head 或 "0006"），否则 Closure Evidence Run 必红（见 §9-1）
```

---

## 9. 审计发现（既有漂移，诚实记录）

| # | 发现 | 严重度 | 建议 |
|---|---|---|---|
| **1** | `tests/test_cloud_0_1_1_a_postgres_alembic_foundation.py:245-247`：`test_alembic_revision_is_0002` 断言 `version_num == "0003"`，但迁移链 head 已是 **0005** → **该断言当前必然失败**（PG-gated，本地默认 skip，故未暴露） | **中**（吞掉真实回归） | 本次改为断言 head（或 `"0006"`）；更稳的写法是断言「等于 head revision」而不是硬编码 |
| **2** | 唯一键文档漂移：`domain/identity.py:9-10`、`domain/identity_resolution.py`、`repositories/identity.py:6-8` 均写 `UNIQUE(channel_id, external_id)`，实际是 **`(tenant_id, channel_id, external_id)`**（`models/identity.py:20-25`、`0002:41-43`） | 低（措辞） | 随变更包 3 一并校正（**不改约束，只改文档**） |
| **3** | `sessions.external_conversation_id` 在 schema 上可空（`test_models.py:171`），但 Runtime 要求非空（`session_resolution.py:64-67` Fail Closed） | 观察项 | 不在本 Gate 范围；登记备查 |
| **4** | `identity.user_id` 曾被要求在 Identity 缺失时也抛 `IdentityResolutionFailedError`（两种情形共用一个错误类），失败诊断无法区分情形 | 低 | F2 之后该歧义**自然消失**（只剩一种情形）——记录为本次变更的副产品收益 |

---

## 10. 风险与明确不做的事

| 风险 | 缓解 |
|---|---|
| 迁移 `downgrade` 被误当安全回滚 | 迁移 docstring 显式写明「有 NULL 行则失败」 |
| SQLite 不支持 `ALTER COLUMN` | 已冻结分工：SQLite 走 `create_all`；0006 只保证 PG（诚实记录限制） |
| 放宽 nullable 被误读为「可以随便写 NULL」 | G 组守卫（零写调用）+ D 组（不新增创建 API）+ D4 未裁决项保持开放 |
| 有人顺手实现 A-alone API | §D.4 冻结原则 + 反向禁令 |

**本 Gate 明确不做**：

```
❌ 未改生产代码 / 未改测试 / 未新建迁移文件（本文件是 change list，不是 change）
❌ 未裁决 D4（自动创建 / 谁有权创建未绑定 Identity）
❌ 未裁决 A.3 的「User 删除三路径」
❌ 未裁决 D5 的未来 A / B / Unbind 独立 API
❌ 未引入任何修改 `ondelete` 的打算（A-2 明确不改 FK 定义）
❌ 未触碰 Repository Port 表面（B-14：零新方法）
❌ 未触碰 Authorization / Runtime order（Gate 2 范围）
❌ 未实现 Multi-Agent Routing（ADR-8 继续冻结）
```

---

## 11. 路线图（Gate 2.6 / Gate 3 编号已对齐）

```
Gate 1     Authorization Impact Audit                     ✅ DONE
Gate 2     Minimal Authorization Design                   ✅ DONE
Gate 2.5   External Identity Lifecycle Audit              ✅ DONE
Gate 2.6   External Identity Semantic Decision (D1–D7)    ✅ DONE / FROZEN
Gate 3     External Identity Contract & Migration Design  ← 本文件（DESIGN FREEZE）
     ↓
Implementation                     （变更包 1–4 落地）
     ↓
External Identity targeted regression       （变更包 2 的 T-1…T-7 + 全量回归 + PG 关闭证据）
     ↓
Authorization implementation               （Gate 2 设计落地：Conversation → Access → Connection）
     ↓
Authorization regression
     ↓
Real WeCom regression
```

> **编号说明**：上一轮交付的语义裁决文件已按本路线图重命名为
> `implementation-gate-2-6-external-identity-semantic-decision.md`
> （原为 `implementation-gate-3-…-semantic-decision.md`，避免与本次 Gate 3 撞号）。

### 关于「我们有没有偏离原来的 Authorization 任务」

**没有偏离。** 实际发生的是：

```
Gate 2  设计 Authorization
        ↓ 发现：Authorization 没有合法的 Runtime Subject
          （每个请求都需要一个 Principal，而当时的身份模型把它绑死在 ACP User 上）
Gate 2.5 审计 External Identity lifecycle
        ↓ 找到原因：Identity Resolution 的成功条件里混入了「User 必须存在」
Gate 2.6 冻结 Subject 的正确语义（D1–D7）
        ↓ 裁决：Identity 是 Channel-scoped 一等运行时身份，User 关联可选
Gate 3  把这个 Subject 真正落到现有 Core 上（契约 + 迁移设计）
```

也就是说：**Gate 3 不是在修 Identity 的 bug，而是在为 Authorization 准备合法的 Subject。**
它之所以必要，是因为「Authorization 的 Subject 从哪来」必须先有一个结构上成立的答案，
否则 Authorization 只能建立在一个随时可能因 User 缺失而崩掉的 Identity 之上。

---

## 附：一句话交付摘要

```
A  user_id: NOT NULL → NULLABLE；FK 保持阻断式（NO ACTION），不 SET NULL / 不 CASCADE；
   零 ORM cascade（结构保证，0 个 relationship）
B  非空假设共 7 处，只有 3 处必须改（domain 1 / response 1 / orm 1）；
   CreateIdentity / IdentityCreate 的 user_id 保持必填 → API 零破坏性变更；
   Repository Port 零新方法（三个 port 面守卫保持 GREEN）
C  IdentityResolution.user: User | None；user missing ≠ FAILED；**单点改造**
   （app/application/identity_resolution.py:61-63）+ 1 处测试反转 + **1 处守卫必须同批改**
D  V0 不新增第二个 API；POST /api/identities 冻结为 Create managed binding；
   语义拆分 ≠ API 数量增加
E  全链路零 identity.user_id 依赖（唯一读取点在 Resolution 内部）→ PASS
F  Identity → Tenant 直接成立；不得出现 Identity → User → Tenant → PASS + 新增守卫
G  自动创建仍 NON-GOAL；本变更零新增创建路径
```
