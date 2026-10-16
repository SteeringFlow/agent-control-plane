"""应用服务层（Step 6.1 起：Application Vertical Slice 的正当归属）。

职责：组合领域对象 + 仓储/UoW 端口完成一个个用例，管理事务边界。

硬约束（机器化校验见 tests/test_import_boundaries.py）：
- 不 import FastAPI / starlette（不接触 Request、Response、APIRouter）
- 不 import SQLAlchemy（不接触 Session、Engine、sessionmaker）
- 不 import app.persistence / app.api / app.adapters
- 只 import app.domain（领域）与 app.repositories（UoW/仓储端口）

依赖方向：Application → Domain / Repository+UoW Port → Persistence Adapter。
具体的 Application Service 随业务 Vertical Slice 逐个加入（不做一次性设计）：
6.1/6.2 Agent、6.3 Channel、6.4 User、6.6 Identity（跨 User/Channel/Identity
三个 Port 编排）、7.2 Connection（跨 Channel/Agent/Connection 编排）。

9.1 起 runtime.py 承载 **Runtime Execution Boundary**（Management 之外的执行
编排边界）：RuntimeExecutionCoordinator = 把 8.x 契约按固定顺序串起来的
Coordinator，不重新定义业务语义、不直接访问 ORM/Adapter。
9.2 起 identity_resolution.py 承载第一个 **Runtime Execution** 组件：
IdentityResolutionExecutor 通过 Repository/UoW Port 让 8.2 Identity Resolution
契约获得真实执行能力（只读组合 Identity + User，Fail Closed；不创建/不猜测）。
9.3 起 session_resolution.py 承载 Session Resolution Execution：
SessionResolutionExecutor 按三维隔离键 (identity_id, connection_id,
external_conversation_id) 只读查找 active session 并给出 REUSE_ACTIVE /
CREATE_NEW **决策**（不创建 Session；connection_id 只消费、不选择）。
9.4 起 routing.py 承载 Routing Execution（RoutingExecutor）：按 channel_id 选出
唯一 active Connection 并**派生** agent_id，0 条 / 多条一律 Fail Closed。
9.5 起 agent_dispatch.py 承载 Agent Dispatch Execution（AgentDispatchExecutor）：
消费 RoutingResult（connection_id/agent_id 全部来自 Routing），经
`AgentAdapter` Port（app/repositories/agent_adapter.py）把 AgentRequest 发给
Agent → AgentResponse。Executor 只执行、不选择；Adapter 由构造注入（9.5 用
Test Adapter）。
9.6 起 delivery.py 承载 Delivery Execution（DeliveryExecutor）：只消费
OutboundResponse（channel_id / external_conversation_id 原样交给 Adapter），经
`ChannelAdapter` Port（app/repositories/channel_adapter.py）交还 Channel ——
Runtime 闭环的最后一段。Delivery is transport, not routing；失败统一映射
CHANNEL_DELIVERY_FAILED。
9.8.2 起 coordinator.py 承载 Runtime Coordinator **实现**（RuntimeCoordinator）：把
六段已冻结能力按 RUNTIME_EXECUTION_ORDER 串起来——Orchestrator 不是 Domain Service
（不碰 Repository/Adapter/ORM）；错误以 typed ApplicationError 原样传播（保留 stage
语义）；None 不做控制流；进入 Dispatch 的 control_plane_session_id 绝不可能是 None。
9.7 起 session_creation.py 承载 Session Creation Execution（SessionCreationExecutor）：
把 9.3 的 CREATE_NEW **决策物化**为真实 CP Session（SessionRepository.create 最小
写路径；agent_id 来自 RoutingResult，不自己找 Agent；channel_id/agent_id 由调用方
传入，不重新 Routing）。
"""

from app.application.access_authorization import AccessAuthorizationExecutor
from app.application.agent_dispatch import AgentDispatchExecutor
from app.application.agents import AgentApplicationService, CreateAgent, UpdateAgent
from app.application.channels import (
    ChannelApplicationService,
    CreateChannel,
    UpdateChannel,
)
from app.application.connection_conversations import (
    ConnectionConversationApplicationService,
    CreateConnectionConversation,
)
from app.application.connections import (
    ConnectionApplicationService,
    CreateConnection,
    UpdateConnection,
)
from app.application.coordinator import RuntimeCoordinator
from app.application.delivery import DeliveryExecutor
from app.application.identities import (
    CreateIdentity,
    IdentityApplicationService,
    UpdateIdentity,
)
from app.application.errors import (
    AccessAuthorizationDeniedError,
    AgentNotFoundError,
    ApplicationError,
    ChannelNotFoundError,
    ConnectionConversationNotFoundError,
    ConnectionNotFoundError,
    DuplicateConnectionConversationError,
    InvalidConnectionConversationInputError,
    DuplicateAgentError,
    DuplicateChannelError,
    DuplicateConnectionError,
    DuplicateIdentityError,
    IdentityNotFoundError,
    AgentDispatchFailedError,
    AgentResponseInvalidError,
    ChannelDeliveryFailedError,
    IdentityResolutionFailedError,
    RoutingFailedError,
    SessionResolutionFailedError,
    InvalidAgentInputError,
    InvalidChannelInputError,
    InvalidConnectionInputError,
    InvalidIdentityInputError,
    InvalidUserInputError,
    UserNotFoundError,
)
from app.application.identity_resolution import IdentityResolutionExecutor
from app.application.interaction_observability import InteractionObservabilityService
from app.application.routing import RoutingExecutor
from app.application.runtime import RuntimeExecutionCoordinator
from app.application.session_creation import SessionCreationExecutor
from app.application.session_resolution import SessionResolutionExecutor
from app.application.users import CreateUser, UpdateUser, UserApplicationService

__all__ = [
    "RuntimeExecutionCoordinator",
    "AccessAuthorizationDeniedError",
    "AccessAuthorizationExecutor",
    "AgentDispatchExecutor",
    "AgentDispatchFailedError",
    "AgentResponseInvalidError",
    "DeliveryExecutor",
    "ChannelDeliveryFailedError",
    "RuntimeCoordinator",
    "IdentityResolutionExecutor",
    "IdentityResolutionFailedError",
    "SessionResolutionExecutor",
    "SessionResolutionFailedError",
    "SessionCreationExecutor",
    "RoutingExecutor",
    "RoutingFailedError",
    "AgentApplicationService",
    "AgentNotFoundError",
    "ApplicationError",
    "ChannelApplicationService",
    "ChannelNotFoundError",
    "ConnectionApplicationService",
    "ConnectionConversationApplicationService",
    "ConnectionConversationNotFoundError",
    "ConnectionNotFoundError",
    "CreateConnectionConversation",
    "DuplicateConnectionConversationError",
    "InvalidConnectionConversationInputError",
    "CreateAgent",
    "CreateChannel",
    "CreateConnection",
    "CreateIdentity",
    "CreateUser",
    "DuplicateAgentError",
    "DuplicateChannelError",
    "DuplicateConnectionError",
    "DuplicateIdentityError",
    "IdentityApplicationService",
    "IdentityNotFoundError",
    "InteractionObservabilityService",
    "InvalidAgentInputError",
    "InvalidChannelInputError",
    "InvalidConnectionInputError",
    "InvalidIdentityInputError",
    "InvalidUserInputError",
    "UpdateAgent",
    "UpdateChannel",
    "UpdateConnection",
    "UpdateIdentity",
    "UpdateUser",
    "UserApplicationService",
    "UserNotFoundError",
]
