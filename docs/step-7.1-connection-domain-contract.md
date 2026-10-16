# Step 7.1 —— Connection Domain Contract

一句话目标：**把 Connection 定义成一个稳定、最小、不会吞掉未来 Authorization /
Runtime 职责的 Domain Entity**（只交付 Contract 层；Application Service 在 7.2）。

## 1. 核心定位（冻结）

```
Connection = Control Plane 中一个 Channel 与一个 Agent 之间的受控访问路径
            （Access Path）
```

不是一次运行、不是权限策略。因此：

```
Connection ≠ Session / Interaction / Router / Agent Runtime
           ≠ Capability / Policy / Permission / Access Control Policy
```

### 冻结的 Domain 契约（直接沿用 4.2 Persistence 字段，不加字段）

```python
@dataclass(frozen=True)
class Connection:
    channel_id: int          # ← immutable binding
    agent_id: int            # ← immutable binding
    name: str                # 显示名，不承担唯一性
    id: int | None = None
    status: str = "active"   # Access Path 可用态（不是 HTTP / processing 状态）
    config: dict | None = None
    credential_ref: str | None = None
    created_at: datetime | None = None   # DB 回显，只读
    updated_at: datetime | None = None
```

## 2. 关系与唯一性

```
Channel 1 ──── N Connection N ──── 1 Agent
User ── Identity ── Channel ── Connection ── Agent   （静态管理关系）
UNIQUE(channel_id, agent_id)                          （4.2 冻结，继续保留）
```

| 场景 | 结论 |
| --- | --- |
| Channel A → Agent X | ✅ |
| Channel A → Agent Y | ✅ |
| Channel B → Agent X | ✅ |
| Channel A → Agent X（第二条） | ❌（MVP：一对端点只有一条受控 Connection；未来真的出现 production/staging 需要再重新设计） |

注意：**Connection 不直接拥有 Identity**（MVP 无 `Identity → Connection` 或
`Connection → Identity` 关系）；Runtime 时才经
`Identity → Session → Connection → Agent` 串起来。

## 3. 关键语义冻结

- **name**：管理员显示名，不承担唯一性 → Port **无 find_by_name**、schema 无
  name 唯一约束；
- **status**：该 Access Path 当前是否可用（active/inactive 字符串）；
  ≠ HTTP status，≠ `Interaction.processing_status`；
- **config**：只能保存 Channel→Agent 连接本身的配置
  （如 `{"timeout": 30, "protocol_options": {}}`）；禁止长成
  `permissions/capabilities/routing_rules/users/tools/memory` 万能容器；
- **credential_ref**：只存引用（`"cred_xxx"`）；解析/存储/加密属于
  Credential/Adapter/Infrastructure 边界，MVP **不做 Credential Management**，
  Domain 无 credential/token/password/secret 字段；
- **immutable binding**：`channel_id/agent_id` 不可 Update——
  改成别的 pair 是「删除旧 Access Path + 建立新 Access Path」，不是更新
  （与 6.6「Identity Update ≠ Identity Linking」同一架构思想）；
- **不负责**「谁有权限用 Agent」：Connection 只是 Access Path，
  Authorization / Policy / Capability 以后单独做。

## 4. 交付内容

| 文件 | 变化 |
| --- | --- |
| `app/domain/connection.py` | **新增** Domain Entity（含契约 docstring） |
| `app/repositories/connection.py` | **新增** Port：`get_by_id/create/update/find_by_channel_and_agent`（无 find_by_name） |
| `app/persistence/mappers/connection.py` | **新增**：to_domain/to_orm/apply_to_orm（apply 绝不覆写绑定） |
| `app/persistence/repositories/connection.py` | **新增** Adapter（只 flush 不 commit） |
| `app/application/errors.py` | + ConnectionNotFoundError / DuplicateConnectionError / InvalidConnectionInputError（7.2 消费） |
| `repositories/unit_of_work.py` / `persistence/unit_of_work.py` | + `connections` 仓储接线 |
| 各层 `__init__.py` | 导出更新 |

Tests：`tests/test_step7_1_connection_domain_contract.py`（17 项）。

## 5. 暂不交付（下一小步 / 后续）

```
7.2  Connection Management Application Slice（Service + 输入契约 + 编排）
7.3  Connection Consistency / Boundary Audit
后续  API / Router / Resolver / Dispatcher / Adapter / Session / Interaction
     / Authorization / Credential Management
```

## 6. 验收结果

`python -m pytest -q` → **250 passed**（233 回归 + 7.1 新增/演进）：

| 项目 | 结果 |
| --- | --- |
| Domain 字段面 = 冻结契约 + frozen | ✅ |
| 非 Access Control 容器（无 permissions/capabilities/...字段） | ✅ |
| status 是 str；无 credential/token/secret 字段 | ✅ |
| UNIQUE(channel_id, agent_id) 存在；name 无唯一约束 | ✅ |
| Port 无 find_by_name；apply_to_orm 不覆写绑定 | ✅ |
| Adapter：create/get/find_by_channel_and_agent/update 行为 | ✅ |
| 多对多但每对一条（A→X/A→Y/B→X 三条） | ✅ |
| DB UNIQUE 硬兜底（重复 pair → IntegrityError） | ✅ |
| update 只改可变字段、绑定不可变（含"误改绑定"不生效） | ✅ |
| Repository 不 commit（未提交回滚不落库） | ✅ |
| 反向：无 ConnectionService / application 文件面固定 / adapters/api 空壳 | ✅ |

同步演进的断言：5.7 closure（SLICE_ENTITIES + connection；A3 允许 datetime）、
6.5 audit（EXPECTED_ERRORS + 3；REMAINING 去 connection）、6.7 closure
（test_j 改为"只交付契约层"，反向审计把 connection 移出下一阶段禁入集）、
test_unit_of_work / 6.1（UoW 成员 + connections）、test_repositories。
