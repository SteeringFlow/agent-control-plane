"""Session 领域实体（Step 9.3：Session Vertical Slice 开始落地）。

一句话定位：**Session = Control Plane 中的一条会话**——某个 Channel 上的某个
Identity，在某条 Connection（Access Path）上，针对某个 Channel conversation
的会话。隔离键（8.3 冻结）：

    Identity.id × Connection.id × external_conversation_id

三条隔离红线：

- **Channel 边界**：actor 是 `Identity.id`，**不是 `User.id`**——同一 User 在
  WeCom / WeChat 的两个 Identity 是两条身份，永不共享会话；
- **Conversation 边界**：同一 Identity 的 conversation-X / conversation-Y 是两条
  会话（防止群聊 / 私聊上下文泄漏）；
- **Control Plane Session ≠ Agent Conversation**：`external_session_id` 只是
  Agent 侧的引用，不是本实体的替代品。

9.3 只落 **读路径** 所需的最小实体：Runtime 需要按上面的隔离键查 active
session，并把它的 id 交给 `SessionResolution`。会话的创建 / 更新与生命周期管理
属于后续 Slice——9.3 明确不做（`CREATE_NEW` 只是**决策**，不是立即 `create`）。

`created_at` / `updated_at` 是 DB 落库时间戳回显（只读）。
"""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Session:
    """一条 Control Plane 会话（Identity × Connection × conversation）。"""

    identity_id: int
    channel_id: int
    connection_id: int | None = None
    agent_id: int | None = None
    external_session_id: str | None = None
    external_conversation_id: str | None = None
    id: int | None = None
    status: str = "active"
    metadata: dict | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
