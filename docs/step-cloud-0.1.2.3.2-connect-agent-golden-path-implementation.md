# Cloud-0.1.2.3.2 —— Connect Agent Golden Path Implementation

> **OSS 发布说明**：本文为历史实现记录；文中引用的 `frontend/`（Web UI 源码）**不包含在当前 OSS 发行内容中**（见 README「Project status」）。

> 基线：Cloud-0.1.2.3（Connect Agent Golden Path Gate，GO：纯编排 / 接纳孤儿 Agent）
> → Cloud-0.1.2.3.1（Agent Endpoint Contract Alignment，GO）
> → Cloud-0.1.2.3.1.1（endpoint 空白归一化，已落地）
> → **本步（Golden Path 前端实现，后端零新增）**。

一句话目标：**把「Connect Agent」落成一个完整的用户任务体验，
而后端事实仍然只有三件——Agent created / Connection created / probe result。**

```
Workspace
   ↓  [ Connect Agent ]
connect-agent.html
   ① Agent Information
   ② Connection Configuration
   ↓
   Create Agent        → POST /api/agents
   ↓
   Create Connection   → POST /api/connections
   ↓
   Optional Probe      → GET  /api/connections/{id}/health
   ↓
   Connected / Actionable Failure
```

---

## 1. 交付面

| 文件 | 变化 |
|---|---|
| `frontend/connect-agent.html` | 新增：引导式单页（Agent Information → Connection Configuration → Progress/Result） |
| `frontend/connect-agent.js` | 新增：唯一交互逻辑——编排既有端点，处理孤儿 Agent / 探针 |
| `frontend/workspace.html` | `#cta-connect-agent` 指向 `/connect-agent.html`（原来指向 `/index.html`） |
| `frontend/styles.css` | 追加 `.step-title` / `.steps` / `.step-item` / `.step-ok|fail|pending`（复用既有变量，纯 CSS） |
| 前端 README（`frontend/` 整体不随初始 OSS 发布） | 记录本页与 Golden Path 语义 |
| `backend/tests/test_cloud_0_1_2_3_2_connect_agent_golden_path_ui.py` | 新增：15 项机器化护栏 |

**后端零改动**：无新 Service / 端点 / schema / 表 / 状态机 / 迁移。

---

## 2. 用户看到的流程（前端）

```
Workspace ──[Connect Agent]──► connect-agent.html
                                   │
   ① Agent Information：name / type / endpoint（可选）
   ② Connection Configuration：Channel（active-only）+ name + config(JSON)
                                   │  [Connect Agent]
   ▶ Progress：Create Agent → Create Connection → Test · Probe
                                   │
   ✅ Connected  ／  ⚠ Actionable Failure（含重试 / 管理 Agent）
```

- 进入页面先 `GET /api/channels` + `GET /api/agents` 拉参考数据；
  401 → 显示「先进入 Workspace」门（链接 `/workspace.html`）；
  Channel 下拉**只列 active**（镜像 11.10 焦点1）。
- 「填入 ACP 模板」把 `protocol/endpoint/bearer_token/runtime_id` 占位模板写入
  config 文本域（可编辑；占位值 `"<acpLink.token>"` 等，**非真实凭据**）。

---

## 3. 编排映射（前端动作 → 既有端点 → 后端事实）

| 步骤 | 前端动作 | 既有端点 | 后端事实 |
|---|---|---|---|
| ① Create Agent | `POST /api/agents {name,type,endpoint?}`；409 → 按 name 复用既有 Agent | `AgentApplicationService.create_agent` | Agent created（独立事务） |
| ② Create Connection | `POST /api/connections {channel_id,agent_id,name,config}` | `ConnectionApplicationService.create_connection` | Connection created（独立事务） |
| ③ Test · Probe | `GET /api/connections/{id}/health` | `ACPAdapter.probe` | probe result（瞬时，不落库） |

**只使用既有端点**：`/api/agents` + `/api/channels` + `/api/connections`
（含 `/{id}/health`）。无 `/api/connect` 之类新端点；本页只用 `POST` / `GET`
（编辑归管理页，无 `PATCH` / `DELETE`）。

---

## 4. Gate 裁决的落地

