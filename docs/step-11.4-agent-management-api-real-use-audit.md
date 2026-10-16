# Step 11.4 — Agent Management API Real-Use Audit

> **真实使用审计步，生产代码 0 行改动。**
> 交付物 = 本报告（用真实 HTTP 请求把四个端点跑一遍，并回答产品问题）。
> 不写 FastAPI / 不写 Pydantic / 不动 Application / Domain / Persistence / Schema。
> 与 11.1（看架构）和 11.3（落地实现）不同——本步**用真实请求验证行为**，
> 并回答：「Agent Management API 是否已足够支撑一个最小 UI？」

---

## 0. 方法与范围

- **被测对象**：`app.api.management.agents` 的 4 个端点（11.2 §0 冻结面），
  经 `app.application.agents.AgentApplicationService` → UoW → 真实 SQLite，
  经 `AgentResponse` 序列化。**与 11.3 实现的代码路径完全一致**。
- **探针性质**：真实 HTTP 请求。先以真实 `uvicorn` 进程（`app.main:app`）在
  `127.0.0.1` 起服务、用 `curl`/HTTP 实测（health 200、生命周期 A 段已在真实
  TCP 下跑通、且 `_dbtest` 独立证明「DB 置 error 后 GET 真实返回 error」）；
  因长驻 server 与脚本的跨进程 SQLite 可见性偶发抖动，最终确定性矩阵改以同
  一 `app.main:app` + 真实 SQLite 文件（标准 `create_all` 测试桩）经 ASGI 真实
  HTTP 请求跑通——**代码路径与 uvicorn 完全一致，仅差一个 TCP socket**。
- **零生产改动证据**：`git diff` 无 `app/` 改动；全量回归 `994 passed`
  （与 11.3 收尾数一致，未增未减）。
- 探针脚本 `_audit_probe.py` / `_audit_bootstrap.py` / `_dbtest.py` 为一次性
  审计工具，**不进 `app/`**，审计后清理（见 §6）。

---

## 1. 完整生命周期（Create → Get → List → Patch → Get → List）

真实请求与响应（节选）：

```
POST /api/agents  (credential_ref="vault://agent/acp-prod")
  => 201  {id, name, type:acp, status:active, endpoint, config, created_at, updated_at}
          （响应中无 credential_ref）

GET  /api/agents/1        => 200  单条可见，无 credential_ref
GET  /api/agents          => 200  {items:[...], total:1}，每条无 credential_ref

PATCH /api/agents/1  (rename→acp-prod-2, status→inactive, rotate credential_ref)
  => 200  字段全部更新，响应仍无 credential_ref

GET  /api/agents/1        => 200  变更已持久化（name/status/endpoint/config 一致）
GET  /api/agents          => 200  list 已反映更新
```

**生命周期闭环 ✅**：创建即可读、可列、可改、改后可见、列表同步。

### 1.1 `credential_ref`「引用入、引用不出」——直接 DB 取证

绕过 API 直读 DB 行（`SELECT ... FROM agents`）：

```
{"id":1, "name":"acp-prod-2", "status":"inactive", ...,
 "credential_ref":"vault://agent/acp-prod-2-rotated"}
```

- 写入有效（Create 写入、Patch 轮转覆盖均落库）；
- **所有 Get / List 响应字段面均不含 `credential_ref`**（逐请求断言，0 泄漏）；
- PATCH 语义（直读 DB 验证）：
  - 缺省（字段不在 body）→ 保留原值；
  - 显式 `null` → **不清除**，保留原值（先置 `vault://victim`，再 `null`/改名，DB 仍为 `vault://victim`）；
  - 非空串 → 覆写。

---

## 2. 关键安全 / 语义边界矩阵

| # | 边界 | 真实请求 | 结果 | 判定 |
| --- | --- | --- | --- | --- |
| 1 | 完整生命周期 | 见 §1 | 全绿 | ✅ |
| 2 | `credential_ref` Create 可写 | POST 带 ref | 201 + 落库 | ✅ |
| 3 | `credential_ref` Get/List 永不泄露 | 逐响应扫描 | 0 泄漏 | ✅ |
| 4 | `credential_ref` Patch 轮转/缺省/null | PATCH | 覆写 / 不变 / 不变 | ✅ |
| 5 | `status` active / inactive 接受 | POST&PATCH | 201 / 200 | ✅ |
| 6 | `status=error` 输入拒绝 | POST&PATCH `error` | **422** | ✅ |
| 7 | `error` 读取可见（运行态信号） | DB 置 error 后 GET | 200 原样返回 `error` | ✅ |
| 8 | `error → active` 管理覆盖 | PATCH `active` | 200 清障 | ✅ |
| 9 | `type=acp` 合法 | POST&PATCH | 201 / 200 | ✅ |
| 10 | `type=openai` 越界 | POST&PATCH | **422** | ✅ |
| 11 | `name` 重复 Create | 两次同名 | **409** | ✅ |
| 12 | `name` 改名撞已有 | PATCH rename | **409** | ✅ |
| 13 | `endpoint` 非法（not-a-url / ftp://x） | POST&PATCH | **422** | ✅ |
| 14 | `name` 空串 | POST | **422** | ✅ |
| 15 | `config` 非 dict | POST | **422** | ✅ |
| 16 | `credential_ref` 空串/纯空白 | POST&PATCH | **422** | ✅ |
| 17 | Get 不存在 id | GET /99999 | **404** | ✅ |
| 18 | Patch 不存在 id | PATCH /99999 | **404** | ✅ |
| 19 | openapi 端点面 | GET /openapi.json | 恰 `POST/GET /api/agents`、`GET/PATCH /api/agents/{id}`；**无 DELETE** | ✅ |

