# Package 2 / Step 7 — Real WeCom E2E Operator Runbook

> 状态：**待执行**（测试与本文档已就绪，等待真实 WeCom 环境）。
> 交付物：`backend/tests/test_package_2_step_7_real_wecom_e2e.py`（env-gated pytest）
> + 本 runbook（人工执行记录）。
> **Step 7 不修改任何生产代码、不改 Package 2 已冻结实现、不加机器守卫。**

---

## 0. 为什么需要 runbook（pytest 无法自动化的部分）

pytest 能自动化**判定**，但不能替人在真实微信群里发消息。因此本步有两个职责不同的产物：

| 产物 | 职责 |
|---|---|
| `test_package_2_step_7_real_wecom_e2e.py` | 机器可重复的**判定**与证据落盘 |
| 本 runbook | 记录**人**做了什么、看到什么、首跑结果（含失败） |

**首跑事实必须留痕**：不得只保留最终成功的复跑结果而抹掉首跑。证据文件按时间戳命名，
永不覆盖；失败也要保留 `phases.<phase>.status = FAILED`。

---

## 1. 冻结的验收链

### Positive path

```
真实 WeCom 群（chatid）
      ↓  真实群成员发消息
真实 WS transport（aibot_msg_callback）
      ↓
Identity（user_id = NULL，未绑定 ACP User）        ← Package 1
      ↓
Conversation → Connection Grant                    ← Step 1 / Step 3
      ↓
Authorization ALLOW                                ← Step 2
      ↓  Routing → Session
local echo Agent（确定性）
      ↓
Interaction SUCCEEDED · DELIVERY DELIVERED
      ↓
WeCom ACK errcode = 0（真实平台）
```

### Negative path（**同一群、同一次运行内**）

```
删除 Grant
      ↓  同群再发一条
Authorization DENY
      ↓
processing=failed · delivery=pending
failure_stage=authorization · failure_code=UNAUTHORIZED · retryable=false
session_id=NULL · response_payload=NULL
      ↓
activities 含 stage_failure(authorization)
授权表确实没有该 grant
```

> 只证明 positive 无法区分「授权正确」与「系统恒允许」——**正反同场是必要条件**。

### Isolation

| 项 | 期望 |
|---|---|
| 群 A（有 grant） | ALLOW |
| 群 B（从未授权） | UNAUTHORIZED（不是群 A 的 grant 泄漏） |
| 同群**不同成员** | 不同 Identity（均 `user_id=NULL`）+ **同一会话级**授权 |

---

## 2. 变量控制（刻意只留一个变量）

```
WeCom        = real          Identity    = real runtime
Transport    = real          Authorization = real persistence
Routing      = real          Session     = real
Delivery     = real          Agent       = **local echo（确定性）**
```

Agent 刻意不用真实 ACP Runtime：否则失败时会多出 Agent 配置 / 网络 / 凭据 / 执行 /
超时等变量，降低本步的诊断价值。

---

## 3. ⚠️ 一个必须先知道的事实：未绑定身份**必须预先存在**

`IdentityResolutionExecutor` 按 ADR-5 **绝不创建** Identity（未找到即 Fail Closed），
而管理 API 的 `IdentityCreate.user_id` **必填**（V0 不暴露「创建未绑定身份」的入口，
Package 1 有守卫冻结这一点）。

**推论**：验收链里的「未绑定群成员」不是运行时造出来的，而是**开通侧先播种
`user_id=NULL` 的 Identity 行**（与 Package 1 测试同法）。运行时验的是
「解析到它并照常放行」。

→ 因此必须事先知道发消息成员的 **WeCom userid**，并列入 `ACP_REAL_WECOM_MEMBER_*_ID`。

> 这是一种**开通前提**，不是缺陷结论。若将来要支持「全新成员首条消息自动放行」，
> 那是新的技术变更，需另开 Package 裁决——**不在 Step 7 内处理**。

---

## 4. 前置条件（只有人能做）

