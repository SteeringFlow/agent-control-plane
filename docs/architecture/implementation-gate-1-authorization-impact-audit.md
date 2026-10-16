# Implementation Gate 1 — Authorization Impact Audit（只读影响审计）

> 性质：**只读审计**。未修改任何生产代码、未建 migration、未新增表、未写实现。
> 本文件只回答「实现冻结模型的最小生产变更边界在哪里」，**不提出代码方案、不设计组件**。
> 输入裁决：`docs/architecture/authorization-and-access-model.md`（ADR-1 ~ ADR-10，FROZEN）

```
阶段状态
  Authorization Architecture      G — PASS / FREEZE   （ADR 已冻结）
  Read-only Impact Audit                          ← 本文件（Gate 1）
  Minimal Design                                  NOT STARTED
  Review                                          NOT STARTED
  Implementation                                  NOT STARTED
  Targeted Tests / Full Regression                NOT STARTED
  Real Channel Integration                        NOT STARTED
  Freeze                                          NOT STARTED
```

---

## 0. 结论先行

**§十四 的答案：是。ACP 已经存在隐含的 Authorization 语义**，且分散在四处已经生效的
机制里（tenant 隔离 / Channel 启用 / Identity 绑定白名单 / Connection active）。
新 Authorization 的净增量不是「从零造一套访问控制」，而是把其中**已经事实存在的
identity-scoped 白名单**，显式化为 ADR-3 要求的 **conversation-scoped access decision**，
并让它**可拒绝、可观测**。

**§十五 的答案：Fail Closed 的确切位置已定位**，且用户的初步判断**成立**——
「Channel Identity → 创建 external identity」的能力**已经存在**（在管理面），
Runtime 只是**选择了** fail closed。因此真正要裁决的很可能是
**Runtime 的 Identity Resolution 策略**，而不是 Identity 模型。
（本审计不下结论，只给出决策点与约束——见 §十五。）

### 变更面裁决（Gate 1 结论）

| 维度 | 裁决 | 依据 |
|---|---|---|
| **Core change**（领域语义 / 运行时策略） | **YES** | 域实体**全部不需要变化**；但 Identity Resolution 的 `fail-closed` 是**运行时策略**，其是否调整必须显式裁决（§十五） |
| **Runtime change** | **YES** | 需要新增一个「访问判定」阶段；其**插入点已定位**（§C），但形态留给 Minimal Design |
| **Persistence change** | **NO**（暂定） | V0 冻结语义下 **0 张新表**可实现；仅当 §B 三个触发条件任一成立时才升级为 YES |
| **Adapter change** | **NO** | Channel Adapter 的 Port 面没有任何策略位，且 8.1 契约明确禁止其承载策略（§D） |
| **Migration** | **NO** | 无 schema 变更；`interactions.failure_stage`(32) / `failure_code`(64) / `activities.stage`(32) 现有列已可容纳拒绝语义 |

> 「Core change」在本文件定义为：领域模型语义或运行时策略语义的变更；
> 「Runtime change」定义为：执行管线（阶段集合/顺序/失败映射）的变更。二者刻意分开回答。

---

## A. Domain — 哪个实体真正需要变化？

逐一核对 8 个实体（字段面 + 现有语义 + 是否有授权承载位）：

