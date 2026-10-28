# Package 2 Implementation Step 0 — Fixture Semantic Classification Report

> **OSS 发布说明**：本文为审计说明；文中引用的 `frontend/`（Web UI 源码）**不包含在当前 OSS 发行内容中**，相关引用仅用于说明被设计的前端面（见 README「Project status」）。

> 状态：**只读审计**。**Step 0 不修改任何文件**（未改生产代码、未改测试、未建 migration、未改守卫）。
> 性质：把 Package 2-C D-6 的**启发式 inventory** 转为**逐文件语义判定**。
> 方法：只读子代理逐文件读源码（85 次工具调用）+ 本人独立 grep 复核 + 3 处抽查纠偏。
>
> 术语：本报告中的「fixture」= pytest fixture **以及各测试文件自己的 seed 辅助函数**。

---

## 0. 判定口径（必须先声明，否则 GranteNeeded 会算错）

```
Authorization 插在 Identity Resolution **之后**、Routing **之前**。
granted(channel, conversation) = ∅  →  DENY（抛 UNAUTHORIZED），不进入 Routing。
```

**由此推出一个容易漏的口径**：

> `GrantNeeded = yes  ⇔  该测试期望越过 Routing`
> **含「期望 Routing 之后某阶段失败」的用例**（`ROUTING_FAILED` / `AGENT_DISPATCH_FAILED` /
> `CHANNEL_DELIVERY_FAILED`）——它们同样必须先过授权，否则会提前变成 `UNAUTHORIZED`。

反之，**在 Identity 阶段就失败的测试不需要 grant**（永远到不了 Authorization）。

| 列 | 含义 |
|---|---|
| RunsFullPipeline | 是否真的执行 Coordinator/Worker/Driver 或 HTTP 入口 |
| ReachesAuthorization | 是否驱动管线**穿过一次成功的 Identity Resolution** |
| HitsRouting | 是否（直接或经管线）到达 Routing |
| ExpectsSuccess | 是否断言成功终态 |
| GrantNeeded | ⇔ ReachesAuthorization |

---

## A. 文件级分类（55 个文件，全覆盖）

