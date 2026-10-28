# Step 13.3 — Interaction Operations View Implementation Audit

> **OSS 发布说明**：本文为历史审计记录；文中引用的 `frontend/`（Web UI 源码）**不包含在当前 OSS 发行内容中**（见 README「Project status」）。

> **审计步，生产代码改动：0 行。**
> 交付物 = 本报告（实现前边界审计），喂给 Step 13.4（Minimal Observation UI）。
>
> 输入三件已落成事实：
> 1. **13.1 只读 Vertical Slice**：`schemas/interactions.py` + `api/management/interactions.py`
>    + `InteractionObservabilityService`，已挂在 `main.py:114-121`（纯读、零写）；
> 2. **13.2 Operational Observation Surface 拍板**：十个设计问题已定（双状态、失败三元组、
>    `processing_started_at` 卡死提示、payload 折叠、按 id deep-link、无实时、只读 Operational
>    Surface 定性）；
> 3. **现有 Management UI**：`frontend/` 下 5 个静态 HTML 页（index/agents、channels、
>    connections、users、identities），共享 `styles.css`，每个页面 `topbar` 里用
>    `.topbar-nav` 互相链接（`index.html:13-16` 等），每个页有自己独立的 `<page>.js`
>    （list + form 的 show/hide 模式，参考 `app.js`）。

---

## 0. 审计目标

回答 Step 13.3 提出的具体问题，并给 Step 13.4 拍板：**13.4 的 UI 是否可以不新增任何
Runtime / Persistence / Domain 语义，仅消费 13.1 已落地的只读 API。**

结论先放：**可以。零新增后端语义；13.4 只新增前端两个文件（或三处 topbar 链接改动），
后端只读 API 已齐备。** 详见 §10。

---

## 1. 逐条问答

### Q1. 现有 Management UI 的 layout / routing 是否可以直接复用？

**复用 layout，不沿用 routing 模式。**

- **Layout 可复用**：`styles.css` 已含全部所需基础类——`.card` / `.card-head` /
  `.card-head-actions` / `.agent-table`（表头表体）、`.badge`（状态胶囊）、`.alert`、
  `.empty-hint`、`.btn` 系列。Observation 用的「列表卡片 + 详情卡片」与现有
  Agent/Channel 的「list-section + form-section」同构，直接套用，**无需新增 CSS**。
- **Routing 不沿用**：现有 5 页是「list + 写 form」的 CRUD 模板，且每页耦合了写动作
  （新建/编辑/状态切换）。Observation 是**纯读**，没有 form，因此不拷贝 `app.js` 的
  写逻辑（`submitForm` / `setStatus` 一类），只复用其「`loadList` → `renderList` →
  `renderRow`」的陈列骨架，并新增「点击行 → `show(detailSection)` 拉详情」的分支
  （参考 `app.js` 里 `openEdit` 切换 section 的写法）。
- **无 SPA 路由**：项目刻意「无框架、无构建、无依赖」（`styles.css:1`）。13.4 维持该纪律，
  Observation 用一个新静态页 `interactions.html`，不引入前端 router。

**拍板**：复用 `styles.css` 全套类；新页面复用列表渲染骨架，但剥离所有写动作；不引入
前端框架/路由。

### Q2. Interaction List 应该放在哪里？

**新建独立页 `frontend/interactions.html`（配 `interactions.js`），并在 5 个现有页
的 `topbar` 各加一条 `.topbar-nav` 链接到它。**

- Observation 是 Control Plane 的第三个面（13.2 Q10），不应塞进 Agent/Channel 任一页的
  子面板——它不是它们的从属视图；
- 现有 topbar 模式是「每页互链」（`index.html:13-16`），新页需反向回链，因此改动是：
  - 新建页 topbar 含 5 条回链（← Agent / Channel / Connection / User / Identity）；
  - 现有 5 页 topbar 各加 1 条「Interaction Observability →」（`/interactions.html`）；
- 这是**纯前端导航改动**，零后端、零路由规则（FastAPI `main.py:201` 已 `StaticFiles`
  catch-all 托管整个 `frontend/` 目录，新增 `.html` 自动可访问）。