| 实体 | 现状（真实代码） | 是否需要变化 | 说明 |
|---|---|---|---|
| **User** | `id/name/status` | **否** | 管理面主体。ADR-1 已冻结「授权不要求 ACP User」；授权判定不消费它 |
| **Identity** | `user_id`(NOT NULL) / `channel_id` / `external_id` / `display_name` / `status` / `metadata`；唯一键 `(channel_id, external_id)` | **模型否，策略待裁决** | 模型无需变化。**待裁决的是 Runtime 解析策略**（§十五） |
| **Channel** | `name/type/id/status/config` | **否** | `status=="active"` 已在接纳层承担渠道级开关（见 §十四）；`config` 纪律禁止承载策略 |
| **Connection** | `channel_id/agent_id/name/status/config/credential_ref`；`UNIQUE(tenant_id, channel_id, agent_id)`；域 docstring 已预留「Authorization 以后单独做」 | **否** | 唯一约束不变；`config` 明确禁止长成 `permissions/...`。只有当需要「会话级子集绑定」时才涉及存储（§B） |
| **Session** | 隔离键 `Identity.id × Connection.id × external_conversation_id` | **否** | 它是**授权之后的产物**；授权位于其之前，故未授权不产生 Session（ADR-7） |
| **Interaction** | `processing_status` × `delivery_status` + `failure_stage`/`failure_code`/`retryable`；`session_id` 可空 | **否（模型）；词汇需扩展** | 列已存在且长度足够（`failure_stage` String(32)、`failure_code` String(64)）；只需新增拒绝语义的**取值**，**不需要 schema 变化** |
| **Activity** | `type`/`stage`(String(32), 可空)/`status`/`message`/`metadata`；append-only | **否（模型）；取值需扩展** | 若授权阶段产出 Evidence，新增 `stage` 取值即可；无 schema 变化 |
| **Agent** | `name/type/endpoint/status/config/credential_ref` | **否** | ADR-2 明确「不重定义 Agent 权限体系」 |

**A 小结**：**没有任何领域实体需要新增字段或改变语义**。唯一实质待决项是
**Identity Resolution 的运行时策略**（§十五），而它是策略问题、不是模型问题。

---

## B. Persistence — 是否需要新表？

### B.1 先回答「能否用 Connection 现有结构表达」

**不能。** 事实依据：

- `connections` 表的维度是 `(channel_id, agent_id)` ——**没有 conversation 维度**；
- `Connection.config` 被冻结为「只保存 Channel→Agent 连接本身的配置」，并明确
  「不是万能配置容器（不得长成 permissions/capabilities/routing_rules/...）」
  → 把「会话 → Connection」塞进 `config` 属于**违反既有冻结**，不是候选方案。

### B.2 再回答「需不需要新表」——按 V0 冻结语义推导，**暂不需要**

ADR-3 冻结的作用域是 `Conversation → Connection`，而 Connection 自身的归属是 Channel。
在 V0 的单连接形态下（`RoutingExecutor` 要求恰好 1 条 active Connection，否则 Fail Closed）：

```
eligible(conversation) ⊆ active_connections(channel_of(conversation))
```

这个集合**可由既有数据派生**（`channel_id` 已在入站事实里、Connection 已表达
channel→agent、tenant 已由 TenantScope 约束），因此 **0 张新表**即可实现
「同渠道会话默认允许使用该渠道的 Connection」。

### B.3 新表的触发条件（**任一成立才需要**，否则就是过度设计）

| # | 触发条件 | 为什么现有结构表达不了 |
|---|---|---|
| T1 | 需要**拒绝**特定 Conversation（deny / allow list） | 现有结构里「允许」是全量派生的，无法表达例外 |
| T2 | 一个 Channel 有 **N>1** Connection，需要把某个 Conversation 绑定到**子集** | `(channel_id, agent_id)` 无法表达「这个会话只能走 C1 不走 C2」；且 N>1 时现有 Routing 会直接 `ROUTING_FAILED` |
| T3 | 需要持久化「**授权关系本身**」（谁在何时授权/拒绝了哪个会话） | Interaction/Activity 能记录**一次拒绝的事实**，但记录不了**授权关系** |

**B 小结**：**Persistence change = 暂定 NO**。请特别注意——
「需要 Authorization」**不自动**推出「需要 authorizations 表」；
表的存在必须先由 T1/T2/T3 中至少一条**真实需求**证明。

---

## C. Runtime — 最小插入点在哪里？

### C.1 三段能力的真实代码边界（逐个核对）

