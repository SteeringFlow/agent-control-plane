# Step 8.3 —— Session Resolution Contract（不实现 SessionResolver）

一句话目标：冻结「给定已解析成功的 Identity + 一个**确定的** Connection +
Channel-side conversation，Runtime 应该得到哪个 Session」这一契约，并回答
「Connection 归属」的架构问题。

    Resolved Identity / User（8.2）
              +
    Connection（已确定，Access Path）
              +
    external_conversation_id
                  │
                  ▼
    Session Resolution（本步骤冻结契约；SessionResolver 属 Runtime，后续实现）
                  │
                  ▼
    REUSE_ACTIVE：复用该 Connection 上已存在的 active session
    CREATE_NEW  ：没有 active session → 需要在该 Connection 上建立新会话

## 1. 契约（冻结在 `app/domain/session_resolution.py`）

纯 domain 值对象（只 import dataclasses），字段 = 恰好 5 个：

| 字段 | 类型 | 必填 | 含义 |
| --- | --- | --- | --- |
| `mode` | `str` | 是 | `REUSE_ACTIVE` / `CREATE_NEW` |
| `identity_id` | `int` | 是 | channel-scoped 的 `Identity.id`（8.2 解析结果），**不是 User.id** |
| `connection_id` | `int` | 是 | **已确定**的 Connection（Access Path）id |
| `external_conversation_id` | `str` | 是 | Channel-side conversation |
| `existing_session_id` | `int | None` | 否 | `REUSE_ACTIVE` 时 = 复用的 active session id |

常量：`REUSE_ACTIVE` / `CREATE_NEW` / `SESSION_RESOLUTION_FAILED`（Fail Closed）。

## 2. 两条已确定的边界

| 边界 | 语义 | 锁死方式 |
| --- | --- | --- |
| **Channel 边界** | actor = `identity_id`（Identity 是 channel-scoped）；同一 User 在 WeCom / WeChat 的两个 Identity **永不共享 Session** | 契约无 `user_id` 字段；sessions 表无 `user_id` 列（只有 `identity_id`） |
| **Conversation 边界** | `external_conversation_id` 是决策的一部分；同 Identity 的 conv-001 / conv-002 是两条 Session | 字段面冻结 + sessions 表 `identity_id × external_conversation_id` 组织 |

## 3. find_active 语义（复用既有冻结，不重新发明）

```
find_active(identity_id, connection_id, external_conversation_id)
    → 命中 → REUSE_ACTIVE（不新建重复会话）
    → 未命中 → CREATE_NEW
```

Session Vertical Slice（domain Session 实体 + Repository + `find_active` 落地）
**仍未启动**——`app/domain/session.py` / `repositories/session.py` 等一律不存在。

## 4. 连接归属裁决（回答 8.3 架构问题）

> Connection 到底是在 8.3 被选出来，还是应该由 8.4 Routing 先选出来？

**裁决：Connection 不是 Session Resolution 选出来的；选择属于 Routing（8.4）。**

- Session 本身携带 `connection_id + agent_id`，但「这条 conversation 走哪条
  Connection」是 Routing 的职责：全新 conversation 由 Routing 选定 Connection
  后交给会话建立；既有 conversation 必须沿用其 active session 已绑定的
  Connection（会话粘性，不得每消息重选）；
- 因此 8.3 的输入是一个**已经确定的 Connection**：契约里 `connection_id` 是
  必填输入，且**不含** `agent_id` / 候选 / 策略 / 路由字段；
- Slice 顺序保持 8.2 → 8.3 → 8.4：8.3 冻结「拿到确定 Connection 之后的会话
  决策」，8.4 再冻结「如何选定 Connection」。

## 5. Fail Closed

- `identity.channel_id != connection.channel_id`（渠道错配）或拿不到确定
  Connection 时，**不得凭空造会话** → `SESSION_RESOLUTION_FAILED`；
- 决策 all-or-nothing：`mode` / `identity_id` / `connection_id` /
  `external_conversation_id` 全部必填无默认值。

## 6. 仍然不做

❌ SessionResolver ❌ domain Session 实体 / Session Repository / `find_active` 实现
❌ Router / Connection selection ❌ Agent dispatch ❌ Authorization / RBAC
❌ Channel Adapter ❌ API

## 7. 验收

`tests/test_step8_3_session_resolution_contract.py`：14 项全绿（纯 domain /
字段与类型冻结 / 常量冻结 / 运行时行为 / actor=identity_id / conversation 隔离 /
Session 层未启动 / find_active 未实现 / Connection 是必填输入 / 决策仅两类 /
Fail-Closed 码 / all-or-nothing / mutation 探测）。7.3 反向审计白名单扩为
精确（文件,类名）注册表并保持 mutation 防逃逸。

`python -m pytest -q` → **330 passed**（316 回归 + 8.3 新增 14）。

后续：Routing（8.4，含 Connection 选定与粘性约束）→ Session Vertical Slice →
WeCom Vertical Slice。
