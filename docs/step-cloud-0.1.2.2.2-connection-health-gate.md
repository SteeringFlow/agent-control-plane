# Cloud-0.1.2.2.2 —— Connection Health（Gate，不实现）

> 基线：Cloud-0.1.2.2（Connect Agent Architecture Gate，GO）→ **本 Gate（设计+机器护栏，无实现）**。
> 本步只裁决 **Health 在 Connection 上的位置**，并把裁决锁进机器化护栏。
> 对应 0.1.2.2 §9 的 **D3**（Runtime Health 派生诊断端点）。

Health 是第一个会把两件事明确呈现给 Cloud 用户的地方：

- **「配置存在」**（静态：Connection 已建、status=active、config 齐全）
- **「当前真的能连接 Agent」**（动态：现在这一刻 ACP runtime 可达）

本 Gate 先把这两者拆开，再决定 `GET /api/connections/{id}/health` 的本质。

---

## 0. 前置事实（代码审计）

| # | 文件 | 事实 | 对 Health 的含义 |
|---|---|---|---|
| F1 | `app/api/health.py`（14.1 冻结） | 进程 `/health` 只做 `SELECT 1`；**明确禁止**「逐步演变成复杂的系统状态机」；不检查 Agent/Channel/Connection 配置、不检查外部服务、不引入 metrics | 即时快照纪律的先例已存在 |
| F2 | `app/application/connections.py` L22-26 | 管理 Service **不做**「Agent token 能不能连接」之类的连接性测试（那是 Runtime/Adapter 职责） | 健康探针**不能**落在管理 Service |
| F3 | `app/adapters/agents/acp.py` | 凭据/配置**只来自** `Connection.config` / `connection.credential_ref`；`Agent.credential_ref` 不被消费 | 探针复用该路径（0.1.2.2 ①） |
| F4 | `app/core/config.py` | 刻意暂不引入 Redis/Celery/Kafka/Vault/K8s | 没有「持久化健康状态」所需的任何存储/调度基座 |
| F5 | `app/application/driver.py` | 唯一后台循环 = outbound 消息 pickup（`find_pending` + `poll_interval`）；返回 `healthy` 仅供自身退避 | 现有后台循环**不是** connection-health poller |
| F6 | `app/domain/connection.py` | `Connection` 只有 `status` 一个状态字段，无 `health*` | 持久化健康状态当前不存在 |

---

## 1. 三轴模型（确认 0.1.2.2，本次聚焦 health 轴）

```
Connection
 ├── status          active / inactive      [持久化 · 配置态 / 管理态]
 ├── health          derived (on-demand)     [不持久化 · 派生诊断]
 └── interaction outcome  processing / delivery  [在 Interaction，不在 Connection]
```

- `status`：Access Path 是否可用（active/inactive）——**唯一持久化**的 Connection 状态。
- `health`：现在这一刻能不能连上 Agent——**派生**，永不落在 Connection 上。
- `interaction outcome`：`processing_status` / `delivery_status` 在 `InteractionRecord`，**不在** Connection。

---

## 2. 关键裁决：`GET /api/connections/{id}/health` = 即时诊断探针（非持久化健康状态系统）

**裁决：前者（即时诊断）。** 理由由代码审计支撑，而非假定：

1. 先例已立（F1）：进程 `/health` 被**显式冻结**为「不进化成状态机」。Connection 级健康必须遵循同一纪律——
   它是「此刻能否连通」的一次性快照，不是会累积、会演进的状态机。
2. 架构无基座（F4）：持久化健康状态需要存储/调度，而 `core/config.py` 已刻意不引入 Redis/Celery/Kafka。
   在「无状态存储」约束下，健康只能即时派生，无法（也不应）落地。
3. 现存后台循环不是健康轮询（F5）：`driver.py` 只做 outbound pickup，与健康无关。

**因此端点契约（实现时锁定）：**

- 返回快照，不写库：`{connection_id, reachable: bool, checked_at, detail, probe}`；
  其中 `checked_at` 是**请求时刻**生成，绝不落库。
- 失败 = **Fail-Closed**：连不上 → `reachable=false` + `detail`（诊断信息），**不是** 500 炸；
  连接缺失 → 404（与 `GET /{id}` 同一语义）。
- **不**引入 `health` 状态字段、**不**建状态机、**不**调度后台轮询、**不**引入
  metrics/Prometheus/OpenTelemetry/tracing（与 `app/api/health.py` 14.1 冻结一致）。
- 它不是「Health Check 子系统」：当前 `connections.py` API 文档写的「不做 Health Check」
  意为**不建持久化健康状态机**；本裁决允许在其位置加**一个**即时诊断探针，二者不冲突。

---

## 3. 探针能力归属：Adapter，不在 Management Service

依据 F2 的硬约束（「连接性测试那是 Runtime/Adapter 的职责」）：

