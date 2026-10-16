"""ConnectionConversation — 授权状态领域实体（Implementation Package 2 / Step 1）。

一句话定位：**「这条 External Conversation 被允许使用这条 Connection」的记录。**

    Conversation（channel_id + external_conversation_id）
            │  ALLOW
            ▼
    Connection（connection_id）

它是 V0 **唯一**授权状态存储的领域镜像（Package 2-C §1.5 的窄范围例外）。

关键边界（全部为冻结红线）：

- **deny 不落库**：没有「拒绝行」——deny 由**缺行**表达
  （ADR-6：「缺授权记录 = 不允许」，允许必须显式成立）；
- 作用域是 **Conversation**，不是 Identity / User（ADR-1 / ADR-3）：
  因此群里不同成员**共用同一条授权**（`G1 → C1`，而不是逐人授权）；
- **不是 ACL / 权限表**（ADR-9 NON-GOAL）：没有 effect / priority / role /
  principal / scope；没有继承、没有条件组合；
- **行不可变**：改授权 = 删除 + 新建（与 Connection 绑定不可变同一思想），
  因此本实体没有 `update` 语义；
- `Conversation` **不是 ACP 实体**（ADR-3）：`external_conversation_id` 只是一个
  裸键列，与 `sessions.external_conversation_id` 同语义——**只在 Channel Scope 内解释**
  （群 `chatid` / 单聊 `userid` 都没有全局唯一性）。

tenant 归属：与其余领域实体一致，**本实体不携带 `tenant_id`**——
租户归属由 Persistence 层以自身列承载（`connection_conversations.tenant_id`），
**绝不**通过 `Connection → Tenant` 推导。
"""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ConnectionConversation:
    """一条「Conversation → Connection」授权（V0 唯一的授权状态记录）。"""

    channel_id: int
    external_conversation_id: str
    connection_id: int
    id: int | None = None
    # created_at / updated_at 是 DB 落库时间戳回显（只读，管理展示用）；写入时不携带。
    created_at: datetime | None = None
    updated_at: datetime | None = None
