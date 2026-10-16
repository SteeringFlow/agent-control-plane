# Step 13.2 — Operational Observation Surface Audit

> **审计步，生产代码改动：0 行。**
> 交付物 = 本报告 + 形状守卫测试
> （`tests/test_step13_2_operational_observation_surface_audit.py`）。
>
> **不是画 UI，不是做 Observability Platform**（10.7.1 裁决原样生效：
> Prometheus / Grafana / OpenTelemetry / tracing / Read Model / CQRS 一律不要）。
> 本步只回答一个前置问题，给 Step 13.3+（ Observability 面富化 / UI ）拍板：
>
> **Observation 面到底要暴露什么、怎么表达、边界在哪。**
>
> 所有决策锚定既有事实：4.2 冻结双状态、10.7.1 审计、13.1 只读 Vertical Slice
> （已落地的字段面与三端点）。

---

## 0. 范围与不做

**做**：用真实数据面（13.1 已落地的 `InteractionSummary/Detail`、`ActivityResponse`、
`GET /api/interactions*`）+ 13.1 验收里跑过的真实执行故事（⑥ WeCom wire → ACK →
Driver → 真六段；两条失败路径），逐条回答 10 个设计问题并拍板。

**不做（本步明确排除）**：

- 不写任何生产代码：不改 schema、不加端点、不加读方法、不加字段、不加权限模型；
- 不引入实时/推送/轮询/WebSocket/SSE 基础设施；
- 不合成任何状态、不派生 overall status、不把双状态合并成单字段；
- 不为「Observation 是否进入 Management 执行动作」拍板——本步结论明确：**不**；
- 不为 UI 布局/组件拍板（那是 13.3+ 的事）。

---

## 1. 十个设计问题的拍板

### Q1. Interaction 列表真正需要哪些字段？

**决策**：列表 = 13.1 `InteractionSummaryResponse` 的原样字段，不再增减：

```
id, direction, channel_id, session_id, external_interaction_id,
processing_status, delivery_status,
failure_stage, failure_code, retryable,
processing_started_at, created_at, updated_at
```

- **只取领域/表的原生列**（廉价，单表 `interactions` 即可，无 join）；
- **不含 payload、不含 timeline**（那是详情面按需拉取的事，列表是陈列面）；
- **不加「状态徽章 / 结果词」计算列**——那等于在列表层重新解释状态，违反 4.2；
- **不加 conversation / identity 文本列**——它们在 `request_payload` 里，列表不 join
  payload；需要时在详情看，或用 `external_interaction_id` 这种已有列做粗筛。
- 排序：`created_at` 降序（最新在前），与 13.1 一致。

### Q2. Detail 页面如何表达双状态？

**决策**：`processing_status` × `delivery_status` 作为**两个独立指示**，并列呈现，
**绝不合成**一个 overall status。

- 这是 4.2 冻结的核心：一个请求的执行结果天然是「Agent 侧」与「投递侧」两件事；
- 最该被看见的恰恰是 `succeeded × failed`（Agent 成功、WeCom 投递失败）这种组合——
  **双状态拆开本身就是信息**，合成成单字段反而丢信息；
- UI 建议：两枚独立状态 chip（如「执行：成功 / 投递：失败」），不用颜色强行归一成
  「失败」；`failure_*` 三元组挂在真正失败的那个侧。

### Q3. Activity Timeline 如何表达执行阶段？

**决策**：时间线 = `activities` 行的 **1:1 有序投影**，以 `stage` 为主轴。

- 顺序：`created_at` 升序、`id` 升序（写入序 tiebreak），与 13.1 一致——原生发生序
  就是「执行故事」，不重排、不归并、不故事化；
- 每行展示：`created_at` + `type`/`stage` + `status` + `message` + `metadata`；
- `stage` 是「执行阶段」的事实载体（如 `interaction_received` / `session_resolved` /
  `agent_dispatched` / `agent_completed` / `channel_delivery_*`）；UI 用 `stage` 做
  阶段轴标签，**不**用 `message` 推断阶段；
