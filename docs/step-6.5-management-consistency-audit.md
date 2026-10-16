# Step 6.5 —— Management Foundation Consistency Audit

> **这不是最终的 6.x Closure Audit。**
> 它只回答一个问题：Agent / Channel / User 三个 Management Slice **并行演进**
> 之后，Application Management 层有没有发生**结构漂移**？

审计文件：`tests/test_step6_5_management_consistency.py`（49 项，全机器化）

## A. Application Service 一致性

| 检查 | 手段 | 结果 |
| --- | --- | --- |
| 只依赖 Domain + Port | AST import 根白名单 = `app.domain / app.repositories / app.application + stdlib` | ✅ |
| 不依赖 Persistence | 禁 `app.persistence` | ✅ |
| 不依赖 FastAPI | 禁 `fastapi / starlette / app.api / app.schemas` | ✅ |
| 不依赖 SQLAlchemy | 禁 `sqlalchemy` + Name 层禁 `Session/sessionmaker/Engine/*Model/...` | ✅ |
| 结构统一 | 每个模块只有一个 `*ApplicationService`，`__init__(self, uow_factory)` | ✅ |

## B. 输入契约方向

方向必须是

```
Application Input Contract  →  Domain Entity
```

而不是

```
API Schema  →  Application
```

| 检查 | 结果 |
| --- | --- |
| `Create*/Update*` 是 Application 层的 **frozen dataclass** | ✅ 6 个契约全部 frozen |
| `create_*(input: CreateX) -> X` 签名锁定 | ✅ 三者一致 |
| 方法体内从 `input` 读字段构造领域对象 | ✅ 证明是 Contract → Domain |
| Application 层不 import `pydantic` / `app.schemas` / `app.api`（无 `BaseModel/Field`） | ✅ 无反向依赖 |
| Update 契约字段全部 Optional 且写明 None 含义（部分更新语义） | ✅ |

## C. Transaction

统一模式，不允许各自演化：

```
Service → UoW → commit
```

| 检查 | 结果 |
| --- | --- |
| 每个用例方法都 `with self._uow_factory() as uow` | ✅ 9 个用例方法全覆盖 |
| commit 唯一形式是 `uow.commit()` | ✅ 无其它 commit 调用 |
| `create_*` / `update_*` 必须 commit | ✅ |
| Service 不自己 `rollback`（交给 UoW 上下文） | ✅ 0 处 |
| 只经 `uow.<repos>.<method>()` 访问仓储，不直接 new 实现 | ✅ |

## D. Repository 方法面（**不要求对称**）

| Port | 方法面 | 语义依据 |
| --- | --- | --- |
| Agent | `get_by_id / create / update / find_by_name` | 全局 name 去重（路由到 ACP Agent 的标识） |
| Channel | `get_by_id / create / update / find_by_type_and_name` | 平台限定 `(type, name)` 去重；wecom 与 feishu 可同名共存 |
| User | `get_by_id / create / update` | 唯一性归未来的 Identity；**不扩张任何 name 查找** |

其它结论：
- 三者公共能力一致（读 / 写 / 更新），差异只在去重语义 → **结构统一，语义不对称**
- Adapter 方法面 **恰好等于** Port 方法面（不多不少 = 无偷渡、无遗漏）
- Port 每个方法都必须有业务语义 docstring（防止方法面默默长成 CRUD）

## E. Error Model

| 检查 | 结果 |
| --- | --- |
| `AgentNotFoundError` / `ChannelNotFoundError` / `UserNotFoundError` 存在 | ✅ |
| 全部直接继承 `ApplicationError`（不是 `SQLAlchemyError` / `HTTPException`） | ✅ |
| `errors.py` 自身零 import（纯语义，不带任何基础设施） | ✅ |
| Service 抛出的**只有** Application 层错误类型 | ✅ |
| Application 层无 `HTTPException / SQLAlchemyError / IntegrityError / JSONResponse` | ✅ |

> 备注：`status` 未列入禁用标识符——它是领域实体/输入契约里的业务状态字段，
> 不是 HTTP 状态码。

## F. 防止 Connection / Identity 偷渡

| 检查 | 结果 |
| --- | --- |
| `application/` 文件面固定 = `{__init__, agents, channels, errors, users}.py` | ✅ |
| identity / connection / session / interaction / activity 无 domain/port/adapter/mapper/application 文件 | ✅ |
| 无 `IdentityService` / `ConnectionService` / `SessionResolver` / `Router` / `Dispatcher` / `Runtime*` 类 | ✅ |
| `*Service` 类只允许出现在 application 层（且必须是 Application Service） | ✅ |
| `adapters/{channels,agents}`、`api/{management,runtime}` 仍是空壳 | ✅ |
| 无 `*_router.py` / `*_dispatcher.py` / `*_resolver.py` / `*_service.py` | ✅ |

## 守卫有效性（变异探测）

向 `app/application/users.py` 注入 `from sqlalchemy.orm import Session` 后，
审计**如预期失败 2 项**（A 依赖白名单 + E 无 SQLAlchemy 语义），
回滚后 184 项全部通过 —— 证明守卫不是装饰品。

## 结论

**Application Management 层没有结构漂移**：三者结构统一（构造器、输入契约、
UoW 事务、错误模型、文件面），领域语义保持不对称（去重键各不相同）。

`python -m pytest -q` → **184 passed**（135 回归 + 6.5 新增 49）。

## 下一步

```
6.6+  Identity Slice（External Identity Resolution 的入口）
      → Management API（把 Application errors 映射成 HTTP）
      → Connection Vertical Slice（授权边界，单独设计）
```

> 最终的 6.x Closure Audit 会在 Management API 之后再单独做一次；
> 本步只回答“三个 Slice 并行演进有没有跑偏”。
