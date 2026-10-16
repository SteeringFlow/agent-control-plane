# M7-AUI-10.6.6 — Real WeCom E2E Success Baseline（真实 E2E 成功基线冻结）

> 状态：**已冻结**（验收基线记录文档，零运行时代码改动）。
> 事实来源：`M7-AUI-10.6.5 — Real WeCom E2E Success Verification Report`（既有验收报告）。
> 本文档职责：把 10.6.5 已确认的真实 E2E 成功证据正式记录为可追溯的验收基线。
> 本文档**不新增证据、不重跑 Verify / Bootstrap Arm、不修改任何业务数据**；
> 所有对象 ID 与结论均逐字来自 10.6.5 验收报告。

---

## 0. 文档边界（先读）

- 本基线记录的是 **单租户、单 Bot 渠道、单 Agent、两条真实消息** 的端到端成功。
- 它**不构成**：
  - 生产 GA（General Availability）宣告；
  - 多租户安全 / 多租户并发的已验证结论；
  - 「所有渠道形态（wecom webhook / wecom_bot / local）均已验证」的结论。
- 历史失败 Interaction 一律保留原判定，**不得**因本基线被追溯标记为成功（§5）。
- 本文档不含任何 `bot_id` / `bot_secret` / API Key / Token / 凭据材料。

---

## 1. 验收范围与环境

| 维度 | 内容 |
|---|---|
| 链路 | 真实企业微信（WeCom 智能机器人 WebSocket）→ SteeringFlow ACP → WorkBuddy Agent → 企业微信回复 |
| 渠道形态 | `wecom_bot`（WS 长连接 transport，`app/adapters/channels/wecom_bot.py`） |
| 租户 | Tenant 4 |
| 验收报告 | M7-AUI-10.6.5（真实 E2E Success Verification） |
| 前置步骤 | M7-AUI-10.6.2-B First Success Recovery（Home「检查首次成功」+ 重新验证引导） |

---

## 2. 对象关联（真实 ID，来自 10.6.5 验收报告）

| 对象 | ID | 名称 / 关键属性 |
|---|---|---|
| Tenant | 4 | 验收所在租户 |
| Channel | 15 | 「企业微信 11」，type=`wecom_bot`，状态 active |
| Connection | 16 | 绑定 Channel 15 与 Agent 12 |
| Agent | 12 | 「WorkBuddy 9」 |
| External Identity | channel_id=15，external_id=`XiangDong` | Identity id=1（user_id=NULL，未绑定 ACP 用户） |
| Conversation Grant | connection_conversations id=1 | channel_id=15 · external_conversation_id=`XiangDong` · connection_id=16 |
| Session | 1 | identity_id=1 · connection_id=16 · agent_id=12 |
| Bootstrap Arm | 13 | channel_id=15 · connection_id=16，由 Interaction 7 消费 |

---

## 3. 两条成功 Interaction（真实消息）

| Interaction | content | channel_id | processing_status | Agent response_payload | delivery_status |
|---|---|---|---|---|---|
| 7 | `Hello Boy 6` | 15 | `succeeded` | 存在（真实 Agent 产出） | `delivered` |
| 8 | `介绍一下你自己` | 15 | `succeeded` | 存在（真实 Agent 产出） | `delivered` |

两条消息均：同一目标 Channel（15）→ 真实 Agent response_payload → 真实平台投递成功（delivered）。

---

## 4. 因果链（Bootstrap Arm → Identity → Grant → Session → Agent Response → Delivery）

```
真实企业微信用户（external_id=XiangDong）发消息
      ↓  aibot_msg_callback（WS）
Channel 15（wecom_bot，active）
      ↓  Bootstrap Arm 13（channel_id=15 · connection_id=16）
      ↓  由 Interaction 7 消费（consumed_by_interaction_id=7 ·
         consumed_external_identity_id=XiangDong）
Identity id=1（channel_id=15 · external_id=XiangDong · user_id=NULL）
      ↓
Conversation Grant（connection_conversations id=1：
  channel_id=15 · external_conversation_id=XiangDong · connection_id=16）
      ↓  Authorization（既有 Grant 授权链）
Session id=1（identity_id=1 · connection_id=16 · agent_id=12）
      ↓  Routing → Agent 12（WorkBuddy 9）
真实 Agent response_payload
      ↓  aibot_respond_msg（WS 回复帧）
delivery_status = delivered（真实平台 ACK）
```

