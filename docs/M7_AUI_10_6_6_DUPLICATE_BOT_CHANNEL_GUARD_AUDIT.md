# M7-AUI-10.6.6 — Duplicate Bot Channel Guard Audit（重复 WeCom Bot 渠道防护只读审计）

> 状态：**只读审计完成，防护未实现**（先审计后实现）。
> 范围：仅 `wecom_bot` 渠道形态；不抽象为通用 Identity Framework。
> 纪律：零运行时代码改动、零数据库/业务数据改动、零凭据读取或输出。
> 配套：`M7_AUI_10_6_6_REAL_WECOM_E2E_SUCCESS_BASELINE.md`（E2E 成功基线，§5.2 记录了本审计确认的机制级根因）。
> 交付：本文档 = 代码证据（B1~B4）+ 当前行为 vs 预期行为差距 + 下一轮最小实现方案（§6，本轮不实现）。

---

## 0. 审计方法

全部结论基于当前工作区真实源码（文件 + 行号引用），未运行任何真实 Verify /
Bootstrap Arm，未修改 Channel 状态，未清理任何 Interaction / Activity / Session /
Grant / Bootstrap Arm 数据。

---

## B1. 创建与更新入口

### B1.0 证据面

| 层 | 文件 | 关键证据 |
|---|---|---|
| API | `backend/app/api/management/channels.py` | `POST /api/channels`（L89-98）、`PATCH /api/channels/{id}`（L125-140）；错误映射：Duplicate→409 / NotFound→404 / Invalid→422（L77-85） |
| Application | `backend/app/application/channels.py` | `ALLOWED_CHANNEL_TYPES`（L44，含 `wecom_bot`）；create 去重守卫仅 `(type, name)`（L115-116）；update 仅在 name/type 变化时查撞（L156-165）；status 仅非空校验、**无白名单**（L148）；config 为 merge 语义（L171-190） |
| Repository Port | `backend/app/repositories/channel.py` | 唯一查重方法 `find_by_type_and_name`（L46-52） |
| Persistence | `backend/app/persistence/repositories/channel.py` | `find_by_type_and_name` 按 tenant 过滤（L73-80） |
| ORM / Schema | `backend/app/persistence/models/channel.py`（L15-32）、`backend/alembic/versions/0001_baseline_core_tables.py`（L43-53） | `channels` 表**只有主键约束，无任何唯一约束**（对照：identities L95 有 `uq_identities_channel_external_id`、interactions L150 有 `uq_interactions_channel_external_id`） |
| 错误契约 | `backend/app/application/errors.py`（L73-88） | `DuplicateChannelError` docstring 自认：`channels 表没有冻结唯一约束，本守卫存在 TOCTOU 窗口` |
| 前端 | `frontend/channels.js` | 创建 `POST`（L272-276）与编辑 `PATCH`（L277-285）只提交 name/type/status/config；**无任何 bot_id 感知**；409 提示写死为「已经存在同名渠道，请换一个名称」（L87）；Secret 哨兵（L26-14、L235-255） |

### B1.1 问题回答

1. **同一 Tenant 下，创建相同 bot_id 的第二条 active `wecom_bot` Channel 会被接受吗？**
   **会。** create 的唯一守卫是 `uow.channels.find_by_type_and_name(input.type, input.name)`
   （`channels.py` L115-116）。bot_id 住 `config["bot_id"]`，**不在任何查重路径上**。
   只要 name 不同（例如「企业微信 11」vs「企业微信 12」），同 bot_id 的第二条
   active 渠道直接创建成功（201）。这正是 10.6.5 事故中 Channel 9/10/11/15 并存的成因。

2. **编辑已有 Channel 的 bot_id 时，是否可能制造重复？**
   **可能。** PATCH 的 config 是 merge 语义（`_apply_change`，L171-190）：提交
   `{"bot_id": "<别的渠道的 bot_id>"}` 会直接覆写，且 update 只检查 name/type 撞车
   （L156-165），**对 config.bot_id 零检查**。即：把 A 渠道的 bot_id 改成 B 渠道的
   bot_id，两条 active 渠道即指向同一外部 Bot。

