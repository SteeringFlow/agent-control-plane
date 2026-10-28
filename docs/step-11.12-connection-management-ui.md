# Step 11.12 — Connection Management UI

> **OSS 发布说明**：本文为历史实现记录；文中引用的 `frontend/`（Web UI 源码）**不包含在当前 OSS 发行内容中**（见 README「Project status」）。

> **Management 闭环收口（镜像 11.5/11.9 的 vanilla 形态，刻意克制）。**
> 11.5 Agent UI ✅ → 11.9 Channel UI ✅ → 11.11 Connection API ✅ → 本步 ✅。
> 至此 Agent / Channel / Connection 三实体管理闭环完整，且全部复用同一形态：
> 静态托管、无构建链、同源 /api、Product Acceptance Loop（保存后回列表复查）。

## 1. 落地面

- `frontend/connections.html`：List（Name | Channel | Agent | Status | Actions）+
  Form（Name / Channel / Agent / Status / Config / Credential Ref）；
- `frontend/connections.js`：唯一交互逻辑；复用 `styles.css`，零新增样式；
- 三页互链：`index.html` 与 `channels.html` 顶部导航各加
  「Connection Management →」；connections.html 回链 Agent / Channel。

## 2. 冻结 UI 合同（11.10/11.11 → UI）

### 解耦解析（11.11 合同 #1）
列表只返回绑定 ID → Channel / Agent 列由 UI 经 `/api/agents`、`/api/channels`
取 `id → 实体` Map 解析为 `name（#id）`；解析失败回退 `#id`，不阻塞列表。
UI 的 fetch 目标恰好 = `/api/connections` + `/api/channels` + `/api/agents`
（测试断言常量定义 + 禁 users/identities/chat/websocket）。

### active-only 下拉（11.10 焦点1）
新建表单的 Channel / Agent 下拉**仅列 active**（共享 `fillSelectActiveOnly`
helper；inactive 与 error Agent 均不进入）。后端 create 仍 existence-only——
下拉过滤纯属 UI 的 UX 选择。若无可选目标，提交时给出「请先创建」提示。

### immutable binding（11.10 焦点4）
```
Create:  channel_id ✓  agent_id ✓   （payload 唯一入口，字面量各出现一次）
Update:  channel_id ✗  agent_id ✗   （payload 从 { name } 起步，仅追加可变字段）
```
编辑态绑定以**只读文本**展示（不渲染下拉、js 无任何 `fChannel.value =` 赋值），
hint 说明换绑路径 = 停用当前 + 新建。后端 `extra="forbid"` 是第二道防线。

### credential_ref 写-only
编辑态显式清空输入框（`fCred.value = ""`）、绝不回显当前引用值（响应字段面
本就不含）；留空 = 保留当前引用，填写 = 轮换。

### config：无哨兵 + 整段替换
刻意不复制 Channel 的 `SECRET_PLACEHOLDER` / merge 逻辑（测试断言 js 无
哨兵常量、无 `********`）：config 是 Access Path 参数，所见即所存；
与回填原文一致 = 未修改 → 省略；有修改 → **整段替换**（11.11 冻结）。
UI 不预设任何业务键（token/corp_id/agentid/... 在 js 中零出现）。

### status 纯管理态
创建：固定 `active（默认）` 且 disabled（后端创建合同无 status 字段，
extra=forbid）；编辑：`（不修改）/ active / inactive`。列表提供
停用/启用动作（PATCH status，软下线）——error 不存在于任何表单选项。

## 3. 不做清单（用户 11.12，测试零出现守卫）

❌ Connection Health ❌ Connection Test ❌ Routing ❌ Failover ❌ Retry
❌ Capability ❌ Permission ❌ **Network Graph** ❌ Secret Vault ❌ Delete

其中 Network Graph 的裁决：Agent/Channel/Connection 三者虽已齐备，但「图该
展示什么」应等真实 Runtime 数据（Session / Interaction）进入产品后再判断，
证据会更充分——现在不做。

## 4. 守卫

新增 `tests/test_step11_12_connection_management_ui.py`（13 项）：落地事实 /
静态可达 + mount 恒最后 / 三页互链 / 解耦解析端点面 / 无 Delete / active-only
下拉 / 绑定不入 Update payload（字面量计数 = 1）/ credential_ref 写-only /
只读绑定 / status 管理态 / config 无哨兵+整段替换+无预设键 / 不做清单零出现。

翻转 2 处既有守卫：
- `test_step11_6::test_b2`：frontend「无 connection 文件」→「connection UI
  文件面 = {connections.html, connections.js}」；
- `test_step11_9::test_f`：channels.html 禁「connection」→ 剔除顶部导航行
  （页面互链非功能越界）后断言。

全量回归 **1127 passed**。

## 5. 下一步（候选）

Management 三切片闭环完成。可选方向（均需先审计再动刀）：
Runtime 可观测性消费（Interaction / Session 证据面）、Network View（等
Runtime 证据）、Credential Management（credential_ref 的解析/存储边界）。
