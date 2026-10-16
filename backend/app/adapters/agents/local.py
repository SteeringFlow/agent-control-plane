"""LocalAgentAdapter —— external-system-free Agent Adapter（Step 15.2.3）。

位置：`app/adapters/agents/local.py`。实现 `AgentAdapter` Port
（`app/repositories/agent_adapter.py`），是 `type="local"` Agent 的真实实现。

用途：OSS V0.x 的 **Local First Success** —— 让开发者在**不接任何外部系统**
（无 Tencent ACP、无公网 HTTPS、无凭据）的前提下，跑通完整 Runtime 链
（Identity → Routing → Session → Dispatch → Delivery）并观察到 AgentResponse。

行为：deterministic echo ——

    content = f"{prefix}{request.content}"

`prefix` 缺省 "Echo: "，可由 `Connection.config["prefix"]` 覆写（Adapter 读
Connection 是既有纪律：ACPAdapter/WeComAdapter 同样只从 Connection/config 取
连接信息）。

**这不是「跳过 Agent」**：响应真实经过
`AgentDispatchExecutor → AgentAdapter.send → AgentResponse`，再由 Coordinator →
PersistingCoordinator 落库为 Interaction 响应快照（Observation 的读取出口）。
与 ACPAdapter 的唯一差别是：不产生任何网络 I/O。

边界（与 ACPAdapter 同一纪律）：
- 不认识 Identity / Session / Routing / Interaction / Agent 选择；
- 不 import SQLAlchemy / persistence / application 编排（只 import 错误类型与域契约）；
- 失败语义不新造错误码：输入非法 → `AgentDispatchFailedError`，由 Executor 统一映射。
"""

from app.application.errors import AgentDispatchFailedError
from app.domain.agent_request import AgentRequest
from app.domain.agent_response import AgentResponse
from app.domain.connection import Connection

#: 缺省 echo 前缀（可经 Connection.config["prefix"] 覆写）。
DEFAULT_PREFIX = "Echo: "

#: AgentResponse.metadata 中的 binding 标识（与 ACPAdapter 的 metadata 对称）。
PROTOCOL_LOCAL_ECHO = "local-echo"


class LocalAgentAdapter:
    """`type="local"` 的确定性 echo Agent（无网络、无凭据、零外部依赖）。

    无状态构造（与 ACPAdapter 同形）：不持有 UoW / Repository / 配置。
    """

    def send(self, request: AgentRequest, connection: Connection) -> AgentResponse:
        """把 AgentRequest 归一化为确定性的 echo AgentResponse（Fail Closed）。

        成功 → `AgentResponse(content=prefix+content, external_session_id=None,
        metadata={"protocol": "local-echo"})`；
        失败（输入不满足契约）→ `AgentDispatchFailedError`（不返回半成品）。
        """
        if not isinstance(request, AgentRequest):
            raise AgentDispatchFailedError()
        content = request.content
        connection_id = connection.id if isinstance(connection, Connection) else None
        if not isinstance(content, str) or not content.strip():
            raise AgentDispatchFailedError(
                agent_id=request.agent_id, connection_id=connection_id)

        prefix = DEFAULT_PREFIX
        config = connection.config if isinstance(connection.config, dict) else {}
        override = config.get("prefix")
        if isinstance(override, str) and override.strip():
            prefix = override

        return AgentResponse(
            content=f"{prefix}{content}",
            external_session_id=None,
            metadata={"protocol": PROTOCOL_LOCAL_ECHO},
        )
