# Step 5.3 —— Eight Core ORM Models

一句话目标：**把 4.2 冻结的 8 个 Persistence Model 映射成 SQLAlchemy ORM，
证明这 8 张表确实表达了 Control Plane 的数据关系，且没有偷偷长出新的业务概念。**

本阶段只做 Schema。**不做** Repository / Mapper / Service / Resolver /
Routing / Capability / Permission / Tenant / AuditLog / Trace / Event /
Conversation / Message / Memory / Context / AgentRuntime / MCPServer。

链路：

```
Frozen Persistence Model (4.2)
        ↓
SQLAlchemy ORM Model        app/persistence/models/*.py
        ↓
Base.metadata               注册点（persistence/models/__init__.py 导入即注册）
        ↓
8 tables
```

---

## 1. 关系（一次建立完整）

```
User ──1:N── Identity ──1:N── Session ──1:N── Interaction ──1:N── Activity
            │                         │
            └─N:1─ Channel            └─1:N─ Activity
                                        │
Channel ──1:N── Connection ──N:1── Agent

Session: N:1 Channel / N:1 Connection / N:1 Agent（identity/channel 必填，
connection/agent 可空——4.2 未冻结 nullability，MVP 取宽松，由 5.4 用例决定）
```

**MVP 用「FK + 正确 Schema」，不构造 relationship() Object Graph。**
需要哪些 relationship 由 5.4 Repository 的真实查询决定——现在做了只会
得到一套没人用的大对象图（5.3.9 明确禁止为关系而关系）。

## 2. 关键设计点

| 主题 | 决定 |
| --- | --- |
| ID | 一律 `INTEGER PRIMARY KEY`（自动自增）。不引入 UUID/ULID/雪花——"Control Plane 未来是分布式"不等于"MVP 现在就按分布式设计" |
| 时间 | `created_at` / `updated_at`，`DateTime(timezone=True)` + `server_default=func.now()`，`updated_at` 带 `onupdate`。SQLite 的 `CURRENT_TIMESTAMP` 即 UTC。**activities 只有 created_at（append-only）** |
| JSON 列 | `config`、`metadata`（列名）、`request_payload`、`response_payload` 用 JSON。**关系型标识一律 FK，绝不进 JSON** |
| `metadata` 列 | SQLAlchemy Declarative 保留属性名 `metadata`，ORM 属性统一命名 `extra_metadata`，DB 列名仍是冻结 schema 的 `metadata` |
| 凭据 | `agents` / `connections` 只有 `credential_ref`（外部 Secret 的引用），**明文绝不入库**（测试守护：列名不含 secret/password/token/api_key） |
| sessions | `external_session_id` = Agent 侧标识（不是本表替代品）；`external_conversation_id` = Channel 侧 conversation 上下文。Control Plane Session ≠ Agent Conversation ≠ Channel Conversation |
| interactions | `processing_status` 与 `delivery_status` 保持两个独立状态，**不合成 status**（processing=SUCCEEDED + delivery=FAILED 合法） |
| activities | append-only：无 updated_at；ORM 不提供"更新 Activity"机制；interaction_id 可空（允许会话级活动） |

## 3. 唯一约束（DB 级，测试用真实重复插入验证）

- `identities`：`UNIQUE(channel_id, external_id)`
  同一个 Channel 中，一个外部身份只能对应一个 Control Plane Identity。
- `connections`：`UNIQUE(channel_id, agent_id)`
  Channel A 可接 Agent X / Agent Y，但同 Channel→同 Agent 只有一条 Connection
  （真实需求出现后再放开）。

## 4. 外键完整性（重要工程点）

**SQLite 默认不启用外键约束**（`PRAGMA foreign_keys` 默认 OFF）——如果不处理，
FK 在 SQLite 上形同虚设。因此 `database.py` 在 Engine 连接级注册了监听器，
对所有 SQLite 连接执行 `PRAGMA foreign_keys=ON`（PostgreSQL 外键默认强制，不受影响）。
测试用悬挂引用真实插入验证被拒（`IntegrityError: FOREIGN KEY`）。

## 5. Schema 一览（列名严格按 4.2）

见各模型文件 docstring 与 `tests/test_models.py::test_b_*`（逐表断言 PK/FK/必填/
nullable/默认值/时间戳/JSON/唯一约束）。

## 6. Alembic 仍未引入

8 张表已落地，但 MVP 仍用 `Base.metadata.create_all`（测试与开发库已满足）。
**引入 Alembic 的触发条件**：出现第一个"需要演进已有库 schema 的真实变更"，
或首次真实部署需要可重复建库——届时再引入，不提前背迁移工具链的债。

## 7. 验收结果

`python -m pytest -q` → **45 passed**（新增 `tests/test_models.py` + 共享 `tests/conftest.py`）：

| 项 | 结果 |
| --- | --- |
| 8 ORM Models | ✅ `app/persistence/models/` 8 个文件 |
| PK / FK | ✅ 逐表断言，含 FK 指向（target_fullname） |
| Unique Constraints | ✅ UNIQUE(channel_id, external_id) 与 UNIQUE(channel_id, agent_id) DB 级拦截重复 |
| Metadata 精确 8 表 | ✅ `set(Base.metadata.tables) == {8 张}`（多/少都失败） |
| 关系完整性（Test E） | ✅ User→Identity→Session→Interaction→Activity + Channel/Connection/Agent 全链建库回读 |
| Context Isolation（Test F） | ✅ Identity A×conv-A/conv-B 与 Identity B×conv-A 互不合并 |
| FK 悬挂引用拒绝（Test D） | ✅ 依赖 PRAGMA foreign_keys=ON |
| ORM ↔ Domain 边界 | ✅ domain 仍空壳，AST 防线含 persistence 反向禁令 |
| Mapper / Repository 未提前实现 | ✅ `mappers/`、`repositories/` 仅 `__init__.py` |
| 全量回归 | ✅ 45 passed（5.1/5.2 断言按演进同步翻转） |

实现期踩过的坑（留档）：
1. `column.foreign_keys` 里的 `ForeignKey` 没有 `referenced_table` 属性——改用
   `fk.target_fullname.partition(".")[0]` 解析指向表名。
2. 5.2 测试的「metadata 为空 / 无业务表」断言在 5.3 合法关闭缺口后翻转
   （审计断言缺口 → 实现步关闭 → 更新前置测试，沿用既有纪律）。

## 8. 下一步

```
5.4  Repository ──> 真实数据库读写
     app/repositories（端口）↔ app/persistence/repositories（SQLAlchemy 实现）
     需要 relationship 时再按真实查询补，而不是现在预置
```
