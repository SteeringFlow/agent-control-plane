"""ActivityRecord —— Activity ORM（4.2 冻结 schema）的记录域镜像（Step 10.4.2）。

Append-only（Evidence / Execution Record，不是 Audit Platform）：

- 只有写入，没有更新——历史事实不允许被改写（ORM 无 updated_at，
  Repository Port 不提供 update/delete）；
- `session_id` 可空（10.4.2 Schema 修正案）：Session 建立之前的阶段失败
  Evidence 同样需要被记录；
- `stage` 覆盖 Runtime 六段：identity_resolution / routing /
  session_resolution / session_creation / agent_dispatch / delivery。
  **session_creation 是条件阶段**：REUSE_ACTIVE 不产生任何
  session_creation Evidence（Evidence = 事实，不凭空制造）。
"""

from dataclasses import dataclass
from datetime import datetime

STAGE_IDENTITY_RESOLUTION = "identity_resolution"
# Implementation Package 2 / Step 2：Authorization 阶段（ADR-7：在 Session
# Resolution 之前；拒绝仍产生 Interaction + stage_failure Evidence）。
STAGE_AUTHORIZATION = "authorization"
STAGE_ROUTING = "routing"
STAGE_SESSION_RESOLUTION = "session_resolution"
STAGE_SESSION_CREATION = "session_creation"
STAGE_AGENT_DISPATCH = "agent_dispatch"
STAGE_DELIVERY = "delivery"

ACTIVITY_RECEIVED = "interaction_received"
ACTIVITY_STAGE_FAILURE = "stage_failure"
ACTIVITY_SESSION_CREATED = "session_created"
ACTIVITY_COMPLETED = "interaction_completed"


@dataclass(frozen=True)
class ActivityRecord:
    """一条 Runtime 执行 Evidence（activities 行，append-only）。"""

    type: str
    status: str
    stage: str | None = None
    message: str | None = None
    metadata: dict | None = None
    interaction_id: int | None = None
    session_id: int | None = None
    id: int | None = None
    # 13.1 Observability 读面：DB 生成的 created_at，只由 mapper to_domain
    # 读回（时间线排序/回显）；to_orm 写路径不携带（仍由数据库生成）。
    created_at: datetime | None = None