| File | Class | Full Pipeline | Reaches Authz | Hits Routing | Expects Success | Grant Needed | Reason |
|---|---|---|---|---|---|---|---|
| test_3b_3_3_wecom_bot_group_chat.py | **A** | yes | yes | yes | yes | **yes** | C 组经 Worker+Coordinator 六段跑通（c1–c4）；A/B 组是 adapter 映射单测。同文件 A/D |
| test_3b_2_2_wecom_bot_websocket_transport.py | **A** | yes | yes | yes | yes | **yes** | E1/F1 经 Worker 跑完六段（succeeded×delivered）；余为 wire 单测。同文件 A/D |
| test_acp_v0_2_artifact_implementation.py | **A** | yes | yes | yes | yes | **yes** | PersistingCoordinator+Coordinator 成功（dod5/6）；含 Artifact 值对象守卫。同文件 A/D |
| test_step10_1_2_acp_agent_adapter.py | **A** | yes | yes | yes | yes | **yes** | F 组真 Coordinator+真 ACPAdapter 跑通；余为 adapter 单测。同文件 A/D |
| test_step10_1_4_3_reuse_active_session.py | **A** | yes | yes | yes | yes | **yes** | coordinator.execute 成功（CREATE_NEW→REUSE_ACTIVE） |
| test_step10_1_4_real_agent_runtime.py | **A** | yes | yes | yes | yes | **yes** | env 门控真实 Tencent E2E，经 coordinator 成功 |
| test_step10_1_4_runtime_vertical_slice_closure.py | **A** | yes | yes | yes | yes | **yes** | B 组真 coordinator 顺序（6 段/REUSE 跳 creation）成功；A/C/D/E/F 为静态审计。同文件 A/D |
| test_step10_2_1_wecom_webhook.py | **A** | yes | yes | yes | yes | **yes** | HTTP POST→Worker→六段成功（b1/b2） |
| test_step10_2_2_outbound_e2e.py | **A** | yes | yes | yes | yes | **yes** | runtime.execute / Worker 出站全链成功（c1/c3） |
| test_step10_3_1_multi_agent_e2e.py | **A** | yes | yes | yes | yes | **yes** | coordinator.execute A/B 隔离，真 ACPAdapter（fake server）成功 |
| test_step9_8_2_runtime_coordinator.py | **A** | yes | yes | yes | yes | **yes** | happy_path 真 coordinator 成功；另含 fake-stage 顺序/失败测试。同文件 A/D |
| test_step9_8_3_first_full_runtime_interaction.py | **A** | yes | yes | yes | yes | **yes** | 第一条完整闭环 E2E，经 coordinator 成功（REUSE/CREATE） |
| test_step15_2_3_local_first_success.py | **A** | yes | yes | yes | yes | **yes** | HTTP POST local → Worker → 六段成功（E1）；含 adapter 单测。同文件 A/D |
| test_step15_5_2_external_session_round_trip.py | **A** | yes | yes | yes | yes | **yes** | 3 轮 round-trip 经 PersistingCoordinator 成功 |
| test_step10_4_2_interaction_persistence.py | **B** | yes | yes | yes | yes | **yes** | 持久化断言为主；成功路径 + routing/dispatch/delivery 失败路径都跨 Identity → 需 grant；b3（identity 失败）不需 |
| test_step10_5_1_interaction_identity_audit.py | **B** | yes | yes | yes | yes | **yes** | 身份/幂等审计（PersistingCoordinator）；成功或经 Routing 失败均需 grant |
| test_step10_5_2_interaction_identity_materialization.py | **B** | yes | yes | yes | yes | **yes** | 身份物化；b1/b2/c1 成功跨过 Routing |
| test_step10_5_3_duplicate_interaction_decision_semantics.py | **B** | yes | yes | yes | yes | **yes** | 重复决策语义；成功与 dispatch/delivery 失败跨 Identity；b2/d2（identity 失败）不需 |
| test_step10_7_1_runtime_observability_evidence_audit.py | **B** | yes | yes | yes | yes | **yes** | Evidence 审计；成功 + delivery 失败跨 Routing |
| test_step10_7_2_evidence_read_response_persistence_audit.py | **B** | yes | yes | yes | yes | **yes** | 响应持久化审计；成功 + 失败探针跨 Routing |
| test_step10_8_1_runtime_result_boundary_audit.py | **B** | yes | yes | yes | yes | **yes** | Result 边界；成功 + 失败探针跨 Routing |
| test_step10_8_3_runtime_coordinator_result_integration.py | **B** | yes | yes | yes | yes | **yes** | 直接 Coordinator 的 Result 接线；a1–a4 成功跨 Routing |
| test_step10_8_4_persistence_result_wiring.py | **B** | yes | yes | yes | yes | **yes** | 持久化 Result 接线；b1/b2 成功跨 Routing |
| test_step10_8_5_runtime_evidence_read_closure_audit.py | **B** | yes | yes | yes | yes | **yes** | Evidence 关闭审计；成功跨 Routing。**另含 1 处直接 RoutingExecutor 调用**（见 §C） |
| test_step10_9_post_10_8_step_selection_audit.py | **B** | yes | yes | yes | yes | **yes** | Step 选择审计；a1 成功跨 Routing。**另含 1 处直接 RoutingExecutor**（见 §C） |
| test_step12_9_4_17_async_runtime_reliability.py | **B** | partial | yes | yes | yes | **yes** | Driver/Worker/围栏可靠性；test_6 与 test_6_live 用真 Coordinator 成功 → 需 grant；其余用 `_OkRuntime` stub |
| test_step13_1_interaction_observability_read.py | **B** | yes | yes | yes | yes | **yes** | 只读 Observability，底层真实 Worker/Driver；S2/S5/S6 成功跨 Routing；S4（identity 失败）不需 |
| test_step12_9_4_5_worker_skeleton.py | **B** | partial | yes | yes | yes | **yes** | Worker primitive；a3 驱动真 Coordinator 成功 → 需 grant；其余 `_OkRuntime` stub |
| test_step12_9_4_6_intake_ack_migration.py | **B** | partial | yes | yes | yes | **yes** | 入站 ACK 边界；`test_http_post_acks_then_worker_executes` 用真 runtime 成功 → 需 grant；其余 `_SpyRuntime` |
| test_step12_8_2_wecom_response_url.py | **B** | no | **UNCERTAIN** | no | yes | **UNCERTAIN** | 六段**全 fake 注入**，从不走真 Identity Resolution。是否命中真 Authorization **取决于接线方式**（新注入依赖 vs coordinator 硬接线）→ §F.2 |
| test_step12_9_4_2_persistence_phase_api.py | **B** | no | no | no | yes | no | phase API；inner = `_StubRuntime`，无真管线 |
| test_step12_9_4_3_interaction_snapshot_reconstruction.py | **B** | no | no | no | yes | no | 快照/重建持久化；inner = `_StubRuntime` |
| test_step12_9_4_12_attempt_fence.py | **B** | no | no | no | yes | no | attempt fence 原语；无真 identity/routing |
| test_step12_9_4_16_production_driver.py | **B** | no | no | no | yes | no | Driver 触发语义；a3 用真 Driver+Worker 但 inner 仍 `_OkRuntime` stub |
| **test_step12_9_4_9_state_aware_duplicate_ack.py** | **B** | no | **no** | no | yes | **no** | ★ 抽查修正：`_OkRuntime` stub + 直接 `record_outcome(PROCESSING_SUCCEEDED/DELIVERED)` 播种状态 → **无真管线** |
| test_step12_6_runtime_production_composition.py | **B** | no | no | no | n/a | no | 生产组合一致性 + AST 守卫 + HTTP 握手探针；**无 text 成功消息**（仅握手/非 text）→ 不触 Authorization |
| test_step9_4_routing_execution.py | **C** | no | no | yes | yes | no | **直接** `RoutingExecutor.execute(channel_id)` ×7（见 §C） |
| test_step9_7_session_creation_execution.py | **C** | no | no | yes | yes | no | **直接** Routing ×2 + SessionCreation ×5 + SessionResolution + Dispatch |
| test_step10_3_1_routing_isolation.py | **C** | no | no | yes | yes | no | **直接** Routing ×6（见 §C） |
| test_step10_6_1_agent_selection_semantics_audit.py | **C** | no | no | yes | yes | no | 静态守卫 + **直接** Routing ×3 + SessionResolution |
| test_step9_A_runtime_slice_closure.py | **C** | no | no | yes | yes | no | 封板审计为主；**1 处直接** Routing + SessionCreation + SessionResolution |
| test_external_identity_decoupling_package_1.py | **C** | no | no | yes | yes | no | Phase 3 **直接** Identity→Routing→Session 链（1 处 Routing）；文件自述「Authorization 属 Package 2」 |
| test_cloud_0_1_3_2_runtime_tenant_fanout_gate.py | **D** | no | no | no | na | no | 架构 Gate（AST L1–L8）+ L7 持久化隔离；从不执行 Runtime |
| test_cloud_0_1_1_b_tenant_isolation_dod.py | **D** | no | no | no | na | no | schema/RLS/scope DoD；不执行 Runtime |
| test_cloud_0_1_1_b_tenant_fanout.py | **D** | no | no | no | na | no | fan-out 组合语义；不执行 Runtime |
| test_cloud_0_1_3_1_wecom_webhook_tenant_resolution.py | **D** | no | no | no | na | no | 入站租户解析；`PersistingCoordinator(inner=None)` **从不执行** |
| test_cloud_0_1_3_1b_wecom_rls_bootstrap_regression.py | **D** | no | no | no | na | no | bootstrap/RLS 回归；仅 intake |
| test_step10_2_1_inbound_adapter_contract.py | **D** | no | no | no | na | no | Application/Port 契约；仅 intake（a5 用 fake runtime 抛错） |
| test_step9_8_1_execution_ordering.py | **D** | no | no | no | na | no | 静态顺序 AST 守卫；无执行 |
| test_step10_3_1_multi_agent_isolation_guard.py | **D** | no | no | no | na | no | AST 架构守卫；无执行 |
| test_step6_7_management_closure.py | **D** | no | no | no | na | no | Management 封板 AST 审计；无执行 |
| test_step14_2_2_fresh_sqlite_schema_init.py | **D** | no | no | no | na | no | schema 初始化；`PersistingCoordinator(inner=None)` |
| test_step12_9_4_4_find_pending_pickup.py | **D** | no | no | no | na | no | 孤立 find_pending 仓库测试；无 Runtime |
| **test_cloud_0_1_5_first_user_first_success.py** | **D** | no | no | no | na | no | ★ 抽查修正：命中的 `PROCESSING_SUCCEEDED/DELIVERED` 是**前端 JS 字符串断言**（`assert 'const PROCESSING_SUCCEEDED = "succeeded";' in js`）→ 纯前端 Gate |
| test_cloud_0_1_4_1_connect_channel_ui.py | **D** | no | no | no | na | no | Connect Channel UI Gate（静态 + mount 探针）；不执行 Runtime |

