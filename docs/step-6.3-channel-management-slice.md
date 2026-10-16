# Step 6.3 —— Channel Management Application Slice

一句话目标：**把 Control Plane 网络拓扑的另一个端点 Channel 做成与 6.2 Agent
对称、但不机械复制的管理 Slice——为之后真正实现 Connection 两端的管理模型。**

```
Channel ──── Connection ──── Agent
   │                             │
 6.3 本步               6.1/6.2 已完成
```

## 0. 为什么 Channel 现在做、Connection 现在不做

Agent / Channel 是拓扑的两个端点。两端管理模型稳定后，Connection 才能有稳定的
承载对象；而 Connection 不是普通 CRUD——它横跨 Channel → Agent，落在真正的
**Authorization boundary**（授权关系，而不是聚合的配置表），需要单独设计。

**Channel type=wecom 不代表实现 WeCom。** 当前 Management 层只表达：
“这是一个什么类型的 Channel，以及它的配置是什么。”真正连接外部系统属于后面的
Adapter / Runtime Vertical Slice。

## 1. 交付内容

| 文件 | 变化 |
| --- | --- |
| `app/application/channels.py` | **新增**：`ChannelApplicationService` + `CreateChannel`/`UpdateChannel` 输入契约 + 同平台去重守卫 + 错误映射 |
| `app/application/errors.py` | 增加 `ChannelNotFoundError` / `DuplicateChannelError` / `InvalidChannelInputError` |
| `app/repositories/channel.py` | Port 方法面 → `get_by_id/create/update/find_by_type_and_name` |
| `app/persistence/repositories/channel.py` | Adapter 实现 `update` / `find_by_type_and_name`（仍只 flush 不 commit） |
| `app/persistence/mappers/channel.py` | 新增 `apply_to_orm`（update 覆写已加载行） |

Tests：`tests/test_step6_3_channel_management.py`（17 项）；5.7 closure A4 方法面演进。

## 2. 与 6.2 的对称 + 不机械复制

| 维度 | Agent（6.2） | Channel（6.3） |
| --- | --- | --- |
| 领域字段 | name/type/endpoint/status/config/credential_ref | name/type/status/config（入站渠道，无 endpoint/凭据引用） |
| 输入契约 | CreateAgent/UpdateAgent（含 endpoint/credential_ref） | CreateChannel/UpdateChannel（name/type/config；status 只走 update） |
| 重复性 | 全局 `find_by_name(name)`：同名即重复 | **平台限定** `find_by_type_and_name(type, name)`：同 type 才比较 name，wecom 与 feishu 可同名共存 |
| 错误 | AgentNotFoundError / DuplicateAgentError / InvalidAgentInputError | ChannelNotFoundError / DuplicateChannelError / InvalidChannelInputError |
| Service 名 | AgentApplicationService | ChannelApplicationService |

## 3. 边界（仍然不做）

❌ WeCom/Feishu Adapter ❌ webhook ❌ channel authentication ❌ Connection
❌ Routing ❌ Identity ❌ Runtime ❌ API

## 4. 验收结果

`python -m pytest -q` → **121 passed**（103 回归 + 6.3 新增 18 = 参数化 4+…）：

| 项目 | 结果 |
| --- | --- |
| 输入契约校验（空 name/type） | ✅ InvalidChannelInputError，DB 零写入 |
| Create 全链路 round-trip（含 config） | ✅ status 默认 active |
| 同平台同名去重 | ✅ DuplicateChannelError；回滚无残留 |
| **不同平台同名共存** | ✅（与 Agent 的关键差异点） |
| Get | ✅ existing→Channel / missing→None |
| Update 部分字段 / type 重分类 | ✅ 跨 UoW 重读一致 |
| Update missing | ✅ ChannelNotFoundError，不建新行 |
| 基础状态管理 | ✅ status=disabled 落库 |
| 事务边界 | ✅ commit 生效；异常自动回滚 |
| AST 依赖边界 | ✅ channels.py 不 import SQLAlchemy/persistence/API |
| 全量回归 | 121 passed |

## 5. 下一步

```
6.4  User / Identity Management（管理面端点之一）
...
Connection Vertical Slice：等 Agent/Channel 两端管理模型稳定后单独设计
（Authorization boundary，非普通 CRUD）
Adapter / Runtime Vertical Slice：WeCom/Feishu 等真实连接
```
