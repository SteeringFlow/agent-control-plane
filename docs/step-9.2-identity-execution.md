# Step 9.2 — Identity Resolution Execution

> 8.2 Identity Resolution 契约的第一次真实执行。这是 Runtime Vertical Slice 的
> 第一块「真正执行能力」：Runtime Application 首次通过 Repository Port 使用 Persistence。

## 与 8.2 的区别

| | 8.2 | 9.2 |
| --- | --- | --- |
| 定位 | 冻结语义契约 | 真实执行契约 |
| Persistence | 不允许碰 | 允许经 Repository Port 使用 |
| 产物 | `IdentityResolution`（identity + user） | 同一个 `IdentityResolution`，但真的查到 |

仍不能越界：Runtime 不碰 SQLAlchemy / ORM / SessionLocal / ORM Model。
链路：

```
Runtime Identity Execution
    ↓ IdentityRepository Port
SQLAlchemy Repository Adapter
    ↓ ORM
Database
```

## 执行链（只消费既有冻结方法，零新 Repository 方法）

```
(channel_id, external_identity_id)
    ↓ IdentityRepository.find_by_channel_and_external_id   （6.6 冻结）
    ↓ Identity                          ← 命中即解析成功
    ↓ Identity.user_id（可为 None：未绑定 ACP Account）
    ↓ UserRepository.get_by_id                             （6.4 冻结）
    ↓ User | None                       ← 可选的管理关联
    ↓ IdentityResolution(identity=…, user=None | User)
```

Runtime Resolution 不为自己的到来反过来污染已冻结的 Repository Contract。

## Fail Closed

- **未找到 Identity** → `IdentityResolutionFailedError` → STOP；
- **User 缺失不再 Fail Closed**（Gate 2.6 D3 / Implementation Package 1）：
  Identity 命中即 RESOLVED，`user = None` 是**正常结果**；
  执行链是**单点改造**（`app/application/identity_resolution.py`）；
- 绝不创建 Identity / User；绝不猜测（无 fuzzy / email / phone matching / 自动
  linking）——「user 缺失」**不得**被转换成「那就补一个 User」。
  这是 6.6 Management + 8.2 契约边界在 Runtime 中的第一次落地。

## 8.2 契约修订记录（Implementation Package 1）

| | 修订前 | 修订后 |
| --- | --- | --- |
| 成功结果 | `identity: Identity` + `user: User` | `identity: Identity` + `user: User \| None` |
| 失败条件 | Identity 未命中 **或** User 缺失 | **仅** Identity 未命中 |
| 失败码语义 | 两种情形共用 `IDENTITY_RESOLUTION_FAILED` | 只表达「Identity 未命中」（歧义消失） |

来源：Gate 2.6 D3（语义裁决）→ Gate 3 §C（契约与迁移设计）。

## Channel 隔离

唯一键 `(tenant_id, channel_id, external_id)`（4.2/6.6，Cloud-0.1.1-B 前导
tenant_id）：同一 `external_id` 跨 Channel 是两个独立查找空间，绝不跨渠道合并。

## 明确不做

IdentityResolver / IdentityResolutionService / IdentityLinker / IdentityMatcher /
IdentityResolutionPolicy ——「resolution」语义不构成创建 Resolver 概念的理由。
Coordinator（9.1）此时也不接入：先独立证明 Identity Execution 能真实工作，再于
9.7 统一接线，避免故障时无法定位是 Coordinator / Execution / Repository /
Transaction 哪一层的问题。

## 交付物

- `app/application/identity_resolution.py`：`IdentityResolutionExecutor`（注入
  `uow_factory`，暴露 `execute(channel_id, external_identity_id) -> IdentityResolution`）；
- `app/application/errors.py`：`IdentityResolutionFailedError`（保持 errors.py 零
  import）；
- `app/application/__init__.py`：导出；
- 5 处 application 文件面断言 +1（`identity_resolution.py`）；6.5 `EXPECTED_ERRORS`
  +1。

## 验收

`tests/test_step9_2_identity_execution.py`（17 项）：正常解析返回 IdentityResolution /
Identity→User 链路 / Identity 不存在 Fail Closed / 失败不创建（计数不变）/
User 缺失 Fail Closed / Channel 隔离（同 external_id 跨渠道独立）/
模块只依赖 Port+Domain（无 SQLAlchemy/ORM/无 create·update 写调用）/
Management Service 仍只 create/get/update / 不 import Management Service /
Repository surface 不变 / Execution ≠ Resolver / mutation（越层 import、
写调用、注入 IdentityResolver → 7.3 反向审计报红）。
全仓 `pytest -q` = 433 passed，lint 0。
