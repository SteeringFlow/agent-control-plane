# Step 5.5 —— Persistence Transaction Boundary & Unit of Work

一句话目标：**回答并落地「谁负责一次业务操作的事务边界」——UoW。**

```
Application
    │
    ▼
   UoW                        app/persistence/unit_of_work.py
   │
   ├── users     ✅  SqlAlchemyUserRepository
   ├── channels  ✅  SqlAlchemyChannelRepository
   ├── agents    ✅  SqlAlchemyAgentRepository
   ├── identities  ⏳（后续 Slice）
   ├── connections ⏳
   ├── sessions    ⏳
   ├── interactions⏳
   └── activities  ⏳
          │
          ▼
      SQLAlchemy
```

4.3 冻结的「Application → UoW → Repository → SQLAlchemy」在此落地。
本步**唯一新增核心文件**：`app/persistence/unit_of_work.py`。

---

## 1. UoW 的职责（只三件事）

1. 提供**同一个 Session**；
2. 暴露 **Repository**（`uow.users` / `uow.channels` / `uow.agents`）；
3. `commit()` / `rollback()` / `close()`，及 `with` 上下文。

```
with uow:
    uow.users.create(...)
    uow.channels.create(...)
    uow.commit()          # 显式提交
```

失败路径（异常）自动 rollback，无需手写 try/except。

## 2. 一个关键边界：UoW 不是 Service

绝不出现：

```python
uow.create_user()        # ❌
uow.resolve_identity()   # ❌
uow.route_agent()        # ❌
```

它只提供 Repository 属性 + `commit/rollback/close`。该边界被双重守护：
- 实例面测试：`vars(uow)` 只有 `_session_factory/session/users/channels/agents`；
- 源码面测试：`unit_of_work.py` 定义的方法面 ⊆ `{__init__, __enter__, __exit__, commit, rollback, close}`。

## 3. Repository 共享同一个 Session（最重要事实）

```
UoW ─ Session #1 ─ UserRepository / ChannelRepository / AgentRepository
```

而非每个 Repository 一个 Session——否则 commit/rollback 无法形成统一事务边界。
测试 A 直接断言 `uow.users._session is uow.channels._session is uow.agents._session is uow.session`。

## 4. Repository 不需要改变

5.4 的 `repository.create()` 内 `flush()` 保持原样。
**绝不**为了 UoW 把 `create()` 改成 `create()+commit()`。
commit 永远属于 UoW。测试 F 双重守护：
- 行为：`with uow: uow.users.create(...)`（无 commit、无异常）退出后**不落库**；
- 源码：adapter 实现里没有 `commit` 调用。

## 5. UoW 生命周期

```
create UoW → __enter__（create Session → create Repositories）
→ Application operation
→ commit / rollback（显式）/ 异常自动 rollback
→ __exit__ / close（关闭 Session，释放引用）
```

## 6. Context Manager 语义（最简）

`enter → operation → exception→rollback → exit→close`。
异常处理不过度设计：没有嵌套事务、没有 savepoint、没有自动重试。

## 7. 只暴露已存在的三个 Port

UoW 现在只装配 `users/channels/agents`（5.4 已启用）。
**不因为 UoW 的"结构看起来需要完整"就顺手实现剩余 5 个 Repository**——
等 5.6/后续 Vertical Slice 启用时再加。节奏与 5.4 的"只启用三个，防止架构漂移"一致。

## 8. UoW 不知道 Domain

UoW 持有 Repository Port，不出现 User/Channel/Agent 的业务操作。
测试守护：`unit_of_work.py` 不 import `app.domain` / `app.repositories`。

## 9. 最禁止的清单

❌ TransactionManager / TransactionService / 分布式事务 / Saga /
Outbox / Event Sourcing / CQRS / Async Transaction Framework / Retry Framework。

现在就是「SQLAlchemy Session + commit + rollback」，这已经足够。

---

## 10. 验收结果

`python -m pytest -q` → **63 passed**（新增 `tests/test_unit_of_work.py`，10 项）：

| 验收项 | 结果 |
| --- | --- |
| UoW 创建 / 关闭 | ✅ `__enter__`/`__exit__`/`close` 生命周期 |
| 三个 Repository 共享 Session | ✅ Test A |
| Commit | ✅ Test B（跨 Session 可见） |
| 显式 Rollback | ✅ Test C（不落库） |
| 异常自动 Rollback | ✅ Test D（raise 后 DB unchanged） |
| Session 不泄漏 | ✅ Test E（QueuePool checkedout 归还、引用清空） |
| Repository 不 Commit | ✅ Test F（行为 + 源码双重） |
| UoW 不是 Service / 不知道 Domain | ✅ 边界测试 |
| 全量回归（53 + 10） | ✅ 63 passed |

实现 Tip：静态池内存库（StaticPool）没有 `checkedout()`，
Session 泄漏测试改用临时文件引擎（QueuePool）。

## 11. 下一步

```
5.6+  Identity / Connection / Session / Interaction / Activity 各自 Slice
     （届时 UoW 逐次装配对应 Repository）
6.x   Runtime Vertical Slice：WeCom → Control Plane → ACP Agent → WeCom
```
