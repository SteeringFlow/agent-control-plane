# Package 2 / Step 6 — Full Regression（最终回归收口）

> 状态：**完成**。
> 本步**零文件改动**（纯验证）——它不产出代码，只产出结论，因此没有独立 commit；
> 本报告与 Step 7 同批进入 Package 2 documentation closure。
> **Package 2 production semantics 在本步一字未动。**

---

## 1. 本步目的

Step 1–5 把授权能力长齐（持久化 / Runtime / 管理面 / 守卫 / 数据库证据）。Step 6
不是继续开发，而是**收口回归**：用一次完整回归证明「前面五步合起来没有把既有系统弄坏」，
并明确冻结两条边界——

```
不再修改任何 Package 2 production semantics
不再为了变绿而放宽任何断言
```

---

## 2. 六项检查结果

| # | 检查 | 结果 |
|---|---|---|
| 1 | **本地完整 pytest**（无 PG） | **2198 passed / 29 skipped / 0 failed** ✅ |
| 2 | **PG 完整 pytest** | **2225 passed / 2 skipped / 0 failed** ✅（见 §4 的时序说明） |
| 3 | **架构守卫** | **368 passed / 6 skipped / 0 failed** ✅ |
| 4 | **migration head** | 迁移目录 `0007` = 真实 PG `0007`；`closure_evidence` **A/B/C → OVERALL PASS** ✅ |
| 5 | **working-tree boundary** | Package 2 生产代码 **0 改动**；唯一修改是 3B-3.3 WIP 的 `wecom_bot.py` ✅ |
| 6 | **唯一失败确认** | 唯一失败 = 已登记 `WIP-3B3-3-WS-TIMING`；**已独立闭环**，见 §5 ✅ |

检查 3 的 368 项 = g13 的四个权威守卫文件 + 14 个边界/收口守卫文件（共 18 文件）。

**收集总量一致性**：Step 6 时点两条线各自 `2227` 条（本地 `2198+29`、PG `2225+2`）。

---

## 3. 一个必须先记下来的方法论陷阱：PG 线**只能**设 `CLOUD_TEST_DATABASE_URL`

第一次 PG 全量跑出 **9 failed**，全部集中在默认配置类测试：

```
tests/test_database.py · tests/test_persistence_smoke.py
tests/test_settings.py · tests/test_step14_2_3_c2_configuration_alignment.py
```

原因不是回归，而是**我的环境**：我在同一个 shell 里既导出了 PG 门控变量，**又导出了
`DATABASE_URL`**，而后者会覆盖应用的默认配置（这些测试正是在断言默认值：

「sqlite 文件」「首次操作前不建库文件」等）。PG 门控只读 `CLOUD_TEST_DATABASE_URL`；
测试自身会在 alembic 调用前后临时设置/还原 `DATABASE_URL`。

修正后（只设门控变量）即 **2225 / 2 skipped / 0 failed**。

> **结论**：PG 全量回归的调用形态是
> `CLOUD_TEST_DATABASE_URL=... python -m pytest`，**不要**附带 `DATABASE_URL`。
> 否则会稳定撞出这 9 个假失败——它们是环境产物，与代码无关。

---

## 4. 唯一失败：`WIP-3B3-3-WS-TIMING`（已独立闭环）

### 4.1 当时的观测

| 线 | 轮次 | 结果 | 失败者 |
|---|---|---|---|
| PG | 主跑 | 2224 passed / 2 skipped / **1 failed** | `test_3b_3_3::test_c1_group_session_matrix_case_a_reuse` |
| 本地 | 默认 | 2198 passed / 29 skipped / 0 failed | — |
| 本地 | `-p no:randomly` | 2197 passed / 29 skipped / **1 failed** | `test_3b_2_2::test_e1_fake_wecom_ws_end_to_end` |

同一套代码、同一批测试，**失败者在 `e1` / `c1` 之间轮换**——这与既有登记件
（`implementation-package-1-baseline-evidence.md`：`WIP-3B3-3-WS-TIMING`，状态 OPEN，
「失败用例在 e1 / c1 / c3 之间轮换」）**逐字吻合**。

### 4.2 三条独立证据链（说明它不是 Package 2 引入）

1. **登记吻合**：失败身份与轮换集合与已登记 issue 完全一致；
2. **pre-Package-2 复现**：在 `e8b12e4`（Package 2 之前）的独立 worktree 上跑同一测试
   10 轮 → **7 通过 / 3 失败**；缺陷早于 Package 2 存在；
3. **隔离非确定性**：`e1` 隔离 10 次 4/6、`c1` 隔离 5 次 4/1、本地默认形态 0 失败而
   `-p no:randomly` 1 失败——确定性回归不会有这种表现。

