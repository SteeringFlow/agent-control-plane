# Step 9.5 — Agent Dispatch Execution

> 8.5 Agent Dispatch 契约第一次真正穿过 **Control Plane → AgentAdapter → Agent**。
> 这是 Vertical Slice 中非常关键的一步：首次出现 Adapter Port 的运行时使用。

## 链路

```
RoutingResult(connection_id, agent_id)
    ↓ ConnectionRepository.get_by_id(routing_result.connection_id)   （既有方法，无新增）
    ↓ Connection（读取 Routing 已选定的 Access Path 的连接信息）
    ↓ AgentRequest（agent_id 来自 RoutingResult.agent_id）
    ↓ AgentAdapter.send(request, connection)
    ↓ AgentResponse
```

**Dispatch is execution, not selection**（8.5 延续）。

## 与 9.4 的本质区别

9.4 是 `Routing → ConnectionRepository → RoutingResult`；9.5 是
`RoutingResult → AgentRequest → AgentAdapter → AgentResponse`——第一次真正需要
**Adapter Port / Protocol boundary** 的运行时实现。

## 阶段决定：不接真实 Agent（ACP）

如果第一次执行失败，需要能区分是 Dispatch 本身 / AgentAdapter / ACP 协议 / HTTP /
Agent 哪一层错了——同时接 ACP 会把多个变量一起引入。因此 9.5 用 **Test Agent
Adapter**（测试内注入，满足 Port）证明链路可执行：

```
TestAgentAdapter.send(...) → AgentResponse(content="test response")
```

## Connection 在 9.5 怎么用

- `RoutingResult.agent_id` → `AgentRequest.agent_id`（Executor 不自己找 Agent）；
- `RoutingResult.connection_id` → `ConnectionRepository.get_by_id()`（既有方法，
  **没有新增 Repository 方法**）——这是读取 Routing 已选定的 Access Path，不是
  重新 Routing（绝不是 `find_active_by_channel()` 再次选路）；
- Adapter 需要 `Connection.credential_ref / config` 才能连接 Agent（由 Port
  `send(request, connection)` 传入）。

## 交付物

- `app/repositories/agent_adapter.py`：`AgentAdapter` **Port**（`send(request,
  connection)`；未来实现落地 `app/adapters/agents/`，本阶段不写实现文件）；
- `app/application/agent_dispatch.py`：`AgentDispatchExecutor(agent_adapter,
  uow_factory)`；
- `app/application/errors.py`：`AgentDispatchFailedError`（AGENT_DISPATCH_FAILED）、
  `AgentResponseInvalidError`（AGENT_RESPONSE_INVALID）——**没有新造**
  AgentConnectionFailed / AuthenticationFailed / ProtocolFailed，底层错误由
  Executor 统一映射到既有 CP failure semantics；
- 既有冻结精确扩边：repositories 文件面 +agent_adapter.py、application 文件面
  ×5 +agent_dispatch.py、6.5 EXPECTED_ERRORS +2、6.7/8.5/8.6/8.A 反向守卫白名单
  （AgentDispatchExecutor / AgentDispatchFailedError / AgentAdapter Port）。

## 边界验证

- Agent ID = RoutingResult.agent_id；Connection = get_by_id(routing_result.
  connection_id)；
- 一致性 Fail Closed：Connection.agent_id ≠ RoutingResult.agent_id → 失败
  （不允许 Connection=10 / Agent=9）；Connection 读不到 → 失败；
- `control_plane_session_id`（CP Session）≠ `external_session_id`（Agent 侧 ref）；
- 不重新 Routing / Identity / Session Resolution；
- AgentResponse 无效 → AGENT_RESPONSE_INVALID；transport 失败 → AGENT_DISPATCH_FAILED
  （不泄漏底层异常）；
- 不直接访问 ORM / SQLAlchemy；不创建 AgentDispatcher / AgentRuntime / AgentPolicy。

## 不做（本阶段）

ACPAdapter 真实实现 / HTTP Client Framework / Retry / Timeout Framework /
Circuit Breaker / Failover / Streaming / Agent Pool / Load Balancing /
Agent Health Check / Agent Runtime / Coordinator 接入。

## 验收

`tests/test_step9_5_agent_dispatch_execution.py`（17 项）：成功返回 AgentResponse /
agent_id 来自 RoutingResult / Connection 来自 routing_result.connection_id /
一致性 Fail Closed（agent 错配、Connection 缺失）/ CP vs external session 不混淆 /
无效响应 → AGENT_RESPONSE_INVALID（3 类）/ transport 失败 → AGENT_DISPATCH_FAILED /
只依赖 Port+Domain（无 ORM/SQLAlchemy）/ 不再次选路（无 find_active_by_channel）/
Execution ≠ Dispatcher / mutation（越层 import、写调用、注入 AgentDispatcher →
7.3 反向审计报红）。全仓 `pytest -q` = 493 passed，lint 0。
