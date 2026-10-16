"""InboundInteraction —— Runtime 入口契约（Step 8.1 Inbound Interaction & Runtime Boundary Contract）。

一句话定位：**InboundInteraction = Channel Adapter 完成协议归一化后，
Control Plane 接收到的一次外部输入**（Runtime 内部的标准 Interaction Contract）。

    External Channel (WeCom)
          │
          ▼
    Channel Adapter      ← Adapter world：协议解析 / 认证 / 归一化
          │
          ▼
    InboundInteraction    ← 本契约（两个世界的分界点）
          │
          ▼
    [Runtime]            ← Runtime world：Identity / Session / Routing /
                            Connection / Dispatch
          │
          ▼
    Agent

它只表达「外部世界说了什么」，不表达「内部控制平面如何解读 / 派发它」。
identity 归属、session 归属、路由到哪个 Agent 都是 Runtime 的职责。

关键边界（与既有冻结保持一致，机器化校验见
tests/test_step8_1_inbound_interaction_contract.py）：

- **≠ Identity**：`external_identity_id` 只是 Channel 世界的外部身份标识
  （如 WeCom 的 userid / 钉钉的 unionid），**不是** `User.id`，也**不是**
  `Identity.id`（6.6 冻结的内部绑定主键）。把它当成内部 id = 绕过
  Identity Resolution。因此本契约**不含** `user_id` / `identity_id`，
  Adapter 也没有机会提前固化内部归属；
- **≠ Session**：不含 `session_id`；会话归属留给 SessionResolver；
- **≠ Interaction ORM**：`interactions` 表（4.2/5.3 冻结）是会话内交互的
  **持久化记录**（含 session FK、direction、payload 快照、processing/
  delivery 状态）；InboundInteraction 是一次性、内存态的值对象，
  **永不落库**——它没有 id / created_at / status，也不会有
  model / mapper / repository / ApplicationService；
- **≠ AgentRequest**：不含路由决策字段（无 `agent_id` / `connection_id` /
  `session_id` / capability / policy / routing），那是 Runtime Router 的事；
- **两世界不混**：Adapter world（解析/认证/归一化）与 Runtime world
  （Identity / Session / Routing / Connection / Dispatch）以本契约为分界，
  任何一侧的职责都不得写入另一侧。

字段说明：
- `channel_id`：内部 `Channel.id`（Adapter 已完成“这条消息来自哪个受控渠道”的归一化）；
- `external_interaction_id`：渠道侧交互 ID（幂等 / 渠道去重依据）；
- `external_conversation_id`：渠道侧会话 / 群 ID；
- `external_identity_id`：渠道侧发送者身份 ID（≠ 内部 Identity.id）；
- `content`：归一化后的文本内容；
- `metadata`：可选结构化扩展（与 Connection.config 同纪律——不做万能容器约定，
  不承载凭据 / 策略 / 路由信息）。
"""

from dataclasses import dataclass

#: M6.6-C3 / ADR-12 §13：渠道**会话形态**（single / group）在 metadata 中的承载键。
#:
#: 为什么走 metadata 而不是新增契约字段：8.1 的六字段契约面冻结，且 `metadata`
#: 已被 `interaction_snapshot` **恒存并重建**（12.9.4.3）⇒ 它天然往返到 Worker，
#: 不需要新列、不需要改 snapshot。协议知识仍只属于 Adapter 层——Core 只读这个键，
#: **绝不**用「conversation != identity ⇒ 群聊」之类的启发式推断。
CHAT_TYPE_METADATA_KEY = "chat_type"

#: 单聊（`wecom` / `wecom_bot` 的 `chattype=single`、`local` 的单会话形态）。
CHAT_TYPE_SINGLE = "single"

#: 群聊（当前仅 `wecom_bot` 的 `chattype=group` 会产出）。
CHAT_TYPE_GROUP = "group"


@dataclass(frozen=True)
class InboundInteraction:
    """Channel Adapter 归一化后的入站交互（Runtime 入口契约，值对象，不落库）。"""

    channel_id: int
    external_interaction_id: str
    external_conversation_id: str
    external_identity_id: str
    content: str
    metadata: dict | None = None