### 分类统计

```
Class A  14 个   全 Runtime 成功路径 → grant 必需
Class B  22 个   其中 15 个需 grant（真管线），6 个不需（stub/无 text），1 个 UNCERTAIN
Class C   6 个   不经 Authorization → 只需适配 Routing 新输入
Class D  13 个   不因本次变更机械修改
─────────────────────────────────────────────
需加 grant 前提        29 个文件
需适配 Routing 签名      8 个文件（Class C 6 + Class B 中的 10_8_5 / 10_9）
需改动合计              35 个文件（并集）
无需改动                19 个文件
UNCERTAIN                1 个（test_step12_8_2）
```

---

## B. Class A / B 的场景前提（conversation / connection / 为什么应被授权）

绝大多数文件共用**同一套 seed 骨架**，因此按骨架分组而非重复 29 行：

### B.1 骨架 α — `alice` + `wx_a` + 单 active Connection（14 个文件）

| 项 | 值 |
|---|---|
| identity | `identities(channel_id=channel, external_id="wx_a")`，绑定 user `alice` |
| connection | `connections(channel_id=channel, agent_id=agent, status="active")` —— 该 channel 上**唯一** active |
| conversation key | `external_conversation_id` = `"conv-1"` / `"conversation-X"` / `"user-A"` / `"conversation-acp"` / `"conv-rt"`（各文件字面量不同，语义相同：**该 channel 上的一个会话**） |
| 为什么应被授权 | 测试的全部意图就是「这个会话的消息应该被这个 Agent 处理」。在 default-deny 下，这个意图**必须显式表达为一条 grant**；否则测试断言的是一个「未被允许的运行」，与它自己的名字/目的矛盾 |