- Evidence 不改写（append-only），Observation 只读不改写历史。

### Q4. Failure 如何呈现，而不重新定义状态？

**决策**：失败 = 一个 **nullable 的 typed 三元组**，挂在产生它的真实侧，绝不发明新状态。

- 三元组：`failure_stage` / `failure_code` / `retryable`；
- **成功时这三个字段是 NULL**——「没有失败点」= 字段为空，不是字段缺失（13.1 S3 已验证
  `response_payload=NULL` + `delivery_status=failed` + `processing_status=succeeded`
  的合法组合）；
- 呈现位置两处一致：① 双状态行的失败侧 chip 下方显示三元组；② 时间线里对应
  `stage_failure` 行的 `metadata.failure_code`；
- `retryable` 仅作为**事实提示**（布尔），**不是动作**——本面不提供重试（10.7.1 已排除
  retry 基础设施）；UI 可灰显「可重试」字样，但不挂重试按钮。

### Q5. Session / Agent / Channel 如何建立关联导航？

**决策**：**关联 = 按 id 引用（deep-link），不嵌入管理实体**。

- 可达路径（13.1 schema 已带）：`channel_id`（直接列）、`session_id`（直接 FK）；
  再经 `sessions` → `connection_id` → `agent_id`（需 session 存在——Session 建立前
  失败则 `session_id=NULL`，本身即事实，10.7.1 §1.1 已记）；
- Observation 只读面**只暴露这些 FK id**，不做跨 management 表 join 去取名字；
- 「进入 Management」= UI 用 id 构造 deep-link（如 `/management/channels/{id}`），
  是**路由/前端关切**，不是 API 契约变更；
- 不在 Observability 响应里嵌 `Channel`/`Agent`/`Session` 响应模型（否则 Observation
  与 Management 写域耦合）。

### Q6. payload 是否需要折叠 / 权限边界？

**决策**：

- **折叠**：payload 只在详情出现（13.1 已落地）。`request_payload` / `response_payload`
  可能较大（Agent 完整回复），UI **默认折叠**，按需展开 + JSON 美化；
- **权限边界**：Observation 落在 **Management 面**，与现有 Management API 同鉴权——
  **不引入新权限模型**；能看到 Management 的人就能看 payload；
- **PII 提示（非本步实现）**：payload 含 `external_identity_id` / 对话原文等外部内容，
  未来若需脱敏是独立关切，不在 13.2 拍板；
- **NULL 必须正确渲染**：`response_payload=NULL`（执行未到 / 投递失败）应显示
  「无响应快照（执行未完成 / 投递失败）」，**不是**报错或空对象——13.1 S3 已锁。

### Q7. 是否需要实时刷新？

**决策**：**不需要实时基础设施**（无 SSE / WebSocket / 长轮询）。

- Observation 读的是**已提交事实**；新完成的 Interaction 靠列表「最新在前」+ 手动刷新
  即可看到；
- 引入推送即踏入 Runtime 邻接的实时通道，属于 10.7.1 已排除的 Observability Platform
  范畴；
- 若未来要「live tail」，是独立关切（且需评估是否触碰 Runtime），不在本步。

### Q8. 是否需要「正在执行」状态？

**决策**：**不需要新状态**——「正在执行」已是 `processing_status` 的一等取值。

- `processing_status = processing` + `delivery_status = pending` 即「执行中」；
- 13.1 schema 已原生携带，Observation 只读它即可；
- **潜在卡死（Worker 中途崩）** 的可见性靠既有时间戳：`processing_started_at`
  （12.9.4.12 attempt fence 事实）+ `reclaim_stale_processing` 孤儿回收——
  UI 可**基于 `processing_started_at` 客户端计算「已执行 N 秒」**做高亮提示，
  **不**落地新状态、不新增存储字段。

