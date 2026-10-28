# Implementation Package 1 — Baseline Evidence（pre-Package-1 A/B）

> 目的：消除 **baseline uncertainty**，让后续每个 Gate（Authorization change → 2112+N tests）
> 不再重复讨论同一个 WeCom timing failure。
>
> 方法：`git worktree add --detach` 于 `f43689a^`，**不动当前工作树**。
> 提交：Package 1 = `f43689a`；基线 = `00344f9`（`f43689a^`）。
> 日期：2026-09-18

---

## 0. 结论

```
该失败在 pre-Package-1 基线上同样出现（且更稳定）
        ↓
判定：既有 3B-3.3 群聊 WIP / timing issue，**与 Package 1 无关**
```

**正式登记为既有问题**（见 §5），不作为 Package 1 的 blocker。

---

## 1. 为什么基线必须同时带 WIP 文件

失败只发生在 **3B-3.3 群聊 WIP** 的测试里，而那三个文件**不在任何 commit 中**：

| 文件 | 状态 |
|---|---|
| `backend/app/adapters/channels/wecom_bot.py` | modified（WIP：新增 `chattype=group` → `chatid` 映射） |
| `backend/tests/test_3b_2_2_wecom_bot_websocket_transport.py` | modified |
| `backend/tests/test_3b_3_3_wecom_bot_group_chat.py` | **untracked**（新文件） |

因此基线 worktree 的构造是「**旧代码 + 当前 WIP 文件**」——
这样 A/B 之间**唯一的差异变量就是 Package 1**（这正是对照实验要的）。

已核实基线 worktree 确实不含 Package 1：

```
00344f9 feat(wecom): add bot websocket transport and delivery ack validation
backend/app/persistence/models/identity.py:29:  ForeignKey("users.id"), nullable=False, index=True
```

---

## 2. 对称 A/B 证据

同一组文件、同一命令、独立 `--basetemp`，两个树各跑两轮：

```
python -m pytest tests/test_3b_2_2_wecom_bot_websocket_transport.py \
                 tests/test_3b_3_3_wecom_bot_group_chat.py -q
```

| 树 | 轮次 | 结果 | 失败用例 | 耗时 |
|---|---|---|---|---|
| **baseline（00344f9，无 Package 1）** | 1 | 1 failed, 84 passed | `test_3b_3_3::test_c1_group_session_matrix_case_a_reuse` | 25.90s |
| **baseline** | 2 | 1 failed, 84 passed | `test_3b_3_3::test_c1_group_session_matrix_case_a_reuse` | 25.15s |
| **Package 1（f43689a）** | 1 | 1 failed, 84 passed | `test_3b_3_3::test_c3_group_session_matrix_case_c_different_user` | 25.08s |
| **Package 1** | 2 | **85 passed** | — | 5.08s |

**关键事实**：基线**两轮都失败**（比 Package 1 更稳定地失败）。

## 3. 辅助证据（此前各次运行）

| 观察 | 数据 |
|---|---|
| `test_3b_2_2::test_e1` 单跑 | **3/3 通过**（各 0.36s） |
| `test_3b_2_2` 文件内单跑 | 1 failed, 68 passed |
| 全量套件（干净 basetemp） | 1 failed, **2112 passed**, 7 skipped；失败者 = `test_e1` |
| 全量套件（默认 `%TEMP%` basetemp） | 17 个 `PermissionError`（环境），非本次变更 |

失败用例**在 `e1` / `c1` / `c3` 之间轮换**——不是固定某一条逻辑被破坏。

## 4. 判定依据（三条独立证据）

1. **基线同样失败** → 不是 Package 1 引入（本 A/B 的直接结论）。
2. **失败集合不稳定**（`c1` / `c3` / `e1` 轮换）→ 时序/竞态特征，不是逻辑回归。
3. **耗时 25s vs 5s 的分野**：25s ≈ `fake.reply_event.wait(20)` 的 20s 预算耗尽后
   才落到后续断言（`s1/s4 is None`）。健康轮次 5s。

**正交性佐证**（即使不看 A/B 也成立）：

- 两处 seed 的都是**已绑定**身份（`Identity(user_id=user.id, ...)`，User 行存在）；
- Package 1 **只在** `user_id IS NULL` 或 User 行缺失时改变行为；
  这两个测试**都不进入该分支** → `IdentityResolutionExecutor.execute` 的返回值
  在改动前后**逐字段相同**。

