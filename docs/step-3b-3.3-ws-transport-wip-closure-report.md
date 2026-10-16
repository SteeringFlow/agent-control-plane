# 3B-3.3 WIP Closure — WeCom WS Transport 并发测试

> Issue：`WIP-3B3-3-WS-TIMING`（原状态 OPEN，登记于
> `docs/architecture/implementation-package-1-baseline-evidence.md`）
> 状态：**CLOSED**
> 分两步：Closure A = 只读根因定位；Closure B = 最小修复 + 验收。
> **Production 0 改动 · Package 2 0 改动。**

---

## 0. 摘要

这个 issue 被登记为「时序 flaky」。定位后它的真实形态更严重：**它不是 flaky，
而是测试证据双向不可靠**——

| 方向 | 表现 | 危险程度 |
|---|---|---|
| 假红 | `_wait_terminal` 15s 超时 | 低：至少报警 |
| **假绿** | 测试通过，但写者停止后权威读数是 `processing` | **高：静默** |

实测：修复前 13 轮中 **6 轮终态写入丢失**——3 轮表现为假红，**3 轮表现为假绿**。
负对照下 e1 八轮里 **7 轮是假绿**（只有 1 轮超时报警）。**假绿是主导形态。**

---

## 1. 现象与最初误判

登记件写的是「失败用例在 `e1` / `c1` / `c3` 之间轮换——不是固定某一条逻辑被
破坏」。这句观察是对的，但推论（"时序问题"）把注意力引向了错误方向：

- 轮换说明**不是某条逻辑被破坏** → 排除语义回归；
- 但它也不说明"时序敏感" → 真正原因是**事务归属错乱**（见 §2）。

---

## 2. 根因（demonstrated synchronization gap）

### 2.1 逐跳追踪

```
fake WS 收到 aibot_respond_msg → reply_event.set()        ← 回复帧已发出
        ↓  （测试线程同时被唤醒，进入轮询）
rx 线程 _handle_frame → inbox
        ↓
exec 线程 _consume → on_frame → accept_message → worker.process
        ↓
Worker: claim_processing(独立事务, COMMIT) → runtime → record_success
        ↓
record_success（**一个**事务）：UPDATE interactions → succeeded
                              INSERT activities(session_created)
                              INSERT activities(interaction_completed)
                              commit()
```

注意：`reply_event` 在 **delivery** 阶段置位，而终态写入在其后的
`record_success`。所以「帧已收到」与「行已落终态」之间隔着**最后一次提交**。

### 2.2 根因

```python
# tests/conftest.py（修复前）
engine = create_engine("sqlite:///:memory:",
                       connect_args={"check_same_thread": False},
                       poolclass=StaticPool)      # ← 全进程唯一一条连接
```

`StaticPool` 只维护**一条** DBAPI 连接，`check_same_thread=False` 允许所有线程
共用它。于是在 SQLite 眼里，「测试线程」与「exec 线程」是**同一个连接上的同一个
事务**：

```
exec 线程（写）                          测试线程（读）
─────────────────────────────────       ────────────────────────────────
record_success: with uow() ← BEGIN
    UPDATE interactions → succeeded（未提交）
                                        _wait_terminal: with uow()
                                            SELECT → **脏读到 succeeded**
                                        __exit__ → session.close()
                                            → ROLLBACK ← 连带回滚写者在途事务
    INSERT session_created       ← 可能已被丢弃
    INSERT interaction_completed
    commit()                     ← 只提交回滚点之后的语句
```

`SqlAlchemyUnitOfWork.__exit__` 走的是 `close()`（`unit_of_work.py:121-124`），
而 `Session.close()` 会回滚仍开启的事务——这条事务属于**连接**，也就是属于写者。

### 2.3 三组观测证据（同一行 id=1）