### Q9. 是否需要从 Observation 进入 Management？

**决策**：**需要导航，但不需要（也不提供）从 Observation 执行 Management 动作**。

- 导航：Q5 的 id deep-link 即「进入 Management」——点到 Channel/Session/Identity 的
  管理页；
- **动作**：Observation 面**绝不含**任何写端点（无 POST/PUT/PATCH/DELETE）；
  重试 / 暂停 / 杀死等操作性动作若存在，属于**独立**的控制动作面，不挂在 Observation 上
  （10.7.1 已排除 retry 基础设施，本步延续）；
- 这是本步最关键的次级结论，喂给 Q10。

### Q10（最关键）. Observation 到底是「日志查看器」还是 Control Plane 的 Operational Surface？

**决策**：**Observation 是 Control Plane 的「只读 Operational Surface」，位于 Management
面之下；它观察 Runtime 执行事实并提供到 Management 的导航，但永不变更状态。**

- **不是「日志查看器」**（被动、历史、无结构、无关联）：Observation 是结构化的
  （双状态 / 失败三元组 / 时间线 stage 轴）、可查询的（bounded list）、**关联化**的
  （FK id → Management deep-link）；
- **也不是「控制面」**（Control Surface 含写动作）：本面零 mutation，与 Persisting
  Coordinator 的写边界对称；
- **架构定位**：`Runtime（执行）← 写 → 数据面`；`Management（配置/变更）`；
  `Observation（只读面，读 Runtime 事实 + 链接 Management）` 是 Control Plane 的
  第三个成员，但与 Management 共享「面」与鉴权，只在**动作能力**上严格只读；
- 命名印证：13.1 API tag 已是 `observability-interactions`，语义即「可观测的执行面」。

> 一句话：**Observation = 只读 Operational Surface（看得到执行故事、点得进 Management，
> 但动不了手）。** 任何「动手术」的需求，都归到独立的控制动作面，不污染 Observation。

---

## 2. 给 Step 13.3+ 的拍板清单

| 主题 | 本步结论 | 留给后续 |
| --- | --- | --- |
| 列表字段 | Summary 原样 13 字段，无计算列 | 富化排序/过滤是 13.3+ 关切（仍 bounded） |
| 双状态 | 两枚独立 chip，不合成 | UI 配色规范 |
| 时间线 | Evidence 1:1 有序，`stage` 为主轴 | 阶段分组/折叠 UI |
| 失败 | nullable 三元组，挂真实侧 | 不挂重试动作 |
| 关联导航 | 按 id deep-link，不嵌实体 | UI 路由实现 |
| payload | 详情出现、默认折叠、同 Management 鉴权 | 脱敏是独立关切 |
| 实时 | 不需要 | live tail 是独立关切 |
| 执行中 | `processing_status=processing` 已表达 | 卡死提示靠 `processing_started_at` |
| 进入 Management | 导航 yes / 动作 no | 控制动作面另立 |
| 面定性 | 只读 Operational Surface（Management 下） | 控制动作面不挂 Observation |

---

## 3. 形状守卫（机器可验证）

`tests/test_step13_2_operational_observation_surface_audit.py` 锁死本步拍板的
**形状级不变量**（读路径现状，非新功能）：

1. **只读面**：`api/management/interactions.py` 的 router 只暴露 GET，无
   POST/PUT/PATCH/DELETE；
2. **双状态不合成**：`InteractionSummaryResponse` / `InteractionDetailResponse`
   同时含 `processing_status` **和** `delivery_status`，且**不含**任何名为
   `status` / `overall_status` / `result` / `state` 的合成字段；
3. **导航不嵌入管理实体**：详情响应携带 `channel_id` / `session_id` 引用，但
   **不嵌入** `Channel` / `Agent` / `Session` 响应模型（Observation 与 Management
   写域解耦）。
