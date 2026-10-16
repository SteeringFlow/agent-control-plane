# Step 11.13 — Management Plane Closure Audit

> **审计步，不增功能。** 回答一个问题：Agent / Channel / Connection 三个
> Management Slice 合在一起后，是否形成一个真正闭合、互不污染的 Control Plane
> 管理面？结论：**是**——三实体边界清晰、生命周期共享同一形态但差异是领域事实、
> Management 与 Runtime 之间无依赖污染、Repository 双路径互不替代、前端三页
> 仅导航互链无业务耦合。
>
> 交付：`tests/test_step11_13_management_plane_closure_audit.py`（7 项，锁定真实
> 边界）+ 本审计文档（含 F 维度「守卫是否开始保护历史实现细节」的标记）。

## A. 三对象边界

```
Agent      = 一个可被连接的 Agent（下游 ACP 对象）
Channel    = 一个外部通信入口
Connection = Channel ↔ Agent 的授权 / 访问路径（Access Path）
```

三者职责**不重叠**：Agent/Channel 是独立受管对象，Connection 是它们的受控
*关系*。证据：

- `api/management/{agents,channels,connections}.py` 各自只 import 自身的
  `application.<entity>` + `application.errors` + `schemas.<entity>`，
  不 import 对方的 Service/Schema（测试 A 锁定）。
- `application/connections.py` 只**编排**：存在性预检 + 去重，绝不构造
  Channel/Agent、绝不调用 `uow.channels.create`（11.11 文档明示）。
- 无「Connection 自己解析通道名称 / 代理名称」的越权逻辑——列表只返回绑定 ID，
  名称解析归前端（解耦）。

**结论：无职责重叠。**

## B. 生命周期一致性

共同形态（测试 B 锁定）：每个管理 router 端点面 = `POST / GET / GET{id} / PATCH`
（恰好 4），**无 DELETE**（软下线 = `PATCH status=inactive`）。

| 维度 | Agent | Channel | Connection | 判定 |
| --- | --- | --- | --- | --- |
| Create | ✓ | ✓ | ✓ | 共同 |
| Read | ✓ | ✓ | ✓ | 共同 |
| Update（局部） | ✓ | ✓（config merge） | ✓（config 整段替换） | **同形不同策略** |
| Delete | ✗ | ✗ | ✗ | 共同（无硬删除） |
| `credential_ref` | ✓ 写-only | ✗（凭据住 config） | ✓ 写-only | 故意不同 |
| config 脱敏 | ✗（不透明） | ✓ 哨兵 `********` | ✗（Access Path 参数） | 故意不同 |
| status 轴 | 管理态 + runtime error 双轴 | 纯管理态 | 纯管理态 | 故意不同 |

差异的**领域理由**（应视为领域事实，而非「实现不统一」）：

- **credential_ref 写-only**：Agent 与 Connection 都把凭据引用当「外部 Secret 存储
  的标识」，引用入、引用不出；Channel 没有该字段——凭据材料直接住 `config`
  （token/encoding_aes_key/corpsecret），故对应物是「读时脱敏」而非 credential_ref。
- **Channel 脱敏 vs Connection 不脱敏**：Channel 的 `sanitize_channel_config()`
  因「凭据住 config + 响应脱敏回显」而生；Connection 的 config 是 Access Path
  参数、所见即所存，无「脱敏回显清空密键」问题——故刻意不复制（11.10/11.11）。
- **status 双轴仅 Agent**：Agent 有 runtime `error`（连接/健康信号、运行态），
  Channel/Connection 是纯管理态。Agent 的 `error` 仅展示不可选，恢复经由列表
  「启用」动作（管理覆盖运行态）。Connection 在 schema 层强制 `{active, inactive}`
  白名单，`error` 在 HTTP 合同面就无法进入。

**结论：共同模式统一、故意差异是领域事实，已被显式锁定。**

## C. Management → Runtime 边界

```
Management                Runtime
  ├─ Agent                   ├─ Session
  ├─ Channel                 ├─ Interaction
  └─ Connection              └─ Activity
```

Management **不**承担 Runtime 职责。证据：

- `api/management/*` 整个目录不 import 任何 Runtime 包（扫描 `app.runtime` /
  `app.api.runtime` / `app.adapters` / `app.application.routing|delivery|
  dispatcher|coordinator|session*|interaction*` / `app.persistence` 实现，
  零命中；测试 C 锁定）。
- 35 处 Runtime/Persistence 导入全部落在 `app.persistence.*` 与组合根
  `app.application.__init__`（装配 Runtime），**没有任何**位于管理面 API /
  管理 Service / Schema。
- Management 模块文档自带「本模块不含 Router/Resolver/Dispatcher/Session/
  Interaction/Channel Adapter/Agent Adapter」的禁止清单（见 `application/
  connections.py`、`api/management/*`）。

