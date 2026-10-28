# Step 11.7 — Channel Management Vertical Slice Audit

> **审计步（生产代码 0 行）。** 交付物 = 本报告 + 审计测试
> （`tests/test_step11_7_channel_management_vertical_slice_audit.py`）。
>
> 目标（与 11.1 Agent / 11.6 Connection 同一形态，但**焦点不同**）：
> 11.6 已裁决「真正的下一刀 = Channel Management」，本步**不重新审计 Connection**，
> 而是逐层清点 Channel 自身的真实能力，并裁决用户给出的三问：
>
> ① **Channel type** —— 是自由字符串还是已有受控集合？已知未来至少有 WeCom，
>    但**不要**因为 Control Plane 理论上支持 WeChat / Feishu / Slack / Discord 就把它们
>    现在全加进枚举；让 **Adapter Registry / 现有代码**决定第一版集合。
> ② **Channel config** —— `Channel.config` 究竟有哪些真实字段？Create 允许输入哪些、
>    Get/List 可以返回哪些？**不要机械照抄 Agent** 的 `credential_ref` 写-only：
>    Channel 的配置结构可能本身就承载 credential-like material。
> ③ **Channel credential 边界** —— `Channel.config` 与 Channel `credential_ref`
>    之间到底是什么关系？**不要在 11.7 随便发明** `channel_secret` / `bot_…` /
>    `credential_ref` 之类的字段。

> **历史状态说明（2026-10-28 复核）**：下文分层清点表中 `frontend/` 的「缺失（占位）/
> 仅有 Agent UI 四件套」是**该步审计时点**的事实；当前 OSS 发行树整体不含 `frontend/`
> （见 README「Web UI」与「Docker」两节），后端静态挂载亦已改为条件式。
> 下文审计结论按原样保留。

---

## 1. 分层现状（逐层清点）

| 层 | 文件 | 状态 | 事实 |
| --- | --- | --- | --- |
| Domain | `domain/channel.py` | ✅ 完整 | frozen dataclass；**5 字段**（name/type/id/status/config）；**无 `credential_ref`**；**无 created_at/updated_at** |
| ORM | `persistence/models/channel.py` | ✅ 完整 | `ChannelModel`；7 列（id/name/type/status/config/json + created_at/updated_at）；config 为 `JSON, nullable`；**无 `credential_ref` 列** |
| Repository Port | `repositories/channel.py` | ✅ 完整 | `get_by_id / create / update / find_by_type_and_name`；**无 list、无 delete** |
| Repository Adapter | `persistence/repositories/channel.py` | ✅ 完整 | `SqlAlchemyChannelRepository` 实现上述 4 方法 |
| Application Service | `application/channels.py` | ✅ 完整 | `create_channel / get_channel / update_channel` + 3 错误类型（ChannelNotFoundError / DuplicateChannelError / InvalidChannelInputError）；`type` 仅 `_require_non_blank` |
| **API Schema** | `schemas/` | ❌ **缺失** | 仅有 `agents.py` / `health.py`；**无 `channels.py`** |
| **HTTP API（管理）** | `api/management/` | ❌ **缺失** | 仅有 `agents.py`；`main.py` 仅挂载 `agents` 与 health，**不挂任何 channel 管理路由**（`api/runtime/channels.py` 是入站 webhook 边界，非管理面） |
| **Frontend** | `frontend/` | ❌ **缺失（占位）** | 仅有 Agent UI 四件套；**无任何 channel 文件** |

**关键发现（与「从零建设」假设相反）**：Channel 的**后端链路**
Domain → ORM → Repository → Application Service **已完整、可运行、带校验与
(type,name) 去重守卫**——这不是要新建，而是要被 API + 前端消费。真正缺失的
是 **Schema / HTTP API / Frontend 三层**，以及服务/端口层面的 **List** 用例。
这与 11.1（Agent）、11.6（Connection）是同一形态。

