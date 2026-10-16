# Step 9.1 — Runtime Execution Boundary & Coordinator Contract

> 第一版 Runtime Vertical Slice 的第一步：先冻结 **Runtime Execution Boundary**，
> 让 8.x 契约从「边界」变成「可执行管线」。不实现任何阶段执行。

## 总目标（Step 9）

真实执行一次：

```
Channel → Control Plane → Agent → Control Plane → Channel
```

拆成小步骤：9.1 Boundary → 9.2 Identity → 9.3 Session → 9.4 Routing →
9.5 Dispatch → 9.6 Delivery → 9.7 First E2E → 9.A Closure Audit。

## 9.1 只解决一件事

8.x 定义了每一个边界，但缺一个「按顺序把这些契约串起来」的执行编排者。

9.1 定义它是 **Execution Coordinator**，而不是 Identity Resolver / Router /
AgentDispatcher / DeliveryService——每个阶段能力由 9.2~9.6 通过
Application / Port / Adapter 落地后注入。

## 交付物

`app/application/runtime.py`

- `RuntimeExecutionCoordinator`（Protocol）：接受 `InboundInteraction`，暴露
  `execute(inbound)`；按固定顺序执行并 Fail Closed；
- `RUNTIME_EXECUTION_ORDER`（9.8.1 依输入依赖修正）：固定顺序
  `identity_resolution → routing → session_resolution → [session_creation（仅
  CREATE_NEW）] → agent_dispatch → delivery`——Routing 必须先于 Session（Session
  Resolution 需要 Routing 产出的 connection_id）；
- 不重新定义任何 8.x Domain Contract（一律从 `app.domain` import）；
- 不 import SQLAlchemy / FastAPI / persistence / adapters；无对象实例化；
- 不引入 `RuntimeResult` 巨型 DTO；不产生新的 Resolver/Router/Dispatcher。

## 锁定的语义

| 原则 | 内容 |
| --- | --- |
| Execution Ordering | 固定顺序，不允许 Routing→Identity / Dispatch→Routing / Delivery→Routing |
| Fail Closed | Identity/Session/Routing 前置失败 → STOP；Dispatch 失败 → NO Delivery |
| Delivery 条件 | 仅 AgentResponse 成功才形成 OutboundResponse 进入 Delivery |
| 依赖边界 | 只依赖抽象（Domain 契约），不直接访问 ORM / Adapter 实现 |

## 同步扩展的既有冻结

application 文件面（5 处冻结断言）加入 `runtime.py`；6.5 防偷渡守卫以精确白名单
放行 `RuntimeExecutionCoordinator`（其余 runtime 类仍报红）。

## 不做（本步）

DB Session 查询 / IdentityResolver / SessionRepository / Router / AgentAdapter /
ChannelAdapter / 真实执行。

## 验收

`tests/test_step9_1_runtime_execution.py`（16 项）：Boundary 存在并可从
application 包导入 / 只依赖允许抽象（无越层 import、无实例化）/
顺序锁定 / Fail Closed 冻结 / Delivery 条件（dispatch < delivery）/
契约 import 而非重定义 / 无 RuntimeResult DTO / 无组件类 / mutation
（sqlalchemy import、倒序、RuntimeResult、IdentityResolver、契约重定义、
真实文件注入 AgentDispatcher → 7.3 反向审计报红）。
全仓 `pytest -q` = 416 passed，lint 0。
