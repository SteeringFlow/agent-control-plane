# Step 5.6 —— Persistence Smoke Test

一句话目标：**把 5.1~5.5 的 Persistence 组件连起来，做一次接近真实应用使用方式的端到端冒烟验证。**

```
Settings
   ↓
Engine（真实 file DB，惰性建连）
   ↓
Session / UoW（默认工厂，与未来 Application 用法一致）
   ↓
Repository Port / Adapter
   ↓
Mapper
   ↓
ORM Model
   ↓
SQLite
```

本阶段**不加架构层**：新 Repository / Application Service / Runtime / API /
Adapter / Routing / Resolver 一律不做；不修改业务模型。本质是 Integration / Smoke Test。

---

## 1. Smoke Path 1 —— 应用式 round-trip（A + E）

用目前真正实现的三个实体（User / Channel / Agent），模拟未来 Application Service 的用法：

```python
with SqlAlchemyUnitOfWork() as uow:      # 无参 = 真实 SessionLocal（Settings 驱动）
    user = uow.users.create(User(name="zhang-san"))
    channel = uow.channels.create(Channel(name="wecom", type="wecom"))
    agent = uow.agents.create(Agent(name="helper", type="acp"))
    uow.commit()

with SqlAlchemyUnitOfWork() as uow:      # 新 UoW 重新读取
    fetched = uow.users.get_by_id(user.id)   # Domain Entity 完整返回
```

这条通过，即可有信心说：**Repository + Mapper + UoW 已具备被 Application Layer 使用的条件**。

## 2. Smoke Path 2 —— Rollback / Exception Rollback（B / C）

在完整 Persistence 链上验证 5.5 的事务边界仍然成立：
显式 `rollback()`、以及 `with` 内 `raise` 后自动回滚，重开 UoW 查得 None。

## 3. Smoke Path 3 —— Constraint（不做）

不为了 Smoke Test 验证 FK 去实现 Connection Repository。
5.3 已用真实重复插入验证过数据库 FK/唯一约束；5.6 的职责不是重测全部 Schema constraint，
当前只验证已启用的 User/Channel/Agent vertical slice。

## 4. Health Smoke（F）

`GET /health` 保持 `200 {"status":"ok"}`，且**不碰数据库**——
/health 表达的是 Process / Application Liveness，不是 Database Readiness（与 5.1 决定一致）。
未来若需要 `/ready` 再定义数据库 readiness，现在不混。

## 5. SQLite File Lifecycle（G）

验证「启动/import 不产生 .db，首次真实 DB 操作才创建文件」：

- 应用源码（app/）**无 `create_all`**（AST 机器化确认）——我们没在 startup 偷偷建表；
- schema 由测试 harness 显式 `create_all` 建立（与应用启动逻辑无关）；
- import 完应用后文件不存在 → 首次真实 DB 操作后文件出现 → UoW commit → 数据可重新读取。

## 6. 结构检查（5.6.10）

测试断言 5.6 未新增任何业务层：`application/` 仍空壳、
`adapters/{channels,agents}` 与 `api/{management,runtime}` 仍空壳、
全 app 无 `*_service.py` / `*resolver*.py`。

## 7. 验收结果

`python -m pytest -q` → **71 passed**（63 回归 + 8 冒烟）：

| 项目 | 结果 |
| --- | --- |
| UoW → 3 Repository | ✅ 真实默认链 |
| Domain → DB → Domain | ✅ round-trip（frozen dataclass 相等） |
| Commit | ✅ 跨 UoW 可见 |
| Rollback | ✅ 三条计数全 0 |
| Exception Rollback | ✅ raise 后 None |
| Session Cleanup | ✅ QueuePool checkedout 归还 |
| DB File Lifecycle | ✅ 惰性建连 + 无 create_all 于 app 源码 |
| /health | 200（不碰 DB） |
| 全量回归 | 71 passed |
| 无新增业务层 | ✅ 结构测试 |

实现 Tip（留档）：UoW commit 后连接已归还连接池，占用断言要在
create/flush 之后、commit 之前做（smoke 测试踩过）。

## 8. 下一步

Persistence 基建（5.1~5.6）到此完整验证。后续两条线任选：
- 5.6+ 其余五个 Repository Slice（Identity/Connection/Session/Interaction/Activity）；
- 或先进入 Runtime Vertical Slice（WeCom → Control Plane → ACP Agent → WeCom），
  用真实用例驱动剩余 Repository 的实现节奏。
