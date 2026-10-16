"""Dispatch Failure Boundary（Step 8.5）—— Agent Dispatch 的失败码（Fail Closed）。

与 8.2~8.4 各契约模块的失败码一样，本模块是 Runtime failure boundary 的一部分。
三个失败码互相独立、语义不可合并——后续运营层需要区分「Agent 失败了」还是
「只是响应太慢」：

- `AGENT_DISPATCH_FAILED`：Dispatch transport / protocol 失败——Connection
  endpoint 不可达 / Agent protocol 调用失败 / Adapter transport error；
- `AGENT_TIMEOUT`：超时，单独区分（Agent 失败了 vs 只是慢）；
- `AGENT_RESPONSE_INVALID`：Adapter 没有正确产生符合 AgentResponse Contract 的
  统一响应（响应无法满足 AgentResponse 字段面）。

Fail Closed：
- 没有确定 Connection → **不 Dispatch**；Agent 不可确定 → **不 Dispatch**；
- AgentResponse 无法验证 → **不继续 Runtime 成功链**；
- 绝不出现「Connection 不明确 → 随便挑一个 Agent」或「AgentResponse 不合法 →
  猜一个 content」。
"""

AGENT_DISPATCH_FAILED = "AGENT_DISPATCH_FAILED"
AGENT_TIMEOUT = "AGENT_TIMEOUT"
AGENT_RESPONSE_INVALID = "AGENT_RESPONSE_INVALID"
