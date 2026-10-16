# Cloud-0.1.5 — First-User / First-Success Validation Gate

> 阶段性质：**审计 + 最小前端收敛**（与 0.1.4「仅审计」不同——本 Gate 在用户可见
> 面上发现并关闭了若干真实违反项）。
>
> 本 Gate 把问题从「我们能不能把产品做出来？」转向「**一个完全不了解 ACP 内部架构的人，
> 能不能自己把它用起来？**」——只审查 5 件事：G1 零知识启动、G2 单一自然下一步、
> G3 配置符合真实用户认知、G4 失败可行动、G5 首次成功足够明确。
>
> 结论先行：**5 项全部 PASS（G1~G3 经最小前端收敛后 PASS；G4~G5 既有实现已满足，
> 本 Gate 仅补机器化护栏）。Backend = 0 changes；Runtime / Domain = 0 changes。**

---

## 一、Golden Path（零知识用户视角）

完全不了解 `Tenant / UoW / Connection / Runtime / webhook routing / ACP protocol`
的用户，唯一已知事实是「SteeringFlow 是 Agent Control Plane」。他应该走完：

```
Sign Up              → 注册即获得一个 Workspace（不知道 Tenant 是什么）
Workspace            → 只有唯一自然下一步：Connect Agent
Connect Agent        → 填 Agent 名 + 选 Channel + 访问配置 → Agent 与绑定就绪
Connect Channel      → 填 WeCom 凭据 + 选已有 Agent → Channel + 绑定 + Webhook
Send WeCom message   → 真实入站
First Success        → 由真实 Interaction 推导（message → Agent → Delivered）
```

本 Gate 不新增任何端点 / 实体 / 状态机；只审查既有 Golden Path 是否满足
零知识用户的可理解性与可行动性。

---

## 二、Gate DoD

| 裁决 | 结论 | 依据 |
|------|------|------|
| **G1**  零知识启动 | **PASS**（收敛后） | Workspace 不再暴露 "Tenant" 字眼；用户无需知道 Tenant/UoW/Connection/Runtime/ACP 即可走完路径；`/api/auth/me` 的 `tenant_id` 仅作内部身份，从不进入用户可见文案 |
| **G2**  单一自然下一步 | **PASS**（收敛后） | Workspace 主 CTA 恒为 `Connect Agent`；`Connect Channel` 作为次级引导 CTA 始终可见（指向页自身会 gate 回 Connect Agent）；Workspace 不链接任何管理页、只谈 Auth API（遵守 0.1.2.1/0.1.4.1 冻结门禁） |
| **G3**  配置符合真实用户认知 | **PASS**（收敛后） | 用户看到 Agent / Channel / WeCom / Webhook / 绑定名称 / Agent 访问配置；不再出现 `Connection config` / `Connection Configuration` / `AgentAdapter` / `Runtime` 等内部术语 |
| **G4**  失败可行动 | **PASS**（护栏补强） | 失败永远呈现「哪一步失败 + 为什么 + 下一步做什么 + 重试动作」；绝不只显示 `Error 500`；不引入新的错误状态机 |
| **G5**  首次成功足够明确 | **PASS**（护栏补强） | First Success 由真实 Interaction 的 `processing=succeeded × delivery=delivered` 推导，文案明确表达 `WeCom message → Agent → Delivered`；绝不等同于「配置成功」 |

---

## 三、各 Gate 的审计发现与处置

### G1 — Zero-Knowledge Start

**发现（收敛前 FAIL）**：`workspace.html` 向用户展示「你已进入自己的 **Tenant** Workspace」
与 `Workspace（Tenant）` 标签，并把内部 `tenant_id` 作为可见身份呈现。零知识用户
不知道「Tenant」为何物，违反 G1（无需理解内部术语即可启动）。

**处置**：
- `workspace.html`：移除 "Tenant" 字眼（"Tenant Workspace" → "Workspace"，
  `Workspace（Tenant）` → `Workspace`）。
- `workspace.js`：`/api/auth/me` 的 `tenant_id` 仍仅作内部身份使用，从不进入用户可见文案。
- 用户仍然无需知道 `UoW / Connection / Runtime / webhook routing / ACP protocol`
  即可走完 Golden Path——这些概念全部由前端编排隐藏在「Connect Agent / Connect Channel」
  两个用户任务背后。