3. **将 inactive Channel 重新启用时，是否可能与现有 active Channel 冲突？**
   **可能。** PATCH `{"status": "active"}` 走 update_channel；name/type 未变时
   **完全跳过查重分支**（L158-160 两个 `changed` 条件均为 False）。停用的旧渠道
   （如事故中的 Channel 9/10/11）被重新启用后，会与现有 active 渠道（Channel 15）
   构成同 bot_id 双 active——且订阅调和会在下一个 30s 周期把它重新 ADD 成绑定
   （`reconcile_interval_s = 30.0`，`wecom_bot.py` L237）。**重新启用路径当前没有
   任何冲突检查。**

4. **并发创建是否可能绕过现有应用层检查？**
   **可能。** 守卫是「先 find、后 create」的应用级软约束，无 DB 唯一约束兜底；
   `DuplicateChannelError` docstring（`errors.py` L80-82）已明示 TOCTOU 窗口。
   两个并发 POST（同 name）或（守卫落地后的）同 bot_id 都可双双通过 find 阶段。
   现网为单实例管理面、并发量极低，属**低概率但真实存在**的窗口。

5. **现有唯一性约束究竟是名称唯一、数据库唯一，还是外部身份唯一？**
   三者皆非：
   - **名称唯一**：应用级、tenant 限定、平台（type）限定——`(tenant_id, type, name)`
     软约束，且**没有**数据库约束；
   - **数据库唯一**：`channels` 表无任何唯一约束（仅 PK）；
   - **外部身份（bot_id）唯一**：完全不存在，创建/编辑/重启用三条路径都不检查。

---

## B2. 外部身份定义与作用域（仅 wecom_bot）

### B2.0 证据面

| 事实 | 证据 |
|---|---|
| `bot_id` 是该 Provider 的稳定外部 Bot 身份标识 | `wecom_bot.py` L200-203（`CONFIG_BOT_ID = "bot_id"`，注释：本模块是这些键的唯一事实源）；L289-299（订阅帧 `body.bot_id` 向 WeCom 声明身份）；L52-54 模块 docstring（帧内 `aibotid` =「哪个 bot 收到的」= Channel 定位键） |
| 入站帧归属校验依赖 bot_id | `wecom_bot.py` L632-643（帧 `aibotid` ≠ 渠道 `config.bot_id` → 拒收 `InboundFormatError`） |
| 连接材料 fail-closed | `wecom_bot.py` L243-269（`connection_spec_from_channel`：缺 `bot_id`/`bot_secret` → `InboundChannelConfigError`，不猜不回退） |
| 运行时按**渠道**枚举（跨租户、按 channel_id） | `backend/app/main.py` L423-424（`bootstrap_uow_factory = partial(SqlAlchemyUnitOfWork, tenant_id=None)`）、L490-508（`discover_bot_connections`：列出**全部租户**的 active `wecom_bot` 渠道，逐条转 spec，无 bot_id 去重） |

### B2.1 判定

1. **bot_id 是否为该 Provider 的稳定外部 Bot 身份标识？**
   **是。** 它是订阅认证（`aibot_subscribe`）的主体、入站帧 `aibotid` 的比对基准、
   也是「同一外部机器人」的唯一自然键。同一 bot_id（无论写在几条 Channel 里）
   在 WeCom 侧就是同一个机器人。

2. **同一 Tenant 下同一 bot_id 是否应该只有一个 active Channel？**
   **应该（语义必然）。** 理由是运行时结构性事实：每条 active `wecom_bot` 渠道
   都会被 `discover_bot_connections` 枚举为一条 spec，`reconcile()` 按 channel_id
   建绑定并各自发起 `aibot_subscribe`。同一 bot_id 两条 active 渠道 ⇒ 两条
   WS 订阅 ⇒ 同一条入站消息经两条连接各进一次 intake（interaction 幂等键是
   `(channel_id, external_interaction_id)`，**跨 channel 不合并**，`wecom_bot.py`
   模块 docstring「重复帧由既有 intake 去重…兜底」+ L818-819）。10.6.5 事故即实证。
   > 注：这是由运行时结构推出的必然性；把「一 bot 一 active 渠道」升格为**冻结的
   > 业务规则**属下一轮契约冻结动作（§6），本文档标注为**应然、未成文、未强制**。