**属于骨架 α**：`test_acp_v0_2...` · `test_step10_1_2` · `test_step10_1_4_3` · `test_step10_1_4_real` ·
`test_step10_1_4_vsc` · `test_step10_4_2` · `test_step10_5_1` · `test_step10_5_2` · `test_step10_5_3` ·
`test_step10_7_1` · `test_step10_7_2` · `test_step10_8_1` · `test_step10_8_3` · `test_step10_8_4` ·
`test_step10_8_5` · `test_step10_9` · `test_step9_8_2` · `test_step15_5_2` · `test_step13_1` ·
`test_step12_9_4_5` · `test_step12_9_4_17`

> 注意：**同一份骨架在 20+ 个文件里被复制粘贴**。这是 `authorized_conversation(...)` 存在的直接理由
> ——把「授权前提」从隐性意图变成一行显式调用。

### B.2 骨架 β — 多渠道 / 多身份矩阵（3 个文件）

| 文件 | conversation key | connection | 为什么应被授权 |
|---|---|---|---|
| `test_step10_3_1_multi_agent_e2e` | A/B 两套 conversation | 2 channel × 2 connection（各 1 active） | 证明 **A 的会话只能走 A 的 Connection**。两侧都需 grant：这正是「授权不越界」的正向证明 |
| `test_step10_5_2` | `conv-a` / `conv-b` | 2 channel 各 1 connection | 身份物化跨渠道不合并；两侧都需 grant |
| `test_step9_8_3` | alice/bob 各一套 | 2 identity + 2 conn（1 active） | 首条完整闭环；需 grant 才能进入 REUSE/CREATE 分支 |

### B.3 骨架 γ — Webhook / HTTP 入口（5 个文件）

| 文件 | conversation key | connection | 为什么应被授权 |
|---|---|---|---|
| `test_step10_2_1_wecom_webhook` | `zhangsan`（single：`from.userid`） | wecom channel 的唯一 active | 验签通过后应被处理 |
| `test_step10_2_2_outbound_e2e` | `zhangsan` | 同上 | 出站全链 |
| `test_step3b_2_2`（WS） | `userid` | wecom_bot channel 唯一 active | WS 回帧依赖执行成功 |
| `test_step15_2_3`（local） | `local-user-1` | local channel 唯一 active | 本地首成功 |
| `test_step12_9_4_6` | `zhangsan` | 唯一 active | `test_http_post_acks_then_worker_executes` |

### B.4 骨架 δ — 群聊（1 个文件，语义特殊）

