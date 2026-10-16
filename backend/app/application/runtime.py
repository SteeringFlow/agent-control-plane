"""Runtime Execution Boundary & Coordinator Contract（Step 9.1 / 9.8.1 / 10.8.3）。

一句话定位：`RuntimeExecutionCoordinator` = 把 8.x 已冻结的边界按**真实执行顺序**
串起来的执行编排边界（Execution Coordinator）。它不重新定义任何业务语义——只消费
已经冻结的契约，让 8.1~8.6 从「边界」变成「可执行管线」。

**关键区分（9.8.1）：8.x 契约定义的是职责与数据依赖（Runtime Contract Graph），
不等同于执行顺序。**

    Runtime Contract Graph（谁提供什么给谁）
        Inbound
          ├── external_identity_id → Identity ── identity.id ──┐
          ├── channel_id → Routing ── connection_id / agent_id ─┤
          └── external_conversation_id ─────────────────────────┤
                                                                ▼
                                                          Session Resolution
                                                         （REUSE / CREATE_NEW）
                                                                ▼
                                                          Dispatch ──> Delivery

    Runtime Execution Order（真正的 imperative 顺序，见 `RUNTIME_EXECUTION_ORDER`）
        Identity → Routing → Session Resolution → [Session Creation?] → Dispatch → Delivery

为什么 Routing 先于 Session Resolution：`SessionResolutionExecutor.execute` 的
`connection_id` 是**必填输入**，而它是 Routing 的产物（8.4：Connection 在 Routing
中被选择）——Session Resolution 无法在不知道 Connection 时执行。这不是架构对称性，
而是输入依赖。Routing 只需要 `channel_id`（Inbound 已提供），不需要 User / Identity /
Session / Content / Conversation。

它**不是** Identity Resolver / Session Resolver / Router / AgentDispatcher /
DeliveryService——9.2~9.7 已让各契约获得执行能力（经 Application / Port / Adapter
注入）。

本模块只冻结（机器化校验见 tests/test_step9_1_runtime_execution.py 与
tests/test_step9_8_1_execution_ordering.py）：

A. **Runtime application boundary**：接受 InboundInteraction；不拥有
   Identity/Session/Routing 的重新定义；
B. **Execution Order（9.8.1 依输入依赖裁决）**：
       Identity → Routing → Session Resolution → [Session Creation（仅
       CREATE_NEW，条件阶段）] → Agent Dispatch → Delivery。
   REUSE_ACTIVE 分支**不创建**；CREATE_NEW 分支才物化 Session（9.7）。
C. **Fail Closed（停止条件随阶段顺序冻结）**：
   - Identity failure → STOP；Routing failure → STOP；Session failure → STOP；
     Session Creation failure → STOP；
   - Dispatch failure → NO Delivery；
   - Delivery failure → processing 仍 SUCCESS、delivery = FAILED
     （processing_status ≠ delivery_status，4.2/5.3）；
   - 不允许倒序：Routing → Identity / Session Resolution → Routing /
     Dispatch → Routing / Delivery → Dispatch / Delivery → Routing。
D. **组件纪律**：Session 阶段不得自己 Routing（不调用 RoutingExecutor）；
   Routing 不得创建 Session（不使用 SessionRepository / SessionCreationExecutor）；
   Delivery 必须最后。
E. **依赖边界**：本层只依赖抽象——不 import SQLAlchemy / FastAPI /
   app.persistence / app.adapters；不直接访问 ORM；不实例化 ChannelAdapter /
   ACPAdapter 等具体实现；具体能力以 Port 注入。

硬约束：引用 8.x 契约一律从 app.domain import，绝不在此重新定义同名类；本模块
只允许出现 Coordinator 边界符号，**不引入 RuntimeResult 巨型 DTO**（10.8.2 的
薄结果契约 RuntimeExecutionResult 是 10.8.1 A 裁决的唯一例外，10.8.3 正式接线，
10.1.4 Freeze Rule 六问预备裁决见 docs/step-10.8.1 §4）；不产生新的
Resolver / Router / Dispatcher / DeliveryService 类。
"""

from typing import Protocol

from app.domain.inbound_interaction import InboundInteraction
from app.domain.runtime_execution_result import RuntimeExecutionResult

# 执行顺序（9.8.1 依输入依赖裁决；不是语义 pipeline 顺序）：
#   identity → authorization → routing → session_resolution →
#   [session_creation?] → agent_dispatch → delivery。
# authorization 由 Implementation Package 2 / Step 2 插入（ADR-7：在 Session
# Resolution 之前；ADR-8：在最终 Routing 之前）——它把 Router 的候选集从
# 「该 Channel 的全部 active Connection」收窄为「已授权集合」。
# session_creation 是**条件阶段**：仅当 session_resolution 给出 CREATE_NEW 才执行
#（REUSE_ACTIVE 分支跳过它直接进入 agent_dispatch）。
RUNTIME_EXECUTION_ORDER = (
    "identity_resolution",
    "authorization",  # Implementation Package 2 / Step 2（ADR-7 / ADR-8）
    "routing",
    "session_resolution",
    "session_creation",  # conditional：仅 CREATE_NEW 分支执行
    "agent_dispatch",
    "delivery",
)


class RuntimeExecutionCoordinator(Protocol):
    """Runtime Execution Coordinator 边界。

    接受一个已归一化的 InboundInteraction，按 `RUNTIME_EXECUTION_ORDER` 的真实
    执行顺序执行一次 Inbound → … → Delivery，并 Fail Closed。具体各阶段能力
    （Identity / Routing / Session / Session Creation / Agent Dispatch / Delivery）
    由 9.2~9.7 以 Port 注入；本模块不实例化任何 Resolver / Router / Dispatcher /
    DeliveryService，也不触碰 ORM / Adapter 实现。

    成功 → 返回 RuntimeExecutionResult（10.8.2 薄结果契约：session_mode /
    session_id / agent_response，10.8.1 A 裁决的三事实）；失败 → 原 typed
    exception 原样传播（**无结果对象**——异常通道本身就是执行事实通道）。
    """

    def execute(self, inbound: InboundInteraction) -> RuntimeExecutionResult:
        """按 `RUNTIME_EXECUTION_ORDER` 执行一次 Runtime 管线。

        成功 → RuntimeExecutionResult（REUSE_ACTIVE → existing session.id /
        CREATE_NEW → newly created session.id；AgentResponse 直接进入 Result）。

        Fail Closed：
        - Identity / Routing / Session / Session Creation 任一失败 → STOP；
        - 只有 AgentResponse 成功（Dispatch 成功）才进入 Delivery；
          Dispatch 失败（Agent failed）→ NO Delivery / No Delivery；
        - Delivery 失败 → processing 仍 SUCCESS、delivery = FAILED；
        - 仅当 Session Resolution 给出 CREATE_NEW 才执行 Session Creation。
        """
        ...