3. **不同 Tenant 是否允许配置同一个外部 bot_id？当前是否有明确的隔离/共享规则？**
   **无明确规则 —— 待决策。** 代码事实：`discover_bot_connections` 跨租户枚举
   （tenant_id=None），所以 A、B 两租户配置同一 bot_id 同样会产生**两条对同一外部
   机器人的 WS 订阅**（订阅是物理连接资源，不按租户隔离；`main.py` L117-118 注释
   亦明确「连接是进程级物理资源（不属于租户）」）。语义上同一 bot_id 只属于一个
   企业微信租户，两租户配同一 bot_id 至少有一方是错误配置；但「同 bot_id 跨租户
   是否允许共存（例如平台代管场景）」是业务决策，**不在本轮拍板**。下一轮最小实现
   建议（§6）按「同 bot_id 全局唯一 active」处理（跨租户同 bot_id 视为冲突并给出
   不泄漏他租户信息的安全报错），若未来需要共享语义再显式解冻。

4. **不同 bot_id 是否应允许分别创建 Channel？**
   **允许，且已被测试锁定。** `backend/tests/test_m6_6_f2_wecom_bot_hot_discovery.py`
   HD1（bot-a / bot-b 两条渠道各自独立订阅，互不影响）。本防护**不得**影响该语义。

5. **Channel inactive 后重新启用时应执行什么冲突检查？**
   应执行与创建/编辑同一条 bot_id 冲突检查（见 §6 路径 3）。当前现状：零检查
   （B1.1-3）。

---

## B3. 运行时订阅关系

### B3.0 证据面

| 环节 | 证据 |
|---|---|
| 发现源 | `main.py` L490-508 `discover_bot_connections`：`uow.channels.list_channels()`（跨租户全量）→ 过滤 `type==wecom_bot && status=="active"` → 逐条 `connection_spec_from_channel`。**按渠道逐条产出，无 bot_id 维度的去重/分组** |
| 绑定身份 | `wecom_bot.py` L760-780 `_Binding`（**身份 = channel_id**）、L840 `self._bindings: dict[int, _Binding]` |
| ADD / KEEP / REMOVE | `wecom_bot.py` L883-940 `reconcile()`：对 channel_id 做 ADD（新 channel_id → 起 rx+exec 线程并订阅）/ KEEP（同 channel_id → 线程不动，spec 变化才置 reconnect）/ REMOVE（消失的 channel_id → 只停该绑定） |
| 同轮去重 | `wecom_bot.py` L910-917：`desired` dict 对**同一 channel_id** 重复出现时告警并忽略（`duplicate wecom_bot binding ignored`）——但两条**不同 channel_id** 的重复 bot_id 各自进入 `desired`，**不会被折叠** |
| 活连接注册表 | `wecom_bot.py` L490-541 `WeComBotConnectionRegistry`：`_senders: dict[int, ...]` 按 channel_id 建键；同 channel 重连时后注册者覆盖前注册者 |
| 入站去重兜底 | `wecom_bot.py` 模块 docstring + L818-819：重复帧由既有 intake 按 `(channel_id, external_interaction_id)` 状态感知接纳去重——**键含 channel_id，跨渠道重复投递不在其覆盖范围内** |

### B3.1 回答

1. **是否按 Channel ID 而非 bot_id 去重？**
   **是，全程按 channel_id。** 绑定身份（L762、L840）、调和三动作（L890-895）、
   同轮折叠（L910-917）、活连接注册表（L511）全部以 channel_id 为键；bot_id 在
   运行时只出现在订阅帧与归属校验里，不参与任何唯一性判断。