但 Channel **与 Agent 有一个本质差异**，正是本审计三问的由来：
**Channel 没有 `credential_ref` 字段，它的凭据直接住在 `Channel.config` 里**
（见 §2.3）。所以 Agent 那套「`credential_ref` 写-only、引用入引用不出」的
成熟护栏**不能 1:1 照抄**——Channel 需要基于 `config` 的**密钥级脱敏 + 合并更新**。

---

## 2. 三问逐项裁决

### 2.1 ① Channel type：自由字符串还是受控集合？

**现状：`type` 是自由字符串（无受控集合）。**

- `Domain/channel.py`：`type: str`；
- `ORM/channel.py`：`type: Mapped[str] = mapped_column(String(64), nullable=False)`；
- `Application/channels.py`：`_require_non_blank(input.type, "type")`——**仅非空校验**，
  **无任何集合/枚举约束**，不 import `enum`、没有任何 `type in {...}` 成员判定。

**受控集合的真实来源（让现有代码决定，而非拍脑袋枚举）：**

- `app/adapters/channels/` 目录下**唯一真实 Adapter 是 `wecom.py`**（`__init__.py`
  只是空壳说明，无 `feishu.py` / `dingtalk.py` / `webhook.py`）。
- **全仓没有任何 adapter registry**：没有 `CHANNEL_ADAPTERS: dict[type, Adapter]`
  这样的注册表，也没有「按 `channel.type` 派发 Adapter」的代码。
- 入站路由 `api/runtime/channels.py` 直接硬编码 `/{channel_id}/wecom/webhook`
  并显式调用 `WeComAdapter`——**它根本不读 `channel.type` 来派发**。

**裁决：**

1. **现在不枚举受控集合**。`wecom / feishu / dingtalk / webhook / wechat / slack /
   discord ...` 这些不是「已知实现」，只是「Control Plane 理论上可能支持」。在
   对应 Adapter 落地前把它们写进枚举，正是 10.9「不为未来提前建设」所冻结的
   反模式。
2. **第一版「受控集合」= 当前真实存在的 Adapter = `{wecom}` 唯一一项**。所谓
   「让 Adapter Registry / 现有代码决定集合」，落到现在就是：只有 `wecom`
   有可服务的 Adapter；其它字符串即使写入也只是「无 Adapter 接管的自由标记」。
3. **收敛时机**：当未来真的出现 `CHANNEL_ADAPTERS` 这类 registry 时，管理 API 的
   `type` 校验**应从该 registry 取允许集合**（单一事实来源），而不是在 domain /
   service 里硬编码一个新枚举——届时「自由字符串 → 受控集合」的闭环才自然闭合。
   在此之前，domain / ORM 保持 `str`，校验留给下游 API。
4. `UpdateChannel.type` 当前允许改动（测试 `test_update_channel_type_allowed`
   明示「管理期允许改 type，不触发 Adapter」）——保持，因为它是分类字段而非
   Adapter 派发键。

### 2.2 ② Channel config：真实字段 + Create/Get/List 暴露策略

**`Channel.config` 不是空壳不透明容器——它有已知的、adapter 专属的真实字段，
且当前就承载 credential-like material。** 真实字段的**唯一事实来源** =
`app/adapters/channels/wecom.py`（也是唯一真实 Adapter）：

| 来源 | 键 | 性质 |
| --- | --- | --- |
| 入站验签材料（`_material`） | `token`、`encoding_aes_key`、`corp_id` | `token` / `encoding_aes_key` 是**共享密钥/密钥材料**（secret）；`corp_id` 是租户标识（非密） |
| 出站凭据 + 连接（`_outbound_material`） | `corpsecret`、`agentid`(int)、`api_base`(可选)、`timeout_s`(可选) | **`corpsecret` 是应用密钥（secret）**；其余为非密连接参数 |

**敏感度分类（决定 Get/List 能否返回）：**

- **密（Get/List 必须脱敏，不得原样回显）**：`corpsecret`、`token`、`encoding_aes_key`；
- **非密（Get/List 可正常返回，供管理可见）**：`corp_id`、`agentid`、`api_base`、`timeout_s`。

**裁决：Create / Get / List 的暴露策略**

