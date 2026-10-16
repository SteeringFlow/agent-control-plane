# Step 6.1 —— Management Application Boundary

一句话目标：**让第一个 Application Service 正确地使用 UoW + Repository Port，
而不越过 Application Boundary——Application 不知道 SQLAlchemy。**

```
API（暂不引入）
 │
 ▼
Application Service            app/application/agents.py（本步唯一新增业务代码）
 │
 ▼
UnitOfWork Port                app/repositories/unit_of_work.py（本步唯一新增端口）
 │
 ├── Repository Port（agents）
 │       │
 │       ▼
 │   Repository Adapter
 │       ▼
 │     Mapper → ORM → DB
 ▼
Domain Entity
```

> API 故意留到 6.2+：本步是 Application Boundary，不是 API Vertical Slice。
> 不做 HTTP schema / auth / error mapping / router / CRUD endpoints。

---

## 1. Vertical Slice 选择：Agent Management

第一刀只做 Agent：5.4 已把 Agent Domain / Port / Mapper / Adapter / ORM 全部跑通，
本步只需把 Application 层接上去，避免同时引入 User/Channel/Connection。

## 2. AgentApplicationService 职责（只两个用例）

- `create_agent(...)`：构造 Agent Domain → `with uow:` `uow.agents.create` → `uow.commit()` → 返回带 id 的 Domain；
- `get_agent(id)`：`uow.agents.get_by_id` → Domain / None。

## 3. 关键设计：Application 依赖 UoW Port，不依赖 SqlAlchemyUnitOfWork

新增 `app/repositories/unit_of_work.py`（薄 Protocol）：

```python
class UnitOfWork(Protocol):
    users: UserRepository
    channels: ChannelRepository
    agents: AgentRepository
    def __enter__(self) -> UnitOfWork: ...
    def __exit__(self, exc_type, exc, tb) -> None: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
```

`SqlAlchemyUnitOfWork`（persistence）结构满足该协议。
依赖方向因此保持：**Application → Repository / UoW Port → Persistence Adapter**，
绝不变成 Application → SQLAlchemy。

## 4. 冻结边界：不做框架

不做 GenericUnitOfWork / TransactionManager / AbstractPersistenceService /
RepositoryManager / ApplicationRepositoryFactory。UoW Port 只表达
「Application 所需要的事务协作能力」，不建设新框架。

## 5. 最小文件增量

```
app/application/agents.py        ← 新增（第一个 Application Service）
app/repositories/unit_of_work.py ← 新增（UoW Port）
tests/test_step6_1_application_boundary.py ← 新增
```

`tests/test_step6_1_application_boundary.py` 覆盖 A~E + AST 护栏 + 防漂移。

## 6. AST 架构护栏（更新后的层规则）

- `application/`：❌ import sqlalchemy / persistence.*（models/mappers/repositories/database/unit_of_work）/ fastapi / api / adapters；✅ import domain / repositories ports。
- `persistence/`：✅ 可实现 repositories ports；❌ 不反向 import application。
- Service 归属：`*Service` 类只允许出现在 application 层（守卫从“禁止提前出现”演进为“只准在 application”）。

## 7. 验收结果

`python -m pytest -q` → **88 passed**（82 回归 + 6.1 新增，含对 5.7/smoke/边界的断言同步演进）：

| 项目 | 结果 |
| --- | --- |
| Application → UoW Port | ✅ agents.py 只 import domain/repositories；无 SqlAlchemy 标识符 |
| UoW Port 薄 | ✅ repositories/unit_of_work.py 只有 UnitOfWork 类 |
| Persistence UoW 满足 Port | ✅ 结构成员齐全 |
| Create Agent 全链 round-trip | ✅ Domain → Application → UoW → Repo → Mapper → ORM → SQLite → 回读相等 |
| Get Agent | ✅ existing → Agent；missing → None |
| 事务边界归 UoW | ✅ 源码无 session 概念；commit 只经 uow.commit() |
| Exception Rollback | ✅ flush 后抛异常 → DB 无该 Agent |
| 不引入 API / Runtime | ✅ api/management|runtime 仍空壳；无 *_router/_dispatcher |
| 全量回归 | 88 passed |

实现 Tip：AST 子串检查会命中 docstring 里"不做 GenericUnitOfWork/..."的否定句，
改用 ClassDef 名集合断言（只有 UnitOfWork）。

## 8. 下一步

```
6.2  Agent Management Application 深化（或用例补全）
6.3  Channel Management Application
6.4  User / Identity Management
...
（编号随真实落地调整，不提前锁死）
```