**拍板**：`/interactions.html` 独立页 + 6 处 topbar 链接（1 新 + 5 旧各 +1）。

### Q3. Detail + Activity Timeline 如何组成最小页面？

**单页内两段式：列表卡片（默认可见）+ 详情卡片（点击行后 show），详情卡内嵌时间线区块。
复用 `app.js` 的 `list-section` / `form-section` show-hide 切换范式。**

```
interactions.html
└─ <section id="list-section">   列表卡片（默认）
     table: id | direction | channel_id | session_id
          | processing_status | delivery_status | failure_* | processing_started_at | created_at
     （行点击 → openDetail(id)）
└─ <section id="detail-section" class="hidden">   详情卡片（点击后）
     事实字段行（双状态 chip + 失败三元组 + attempt fence 时间戳 + DB 审计时间戳）
     request_payload  <details 折叠>
     response_payload <details 折叠>
     <section id="timeline">  ActivityResponse 有序投影（stage 主轴）
     返回列表按钮 → show(listSection)
```

- 时间线**不是独立路由/独立页**，是详情卡内的一个子区块——避免引入第二层路由，符合
  「无框架」纪律；
- `GET /{id}` 一次返回 `facts + payload + activities`（`schemas/interactions.py:73-78`
  `InteractionDetailResponse`），**一次调用即还原执行故事**，无需先列表再拉时间线两次请求。

**拍板**：单页两段式（list ↔ detail），detail 内嵌 timeline 子区块；详情一次调用取全。

### Q4. 双状态如何原样表达？

**两个独立 chip，绝不合成。** 直接取 `processing_status` 与 `delivery_status` 两个字段
（`schemas/interactions.py:63-64`），各渲染一枚 `.badge` 风格胶囊：

- 标签明示两侧归属，如「执行：<chip> / 投递：<chip>」，不用颜色强行归一成单一「失败」；
- 关键合法组合 `succeeded × failed`（Agent 成功、WeCom 投递失败）要**并排可见**，不丢信息
  （13.1 S3 已验证该组合原样透出）；
- 复制 `app.js:52-57` 的 `statusBadge` 写法生成胶囊，但**不引入任何计算列**——值直接来自
  API 字段，不派生 overall status / result / state（13.2 形状守卫 #2 已锁死）。

**拍板**：两枚独立 chip，值透传，无合成字段。

### Q5. `processing_started_at` 如何用于「可能卡住」的事实提示？

**客户端基于 `processing_started_at` 计算「已执行 N 秒」，仅对 `processing_status=processing`
的行/详情做高亮，不落地任何新字段、不新增存储。**

- `processing_started_at` 是 attempt fence 事实（`schemas/interactions.py:68`，12.9.4.12），
  API 已原样返回；
- UI 逻辑：若 `processing_status === "processing"` 且 `processing_started_at` 存在，
  计算 `now - processing_started_at` 的秒数，超过某阈值（如 5 分钟，纯前端常量，非配置、
  非后端字段）则在行/详情上渲染「⚠ 已执行 Ns（疑似卡住）」灰/黄提示；
- 该提示是**事实呈现**（`reclaim_stale_processing` 孤儿回收由 Runtime Driver 负责，UI 不重做）；
- `processing_status` 不是 `processing` 时，该字段不参与任何提示——`now()` 纯前端，零网络、
  零后端改动。

**拍板**：纯客户端时间差高亮，阈值写死在前端常量，不新增后端/存储语义。

### Q6. payload 默认折叠的具体 UI 边界是什么？

**边界 = `InteractionDetailResponse` 的两个字段（`schemas/interactions.py:76-77`）：
`request_payload` / `response_payload` 仅详情出现，列表绝不含（13.1 陈列面纪律）。**

- 详情里两者默认折叠：用原生 `<details><summary>`（或等价的 show/hide 按钮）包裹，
  展开时 `JSON.stringify(payload, null, 2)` 美化 + `<pre>` 等宽呈现；
