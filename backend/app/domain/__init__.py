"""领域层。

第一批领域实体：User / Channel / Agent（5.4 First Vertical Slice）；
Step 6.6 起加入 Identity（外部身份桥梁）；
Step 7.1 起加入 Connection（Channel ↔ Agent Access Path，Domain Contract）；
Step 9.3 起加入 Session（Control Plane 会话：Identity × Connection × conversation；
只落读路径，会话创建属后续 Slice）；
Step 8.1 起加入 InboundInteraction（Runtime 入口契约，值对象，**不落库**——
不是 Session/Interaction 实体，不代表持久化记录）；
Step 8.2 起加入 IdentityResolution（Identity Resolution 契约：成功结果 +
IDENTITY_RESOLUTION_FAILED Fail-Closed 语义；**不实现 Resolver**）；
Step 8.3 起加入 SessionResolution（Session Resolution 契约：REUSE_ACTIVE /
CREATE_NEW 决策 + SESSION_RESOLUTION_FAILED；**不实现 SessionResolver**，
也不创建 Session 实体——Session 实体等 Session Vertical Slice 再落）；
Step 8.4 起加入 RoutingResult（Routing 输出契约：{connection_id, agent_id} +
ROUTING_FAILED Fail-Closed；**不实现 Router / Routing Rules**——Connection
选择归 Routing，但规则引擎等真实多 Connection 用例）；
Step 8.5 起加入 AgentRequest / AgentResponse（Control Plane ↔ Agent 归一化消息
契约）与 dispatch_failure（Agent Dispatch 失败码：AGENT_DISPATCH_FAILED /
AGENT_TIMEOUT / AGENT_RESPONSE_INVALID；**不实现 AgentDispatcher / Adapter**）。
Step 8.6 起加入 OutboundResponse（Runtime 出口契约：Delivery 交还给 Channel 的
消息 + CHANNEL_DELIVERY_FAILED Fail-Closed；与 InboundInteraction 对称；
**不实现 DeliveryService / ChannelAdapter**——Delivery is transport, not routing）。
Step 10.8.2 起加入 RuntimeExecutionResult（Runtime 执行结果契约：一次成功执行的
三个最小事实 session_mode / session_id / agent_response；**不接线**——
RuntimeExecutionCoordinator 仍 execute() -> None，接线须过 10.1.4 Freeze Rule）。
ACP V0.2 起加入 Artifact（Agent 工作成果值对象：随 AgentResponse.artifacts 加性携带，
**不落库 / 不投递 / 不流式 / 不进 RuntimeExecutionResult**——见
docs/step-acp-v0.2-artifact-contract-gate.md）。

其余（Session/Interaction/Activity 等实体）等各自 Slice 到来时再落——
不为了架构完整一次性全设计。

硬约束（机器化校验见 tests/test_import_boundaries.py）：
- 不 import SQLAlchemy / FastAPI / starlette
- 不 import app.schemas（Pydantic API Schema 不得反向流入领域）
- 不 import app.application / app.persistence / app.adapters / app.repositories
领域只依赖标准库（dataclasses 等）。
"""

from app.domain.access_authorization import AccessAuthorization
from app.domain.agent import Agent
from app.domain.agent_request import AgentRequest
from app.domain.agent_response import AgentResponse
from app.domain.artifact import Artifact
from app.domain.channel import Channel
from app.domain.connection import Connection
from app.domain.connection_conversation import ConnectionConversation
from app.domain.identity import Identity
from app.domain.identity_resolution import IdentityResolution
from app.domain.inbound_interaction import InboundInteraction
from app.domain.outbound_response import OutboundResponse
from app.domain.routing_result import RoutingResult
from app.domain.runtime_execution_result import RuntimeExecutionResult
from app.domain.session import Session
from app.domain.session_resolution import SessionResolution
from app.domain.user import User

__all__ = [
    "AccessAuthorization",
    "Agent", "AgentRequest", "AgentResponse", "Artifact", "Channel", "Connection",
    "ConnectionConversation",
    "Identity", "IdentityResolution", "InboundInteraction", "OutboundResponse",
    "RoutingResult", "RuntimeExecutionResult", "Session", "SessionResolution",
    "User",
]
