# Step 8.1 —— Inbound Interaction & Runtime Boundary Contract

一句话目标：Runtime 的第一块地基——冻结「外部消息进入 Control Plane 后，
内部以什么标准对象表示」的 **InboundInteraction** 契约。

    External Channel (WeCom)
          │
          ▼
    Channel Adapter        ← Adapter world：协议解析 / 认证 / 归一化
          │
          ▼
    InboundInteraction     ← 本步骤冻结（两个世界的分界点）
          │
          ▼
    [Runtime]              ← Runtime world：Identity / Session / Routing /
                               Connection / Dispatch（尚未实现）
          │
          ▼
    Agent

## 1. 契约（冻结在 `app/domain/inbound_interaction.py`）

纯 dataclass（frozen），字段 = 恰好 6 个，顺序冻结：

| 字段 | 类型 | 必填 | 含义 |
| --- | --- | --- | --- |
| `channel_id` | `int` | 是 | 内部 `Channel.id`（Adapter 已归一化「来自哪个受控渠道」） |
| `external_interaction_id` | `str` | 是 | 渠道侧交互 ID（幂等 / 渠道去重） |
| `external_conversation_id` | `str` | 是 | 渠道侧会话 / 群 ID |
| `external_identity_id` | `str` | 是 | 渠道侧发送者身份 ID |
| `content` | `str` | 是 | 归一化后的文本内容 |
| `metadata` | `dict | None` | 否 | 可选结构化扩展（默认 None） |

## 2. 边界（机器化锁定）

| 边界 | 含义 | 锁死方式 |
| --- | --- | --- |
| ≠ Identity | `external_identity_id` 只是渠道侧标识（WeCom userid 等），**不是** `User.id` / `Identity.id` | 契约不含 `user_id` / `identity_id`；解析归未来的 IdentityResolver |
| ≠ Session | 不含 `session_id`，会话归属留给 SessionResolver | 无 `session_id` 字段 |
| ≠ Interaction ORM | `interactions` 表（4.2/5.3）是持久化交互记录；本契约是一次性内存值对象 | 无 `id`/`created_at`/`status`；无 model/mapper/repository/ApplicationService |
| ≠ AgentRequest | 不含路由决策（无 `agent_id`/`connection_id`/policy/capability/routing） | 字段面 = 恰好的 6 个；无派发语义 |
| 两世界不混 | Adapter 负责解析/认证/归一化；Runtime 负责 Identity/Session/Routing/Connection/Dispatch | Adapter 目录仍空壳；Runtime 组件（Resolver/Router/Dispatcher）不存在 |

## 3. 反向审计扩边（只放行冻结契约）

7.3 的反向审计把 Session/Interaction/Router/Resolver/... 列为禁止项。8.1
**主动冻结** `InboundInteraction` 后，审计做**精确白名单**放行：

- 只放行 `app/domain/inbound_interaction.py::InboundInteraction` 这一个；
- 任何其他 "interaction/session/..." 命名的类、函数、文件仍是偷渡；
- mutation 测试新增第 3 种探测：**在白名单文件之外重声明**
  `class InboundInteraction`（逃逸白名单）→ 必须报红。

## 4. 验收

`tests/test_step8_1_inbound_interaction_contract.py`：13 项全绿
（契约纯性 / 字段面冻结 / 类型与必填性 / 无内部 id / 运行时 frozen /
不落库 / interactions 表独立 / 两世界空壳 / 唯一 interaction 命名类 /
7.3 审计一致性）。

`python -m pytest -q` → **302 passed**（289 回归 + 8.1 新增 13）。

后续：IdentityResolver / SessionResolver / Router（8.2+）→ WeCom Vertical Slice。
