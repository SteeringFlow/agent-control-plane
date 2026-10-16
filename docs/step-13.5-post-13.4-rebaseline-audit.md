# Step 13.5 — Post-13.4 Architecture Rebaseline Audit

> **纯审计步，生产代码改动：0 行。**
> 回答一个核心问题：完成 Async Runtime（12.9.4）+ Observation（13.x）之后，
> ACP 距离「可对外验证的 MVP」还差什么。
>
> 原则：不再堆能力；剩余事项重新分成
> **A. MVP 必须完成 / B. MVP 可以暂缓 / C. 纯技术债暂不处理** 三类。
> 不重新设计 12.9.4（FROZEN 不动）。

---

## 0. 审计输入（本会话核实的事实）

- Runtime 链路代码：`worker.py`（`claim_processing` CAS → attempt fence →
  `record_success/failure`）、`driver.py`（DISCOVER / TRIGGER / RECOVER 三相循环）、
  `channel_inbound.py`（`create_pending` ACK 边界，绝不执行 Runtime）；
- Observation：13.1 三端点 + 13.4 UI，已通过真浏览器验收（0 写请求）；
- Management：5 个管理页（agents 11.5 / channels 11.9 / connections 11.12 /
  users+identities 12.4）；
- **Authentication：0 实现**（全局搜索 JWT/Bearer/login 仅命中 Adapter 出站凭据，
  ACP 自身无任何鉴权）；
- **Legacy `PersistingCoordinator.execute()`：生产调用 = 0**（生产只用
  claim/record/create/find/reclaim 六个 phase API；`.execute(inbound)` 仅 7 个测试文件
  约 23 处调用，均为 9.x/10.x 时代验收）；
- 部署基线：`docker-compose.yml` 为开发形态（启动时 pip install、--reload、SQLite、
  单 service）；`/health` 存活探针已有（不查依赖）；`_deploy/` 含 12.8.x 真实 WeCom
  冒烟所用部署脚本（真库 `/data/agent_control_plane.db`）；
- **CI：不存在**（无 `.github/`）；
- 测试面：`backend/tests/` 101 个测试文件（含 12.9.4.17 可靠性、12.9.4.13 E2E 闭环、
  12.8.6 真实 wire 冒烟、13.1 只读验证）。

---

## 1. 逐题拍板

### Q1 — Runtime 是否达到 MVP 可靠性要求？→ **达标（FROZEN，不动）**

链路 `Inbound → PENDING → Worker → CAS Claim → Attempt Fence → Runtime → Outcome →
Delivery → Driver Recovery` 每一段都有真实归属与验收：

| 故障场景 | 已有裁决 | 证据 |
| --- | --- | --- |
| 重复请求 | UNIQUE 身份物化（10.5.2）+ state-aware duplicate ack（12.9.4.9） | 不会产生双 PENDING |
| BackgroundTask 丢失 | Driver Trigger 2 DISCOVER 排空积压 | 12.9.4.18A 双触发声明 |
| Worker crash | `reclaim_stale_processing` 孤儿回收 + stale threshold | 12.9.4.10/12.9.4.7 |
| 多 Driver 竞争 | 执行权唯一由 CAS claim 裁决，重复触发只得 claim=False | Driver docstring 明示 |
| delivery failure | 落为 outcome + Evidence（`succeeded × failed` 合法组合） | 13.1 S3 / 13.4 实测 |
| late stale Worker | Attempt Fence（`processing_started_at` 等式围栏）旧 outcome 拒写 | 12.9.4.11/12.9.4.12 |

12.9.4.17（可靠性）+ 12.9.4.13（E2E 闭环）已验证。**结论：支撑 MVP 足够；本步起
Runtime 整体 FROZEN，任何可靠性增强必须先有真实故障案例再立项。**

### Q2 — Observation 是否达到 MVP 可运营要求？→ **达标（FROZEN，不动）**

13.4 实测已覆盖 list / detail / timeline / 双状态 / 失败三元组 / payload 快照 /
session-channel 引用 / deep-link 全链。Operator 能回答「这次请求发生了什么」。

剩余均为体验项而非阻塞项（归 B 类 fast-follow）：
- 列表无按状态/channel 过滤（bounded limit 模型下可先靠「最新在前 + 刷新」）；
- deep-link 精确定位（`?id=` 解析）未实现；
- 无「只看失败」的一键视图（等真实 Operator 反馈再做）。

**不存在真正阻塞 Operator 的缺口。**

### Q3 — Management 是否达到可用程度？→ **达标（可用，冻结在当前功能面）**

第一次接触的 Operator 沿「Channel → Agent → Connection → User → Identity」配置链
可以完成全部配置：每页均有字段级提示（type 约束、credential 写-only、config 不透明
JSON 等），Connection 响应只带 ID 由 UI 解耦解析（11.11）。

判断依据：13.4 验收时 Operator 侧的完整故事（配置 → 发请求 → 观察）已在真实数据上
走通。缺口不是功能而是**引导**（首次上手顺序无文档化 walk-through）——归 B 类
（部署文档的一部分），不新增 Management 功能。