| 阶段 | 实现 | 真实签名 / 边界 | 失败语义 |
|---|---|---|---|
| **Identity** | `app/application/identity_resolution.py`<br>`IdentityResolutionExecutor` | `execute(channel_id, external_identity_id) -> IdentityResolution(identity, user)`；只读 `find_by_channel_and_external_id` + `users.get_by_id`；**绝不创建** | Identity 未命中 / User 缺失 → `IdentityResolutionFailedError`（STOP） |
| **Routing** | `app/application/routing.py`<br>`RoutingExecutor` | `execute(channel_id) -> RoutingResult(connection_id, agent_id)`；`connections.find_active_by_channel(channel_id)` | `len(connections) != 1` → `RoutingFailedError`（STOP；`agent_id` 派生自 Selected） |
| **Session** | `app/application/session_resolution.py`<br>`SessionResolutionExecutor` | `execute(identity_id, connection_id, external_conversation_id) -> SessionResolution(mode, …)`；只读 `sessions.find_active(...)` | 输入形状非法 → `SessionResolutionFailedError`（STOP）；`CREATE_NEW` 只是决策 |
| **编排** | `app/application/coordinator.py`<br>`RuntimeCoordinator.execute` | 步骤 **1 Identity → 2 Routing → 3 Session Resolution → 4 (Creation?) → 5 Dispatch → 6 Delivery** | typed error 原样上抛（不归一） |
| **顺序常量** | `app/application/runtime.py` | `RUNTIME_EXECUTION_ORDER = (identity_resolution, routing, session_resolution, session_creation, agent_dispatch, delivery)` | **被多处测试严格断言（冻结面）** |
| **失败映射** | `app/application/interaction_recording.py` | `STAGE_FAILURE_MAP: typed error → (failure_stage, failure_code, retryable)`（唯一一处） | 覆盖性被测试锁定 |

**关键顺序事实**：Routing 之所以在 Session 之前，是**输入依赖**（`SessionResolutionExecutor`
的 `connection_id` 是必填输入，而它是 Routing 的产物）——`runtime.py` 已明确写死这个理由。
这直接决定 ADR-8 的落点：**授权必须在 Routing 之前**（否则 Routing 会先行选中一条
未被许可的 Connection，授权只能事后否决一次已做出的选择）。

### C.2 结构性发现（本次审计最重要的一条）：Runtime 是**两阶段**的

```
第 1 阶段：接纳（HTTP 请求内）        第 2 阶段：执行（响应之后）
────────────────────────────         ────────────────────────────
ChannelInboundService.accept_message   Worker.process(interaction_id)
  1. _load_channel  (URL 定位+active)    1. 读回 interaction（request_payload 快照）
  2. adapter.parse_message（验签=信任）  2. claim_processing（CAS，未取得执行权→STOP）
  3. _tenant_scope（验签之后才可信）      3. inbound_from_snapshot（忠实重建）
  4. 幂等回查 existing row               4. RuntimeCoordinator.execute(inbound)
  5. create_pending → COMMIT             5. record_success / record_failure
  6. HTTP 200（ACK ≠ 执行）
```

含义：**「未授权请求不应该进入正常 Agent Session」这条 ADR-7 约束，跨了两个阶段**。
`InboundInteraction`（含 `external_conversation_id`）已被 `request_payload` **完整快照**，
并由 `inbound_from_snapshot` **严格重建**（缺字段即 `ValueError`，不静默补全）——
所以授权所需的全部输入，**在第 2 阶段同样可得到**。

### C.3 两个候选插入点（本审计**只标注，不选型**）

| 候选 | 位置 | 满足 ADR | 代价 / 风险 |
|---|---|---|---|
| **P1 接纳期** | `ChannelInboundService.accept_message` 中 `create_pending` **之前** | 部分 | 触碰三条已冻结语义：10.2.1（入站异常不进 Runtime Failure Model）、12.9.4.6（**ACK ≠ 执行**，200 = 已持久化接收）、12.9.4.9（状态感知重复接纳一律 **ACK + STOP**）。在接纳层做访问判定，会把「许可」混进「接收」语义 |
| **P2 执行期** | `RuntimeCoordinator.execute` 中 **Identity 之后、Routing 之前** | 全部 | Interaction 已存在（PENDING→PROCESSING），拒绝经既有 `record_failure` 落 `processing=failed + failure_stage=authorization + failure_code=UNAUTHORIZED + delivery=pending`：**不创建 Session、不 Dispatch、不 Delivery，但事实被记录**——正是 ADR-6/ADR-7 要求的形状 |