2. **创建重复 Channel 后为什么会产生多个 WS 订阅？**
   因果链：两条不同 channel_id、同 bot_id 的 active 渠道 → `discover_bot_connections`
   产出 2 条 spec → `reconcile()` 的 `desired` 各自成立（channel_id 不同，L910-917
   的折叠不触发）→ 2 条 `_Binding` 各起 rx+exec 线程 → 2 次 `aibot_subscribe`
   （同一 `bot_id`+`secret`）→ 同一外部机器人回推的消息在两条连接上各出现一次 →
   `_on_bot_frame` 经各自 `binding.channel_id` 进入 intake → 幂等键
   `(channel_id, external_interaction_id)` 不同 → 产生两条独立 Interaction
   （这就是 10.6.5 事故中历史失败/重复交互的机制级根因）。

3. **是否存在可复用的订阅唯一性保护？**
   存在两处**形态可复用**（但当前都不在 bot_id 维度上工作）：
   - `reconcile()` 内 `desired: dict[int, BotConnectionSpec]` 的同轮折叠模式
     （L910-917）——把键从 channel_id 换成「bot_id → 唯一 spec」即得订阅级唯一性
     （运行时兜底，治标）；
   - `DuplicateChannelError` 的应用级守卫 + 409 映射先例（`errors.py` L73-88、
     `api/management/channels.py` L77-85）——下一轮创建/编辑/重启用防护应完全
     复用该先例（治本入口，见 §6）。
   本轮不修改 Transport、不改变订阅生命周期与重连语义（指令红线）。

---

## B4. 历史重复数据（只读）

### B4.1 现有代码如何处理历史重复

- **运行时不做任何自动收敛**：`reconcile()` 对「同 bot_id 多 channel」不做停用/
  合并/告警升级（只有同 channel_id 重复的 DEBUG 级折叠，L910-917）；
- **应用层不做任何检测**：list / get / patch 均不聚合 bot_id（B1.0）；
- **唯一的人工恢复手段**是既有 `PATCH /api/channels/{id}` 软下线
  （`status → inactive`）——10.6.5 事故即以此收敛（Channel 9/10/11 停用），
  历史失败 Interaction / Activity / Session / Grant / Bootstrap Arm 全部保留原样。

### B4.2 未来的冲突检测与恢复建议（仅建议，本轮不执行）

1. **只读检测报告**：新增一次性只读审计脚本（或管理面诊断端点），按
   `config.bot_id` 对 `type=wecom_bot && status=active` 的渠道分组，报告
   「同 bot_id 多 active 渠道」的组及各渠道 id/name/tenant——不修改任何数据；
2. **恢复纪律**：检测出的重复组由**人工**按既有 PATCH 软下线多余渠道（与 10.6.5
   同一手段），绝不自动停用/删除/合并 Channel 或 Connection，绝不清理历史
   Interaction 数据；
3. **不迁移历史数据**：历史失败 Interaction 保留原判定（E2E 基线文档 §5.3 纪律）；
4. 若下一轮引入数据库唯一约束，必须先出具「历史重复清单 + 人工收敛确认」，
   再建部分唯一索引（见 §6.2 的兼容方案）。

---

## 5. 当前行为 vs 预期行为差距（Gap Matrix）

| # | 场景 | 当前行为 | 预期行为 | 差距 |
|---|---|---|---|---|
| G1 | 创建同 bot_id 第二条 active `wecom_bot` | 201 创建成功 | 409 拒绝，提示复用既有渠道 | **防护缺失** |
| G2 | PATCH 修改 bot_id 指向他渠道 bot_id | 200 成功 | 409 拒绝（self 除外） | **防护缺失** |
| G3 | inactive 渠道重新启用为 active | 200 成功（可能与他渠道同 bot_id 双 active） | 409 拒绝（self 除外） | **防护缺失** |
| G4 | 并发创建同 bot_id | 应用级 find→create 有 TOCTOU 窗口 | 硬保证或明确接受残余风险 | **无 DB 约束**（守卫落地后仍存在） |
| G5 | 运行时同 bot_id 多订阅 | 各 channel 独立订阅，消息跨渠道重复进入 intake | 订阅级唯一（一 bot 一连接） | **运行时无 bot_id 去重** |
| G6 | 跨租户同 bot_id | 无规则，会双订阅 | 待决策（建议按冲突处理 + 安全报错） | **待决策** |
| G7 | 状态字段白名单 | `status` 仅非空校验，可写入任意字符串 | 至少限定 `active`/`inactive`（UI 已限定，后端未限定） | 顺带记录，**不属本防护范围**（不顺手修复） |

