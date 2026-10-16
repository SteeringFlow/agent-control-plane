# Package 2 / Step 4 — Architecture Guards + Authorization Contract Closure

> 状态：**完成**。全量回归 `2198 passed / 13 skipped / 0 failed`。
> 本步**零生产代码改动**——只做守卫收口（纯审计件）。

---

## 1. 为什么需要这一步

授权能力是分三步长出来的（Step 1 持久化 / Step 2 Runtime / Step 3 管理面），
每一步的守卫都长在各自的文件里。Step 4 做两件事：

1. **修订已经过期的 ADR 守卫**（它们写于「授权尚未实现」时）；
2. 把散落的契约收敛为**一处可执行的收口清单**，并自检清单不会静默消失。

## 2. ADR 守卫的 4 处显式修订（**无一放宽语义**）

| # | 守卫 | 修订前 | 修订后 |
|---|---|---|---|
| 1 | `test_adr6_no_implicit_grant_on_first_message` | 注释写「授权尚未实现是当前事实」 | 注释更新为「已实现，但三条断言一字不放宽」——授权在 ACP 里不是 decision 实体，而是**一行** `ConnectionConversation` |
| 2 | `test_adr7_authorization_must_precede_session_resolution` | 只锁 `identity < routing < session`（顺序**基座**） | 升级为锁 **ADR-7 的实现位置**：`identity < authorization < routing < session` |
| 3 | `test_adr8_router_consumes_eligible_connections_only` | 纯文档断言（"Eligible Connections" 在 ADR 文中） | 升级为**实现断言**：Router 的 `granted_connection_ids` 在场 + `if len(connections) != 1` 未被放宽 |
| 4 | `test_adr9_no_authorization_module_in_domain_layer` | 禁 `authorization` 模块（V0 无授权） | 语义收窄为**禁权限体系模块**，并把例外写成**精确单点**：唯一允许 `access_authorization.py`，且其字段面必须恰好 `{granted_connection_ids}` |

第 4 条值得单记：这是 Gate 2-A §M6 预警过的场景——**授权实现后，原守卫的语义
前提变了**。处理方式是「显式收窄 + 明文例外」，**不是**靠改文件名绕开守卫。

## 3. 新增收口清单：`tests/test_package_2_step_4_authorization_contract_closure.py`

16 项（13 条契约 + 3 个 mutation 探针）：

| # | 契约 | 断言要点 |
|---|---|---|
| **g1** | 结果契约 = 只有许可集合 | 字段面 `{granted_connection_ids}`；执行器类面恰 `__init__/execute`；判定输入恰 `(channel_id, external_conversation_id)` |
| **g2** | 阶段接线冻结 | `(authorization, UNAUTHORIZED, False)`；执行顺序 identity < authz < routing < session；**不借用**其他 stage 的 typed error |
| **g3** | **default-deny 是结构性的** | 空集必然 `raise`；无兜底放行写法（mutation 验证） |
| **g4** | **无 blanket authorization** | 全 app 无通配/bulk/全量授权 token；schema 字段面恰三键；**conftest 零预置**；**无 autouse fixture 创建授权**（Step 0 G-5 红线，mutation 验证） |
| **g5** | **known 永不作为授权输入** | 授权执行器不出现 sessions / Discovery 标识；判定查询只读授权表；Discovery 端点不调用任何创建方法 |
| **g6** | **Conversation → Connection，不是 → Agent** | 契约/表/查询均无 `agent_id`；授权执行器无 agent 派生逻辑 |
| **g7** | **Router 只消费 eligible** | 候选集参数必填**无默认值**；`c.id in granted_connection_ids`；Router 源码不含 `connection_conversations`；Coordinator 确实传入 |
| **g8** | **Authorization 不读 Connection 状态** | 执行器与判定查询均无 `status` / `connections` / `find_active_by_channel` |
| **g9** | **Runtime 零自动创建** | 全 app 只有管理面一处 `connection_conversations.create`；运行时链路不 import 管理面 Service |
| **g10** | **无 Identity / User 耦合** | 执行器标识符无 identity/user；判定查询参数只有会话键 |
| **g11** | **无 Update / bulk / policy engine** | Port 无 update；Service 无 update/bulk；API 无 PUT/PATCH；授权面标识符无 policy/effect/principal/priority/role |
| **g12** | 存储仍是一张普通表 | 列面禁 status/effect/priority/role/principal/identity_id/user_id/agent_id/decision |
| **g13** | **守卫清单自检** | 31 条既有守卫 node id 逐条比对（改名/删除即报红） |

**mutation 探针**（证明守卫不空转，沿用 `test_step9_4::test_g_*` 的纪律）：
`g3b`×2 注入兜底放行、`g4b` 注入「授权全部会话」入口——两者都必须报红。

## 4. 验证

```
Step 4 新增          16 项全绿
ADR 守卫（修订后）    15 项全绿
全量回归             2198 passed / 13 skipped / 0 failed
生产代码改动          0
```

## 5. 登记：`tools/closure_evidence.py` 的 stale PG evidence（**本步不修**）

按裁决留到 **Step 5 PG Closure** 一并修正。精确定位如下（3 处）：

| 行 | 现状 | 问题 |
|---|---|---|
| `:59` | `_ok("alembic head = 0002", rev == "0002")` | head 已是 **0007** → 必然 FAIL |
| `:64-66` | `expected = set(CORE_TABLES) \| {"tenants", "alembic_version"}`，标签「8 业务表」 | 表集合已 +`connection_conversations`（授权表）→ 必然 FAIL |
| `:110-116` | 「8 业务表 + tenants 均 ENABLE+FORCE RLS」 | 新表也必须有 RLS → 需一并纳入 |

**注意**：这是**工具脚本**（非测试套件成员），因此不影响本地/CI 回归结论——
但它会在 Step 5 的 PG Run 里立刻暴露，必须在同一步修正。

## 6. 路线图定位

```
Step 4  Guards + Contract Closure        ✅ 本步（提交待确认）
   ↓
Step 5  PostgreSQL Closure               ⏳ 下一步（含 closure_evidence.py 校正）
   ↓
Step 6  Full Regression                  ⏳
   ↓
Step 7  Real WeCom E2E                   ⏳ ← Package 2 最终验收
```

**Step 7 为什么是最终验收**：它把最初的问题一路闭环到生产行为——

```
ACP 管理员绑定 Bot → Bot 进入群
   → 未绑定 ACP User 的群成员发消息
   → External Identity(user_id = NULL)        ← Package 1
   → Conversation Grant（显式授权）            ← Step 1 / Step 3
   → Authorization ALLOW                      ← Step 2
   → Agent 正常响应                            ← 既有 runtime
```

这条链任一环缺失，Package 1–3 的意义都不成立；它是「身份可以未绑定」+
「授权按会话而非按人」两个裁决的**联合生产验证**。