**约束（无论选哪个都不变）**：

1. 授权必须在 **Session Resolution 之前**（ADR-7 硬约束）；
2. 授权必须在 **Routing 之前**（ADR-8：Router 只消费 eligible connections）；
3. 授权拒绝**不得**创建 Session / 调用 Agent / 产生 Delivery；
4. 授权拒绝**必须**可观测（Interaction 终态 + Evidence），且**不得新增 `status` 列**、
   不得合成双状态；
5. 若选择 P2，则**未授权的 interaction 已经在库中存在**——这一点与 ADR-6
   （Discovery ≠ Authorization）语义自洽：发现记录 ≠ 授权处理。

### C.4 顺序常量是冻结面：新增阶段属于「有序变更」，不是回归

新增授权阶段会触及以下四处**同源定义**，必须同步、且必须显式评审：

- `RUNTIME_EXECUTION_ORDER`（runtime.py）
- `ActivityRecord.STAGE_*`（domain/activity_record.py，六段词汇）
- `STAGE_FAILURE_MAP`（interaction_recording.py，唯一失败映射源）
- 多个**顺序断言测试**（见 §E 第 2 类）

---

## D. Channel Adapter — 是否由 Core 统一调用？是否需要 capability information？

### D.1 真实接口面（无策略位）

| Port | 文件 | 方法 |
|---|---|---|
| 入站 | `app/repositories/channel_inbound.py` `InboundChannelAdapter` | `verify_handshake(channel, *, signature, timestamp, nonce, echostr) -> str`；`parse_message(channel, *, signature, timestamp, nonce, body) -> InboundInteraction \| None` |
| 出站 | `app/repositories/channel_adapter.py` `ChannelAdapter` | `deliver(outbound: OutboundResponse) -> None` |

实现：`WeComAdapter`（验签+AES+XML）、`WeComBotAdapter`（WS 回调，`verify_handshake` 恒抛）、
`LocalChannelAdapter`（本地 JSON/JSONL）、`MultiChannelAdapter`（按 `channel.type` 分派，
未知 type Fail Closed）。**没有任何 `probe` / `capabilities` / `features` / `describe` 方法**
（对比：Agent 侧有 `ACPAdapter.probe() -> AgentProbeSnapshot`，Channel 侧没有）。

### D.2 回答 1：Authorization 是否应该由 Core 统一调用？

**是（Core 统一调用）**，依据：

1. **Port 面无策略位**——Adapter 接口里没有、也不应有访问判定入口；
2. **契约明确禁止** ——`InboundInteraction.metadata` 被冻结为「**不承载凭据 / 策略 / 路由
   信息**」，且契约本身不含 `capability` / `policy` / `routing`；
3. **职责已经分清**——Adapter 承担的是 **authentication（信任闸门）**：
   `parse_message` 的验签发生在 tenant 解析**之前**，`accept_message` 的顺序被写死为
   「定位（不构成信任）→ 验签（信任闸门）→ tenant（取自已认证对象）」；
   而 Authorization 是**信任之后的许可判定**，属 Core；
4. **ADR-4 冻结**——「Channel Native Authorization 归渠道；ACP 只处理
   Conversation → Connection 之间的 Agent access」。

### D.3 回答 2：Channel Adapter 是否需要提供 capability information？

**V0：不需要。** 依据：

- **没有接口位**：新增 capability 暴露点会直接违反 8.1 冻结的入站契约字段面
  （「不含 capability / policy / routing」）；
- **没有消费方**：ADR-9 已把 Policy Engine / ABAC 等列为 NON-GOAL，V0 的判定只需要
  `(channel_id, external_conversation_id) → eligible connections`；
- **ADR-4 已经给出未来路径**：若未来需要「渠道侧授权结论」，正确形态是
  **渠道侧决策以最小形式（会话键）投影进来**，而不是让 Adapter 暴露能力/策略对象。

> 因此本审计**不**建议扩展 Adapter 接口。若未来确需，必须先有一次独立裁决。

---

## E. Existing Runtime Tests — 哪些行为必须保持不变？