### G2 — CTA 是否只有一个自然下一步

**发现与裁决**：G2 的真实意图是「不要把用户扔进一堆管理页面」，并给出清晰的自然顺序
`Workspace → Connect Agent → Connect Channel`。这里与两个**冻结门禁**发生了张力，
本 Gate 以冻结门禁为准、用最小方式满足 G2：

- 冻结门禁 `0.1.4.1 test_a3` **要求** Workspace 必须存在 Connect Channel CTA
  （即不允许把它从 Workspace 移除）；
- 冻结门禁 `0.1.2.1 test_workspace_ui_only_talks_to_auth_api` **要求** Workspace UI
  只与 Auth API 对话（不允许为「条件揭示 CTA」而新增 `GET /api/agents` 调用）。

**处置**（在冻结门禁约束内满足 G2）：
- `Connect Agent` 保持**唯一主 CTA**（`btn-primary`，既有 `0.1.2.3.2 test_a3` 锁定的
  `cta-connect-agent` 不变）；
- `Connect Channel` 作为**次级引导 CTA**（`btn-ghost`）始终可见，但明确为次要入口；
- 若用户尚未接入 Agent 就点开 Connect Channel，该页自身的 gate 会引导「先 Connect Agent」，
  因此整体仍形成 `Workspace → Connect Agent → Connect Channel` 的自然顺序；
- Workspace **不链接任何管理页**（无 `/index.html` / `/channels.html` 等），避免「一堆管理页面」；
- Workspace UI **只调用 Auth API**（不新增 `/api/agents` 等调用），遵守 `0.1.2.1` 冻结门禁。
  （早期曾尝试「默认隐藏、有 Agent 后揭示」方案，因需新增非 auth API 调用违反 `0.1.2.1`
  而放弃——这是 G2 与冻结门禁的已知取舍，记录备查。）

### G3 — Configuration 是否符合真实用户认知

**发现（收敛前 FAIL）**：`connect-agent.html` 使用 `② Connection Configuration`、
`Connection Name`；`connect-channel.html` 使用 `Connection Name` / `Connection Config`；
`connect-agent.html` 的 type 提示出现 `运行时` / `AgentAdapter` 等内部术语。零知识用户
不认识 `Connection config` / `Adapter` / `Runtime`。

**处置（保留既有 Gate 锁定的后端字段引用，只动用户可见标签）**：
- `connect-agent.html`：`② Connection Configuration` → `② Channel 与访问配置`；
  `Connection Name` → `绑定名称`；type 提示改为用户语言
  （「选择 SteeringFlow 连接你的 Agent 的方式（acp = 外部 Agent；local = 本地测试回声）」）。
- `connect-channel.html`：`Connection Name` → `绑定名称`；
  `Connection Config（JSON 对象，可选）` → `Agent 访问配置（JSON，可选）`。
- 保留 `Connection.config.endpoint` 这类**后端字段引用**（Gate 0.1.2.3.2 `test_c2`
  显式锁定，面向需要填 endpoint 的用户），因为它们指向真实 API 字段而非营销术语。
- `Connection` 作为「已创建的访问绑定事实」在结果区仍然出现（Gate 0.1.4 明确：
  「后端事实始终只有三件：Channel 已创建、Connection 已创建、Interaction 真实结果」），
  属于被既有架构冻结的可见事实，**不在 G3 禁止之列**（G3 禁止的是 `Connection config`
  这一**配置术语**，不是 `Connection` 实体名）。

### G4 — Failure 是否可行动

**现状（PASS）**：`connect-agent.js` / `connect-channel.js` 在两写两步失败时都：
1. 标记具体失败步骤（`markStep(stepX, "fail", …)`）；
2. 展示 `失败 —— <err.message>`（err.message 内含后端 typed detail，而非裸状态码）；
3. 显式展示「部分成功」并给出重试（`btnRetry` 重提交既有端点，不自动回滚）。
4. 探活 503 被识别为「探针不可用」而非连接失败。

