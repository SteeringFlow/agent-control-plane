"""AgentResponse —— Agent → Control Plane 的归一化响应（Step 8.5 Agent Dispatch Contract）。

定位：**AgentResponse = AgentAdapter 归一化后返回的统一响应模型。**
它不是 ACP / A2A / HTTP / Agent 内部 Message——协议差异由 AgentAdapter 负责。

- `content`：Agent 产出的内容；
- `external_session_id`：Agent 侧 session 标识。Agent 第一次响应时可能返回
  "abc123"，后续 Runtime 可以把它保存/关联到 Control Plane Session——但那属于
  后续 Session 生命周期处理；**8.5 不提前引入 Session 更新机制**；
- `metadata`：protocol-neutral auxiliary metadata（结构化字段面冻结）；
- `artifacts`：**ACP V0.2 加性扩展** —— Agent 工作成果（`Artifact` 列表，可 None）。
  默认 `None` ⇒ 旧 Agent / `AgentResponse(content="hello")` **完全向后兼容**；
  Artifact 不进入 Delivery / Tencent SSE / DB schema / RuntimeExecutionResult，
  仅随本响应进入 Interaction 的响应快照（见
  docs/step-acp-v0.2-artifact-contract-gate.md）。

所有权边界（与既有冻结一致）：
    Control Plane  owns → Control Plane Session
    Agent          owns → Agent Conversation / Context / Memory / Business State
"""

from dataclasses import dataclass

from app.domain.artifact import Artifact


@dataclass(frozen=True)
class AgentResponse:
    """Agent → Control Plane 的归一化响应。"""

    content: str
    external_session_id: str | None = None
    metadata: dict | None = None
    artifacts: list[Artifact] | None = None
