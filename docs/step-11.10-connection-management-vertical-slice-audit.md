# Step 11.10 — Connection Management Vertical Slice Audit（再审计）

> **审计步（生产代码 0 行）。** 11.6 已完成 Connection 后端审计，并裁决「先做
> Channel，因为 Connection 强依赖一个可管理的 Channel，而当时生产环境 Channel
> 数量为 0」。截至本步，Channel Management 已完整落地（11.8 API + 11.9 UI），
> 11.6 的阻塞前置**已解除**。本步在「UI/API 联动前」对 Connection 真实现状做
> **第二次冻结**：逐条回答用户五个焦点问题，固化 Connection Management API/UI
> 的合同边界，并锁定「后端唯一缺口 = `list_connections`」。

---

## 0. 就绪度账本（相对 11.6 的更新）

```
Agent       ✅ 完整（Domain→UI，11.2~11.5）
Channel     ✅ 完整 + ✅ 可管理（11.8 API + 11.9 UI）→ 11.6 阻塞前置已解除
Connection  ✅ 后端完整   ❌ 缺 API/UI   → 但「可接线」的前置现已满足
```

11.6 的 Q8/Q9 结论（生产无法造 Channel → 无法造 Connection）**在本步翻转**：
`ChannelApplicationService.create_channel` 已通过 `api/management/channels.py`
（`build_channels_api`，11.8）在 `main.py` 装配，且有 `channels.html/channels.js`
（11.9）真实写入路径。因此一个真实的 Channel 已可由管理员在生产环境创建，
Connection 的创建闭环在**产品层面**首次可行。

**结论：Connection 后端 100% 就绪（仅一处口径缺口，见 §6）；下一刀 = Connection
Management API（镜像 11.3/11.8）+ UI（镜像 11.5/11.9），几乎零后端阻力。**

---

## 1. 焦点 1 —— Channel / Agent 下拉取什么状态的数据

```
Channel 1 ───── N Connection N ───── 1 Agent
```

- **`新建 Connection` 表单的两个下拉（channel_id / agent_id）= 仅列 `active`**：
  - 你不会把一个新 Access Path 绑到一个当前不可运营的对象上；
  - 因此 **`inactive` 与 `error` Agent 均排除**于新建下拉（error 是 Agent 的
    运行态信号，更不该成为新绑定的目标）。
- **后端 API 保持 existence-only（不加 status 门禁）**：`create_connection` 只校验
  `channel_id / agent_id` 存在（→ `ChannelNotFoundError / AgentNotFoundError`），
  **不引入任何 `status` 比较**。理由：把「运行态过滤」塞进管理 API 会污染最小接口、
  并悄悄复制 Agent 的双轴语义——下拉过滤是 **UI 层 UX 选择**，不是后端契约。
- **编辑既有 Connection 时绑定为只读**：`channel_id / agent_id` 不可变
  （见 §4），编辑表单**不渲染下拉**，只显示已绑定的 channel/agent 标识文本 +
  名称（名称由 UI 经 `/api/channels`、`/api/agents` 解析，§5）。

冻结：下拉 active-only（UI）；后端 existence-only（API/Service）。

## 2. 焦点 2 —— Connection config 的真实语义

Connection.config = **这条 Channel→Agent Access Path 本身的参数**
（`timeout / protocol_options ...`），是连接级参数，**不是万能配置容器**
（不得长成 `permissions / capabilities / routing_rules`），也**不是** Channel/Agent
自身的 config。

三条硬边界：

1. **不复制 Channel secret 语义**：Channel.config 有 `sanitize_channel_config()`
   脱敏 + `********` 哨兵（11.7/11.8）——**Connection.config 不做任何脱敏、不出现
   `********`、不引入敏感键集合**。它天生就不是凭据容器。
2. **`credential_ref` 是写-only 引用（复制 Agent 护栏）**：只存 `cred_xxx` 引用，
   响应**永不回显**（字段「不在场」排除，与 `AgentResponse` 同一手法，11.3
   合同 #1）。Connection.credential_ref 与 Agent.credential_ref 是两表两概念
   （前者=Access Path 鉴权引用，后者=Agent 运行时凭据引用），平行不替代——但
   **「引用入、引用不出」的字段面护栏必须被 11.11 的 `ConnectionResponse` 复用**。
3. **config 不透明 JSON**：UI 不预设字段（与 Channel/Agent 一致）。

冻结：Connection.config = 不透明 Access Path 参数；Connection 响应排除
`credential_ref`；Connection 绝不引入 secret 脱敏/哨兵。