**补强护栏**：本 Gate 机器化锁定——
- 失败处理必含 `resultError` 展示与 `err.message`（即原因可见）；
- 必含重试/恢复动作（`btnRetry.addEventListener(..., createConnection)`）；
- 不存在任何把 `Error 500` / 裸 `HTTP <status>` 直接吐给用户的路径；
- **不引入新的错误状态机**：Golden Path 前端与 Backend 均无新增 `ErrorState` /
  `FailureState` / error-state-machine 类或对象。

### G5 — First Success 是否足够明确

**现状（PASS）**：`connect-channel.js` 的 `checkFirstSuccess()` 由
`GET /api/interactions` 的原生双状态推导——`processing=succeeded × delivery=delivered`，
文案为 `WeCom message → Agent → Delivered`，并回显 `Interaction #<id>` 让用户确知
「我的 WeCom 消息真的经过 SteeringFlow → Agent → Response → Delivery」。它**明确不等同于**
「配置成功」（页面分别给出 `configuration ready` 与 `first success` 两个 badge）。

**补强护栏**：本 Gate 机器化锁定 `PROCESSING_SUCCEEDED="succeeded"` /
`DELIVERY_DELIVERED="delivered"` 常量与双状态判定，锁定成功文案表达真实运行链路，
并锁定 `Channel 创建成功` 绝不被视为 First Success。

---

## 四、变更面汇总

```
Runtime semantic diff:   ZERO
Domain changes:          ZERO
New entities:            ZERO
New endpoints:           ZERO
Backend code changes:    ZERO      （仅前端用户可见面收敛 + 机器化护栏测试）

Frontend（仅用户可见标签 / CTA 主次，无新框架 / 无构建链 / 无新增 API 调用）：
  workspace.html         Tenant 字眼移除；CTA 主次化（Connect Agent 主 / Connect Channel 次）
  workspace.js           无新增非 auth API 调用（遵守 0.1.2.1 冻结门禁）
  connect-agent.html     Connection Configuration/Name → Channel 与访问配置/绑定名称；去 Runtime/AgentAdapter 术语
  connect-channel.html   Connection Name/Config → 绑定名称/Agent 访问配置；去 ChannelAdapter 术语
```

### 机器化护栏（machine guards）

测试文件：`backend/tests/test_cloud_0_1_5_first_user_first_success.py`

```
覆盖：
  G1  workspace.html 无 "Tenant" 用户可见字眼；tenant_id 仅内部使用
  G2  Workspace 主 CTA 恒为 Connect Agent（btn-primary）；Connect Channel 为次级
      （btn-ghost）引导 CTA；Workspace UI 仅谈 Auth API（不调 /api/agents 等）；
      不链接任何管理页
  G3  金路径文件无 TenantScope / Connection config / Connection Configuration /
      AgentAdapter / Runtime / Adapter 用户可见术语
  G4  失败必含步骤+原因+恢复动作；无裸 Error 500；无新错误状态机
  G5  First Success 由真实 Interaction 双状态推导；文案表达运行链路；
      不等于「配置成功」
```

---

## 五、明确禁止事项（forbidden）

```
❌ 在用户可见面暴露 Tenant / UoW / Runtime / ACP protocol / webhook routing 术语
❌ Workspace 平铺多个并列 CTA（必须单一自然下一步）
❌ 用 Connection config / Adapter / Runtime 等内部术语做用户标签
❌ 失败只显示 Error 500（必须：哪一步 + 为什么 + 下一步 + 重试）
❌ 把「配置成功 / Channel 创建成功」当作 First Success
❌ 为 G4 引入新的错误状态机 / 错误枚举体系
❌ 为上述任一 Gate 新增后端端点 / 实体 / 状态列
```

---

## 六、给后续步骤的约束

1. 任何面向 First-User 的页面都不得（重新）暴露 `Tenant` 等内部术语；
2. Workspace 永远只有一个主 CTA（除非前置事实已成立，否则不揭示下一步入口）；
3. 用户标签使用 Agent / Channel / WeCom / Webhook / 绑定 等真实认知词，
   内部字段名（如 `Connection.config.endpoint`）仅作为面向高级用户的 API 引用出现；
4. 失败展示的三要素（步骤 / 原因 / 下一步）不可省略，且不得引入错误状态机；
5. First Success 永远由真实 Interaction 双状态推导，UI 不得写入任何 onboarding /
   first_success 持久态。