- **Create（输入）**：管理 API **接受管理员贴入的完整 `config` 字典**，MVP 不做
  adapter 专属键级校验；缺失/非法键由 **Adapter 在运行时**抛
  `InboundChannelConfigError` / `ChannelDeliveryFailedError`（Fail Closed）兜底。
  管理面只保证「dict 落库」。
- **Get / List（输出）**：**必须对 `config` 做密钥级脱敏**——返回 `config` 但把
  `corpsecret` / `token` / `encoding_aes_key` 替换为占位符（如 `"***REDACTED***"`），
  同时保留 `corp_id` / `agentid` / `api_base` / `timeout_s` 供管理员可见。
  这是 **Channel 专属版**的 Agent「`credential_ref` 写-only」——但作用在
  **config 键级**，因为 Channel 没有独立的 `credential_ref` 列。
  **不要机械照抄 Agent**：不能简单「整个 `credential_ref` 字段不出现在响应」，
  而要对 config 内具体密键脱敏（否则管理员连 `agentid` 都看不到）。
- **Update（配置更新语义，关键缺口）**：当前 `UpdateChannel.config` 在 `_apply_change`
  里是 `replace(current, config=change.config)`——**整段替换**。若响应已脱敏、管理员
  只 PATCH `{agentid: ...}`，密键会被**整段清空**，下一次出站 `corpsecret` 缺失即
  Fail Closed。⇒ 下游建管理 API 时，`config` 更新**必须是 merge 语义**（提供的键
  覆写、未提供的键——尤其密键——保留），**不能整段替换**。这是 Channel 专属、Agent
  不需要的要求（Agent 凭据是独立字段，不混在 config 内）。

**附：审计时间戳回显（次要）**

- ORM `ChannelModel` 有 `created_at` / `updated_at`，但 `Domain/channel.py` **未声明**
  这两个字段（与 Agent 不同——Agent 11.3 已把二者纳入领域回显）。为管理 UI 可见性
  对称，建议下游响应体补回这两个只读审计时间戳（镜像 Agent 11.3 先例），domain 层
  是否补由实现侧决定。

### 2.3 ③ Channel credential 边界：Channel.config ↔ Channel credential_ref

**核心发现：Channel 没有 `credential_ref` 字段（domain 与 ORM 均无）。**
全仓只有 `Agent`（`domain/agent.py`）与 `Connection`（`domain/connection.py`）
有 `credential_ref`；Channel 二者皆无。

- 关系实质：**`Channel.config` 是 WeCom 入站 + 出站凭据的「唯一」承载结构**，
  Channel 没有兄弟字段 `credential_ref`。
- 既有裁决（10.2.1 / 10.2.2 模块 docstring 显式记录）：
  > 「`corpsecret` **不属于 Runtime Contract，也不修改 Channel domain schema**——
  > MVP 暂存于 `Channel.config`，credential_ref 化留待生产化决策」。
- 即：凭据在 MVP 期**就住 config**；「抽成 `credential_ref` 指向外部 Secret
  存储」是一个**已被显式推迟的、未来的生产化决策**，不是「11.7 应当现在发明」的。

**裁决（冻结边界，防漂移）：**

1. **11.7 不得发明 `channel_secret` / `bot_token` / `credential_ref` 之类的
   Channel 字段或列**。不要在审计步给 Channel 增加任何 credential 载体——
   这会和 10.2.x 的「暂存 config、未来再 credential_ref 化」裁决冲突，且会
   与 Agent / Connection 既有的 `credential_ref` 语义产生不必要的三方分叉。
2. Channel 的「凭据」= `Channel.config` 中那几个密键（§2.2）；保护手段是
   **读时脱敏 + 更新时 merge 保留**（§2.2），而不是新增一个写-only 引用字段。
3. 未来若真要「`credential_ref` 化」（抽密键到外部 Secret 存储），应作为**独立的
   生产化切片**单列，与 Agent / Connection 平行处理，而非在本审计顺手加字段。

**三实体凭据模型对照（成文，防未来混淆）：**