> G7 仅登记，不在本轮修复（「不顺手修复无关问题」纪律）。

---

## 6. 下一轮最小实现方案（**已由 M7-AUI-10.6.7 实施**）

> 实施状态（10.6.7 更新）：§6.1/§6.3/§6.4/§6.5/§6.6/§6.7 已落地——
> `DuplicateBotChannelError`（`app/application/errors.py`）+ 集中式守卫
> `_ensure_no_active_bot_identity_conflict`（`app/application/channels.py`，
> 创建/更新/重启用三入口共用）+ API 结构化 409 detail
> `{code: "duplicate_bot_channel", message}`（`app/api/management/channels.py`）
> + 前端 `err.code` 分支提示（`frontend/shared/http.js` + `frontend/channels.js`）。
> 验收：`backend/tests/test_m7_aui_10_6_7_duplicate_bot_channel_guard.py`（18 项）。
> **§6.2 数据库唯一索引仍为 deferred（本轮未加迁移）——TOCTOU 残余风险继续存在，
> 未宣称数据库级并发唯一性保证；§6.8 D1 跨租户策略保持待决策。**

原则：复用既有 Channel 管理 / 错误处理模式（`DuplicateChannelError` 先例、409 映射、
tenant-scoped UoW），**不新建**通用身份抽象、管理页面、无关状态机；不修改
`WeComBotTransport` 及其订阅生命周期。

### 6.1 在哪一层实施

**ChannelApplicationService（业务规则唯一裁决点）**，与现有 `(type, name)` 守卫
同层同形：

- 新增一个**领域内聚**的辅助读取（不新建 Repository 通用方法族）：在 application
  层经既有 `uow.channels.list_channels()`（tenant-scoped，量小）过滤
  `type == "wecom_bot" and status == "active"`，提取 `config["bot_id"]` 做比对。
  不在 Repository 层做 JSON 表达式查询（SQLite/PG 方言差异，属过度建设）；
- 判定函数建议为纯函数（输入渠道列表 + 目标 bot_id + self_id → 冲突渠道），
  便于单测，不触碰 DB。

### 6.2 是否需要数据库唯一约束

**最小轮：不加。** 理由：
- `config` 是 JSON 列，bot_id 表达式唯一索引在 SQLite 不可行、PG 需表达式索引
  + 历史重复数据先收敛，迁移面大；
- 现有 `(type, name)` 守卫同样是应用级 + 已知 TOCTOU（`errors.py` L80-82 先例），
  防护语义保持一致，风险等级（单实例管理面、低频人工操作）可接受；
- **后续可选加固**（单列一条 deferred decision）：待历史重复数据人工收敛完成后，
  在 PG 上建部分唯一表达式索引
  `UNIQUE ((config->>'bot_id')) WHERE type='wecom_bot' AND status='active'`，
  并同步 `DuplicateChannelError` 式错误兜底（DB IntegrityError → 409）。届时需要
  独立 migration + 回填审计，**不在最小轮**。

### 6.3 三条路径的处理

| 路径 | 处理 |
|---|---|
| 创建（POST） | 提取 `config.bot_id`（存在且非空时）→ 冲突检测 → 命中抛新错误 `DuplicateBotChannelError`（或复用 `DuplicateChannelError` 扩展字段），映射 409 |
| 编辑 bot_id（PATCH config） | merge 后的有效 bot_id 与原值不同时 → 同一检测（排除 self）→ 409 |
| 重新启用（PATCH status→active） | 目标为 `wecom_bot` 且将变为 active 时 → 同一检测（排除 self）→ 409；type 不是 `wecom_bot` 的渠道不受影响 |