**结论：Management 未偷偷承担 Runtime 职责。**

## D. Repository / UoW 是否出现「为 Management 污染 Runtime」

`ConnectionRepository` Port 有两条读路径，分属两界、互不替代：

| 方法 | 归属 | 语义 | 使用者 |
| --- | --- | --- | --- |
| `find_active_by_channel` | **runtime** | 单 Channel + active 过滤（9.4 Routing 最小读） | `application/routing.py`、`application/agent_dispatch.py` |
| `list_connections` | **management** | 全量、含 inactive（11.11 陈列读） | `application/connections.py`、`api/management/connections.py` |

污染判据（测试 D 锁定）：管理 Service / API **绝不调用** `find_active_by_channel`
（docstring 可提及，调用带 `(` 则禁）；runtime **绝不调用** `list_connections`。
`application/agent_dispatch.py` 还显式警示「绝不再次 `find_active_by_channel` 选路」。

**结论：双路径保持清晰，UoW 未被 Management 污染。**

## E. 前端是否出现跨页面业务耦合

三页互链（Agent ↔ Channel ↔ Connection）是 HTML `<a href>` 导航，不是 JS 业务
耦合。证据（测试 E 锁定）：

- `app.js` / `channels.js` / `connections.js` 各声明唯一自身的 `const API =
  "/api/X"` 常量，三者互异；
- 脚本之间**不** `import` / `require` / 引用兄弟页面脚本（无共享模块、无跨页函数）；
- Connection 页的 fetch 目标恰好 = `/api/connections`（自身）+ `/api/channels` +
  `/api/agents`（解耦解析名称），不引用 `/api/users`、`/api/identities`；
- Agent 页只引用 `/api/agents`、Channel 页只引用 `/api/channels`；
- 每个 HTML 只加载自身脚本（导航不加载对方脚本）。

因此页面关系停留在「**导航 + API 层面**」，未下沉为「Agent JS → Connection 业务
逻辑 → Channel JS」的耦合。

**结论：三页仅导航/API 互链，无跨页业务耦合。**

## F. 守卫是否开始失控？

当前 **1127 → +7（本审计）= 1134 passed**。审计特意核对了「守卫在保护真实边界，
还是开始保护历史实现细节」：

### F.1 保护真实边界、应保留
- 响应字段面排除（credential_ref 不出、channel_name/agent_name 不出）——合同；
- 绑定不可变（`extra="forbid"`）——合同；
- 无 DELETE / 无 runtime 依赖 / 端点面 4 端点——形态闭合；
- 本审计新增的 A–E 跨切片不变量——高层边界。

### F.2 保护真实边界、但实现方式偏脆（标记，不删、不建议继续堆）
以下守卫**意图正确**，但以「源码字面量 / 计数」方式耦合了实现细节，重构时易误伤：

1. **`test_step11_9::test_c1`** 断言 `const SECRET_PLACEHOLDER = "********";` 字面量
   + `if (value === SECRET_PLACEHOLDER) continue;`。若 UI 抽出共享脱敏 helper 或
   重命名常量即崩。建议改用语义化断言（脱敏 round-trip 行为），而非字面量。
2. **`test_step11_5/11_9/11_12` 的 `test_d1`** 用
   `js.count('for (const value of ["active", "inactive"])') == N` 计数断言。
   一旦抽出共享 `fillStatusSelect` helper，计数从 N 变 1 即崩。建议只断言
   `<option value="error">` 不在 HTML 且 `option.value = "error"` 不在 JS。
3. **`test_step11_12::test_c2`** 用 `js.count('"channel_id"') == 1` 锁定「绑定只在
   创建出现」。重构（重命名/结构调整）即崩。建议改断言「PATCH 分支 payload 构造处
   不含 channel_id/agent_id 子串」——更语义化。
4. **多文件重复断言 frontend 文件集** `== ["index.html","app.js","styles.css",
   "README.md"]`（11.1 / 11.5 / 11.6）。边界含义合理（前端面闭合），但同一断言
   散落多处——一旦合法新增共享资产（如 `common.js`），会同时多处失败。建议收敛为
   单一共享 helper。

> 处理原则（用户 11.13 指令）：**标记而非继续堆守卫**。上述 F.2 项暂不改动，仅在
> 此处记录为「高维护成本守卫」，待下次相关重构时一并迁移为语义化断言即可。

## 收口结论

Management Plane 在 A–E 五个维度上**闭合且互不污染**：

- 三对象边界清晰、无重叠；
- 生命周期共享形态，差异是领域事实；
- Management 与 Runtime 零依赖污染；
- Repository 双路径（runtime 读 / management 读）互不替代；
- 前端三页仅导航/API 互链，无跨页业务耦合。

可以安全地在此管理面上继续做「消费」工作（如 Runtime 可观测性、Network View），
而无需回头修正管理面边界。
