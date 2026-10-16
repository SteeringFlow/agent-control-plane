# Step 9.8.2 — Runtime Coordinator Implementation

> `RuntimeExecutionCoordinator`（9.1 边界 Protocol）第一次获得真正实现：
> `app/application/coordinator.py::RuntimeCoordinator`。不设计新 Runtime 语义——
> 全部使用已冻结组件；仍不接真实 ACP/WeCom（Test Agent + Test Channel）。

## Coordinator = Orchestrator，不是 Domain Service

它只知道「先调用谁 / 拿到什么 / 下一步调用谁 / 失败在哪里停止」，不知道如何找
Identity / 选 Connection / 找 Session / 连接 Agent / 发送 Channel。

```
Coordinator
    ├── IdentityResolutionExecutor     （注入）
    ├── RoutingExecutor                （注入）
    ├── SessionResolutionExecutor      （注入）
    ├── SessionCreationExecutor        （注入）
    ├── AgentDispatchExecutor          （注入）
    └── DeliveryExecutor               （注入）
```

模块不 import Repository / Adapter / Persistence / SQLAlchemy——六段能力全部构造注入。
源码只有 `execute` 调用（AST 锁死：`call_attrs == {"execute"}`）。

## 编排逻辑（按 RUNTIME_EXECUTION_ORDER）

```
1. IdentityResolutionExecutor → identity_id
2. RoutingExecutor            → RoutingResult（connection_id / agent_id）
3. SessionResolutionExecutor  → REUSE_ACTIVE / CREATE_NEW
   ├─ REUSE_ACTIVE → control_plane_session_id = existing_session_id
   └─ CREATE_NEW   → SessionCreationExecutor → created.id
4. AgentDispatchExecutor(routing_result, control_plane_session_id, content)
5. OutboundResponse → DeliveryExecutor
```

**不变量**：进入 Dispatch 的 `control_plane_session_id` 要么 = `existing_session_id`
（REUSE），要么 = `created.id`（CREATE_NEW），**绝不可能是 None**（否则 Fail Closed）。

## 错误传播（保留 stage semantics）

不吞异常、不把一切归一成 RuntimeFailed——各 Executor 的 typed ApplicationError 原样
上抛：Identity/Routing/Session/Session Creation 失败 → STOP；Dispatch 失败 →
NO Delivery；Delivery 失败 → 在 Dispatch 成功之后抛 ChannelDeliveryFailedError
（processing 仍 SUCCESS、delivery=FAILED 的语义归持久化层 4.2/5.3）。

## None 不是控制流协议

各 Executor 成功返回 typed domain 结果、失败抛 typed error——不出现
`if result is None: ...` 的模糊分支；本模块仅有的 `is None` 守卫（identity_id /
cp_session_id）都紧跟 raise（Fail Closed），AST 测试锁死。

## 验收

`tests/test_step9_8_2_runtime_coordinator.py`（15 项）：
- 真实 E2E REUSE（复用 existing_session_id，session 计数不变、不创建）；
- 真实 E2E CREATE_NEW（+1 条 session；Dispatch 的 cp_session_id 对应真实 Session 行）；
- **调用顺序录制**（fake executor 的 calls 来自真实执行）：CREATE_NEW →
  [identity,routing,session,session_creation,dispatch,delivery]；REUSE →
  [identity,routing,session,dispatch,delivery]；
- 失败传播：Identity → calls==[identity]；Routing → STOP 于 routing；Session →
  STOP 于 session（无 creation/dispatch/delivery）；Creation → STOP 于 creation；
  Dispatch → NO Delivery；Delivery → Dispatch 已成功（请求已发）后抛
  ChannelDeliveryFailedError（含真实执行器 + FailingChannelAdapter 验证）；
- cp_session_id 绝不 None；模块边界（只调 execute、无 repo/ORM、类面={RuntimeCoordinator}、
  `is None` 守卫必跟 raise）。

全仓 `pytest -q` = 565 passed，lint 0。未做（按范围冻结）：Interaction/Activity
持久化、retry/queue/async、ACP/WeCom、runtime API、frontend。