### 6.4 并发防护

- 最小轮接受应用级 TOCTOU 残余风险（与 `(type, name)` 守卫同级，单实例管理面
  残余风险极低），在错误契约 docstring 中如实声明（对齐 `errors.py` L80-82 的
  诚实先例）；
- 若未来要求硬保证 → 走 §6.2 的 deferred DB 部分唯一索引，不在最小轮实现。

### 6.5 API 冲突返回

- 状态码：**409 Conflict**（复用 `_map_application_error` 既有映射点）；
- 安全错误信息：`已存在使用同一企业微信机器人（bot_id）的启用渠道：{name}（#{id}），请复用该渠道或先停用它。`
  —— 只引用**本租户内**冲突渠道的 name/id（tenant-scoped 读取天然不跨租户），
  绝不回显任何 config / 凭据；跨租户同 bot_id（若按 §6.8 决策纳入）报错文案
  不得包含他租户任何信息，统一为「该企业微信机器人已被其他渠道占用」。

### 6.6 前端提示

`frontend/channels.js` 的 `userMessage()`（L81-91）为 409 增加 bot 维度文案分支：
后端 409 detail 透传时优先显示后端文案（含冲突渠道名），fallback 文案由
「已经存在同名渠道，请换一个名称」扩展为兼容两种冲突的提示
（如「已存在同名或使用同一机器人的启用渠道，请复用已有渠道或先停用它」）。
表单不做 bot_id 预检（保持 UI 与 config 不透明语义一致，提交后由 409 驱动提示）。

### 6.7 测试覆盖（下一轮验收清单）

1. 创建：同 bot_id 第二条 active → 409；不同 bot_id → 201（回归 HD1 语义）；
   非 `wecom_bot` 类型不受影响（`wecom`/`local` 同名同 bot_id 均不管）；
2. 编辑：PATCH bot_id 指向他渠道 → 409；改回自身原值（无变化）→ 200；
   自身渠道改名不受影响；
3. 重新启用：inactive 渠道启用撞 active 同 bot_id → 409；无冲突 → 200；
4. 并发：登记 TOCTOU 残余风险（docstring 断言级），如实现 DB 约束再补
   IntegrityError→409 兜底测试；
5. 运行时回归：`test_m6_6_f2_wecom_bot_hot_discovery.py` 全量保持绿
   （ADD/KEEP/REMOVE 语义零变化）；
6. 安全：409 响应体不含任何 config / 凭据字段（复用脱敏响应模型已保证，补断言）。

### 6.8 待决策项（实现前须拍板）

- **D1**：同 bot_id 冲突的作用域——建议「全局（跨租户）唯一 active」；
  若业务确认允许跨租户共享同一外部 Bot（平台代管），需显式解冻并重写 §6.5 文案
  与检测作用域（tenant 内 / 全局）；
- **D2**：错误对象——新建 `DuplicateBotChannelError`（语义精确）vs 扩展
  `DuplicateChannelError`（改动最小）；倾向前者（复用同一 409 映射分支）；
- **D3**：是否需要数据迁移——最小轮**不需要**（零 schema 变更）；DB 约束为
  deferred（§6.2），届时需 migration + 历史重复收敛报告先行。

---

## 7. 本轮合规声明

- 未修改任何运行时代码（`git status` 仅新增本文档与 E2E 基线文档）；
- 未修改数据库、配置、业务对象、Channel 状态、运行时订阅；
- 未重新运行真实 Verify / Bootstrap Arm；未清空任何数据；
- 未读取或输出任何 `bot_id` 之外的凭据材料（`bot_id` 属管理面可见的非敏感键，
  `app/schemas/channels.py` L39-41 敏感集合仅含 `token/encoding_aes_key/corpsecret/bot_secret`）。
