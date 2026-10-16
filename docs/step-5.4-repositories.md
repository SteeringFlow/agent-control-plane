# Step 5.4 —— Repository Boundary & First Persistence Adapter

一句话目标：**把 Persistence Schema 变成 Application 可实际使用的
Persistence Port / Adapter，并用一个 Vertical Slice 证明整条链真实成立。**

4.3 冻结的原则在此落地：

```
Application
     │
     ▼
Repository Interface (Port)      app/repositories/
     ▲
     │  interface
Persistence SQLAlchemy Repository app/persistence/repositories/
```

而不是 `Application → SQLAlchemy Session`。

---

## 1. First Vertical Slice：只做 User / Channel / Agent

为了不重新引发"架构提前设计"，本步刻意只做**三个最简单的 Repository**，
先验证链路成立，再考虑扩 Identity / Connection / Session / Interaction / Activity：

```
UserRepository (Port)
     │
     ▼
SqlAlchemyUserRepository (Adapter)
     │
     ▼
UserModel
     │
     ▼
UserMapper
     │
     ▼
User (Domain Entity)
```

测试要求：create → commit → get_by_id → Domain Entity 的**真实 round-trip**。
Channel / Agent 复用同一模式。

## 2. Repository 分两层（目录即边界）

```
app/repositories/              # Port：只描述"业务需要什么查询能力"
├── user.py                    UserRepository（get_by_id / create）
├── channel.py                 ChannelRepository
└── agent.py                   AgentRepository

app/persistence/repositories/  # Adapter：只知道 SQLAlchemy 怎么实现
├── user.py                    SqlAlchemyUserRepository
├── channel.py
└── agent.py
```

Port 不 import：SQLAlchemy / persistence / FastAPI / Adapter / persistence/repositories。
（AST 边界测试按层守护；`repositories` 层规则已禁止上述依赖。）

## 3. Repository 不是 Generic CRUD

方法按**业务语义**命名（如未来的 Identity `find_by_external_identity()`、
Session `close()`、Interaction `update_status()`、Activity `append()`），
不做 `GenericRepository[T]` 万能 CRUD。本轮三者的最小语义集是
`get_by_id` / `create`。

## 4. Mapper 从空壳进入使用

首次真正触碰 Domain Entity 时的纪律：

- **方案 A**（ORM Model 直接返回给 Application）→ Persistence 泄漏，不采用；
- **方案 B**（ORM Model → Mapper → Domain Entity）→ 本步采用。

每个 Mapper 提供 `to_domain`（读路径）与 `to_orm`（写路径）。
Mapper 不承载业务规则。

## 5. Domain Entity（第一小批）

`app/domain/{user,channel,agent}.py` 为纯 Python frozen dataclass：
- 不继承 SQLAlchemy `Base`（测试断言无 `__table__`）；
- 不依赖 Pydantic / FastAPI（AST 层规则守护）；
- `credential_ref` 只承载引用，凭据明文不进 Domain 也不进 DB。

只设计这三个实体的领域对象，其余五个等各自 Slice 再落（防漂移测试守护：
identity/connection/session/interaction/activity 的 port/adapter/mapper/domain
文件不得提前出现）。

## 6. Session 从哪来、事务归谁

- Repository **接受 Session**，Session 来自 Persistence 基础设施（`SessionLocal`），
  不是 Application 自己创建；
- Repository **不 commit**（`create` 只 `flush` 拿回自增 id）；
- 事务边界属于 **5.5 Unit of Work**——本步明确不引入 UoW。

## 7. relationship() 仍为零

5.3 的决定自然延续：本轮三个 Repository 通过 FK 主键查询即可满足，
无需为查询增加 ORM relationship。原则不变：只为真实 Repository 查询加关系，
不为"ORM 完整性"增加。

## 8. 验收结果

`python -m pytest -q` → **53 passed**（新增 `tests/test_repositories.py`）：

| 验收项 | 结果 |
| --- | --- |
| Port（repositories/）不 import SQLAlchemy/persistence/FastAPI | ✅ AST 边界测试 |
| Adapter 满足 Port 协议 | ✅ isinstance(UserRepository) 等 |
| User Round Trip（Domain→DB→Domain） | ✅ create→commit→get_by_id→frozen dataclass 相等 |
| Channel Round Trip（含 config JSON） | ✅ |
| Agent Round Trip（全字段含 credential_ref） | ✅ |
| get_by_id 不存在返回 None | ✅ |
| Repository 不 commit：rollback 后不落库 | ✅ |
| Domain 纯度（不继承 Base、无 __table__） | ✅ |
| 只做三个（防漂移） | ✅ 其余五个文件不存在 |
| 全量回归（5.1~5.3 断言按演进翻转） | ✅ 53 passed |

实现期环境坑（留档）：
本机 Python 用户环境的 SQLAlchemy 被漂移降级到 **2.0.35**，该版本在
Python 3.14 下有已知 `typing.Union.__getitem__` 兼容 bug，导致 ORM 类定义
即抛 `TypeError`。`requirements.txt` 早已声明 `sqlalchemy>=2.0.51`，
按声明升级回 `2.0.52` 后恢复。**教训：发现"同一段代码上次还绿这次红"，
先查依赖版本是否被外部改动。**

## 9. 本步刻意不做

- Service / Routing / IdentityResolver / SessionResolver / AgentDispatcher / Runtime / API
- Unit of Work（5.5）
- 其余五个 Repository（各自 Slice）
- relationship()（等真实查询需要）
- Alembic（触发条件见 5.3 §6）

## 10. 下一步

```
5.5  Unit of Work：多个 Repository + 一个事务边界
5.6+  Identity / Connection / Session / Interaction / Activity 各自 Slice
```
