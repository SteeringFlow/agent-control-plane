# Step 13.1 — Interaction Observability Read Vertical Slice

> **纯读 Vertical Slice。** 交付物 = 本报告 + 只读读路径
> （Port / Adapter / Service / Schema / API）+ 验收测试
> （`tests/test_step13_1_interaction_observability_read.py`）。
>
> **不是做 Observability Platform**（10.7.1 的裁决原样生效：Prometheus /
> Grafana / OpenTelemetry / tracing / Read Model / CQRS 一律不要）。
> 只做第一刀，非常小：
>
>     GET /api/interactions
>     GET /api/interactions/{id}
>     GET /api/interactions/{id}/activities
>
> **最终验收标准只有一个**：不直接查数据库，仅通过 Observability API，
> 能完整还原一次真实 Interaction 的执行故事。

---

## 0. 触发条件与范围

10.7.1 §Q4/Q10 记录了读路径的触发条件——「出现真实消费者场景」。本步即该
触发条件的达成：管理员 / UI 需要「这次请求发生了什么」的读用例。

**做**：

- Repository Port + SQLAlchemy Adapter 新增**纯读**方法：
  `InteractionRecordRepository.list_recent(limit)` /
  `ActivityRecordRepository.list_by_interaction(interaction_id)`；
- Application 新增只读服务 `InteractionObservabilityService`
  （List / Get / Timeline 三个用例）；
- Management 面新增三个 GET 端点（`api/management/interactions.py`）；
- 领域记录补只读时间戳回显（`InteractionRecord.created_at/updated_at`、
  `ActivityRecord.created_at`）——mapper `to_domain` 读回，`to_orm` 写路径
  零改动（仍由 DB 生成）。

**不做（边界冻结）**：

- 不修改 Runtime / Async Core / Driver / 六段 Executor / Worker /
  PersistingCoordinator（零行改动）；
- 不改变 Interaction / Activity 状态——不 create / claim / record_outcome /
  reclaim / append；读用例不 commit（没有要提交的变更）；
- 不重新解释状态：不合成 overall status、不派生结果字段、不改写 Evidence
  （不归并 / 不过滤 / 不故事化）；
- 不做分页游标 / 任意条件过滤 / 聚合统计 / Read Model / Projection / CQRS
  （bounded limit，与 `find_pending` 同纪律）；
- 不做写端点 / 重试 / replay / 状态修复。

## 1. 端点面（13.1 冻结）

| 端点 | 语义 | 404 |
| --- | --- | --- |
| `GET /api/interactions?limit=` | 最近 N 条（最新在前；陈列面，**不含 payload**） | — |
| `GET /api/interactions/{id}` | 详情 = 事实字段 + request/response payload + Evidence 时间线（**一次调用还原执行故事**） | ✅ |
| `GET /api/interactions/{id}/activities` | Evidence 时间线（发生序） | ✅（interaction 不存在）；存在但无 Evidence → 空列表 |

- `limit`：默认 50，合同面 `1..200`（越界 422，不静默截断）；固定上限，
  不是翻页参数；
- 错误映射：本面只有 404（Interaction 不存在，隐藏存在性）。

## 2. 响应契约要点

**不重新解释状态**（核心红线）：

- `processing_status × delivery_status` 原生双状态独立透出——
  「Agent 成功 × 投递失败」这类 4.2 冻结的合法组合在协议面就是两个字段，
  绝不合成单一 status；
- 失败三元组 `failure_stage / failure_code / retryable` 原样透出
  （成功时为 NULL = 没有失败点，事实为空不是字段缺失）；
- `processing_started_at`（attempt fence 事实，12.9.4.12）原样透出；
- `created_at / updated_at`（DB 审计时间戳）读路径回显；
- Evidence 1:1 投影：`type / stage / status / message / metadata /
  created_at`，按 `created_at` 升序、`id` 升序（写入序 tiebreak）排列；
- payload 只出现在详情：`request_payload`（12.9.4.3 入站快照）、
  `response_payload`（10.7.2 B 裁决的 AgentResponse 原样快照）。

## 3. 架构位置

```
api/management/interactions.py   FastAPI（HTTP 事实提取 + 序列化 + 404）
    ↓
application/interaction_observability.py   InteractionObservabilityService（纯读用例）
    ↓
repositories Port（list_recent / get_by_id / list_by_interaction）
    ↓
persistence Adapter（单一 select，零写入）→ DB
```

- 10.7.1 §Q6 的边界张力在本步落定：Evidence 是 Runtime 数据面的产物，
  但读它的是管理员——端点落在 **Management 面**；读路径**绝不经过**
  Runtime Coordinator / Worker / PersistingCoordinator（读不改执行，
  与 PersistingCoordinator 的写边界对称）；
- 依赖方向由 `tests/test_import_boundaries.py` 继续锁死；
  application 文件面守卫（6.7）更新：`+ interaction_observability`。

## 4. 验收（真实 E2E 数据基准 = 12.9.4.17）

被测链路全部是真实 Core（真库 / 真 phase API / 真 Worker / 真 Driver /
真六段 RuntimeCoordinator / 真 WeCom wire）；仅 Agent wire / Channel 出站
用记录型 / 失败型 Adapter 替代（12.9.4.17 同一裁决）。

| # | 验收 | 测试 |
| --- | --- | --- |
| 1 | 只读服务零写能力、零 Runtime/persistence 依赖（AST） | S1-a |
| 2 | Observability API 只有 GET 端点（AST） | S1-b |
| 3 | 读方法函数体只有 select，无 update/insert/delete/add/flush（AST） | S1-c |
| 4 | ⑥ 复刻：WeCom wire → ACK（列表可见 PENDING，ACK ≠ 执行）→ Driver → API 还原成功故事（双状态 / 失败点为空 / Who / Conversation / response_payload 内容 / fence 时间戳 / 时间线 received→completed / 时间线端点一致） | S2 |
| 5 | 投递失败故事：`succeeded × failed` 合法组合原样透出 + 失败三元组 + `response_payload=NULL`（不可伪造）+ 时间线 received→stage_failure（metadata.failure_code） | S3 |
| 6 | Session 建立前失败：`session_id=NULL` 本身是事实 + `identity_resolution` 失败三元组 | S4 |
| 7 | 列表 = 陈列面：最新在前、不含 payload/时间线、limit 越界 422 | S5 |
| 8 | 未知 id → 404；三次 GET 后两表全量快照零变化（纯读证明） | S6 |