### Q4 — Authentication 是否已从「未来问题」变成 MVP 必须问题？→ **拆开拍板**

按产品形态判断，**不加 JWT 不是第一个外部 Pilot 的阻塞**，但**暴露面管理是**：

| 形态 | 认证要求 |
| --- | --- |
| Local / Demo | 无需（本机） |
| **Single Operator Pilot（当前目标）** | **无需应用层 Auth；必须网络层隔离**（反向代理 Basic Auth / IP allowlist / 内网部署）。原因：webhook 天然公网，但 Management + Observation 含 payload（对话原文），绝不能裸公网 |
| Private Deployment | 最小 Bearer Token（fast-follow） |
| Enterprise SaaS | 完整 JWT / 多租户（Future，届时重设计） |

**拍板：MVP 不引入应用层 Authentication（不加 JWT）；「Pilot 暴露面隔离」作为部署
基线的一部分进入 A 类（它属于 Deployment，不属于 Auth 系统）。** 理由：认证是
Enterprise SaaS 的问题，被提前到此只会推迟外部验证；而裸公网暴露管理面是当下真实
风险，解法是网络层一行配置，不是一套权限模型。

### Q5 — Retry / Replay 是不是 MVP 必需能力？→ **不是（B 类 fast-follow）**

MVP 的最小闭环是**「能够观察失败」**，不是「能够从失败恢复」：

- 对外验证要回答的产品问题是「请求进来后发生了什么、结果对不对」——Observation 已
  完整回答；失败后 Operator 当前的人工恢复路径（修配置 + 用户重发一条消息）在
  Pilot 规模（单 Operator、低频）完全可行；
- Retry 会强制开启**控制动作面**（新面、幂等重放语义、与 Attempt Fence / 孤儿回收
  的交互），是 10.7.1 以来的最大未动面积，风险远大于收益；
- `retryable` 已作为事实提示透出（13.2 Q4），未来 Retry 落点清晰。

**拍板：Retry / Replay 不进 MVP；在 Pilot 拿到真实失败处理反馈后，作为独立的
Control Action Surface 立项（绝不挂在 Observation 上）。**

### Q6 — TOCTOU Duplicate Convergence 的优先级 → **C 类（fast-follow 技术债）**

`lookup → create → UNIQUE(channel_id, external_interaction_id)` 的并发窗口，最坏
情形是并发重复 webhook 中有一条撞 UNIQUE 得 5xx，WeCom 按协议重试后命中
state-aware duplicate ack 正确收敛——**系统自愈，无双重执行、无双 PENDING**。

- 身份正确性由 DB UNIQUE 硬保证（10.5.2），极窄窗口影响的只是**一次 5xx 重试**，
  不是 correctness；
- 修复它（INSERT ... ON CONFLICT / 原子 upsert）会触碰 12.9.4.9 已验证的
  Duplicate Acceptance 边界，回归成本 > 收益；
- **不要因为「理论上不完美」破坏已验证边界。** 拍板：维持 C 类，等出现真实
  高频重复场景再评估。

### Q7 — Legacy `PersistingCoordinator.execute()` / inner → **C 类（延后清理）**

- 生产 caller = 0（本步核实）；
- 测试 caller：7 个测试文件约 23 处 `.execute(inbound)`（9.x/10.x 时代验收）+
  多处 `inner=` 构造参数；
- 删除需改动上述测试与构造签名，属**代码卫生，不是产品风险**；
- 执行路径已唯一（Worker），无「双执行来源」歧义（12.9.3.6 §11 已裁决）。

**拍板：延后。** 在下次大规模触碰 `interaction_recording.py` 时顺手清理，或作为
「测试口径现代化」独立小步，不单独立项。

### Q8 — Deployment Baseline → **拆开拍板：这是唯一真正的 MVP Blocker**

区分「能够部署」（✅ 已具备）与「企业规模生产部署」（❌ 且不需要）：

| 项 | 现状 | Pilot 必须？ |
| --- | --- | --- |
| 运行链 | FastAPI + SQLite + resident Driver + BackgroundTasks | ✅ 够用（单机单 Operator，SQLite 是 MVP 的真实数据库，5.2 裁决） |
| `/health` | 存活探针已有 | ✅ |
| Docker | 有 compose，但为开发形态（启动装依赖、--reload、DB 无持久卷） | ⚠️ 需加固 |
| 真实部署经验 | `_deploy/` 已有 12.8.x 真机 WeCom 冒烟脚本 | ✅（非零起点） |
| 部署文档 / Operator walk-through | 无 | ⚠️ 缺 |
| PostgreSQL / Alembic 迁移 | 无 | ❌ 不需要 |
| CI | 无 | ❌ 不是 Pilot 阻塞（本地 pytest 面已 101 文件） |
| 暴露面隔离 | 无 | ⚠️ 必须（见 Q4） |

**拍板：进入 A 类的唯一项目 = 「Pilot Deployment Hardening」**：
可复现的单机部署包（compose 加固：持久卷、去 --reload、依赖预装）+ 部署 /
首次配置 walk-through 文档 + webhook 公网配置与网络层隔离说明。
**明确排除**：PostgreSQL、迁移工具链、CI、监控——全部不属于「第一个外部 Pilot」。

