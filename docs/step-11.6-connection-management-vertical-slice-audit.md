# Step 11.6 — Connection Management Vertical Slice Audit

> **审计步（生产代码 0 行）。** 清点 Connection 纵向切片各层真实状态，并裁决
> "下一刀到底做 Connection 还是 Channel"。
>
> **核心结论（代码+产品闭环决定，非预设）：**
> **Agent ✅ 完整；Connection 后端 ✅ 已完整但缺 API/UI；Channel ❌ 不可管理
> （无管理 API、无 UI、无生产种子）。Connection 强依赖一个已存在的 Channel，
> 而生产环境当前 Channel 数量为 0 → 真正的下一刀 = Channel Management，
> 不是 Connection。** Connection 后端是"提前建好、只待接线"的，阻塞项是缺失
> 的 Channel 前置。

> **历史状态说明（2026-10-28 复核）**：下文分层清点表中 `frontend/` 的「缺失 / 仅
> Agent UI 四件套」是**该步审计时点**的事实；当前 OSS 发行树整体不含 `frontend/`
> （见 README「Web UI」与「Docker」两节），后端静态挂载亦已改为条件式。
> 下文审计结论按原样保留。

---

## 1. 各层真实状态（逐层清点）

| 层 | 文件 | 状态 | 说明 |
| --- | --- | --- | --- |
| Domain | `domain/connection.py` | ✅ 完整 | frozen dataclass；`UNIQUE(channel_id, agent_id)`；status 默认 active；config 不透明；credential_ref 引用；绑定不可变 |
| ORM | `persistence/models/connection.py` | ✅ 完整 | `UniqueConstraint(channel_id, agent_id)`；`FK→channels.id` + `FK→agents.id`；credential_ref(String,参考)；config(JSON)；审计时间戳 |
| Repository Port | `repositories/connection.py` | ✅ 完整 | `get_by_id / create / update / find_by_channel_and_agent / find_active_by_channel`；**无 delete** |
| Repository Impl | `persistence/repositories/connection.py` | ✅ 完整 | 实现上述 5 方法 |
| Application Service | `application/connections.py` | ✅ 完整 | `create_connection / get_connection / update_connection`；三错误预检 + 5 错误类型；绑定不可变（UpdateConnection 无 channel/agent 字段） |
| **API** | `api/management/` | ❌ **缺失** | 仅有 `agents.py`，**无 `connections.py`**；`schemas/` 无 connection schema |
| **UI** | `frontend/` | ❌ **缺失** | 仅 Agent UI 四件套，无 connection 任何文件 |

**结论：Connection 后端（Domain→Service）已 100% 就绪，缺口仅在 API + UI。**
这与 11.1（Agent 后端就绪、缺 API/UI）是同一形态——但多了一步前置依赖。

## 2. 逐条回答用户八问

### Q1. channel_id + agent_id 唯一约束目前是否已经足够？
**足够（MVP）。** DB 层 `UniqueConstraint("channel_id","agent_id")` + 应用层
`find_by_channel_and_agent` 预检 + `DuplicateConnectionError` 双重保证。
语义：`Channel N ── N Connection N ── 1 Agent`，即"同一 Channel→同一 Agent
只允许一条受控路径"。一个 Agent 可接多个 Channel、一个 Channel 可接多个 Agent
（各自成对唯一），符合领域契约。Domain 注释已标注这是 MVP 假设，真实多路径
需求出现时再放宽。

### Q2. credential_ref 语义和 Agent credential 是否一致？
**意图一致，但 Connection 的"引用不出"尚未被 API 守护。**
- 两者均为 `String` 存**引用**（非明文），均为 nullable、写路径接收、响应不可回显。
- 但 Agent 已在 `schemas/agents.py` 用 `AgentResponse/AgentListResponse`
  **字段面排除** credential_ref，落实"引用不出"；Connection **尚无 schema**，
  所以该护栏目前**未被任何代码强制**（只是 domain 注释层面的承诺）。
- ⚠️ 未来建 Connection API 时，**必须复制 Agent 的"引用入、引用不出"字段面护栏**，
  否则会出现不一致泄漏。Connection.credential_ref 与 Agent.credential_ref 是
  **两张不同的表、两个不同概念**（前者=Access Path 鉴权引用，后者=Agent 运行时
  凭据引用），平行但互不替代。

### Q3. Connection 自己的 config 到底承载什么？
**Channel→Agent 这条 Access Path 本身的配置**（领域注释：`timeout /
protocol_options ...`），是连接级参数，**不是万能配置容器**（不得长成
permissions / capabilities / routing_rules）。`JSON` 列、nullable、管理态写入、
不透明。与 Channel.config（渠道级）、Agent.config（Agent 级）三者分层、互不混淆。

### Q4. status 是否也存在 management/runtime 双轴问题？
**当前不存在（比 Agent 更干净）。**
- Domain 注释明确：Connection.status 是"该 Access Path 是否可用"，沿用
  `active/inactive` 字符串，**不是** `Interaction.processing_status` 那种运行态。
- 全仓 grep 写入 `status="error"` 的**仅 `application/agents.py`**（Agent 的
  双轴是 11.2 刻意设计：error 由 Runtime 专有写、输入侧拒绝）。Connection 无任何
  运行路径把其 status 置 error。