| 文件 | conversation key | connection | 为什么应被授权 |
|---|---|---|---|
| `test_3b_3_3` | **`chatid`**（`CHAT-G1` / `CHAT-G2`） | wecom_bot channel 唯一 active | 这是 **ADR-3「G1 → C1」的原型场景**：一个群、两名成员（user-a/user-b）、**一条** grant（群 → 连接），而**不是**逐人授权 |

**这一条是 Package 2 必须保留的语义锚点**：grant 的粒度是「群」，不是「群成员」。
`test_c3_group_session_matrix_case_c_different_user`（不同 user、同群）将来必须仍然通过
——它证明「同一 grant 覆盖群内不同成员」。

---

## C. Class C 明细（旧 Routing 调用 → 新输入；**未修改代码**）

```
旧：RoutingExecutor(uow_factory=...).execute(channel_id)
新：候选集 = active(channel) ∩ granted(conversation) 作为 Router 的输入（唯一性判定不变）
```

| 文件 | 旧调用形式 | 调用点数 |
|---|---|---|
| `test_step9_4_routing_execution.py` | `_executor(db_engine).execute(g["channel"])` / `.execute(bad)` | **7** |
| `test_step10_3_1_routing_isolation.py` | `_routing(db_engine).execute(paths["channel_a"])` / `routing.execute(99999)` | **6** |
| `test_step10_6_1_agent_selection_semantics_audit.py` | `RoutingExecutor(_uow_factory(db_engine)).execute(g["channel"])` | **3** |
| `test_step9_7_session_creation_execution.py` | `RoutingExecutor(_uow_factory(db_engine)).execute(g["channel"])` | **2** |
| `test_step9_A_runtime_slice_closure.py` | `rr = RoutingExecutor(uow).execute(ch_id)` | **1** |
| `test_external_identity_decoupling_package_1.py` | `RoutingExecutor(uow_factory=lambda: _uow(db_engine)).execute(channel.id)` | **1** |
| *（Class B 中顺带直接调用的 2 处）* `test_step10_8_5` / `test_step10_9` | `RoutingExecutor(_uow_factory(db_engine)).execute(g["channel"])` | **2** |
| **合计** | | **22 处** |

**不得通过创建 Grant 来「修复」Class C**：它们根本不经过 Authorization。
它们需要的是**输入适配**。

---

## D. Blanket Grant Audit

| 检查项 | 结论 | 证据 |
|---|---|---|
| 是否已有 `global authorization fixture` | **无** | 全 `backend/**/*.py` 正则 `authorize_all｜authorize_everything｜grant_all｜allow_all｜auto_authorize｜authorized_conversation` → **0 命中**（本人独立执行） |
| 是否已有 `authorized_conversation` | **无** | 同上 0 命中 |
| 是否存在 `autouse=True` fixture | **无** | 同一次 grep 覆盖 `autouse` → **0 命中**；`tests/conftest.py` 仅 `db_engine` / `db`，两者都不是 autouse |
| 是否存在「为了让测试通过而默认授权全部」的路径 | **无** | 仓库当前**根本没有授权写入路径**（Authorization 尚未实现）；ADR guard `test_adr6_no_implicit_grant_on_first_message` 断言 `AuthorizationGrant` / `AutoGrant` / `AuthorizationDecision` 类**当前不存在** |
| 是否有测试绕过 Coordinator 后错误依赖 Authorization | **无** | Class C 的 6 个文件直接调组件，**完全不涉及授权前提**；它们不是「被全局 grant 放行」的对象 |

### D.1 一个必须记录的**命名碰撞**（影响 G-5 / G-1 守卫的写法）

```
backend/app/application/browser_auth.py:86    class BrowserSessionGrant        ← 浏览器登录凭据，与授权无关
backend/tools/closure_evidence.py:156-158     SQL: GRANT USAGE ON SCHEMA ...   ← PostgreSQL 权限语句
```

**后果**：任何「扫描 `grant` 子串」的守卫都会**误报**这两处。
因此 G-5 / G-1 必须沿用本仓库既有的 AST 纪律（精确标识符匹配），而不是子串扫描。
这同时**验证了 Package 2-C 的命名选择**：新概念不应在标识符里用 `Grant`。

---

## E. 变更预算

> 全部为**预期**，Step 0 未落任何一项。

### E.1 Production files expected to change（≈20 个，其中 7 个新建）