## 3. 焦点 3 —— Connection status 纯管理态

- Domain/ORM/Service 全链路：**Connection.status 无运行态写入者**（全仓 `status="error"`
  仅 `application/agents.py` 一处，Connection 没有任何运行路径置 error）。
- 冻结：**Connection status 纯 management**（`active / inactive` 自由字符串），
  **不引入** Agent 那种 runtime error 双轴（无误 error 选项、无 error→active
  「启用」清障动作）。
- 11.11 的 `ConnectionUpdate.status` 仅接受 `active / inactive`（schema 级白名单），
  Service 不特判 `error`。若未来真实出现「Channel/Agent 故障需标记 Connection 不可用」
  的运行信号，届时**才**按 Agent 的护栏复制，不在本步预埋。

## 4. 焦点 4 —— immutable binding

- `channel_id + agent_id` 维持 **immutable**（7.1 冻结，DB `UNIQUE(channel_id, agent_id)`）。
- 代码层面双重防线已在：`UpdateConnection` dataclass **无 `channel_id / agent_id` 字段**；
  `ConnectionApplicationService._apply_change` 用 `replace` 只动可变字段
  （name/status/config/credential_ref），绑定不被覆盖；Persistence 的
  `apply_to_orm` 同理不写绑定列。
- **「换 Agent」= 新建 Connection，不是修改**：因 DROP 裁决无硬删除（4.2/6.5），
  切换路径的做法是——把旧 Connection 置 `inactive`（软下线）+ 新建一条指向新
  Agent 的 Connection。UI 编辑表单**不提供**改绑入口。

冻结：绑定不可变；换绑 = 禁改 + 新建（旧 inactive）；`UpdateConnection` 无绑定字段。

## 5. 焦点 5 —— 现有 Connection Backend 是否真的已满足 API/UI

**基本满足，且 11.6 阻塞前置已解除，但有一处口径缺口须由 11.11 补齐：**

| 能力 | 现状 | 11.11 是否要动 |
| --- | --- | --- |
| 创建（存在性预检 + 绑定唯一） | ✅ `create_connection` 三预检完整 | 不动 |
| 读取单条 | ✅ `get_connection` | 不动 |
| 部分更新（绑定不可变） | ✅ `update_connection` | 不动 |
| 绑定去重 | ✅ `find_by_channel_and_agent` + `DuplicateConnectionError` | 不动 |
| 运行时读路径 | ✅ `find_active_by_channel`（Runtime 9.4 最小读，须保留） | **保留，不删** |
| **管理面列举** | ❌ **仅 `find_active_by_channel`，无 `list_connections`** | **11.11 须新增 `list_connections`** |

- **缺口（唯一后端改动）**：Management API 需要「枚举全部 Connection（跨所有
  Channel/Agent、含 inactive）」的读路径，而现有 Port 只有 runtime 向的
  `find_active_by_channel`（按单 Channel + active 过滤，不适用于管理列表）。
  按 Agent/Channel 的演进惯例（11.3 `list_agents`、11.8 `list_channels`），
  **11.11 须在 `ConnectionRepository` Port + Impl + `ConnectionApplicationService`
  补 `list_connections`**（返回全部，与 `list_agents/channels` 同形）。
- **管理列表的名称解析（解耦）**：Connection 列表响应只带 `channel_id / agent_id`
  （绑定 id），**不**在服务层 join Channel/Agent 名称（避免 Service 复制实体读取）。
  UI 经 `/api/channels`、`/api/agents` 自行解析名称（与 §1 下拉同源）。冻结：
  `ConnectionResponse` 不含 channel/agent 名称字段。

**其余 11.6 缺口结论不变**：Connection 仍**无** `connections.py`（API）、
**无** connection schema、**无** frontend connection 文件——这些属于 11.11/11.12
的交付，本审计步不实现。

## 6. 提交清单（本步 = 文档 + 测试，生产代码 0 行）

- `docs/step-11.10-connection-management-vertical-slice-audit.md`（本文件）；
- `tests/test_step11_10_connection_management_vertical_slice_audit.py`：后端完整性
  复审计 + 五焦点冻结决策 + 11.6 前置解除 + 唯一缺口 `list_connections` 冻结为
  「待 11.11 补齐」（届时该缺口测试翻转）。

> 既有守卫（`test_step11_6` 的 `test_b1` 无 connections API / `test_b2` 无 connection
> UI / `test_c1` Channel 已可管理）**保持有效、不翻转**——Connection API/UI 尚未
> 落地，正是 11.11/11.12 的范围。