| 观测 | 内容 |
|---|---|
| 脏读 | poll 在 0.036s 读到 `(1,'succeeded','delivered',session=1)`；写者停止后权威读数是 `processing/pending/NULL` |
| **半损（决定性）** | `activities` 有 3 条（`received`·`session_created`·`completed`，后两条 `session_id=1`），同一行的 `UPDATE` 却丢失、`sessions.external_session_id` 仍为 `NULL`。`record_success` 是**一个**事务——UPDATE 丢而后续 INSERT 活，只能由「事务中途被外部回滚」解释 |
| 双向 | 13 轮中 6 轮丢失：3 轮假红 + **3 轮假绿** |

---

## 3. 为什么只有 WS transport 测试命中

`WeComBotTransport` 是唯一把 `on_frame`（内含 Worker + 全七段 Runtime 与持久化）
放到**后台 exec 线程**、同时测试线程轮询 DB 的结构。仓库里只有两个文件使用它：

```
tests/test_3b_2_2_wecom_bot_websocket_transport.py
tests/test_3b_3_3_wecom_bot_group_chat.py
```

webhook 类测试把 Worker 跑在**测试线程自身**（同线程写+读）→ 不触发。
这正好解释了登记件里「失败用例在 `e1`/`c1`/`c3` 间轮换」。

### 与 Package 1 / Package 2 的关系

**都不是引入者。** pre-Package-2 基线（`e8b12e4` worktree）实测同测试 10 轮
7 通过 / 3 失败 → 缺陷早于 Package 2 存在。Package 2 新增的 authorization 阶段多
一次 UoW 读，改变了重叠窗口（30% → 约 50–60%）——**概率漂移，不是因果**。

---

## 4. 修复（最小、只碰测试基础设施）

| 文件 | 改动 | 性质 |
|---|---|---|
| `tests/conftest.py` | **+34**：新增 `isolated_db_engine`（文件型 SQLite，每条连接独立事务） | 纯新增 |
| `tests/test_3b_2_2_wecom_bot_websocket_transport.py` | **+20**：模块级 `db_engine` 覆写 | 纯新增 |
| `tests/test_3b_3_3_wecom_bot_group_chat.py` | **+16**：同上 | 纯新增（该文件是你的 WIP，未跟踪） |

**零删除、零生产代码改动、Package 2 零改动。**

### 明确没有做的事

```
❌ 不改 _wait_terminal()          —— 它是受害者，不是病因
❌ 不加 sleep()                   —— 不靠等待掩盖竞态
❌ 不放宽任何 timeout             —— 15s 预算原样保留
❌ 不加 retry                     —— 不重试掩盖丢失
❌ 不改 wecom_bot.py / Worker / Driver / UoW / Runtime / Delivery / Authorization
❌ 不加 xfail / skip
```

改的是「测试数据库的并发事务语义」，不动任何断言强度或等待预算。

---

## 5. 验收矩阵（实测）

| 环境 | 轮次 | 期望 | 实测 |
|---|---|---|---|
| 文件 SQLite / 隔离 e1 | 10 | 10/10 | **10/10** ✅ |
| 文件 SQLite / 隔离 c1 | 10 | 10/10 | **10/10** ✅ |
| 文件 SQLite / 隔离 c3 | 10 | 10/10 | **10/10** ✅ |
| WS 定向套件（两个文件全量） | ×3 | 0 failures | **85 passed ×3** ✅ |
| 本地全量回归 | full | 0 new failures | **2198 passed / 29 skipped / 0 failed** ✅ |
| PG 全量回归 | full | 0 new failures | **2225 passed / 2 skipped / 0 failed** ✅ |
| `git diff` → Package 2 | — | 0 changes | **0**（生产侧仅你的 3B-3.3 WIP，指纹未变）✅ |

**两条线的对比**：

| | 修复前 | 修复后 |
|---|---|---|
| 本地 | 2198 + **1 failed** | 2198 + **0 failed** |
| PG | 2224 + **1 failed** | **2225** + **0 failed** |