- [ ] 一个真实企业微信机器人：`bot_id` / `bot_secret`
- [ ] 该 Bot 已加入**群 A**；另需**群 B**（从未授权，用于隔离项）
- [ ] 群 A 的 `chatid`
- [ ] 群 A 中两名成员的 WeCom userid（成员 A / 成员 B）
- [ ] 运行进程可出网到 `openws.work.weixin.qq.com`
- [ ] **backend 单实例**（`aibot_respond_msg` 必须经本进程持有的活连接）

凭据只经 env 或 `backend/.env`——**不进 fixture、不进文档、不进提交**。

---

## 5. env

| 变量 | 必填 | 说明 |
|---|---|---|
| `ACP_REAL_WECOM_BOT_ID` | ✅ | 机器人 id |
| `ACP_REAL_WECOM_BOT_SECRET` | ✅ | 机器人 secret（读时脱敏，不落盘） |
| `ACP_REAL_WECOM_GROUP_CHATID` | — | 群 A 的 chatid；**缺省则自动发现** |
| `ACP_REAL_WECOM_MEMBER_A_ID` | — | 成员 A 的 userid；**缺省则自动发现** |
| `ACP_REAL_WECOM_MEMBER_B_ID` | — | 成员 B；**缺省则由「另一位成员」当场发现** |
| `ACP_REAL_WECOM_GROUP_B_CHATID` | — | 群 B 的 chatid；**缺省则自动发现** |
| `ACP_REAL_WECOM_WS_URL` | — | 缺省 `wss://openws.work.weixin.qq.com` |
| `ACP_REAL_WECOM_WAIT_S` | — | 单个等待窗口秒数，默认 120 |

**缺 `BOT_ID` / `BOT_SECRET` → 整文件 skip**（普通 CI 不会因缺少真实 WeCom 环境而失败）。

### ⚠️ 群聊必须 **@机器人**

官方回调触发机制保证「群聊回调必为 @Bot 消息」——**不在群里 @ 机器人，官方根本不会
推送 `aibot_msg_callback`**，测试会看不到任何 interaction。每条消息都要 @。

### chatid / userid 的取得：bootstrap discovery（缺省即走此路）

企业微信界面不显示 chatid / userid，因此本测试**自动获取**：一条真实消息落库后，
interaction 快照（`request_payload`）里就有 `external_conversation_id`（= chatid）与
`external_identity_id`（= 发送者 userid）。流程是：

```
第一条消息（无 grant / 无身份）→ 失败但**仍产生 Interaction + 快照**
      ↓
反查 chatid + 发送者 userid
      ↓
就地播种 user_id=NULL 的未绑定身份（记为开通动作）
      ↓
建 grant → 正式 ALLOW 验收
```

它被**单独记录为 `bootstrap` 阶段**（`mode = bootstrap_discovery`），
**绝不混入 ALLOW 验收结论**。群 B 的 chatid 同样自动发现。

> 未绑定身份必须**预先存在**（运行时不创建，见 §3）；就地播种是**开通动作**，
> 每次都记入证据（`provisioned_in_phase:<interaction_id>`）。

---

## 6. 执行

```bash
cd backend
ACP_REAL_WECOM_BOT_ID=... \
ACP_REAL_WECOM_BOT_SECRET=... \
ACP_REAL_WECOM_WAIT_S=180 \
    python -m pytest tests/test_package_2_step_7_real_wecom_e2e.py -q -s
```

只需两项凭据——chatid / userid / 群 B 全部自动发现（§5）。

**必须 `-s`**（pytest 会打印阶段提示），且**每条消息都必须 @机器人**。

### 消息序列

| # | 位置 | 发送者 | 阶段 | 期望 |
|---|---|---|---|---|
| 1 | 群 A | 任意成员 | bootstrap | **本条会失败**（属预期）：自动反查 chatid + 播种未绑定身份 |
| 2 | 群 A | 同一成员 | positive | **ALLOW**：`succeeded` / `delivered`，群内收到机器人回复 |
| 3 | 群 A | **另一位成员** | second_member | **本条会失败**（属预期）：该成员尚无身份 → 就地播种 |
| 4 | 群 A | 同一位（第二位）成员 | second_member | **ALLOW**：不同 Identity、**同一**会话级授权 |
| 5 | 群 A | 任意成员 | deny | **UNAUTHORIZED**（grant 已被自动删除） |
| 6 | 群 B | 任意成员 | group_b | **UNAUTHORIZED**（隔离；chatid 自动发现） |

