# Step 13.4 — Minimal Interaction Operations View

> **OSS 发布说明**：本文为历史实现记录；文中引用的 `frontend/`（Web UI 源码）**不包含在当前 OSS 发行内容中**（见 README「Project status」）。

> **纯前端 Vertical Slice。** 后端改动：**0 行**——只消费 13.1 已落地的三个 GET 端点。
> 交付物 = 本报告 + `frontend/interactions.html` + `frontend/interactions.js`
> + 现有 5 页 topbar 导航各 +1 条 + `styles.css` 语义化微调（纯视觉，零语义）。

---

## 1. 实现范围（严格按 13.3 边界）

**做了**：

| 项 | 文件 | 说明 |
| --- | --- | --- |
| Observation 页 | `frontend/interactions.html` | List + Detail 两段式；topbar 回链 5 个管理页 |
| 只读交互逻辑 | `frontend/interactions.js` | 仅 `GET`（`requestGet` 是全页唯一网络出口） |
| 导航 | `index/channels/connections/users/identities.html` | 各 +1 条 `.topbar-nav` → `/interactions.html` |
| 样式微调 | `frontend/styles.css` | 新增 badge-processing/pending、timeline、payload、fact 样式；全部复用既有 CSS 变量 |

**没做（13.3 禁止清单逐项核对）**：

❌ 新 API ❌ 新 DB 字段 ❌ 新 DB 表 ❌ Runtime 修改 ❌ Worker 修改 ❌ Driver 修改
❌ Persistence 写路径修改 ❌ 新状态 ❌ overall_status ❌ Retry ❌ Replay ❌ SSE
❌ WebSocket ❌ Polling ❌ Metrics ❌ Dashboard ❌ 新权限模型 ❌ Agent/Channel/Session 嵌入式实体
——以上**全部为零**（后端 `app/` 目录零改动；前端无 WebSocket/EventSource/setInterval）。

## 2. 页面形态

```
interactions.html
├─ List 卡片（陈列面，?limit=50，不含 payload）
│    列：ID / Direction / Channel(deep-link) / Session / 执行 chip / 投递 chip
│        / Failure(stage/code) / Created
│    行内卡死提示：processing 行显示「已执行 Ns」，≥300s 追加「（疑似卡住）」
└─ Detail 卡片（点击行后 GET /{id} 一次取全）
     ├─ 双状态：执行：<chip> 投递：<chip>（两枚独立，绝不合成）
     ├─ 卡死提示条（仅 processing × processing_started_at，纯客户端时间差）
     ├─ 失败三元组（nullable；有则挂真实失败侧；retryable 仅事实提示文案，无按钮）
     ├─ fact grid：direction / external_interaction_id / channel_id(deep-link)
     │            / session_id（NULL→"—"，无失效链接）/ processing_started_at
     │            / created_at / updated_at
     ├─ request_payload  <details> 默认折叠，JSON 美化；NULL→「无入站快照」
     ├─ response_payload <details> 默认折叠，JSON 美化；NULL→「无响应快照（执行未完成 / 投递失败）」
     └─ Activity Timeline：Evidence 1:1 有序投影（API 返回序原样渲染）
          每行 = created_at + stage(主轴，缺失回退 type) + status chip
                + message + metadata（原样 JSON）
```

## 3. 唯一验收问题：**PASS**

> 「一个真实 Interaction，从 List 点进去后，仅通过已有 13.1 Read API，能完整看到它的
> 执行事实，并且整个 Observation 页面产生 0 个写请求、0 个 Runtime 调用、0 个状态改变。」

实测记录（本地真库 + 真服务 + 真浏览器自动化）：

| # | 验证 | 结果 |
| --- | --- | --- |
| 1 | `GET /api/interactions?limit=` → 200（最新在前；bounded limit） | ✅ |
| 2 | List 渲染三条真实形状 Interaction：`succeeded×delivered`、`succeeded×failed`（4.2 合法组合）、`processing×pending`（已执行 28843s 疑似卡住提示生效） | ✅ |
| 3 | 点击行 → `GET /{id}` → Detail：双状态两枚独立 chip、失败三元组挂投递侧、payload 默认折叠、时间线 `interaction_received→…→stage_failure(metadata.failure_code)` 完整发生序 | ✅ |
| 4 | `session_id=NULL`（Session 建立前失败）渲染 "—"；`response_payload=NULL` 渲染固定事实文案 | ✅ |
| 5 | channel_id deep-link：`/channels.html?id={id}`（id 引用，不嵌实体） | ✅ |
| 6 | **0 写请求**：服务访问日志 `POST/PUT/PATCH/DELETE = 0`；整个会话 `GET /api/interactions* = 14` 次 | ✅ |
| 7 | 0 Runtime 调用 / 0 状态改变：读路径不经 Runtime（13.1 已锁）；前后全表事实未变 | ✅ |
| 8 | 未知 id → 404（三次 GET 后数据零变化，13.1 S6 同纪律） | ✅ |
| 9 | `node --check interactions.js` 语法通过；无 WebSocket/EventSource/setInterval | ✅ |

## 4. 语义红线遵守情况（逐条）

1. **只有 GET**：全页网络出口唯一（`requestGet`），仅列表/详情/刷新三个调用点；
2. **双状态不合成**：`processing_status` / `delivery_status` 直接透传为两枚 chip，
   无任何 `status`/`overall_status`/`result` 计算；
3. **Evidence 1:1**：时间线按 API 返回序渲染，不重排/不归并/不故事化；`stage` 主轴，
   缺失时回退显示 `type`（仍是原样字段，非推断）；
4. **payload NULL 文案**：`null` →「无入站快照」/「无响应快照（执行未完成 / 投递失败）」，
   不报错、不渲染空对象；
5. **失败三元组**：nullable——成功时整体不渲染（事实为空不是字段缺失）；
   `retryable` 只出现在文案中，无重试按钮；
6. **导航 = id 引用**：channel deep-link；session 无管理页故渲染纯文本；不请求任何
   Management 实体详情；
7. **无实时基础设施**：仅手动刷新（列表按钮 / 详情按钮），无 SSE/WS/轮询；
8. **卡死提示**：阈值 300s 为前端常量（`STUCK_HINT_SECONDS`），纯 `Date.now()` 时间差，
   不落地新字段、不重做 `reclaim_stale_processing`（孤儿回收仍归 Runtime Driver）。

## 5. 已知边界（与 13.3 拍板一致，非缺陷）

- **deep-link 精确定位**：`/channels.html?id=` 落到 Channel 列表页顶部，Channel 页尚未
  解析 `?id=` 定位到具体行——后续增强，不阻塞本步；
- **分页**：未实现，沿用 13.1 bounded `limit=50`（固定上限非翻页）；
- **自动刷新**：无（13.2 Q7 结论）；
- 验收所用演示数据经 ORM 写入本地开发库（字段形状与 Runtime 写路径一致），仅用于
  UI 渲染验收，seed 脚本为一次性临时文件，已删除。

## 6. 闭环结论

13.x Observation Vertical Slice 闭环：**13.1 只读 Read API → 13.2 表面拍板 → 13.3 实现
前审计 → 13.4 零后端改动的最小 Observation UI**。Observation 作为 Control Plane 的
只读 Operational Surface（13.2 Q10）已可用：看得到执行故事、点得进 Management、动不了手。
