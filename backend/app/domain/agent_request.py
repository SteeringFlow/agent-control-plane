"""AgentRequest —— Control Plane → Agent 的归一化调用请求（Step 8.5 Agent Dispatch Contract）。

定位：**AgentRequest = Control Plane 向 Agent Adapter 交付的统一请求模型。**
它不是 ACP Request / A2A Request / HTTP Request / Agent 内部 Message——那些都
属于具体 Adapter / Agent 协议，由 AgentAdapter 负责翻译（8.5 不实现 Adapter）。

核心原则：**Dispatch is execution, not selection.**

- 本请求到达这里时，Identity（8.2）/ Session（8.3）/ Routing（8.4）已完成；
  8.5 不再做任何 Identity / Session / Connection / Agent 选择，只负责把已确定的
  请求交给已确定的 Agent；
- `agent_id`：**确定的** Agent（来自 RoutingResult.agent_id，与 Connection 一致）；
- `control_plane_session_id`：这次 Agent 调用属于 Control Plane 的哪个 Session。
  它**不等于** Agent internal session——后者由 Agent / Agent Protocol 管理；
- `external_session_id`：Agent 侧**已经存在的**、由 Agent / Agent Protocol 管理
  的 session 标识（如 "agent-session-abc"）。Control Plane 可以把它继续传给
  Agent Adapter，但**不负责创建、管理或解释 Agent 内部 Session 的生命周期**；
- `content`：归一化后的请求内容。

禁止携带（机器化锁定）——AgentRequest 是 Control Plane → Agent 的**调用请求**，
不是 Control Plane 的整个 Runtime Context，也不是 Agent 的 Memory / Context：
    user_id / identity_id / channel_id / connection_id / routing_rule /
    capability / permission / tool / memory / conversation_history / session_state

`metadata` 边界：只允许 protocol-neutral auxiliary metadata；不得把它当“万能
逃生舱”偷渡 routing decision / authorization decision / identity resolution
result / session state / agent memory / tool permissions——本契约用**结构化字段
面**锁定（不是穷举 JSON key）。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentRequest:
    """Control Plane → Agent 的归一化调用请求（统一交付模型）。"""

    agent_id: int
    control_plane_session_id: int
    content: str
    external_session_id: str | None = None
    metadata: dict | None = None
