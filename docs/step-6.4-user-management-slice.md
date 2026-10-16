# Step 6.4 —— User Management Application Slice

一句话目标：**把已冻结的 User Domain / Repository / Mapper / ORM 接到 Application 层，
并顺便回答一个真实问题：User 的唯一性到底由什么定义？**

```
User ──── Identity ──── Channel
  │
 6.4 本步（User 本身）｜Identity 单独 Slice
```

## 1. User 唯一性到底由什么定义？（本步的判断）

| 实体 | 唯一性 | 依据 |
| --- | --- | --- |
| Agent | `name` | 路由到 ACP Agent 的标识，管理期同名即重复 |
| Channel | `(type, name)` | 平台限定：同一渠道类型下的同名渠道才重复，wecom 与 feishu 可同名共存 |
| **User** | **不在 User 自身** | User 是 Control Plane 内部主体（principal）；「他是谁」由 **Identity（channel + external_id）** 承载 |

推理链：
1. `users` 表只有 `id / name / status / timestamps`，**没有** name 唯一约束（4.2 冻结）；
2. `users.name` 语义是显示名——两个真人可以都叫“张伟”，它不是身份键；
3. 真正能回答“是不是同一个人”的是外部身份：`identities` 表已冻结
   `UNIQUE(channel_id, external_id)`——即「某渠道上的某外部账号」；
4. 而 Identity 涉及 **External Identity Resolution**（Runtime 的核心基础之一），
   不属于 User 的普通 CRUD 字段 → 留在独立 Slice。

**结论（写进代码与测试）：**
- 不提供 `DuplicateUserError`（不为与 Agent/Channel 对称而预设）；
- `create_user` 无去重守卫，同名 User 允许共存；
- `UserRepository` **不扩张** `find_by_name` 等无用例支撑的 Port 方法——
  Port 方法面 = `get_by_id / create / update`；
- **不为对称性强行给 `users.name` 加唯一约束**（schema 维持 4.2 冻结）。
- 唯一性规则随 Identity Slice 一起定义（届时才是 `UNIQUE` 语义真正落地的地方）。

## 2. 交付内容

| 文件 | 变化 |
| --- | --- |
| `app/application/users.py` | **新增**：`UserApplicationService` + `CreateUser`/`UpdateUser` 输入契约 + 校验 + 错误映射 |
| `app/application/errors.py` | 增加 `UserNotFoundError` / `InvalidUserInputError`（并留下「故意没有 DuplicateUserError」的说明） |
| `app/repositories/user.py` | Port 加 `update`（仅此一个，无 name 查找） |
| `app/persistence/repositories/user.py` | Adapter 实现 `update`（仍只 flush 不 commit） |
| `app/persistence/mappers/user.py` | 新增 `apply_to_orm`（update 覆写已加载行） |

Tests：`tests/test_step6_4_user_management.py`（14 项）；5.7 closure A4 方法面演进。

## 3. 边界（仍然不做）

❌ Identity ❌ Connection ❌ Session ❌ Interaction ❌ Routing ❌ Runtime
❌ Channel/Agent Adapter ❌ API ❌ Frontend ❌ Authentication / RBAC

## 4. 验收结果

`python -m pytest -q` → **135 passed**（121 回归 + 6.4 新增 14）：

| 项目 | 结果 |
| --- | --- |
| **同名 User 共存**（唯一性判断的行为锁） | ✅ 两条记录、id 不同 |
| **不预设 DuplicateUserError**（源码级锁） | ✅ errors.py 无该类 |
| **User Port 无 name 查找**（方法面锁） | ✅ = {get_by_id, create, update} |
| 输入契约校验（空/空白 name） | ✅ InvalidUserInputError，DB 零写入 |
| Create / Get / Update round-trip | ✅ status 默认 active，跨 UoW 重读一致 |
| Update missing | ✅ UserNotFoundError，不建新行 |
| 基础状态管理 / 事务边界 | ✅ disabled 落库；异常自动回滚 |
| AST 依赖边界 | ✅ users.py 不 import SQLAlchemy/persistence/API |
| 全量回归 | 135 passed |

## 5. 下一步

```
6.5  Identity Slice（External Identity Resolution 的入口：
     唯一性真正落地处 UNIQUE(channel_id, external_id)，涉及 Channel 关联）
...
Connection Vertical Slice：等 Agent/Channel/User/Identity 管理模型稳定后单独设计
```