### 第 1 类：**行为必须完全不变**（语义红线，不得因引入授权而改动）

| 行为域 | 代表测试（文件 :: 函数） |
|---|---|
| 私聊全链 | `test_step9_8_3_first_full_runtime_interaction.py::test_e2e_reuse_active_full_loop` / `test_e2e_create_new_full_loop` / `test_e2e_message_round_trip_content` |
| 群聊映射与会话矩阵 | `test_3b_3_3_wecom_bot_group_chat.py::test_a2_group_mapping_identity_userid_conversation_chatid` / `test_a2b_group_conversation_follows_chatid_not_sender` / `test_c1..c4_group_session_matrix_*` / `test_c4_group_end_to_end_reply_and_duplicate_dedup` |
| Identity Fail Closed（**在策略被显式裁决前**） | `test_step9_2_identity_execution.py::test_b_unknown_identity_fails_closed` / `test_b_failure_does_not_create_identity_or_user` / `test_b_identity_without_user_fails_closed`；`test_step8_2_identity_resolution_contract.py::test_a_failure_code_frozen` |
| Session 复用 / 新建 / 隔离键 | `test_step9_3_session_execution.py::test_a_existing_session_is_reused` / `test_g_find_active_key_is_three_dims`；`test_step10_6_2_session_resolution_identity_hardening.py::test_a1_canonical_key_is_three_dimensions_no_agent_id` |
| Routing 唯一性 | `test_step9_4_routing_execution.py::test_a_zero_active_connections_fails` / `test_a_multiple_active_connections_fail` / `test_b_agent_id_derives_from_selected_connection` |
| Delivery 双状态语义 | `test_step9_6_delivery_execution.py::test_d_delivery_failure_not_processing_failure_frozen`；`test_step8_6_delivery_contract.py::test_f_two_independent_statuses` |
| 重复交互 / claim / orphan / fence | `test_step10_5_3_duplicate_interaction_decision_semantics.py::test_d1_succeeded_terminal_duplicate_is_side_effect_free_conflict`；`test_step12_9_4_12_attempt_fence.py::test_d1_old_attempt_cannot_write_through_new_attempt`；`test_step12_9_4_17_async_runtime_reliability.py::test_5_two_drivers_competing_execute_exactly_once` |
| Tenant / RLS 隔离 | `test_cloud_0_1_1_b_tenant_isolation_impl.py::test_repository_scopes_reads_and_writes_by_tenant`；`test_cloud_0_1_3_1b_wecom_rls_bootstrap_regression.py::test_post_resolution_scope_sees_only_own_tenant`；`test_cloud_0_1_3_3_multitenant_wecom_e2e.py::test_cross_tenant_execution_impossible_a_to_b` |
| interactions 双状态 + 失败三元组 | `test_step10_4_2_interaction_persistence.py::test_b6_delivery_failure_is_not_processing_failure` / `test_b3_identity_failure_recorded`；`test_step10_7_1_runtime_observability_evidence_audit.py::test_a4_dual_status_and_failure_triple_answer_what_and_where` |

### 第 2 类：**必须显式更新的是「集合」，而不是「语义」**（有序变更）

| 需要更新 | 原因 |
|---|---|
| `test_step9_8_1_execution_ordering.py::test_order_is_canonical_six_stages` / `test_routing_precedes_session_in_order` | 顺序常量新增阶段 → 断言集合必须更新 |
| `test_step10_1_4_runtime_vertical_slice_closure.py::test_create_new_path_executes_all_six_stages_in_order` / `test_reuse_active_path_skips_session_creation` | spy trace 严格等于 `RUNTIME_EXECUTION_ORDER` |
| `test_step9_8_2_runtime_coordinator.py::test_call_order_create_new` / `test_call_order_reuse_active` | 调用顺序断言 |
| `test_step9_1_runtime_execution.py::test_c_execution_ordering_locked`；`test_step9_A_runtime_slice_closure.py::test_order_is_single_source_and_canonical`；`test_cloud_0_1_1_a_postgres_alembic_foundation.py::test_runtime_execution_order_frozen` | 同一冻结常量的多处锁定 |
| `test_step10_4_2_interaction_persistence.py::test_a2_failure_map_covers_all_runtime_typed_errors` | 新增授权失败类型后，覆盖性必须同步 |
| `test_step10_1_4_runtime_vertical_slice_closure.py::test_six_failure_stages_map_to_distinct_typed_errors` / `test_each_stage_raises_only_its_own_error` | 「每阶段只抛自己的错」需纳入新阶段 |