**节奏**：每条之间等 **20–30 秒**（尤其 #3 → #4：#4 是「重发」，必须在 #3 的失败被处理、
身份播种完成之后再发）。#2 与 #4 应在群里看到机器人回复。

阶段按**规范顺序**执行（状态机保证，即便 pytest 随机化测试顺序也不乱）；某阶段超时会
失败并保留证据，重跑即可（每次运行一个独立证据文件）。

### 若由 coding assistant 代跑

提示不会实时回显到操作员屏幕上（命令输出在结束后才返回），因此**按上表定时发送**即可，
不需要看提示。

---

## 7. 执行记录表（逐次填写；首跑与复跑**分表**保留）

### 7.1 环境

| 项 | 值 |
|---|---|
| 执行时间（本地 / UTC） | |
| 执行人 | |
| 代码版本（commit） | |
| backend 实例数（必须 1） | |
| 数据库 | |
| WeCom 机器人 id | |
| 群 A chatid | |
| 群 B chatid | |
| 成员 A / B 的 userid | |

### 7.2 开通

| 项 | 值 |
|---|---|
| Channel id（type=wecom_bot, status=active） | |
| Connection id | |
| Agent（local echo）id | |
| 播种的未绑定 Identity（user_id=NULL） | |
| Grant（conversation=chatid）id | |

### 7.3 第一次真实消息（ALLOW）

| 项 | 值 |
|---|---|
| 发送时间 | |
| 发送成员 | |
| **Interaction ID** | |
| external_conversation_id（应 = chatid） | |
| Identity.user_id（应 = NULL） | |
| processing_status（应 succeeded） | |
| delivery_status（应 delivered） | |
| session_id（应非 NULL） | |
| response_payload（应非空，echo 内容） | |
| 群内是否真的收到回复 | |
| ACK errcode（真实平台，应 0） | |

### 7.4 删除 Grant 后（DENY）

| 项 | 值 |
|---|---|
| 删除时间 / Grant id | |
| 第二次发送成员与时间 | |
| **Interaction ID** | |
| processing_status（应 failed） | |
| delivery_status（应 pending） | |
| failure_stage（应 authorization） | |
| failure_code（应 UNAUTHORIZED） | |
| retryable（应 false） | |
| session_id（应 NULL） | |
| response_payload（应 NULL） | |
| stage_failure 证据（应存在） | |
| 授权表最终状态（应无该 conversation） | |

### 7.5 隔离

| 项 | 值 |
|---|---|
| 群 B Interaction ID / failure_code | |
| 同群不同成员：Identity A vs B（应不同，均 NULL） | |
| 两者 conversation（应同为 chatid） | |

### 7.6 结果

| 项 | 值 |
|---|---|
| **首跑结果**（PASS / FAIL） | |
| 首跑失败点（若有） | |
| 证据文件路径（`backend/artifacts/step7-*.json`） | |
| 是否复跑（复跑结果与首跑**都**保留） | |

---

## 8. 失败时：先分层定位，**不得**先改 Runtime

```
WeCom WS（订阅/errcode）
   ↓ Adapter（inbound_from_callback / chatid）
   ↓ Interaction persistence
   ↓ Identity Resolution
   ↓ Authorization
   ↓ Routing
   ↓ Session
   ↓ Agent
   ↓ Delivery
   ↓ WeCom ACK
```

只有真实证据证明**当前 frozen architecture / implementation 有缺陷**，才进入新的
技术变更。首次失败先记录、先分层，不修生产代码。

常见误判提醒：

- **ACK 超时 10s 与 Agent 执行时长无关**（`DEFAULT_ACK_WAIT_TIMEOUT_S` 只覆盖
  「发帧 → errcode 返回」这一段）；若 Agent 慢，不要误判为授权问题。
- `IDENTITY_RESOLUTION_FAILED` 说明**发送者没有身份行**——把该成员的 userid 加入
  `ACP_REAL_WECOM_MEMBER_*_ID` 后重跑，不是授权失败。
