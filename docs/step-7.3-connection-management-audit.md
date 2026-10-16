# Step 7.3 —— Connection Management Consistency / Boundary Audit

一句话目标：把 **Agent / Channel / User / Identity / Connection 五个 Management
对象**放在同一把尺子下审计，确认 Step 6.1~7.2 一路演进后统一边界没有漂移，
并专门收口两件此前没系统验证的事：**跨实体编排**与 **Connection 特有边界**。
最后用 mutation test 证明反向审计真能抓偷渡。

## 1. 正向审计（五实体统一边界）

| 项 | 检查 | 结果 |
| --- | --- | --- |
| **A Domain** | 五个实体都是纯 dataclass（Connection 额外允许 datetime），彼此零耦合，不继承 ORM Base | ✅ |
| **B Application** | 五个 Service 结构统一：只依赖 Domain + Port（UoW），用例面 = `create_*/get_*/update_*`，类面 = `*ApplicationService` + `Create*` / `Update*` | ✅ |
| **C Repository Port** | `repositories/` 只有 Protocol（依赖 domain），无实现、无 SQLAlchemy；Application 零 `app.persistence` | ✅ |
| **D Persistence** | Adapter 恰好实现 Port 方法面、只 flush 不 commit、不 import Application；Mapper 只认 Domain + ORM Model | ✅ |
| **E Transaction** | commit/rollback 只在 UoW；Service 只 `uow.commit()`、不自己 rollback；每用例开 UoW | ✅ |
| **F Input Contract** | 输入契约 = frozen dataclass（`Create*`/`Update*`），无 pydantic / `app.schemas` | ✅ |
| **G Error Contract** | 全部继承 `ApplicationError`；`errors.py` 零 import；Service 只抛 Application 错误 | ✅ |
| **H UoW** | Port 声明 + Adapter 接线，五个仓储（users/channels/agents/identities/connections）一致 | ✅ |

## 2. 跨实体编排（orchestration ≠ 复制）

| Service | 只读校验（存在性） | 只写自身 | 禁止复制 |
| --- | --- | --- | --- |
| `IdentityApplicationService` | `uow.users.get_by_id` / `uow.channels.get_by_id` | `uow.identities.create/update` | `uow.users.create` / `uow.channels.create`、构造 `User()`/`Channel()` |
| `ConnectionApplicationService` | `uow.channels.get_by_id` / `uow.agents.get_by_id` / `uow.connections.find_by_channel_and_agent` | `uow.connections.create/update` | `uow.channels.create` / `uow.agents.create`、构造 `Channel()`/`Agent()` |

源码级锁定：跨实体只是「编排」——只读被依赖实体的存在性 + 写自身，绝不构造或创建被依赖实体。

## 3. Connection 特有边界（双重防线收口）

| 边界 | 锁死方式 |
| --- | --- |
| `(channel_id, agent_id)` = Access Path 身份 | schema `UNIQUE(channel_id, agent_id)` 存在；`name` 不参与唯一约束 |
| 绑定不可变 | `UpdateConnection` 无 `channel_id/agent_id`；`apply_to_orm` 不覆写绑定 |
| `name` ≠ 唯一身份 | Port 无 `find_by_name`；schema 无 name 唯一约束 |
| `credential_ref` ≠ credential | 唯一凭据字段是 `credential_ref`（引用），无 `credential/token/password/secret/api_key` |
| `config` ≠ policy/capability/routing 容器 | 无 `permissions/capabilities/policy/routing_rules/users/tools/memory/...` 字段 |

## 4. 反向审计 + 变异探测

`_next_stage_problems()` 扫描磁盘：下一阶段实体/组件
（Session / Interaction / Activity / Router / Dispatcher / Resolver /
Capability / Policy / Authorization / Adapter）在非 models 层一律不准出现。

- `test_k_reverse_audit_clean`：当前仓干净（5 个 Management 对象已交付，其余未偷渡）。
- `test_k_reverse_audit_mutation_catches_smuggling`：**变异探测**——
  1. 注入 `domain/session.py`（premature entity 文件名）→ 必须报红；
  2. 注入 `domain/helper.py` 内含 `class Policy`（文件名无辜、类名为 forbidden 组件）→ 必须报红；
  finally 清理。证明审计不是永远返回空。

## 5. 验收结果

`python -m pytest -q` → **289 passed**（271 回归 + 7.3 新增 18）。

后续：Runtime 入口契约（8.1 InboundInteraction）→ Management API →
Runtime（Router / Resolver / Adapter / Session / Interaction / Authorization）。