> **纪律**：第 2 类更新必须**只改集合/枚举，不改语义**（不得顺手放宽或改写既有断言），
> 且必须在同一个变更里显式说明「新增了一个阶段」，不得静默修改。

### 第 3 类：**后续 Targeted Tests 必须新增**（本审计不写）

授权拒绝路径（不产生 Session / Dispatch / Delivery）；Conversation-scoped 允许与拒绝；
群聊 `G1 → C1 = ALLOW`；拒绝仍可观测（Interaction 终态 + Evidence）；
未绑定参与者场景（取决于 §十五 裁决）。

---

## §十四 当前 ACP 是否已存在隐含的「Authorization」语义？

**是。** 审计确认下列**已经在生效**的机制，各自承担了一部分 access control：

| # | 已存在机制 | 代码位置 | 实际承担的访问控制语义 |
|---|---|---|---|
| 1 | **Tenant isolation** | `core/tenant.py`（`TenantScope`/`IngressTenantError`）+ tenant-scoped UoW + Repository 过滤 + PostgreSQL RLS | 「只有本租户的数据可见/可写」——**最强的隔离边界**，跨租户执行被测试证明为不可能 |
| 2 | **Channel enablement** | `ChannelInboundService._load_channel` | 渠道不存在 / `status != "active"` → `InboundChannelConfigError`（500，不接纳）——**渠道级开关** |
| 3 | **Channel authentication** | `InboundChannelAdapter.parse_message`（WeCom 验签 / 解密） | 不可信请求 → 403；`None` → acknowledge-ignore——**「谁能进来」的信任闸门**（authentication，非 authorization） |
| 4 | **Identity 绑定白名单（现状事实上的授权）** | `IdentityApplicationService.create_identity`（管理面，需 `user_id`+`channel_id`+`external_id` 且唯一键预检）→ Runtime `IdentityResolutionExecutor` **Fail Closed** | **只有被管理员显式绑定过的外部身份，才能进入 Runtime 并触发 Agent**——这是一套 **identity-scoped allowlist**，即当前实际生效的授权机制 |
| 5 | **Connection 边界与启用** | `ConnectionRepository.find_active_by_channel` + `RoutingExecutor`（恰好 1 条否则 Fail Closed） | 只有 `active` 的 Connection 能被选中；`>1` 直接拒绝——**Agent 访问边界的开关** |
| 6 | Session 三维隔离 | `SessionResolutionExecutor` | 上下文隔离（防群聊/会话泄漏），**不是**授权 |
| 7 | Interaction 身份幂等 | `interactions` UNIQUE(tenant,channel,external_id) + 12.9.4.9 | 幂等/去重，**不是**授权 |

### 结论与纪律

- 新 Authorization 的**净增量** = 把 #4 的 **identity-scoped** 白名单，显式化为 ADR-3 的
  **conversation-scoped** access decision，并使其**可拒绝 + 可观测**；
- 应当**组合** #1（tenant）、#2（channel active）、#5（connection active）作为既有前提，
  **不重复实现、不替换**；
- **不得**再造与 #1/#2/#5 平行的第二套隔离/开关机制（例如「Channel 级授权表」与
  `Channel.status` 并存、或「Connection 级许可表」与 `Connection.status` 并存）。

---

## §十五 Identity Resolution Fail Closed 到底在哪里发生？

### 15.1 精确定位（真实代码，三处）

