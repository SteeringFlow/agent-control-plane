# Step 10.1.1 — ACP Adapter Contract Discovery

> Step 10 的第一刀。**先不实现 ACP**，只回答一个问题：Frozen `AgentAdapter` Port
> 是否足以承载一个真实 Agent Protocol？交付物 = Responsibility Map（成功标准）。

## 背景与代码范围

- 目标链路：`RuntimeCoordinator → AgentAdapter Port → ACPAdapter(未来) → ACP Agent`；
  Channel 侧暂用 Test Channel Adapter（只验证 Agent Adapter，不引入两个未知因素）。
- 本项目语境里 "ACP" = 对下游 `type="acp"` Agent 的通信协议（HTTP 承载，见 9.5/8.5），
  仓库内**没有具体 wire spec** → 协议细节以「问题清单」记录，待 10.1.2 连真实
  ACP-compatible Agent 确认。
- 本步**未新增生产文件**：`app/adapters/agents/acp.py` 推迟到 10.1.2 与真实实现一起
  落地（届时同步放开 `adapters/agents` 空壳守卫）；Frozen Contract 全部未改动。

## Discovery 裁决原则

如果 ACP 需要 X 而 `AgentRequest` 没有 X：**先不修改 AgentRequest**，判断 X 是
Adapter 内部协议细节，还是 Control Plane 真正需要拥有的概念。

- Adapter 内部：ACP transport / initialization / serialization / protocol metadata /
  authentication / session protocol / response normalization → 留在 ACPAdapter；
- CP 概念：Agent 选择（Routing）、Connection（CP 持有）、credential_ref（CP 持有）、
  Control Plane Session（Runtime）→ 由既有契约承载。

## ACP 需求问题清单（10.1.2 连真实 Agent 时逐项确认）

| 问题 | 归属判断 | Owner |
| --- | --- | --- |
| Agent 如何启动/连接？ | transport | ACPAdapter |
| Transport 是什么（HTTP/WS/streaming）？ | transport | ACPAdapter |
| initialize / capability negotiation 怎么做？ | protocol | ACPAdapter |
| Agent session 如何创建/复用？ | session protocol | ACPAdapter |
| message 如何发送/response 如何返回？ | protocol/serialization | ACPAdapter |
| streaming 是否必需？ | protocol | ACPAdapter（不提前做） |
| authentication 如何表达？ | protocol auth | ACPAdapter（凭据来自 connection.credential_ref） |
| external_session_id 如何映射？ | session 引用 | ACPAdapter / Agent |
| 一个 AgentRequest 是否足以完成一次 ACP 调用？ | → 裁决见下 | — |

## Responsibility Map（本步交付物）

| 问题 | Owner |
| --- | --- |
| Agent 选择 | Routing |
| Connection | Control Plane |
| credential_ref | Control Plane |
| ACP authentication | ACPAdapter |
| ACP transport | ACPAdapter |
| ACP serialization | ACPAdapter |
| ACP session protocol | ACPAdapter |
| Control Plane Session | Runtime |
| Agent external session | ACPAdapter / Agent |
| Agent response normalization | ACPAdapter |
| Delivery | DeliveryExecutor |

## 裁决：Frozen Port 形状是否足够？

**足够承载映射（形状级）**：

- `AgentRequest`（agent_id / control_plane_session_id / content /
  external_session_id? / metadata?）——routed agent + CP Session + 内容 + 已有 Agent
  引用，均来自 CP 已确定事实；
- `Connection`（含 credential_ref / config）——Adapter 连接/鉴权所需凭据与配置由 CP
  持有并在 send 时传入；
- `AgentResponse.external_session_id` —— Agent 侧新 session 引用回流通道存在。

协议内部细节（5 项）全部在 ACPAdapter 侧解析/构造，不要求 AgentRequest/AgentResponse
新增任何字段。若 10.1.2 连真实 Agent 后发现缺口，按「先判断 X 是协议细节还是 CP 概
念」处置——本步机器化锁定该处置原则未被破坏。

## 机器化验收

`tests/test_step10_1_1_acp_adapter_contract.py`（11 项）：
A Frozen 契约字段面未变；B AgentAdapter Port 仍是 `send(request, connection)` 且只依
赖 domain；C 协议内部概念（transport/auth/capability/negotiation/skill/stream/…）零泄
漏进 CP 契约字段；D Responsibility Map 自洽（每个 concern 一个 owner）；
E Port 输入（AgentRequest+Connection）已含映射所需数据、external_session_id 回流通道
存在；F 无 ACPAdapter 实现提前出现（adapters 仍空壳）；dispatch/coordinator 未变。

全仓 `pytest -q` = 614 passed，lint 0。
