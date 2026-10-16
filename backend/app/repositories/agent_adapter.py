"""AgentAdapter 端口（Port，Step 9.5 Agent Dispatch Execution）。

这是 Control Plane → Agent 的**出站适配器边界**：描述「Runtime 需要把一个已确定的
AgentRequest 发给一个 Agent」的能力。只描述形状，不含实现：

    app/application/agent_dispatch.py   ← 依赖本端口（不依赖具体 Adapter）
    app/adapters/agents/…               ← 未来实现（9.5 阶段用 Test Adapter 注入）

`send(request, connection)`：
- `request`：8.5 冻结的 AgentRequest（agent_id / control_plane_session_id /
  content / external_session_id? / metadata?）——它是 Routing 已确定的 Agent 的
  归一化调用请求；
- `connection`：Routing 已选择的 Access Path（Connection 域对象）。Adapter 需要
  从这里读取 `credential_ref` / `config` 才能连接 Agent——这不是重新 Routing，是
  读取 Routing 已经选定的连接信息完成 Dispatch。

Adapter 自己负责：protocol authentication / serialization / transport /
protocol-specific response parsing。
Adapter **不负责**：Identity / Session Resolution / Routing / Authorization。

失败语义：本端口不引入新的底层错误（无 AgentConnectionFailed /
AuthenticationFailed / ProtocolFailed）——底层失败由 Executor 统一映射到既有的
AGENT_DISPATCH_FAILED / AGENT_TIMEOUT / AGENT_RESPONSE_INVALID。
"""

from typing import Protocol, runtime_checkable

from app.domain.agent_request import AgentRequest
from app.domain.agent_response import AgentResponse
from app.domain.connection import Connection


@runtime_checkable
class AgentAdapter(Protocol):
    """Control Plane → Agent 的出站适配器（发送归一化请求，取回归一化响应）。"""

    def send(self, request: AgentRequest, connection: Connection) -> AgentResponse:
        """把已确定的 AgentRequest 通过该 Connection 的连接信息发给 Agent。

        成功 → 返回满足 AgentResponse 契约的响应；
        失败（transport / protocol / auth）→ 抛异常，由 Executor 映射为
        `AGENT_DISPATCH_FAILED`（不返回半成品响应）。
        """
        ...
