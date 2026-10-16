"""Agent Runtime SDK —— independent wire models（Step 15.5.5.1）。

本模块是 Agent-side Runtime SDK 的独立 wire 契约镜像（15.5.3 / 15.5.4 冻结）：

- `AgentRequest` / `AgentResponse` 是 `app/domain/agent_request.py` /
  `app/domain/agent_response.py` 的**独立镜像**（字段面 15.5.5.1 冻结），
  不是 import 复用 —— 契约共享、实现独立；本包**绝不 import `app.*`**。
- `Artifact`（ACP V0.2 加性）是 `app/domain/artifact.py` 的独立镜像；SDK
  `AgentResponse.artifacts` 与 CP 字段面对等（默认 None ⇒ 旧 Agent 完全兼容）。
  Artifact **不进 minimal wire**（同 metadata，15.5.5.2 冻结）。
- `ReasoningChunk` / `ContentChunk` / `TurnEnd` 是流式单轮的唯一 Chunk 集；
  `TurnEnd` 无业务载荷。

语义边界（Hard Rules，15.5.3 冻结）：
- `control_plane_session_id` 与 `external_session_id` 是**两个不同概念**：
  前者是 Control Plane 侧的不透明标识（SDK 不解释、不持久化），后者是
  Agent 侧自拥的会话引用；
- SDK 不保存 Session、不实现 SessionStore；`external_session_id` 只做
  input/output pass-through，Agent session state 完全由 Agent 作者自拥；
- 依赖闭包只允许标准库 + 本包相对导入（无 SQLAlchemy / Redis / FastAPI /
  httpx / anyio / 任何第三方依赖）。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentRequest:
    """Control Plane → Agent 的归一化调用请求（SDK 侧独立镜像）。

    与 Control Plane 契约字段面一致，但按 15.5.5.1 冻结：
    `agent_id` / `control_plane_session_id` 允许 `None`（SDK 侧不解释、
    只透传这两个标识）；`content` 是唯一必填字段。
    """

    content: str
    agent_id: int | None = None
    control_plane_session_id: int | None = None
    external_session_id: str | None = None
    metadata: dict | None = None


#: Artifact 类型判别常量（str 常量单一事实源；与 app/domain/artifact.py 镜像一致）。
ARTIFACT_KIND_FILE = "file"
ARTIFACT_KIND_DATA = "data"
ARTIFACT_KIND_LINK = "link"
ARTIFACT_KIND_IMAGE = "image"


@dataclass(frozen=True)
class Artifact:
    """Agent 工作成果值对象（SDK 侧独立镜像；字段面 == app/domain/artifact.py）。

    ACP V0.2 加性契约：Artifact 只随 `AgentResponse.artifacts` 透传，SDK 不存储、
    不投递、不流式——不参与 minimal wire（同 metadata，15.5.5.2 冻结）。
    """

    artifact_id: str
    kind: str
    name: str
    uri: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None
    metadata: dict | None = None


@dataclass(frozen=True)
class AgentResponse:
    """Agent → Control Plane 的归一化响应（SDK 侧独立镜像）。

    `external_session_id`：Agent 自拥的会话引用 —— 首轮可返回新建 id，
    后续轮从 `request.external_session_id` 透传回来。SDK 不保存 Session。
    `artifacts`：ACP V0.2 加性字段（默认 None ⇒ 旧 Agent / `AgentResponse(content=...)`
    完全向后兼容）。
    """

    content: str
    external_session_id: str | None = None
    metadata: dict | None = None
    artifacts: list[Artifact] | None = None


@dataclass(frozen=True)
class ReasoningChunk:
    """流式推理块（Agent 思考过程；不属于用户可见正文）。"""

    content: str


@dataclass(frozen=True)
class ContentChunk:
    """流式内容块（用户可见正文增量）。"""

    content: str


@dataclass(frozen=True)
class TurnEnd:
    """流式收尾信号（无业务载荷，15.5.5.1 冻结）。"""
