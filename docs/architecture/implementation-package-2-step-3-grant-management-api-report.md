# Package 2 / Step 3 — Grant Management API Implementation Report

> 状态：**完成**。全量回归 `2182 passed / 13 skipped / 0 failed`。
> 上游：Package 2-B §B4（三个必需动作 + 端点形态 + 状态码先例）、§B4.5（known-conversations
> 投影）、Step 1（`connection_conversations` 持久化）、Step 2（Runtime Authorization）。

---

## 1. 端点面（B4.3/B4.5 冻结形态落地）

```
POST   /api/connection-conversations                       201 / 404 / 409 / 422
GET    /api/connection-conversations?connection_id={id}    200
DELETE /api/connection-conversations/{id}                  204 / 404
──
GET    /api/channels/{channel_id}/known-conversations      200 / 404（B4.5 投影）
```

**独立资源边界**（B4.3）：授权**不**嵌在 `/connections/{id}/…` 之下——ADR-2 红线
（Connection 域不得长成授权对象）；DELETE 直接用**行 id** 定位（外部会话键含
特殊字符，不适合做路径参数）。URL 未被 2-C 冻结，本实现采用 2-B §B4.3 的记录形态。

## 2. 生产变更（11 文件）

| 文件 | 变更 |
|---|---|
| `app/application/connection_conversations.py` **NEW** | `ConnectionConversationApplicationService`：Create / List / Delete + known-conversations 投影（业务规则唯一裁决点） |
| `app/api/management/connection_conversations.py` **NEW** | 路由 + 错误→状态码映射（唯一映射点，B4.4 裁决表） |
| `app/schemas/connection_conversations.py` **NEW** | Create（`extra=forbid`）/ Response / ListResponse / KnownConversationsResponse；**无 Update schema** |
| `app/repositories/connection_conversation.py` | Port +2：`delete` / `list_by_connection` |
| `app/persistence/repositories/connection_conversation.py` | 两者实现 |
| `app/repositories/session.py` + adapter | Port +1：`list_known_conversation_ids`（distinct、滤空、稳定排序） |
| `app/application/errors.py` | +3：`DuplicateConnectionConversationError`(409) / `ConnectionConversationNotFoundError`(404) / `InvalidConnectionConversationInputError`(422) |
| `app/application/__init__.py` / `app/main.py` | 导出 + tenant-scoped `service_provider` 接线（与 identities 同一注入纪律） |

**Port 扩边的正当性**（延续 Step 1 写下的「无消费者不扩张」账本）：
`delete` / `list_by_connection` 的消费者 = B4.2 的 Delete / List；
`update` **依旧不存在**（行不可变，改授权 = 删 + 建）。

## 3. Create 校验链（错误码 = B4.4 先例，不发明）

```
1. 输入形状（正整数键 / 非空白会话键）   → InvalidConnectionConversationInputError  422
2. channel 存在性（tenant 作用域内）    → ChannelNotFoundError                     404
3. connection 存在性（tenant 作用域内） → ConnectionNotFoundError                  404
4. connection 必须属于该 channel        → InvalidConnectionConversationInputError  422
5. (channel, conversation, connection)  → DuplicateConnectionConversationError     409
```

跨租户 = 404（tenant-2 作用域内 channel/connection **等价于不存在**；DB 层还有
Step 1 的复合 FK 作第二道防线）。

## 4. ★ 核心红线：known ≠ authorized（ADR-6）

`list_known_conversations` 只**读** sessions（distinct、滤空、稳定排序）：

- 有 Session 的会话 → 可见；无 Session → 不可见；
- **GET 前后授权计数不变**（观察不产生授权状态）；
- **决定性测试**（`test_d4`）：会话已出现（Discovery 可见）但无 grant →
  `AccessAuthorizationExecutor` **仍然 DENY**；显式 Create grant 后同一会话才 ALLOW；
- 源码级守卫：投影方法（adapter + service）零写入调用（`add/delete/flush/commit/create`）。

## 5. 明确不做（B4.2 冻结，全部有守卫）

```
❌ Update / PATCH / PUT        → test_e1：路由面无任何变更方法
❌ 嵌进 Connection 子资源       → test_e2：授权路由不含 /connections/
❌ discovery 自动创建 grant     → test_d3 + test_e3
❌ bulk / priority / effect    → schema extra=forbid（合同面 422）
```

## 6. 守卫更新（13 处，全部是**面清单 +1**，无一放宽语义）

```
application 文件面 ×4（test_step8_2 / 6_5 / 6_7 / 7_1/7_3）
api/management 面   ×6（persistence_smoke / 11_6 / 11_7 / 6_5 / 6_7 / 6_1 / 7_1）
schemas 面          ×4（11_6 / 11_7 / 6_7 / 7_1）
errors 面           ×1（test_step6_5 test_e：+3 错误类）
grant Port 面       ×1（test_package_2_step_1：3→4→6 方法，update 仍不存在）
session Port 面     ×2（test_step9_3 / 9_7：+list_known_conversation_ids）
```

（部分守卫同时覆盖多面，故计数有重叠。）

## 7. 验证

```
Step 3 新增测试      21 项全绿（A×6 / B×3 / C×3 / D×5 / E×3）
全量回归             2182 passed / 13 skipped / 0 failed
                     （含 Step 2 期间偶发的 WIP-3B3-3-WS-TIMING——本轮未复现）
边界确认             未动 Runtime / Session 模型 / Identity 模型 / Worker /
                     Delivery / WeCom adapter / 3B-3.3 WIP
```

## 8. 状态判定

```
Grant Management API         →  PASS
Runtime Authorization（Step 2）→  PASS（未受影响）
PG Closure Evidence          →  ⏳ 待 PG Run
git 提交                      →  ⏳ 等确认
```