| 层 | 位置 | 形态 |
|---|---|---|
| **运行时实现** | `app/application/identity_resolution.py` :: `IdentityResolutionExecutor.execute` | 两处 `raise IdentityResolutionFailedError(channel_id, external_identity_id)`：① Identity 未命中；② 命中但 `User` 缺失 |
| **领域契约** | `app/domain/identity_resolution.py` | 冻结失败码 `IDENTITY_RESOLUTION_FAILED` + 「不做 Resolver / 不猜 / 不自动 linking」 |
| **测试锁定** | `test_step9_2_identity_execution.py`（`test_b_unknown_identity_fails_closed`、`test_b_failure_does_not_create_identity_or_user`、`test_b_identity_without_user_fails_closed`）；`test_step8_2_identity_resolution_contract.py`（`test_a_failure_code_frozen`） | 行为与失败码均为**冻结面** |

**注意**：当前**没有** Policy 对象——fail-closed 是**硬编码在 Executor 里**的一个决定，
其语义被领域契约与测试双重冻结。

### 15.2 「Channel Identity → resolve/create external identity」是否已经存在？

**存在，但在管理面，不在运行时。** 事实：

```
IdentityApplicationService.create_identity(CreateIdentity(user_id, channel_id, external_id))
    → users.get_by_id 存在性校验
    → channels.get_by_id 存在性校验
    → identities.find_by_channel_and_external_id 唯一键预检（重复 → DuplicateIdentityError）
    → identities.create + commit
```

并且该服务 docstring 明确划界：「**Identity Management ≠ Identity Resolution**」、
「不做自动 Identity Linking」。

### 15.3 因此：用户的初步判断成立——可能只需改**策略**，不改模型

推理链（基于上述事实）：

1. 「创建外部身份」的能力**已经存在**（管理面，6.6）；
2. Runtime 侧**主动选择**了 fail closed（9.2），并且是**契约冻结**的；
3. `identities.user_id` 为 **NOT NULL** → 在当前模型下，「没有 ACP User 的外部身份」
   **不可能**以 Identity 行的形式存在（它只能是「没有 Identity 行」）；
4. 所以「新模型下 External Identity 可以第一次出现」这一需求，落在现有结构上只有两种
   可能语义：
   - **语义 A**：该参与者**不建立 Identity 行**，授权**不依赖**身份解析结果
     （与 ADR-1 的口径 C2、与 §十四 #4 的「白名单」组合后自洽）；
   - **语义 B**：放宽 Identity 模型（例如允许无 User 的外部参与者）→ **会触及
     `user_id` NOT NULL 与 6.6/8.2/9.2 三处冻结**，代价显著更高。

### 15.4 本审计的结论（**不下最终结论，按用户要求**）

- **高置信判断**：需要变化的**大概率是 Identity Resolution 的运行时策略**
  （在 Core 内），而**不是** Identity 域模型；
- **必须显式裁决的点**（留给 Minimal Design）：
  1. fail-closed 是保留、放宽、还是按场景分派？
  2. 未绑定参与者是否允许走到授权判定（若不允许，ADR-3 的 `G1 → C1` 在群聊里对
     未绑定成员不成立）；
  3. 若放宽，放宽到「不阻断但记录 unresolved」，还是「完全跳过身份解析」？
  4. 无论哪种，都必须**不自动创建 Identity/User**（ADR-5 红线）且**不改 schema**
     （语义 A 路径）；
- **禁止**：在本次审计范围内改动 8.2/9.2 的冻结契约（需要独立 Gate）。

---

## §十七 审计输出表（Area / Current behavior / Required change / Scope）

