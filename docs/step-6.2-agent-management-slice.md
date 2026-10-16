# Step 6.2 —— Agent Management Application Slice

一句话目标：**把 6.1 的两个方法（create_agent/get_agent）验证成一个完整但很小的
Agent Management Application Slice——不是 CRUD 全家桶，而是确认 Create / Get /
Update + 基础业务约束在 UoW transaction boundary 上稳定运行。**

```
Agent
 ├── Create   （输入契约 + 校验 + 应用级去重）
 ├── Get      （existing → Agent；missing → None）
 ├── Update   （部分更新；not found → AgentNotFoundError）
 └── 基础状态  （status 字段可管理：active/disabled/...）
```

仍不做：Channel / User / Identity / Connection Management、API、Frontend、
Runtime、Router、AgentAdapter。尤其不为“Agent 管理完整”提前实现 **Connection**——
Connection 是 Control Plane 最核心的运行时授权边界，等 Agent/Channel 两端管理
模型稳定后再单独切 Vertical Slice。

---

## 1. 交付内容

### Production Code

| 文件 | 变化 |
| --- | --- |
| `app/application/errors.py` | **新增**：Application 层错误类型（ApplicationError 基类 + AgentNotFoundError / DuplicateAgentError / InvalidAgentInputError）。只表达用例失败，不带 SQLAlchemy/FastAPI 概念；跨层 core/errors.py 仍延后 |
| `app/application/agents.py` | **扩展**：`CreateAgent` / `UpdateAgent` 输入契约（frozen dataclass，非 Pydantic）；校验；create 前应用级去重守卫；`update_agent` 部分更新；错误映射 |
| `app/repositories/agent.py` | Port 方法面扩为 `get_by_id/create/update/find_by_name`（业务语义） |
| `app/persistence/repositories/agent.py` | Adapter 实现 `update` / `find_by_name`（仍只 flush 不 commit） |
| `app/persistence/mappers/agent.py` | 新增 `apply_to_orm(entity, row)`（更新路径覆写已加载行） |

### Tests

`tests/test_step6_2_agent_management.py`（15 项）+ 6.1 测试随输入契约演进 + 5.7 A4 方法面演进。

## 2. 关键设计决定

- **输入契约放 application 层**：application 不 import `app.schemas`（API schema 层），
  故用 frozen dataclass（`CreateAgent`/`UpdateAgent`）做输入契约，字段级校验抛
  `InvalidAgentInputError`。
- **DuplicateAgentError 是应用级软约束**：agents 表未冻结 name 唯一约束，
  应用在 create 前 `find_by_name` 守卫，存在 TOCTOU 窗口——文档明确：若真实需求
  要求硬保证，应通过 schema/migration 加唯一约束后再移除守卫。
- **update 的 None 语义**：None = 不修改；本 Slice 不支持把字段显式清空为 None
  （清空语义等真实管理需求出现再定义）。
- **update 缺行抛 AgentNotFoundError**（同一 UoW 内先 get_by_id 确认存在）。
- **事务边界不变**：每个用例自持一个 UoW，`uow.commit()` 显式提交，
  异常自动 rollback；Repository 仍只 flush。

## 3. 边界（Scope Audit 仍然成立）

❌ Channel/User/Identity/Connection Management ❌ API ❌ Frontend ❌ Runtime
❌ Router ❌ AgentAdapter ❌ Generic CRUD / 全家桶

## 4. 验收结果

`python -m pytest -q` → **103 passed**（88 回归 + 6.2 新增，1.5x 于上一阶段）：

| 项目 | 结果 |
| --- | --- |
| 输入契约校验（空 name/type） | ✅ InvalidAgentInputError，DB 无写入 |
| Create 全链路 round-trip（含 config/credential_ref） | ✅ |
| Duplicate 去重守卫 | ✅ DuplicateAgentError；事务回滚无残留半成品 |
| Get | ✅ existing→Agent / missing→None |
| Update 部分字段 | ✅ 跨 UoW 重读全字段一致，未提供字段保持 |
| Update missing | ✅ AgentNotFoundError，且不建新行 |
| 基础状态管理 | ✅ status=disabled 落库 |
| 事务边界 | ✅ commit 生效；异常自动 rollback 无残留 |
| 全量回归 | 103 passed |

同步演进的断言（沿用纪律）：6.1 测试签名随输入契约演进；
5.7 closure A4 从“Port 方法面 ⊆ {get/create}”演进为按实体维护预期方法面
（agent = {get_by_id, create, update, find_by_name}；user/channel 不变）。

## 5. 下一步

```
6.3  Channel Management Application（复用同一模式；暂不碰 Connection）
6.4  User / Identity Management
...
Connection 单独 Vertical Slice（等 Agent/Channel 两端管理模型稳定）
API 层在管理 Slice 稳定后引入
```
