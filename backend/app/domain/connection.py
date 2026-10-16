"""Connection 领域实体（Step 7.1 Connection Domain Contract）。

一句话定位：**Connection = Control Plane 中一个 Channel 与一个 Agent 之间的
受控访问路径（Access Path）。**

    Channel 1 ───── N Connection N ───── 1 Agent

它不是一次运行（Session/Interaction），不是路由（Router），不是权限策略
（Policy/Capability/RBAC），也不是 Agent Runtime。

关键冻结契约：
- 唯一性：UNIQUE(channel_id, agent_id)（4.2 冻结）——一个 Channel 到一个 Agent
  只有一条受控 Connection；
- name：管理员的显示名，**不承担唯一性**（无 find_by_name、无 name UNIQUE）；
- status：该 Access Path 当前是否可用（沿用 active/inactive 字符串）；
  不是 HTTP status，也不是 Interaction.processing_status 那种运行状态；
- config：只保存 Channel→Agent 连接本身的配置（timeout / protocol_options ...），
  不是「万能配置容器」（不得长成 permissions/capabilities/routing_rules/...）；
- credential_ref：只存凭据**引用**（"cred_xxx"）；如何解析/存储/加密属于
  Credential / Adapter / Infrastructure 边界，当前 MVP 不做 Credential
  Management；
- 绑定不可变：channel_id / agent_id 是 immutable binding——改绑定 = 删除旧
  Access Path + 建立新 Access Path（与 6.6「Identity Update ≠ Identity Linking」
  同一架构思想）；
- 不负责判断「谁有权限用 Agent」：Connection 只是 Access Path，
  Authorization / Policy / Capability 以后单独做。

created_at / updated_at 是 DB 落库的时间戳回显（只读，管理展示用）；
写入时不携带（由 DB server_default 生成）。
"""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Connection:
    """Channel ↔ Agent 受控访问路径。"""

    channel_id: int
    agent_id: int
    name: str
    id: int | None = None
    status: str = "active"
    config: dict | None = None
    credential_ref: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