| Area | Current behavior | Required change | Scope |
|---|---|---|---|
| **User** | 管理面主体（`id/name/status`）；运行时只读、不创建 | 无 | **None** |
| **Identity** | `(channel_id, external_id)` 唯一键；`user_id` NOT NULL（FK）；管理面可创建；运行时 **Fail Closed** 且绝不创建 | 模型：无。**策略**（Runtime Resolution）待裁决（§十五） | **Core（策略）** |
| **Channel** | `status=="active"` 在接纳层作为渠道开关；`config` 禁承载策略 | 无 | **None** |
| **Connection** | `UNIQUE(tenant, channel, agent)`；`status=active` 经 `find_active_by_channel` 生效；域已预留 Authorization | 无（存储仅在 §B 的 T1/T2/T3 成立时才涉及） | **None**（暂定） |
| **Session** | 三维隔离键；`find_active` → REUSE/CREATE_NEW；创建在 CREATE_NEW 分支 | 无（授权须在其之前，未授权不产生 Session） | **None** |
| **Interaction** | `processing_status × delivery_status` + `failure_stage`(32)/`failure_code`(64)/`retryable`；`session_id` 可空 | 无 schema 变化；仅需**新增拒绝取值**（如 `stage=authorization` / `code=UNAUTHORIZED`） | **Core（取值）** |
| **Activity** | append-only；`stage`(32) 覆盖六段 | 无 schema 变化；若产出拒绝 Evidence 则新增 stage 取值 | **Core（取值）** |
| **Runtime** | `RUNTIME_EXECUTION_ORDER` 六段；`RuntimeCoordinator` 步骤 1/2/3；两阶段（接纳 ACK + Worker 执行） | **需要**新增访问判定阶段（插入点见 §C.3；形态留给 Minimal Design） | **Runtime** |
| **Router** | `channel_id → 恰好 1 条 active Connection`，否则 `ROUTING_FAILED` | **需要**：Router 只消费「已授权 eligible connections」（ADR-8）；V0 仍是单连接，不开发 Multi-Agent Routing | **Runtime** |
| **WeCom / Adapter** | 入站 `verify_handshake`+`parse_message`（验签=信任闸门）；出站 `deliver`；无 capability/probe | 无（授权由 Core 统一调用；不扩展 Adapter 接口、不传 capability） | **None** |
| **Persistence** | 11 张表；`connections`/`identities`/`sessions`/`interactions`/`activities` 均无授权位 | 无（V0 可 0 表；T1/T2/T3 任一成立才新增） | **None**（暂定） |
| **Tests** | 顺序/失败映射/双状态/隔离/幂等全量锁定 | 第 1 类行为不变；第 2 类**集合显式更新**；第 3 类新增 Targeted Tests | **Tests** |

### 最终裁决

```
Core change:         YES   （不是域模型，而是 Identity Resolution 运行时策略 + Interaction/Activity 拒绝取值）
Persistence change:  NO    （暂定；触发条件 T1/T2/T3 任一成立才升级为 YES）
Runtime change:      YES   （新增访问判定阶段；插入点已定位，形态未定）
Adapter change:      NO
Migration:           NO
```

---

## §十八 本次审计**没有**做什么（防止范围失控）

按用户要求，本次工作顺序严格保持为
`Architecture Freeze → Read-only Impact Audit → Minimal Design → Review → Implementation → Targeted Tests → Full Regression → Real Channel Integration → Freeze`。

本 Gate 1 **未做、也不得在下一步之前做**：

```
❌ 未设计 AuthorizationService / AuthorizationPolicy / AuthorizationRule
❌ 未设计 Permission / Role / Scope / Principal / Resource / Effect
❌ 未新增任何类、模块、表、列、migration
❌ 未修改任何生产代码、任何测试、任何既有契约
❌ 未选择 P1 / P2 插入点（只标注候选与约束）
❌ 未裁决 Identity Resolution 策略（只定位与列出决策点）
❌ 未改动 RUNTIME_EXECUTION_ORDER / STAGE_FAILURE_MAP / Success 语义
❌ 未提出实现步骤或文件清单
```

---

## 附：下一步（Minimal Design）的输入清单

Minimal Design 必须先回答下面 5 个问题（全部来自本审计的未决项），再谈形态：

1. **插入点选 P1 还是 P2**（§C.3），以及拒绝时的 ACK 语义是否需要调整；
2. **Identity Resolution 策略**如何裁决（§15.4 的 4 个子问题）；
3. 是否需要 §B 的 **T1/T2/T3**（决定 Persistence = NO 还是 YES）；
4. 拒绝语义的**取值**（`failure_stage` / `failure_code` / `activities.stage`）——
   必须在 `STAGE_FAILURE_MAP` 单一源内扩展；
5. 第 2 类测试（顺序/失败映射）的**显式更新清单**（只改集合，不改语义）。

> 本文件为只读审计产出，**不构成实现批准**。任何实现必须先经 Minimal Design → Review。
