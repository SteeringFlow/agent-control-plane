# Step 9.4 — Routing Execution

> 8.4 Routing 语义的第一次真实执行。8.4 把 Routing 压得非常小，9.4 只是把它执行出来。

## 执行规则（8.4 冻结，9.4 落地）

```
channel_id
    ↓ ConnectionRepository.find_active_by_channel   （9.4 新增最小读路径）
    ↓ active Connections
0 条  → RoutingFailedError（Fail Closed）
1 条  → RoutingResult(connection_id=selected.id, agent_id=selected.agent_id)
>1 条 → RoutingFailedError（Fail Closed）
```

## 真实检查点：ConnectionRepository 缺什么

检查结果：`ConnectionRepository` 只有 `get_by_id / create / update /
find_by_channel_and_agent`，没有「取某 Channel 的 active Connections」。

按 9.3 同样的原则处理——只补 Routing 所需的最小读路径：

```python
def find_active_by_channel(self, channel_id: int) -> list[Connection]: ...
```

（Port + SQLAlchemy Adapter 各 +1；UoW 无需改动，Connection 早已挂载。）

**没有趁机增加**：`find_by_status` / `find_all` / `list_connections` /
`find_candidates` / `find_routing_rules` / `find_by_agent`，更不建
RoutingRuleRepository / RoutingPolicyRepository / RoutingTable。

## 职责边界

| 职责 | 归属 |
| --- | --- |
| selects Connection | ✅ RoutingExecutor |
| derives agent_id | ✅（派生自被选中 Connection） |
| Identity / Session Resolution | ❌（9.2 / 9.3 已完成，不重新解析） |
| Agent Dispatch | ❌（9.5） |
| fallback / priority / weight / load balancing / round robin / policy | ❌ |

**agent_id 是 Connection 的派生结果**：选了 Connection（agent_id=7）就不可能
得出 agent_id=9。

## 无隐式耦合（重点测试之一）

Routing 只吃 `channel_id`——不因为 `identity_id` / `external_conversation_id` /
`content` 改变选择。IdentityResolution → Routing 之间无隐式耦合。若未来真实用例
证明「仅靠 channel_id 不够」，再按真实案例升级 Routing，现在不提前设计规则系统。

## 依赖边界

- 只依赖 Repository / UoW Port；不 import SQLAlchemy / ORM / persistence /
  adapters；
- 不 import IdentityRepository / SessionRepository（不重新解析 / 不碰会话）；
- 不 import ConnectionRepository（走 `uow.connections`，只读）。

## 交付物

- `app/repositories/connection.py` + `app/persistence/repositories/connection.py`：
  `find_active_by_channel`（Port + Adapter）；
- `app/application/routing.py`：`RoutingExecutor.execute(channel_id) -> RoutingResult`；
- `app/application/errors.py`：`RoutingFailedError`（errors.py 保持零 import）；
- `app/application/__init__.py`：导出；
- 既有冻结精确扩边：5.7 / 7.3 `EXPECTED_PORT_METHODS["connection"]` +1、7.1
  Connection Port 方法面 +1、6.5 `EXPECTED_ERRORS` +1、6.7 反向守卫白名单
  （RoutingExecutor / RoutingFailedError / routing.py）、application 文件面 ×5 +1。

## 验收

`tests/test_step9_4_routing_execution.py`（17 项）：0 条 fail / 1 条 success /
>1 条 fail / inactive 不参与 / 非法 channel_id Fail Closed / agent_id 派生自所选
Connection（不会出现 Connection=10 / Agent=9）/ execute 只吃 channel_id /
不 import Identity·Session Resolution（无隐式耦合）/ Repository 只 +1 读方法 /
Execution ≠ Router / mutation（越层 import、写调用、注入 Router → 7.3 反向审计
报红）。全仓 `pytest -q` = 476 passed，lint 0。