- Interaction 7 是该身份链的**首个真实成功交互**（消费了 Bootstrap Arm 13）；
- Interaction 8 在同一身份 / Grant / Session 链上复现成功（无需 Arm）。

---

## 5. 事故收敛与历史失败处置

### 5.1 事故收敛（真实事实）

- Channel 9、10、11 已停用（inactive）。
- Channel 15 保持 active。
- 同一 Bot 身份组中仅 Channel 15 保持 active。
- 运行时订阅调和（`WeComBotTransport.reconcile()` 的 REMOVE 语义）已确认
  Channel 9、10、11 的旧订阅停止。
- 历史失败 Interaction **全部保留原样**，未清空数据库、未改写任何历史判定。
- Home「检查首次成功」已基于真实数据判定成功。

### 5.2 历史失败的机制级根因

历史失败 Interaction 的**逐条明细以 10.6.5 验收报告与库内原样记录为准**（本文档
不改写、不重判）。其**机制级根因**由本任务配套审计
（`M7_AUI_10_6_6_DUPLICATE_BOT_CHANNEL_GUARD_AUDIT.md`，代码证据支持）确认为：

> 同一外部 Bot 身份（bot_id）被创建为多条 `wecom_bot` Channel（不同 channel_id、
> 不同名称均可通过现有 `(type, name)` 应用级唯一性检查），而系统对 bot_id **没有任何
> 唯一性防护**；`discover_bot_connections` 按渠道枚举、`reconcile()` 按 channel_id
> 建立绑定，于是同一 bot_id 产生**多条 WS 订阅**，同一入站消息在多条渠道上各自
> 进入 intake（interaction 幂等键是 `(channel_id, external_interaction_id)`，
> 跨 channel 不合并），造成历史交互失败/重复消费窗口。

恢复手段（已执行，属 10.6.5 事实）：人工停用 Channel 9/10/11，仅保留 Channel 15
active；订阅调和随后 REMOVE 旧绑定。

### 5.3 处置纪律

- 历史失败 Interaction 保持 `FAILED` / 原状态，**绝不**标记为成功；
- 不合并 Channel / Connection、不清理 Interaction / Activity / Session / Grant /
  Bootstrap Arm；
- 根因修复（重复 bot_id 防护）留待下一轮最小实现（见配套审计文档 §方案），
  本轮**只审计不实现**。

---

## 6. 验收结论与已知边界

**结论**：真实企业微信 → SteeringFlow → WorkBuddy Agent → 企业微信回复的端到端
链路在 Tenant 4 / Channel 15 / Agent 12 上，经两条真实消息（Interaction 7、8）
验证成功；因果链（Bootstrap Arm 13 → Identity 1 → Grant 1 → Session 1 →
Agent Response → Delivery）完整可追溯。

**已知边界（不得夸大）**：

1. 单租户成功 ≠ 多租户安全已验证（多租户隔离由 Cloud-0.1.1-B 等 Gate 单独覆盖）；
2. 单 Bot 渠道成功 ≠ 所有渠道形态已验证（`wecom` webhook 与 `local` 形态各有
   独立验收链）；
3. 单 Agent 成功 ≠ Agent 面已 GA；
4. 事故根因（重复 bot_id 渠道）当前**无系统级防护**，依赖人工纪律（见配套审计
   文档），该缺口是下一轮最小实现的输入；
5. WS transport 保持既有单实例前提（`wecom_bot.py` 模块 docstring 冻结的边界）。

---

## 7. 凭据声明

本文档及配套审计文档均不包含任何 `bot_id`、`bot_secret`、API Key、Token 或其他
凭据材料。Channel.config 的凭据字段在管理面响应中始终脱敏
（`app/schemas/channels.py::sanitize_channel_config`，`bot_secret` 属敏感键集合）。