### 4.3 闭环（独立于 Package 2 的专项）

```
Closure A（只读定位）  真因 = 测试基础设施的并发事务语义弱于生产
                      测试数据库共用一条 SQLite 连接（conftest `:memory:` + StaticPool），
                      WS transport 的 exec 线程与测试线程落入同一事务：
                        读者脏读写者未提交数据 → 假绿
                        读者 Session.close() 回滚写者在途 UPDATE → 假红
                      实测 13 轮中 6 轮丢失终态写入（3 假红 + 3 假绿）
   ↓
Closure B（最小修复）  conftest 新增 `isolated_db_engine`（文件型 SQLite，
                      一个 UoW = 一条连接 = 一个隔离事务）+ 两个 WS 模块覆写 `db_engine`；
                      **未改 `_wait_terminal`**、无 sleep / 放宽 timeout / 重试 / xfail
   ↓
闭环后的数字          本地 2198 passed / 29 skipped / **0 failed**
                      PG   2225 passed /  2 skipped / **0 failed**
```

Closure B 附带的**可证伪验证**（防止「装置空转」）：

```
e1 / c1 / c3 各连续 10 轮           30/30 干净
WS 定向套件                          85 passed × 3 轮
不假绿检查（真实测试本体 + 独立 sqlite3 权威读数）  30/30 干净
负对照（放回旧 fixture）             e1 8/8 有缺陷（7 假绿 + 1 超时）；c1 5/8
```

完整记录见 `docs/step-3b-3.3-ws-transport-wip-closure-report.md`。

### 4.4 对 Package 2 的结论

```
Package 2 不是引入者        pre-Package 2 已可复现（30%）
Package 2 的实际影响        概率漂移：authorization 阶段多一次 UoW 读，
                            改变了重叠窗口（约 30% → 约 50–60%）
Package 2 引入的新 failure  0
```

---

## 5. 边界确认（本步最重要的冻结项）

| 项 | 事实 |
|---|---|
| Package 2 production 代码改动 | **0**（`git diff -- backend/app` 为空） |
| 工作树唯一修改 | `backend/app/adapters/channels/wecom_bot.py` —— **3B-3.3 的 WIP**，与本步无关 |
| WIP diff 指纹 | `99fca9e843bcd598b1cbc6396a1bf2d4e05ec4a2`（收口前后未变） |
| 断言强度 | 未放宽；**未加** `xfail` / `skip` |
| 等待预算 | 未提高（`_wait_terminal` 的 15s 原样保留，Closure B 亦未动它） |
| 机器守卫 | 未新增 |

---

## 6. 迁移头与数据库证据（同一次 PG 运行）

```
alembic heads（迁移目录）      0007 (head)
alembic current（真实 PG）     0007 (head)
closure_evidence.py            A. Migration : PASS
                               B. SET LOCAL : PASS
                               C. RLS       : PASS
                               OVERALL      : PASS
```

证据面涵盖：迁移链拓扑、逐版本 DDL、`tenant_id` 列 + FK + 索引、四元组 UNIQUE、
10 张表的 ENABLE+FORCE RLS、11 条策略、事务级 `SET LOCAL` 清理、非超级用户角色的
跨租户隔离（含 unset 上下文 fail-closed 读到 0 行）。

---

## 7. 数字口径说明（避免后续误读）

| 现象 | 说明 |
|---|---|
| 本地 `29 skipped` | Step 6 时点。Step 7 提交后新增 4 条 env 门控 skip → **33**（与 PG 无关，无凭据即 skip） |
| PG `2 skipped` | **真实外部服务**门控（`TENCENT_ACP_URL` / `TENCENT_ACP_TOKEN` / `TENCENT_RUNTIME_ID`），非 PG 相关 |
| PG 下 9 个「配置类」失败 | 只会在**误设 `DATABASE_URL`** 时出现（§3），不是回归 |
| Step 5 的 `16` 项 PG 测试 | PG-gated；无 `CLOUD_TEST_DATABASE_URL` 时整体计入 skipped |

---

## 8. 路线图定位

```
Step 0  Fixture Classification      ✅
Step 1  Authorization Persistence   ✅
Step 2  Runtime Authorization       ✅
Step 3  Grant Management API        ✅
Step 4  Guards + Contract Closure   ✅
Step 5  PostgreSQL Closure          ✅
Step 6  Full Regression             ✅ 本步（回归收口 + 唯一失败闭环）
Step 7  Real WeCom E2E              ✅ Package 2 最终验收
```

Package 2 的开发与验收到此结束：六项检查全绿，唯一失败有独立闭环，生产语义零改动。
