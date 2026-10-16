"""Session Resolution Contract（Step 8.3）—— 冻结「输入 / 边界 / 输出决策」，不实现 Resolver。

SessionResolver 属 Runtime 组件，留到后续 Slice；8.3 只冻结契约。

8.3 回答的唯一问题：

    给定已经解析成功的 Identity（8.2）+ 一个**确定的** Connection（Access Path）
    + Channel-side conversation，Runtime 应该得到哪个 Session？

        Resolved Identity / User
              +
        Connection（已确定）
              +
        external_conversation_id
                  │
                  ▼
        Session Resolution（本模块冻结契约；不实现 Resolver）
                  │
                  ▼
        REUSE_ACTIVE：复用该 Connection 上已存在的 active session
        CREATE_NEW  ：没有 active session → 需要在该 Connection 上建立新会话

两条已经确定的事实（机器化锁定）：
1. **Session 必须保持 Channel 边界**：actor 组件是 channel-scoped 的
   `identity_id`（Identity.id，8.2 解析结果），**不是 User.id**——同一 User 在
   WeCom / WeChat 的两个 Identity 是两条身份，永不共享 Session；
2. **Session 必须保持 Conversation 边界**：`external_conversation_id` 是决策的
   一部分——同一 Identity 的 conversation-001 / conversation-002 是两条 Session。

查找语义（复用既有冻结的 find_active）：
    find_active(identity_id, connection_id, external_conversation_id)
    → 命中 → REUSE_ACTIVE（不新建重复会话）
    → 未命中 → CREATE_NEW

连接归属裁决（8.3 Contract 阶段必须回答的架构问题）：
    Connection 不是 Session Resolution 选出来的。Session 本身携带 connection_id
    + agent_id，但「这条 conversation 走哪条 Connection」属于 Routing（8.4）：
    全新 conversation 由 Routing 选定 Connection 后交给会话建立；既有 conversation
    必须沿用其 active session 已绑定的 Connection（会话粘性，不得每消息重选）。
    因此 8.3 的输入是一个**已经确定的 Connection**，本契约**不含**
    agent_id / 选择意图 / 策略字段——Connection 选择不在本 Slice 内。

Fail Closed：
    identity.channel_id != connection.channel_id（渠道错配）或拿不到确定 Connection
    时，不得凭空造会话 → SESSION_RESOLUTION_FAILED。
"""

from dataclasses import dataclass

# Runtime failure boundary：会话无法解析时的失败码（Fail Closed）。
SESSION_RESOLUTION_FAILED = "SESSION_RESOLUTION_FAILED"

# 会话决策模式
REUSE_ACTIVE = "REUSE_ACTIVE"   # find_active 命中：复用现有 active session
CREATE_NEW = "CREATE_NEW"       # 未命中：需要在该 Connection 上建立新会话


@dataclass(frozen=True)
class SessionResolution:
    """一次会话解析决策：给定 (identity, connection, conversation)，Runtime 用哪个 Session。

    字段语义：
    - `mode`：REUSE_ACTIVE（复用现有 active session）/ CREATE_NEW（需新建）；
    - `identity_id`：channel-scoped 的 Identity.id（8.2 解析结果）——不是
      User.id，天然保持 Channel 边界（同 User 不同渠道 = 不同 Identity）；
    - `connection_id`：**已确定**的 Connection（Access Path）id——选择归
      Routing（8.4），本契约不选 Connection；
    - `external_conversation_id`：Channel-side conversation——保持 Conversation
      边界（同 Identity 的不同 conversation 是不同 Session）；
    - `existing_session_id`：mode == REUSE_ACTIVE 时 = 复用的 active session id；
      mode == CREATE_NEW 时为 None；
    - `external_session_id`（15.5.2）：**Agent 侧**会话引用的只读投影——仅在
      REUSE_ACTIVE 时从已复用 Session 原样带出（`session.external_session_id`，
      可能为 None：首轮 Agent 尚未回流）；CREATE_NEW 恒为 None。本字段只是
      把已持久化事实带给 Runtime，Resolver 不解释、不创建、不管理 Agent Session。

    语义 guard（15.5.2 冻结）：`existing_session_id` 是 **Control Plane Session
    id**，`external_session_id` 是 **Agent / Agent Protocol 的 Session 引用**——
    两个命名空间互不相干，绝不混淆、绝不互相替代。
    """

    mode: str
    identity_id: int
    connection_id: int
    external_conversation_id: str
    existing_session_id: int | None = None
    external_session_id: str | None = None