---

## 5. 正式登记

```
Issue ID : WIP-3B3-3-WS-TIMING
状态     : OPEN（既有问题，非 Package 1 引入）
归属     : 3B-3.3 WeCom 群聊 WIP（wecom_bot.py + 两个其测试文件）
影响     : 全量回归噪音——每次 Authorization 变更都要重新解释一遍
根因候选 : fake WeCom WS 的握手/帧推送与 Worker 执行之间缺少显式同步，
           `reply_event.wait(20)` 在加载下被耗尽（非确定性）
```

**明确不做**（避免把 WIP 信号掩盖成绿灯）：

```
❌ 不在 Package 1 里给这些测试加 xfail / skip / 放宽断言
❌ 不修改 wecom_bot.py / test_3b_2_2 / test_3b_3_3（属 3B-3.3 的工作面）
❌ 不用「提高 wait 预算」来掩盖根因（可作为 3B-3.3 的候选修法，需其自行裁决）
```

**建议**：由 3B-3.3 自行收口（显式同步握手 / 隔离 fake WS 时序 / 收紧回归预算）。

---

## 6. 状态区分：Package 1 ≠ PG Closure Evidence

**这两件事必须分开陈述**（否则会把「环境矩阵待补」误读成「实现未完成」）：

```
Package 1（External Identity Decoupling）   = DONE / COMMITTED（f43689a）
    - 语义落地  ✅        - 结构落地  ✅（phase 1–5 证据 18 项）
    - 全量回归  2112 passed ✅（唯一失败已登记为 WIP-3B3-3-WS-TIMING）
    - 本地证据  已具备：真实 DB Runtime 已证明 Identity(user_id=NULL) 可进入 Runtime

PG Closure Evidence                         = PENDING（环境矩阵的最后一块）
    - 不是设计风险，也不改变 Package 1 的实现结论
```

| 项 | 状态 |
|---|---|
| Package 1 实现 + 提交 | ✅ DONE（`f43689a`） |
| Baseline Evidence | ✅ 本文件 |
| **PG Closure Evidence** | ⏳ **PENDING** |
| Package 1 FREEZE | ⏳ 待 PG Closure 完成后 |
| Package 2（Access Authorization） | ⏳ 未开始 |

### PG Closure 需要验证的四件事

```
① 迁移链            0005 → 0006 → head（单一 head）
② identities.user_id  nullable = true
③ FK                 NO ACTION（无 ondelete 子句 = 阻断式）
④ 真实 PostgreSQL 下 Identity(user_id=NULL) 能进入 Runtime
```

①–③ 是 DDL 事实核对，④ 是行为核对（本地已用**真实 DB** 证明，PG 侧只是补齐环境矩阵）。
另需让 PG-gated 的 `test_alembic_revision_is_head` 真正执行一次（本地默认 skip）。

---

## 7. 复现命令

```powershell
# 基线 worktree（pre-Package-1 + 当前 WIP 文件）
# 在仓库根目录执行（本文件使用相对路径，不暴露任何本机目录结构）
cd <repo-root>
git worktree add --detach ../acp-baseline-worktree f43689a^
Copy-Item backend/app/adapters/channels/wecom_bot.py ../acp-baseline-worktree/backend/app/adapters/channels/wecom_bot.py -Force
Copy-Item backend/tests/test_3b_2_2_wecom_bot_websocket_transport.py ../acp-baseline-worktree/backend/tests/ -Force
Copy-Item backend/tests/test_3b_3_3_wecom_bot_group_chat.py ../acp-baseline-worktree/backend/tests/ -Force

# A/B
cd ../acp-baseline-worktree/backend
python -m pytest tests/test_3b_2_2_wecom_bot_websocket_transport.py tests/test_3b_3_3_wecom_bot_group_chat.py -q --basetemp="$env:TEMP\acp_bl"
cd <repo-root>/backend
python -m pytest tests/test_3b_2_2_wecom_bot_websocket_transport.py tests/test_3b_3_3_wecom_bot_group_chat.py -q --basetemp="$env:TEMP\acp_p1"

# 清理（本文件写完后已执行）
git worktree remove --force ../acp-baseline-worktree
```

> 注：`--basetemp` 指向独立目录是必需的——默认 `%TEMP%\pytest-of-*` 在本机存在
> 残留目录权限问题，会产生 17 个与代码无关的 `PermissionError` 噪音。
