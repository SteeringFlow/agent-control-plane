"""Routing Result Contract（Step 8.4）—— 冻结「输出 / 职责边界 / MVP 选择依据」，
不实现 Router，也不设计 Routing Rules。

8.4 **不做**：RoutingRule / RoutingPolicy / RoutingResolver / RoutingTable /
priority / weight / fallback / load_balance / round_robin。这些可能最终都需要，
但现在没有任何业务证据支持 MVP 引入规则引擎——只有真实用例出现「一个 Channel
多条 Connection 需要按用户/会话/内容选择」时才进一步增加规则。

Routing 在管道中的位置（正好兑现 8.3 的裁决：**Connection 在 Routing 中被选择**）：

    InboundInteraction
        ↓ Identity Resolution（8.2）
        ↓ Session Resolution（8.3）
        ↓
    Routing                    ← 本模块只冻结其输出契约
        ↓
    Connection（唯一、确定；Agent Access Path）
        ↓
    Agent

Routing 的核心问题（8.4）：
    给定一个已经完成 Identity / Session Resolution 的 inbound interaction，
    系统如何得到一条**唯一的、确定的** Connection（Agent Access Path）？

职责边界（不得职责倒流）：
    Routing
      ├── Resolve Identity  ❌（8.2 已完成；只能作为上下文消费，不得重新解析）
      ├── Resolve Session   ❌（8.3 已完成；不得创建/复用 Session）
      ├── Select Connection ✅（唯一职责）
      └── Dispatch Agent    ❌（Agent Dispatch 属于下一步）

MVP 选择依据（8.4 只采用当前真正需要的最小机制）：
    Channel → 唯一 Connection → Agent
    - 选择键只有 channel_id；resolved Identity / conversation 只是上下文，
      **不是选择键**（content 不参与选择）；
    - 结果必须唯一且确定：Channel 有且仅有一条 active Connection → 选它；
      0 条或 >1 条 → 无法唯一确定 → ROUTING_FAILED（Fail Closed）。

输出（RoutingResult）：
    { connection_id, agent_id }
    - connection_id：被选中的 Connection（Channel ↔ Agent Access Path）；
    - agent_id：**派生**自被选中 Connection（Connection.agent_id），不是独立
      选择——保证结果与 Connection 一致，不允许「选了 C 却派给别的 Agent」。
"""

from dataclasses import dataclass

# Runtime failure boundary：无法唯一确定 Connection 时的失败码（Fail Closed）。
ROUTING_FAILED = "ROUTING_FAILED"


@dataclass(frozen=True)
class RoutingResult:
    """Routing 输出：唯一确定的 Agent Access Path。

    - connection_id：被选中的 Connection（Channel ↔ Agent 受控访问路径）；
    - agent_id：派生自被选中 Connection 的 agent_id（非独立选择，避免不一致）。
    """

    connection_id: int
    agent_id: int
