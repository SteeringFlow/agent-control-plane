# Step 11.5 — Agent Management UI

> **OSS 发布说明**：本文为历史实现记录；文中引用的 `frontend/`（Web UI 源码）**不包含在当前 OSS 发行内容中**（见 README「Project status」）。

> **第一条 Control Plane Management UX Vertical Slice。**
> 交付物 = frontend/ 三件套（生产代码）+ 组合根 1 处最小装配 + 本报告 +
> 语义守护测试（10 项）。最终全量回归：**1004 passed**。
>
> 与 11.4 的分工：11.4 验证「API 对不对」；11.5 验证「产品是否真的可用」——
> 一个没有看过代码的使用者，能否仅凭 UI 完成 Agent 的注册和修改？
> （真实浏览器驱动验证：能。）

---

## 1. 范围（严格按用户 11.5 指令：不做大而全）

```
Agent Management
├── Agent List        Name / Type / Endpoint / Status / Actions（编辑、停用|启用、刷新）
└── Agent Form        Name / Type / Endpoint / Credential Ref / Config / Status
```

用户能力闭环：**查看 → 创建 → 修改 → 重新查看**（+ 停用/启用）。

**明确不做**（锁死）：Connection / Channel / Routing / Session UI、
Network Graph、Health Check、**Agent Test Chat**、Capability、Permissions、
Deployment。理由（用户 11.5）：Test Chat 看似自然，实则把 Agent 注册与
Runtime 耦合——留待 Runtime 侧真实需求出现。

## 2. 技术形态：无框架、无构建、单服务

- `frontend/index.html` + `app.js` + `styles.css`，纯 vanilla；
- **组合根唯一改动**（`main.py`，+6 行）：`app.mount("/", StaticFiles(...))`
  托管 frontend/，同源访问 `/api`——无 CORS、无 Node 工具链、无新基础设施；
- mount 恒为**最后一个路由**（`test_a3` 断言注册顺序），`/api`、`/docs`、
  `/openapi.json`、`/health` 优先匹配不受影响。

## 3. 五条语义红线的落实（每条均有守护测试）

| # | 红线（11.2/11.4 冻结） | UI 落实 | 守护 |
| --- | --- | --- | --- |
| ① | credential_ref 写-only | 编辑表单**永不回显当前值**（`value=""` 恒空）；占位「留空 = 保留当前引用」；绝不出现 Current Credential Ref | `test_c1` |
| ② | status 双轴 | 表单只有 `active`/`inactive`；`error` 仅红色徽章展示 + 编辑态警示文案；恢复 = 列表「启用」（error → active 手动清障）；**编辑缺省「（不修改）」——保存不会静默改写运行态**（防：打开 error 态表单直接保存 → 意外停用） | `test_d1`/`test_d2` |
| ③ | type 受控 | `<select>` 仅 `acp`，无自由文本框 | `test_e1` |
| ④ | 无 Delete | UI 全文无删除动作；生命周期 = active ⇄ inactive | `test_b2` |
| ⑤ | config 不透明 | 原始 JSON 编辑（客户端校验 JSON 对象、整体替换/留空不改）；**不预设** timeout/model/transport/headers 等字段 | `test_e2` |

另：`test_b1` 断言 UI 全部 fetch 目标 ∈ `/api/agents`（不越界到
channels/connections/chat 等）。

## 4. Product Acceptance Loop（真实浏览器驱动，playwright 实测）

对 **真实 uvicorn + 真实 SQLite**（`http://127.0.0.1:8020/`）驱动真实浏览器：

| 步 | 操作 | 结果 |
| --- | --- | --- |
| 1 | 打开 `/` | Agent List 空态提示 ✅ |
| 2 | 新建表单：填 name/endpoint/credential/config → 创建 | 列表出现 `acp-prod / acp / https://... / active / 编辑 停用` ✅ |
| 3 | **DB 取证**（绕过 API 直读） | 行已落库：`credential_ref=vault://agent/acp-prod`、`config={"protocol":"tencent-acp-sse","timeout_s":30}` ✅ |
| 4 | 编辑表单 | **credential 输入为空**（占位「留空 = 保留当前引用」）、status 缺省「（不修改）」✅ |
| 5 | 改 endpoint + 轮转 credential + 换 config → 保存 | 列表 endpoint 更新；DB：`credential_ref=vault://agent/acp-prod-rotated`、`config={"timeout_s":60}` ✅ |
| 6 | 停用 → 启用 | active ⇄ inactive 往返，动作随状态切换 ✅ |
| 7 | Runtime 专有写路径置 `error`（DB 直写）→ 刷新 | 列表显示红色 `error` 徽章 + 「启用」动作 ✅ |
| 8 | 编辑 error 态 Agent | status「（不修改）」+ 警示「当前运行态为 error（不可选择）…」✅ |
| 9 | 列表「启用」 | error → active（唯一的管理覆盖运行态）✅ |
| 10 | 重复 name 创建 | 表单内联显示 `HTTP 409: Agent 已存在：acp-prod` ✅ |

**结论：不看代码的使用者可仅凭 UI 完成 Agent 注册与修改；错误反馈（409/422
detail）直接可达。**

## 5. 既有守护的翻转（事实推进，非放松）

- `test_step11_1.test_b3`：`frontend_is_placeholder_only`（占位断言）
  → **翻转**为 `frontend_now_has_real_agent_ui`（三件套存在断言）。
  与 11.3 翻转政策缺口断言同一模式；其余 11.1 守护未动。

## 6. 新增守护

`tests/test_step11_5_agent_management_ui.py`（10 项，静态断言 + 真实 app
可达性 + 路由注册顺序；**不触生产 DB**）：

```
A1 三件套存在            A2 组合根静态托管         A3 / 可达 + mount 恒最后 + API 路由在册
B1 fetch 只访问 /api/agents（禁越界概念）             B2 无 DELETE/删除
C1 credential 恒空 + 「留空=保留」占位 + 无回显语句   D1 status 选项无 error
D2 error 徽章可见 + 「启用」清障 + 「（不修改）」缺省  E1 type 受控 select  E2 config 无预设字段
```

## 7. 不做（与本步范围一致）

分页/过滤、多页面路由、组件框架/构建链、Connection/Channel 等其余管理面、
Test Chat、鉴权（无登录概念——内网管理面，未来按需单列步骤）。

## 8. 后续触发点（记录，不预建）

- adapter registry 新增类型 → `index.html` type 下拉加一项（受控集合扩边）；
- 真实出现「结构化 config 编辑」需求 → 由具体 Agent 类型字段驱动；
- Agent 数量增长到需要分页 → 与 API 分页（11.2 §5.4 预留）同一切片。