- 建议：**保持 Connection status 纯管理态**，除非未来真实出现"Channel/Agent
  故障需标记 Connection 不可用"的运行信号——届时再按 Agent 的双轴护栏复制，
  但不必现在预埋。

### Q5. 创建 Connection 是否必须验证 Agent / Channel 存在？
**必须，且已在服务层强制。**
`create_connection` 依次：`uow.channels.get_by_id → ChannelNotFoundError`、
`uow.agents.get_by_id → AgentNotFoundError`、`find_by_channel_and_agent →
DuplicateConnectionError`，三者均先于写入。DB 层 FK 为并发兜底。
**这条校验正是"Channel 必须先可管理"的根本原因**——你无法提供
一个不存在/无法创建的 channel_id。

### Q6. 删除是否仍然应该 DROP？
**维持 DROP。** Connection Repository Port 无 `delete` 方法，Service 无删除用例，
与 Agent 的冻结裁决（4.2/6.5）一致：生命周期 = active ⇄ inactive 软下线，
无硬删除。Channel 侧同样（`ChannelRepository` 仅 get/create/update/
find_by_type_and_name，无 delete）。

### Q7. Connection 的 UI 最小字段是什么？
（待建，镜像 Agent UI：List + Form + 停用/启用）
- **List 列**：Name / Channel（名+type）/ Agent（名）/ Status / Actions
  （编辑、停用|启用、刷新）。注意 Connection **自身无 type 字段**（type 在
  Channel/Agent 上），UI 不画 Connection.type。
- **Form 字段**：`channel_id`（受控下拉=已有 Channel 列表）、`agent_id`
  （受控下拉=已有 Agent 列表）、`name`、`config`（不透明 JSON）、
  `credential_ref`（写-only，占位"留空=保留当前引用"）、`status`
  （active/inactive）。
- 关键依赖：**channel_id 下拉的前提是 Channel Management 已可用**——这又把
  刀引回到 Channel。

### Q8. 是否能在不实现 Channel Management 的情况下创建 Connection？
**后端服务层技术可行（若 channel 行已存在），但真实产品闭环不可行。**
- 服务层 `create_connection(channel_id=…)` 只要该 channel_id 在库即成功；
  但**生产环境无任何 Channel 写入路径**：无管理 API、无 UI、无种子
  （`create_all` 仅测试 harness；`main.py` 不挂载 channel router、不播种）。
- 因此 production 启动后 `channels` 表为空 → 任何 Connection 创建都会
  `ChannelNotFoundError`。
- **UI 闭环更不可能**：既没有 Channel UI 去造 Channel，也没有 Connection UI 去绑。

### Q9（用户特别强调）. 当前是否已有真实 Channel 可作第一个 Connection consumer？
**没有。**
- 生产代码：`app.main.create_app` 不建 schema、不播种 Channel；唯一的 Channel
  写入是 `application/channels.py::create_channel`——但它**没有被任何 HTTP 路由
  暴露**（`api/management` 无 channels、`main.py` 未挂载 runtime channel router，
  注释明确"暂不装配生产实例"）。
- 测试中的 `_seed_channel` / `_seed(...)` 均为测试 fixture，不是生产数据。
- ⇒ 第一个真实 Channel 只能来自 **Channel Management 切片**。

## 3. 决策（让代码与闭环决定）

```
Agent       ✅ 完整（Domain→UI，11.2~11.5）
Connection  ✅ 后端完整   ❌ 缺 API/UI   → 且依赖 Channel 存在
Channel     ✅ 后端完整   ❌ 缺 API/UI   ❌ 生产零样本  → 是 Connection 的前置
```

**真正的下一刀 = Channel Management（API + UI），镜像 Agent 的 11.3(API) + 11.5(UI)。**
理由（事实驱动，非拍脑袋）：
1. Connection 后端已就绪，建它只是"补 API+UI"，但补完也跑不起来——没 Channel 可绑；
2. Channel 是唯一既"后端就绪"又"零管理面/零样本"的实体，是整条管理闭环的
   实际瓶颈；
3. 先把 Channel 管理做通，Agent(已有) + Channel(新增) 即可作为 Connection 的
   两个下拉数据源；届时 Connection API+UI 几乎零阻力落地，且复用本审计已确认的
   完整后端。

**建议下一步：Step 11.7 — Channel Management API（镜像 11.3）→ Step 11.8 —
Channel Management UI（镜像 11.5）；之后 11.9 才轮到 Connection Management
（几乎只剩 API+UI，后端免动）。**

## 4. 冻结态守护（防漂移，生产代码 0 行）

新增 `tests/test_step11_6_connection_management_vertical_slice_audit.py`：
- Connection 后端完整性（domain 字段面 / ORM unique+FK / port 5 方法 / service 三用例+5 错误类型 / 无 delete）；
- Connection API/UI 缺口（api/management 仅 agents.py；schemas 无 connections.py；frontend 无 connection 文件）；
- Channel 侧缺口（ChannelApplicationService 存在，但 api/management 无 channels、main.py 无 channel 路由、frontend 无 channel UI）；
- 依赖事实（create_connection 调用 `uow.channels.get_by_id` 且抛 `ChannelNotFoundError`）；
- 删除 DROP（Connection port/service 无 delete）。

> 既有守卫（`test_step6_5` / `test_step6_1` 等断言 `api/management=={__init__,agents.py}`、
> `api/runtime=={__init__,channels.py}`）保持不变——本步不翻转，仅新增冻结态断言。