全部 19 项通过，无 `credential_ref` 泄漏（0 LEAK）。

---

## 3. `status` 双轴结论（对 11.2 §1.2 的实测确认）

- 管理态 `active`/`inactive`：API 输入白名单仅这两个，实测 201/200。
- 运行态 `error`：**API 永不接受为输入**（422），但**读路径原样返回**（不裁切），
  管理员可 `PATCH status=active` 手动清障（唯一的管理覆盖运行态）。
- 行为与 11.2 裁决、11.3 实现三方一致，**无实测偏差**。

---

## 4. 错误码映射（对 11.2 §6）

| Application 错误 | HTTP | 实测 |
| --- | --- | --- |
| `DuplicateAgentError` | 409 | ✅（Create / Patch 改名两条路径） |
| `AgentNotFoundError` | 404 | ✅（Get / Patch 缺失 id） |
| `InvalidAgentInputError` | 422 | ✅（空字段 / type 越界 / status=error / 非法 URL / 空白 credential_ref / config 非 dict） |

映射唯一且正确。

---

## 5. 产品问题：API 是否已足够支撑一个最小 UI？

### 5.1 最小 UI 的核心需求 ↔ API 覆盖

| 最小 UI 需要 | 对应端点 | 覆盖 |
| --- | --- | --- |
| 列出 Agent（表格） | `GET /api/agents` | ✅ |
| 新建 Agent（表单） | `POST /api/agents` | ✅ |
| 查看单个 Agent（详情） | `GET /api/agents/{id}` | ✅ |
| 编辑 Agent（表单） | `PATCH /api/agents/{id}` | ✅ |
| 「删除」→ 软下线 | `PATCH status=inactive` | ✅（设计内，非硬删） |
| 校验反馈（重名/非法） | 409 / 422 + `detail` | ✅ |
| 运行态可见（error 告警） | GET 原样返回 `error` | ✅ |
| 重新启用 | `PATCH status=active` | ✅ |

**结论：四个 CRUD 形态操作齐全、错误语义完整、安全属性（credential 不出）
与状态机（管理态/运行态）均经真实请求验证正确。**

### 5.2 真实的产品语义考量（非阻塞，带入 11.5 / 后续）

这些不是「API 缺陷」，是**已知的产品语义权衡**，最小 UI 可避让或适配：

1. **`credential_ref` 写-only、读不出**（设计性安全属性，非 bug）：
   编辑表单无法回显当前值，只能以「留空 = 保留当前；填写 = 轮转」的语义呈现
   （已实测 `null`/缺省不清除）。**最小 UI 需在表单注明此语义**，不影响数据完整性。
2. **`config` 为不透明自由 dict**：最小 UI 只能以原始 JSON 编辑；结构化表单
   （protocol / timeout_s 等）留未来，非最小 UI 必需。
3. **无分页 / 过滤**（11.2 §5.4 明确 MVP 不做）：少量 Agent 场景可接受。
4. **`name` 唯一性仅应用层守卫（TOCTOU，无 DB UNIQUE）**：单管理员最小 UI
   风险低；触发条件 = 并发真实化时再补 migration（11.3 §4 / 10.9 已记，非阻塞）。
5. **无硬删**（设计内软下线）：UI 以「启用 / 停用」代替「删除」即可。
6. **无「测试连接 / 健康」端点、无「哪些 Connection 引用此 Agent」视图**：
   辅助信息，最小 UI 不依赖。

### 5.3 裁决

> **Yes —— Agent Management API 已足够支撑一个最小 UI。11.5 可正式进入
> Agent Management UI（11.1 ↔ 10.9 G10 的「真实消费者」升级 NOW 落点成立）。**
>
> 理由：四端点生命周期、错误码、安全属性、状态双轴全部经真实 HTTP 验证正确；
> 上列 6 项均为**已知权衡**而非**阻塞性缺陷**，均可在 UI 层避让或留作后续切片。
> 不建议在进 UI 前再动 API——那样会偏离「先验证消费者再返修」的节奏（10.9）。

---

## 6. 交付物与清理

- 本报告（docs/step-11.4-...md）= 唯一交付物；**生产代码 0 行**。
- 全量回归：`994 passed`（与 11.3 一致，无新增无回退）。
- 一次性审计脚本与临时 DB / 日志已清理，不留在仓库：
  `_audit_bootstrap.py`、`_audit_probe.py`、`_dbtest.py`、
  `audit_step11_4*.db`、`audit_uvicorn*.log/.err`、`_audit_transcript.txt`。
- 后续步骤：11.5 Agent Management UI（基于本报告判定进入）。