- **NULL 必须正确渲染**：`response_payload === null`（执行未到 / 投递失败，13.1 S3 锁死）
  显示「无响应快照（执行未完成 / 投递失败）」文案，**不是**报错、不是空 `{}`、不是 `null` 字面；
  `request_payload === null` 同理显示「无入站快照」；
- 折叠是纯前端行为，无 API 改动；后端只在详情端点返回 payload（列表端点本来就不返回，
  见 `InteractionSummaryResponse` 无 payload 字段）。

**拍板**：详情内 `<details>` 默认折叠 + JSON 美化；NULL → 固定文案；列表永远不带 payload。

### Q7. channel / session deep-link 现有路由是否足够？

**部分足够：路由「落地页」足够，但「按 id 精确定位实体」当前不足——MVP 可先用粗粒度链接，
精确 deep-link 是后续增强，且不阻塞 13.4。**

- 落地页存在：Channel / Connection / User / Identity 各有静态页（`/channels.html` 等），
  FastAPI catch-all 已托管（`main.py:201`），新页与现有页都能互链；
- **精确 id 定位不足**：现有 5 个 `.js`（含 `app.js`）都不解析 `?id=` / `URLSearchParams`
  （全局搜索 `location.search` / `URLSearchParams` = 0 命中），即点击 Observation 里的
  `channel_id` 跳到 `/channels.html?id=123` 只会落到该页列表顶部，**不会高亮/打开指定 Channel**；
- 13.2 Q5 把 deep-link 定性为「路由/前端关切，非 API 契约变更」。13.4 MVP 的做法：
  - Observation 把 `channel_id` / `session_id` 渲染为 `<a href="/channels.html?id={id}">` 形式的
    链接（id 引用，不嵌实体，符合 13.2 形状守卫 #3）；
  - 精确 deep-link（Management 页读取 `?id=` 并定位）**留作 13.4 之后的增强**，MVP 不强求；
- 注意：`session_id` 在 Session 建立前失败的 Interaction 上是 `NULL`（13.2 Q5 / 13.1 S4），
  UI 须对 `NULL` 渲染为「—」且不生成失效链接。

**拍板**：MVP 用「带 `?id=` 的粗粒度链接 + 现有 Management 落地页」即可；精确 deep-link
为后续增强，不阻塞 13.4，且全程零后端改动。

### Q8. 是否需要分页？

**不需要，MVP 沿用 13.1 的 bounded `limit` 模型。**

- 13.1 已定：`GET /api/interactions?limit=`（默认 50，合同 `1..200`，越界 422，固定上限、
  非翻页游标）（`api/management/interactions.py:58-70`、`MAX_LIST_LIMIT=200`）；
- 现有前端 `loadList` 范式（`app.js:97-105`）天然适配「一次取 N 条渲染」，**无需分页组件**；
- 若未来数据量逼出翻页，是独立关切（且需重新评估是否触碰 limit 合同），不在 13.4。

**拍板**：不实现分页；列表调用 `?limit=50`（或 UI 可选 50/100/200 下拉，仅改 query 参数）。

### Q9. 是否需要自动刷新？

**不需要（与 13.2 Q7 结论一致）。**

- 观察的是**已提交事实**；新完成 Interaction 靠列表「最新在前」+ 手动「刷新」按钮即可见；
- 现有每页都有手动「刷新」按钮（`index.html:25` `id="btn-refresh"`，`app.js:311` 绑定
  `loadList`），Observation 直接复用该模式，**不引入 SSE / WebSocket / 长轮询**；
- 引入实时推送即踏入 10.7.1 已排除的 Observability Platform 范畴。

**拍板**：仅手动刷新，复用现有 `.btn-ghost#btn-refresh` 模式；无自动刷新基础设施。

### Q10（最关键）. 13.4 UI 是否能够做到零新增 Runtime / Persistence / Domain 语义？

**能。且已具备全部前置条件。**

