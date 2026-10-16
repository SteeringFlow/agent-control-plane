# Step 6.6 —— Identity Management Application Slice

一句话目标：**落地 Control Plane 的外部身份桥梁 Identity**
（`User ──1:N── Identity ──N:1── Channel（external_id）`），
并第一次让 Application 层**同时协调三个 Repository**。

## 1. Identity 与前面三个 Slice 的本质区别

前面（Agent/Channel/User）都是「一个管理对象」；Identity 表达的是**关系**：
"某个 Channel 上的某个 external identity 属于哪个 User"。

```
User.name          ≠ identity        （显示名，不是身份键）
Identity.external_id = 外部身份键      （但只在 (channel_id, external_id) 内唯一）
```

核心唯一键在 4.2 / 5.3 已冻结：`UNIQUE(channel_id, external_id)`。

## 2. 交付内容

| 文件 | 变化 |
| --- | --- |
| `app/domain/identity.py` | **新增**：Identity frozen dataclass（user_id/channel_id/external_id/id/display_name/status/metadata） |
| `app/repositories/identity.py` | **新增** Port：`get_by_id/create/update/find_by_channel_and_external_id`（无裸 external_id 全局查找） |
| `app/persistence/mappers/identity.py` | **新增**：to_domain/to_orm/apply_to_orm（metadata ↔ extra_metadata 差异在 Mapper 内消化） |
| `app/persistence/repositories/identity.py` | **新增** Adapter（只 flush 不 commit） |
| `app/application/identities.py` | **新增**：IdentityApplicationService + CreateIdentity/UpdateIdentity |
| `app/application/errors.py` | + IdentityNotFoundError / DuplicateIdentityError / InvalidIdentityInputError |
| `app/repositories/unit_of_work.py` / `persistence/unit_of_work.py` | + `identities` 仓储 |
| 各层 `__init__.py` | 导出更新 |

Tests：`tests/test_step6_6_identity_management.py`（20 项）。

## 3. 创建时必须回答的三个问题（首次跨三个仓储编排）

| 问题 | 回答 |
| --- | --- |
| user_id 是否存在？ | `uow.users.get_by_id()` → 不存在抛 **UserNotFoundError** |
| channel_id 是否存在？ | `uow.channels.get_by_id()` → 不存在抛 **ChannelNotFoundError** |
| (channel_id, external_id) 已存在？ | `uow.identities.find_by_channel_and_external_id()` → 已存在抛 **DuplicateIdentityError**（DB 另有 UNIQUE 硬兜底） |

## 4. 关键边界（写进代码 + 测试锁定）

### Orchestration，不是职责污染

```
✅ IdentityApplicationService
       ├── uow.users.get_by_id()      （只读存在性校验）
       ├── uow.channels.get_by_id()   （只读存在性校验）
       └── uow.identities.create()    （写入 Identity）

❌ User creation logic  /  Channel creation logic  /  Identity logic 混在一起
```

源码级测试：identities.py 不得出现 `User(...)`/`Channel(...)` 构造，
不得调用 `uow.users.create` / `uow.channels.create`。

### 不做自动 Identity Linking

`external_id = "zhangsan"` 在 WeCom 与 Feishu 上**默认不是同一个人**：

```
WeCom/zhangsan ─→ Identity A ─→ User 1
Feishu/zhangsan ─→ Identity B ─→ User 2   ← 完全合法
```

只有未来管理员**显式**执行 `Identity B → bind → User 1` 才算 Identity Linking
（本 Slice 不做，测试锁定无 resolve/link/merge/match 能力）。

### Update 只改档案字段

`UpdateIdentity` = `{display_name, status, metadata}`；
绑定骨架（user_id/channel_id/external_id）**不可通过 update 修改**——
改绑定 = Linking，属后续 Slice。

### Identity Management ≠ Identity Resolution

- Management（本 Slice）：`external identity → User` 的管理绑定；
- Runtime Resolution（未来）：`channel_id + external_id → Identity → User`。
- 测试锁定：application 层无任何 `resolve`/`link` 方法或类。

## 5. 验收结果

`python -m pytest -q` → **217 passed**（184 回归 + 6.6 新增/演进）：

| 项目 | 结果 |
| --- | --- |
| missing user / missing channel | ✅ UserNotFoundError / ChannelNotFoundError，零写入 |
| 跨三仓储编排且不复制业务逻辑 | ✅ 源码级锁定 |
| Create round-trip（含 display_name/metadata） | ✅ |
| 同渠道重复 (channel_id, external_id) | ✅ DuplicateIdentityError |
| **跨渠道同名 external_id 不自动合并** | ✅ 两个 Identity、可属不同 User |
| DB UNIQUE 硬兜底 | ✅ 绕过守卫直写 → IntegrityError |
| Get / Update（档案字段）/ Update-missing | ✅ 绑定骨架不变 / IdentityNotFoundError |
| 输入校验（id≤0 / 空 external_id） | ✅ InvalidIdentityInputError |
| 无 resolve/link/merge/match 能力 | ✅ 源码级锁定 |
| 事务边界（commit / 异常回滚） | ✅ |
| 全量回归（含 6.5 audit 参数面扩到 identity） | 217 passed |

同步演进的断言：5.7 closure（SLICE_ENTITIES + identity）、test_repositories、
test_unit_of_work、6.5 Management Consistency Audit（A~F 全部覆盖 identity）。

## 6. 下一步

```
Connection Vertical Slice（等两端模型 + Identity 稳定后单独设计）
或先落 Management API / 再把 Identity 接进 Connection 链路
Runtime Vertical Slice：channel_id + external_id → Identity → User 解析
```