```
Agent      : credential_ref  (独立引用字段) → 写-only、引用入引用不出 ✅ 已落地
Connection : credential_ref  (独立引用字段) → 与 Agent 平行、待 API 时复制护栏
Channel    : 无 credential_ref；凭据嵌入 Channel.config 的密键
             → 读时按 key 脱敏(corpsecret/token/encoding_aes_key)
             → 更新时 merge 保留密键（不得整段替换）
             → credential_ref 化是未来的独立生产化切片，11.7 不发明
```

---

## 3. 闭环就绪度结论

```
Channel Management Vertical Slice
   │
   ├── 后端（Domain→ORM→Repository→App Service）：✅ 已就绪
   │     └── 仅缺 List 用例（管理闭环必要；Delete 维持 DROP）
   │
   ├── API Schema / HTTP API（管理）：❌ 缺失（api/management 仅 agents.py）
   ├── Frontend：❌ 缺失（占位）
   │
   └── 三问裁决（待下游 API 切片一并收口，11.7 不写生产代码）：
        ├── ① type：保持自由字符串；受控集合由未来 Adapter Registry 决定，
        │         第一版真实集合 = {wecom}；现在不枚举未来平台
        ├── ② config：密键(corpsecret/token/encoding_aes_key) 读时脱敏、
        │          非密键正常返回；Update 必须 merge 保留密键（不得整段替换）
        └── ③ credential 边界：Channel 无 credential_ref；凭据住 config；
                  11.7 不发明任何 credential 字段；credential_ref 化是未来切片
```

**最小 API 切片建议（下游，不以 11.7 自称 NOW）**：
`api/management/channels.py` 建 Pydantic 请求/响应 Schema + 路由
（POST /api/channels、GET /api/channels/{id}、GET /api/channels 列表、
PATCH /api/channels/{id}）；后端补 `list_channels`；响应体对 `config` 做密钥级
脱敏、对未提供的 config 键做 merge 保留；`type` 校验在 API 层对齐真实 Adapter
（当前 `{wecom}`）；镜像 Agent 的 11.3(API) + 11.5(UI)。Connection Management
（11.6 已确认后端完整）可在此之后几乎零阻力落地，复用本审计确认完整的 Channel 后端。

> **步骤编号说明**：11.6 曾建议「11.7=Channel API、11.8=Channel UI、11.9=Connection」。
> 现 11.7 改为 Channel **审计**（本步），故下游建议顺延：
> **11.8 = Channel Management API → 11.9 = Channel Management UI → 11.10 = Connection Management**。

---

## 4. 验收（→ 测试）

| # | 条件 | 测试 |
| --- | --- | --- |
| 1 | Channel Domain 字段面 == {name,type,id,status,config}；**无 credential_ref** | A1 |
| 2 | Channel ORM 列面含 config(JSON) + created_at/updated_at；**无 credential_ref 列** | A2 |
| 3 | Repository Port 方法面 == {get_by_id,create,update,find_by_type_and_name}；无 list/delete | A3 |
| 4 | Application Service 提供 create/get/update + 3 错误类型；`type` 仅非空校验 | A4 |
| 5 | `api/management/` 仅 `agents.py`；`schemas/` 无 `channels.py` | B1 |
| 6 | frontend 仅 Agent UI 四件套，无 channel 文件 | B2 |
| 7 | `type` 是自由字符串（domain `str`、service 仅 non-blank、无 enum/成员判定） | C1 |
| 8 | 唯一真实 Adapter = `wecom.py`（无 registry、`feishu/dingtalk/webhook` 均未实现） | C2 |
| 9 | 入站路由硬编码 wecom、不按 `channel.type` 派发（印证 type 非派发键） | C3 |
| 10 | `config` 真实密键来源（wecom.py 读取）：入站 token/encoding_aes_key/corp_id；出站 corpsecret/agentid/api_base/timeout_s | D1 |
| 11 | Channel **无** `credential_ref`（domain + ORM 均无；对照 Agent/Connection 有） | D2 |
| 12 | `config` 更新当前为整段替换（缺 merge → 密键会被清空，下游 API 须修正） | D3 |
| 13 | 闭环就绪度结论 + 三问裁决 + 下游 API 切片建议成文（防漂移） | E1 |