| # | 文件 | 变更 | 早前计划是否覆盖 |
|---|---|---|---|
| 1 | `app/domain/activity_record.py` | `+ STAGE_AUTHORIZATION` | ✅ |
| 2 | `app/application/errors.py` | 新增 typed error | ✅ |
| 3 | `app/application/<authz>.py` | **新建**执行组件 | ✅ |
| 4 | `app/application/interaction_recording.py` | `STAGE_FAILURE_MAP` 注册 | ✅ |
| 5 | `app/application/runtime.py` | `RUNTIME_EXECUTION_ORDER` +1 | ✅ |
| 6 | `app/application/coordinator.py` | 新增阶段 | ✅ |
| 7 | `app/application/routing.py` | 候选集收窄 | ✅ |
| 8 | **`app/application/__init__.py`** | 导出新组件 | ❌ **Step 0 新增** |
| 9 | `app/domain/<值对象>.py` | **新建** | ✅ |
| 10 | **`app/domain/__init__.py`** | 导出 | ❌ **Step 0 新增** |
| 11 | `app/repositories/<新端口>.py` | **新建** | ✅ |
| 12 | **`app/repositories/unit_of_work.py`** | UoW **Protocol** 新增属性 | ❌ **Step 0 新增** |
| 13 | `app/persistence/unit_of_work.py` | UoW 实现新增属性 | ✅（原写作「Port」） |
| 14 | `app/persistence/models/<新表>.py` | **新建** | ✅ |
| 15 | **`app/persistence/models/__init__.py`** | 注册模型 + `__all__` | ❌ **Step 0 新增** |
| 16 | `app/persistence/mappers/<新>.py` | **新建** | ✅ |
| 17 | `app/persistence/repositories/<新>.py` | **新建** | ✅ |
| 18 | `app/api/management/<新路由>.py` + `app/schemas/<新>.py` | **新建** | ✅ |
| 19 | `app/main.py` | 组合根注入 | ✅ |
| 20 | 只读投影（known-conversations）落点 | 待实现裁决 | ✅ |

### E.2 Tests expected to change（35 个）

```
需加 grant 前提：   29 个（Class A 14 + Class B 15）
需适配 Routing 签名： 8 个（Class C 6 + Class B 2）
并集：              35 个
无需改动：          19 个（Class D 13 + stub 型 Class B 6）
UNCERTAIN：          1 个（test_step12_8_2）
新增测试文件：        ~1–2（Targeted Tests 7 项）
共享夹具：           tests/conftest.py 新增 authorized_conversation(...)（**非 autouse**）
```

### E.3 Architecture guards expected to change

| 守卫 | 变更 |
|---|---|
| `test_adr_authorization_and_access_model.py` | M5 / M6 / M7（按 ADR-11 显式授权）+ 新增 G-1 ~ G-6 |
| `test_step8_2::EXPECTED_APPLICATION_FILES`（`:46` + `:247-252`，**精确等值断言**） | **新 application 模块 → 必破**，须加入新 stem |
| `test_step9_8_1::CANONICAL`（`:43-44`）+ `test_order_is_canonical_six_stages`（名字含 "six"） | 6 → 7 段 |
| `test_cloud_0_1_1_a::test_runtime_execution_order_frozen` | 常量 +1 |
| `test_cloud_0_1_1_b_tenant_isolation_dod::test_dod_schema...`（`:55-56`，**精确表集合**） | 新表 → 必破，加入表名 |
| `test_cloud_0_1_1_a::test_fresh_pg_after_upgrade...`（PG-gated，**精确表集合**） | 同上 |
| `test_step9_1_runtime_execution.py`（18 处引用） | 顺序集合 |
| `test_step9_A` / `test_step10_1_4` / `test_step9_8_2` / `test_step10_8_2/3/4` / `test_step10_6_1` / `test_step10_7_2` / `test_step10_4_2` | 集合显式更新（spy trace / failure-map 覆盖） |
| **`test_step7_3::_next_stage_problems()`**（被 ~10 个文件当变异backstop 复用） | ⚠️ **必须验证**：新 application 模块是否被它当成「下一阶段实体偷渡」而报红 |
| `test_step6_7_management_closure` / `test_step10_4_1`（domain 文件名断言） | ⚠️ **必须验证** |

### E.4 Migration files

```
新增：  1 个（0007_connection_conversations_and_backfill，revises 0006）
不动：  0001 – 0006（含 Package 1 的 0006）
```