### Q9 — 第二 Channel / 第二 Agent 是否现在进入？→ **不进入（C 类）**

- 接口扩展性已由 Adapter Registry 保证（Channel type 自由字符串、Agent type 受控
  集合，11.8/11.2 纪律：出现第二个真实 Adapter 再重新审计）；
- 「接口具备扩展性 ≠ MVP 必须实现第二个 Adapter」：第一个外部用户闭环不因缺第二
  Adapter 受阻；
- 引入第二 Adapter 的唯一正当理由是**真实 Pilot 用户需求**（demand-driven），提前
  做是 speculative generality。

**拍板：不进 MVP。首个 Pilot 用户表达真实渠道/模型需求时，按对应 Vertical Slice
纪律立项。**

### Q10（最关键）— MVP「第一个外部用户闭环」缺哪一环？→ **闭环已通，缺的只是部署外壳**

从产品倒推逐环核对：

```
真实用户        ✅（WeCom 侧真实用户）
接入 Channel    ✅ WeCom 入站 wire（12.8.5/12.8.6 真实分类冒烟）
连接 Agent      ✅ Management 五页可完成全链配置（Q3）
发送请求        ✅ webhook → ACK（12.9.4.6）
Agent 执行      ✅ 真 ACPAdapter 六段 Runtime（12.9.4.13 E2E 闭环）
得到结果        ✅ response_payload 快照（10.7.2）
Delivery        ✅ WeCom 出站（10.2.2）+ 失败可观测（succeeded×failed）
Observation     ✅ 13.1 API + 13.4 UI（0 写请求验收通过）
Operator 理解   ✅ 双状态 / 失败三元组 / 时间线 / 卡死提示
```

**九环全部有代码、有验收、有真实数据证据。唯一没有的是「把这套已验证的东西放到
一个 Operator 可以安全访问的地方」——即 Q8 的 Pilot Deployment Hardening。**

**最终拍板：下一阶段不再做任何基础设施；完成部署加固后直接进入 Pilot /
External Traction。**

---

## 2. 优先级矩阵

| 项目 | 当前状态 | MVP 必须？ | 下一步 |
| --- | --- | --- | --- |
| Async Runtime | FROZEN（12.9.4.13/17 验收） | ✅ | 不动 |
| Observation | FROZEN（13.1–13.4 闭环） | ✅ | 不动 |
| Management | 可用（5 页全链配置） | ✅ | 不加功能；walk-through 并入部署文档 |
| Authentication | 0 实现 | ❌（网络层隔离 ✅） | Pilot 用网络层隔离；Bearer Token 归 fast-follow；JWT 归 Enterprise |
| Retry / Replay | 不存在（`retryable` 仅事实提示） | ❌ | Pilot 反馈后独立 Control Action Surface 立项 |
| TOCTOU Duplicate | UNIQUE 自愈 + 5xx 重试收敛 | ❌ | 技术债延后，不动已验证边界 |
| Legacy cleanup | 生产 caller = 0 | ❌ | 触碰该文件时顺手清理 |
| Deployment | 开发形态 compose + `_deploy/` 冒烟经验 | ✅（**唯一 Blocker**） | **Pilot Deployment Hardening（下一实施步）** |
| Second Channel | 仅 WeCom；接口可扩展 | ❌ | demand-driven，Pilot 用户提出再立项 |
| Second Agent | 仅 ACP；接口可扩展 | ❌ | demand-driven，同上 |

## 3. 分类汇总

- **A. MVP 必须完成（仅 1 项）**：Pilot Deployment Hardening —— compose 加固
  （持久卷 / 去 --reload / 依赖预装）+ 部署与首次配置 walk-through + webhook 公网
  配置与网络层隔离说明；
- **B. MVP 可以暂缓（fast-follow，等 Pilot 反馈）**：列表过滤/「只看失败」视图、
  deep-link 精确定位、最小 Bearer Token、Retry/Replay 控制动作面、CI；
- **C. 纯技术债 / 暂不处理**：TOCTOU 并发窗口、Legacy `PersistingCoordinator.execute()`
  / inner 清理、PostgreSQL / 迁移工具链、第二 Channel / 第二 Agent、监控 / 企业级
  生产化。

## 4. 最终拍板

```
             Post-13.4
          Rebaseline Audit
                  │
       ┌──────────┼──────────────────┐
       ↓          ↓                  ↓
   MVP Blocker  Fast-follow       Future
       │        (过滤/Bearer/     (Retry 面/JWT/
       │         CI/deep-link)    PG/SaaS 化)
       ↓
   Pilot Deployment Hardening
   （compose 加固 + 部署 walk-through + 暴露面隔离）
       │
       ↓
   下一真正实施步骤 = Step 14.1
   Pilot Deployment Baseline（部署外壳，零新架构语义）
       │
       ↓
   之后不再堆基础设施 → Pilot / External Traction
```

> 一句话：**产品闭环九环已全通；ACP 距离「可对外验证的 MVP」只差一个可复现、
> 暴露面受控的部署外壳。除此之外的一切，都等第一个外部用户的真实反馈。**