- `ROUTING_FAILED`（不是 `UNAUTHORIZED`）说明有 grant 但候选不唯一/不可用。

---

## 9. 收尾（凭据卫生）

按 `docs/step-3b-2.6-wecom-bot-delivery-ack-validation.md` §G：

- [ ] 停进程
- [ ] 删临时脚本 / 临时 env
- [ ] 凭据残留扫描（工作树、`.env`、artifacts）
- [ ] 平台侧轮换 `bot_secret`
- [ ] 证据与 runbook 归档后再提交

> artifacts 内**不含** `bot_secret`（记为 `***configured***`），但仍需按 SOP 扫描。

---

## 10. 路线图定位

```
Step 5  PostgreSQL Closure        ✅
Step 6  Full Regression           ✅
Step 7  Real WeCom E2E            ⏳ 本步 = Package 2 最终验收
```

完成后统一整理 Package 2 的 documentation closure（含仍未提交的
Step 1 报告）。

---

## 11. 首跑记录（2026-09-19，实测）

> §7 的空表供**复跑**填写；本节是**首跑**实测事实，含失败，按要求保留。

### 11.1 环境与预检

| 项 | 值 |
|---|---|
| 执行时间（本地） | 2026-09-19 08:35:27（用时 9 分 09 秒） |
| 代码版本 | 未提交工作树（`b09349c` + Step 7 新文件） |
| backend 实例数 | 1（仅测试进程持有 Bot 连接） |
| 预检 | TCP 443 可达（183.47.120.160）；`aibot_subscribe` → **errcode=0** |
| 机器人 id | `aibzwp1ewSVcQhcpB-3WFJmcwh2lQQ9fIdp` |
| 群 A chatid | `wrpo0JRgAA8w1-w3Y7e_9PQRDpQjfe-g`（**自动发现**） |
| 群 B chatid | `wrpo0JRgAAdSinrIkK1KPkW5V2f2i7uw`（**自动发现**） |
| 成员 A / B userid | `XiangDong` / `steeringflow`（**自动发现**） |

### 11.2 开通

| 项 | 值 |
|---|---|
| Channel | id=1（`type=wecom_bot`, `status=active`） |
| Connection | id=1（active） |
| Agent | `local echo`（确定性） |
| 未绑定身份 | `XiangDong`、`steeringflow`（均 `user_id=NULL`，由 harness 播种并记入证据） |
| Grant | id=1 → conversation = 群 A chatid |

### 11.3 真实消息链（每次 interaction 的实测事实）

| # | interaction | 会话 | 发送者 | 结果 |
|---|---|---|---|---|
| 1 | 1 | 群 A | `XiangDong` | `IDENTITY_RESOLUTION_FAILED` — **bootstrap**：反查 chatid + 播种身份 |
| 2 | 2 | 群 A | `XiangDong` | **ALLOW**：`succeeded` / `delivered` / `session_id=1` / `"Echo: @ACP_Bot 2"` |
| 3 | 3 | 群 A | `steeringflow` | `IDENTITY_RESOLUTION_FAILED` — 就地播种第二位成员身份 |
| 4 | 4 | 群 A | `steeringflow` | **ALLOW**：`succeeded` / `delivered` / `session_id=2` / `"Echo: @ACP_Bot 4"` |
| 5 | 5 | 群 A | `steeringflow` | **UNAUTHORIZED**（`revoked=True`；`delivery=pending` / `session_id=NULL` / `response_payload=NULL`） |
| 6 | 6 | 群 B | `steeringflow` | **UNAUTHORIZED**（隔离） |

活动证据：ALLOW 两条 = `interaction_received` + `session_created` + `interaction_completed`；
DENY 两条 = `interaction_received` + `stage_failure(authorization)`。
`final_state`：`grants = []`，6 条 interaction 全部符合上表。

**Package 1 × Package 2 联合验证成立**：两条 ALLOW 的 Identity 均 `user_id=NULL`，
同一群（同一 conversation）的**不同成员**各自独立 Identity、共享**同一会话级**授权。

### 11.4 结果（含失败事实）