已登记的 flake 在两条线**同时消失**（PG 那个补位正是此前失败的那条）。

### 验证 B 的形式（不假绿，更重要的那一条）

判定链必须完整，缺一不可：

```
真实测试本体执行（其内部 poll 已观察到终态）
        ↓  transport.stop() —— 写者线程彻底结束
第三方观察者：独立 sqlite3 连接（绕开 ORM / 连接池 / 共享事务）
        ↓
权威读数仍须为终态（succeeded / delivered / session_id IS NOT NULL）
```

结果：`e1 / c1 / c3` × 10 轮 = **30/30 全干净**。

### 负对照：装置必须有牙齿

同一个装置、同一个真实测试本体，放回**修复前**的共享 `StaticPool` fixture：

| | 假绿 | 超时（假红） | 干净 |
|---|---|---|---|
| e1 × 8 | **7** | 1 | 0 |
| c1 × 8 | **4** | 1 | 3 |

若装置在旧 fixture 下一次都抓不到（0 检测），它就不足以作为验收依据——
负对照排除了这个可能。

---

## 6. ★ 架构结论：测试基础设施约束

> **测试数据库的并发事务语义不能比生产数据库弱。**

这不是「换 SQLite 后 flaky 消失」这类经验描述，而是一条**约束**。理由是 ACP
Runtime 的形态已经确定：

```
Worker + Driver + background execution + independent UoW
```

也就是说，**「后台线程写 + 另一处读」是产品的正常形态**，不是边缘情形。因此测试
基础设施**不能**用

```
single shared DB connection
```

来模拟它。一旦这么做，两侧会落入同一连接上的同一事务，同时产生：

```
false failure（读者 rollback 毁掉写者在途事务）
false success（读者脏读到写者未提交数据）   ← 更危险
```

**为什么后者更危险**：假红至少报警，假绿会让「跑通了」的证据建立在从未提交的数据
上——而且它不会失败，只会让结论静默失效。

### 约束的落地形式

1. **现状**：`tests/conftest.py::db_engine` 仍是 `:memory:` + `StaticPool`，仅适用于
   **同线程写+读**的测试（绝大多数现有测试）；
2. **新增**：`isolated_db_engine`（文件型 SQLite，一个 UoW = 一条连接 = 一个隔离
   事务）。任何把写者放到**后台线程**且测试线程读 DB 的测试必须使用它；
3. **不扩大适用范围**：没有把 `db_engine` 全面换成文件型——那会同时改变 2000+
   测试的既有前提，风险与收益不成比例。只在并发场景切换。

### 附带的信号：`:memory:` + StaticPool 下不存在独立观察者

对内存库而言，新增一条连接 = 新建一个**空库**（内容全部丢失），所以「第三方观察者」
根本无法存在——只能在同一连接上、等写者结束后读取。这本身也是该 fixture 不适合
并发测试的独立证据：一个连外部观察者都不存在的数据库，无法验证任何并发语义。

---

## 7. 遗留建议（不属本次修复，供裁决）

1. **是否加机器守卫**：禁止 WS transport 类测试请求共享 `db_engine`
   （例如 AST/源码扫描这两文件不得出现 `def db_engine(...)` 之外的取用方式）。
   成本很低，可防止未来新加的后台线程测试踩同一个坑——但需你确认是否值得再增一条
   守卫（每条守卫都有维护成本）。
2. **未来新增后台线程写 DB 的测试**：一律用 `isolated_db_engine`。
3. **生产侧无需任何动作**：生产引擎来自 `SessionLocal`（真实连接池），
   本就是每 UoW 一条连接；本缺陷不影响生产运行时行为。
4. **`test_3b_3_3_wecom_bot_group_chat.py` 仍未跟踪**：本次为它加的 fixture 覆写
   尚未入库；与 `wecom_bot.py` 一起收口时一并提交。