- 必须给 `ACPAdapter` 增加 `probe(connection: Connection) -> ProbeResult` 能力
  （只读 `Connection.config`：endpoint / bearer_token / runtime_id），做最小可达性检查，
  **Fail-Closed**（不可达/超时/401 → `reachable=false`）。
- `GET /api/connections/{id}/health` 调用该 adapter `probe`，**不**经
  `ConnectionApplicationService` 写库。
- `probe` 复用 `send` 的既有纪律（10.1.3 范围外一律不做）：不缓存连接、不重试、不刷新 token。

---

## 4. 凭据纪律复用（0.1.2.2 ①）

探针只读 `Connection.config` / `connection.credential_ref`；**绝不**读
`Agent.credential_ref`（那是占位，runtime 不消费）。健康是「连得通 config 指向的
runtime」的派生结论，与 Agent 上的引用无关。

---

## 5. 不引入新基础设施（F4）

本步**不**新增 Redis/Celery/Kafka/调度器/状态表/metrics。健康状态不落地，
故无需任何存储或调度——这反过来**证明**了「持久化健康状态系统」不是本架构的选项。

---

## 6. 用代码审计 + Gate 证明（不是假定）

**正证（现状已是即时诊断取向）：**
- 进程 `/health` 是 `SELECT 1` 即时快照，且文档显式拒绝「状态机」（F1）。
- 唯一后台循环 `driver.py` 是 outbound pickup，非 health poller（F5）。
- `Connection` 无 `health*` 字段（F6）。

**反证（护栏必须锁死，见 `test_cloud_0_1_2_2_2_connection_health_gate.py`）：**
- `Connection` 域/模型**无** `health*` 字段/列。
- `ConnectionApplicationService` **不**持久化 health（无 `health_status`/`last_health_check`/…）。
- 全 `app` **不** import redis/celery/kafka/apscheduler。
- `connections` API 模块即便未来加 health 路由，也**不得**写库 / 引用状态机（forward-looking 锁）。
- `ACPAdapter` 的探针依据**只来自** `Connection.config`。

---

## 7. 未来实现合同（实现该端点时的硬约束）

供后续 sub-step（如 0.1.2.2.2.1）逐条落实，本 Gate 仅裁决不实现：

1. 新增 `ConnectionHealthResponse` schema：
   `connection_id / reachable: bool / checked_at: datetime / detail: str|None / probe: str`
   ——**不得**含任何存储字段（无 `health_status` 列）。
2. `GET /api/connections/{id}/health`：
   404（不存在）/ 200（快照）；探针失败 → `reachable=false`，不抛 500。
3. 端点调用 `ACPAdapter.probe(connection)`（新增方法，只读 `Connection.config`/`credential_ref`，
   Fail-Closed），**不**经 `ConnectionApplicationService` 写库。
4. `probe` 复用 `_endpoint_and_timeout` / Tencent SSE 建连逻辑做最小可达性检查；
   不缓存连接、不重试、不刷新 token（与 `send` 同纪律）。
5. 不引入调度器/状态表/metrics；复用 `/health` 的「即时快照、不进化成状态机」纪律（14.1）。
6. 更新 `app/api/management/connections.py` L4 文档：「不做 Health Check」
   细化为「不做 Health Check **状态机 / 持久化健康子系统**；允许即时诊断探针」。

---

## 8. 验收（护栏）

| 类别 | 验收项 | 现状 |
|---|---|---|
| 三轴分离 | `status` 唯一持久化（`{active,inactive}`）；`health`/`interaction outcome` 不在 Connection | ✅ 锁进护栏 |
| 无持久健康状态 | `Connection` 域/模型无 `health*` 字段/列 | ✅ |
| 管理 Service 不持久化 health | `ConnectionApplicationService` 源码无 `health_status`/`last_health_check`/… | ✅ |
| 无健康存储/调度基座 | 全 app 不 import redis/celery/kafka/apscheduler | ✅ |
| 现存后台循环非 health poller | `driver.py` 无 connection-health 持久化 token | ✅ |
| 即时快照先例 | `app/api/health.py` 显式拒绝「状态机」 + `HealthResponse` 仅 `status` | ✅ |
| 探针依据 Connection.config | `acp.py` 凭据只来自 `Connection.config`/`credential_ref`，无 `agent.credential_ref` | ✅ |
| Forward-looking 锁 | 未来 `connections` API 加 health 路由不得写库/状态机 | ✅（静态锁） |

---

## 9. 与 0.1.2.2 的关系

兑现 0.1.2.2 §9 的 **D3**（Runtime Health 派生诊断端点）的**设计裁决**：
Health = 派生诊断、**非持久状态系统**。本 Gate 把「0.1.2.2 关注不可变绑定」推进到
「0.1.2.2.2 关注可变但不可持久化的运行时健康」。三轴（配置态/派生健康/交互结果）
至此在架构层面完全分离且由护栏锁死。