| 项 | 值 |
|---|---|
| 首跑结果 | **3 passed / 1 failed** |
| 唯一失败 | `test_step7_1`：**第一次** bootstrap 等待窗口（150s）到期——操作员尚未发出第 1 条消息 |
| 失败性质 | **协调/时序**，非产品缺陷：该窗口到期后，测试下次 `ensure` 重新进入 bootstrap，收到第 1 条消息即成功，随后整条链全部通过 |
| 是否影响验收结论 | **否**：`positive` / `second_member` / `deny` / `group_b` 四项断言全部通过，同一证据文件 |
| 证据文件 | `backend/artifacts/step7-real-wecom-20260919-003528-603.json` |
| 复跑 | 待确认（§7 空表备用） |

> 首跑的失败尝试现已写入证据文件的**独立键**
> `failed_attempt:<phase>:<n>`，不会被后续成功覆盖。

### 11.5 执行的边界（全程遵守）

```
✅ 未改任何生产代码 / Package 2 已冻结实现
✅ 未改 _wait_terminal、未加 sleep / 放宽 timeout / 重试
✅ 未加机器守卫
✅ 凭据仅内联 env，未写入 .env / 文档 / 提交
✅ 未提交任何代码（等验收确认）
```

**待办**：按 §9 轮换 `bot_secret`（凭据已在会话中出现过）。

---

## 12. 复跑记录（2026-09-19 09:13，**干净复跑 4/4**）

> 按要求首跑与复跑**都保留**。复跑走的是「**正常流程**」：chatid / 成员 id 事先已知，
> 跳过 bootstrap（即 §5 所述正常路径）。

| 项 | 值 |
|---|---|
| 结果 | **4 passed / 0 failed / 0 skipped**（108.80 秒） |
| 配置 | 群 A chatid + 成员 A/B **预置**；群 B chatid **未预设**（自动认领「非群 A 会话」） |
| 证据文件 | `backend/artifacts/step7-real-wecom-20260919-011331-249.json` |

| # | 阶段 | interaction | 身份 | 会话 | 结果 |
|---|---|---|---|---|---|
| 1 | positive | 1 | `XiangDong` | 群 A | `succeeded` / `delivered` / `session_id=1` / `"Echo: @ACP_Bot 111"` |
| 2 | second_member | 2 | `steeringflow` | 群 A（**同会话**） | `succeeded` / `delivered` / `session_id=2` / `"Echo: @ACP_Bot 222"` |
| 3 | deny | 3 | `steeringflow` | 群 A | `UNAUTHORIZED` / `session_id=NULL` / `response_payload=NULL` |
| 4 | group_b | 4 | `steeringflow` | 群 B（自动发现） | `UNAUTHORIZED` / `session_id=NULL` |

`setup.grants = []` → `positive.grants = [[1, 群 A]]` → `deny.revoked = True` → `grants = []`；
群 B 的 chatid 由本次消息自动认领。

### 12.1 中间一次复跑（09:08）为何是 3 passed / 1 skipped

中间那次把**首跑记录到的群 B chatid 当成了固定值**写进 env，而操作员本次发的
「群 B」消息来自**另一个会话**（`wrpo0JRgAAoad7…` ≠ 首跑的 `wrpo0JRgAAdSin…`）——
群 chatid 是稳定的，说明两次用的不是同一个群。阶段在等那个猜错的 chatid，于是超时，
记为 `NOT_OBSERVED`（**未静默当成通过**）。

**结论**：`ACP_REAL_WECOM_GROUP_B_CHATID` 不该硬编码一次观察值——改为**不预设、由
「非群 A 会话」自动认领**（§12 的配置），复跑即 4/4。这是 runbook 层面的操作结论，
不是产品缺陷。

### 12.2 两次运行的共同结论

```
Positive : 真实群 + grant → ALLOW → Session → local echo → delivered
           两条 ALLOW 的 Identity 均 user_id = NULL（Package 1）
           同群不同成员 → 不同 Identity、同一 conversation 级授权（Step 2）
Negative : 撤销 grant 后同群再发 → UNAUTHORIZED（冻结形状逐字命中）
Isolation: 从未授权的另一个群 → UNAUTHORIZED
无任何 authorization / routing / session / delivery 层缺陷证据
```