### E.5 Unexpected files（Step 0 才发现，早前计划未列）

| # | 文件 | 为什么必须变 |
|---|---|---|
| 1 | `app/application/__init__.py` | 本仓库以 `from app.application import XxxExecutor` 导出执行器（9.2 起的既有模式） |
| 2 | `app/domain/__init__.py` | 同上（`from app.domain import IdentityResolution` 有守卫断言） |
| 3 | `app/persistence/models/__init__.py` | 新 ORM 必须注册进 `Base.metadata`，否则 `create_all` 与守卫都看不到它 |
| 4 | `app/repositories/unit_of_work.py` | UoW **端口**（Protocol）需新增 repo 属性；它是**两个**文件，早前只记了一个 |
| 5 | `backend/tests/conftest.py` | 共享 `authorized_conversation(...)` 的落点 |
| 6 | `backend/tools/closure_evidence.py` | 它负责给 `APP_ROLE` 授权（`GRANT ... ON ALL TABLES`）——新表在被 `ALL TABLES` 覆盖前**不存在**，需确认执行顺序；如它显式枚举表/RLS 则必须改 |
| 7 | `frontend/connect-agent.html` / `connect-agent.js`（+ 可能 `connections.*`） | ADR-10 的 "Where should it work?" 是**用户可见面**；若 UX 与 Package 2 同批，则前端也在范围内 → **需你裁决是否纳入本 Package** |
| 8 | `test_step8_2::EXPECTED_APPLICATION_FILES`（守卫内嵌清单） | 见 E.3 |

---

## F. 审计发现

### F.1 对我自己启发式的两处**修正**（诚实记录）

| 早前说法 | 实情 |
|---|---|
| 「S = 断言成功终态的 11 个文件」 | S 信号**不可靠**：`test_cloud_0_1_5` 命中是**前端 JS 字符串**；`test_step12_9_4_9` 是 **stub + 直接播种状态**。二者都不走真管线 → **S 是假阳性来源**，不能当「需 grant」的代理指标 |
| 「55 个文件触及 Runtime 链条」 | 数量对，但**类别差异极大**：实际需改动的是 **35** 个，**19** 个完全不需要 |

### F.2 一个**影响变更预算的接线决策**（必须在你批准时一并裁决）

```
Authorization 是「第 7 个注入依赖」还是「coordinator 内硬接线」？
```

| 选项 | 后果 |
|---|---|
| **注入第 7 依赖**（与既有 6 段一致） | 所有**真 Coordinator 构造点**必须补参数（`test_step9_8_2` / `test_step10_1_4*` / `test_step10_8_3` / 组合根）；但 `PersistingCoordinator(inner=<fake>)` 类不受影响 → **预算 = 35 内** |
| **coordinator 内硬接线** | 违反既有注入纪律（coordinator 不得 import persistence/Repository 标识符）；但 fake-stage 测试（`test_step12_8_2`）无需改动 |

**我建议注入第 7 依赖**（与 6 段完全同形，且满足 coordinator 的依赖守卫）。
若采纳，`test_step12_8_2` 的 UNCERTAIN 将解析为 **no change**（它 fake 的是 inner runtime 的六段，
不构造真 Coordinator 的七段——需实现步确认构造点）。

### F.3 一条**必须保留的语义锚点**

`test_3b_3_3::test_c3_group_session_matrix_case_c_different_user`（**同群、不同成员**）在
Package 2 之后**必须仍然通过**——它机器化地证明「grant 的粒度是群会话，不是群成员」，
即 ADR-3 的核心承诺。任何让该用例需要「逐成员授权」的实现都是错的。

---

## G. Step 0 声明

```
✅ Step 0 未修改任何文件（生产代码 / 测试 / migration / 守卫 全部未动）
✅ 未新增 authorized_conversation(...)
✅ 未为「全绿」给任何测试加 Grant
✅ 只读：仅本报告一个新增文档
❌ 未裁决接线方式（§F.2 提为决策项）
❌ 未裁决前端是否纳入 Package 2（§E.5 #7）
❌ 未开始实现
```

**下一步（等你裁决 §F.2 与 §E.5 #7）**：ADR-11 签发 → 实现
（`0007` → ORM/port/adapter → 阶段 → routing/coordinator → 管理面 → 35 个测试改造 → Targeted Tests）。
