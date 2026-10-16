# Step 7.2 —— Connection Management Application Slice

一句话目标：**让 Connection 成为可管理对象**（7.1 只是 Domain Contract；
本步交付 Application Service，与 6.6 Identity 同构，是**第三个跨实体编排点**）。

## 1. CreateConnection 的编排（三个 Port）

```
CreateConnection
       ├── Channel exists?                     uow.channels.get_by_id()
       ├── Agent exists?                       uow.agents.get_by_id()
       ├── (channel_id, agent_id) exists?      uow.connections.find_by_channel_and_agent()
       │                                          → DuplicateConnectionError
       └── create Connection → commit
```

Application Service 第一次同时协调 **ChannelRepository + AgentRepository +
ConnectionRepository**——但仍**只编排，不复制业务逻辑**（不构造 Channel/Agent、
不调用 uow.channels.create / uow.agents.create，源码级锁定）。

**Application duplicate check ≠ database uniqueness guarantee**：应用级预检给出
明确错误语义；并发硬保证始终归 `UNIQUE(channel_id, agent_id)`（与 6.6 一致）。

## 2. UpdateConnection（双层防线）

| 层 | 防线 |
| --- | --- |
| Application | `UpdateConnection = {name, status, config, credential_ref}`，**无 channel_id/agent_id** |
| Persistence | `repo.update()` / `apply_to_orm()` 不覆写绑定（7.1 已验证） |

`channel_id/agent_id` 是 immutable binding：改成别的 pair = 删除旧 Access Path +
建立新 Access Path，不是更新。

## 3. 交付内容

| 文件 | 变化 |
| --- | --- |
| `app/application/connections.py` | **新增**：ConnectionApplicationService + CreateConnection / UpdateConnection |
| `app/application/__init__.py` | 导出 ConnectionApplicationService / 契约 / Connection 错误 |

Repository Port 面 **不扩张**（7.1 已就绪：get_by_id/create/update/find_by_channel_and_agent）。

Tests：`tests/test_step7_2_connection_management.py`（21 项）。

## 4. 明确不做（保持锁死）

❌ API ❌ FastAPI schema ❌ Authentication ❌ Authorization ❌ RBAC
❌ Capability ❌ Policy ❌ Router ❌ Resolver ❌ Dispatcher ❌ Session
❌ Interaction ❌ Channel Adapter ❌ Agent Adapter

尤其**不做**"Agent token 能不能连接"之类的连通性测试——那属于
Runtime / Adapter，不是 Management Application Slice。

## 5. 验收结果

`python -m pytest -q` → **271 passed**（250 回归 + 7.2 新增/演进）：

| 项目 | 结果 |
| --- | --- |
| missing channel / missing agent | ✅ ChannelNotFoundError / AgentNotFoundError，零写入 |
| 三仓储编排且不复制业务逻辑 | ✅ 源码级锁定 |
| Create round-trip（status 默认 active；config/credential_ref） | ✅ |
| 绑定重复 → DuplicateConnectionError，无残留 | ✅ |
| A→X / A→Y / B→X 跨 pair 允许 | ✅ |
| DB UNIQUE 并发硬兜底（绕过守卫 → IntegrityError） | ✅ |
| Get / Update（可变字段落库、绑定不变）/ Update-missing | ✅ ConnectionNotFoundError |
| UpdateConnection 无绑定字段（App 第一道防线） | ✅ |
| 输入校验（id≤0 / 空 name） | ✅ InvalidConnectionInputError |
| 事务边界（commit / 异常回滚） | ✅ |
| 反向：方法面无 connect/ping/auth/adapter 语义；用例面 = create/get/update | ✅ |

同步演进的断言：6.5 audit（application 文件面 + connections.py）、6.7 closure
（test_j 更新为"Application 层已交付、Runtime 未交付"、反向审计文件面 +1）、
7.1 test（application 文件面 +1）。
