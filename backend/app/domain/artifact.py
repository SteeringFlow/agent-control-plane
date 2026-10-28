"""Artifact —— Agent 工作成果值对象（ACP V0.2 Artifact Contract）。

定位：**Artifact = Agent 在一次执行中产出的、Control Plane 可观察的工作成果引用。**
它不是文件存储、不是投递载体、不是新的执行阶段——只是一段**结构化的事实描述**：
「Agent 产出了什么」。真实字节 / 对象的**存储**与**投递**属未来 Step（DEFERRED）。

为什么是值对象而不是实体（V0.2 冻结边界）：
- 它没有自己的生命周期（无状态机、无 phase API）；
- 它随 `AgentResponse` 一起被原样快照进 Interaction 的响应快照（单一事实源），
  Control Plane **不为它建表、不建 storage、不做 DB schema 变更**；
- 与项目其他契约（AgentResponse / RoutingResult / SessionResolution）同风格：
  plain frozen dataclass，**不内嵌校验**（Fail Closed 校验在生产者侧
  `AgentDispatchExecutor._valid_response`）。

字段语义：
- `artifact_id`：不透明唯一标识（CP 或 Agent 提供），必填——它是 artifact 的身份；
- `kind`：类型判别，用下方 `ARTIFACT_KIND_*` str 常量（与 `session_mode` /
  `processing_status` 单一事实源纪律一致，**不新建 Enum**）；不穷举，未知 kind
  由消费侧按 file-like 默认处理，不抛错；
- `name`：人类可读名，必填；
- `uri`：可解析位置，可为 None（延迟交付 / 纯引用）。**ACP 从不解析、不取回**它——
  V0.4 Phase 2 只做**结构与协议策略判定**（是否允许作为引用呈现），
  见 docs/step-acp-v0.4-artifact-uri-policy.md；
- `mime_type` / `size_bytes`：可选描述性元数据（**Agent 声明值**，ACP **未**验证）；
- `metadata`：protocol-neutral auxiliary（同 `AgentResponse.metadata` 边界）。

边界（V0.2 刻意不做，见 docs/step-acp-v0.2-artifact-contract-gate.md §7/§8）：
- **不流式**：Artifact 不进 Tencent SSE / minimal wire；
- **不存储**：无 artifact 表 / 列 / blob store / DB schema 变更；
- **不进 RuntimeExecutionResult**：Result 字段面恒三字段，Artifact 仅经
  `AgentResponse.artifacts` 顺带携带（不改变执行语义）。

**已交付的出站引用能力（ACP V0.3 Phase 1 / V0.4 Phase 2）**：
- Artifact **引用**可进 Delivery / ChannelAdapter / OutboundResponse，并在出站消息中
  **安全呈现**（见 docs/step-acp-v0.3-artifact-outbound-implementation-gate.md）；
- 呈现层**只判结构与协议**，**不取回字节、不代理、不缓存、不存储**，
  **不做可达性探测**，**不提供任何文件级访问授权**——
  「可呈现」≠「可访问」≠「可信」≠「已授权」。
"""

from dataclasses import dataclass

#: Artifact 类型判别常量（str 常量单一事实源；不建 Enum）。
ARTIFACT_KIND_FILE = "file"      # 落盘 / 可下载文件引用
ARTIFACT_KIND_DATA = "data"      # 结构化数据对象（JSON 等）
ARTIFACT_KIND_LINK = "link"      # 外链 / 资源指针
ARTIFACT_KIND_IMAGE = "image"    # 图片类资源


@dataclass(frozen=True)
class Artifact:
    """Agent 工作成果值对象（frozen；校验在生产者侧 Fail Closed）。"""

    artifact_id: str
    kind: str
    name: str
    uri: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None
    metadata: dict | None = None