| 维度 | 13.4 是否需要新增 | 现状证据 |
| --- | --- | --- |
| Runtime | 否 | 13.1 纯读，不经 Runtime Coordinator/Worker/PersistingCoordinator（`main.py:114-121` 注释） |
| Persistence | 否 | 13.1 只读 Adapter（单 select，无 insert/update/delete），13.1 S1-c 形状守卫已锁 |
| Domain | 否 | Interaction/Activity 领域模型零改动；UI 只读事实字段，不合成状态 |
| API 契约 | 否 | 三端点已落地（`api/management/interactions.py`），13.4 只消费 |
| Schema | 否 | `InteractionSummary/DetailResponse` + `ActivityResponse` 已齐（`schemas/interactions.py`） |
| 前端框架/构建 | 否 | 沿用「无框架、无构建、无依赖」纪律，新增 `interactions.html` + `interactions.js` |
| CSS | 否 | 复用 `styles.css` 全部现有类，零新增样式（除非仅做语义化微调） |
| 路由 | 否 | FastAPI `StaticFiles` catch-all（`main.py:201`）自动托管新 `.html` |
| 鉴权 | 否 | Observation 落在 Management 面，复用现有 Management 鉴权（13.2 Q6），不引入新权限模型 |

**13.4 唯一的生产代码改动范围（纯前端，0 行后端）**：
1. 新增 `frontend/interactions.html`（列表 + 详情两段式，含 topbar 回链）；
2. 新增 `frontend/interactions.js`（list 渲染骨架 + 点击行拉详情 + 双状态 chip +
   payload `<details>` 折叠 + 时间线 stage 轴 + `processing_started_at` 客户端卡死提示 +
   手动刷新）；
3. 在现有 5 个 HTML 的 `topbar` 各加 1 条 `.topbar-nav` 链接到 `/interactions.html`。

> 一句话：**13.4 是纯消费侧 UI，后端只读面已在 13.1 完整交付，零新增 Runtime / Persistence /
> Domain 语义——审计通过，可直接进入 13.4 实现。**

---

## 2. 给 Step 13.4 的边界清单（实现时严格守）

| 主题 | 13.3 拍板 | 13.4 实现守约 |
| --- | --- | --- |
| Layout | 复用 `styles.css` | 不新增 CSS 类（除非语义化微调） |
| Routing | 独立 `interactions.html`，无 SPA router | 不引入前端框架/路由 |
| List 位置 | 新建页 + 6 处 topbar 链接 | 改动仅限前端导航 |
| 详情组成 | list ↔ detail 两段，detail 内嵌 timeline | 一次 `GET /{id}` 取全 |
| 双状态 | 两枚独立 chip，值透传 | 不合成 status/result/state |
| 卡死提示 | 客户端时间差，阈值写死常量 | 不新增后端/存储字段 |
| payload | 详情 `<details>` 折叠，NULL→固定文案 | 列表永远不带 payload |
| deep-link | 粗粒度 `?id=` 链接即可，精确定位后续增强 | 不嵌 Management 实体响应 |
| 分页 | 不实现，沿用 `limit` | 不引入翻页游标 |
| 自动刷新 | 否，仅手动刷新 | 不引入 SSE/WS/轮询 |
| 后端语义 | 零新增 | 不碰 Runtime/Persistence/Domain/API/Schema |

---

## 3. 形状守卫（机器可验证，建议并入 13.4 测试）

13.4 交付后，下列不变量应被前端测试/静态检查锁死（与 13.2 后端形状守卫互补）：

1. **零写请求**：`interactions.js` 内 `fetch` 调用只使用 `GET` 方法，无 POST/PUT/PATCH/DELETE；
2. **双状态不合成**：详情渲染函数直接读取 `processing_status` 与 `delivery_status`，不出现
   任何 `status` / `overall_status` / `result` / `state` 的合成字段或计算；
3. **导航不嵌实体**：`channel_id` / `session_id` 仅渲染为带 `?id=` 的链接或纯文本，
   Observation 响应体不请求/不渲染 `Channel` / `Agent` / `Session` 管理实体详情；
4. **payload NULL 文案**：`response_payload === null` 时显示「无响应快照」而非报错/空对象；
5. **无实时基础设施**：`interactions.js` 不含 `WebSocket` / `EventSource` / `setInterval` 轮询。