| 裁决（0.1.2.3） | 本步落地 |
|---|---|
| 纯编排，非实体 | 前端编排；后端无 `ConnectAgentService` / 无新端点 |
| 接纳孤儿 Agent，不回滚 | Agent 成功、Connection 失败 → 显式「部分成功」+「重试 Connection」（重提交既有端点）；**无 Saga / 补偿 / 删除** |
| 允许「只有 Agent 无 Connection」 | 部分成功面板展示 Agent id，并给「管理 Agent」入口（既有管理页做软下线） |
| Health Probe 非强制 | 成功后自动探活一次，同时保留「Test」按钮；探针失败 / 503 **不撤销** Connection |
| Connected = 瞬时派生 UI 态 | 徽标由最近一次 health 快照 `reachable` 派生，文案明示「不持久化」 |
| 失败允许 retry | 重试 = 重提交 `POST /api/connections`（重复绑定 → 409 由既有合同处理）；无 retry 框架 |
| 无 onboarding / provisioning 状态 | 无任何状态字段；UI 只呈现三件后端事实 |
| endpoint 可选元数据（0.1.2.3.1 / .1.1） | Agent 表单 endpoint「可选」，留空不发送；提示「不用于连接；实际访问地址在 Connection.config.endpoint」 |

---

## 5. 明确不做（与 Gate 一致）

`ConnectAgentService` / Workspace Entity / Integration Entity / onboarding state /
provisioning state / rollback / Saga / retry framework / secret manager /
新 Connection 状态机 / 新端点 / 新表。

---

## 6. 机器化护栏（Verification Gate）

`tests/test_cloud_0_1_2_3_2_connect_agent_golden_path_ui.py`（15 项，含**去注释**扫描）：

1. 落地：`connect-agent.html` / `connect-agent.js` 存在。
2. 静态可达：真实应用 `GET /connect-agent.html` / `/connect-agent.js` → 200；
   静态 mount 恒最后；`/api/agents`、`/api/connections` 在册。
3. Workspace CTA 指向 `/connect-agent.html`；README 记录本页。
4. 只引用既有端点（`/api/agents`+`/api/channels`+`/api/connections`）；
   无 `/api/users` / `/api/identities` / `/api/connect` / chat / websocket。
5. 后端无越界编排类（`ConnectAgentService`/`OnboardingState`/`Saga`/… 全无）。
6. 本页只用 `POST` / `GET`（无 `PATCH` / `DELETE` / `PUT`）。
7. 编排顺序：Agent → Connection → 可选 Probe（`/{id}/health`）。
8. `endpoint` 可选：仅在提供时携带，绝不当作必填。
9. 同名 Agent 409 → 复用既有 id（`createdNow: false`）。
10. 部分成功「部分成功」可见 + `btnRetry` 重试接线。
11. 无 `rollback` / `saga` / `compensat` / `DELETE`；两次独立 POST。
12. 无 onboarding / provisioning / first_success / connect_status。
13. 探针建议性：`err.status === 503` 分支 + 「不撤销 / 不持久化」；`snap.reachable`；UI 有 Test / badge。
14. 凭据仅以 Connection.config 占位模板出现（`"<acpLink.token>"`），无真实密钥、无 config 回显。

---

## 7. 验收

| 类别 | 验收项 | 结果 |
|---|---|---|
| 静态可达 | `/connect-agent.html` / `/connect-agent.js` 由应用托管（200） | ✅ |
| 纯编排 | 只用既有端点；后端零新增 | ✅（护栏 §6.4/6.5） |
| 孤儿 Agent | 部分成功显式展示 + 重试；无回滚 | ✅（护栏 §6.10/6.11） |
| Probe | 建议性、瞬时、不撤销 Connection | ✅（护栏 §6.13） |
| endpoint 可选 | 留空不阻断 | ✅（护栏 §6.8） |
| JS 语法 | `node --check connect-agent.js` OK | ✅ |
| 回归 | 受影响 UI/审计测试全绿 | ✅ |
| 回归 | 全量 `pytest -q`：**1862 passed / 5 skipped**；17 errors 均为 `test_step15_2_3` Windows `tmp_path` `PermissionError`（环境问题，与基线一致） | ✅ |

---

## 8. NOT TOUCHED（冻结边界）

| 类别 | 范围 | 理由 |
|---|---|---|
| **后端全域** | `app/**`（application / api / domain / persistence / adapters） | Golden Path 纯前端编排，后端零改动。 |
| **既有管理页** | `index.html`+`app.js`、`channels.*`、`connections.*`、`users.*`、`identities.*`、`interactions.*` | 互不耦合；只从 workspace 增加一个入口链接。 |
| **认证 / 租户** | `workspace.js`、`app/api/auth.py` | 只改 workspace.html 的 CTA href。 |
| **Runtime / Driver / Worker** | — | 不涉及。 |
