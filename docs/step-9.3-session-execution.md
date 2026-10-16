# Step 9.3 — Session Resolution Execution

> 8.3 Session Resolution 契约的第一次真实执行；也是第一个真正依赖 Connection 的
> Runtime stage——但 **Connection 只消费、不选择**。

## 一个必须先说清的前提（本次的实际发现）

8.3 的 docstring 写的是「复用既有冻结的 `find_active`」，但真实仓库里：

- `sessions` 表（ORM）**已存在**（4.2/5.3 冻结）；
- `Session` 领域实体、`SessionRepository` Port、Mapper、Adapter **都不存在**；
- 8.3 的验收测试甚至显式冻结了两条「未实现」断言（Session Vertical Slice 未开始、
  全仓无 `find_active`）。

也就是说：9.3 要「真正执行」，`find_active` **根本不存在**——这属于用户既定原则
里"实际执行暴露不足"的情形，因此本次落地了 Session 的**读路径**地基：

| 新增 | 内容 |
| --- | --- |
| `domain/session.py` | `Session` 实体（隔离键 Identity × Connection × conversation） |
| `repositories/session.py` | `SessionRepository` Port —— **只有 `find_active`** |
| `persistence/mappers/session.py` | `to_domain`（只落读路径，无 `to_orm`） |
| `persistence/repositories/session.py` | `SqlAlchemySessionRepository.find_active` |
| UoW | 新增 `sessions` 属性（Port + 实现） |

刻意**没有** create / update：会话创建属后续 Slice。

## 执行链（只读；CREATE_NEW 只是决策）

```
(identity_id, connection_id, external_conversation_id)
    ↓ SessionRepository.find_active（status == "active"）
    ↓ hit  → REUSE_ACTIVE（existing_session_id = 该 session 的 id）
    ↓ miss → CREATE_NEW（existing_session_id = None）
```

**CREATE_NEW 是决策结果，不是立即 `sessions.create()`**——否则 Executor 会同时
拥有 Resolution / Persistence mutation / Session lifecycle 三个职责。

## 三维隔离（本次最重要的测试）

四个组合各自独立，互不复用：

```
i1 + c1 + X      i1 + c1 + Y      i1 + c2 + X      i2 + c1 + X
```

- `identity_id` 用 **Identity.id**（Channel 边界），不是 `User.id`；
- `external_conversation_id` 参与查找，绝不可省略（防群聊/会话上下文泄漏）；
- 非 active 的 session 不算命中。

## 边界

- `connection_id` 只消费：不 import `ConnectionRepository`、不查 Connection
  （Connection 选择归 8.4 Routing）；
- 失败 → `SessionResolutionFailedError`（STOP）：identity_id / connection_id 非
  正整数、conversation 为空；
- 不创建 Session / Identity / Connection，不猜 conversation，不跨 conversation
  复用；
- 不创建 SessionResolver / SessionService / SessionManager / SessionPolicy /
  SessionLifecycleManager；
- 不 import SQLAlchemy / ORM / persistence / adapters。

## 验收

`tests/test_step9_3_session_execution.py`（26 项）：REUSE_ACTIVE / CREATE_NEW /
mode 与 existing_session_id 一致 / 四象限隔离 / 不跨 conversation 复用 /
非 active 不算命中 / 用 Identity.id（sessions 表无 user_id）/ 不选择 Connection /
CREATE_NEW 不落库 / 源码无写调用 / Repository 只有 find_active 且三参数 /
Fail Closed（8 组非法输入）/ Execution ≠ Resolver / mutation（越层 import、
写调用、注入 SessionResolver → 7.3 反向审计报红）。

既有冻结精确扩边：5.7 文件清单 + REMAINING_ENTITIES、6.5 REMAINING_ENTITIES +
EXPECTED_ERRORS + application 文件面、6.7 forbidden_next + 文件面、7.1 文件面、
7.3 NEXT_STAGE（session 从"未来"转"已交付"）+ 文件面、7.3 mutation 用例改用
interaction（避免覆盖真实 session.py）、8.1 命名类白名单、8.2 文件面、
8.3 两条"未实现"断言改为"读路径已落地 / find_active 只在 Port+Adapter"。

全仓 `pytest -q` = 459 passed，lint 0。
