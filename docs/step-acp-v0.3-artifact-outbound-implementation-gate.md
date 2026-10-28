# ACP V0.3 Artifact Outbound Delivery — Implementation Gate（独立审查报告 + 实施计划）

> 状态：**READY FOR APPROVAL** —— 独立 Implementation Gate 审查完成，**D1–D5 决策已全部结案**，
> 无实施前阻塞项；**Phase 1 已于 2026-10-26 实施并逐条验收**（证据映射见 §8.1，
> 残留项见 §8.2）。本文在实施后做了一次**范围受限的对账**（§6.4.3 的 Local 表述修正
> + §8 编号与计数统一 + 新增 §8.1 / §8.2），随后完成 **R-1 Unicode 分隔符加固**
> （呈现层字符分类扩至 `Cc`/`Cf`/`Zl`/`Zp`；C6 / C17 口径**同步收紧**；**47 条全部 PASS**）——
> 除该加固所需的**呈现层**代码与用例外，**未改任何数据库 / schema / 迁移 / SDK /
> `AgentRequest` / 运行时状态机 / Delivery 语义 / Release Freeze**。
>
> **2026-10-26 最终验收审计收口**：审计确认 R-1 已闭合、47 条标准全部 PASS、R-2…R-5
> 状态措辞未误标；并**补全 §8.2 R-6 的消费者实测清单**（写端 / 读端 / 无生产读者 /
> 仓库外未核实），同时把 Phase 1 新增测试文件内 **4 处** outbox 读点由 `splitlines()`
> 改为 `split("\n")`（**仅测试文件**；既有 `test_step15_2_3_local_first_success.py`
> 的 3 处**未改**，不在 Phase 1 授权范围）。**验收标准仍为 47 条，编号与计数未变。**
>
> **2026-10-26 后续阶段验收（交叉引用）**：**ACP V0.4 Phase 2（URI 策略与安全呈现）已验收**，
> 验收范围**仅限**「URI 策略检查与安全呈现」（方案 A′）—— 契约与验收记录见
> `docs/step-acp-v0.4-artifact-uri-policy.md` §13。该阶段**未改**本文的任何实现代码、
> **未改** 47 条验收标准（仅 C12 的 import 守卫**措辞**精化，实质保护**增强**）、
> **未改** §8.2 **R-2…R-6 的状态措辞**——它们**仍为「已登记」**，
> **不得**表述为**已解决**或**已验证**。
> 上游：`docs/step-acp-v0.2-artifact-contract-gate.md`（V0.2 冻结记录，字段面未动）。
> 依据：Artifact 能力审计报告 + Phase 0 文档对账（`docs/step-acp-v0.2-artifact-contract-gate.md` 已修正事实漂移）。
> 轮次：① 独立审查（Q1–Q5 待裁决）→ ② **决策收敛（2026-10-26，Q1–Q5 全部结案）**。
> 本轮性质：**只读审查 + 决策落文档**。未修改 `backend/` 任何代码或测试、未改 schema / 迁移、未实施 Phase 1。

---

## 0. 审查基线（实测，非引用旧报告）

| 项 | 实测值 |
|---|---|
| 分支 | `master`（`origin/master`，ahead 0 / behind 0；唯一分支） |
| HEAD SHA | `a5fd8ab` — `chore(release): prepare 0.1.0-rc.10 candidate` |
| VERSION | `0.1.0-rc.10` |
| 工作树（审查开始时） | ` M README.md`（既有）<br>` M docs/step-acp-v0.2-artifact-contract-gate.md`（Phase 0 产出，未提交）<br>` M docs/step-cloud-0.1.1-B-tenant-isolation-implementation-boundary.md`（Phase 0 产出，未提交）<br>`?? 0`、`?? build`（既有未跟踪杂文件） |
| 全量后端测试基线 | **3 failed / 2839 passed / 41 skipped**（本轮实测 `pytest tests -q`，327.51s）——**与任务书 §6 所述基线逐项一致** |
| 3 项持续失败 | `test_m6_6_f4_a_browser_first_success::test_t6_to_t9_diagnosis_mapping_identical_in_home`<br>`test_m7_aui_6_channel_credential_panel::test_e8_case_a2_new_channel_uses_post`<br>`test_m7_aui_9_multi_channel_inline_connection::test_b36_panel_toggle_add_and_edit` |

**Release Freeze 状态**：V0 冻结治理文档（内部治理文档，不随 OSS 发布）仍为 **FROZEN**（§1 行 31 Delivery、§2 行 45 契约冻结、§3.3 行 88/98、§4 行 104-106）。**本轮未修改该文件**（`git diff --name-only` 实证为空），亦**未添加任何 `release-blocker` 标注**。

---

## 1. 已批准决策落文档（D1–D5）

### D1 — 版本边界：Phase 1 属 Cloud V0 发布后的独立版本 / 后续阶段

**决策内容**：Phase 1 **不得**作为未经批准的变更混入当前冻结的 Cloud V0；不修改 Release Freeze 绕过审批；不自行添加 `release-blocker` 标注。

**落点**：本文 §0（基线与 Freeze 状态）、§4（实施文件清单 → 分支约束）、§9.3（提交与分支约束）。

**对本轮的直接约束（已遵守）**：
- 未修改 V0 冻结治理文档（内部治理文档，不随 OSS 发布）
- 未添加 `release-blocker:` 标注
- 未修改版本号 / 标签 / 分支；未 commit / push / tag / 部署
- §9.3 明确 Phase 1 的实施分支为 **post-V0**，并要求在实施 PR 描述中标注「非 V0 范围变更」而非 `release-blocker`

### D2 — Delivery 契约差异：采用 S1（OutboundResponse 显式多一个 `artifacts` 字段）

**决策内容**：明确允许 `OutboundResponse` 比对应 `InboundInteraction` 契约多一个 `artifacts` 字段；**不得**把 artifacts 藏进 `metadata` 规避显式契约差异。

**落点**：本文 §3.2（实测字段集合 + 精确不变式）、§5（契约与兼容性计划）、§8（验收标准 A1–A3）。

#### 1.2.1 实测字段集合（本轮重新核实，非沿用旧报告）

```
INBOUND  (InboundInteraction,  6): channel_id, content, external_conversation_id,
                                    external_identity_id, external_interaction_id, metadata
OUTBOUND (OutboundResponse,    4): channel_id, content, external_conversation_id, metadata
AGENTRESP(AgentResponse,       4): artifacts, content, external_session_id, metadata
RESULT   (RuntimeExecutionResult,3): agent_response, session_id, session_mode
```

实测差集（当前，尚未加 `artifacts`）：
```
inbound - outbound = {external_identity_id, external_interaction_id}
outbound - inbound = {}          # ← 加入 artifacts 后应变为 {artifacts}
agent & outbound   = {content, metadata}
```

#### 1.2.2 精确不变式（D2 指定，与实测自洽 ✅）

Phase 1 实施后必须成立：

```
inbound - outbound == {"external_interaction_id", "external_identity_id"}
outbound - inbound == {"artifacts"}
```

**自洽性验证**：上表实测 `inbound - outbound` 已**恰好**等于 `{external_interaction_id, external_identity_id}`；加入 `artifacts` 到 Outbound（且**不**加入 Inbound）后，`outbound - inbound` 由 `{}` 变为 `{artifacts}`。两侧不变式**同时成立且互斥无交叠** ✅。

**前置条件（必须保持）**：`artifacts` **只**加在 `OutboundResponse`；`InboundInteraction` **不加** `artifacts`（入站附件属后续阶段，D3 同批不做）。一旦 Inbound 也加 `artifacts`，上式立即失效。

**实测差异披露（重要）**：任务书提示「如发现其他差异，报告并停止将其当作已解决」。本轮核实发现 **D2 指定的不变式与实测字段集合完全相符，无额外差异** ✅。但核实过程中发现**另一个独立的不变式会被同一改动破坏**，故 D2 的解法必须附带该不变式的修订（见下）。

#### 1.2.3 ⚠️ 附加发现：`AgentResponse ∩ OutboundResponse` 不变式被同一改动破坏

`tests/test_step8_6_delivery_contract.py:252-262`（`test_c_agent_response_not_merged_with_outbound`）断言：

```python
assert "external_session_id" not in outbound
assert not (agent & {"channel_id", "external_conversation_id", "agent_id"})
assert agent & outbound == {"content", "metadata"}
```

加入 `artifacts` 到 Outbound 后，`agent & outbound` 变为 `{content, metadata, artifacts}` → **该断言破裂**。

**这与 D2 的关系**：D2 授权的是「Outbound 比 Inbound 多一个 `artifacts` 字段」。而上面这条不变式表达的是另一件事——**「AgentResponse 与 OutboundResponse 的字段交集只应是两者共有的 `content` / `metadata`」**。`artifacts` 同时属于 AgentResponse 与 OutboundResponse，交集变大在语义上是**正确的**（出站消息携带 agent 产出的 artifact 引用，恰恰说明两者共享该概念），但它**推翻了 8.6 的一条既有不变式**。

**处置（项目负责人已批准，选项 A）**：把该不变式修订为**显式形式**——`agent & outbound == {"content", "metadata", "artifacts"}`，并**同时保留**同函数的另外两条边界断言（`external_session_id not in outbound`、`not (agent & {channel_id, external_conversation_id, agent_id})`）——后两条语义不变，继续守「Channel 归属不进 Agent 响应 / Agent 侧引用不进 Channel 消息」。

**裁决记录（2026-10-26，Q1 = A，已结案）**：
- **不得删除任何保护性断言**：该测试函数的三条断言**全部保留**，仅第三条的期望集合按 D2 显式扩为三项。
- 修订后的不变式：
  - `agent & outbound == {"content", "metadata", "artifacts"}`
  - `outbound - agent == {"channel_id", "external_conversation_id"}`（等价于保留原第二条断言）
  - `agent - outbound == {"external_session_id"}`（等价于保留原第一条断言）
- 语义说明：`artifacts` 同时属于 AgentResponse 与 OutboundResponse，交集变大**是 D2 的正确结果**（出站消息携带 agent 产出的引用，恰恰说明两者共享该概念）；被守住的语义边界是**归属字段不得交叉**（`channel_id` / `external_conversation_id` / `agent_id` 不进 Agent 响应；`external_session_id` 不进 Channel 消息）。

**本项已结案**（见 §9.2 Q1 状态；验收标准 A6 已据此定稿）。

### D3 — Activity 事件：Phase 1 不新增独立 Artifact Activity 事件

**决策内容**：Phase 1 **不新增**独立 Artifact Activity 事件，**不增加**新的 Activity 事务或交付事件。Interaction 中已有的 Artifact 快照与现有 Interaction 状态继续作为本阶段可观察信息。**不得**宣称 ACP 已确认文件成功显示、下载或被用户接收。

**落点**：本文 §4（实施文件清单——**不含** `interaction_recording.py`）、§6.3（事务与运行时影响：零新事务）、§8（验收标准：零新 Activity 类型 / 零新事务 / `delivery_status` 取值集合不变）。

**本轮核实的事实（支撑该决策的可执行性）**：
- Artifact 快照已存在：`interactions.response_payload["artifacts"]`（`app/application/interaction_recording.py:311-316`），由 `record_success` 在既有事务内写入 → **Phase 1 无需新增任何持久化写入点**
- 现有 Activity 类型共 4 个（`app/domain/activity_record.py:28-31`）：`interaction_received` / `stage_failure` / `session_created` / `interaction_completed` → **Phase 1 不新增**
- `activities` 表已有 `extra_metadata` JSON 列（`app/persistence/models/activity.py:40`），但 **Phase 1 不写**（避免在既有事务内增加失败点）

### D4 — Artifact 引用的安全与呈现

**决策内容**（语义要点）：Artifact 是 **Agent 提供的引用**；ACP **不得**声称已验证 / 已上传 / 已存储 / 已下载；渠道呈现须使用明确提示（语义等价于「Agent 提供的文件引用，ACP 未验证其可用性或安全性」）；不生成 Markdown / HTML 链接；不自动抓取 URI；不渲染 `Artifact.metadata`；`uri=None` 显示清楚的「未提供可用引用」提示且不生成空链接；对控制字符做安全处理；明确最大数量 / 长度 / 输出长度策略（数值须来自现有约束或**明确设计**，不得凭空宣称已有统一限制）；日志 / Activity / 错误信息不得泄露完整签名 URL / token / 敏感 metadata；须检查快照持久化与租户隔离读取边界，不得因增加展示而扩大数据访问权限；无法证明安全边界者列为实施前阻塞项。

**落点**：本文 §6（安全审查，含逐条对照与数值来源）、§8（验收标准 S1–S9）。

**本轮关键核实结论**：Phase 1 的呈现路径**不读数据库**（见 §3.1 第 6 段），因此**不会扩大任何数据访问面**；租户隔离边界保持不变（§6.5）。

### D5 — Local outbox 兼容性

**决策内容**：无 Artifact 时 Local outbox 既有输出**必须保持兼容、不新增空 `artifacts` 键**；有 Artifact 时才按明确的新增契约输出 `artifacts`；须设计正向与负向回归测试（无 artifact 旧结构不变 / 有 artifact 引用正确透传 / 无效 artifact 不绕过既有 Fail Closed 校验）。

**落点**：本文 §5.3（Local 契约与正/负回归策略）、§8（验收标准 L1–L3）。

---

## 2. 本轮实际核实范围

**已实读**：`backend/app/domain/{artifact,agent_response,outbound_response,inbound_interaction,activity_record,runtime_execution_result}.py`、`backend/app/application/{agent_dispatch,coordinator,delivery,interaction_recording,worker}.py`、`backend/app/adapters/channels/{wecom,wecom_bot,local,registry}.py`、`backend/app/persistence/models/{interaction,activity}.py`、`backend/app/persistence/repositories/interaction_record.py`、`backend/app/repositories/channel_adapter.py`、`backend/app/application/interaction_observability.py`、`backend/app/api/management/interactions.py`、`backend/agent_runtime_sdk/models.py`、7 个指定测试文件 + 3 个额外相关测试文件，以及 V0 冻结治理文档、内部索引文档、内部项目概览文档（三者均为内部治理文档，不随 OSS 发布）。

**术语校正**：任务书 §5 写「DeliveryService」，仓库实际类名为 **`DeliveryExecutor`**（`backend/app/application/delivery.py:31`）；不存在名为 `DeliveryService` 的符号。本报告一律使用实测名。

---

## 3. 调用链核实（逐段，以当前代码为准）

### 3.1 全链路：Artifact 在哪一层首次出现 / 被校验 / 被持久化 / 被丢失

| # | 层 | 当前行为（实测） | Artifact 在此层发生了什么 | 证据 |
|---|---|---|---|---|
| 1 | **Agent wire → AgentResponse** | 适配器**只解析** `content` / `session_id`；wire 上任何 artifact/file/media 字段**被忽略**（既不解析也不报错） | **首次可能出现**：Agent 若在 wire 上带 artifact 字段，**当前无人读取** | `app/adapters/agents/acp.py:737-767`（`_normalize_acp_response`）；`:468-661`（Tencent SSE 路径，`_is_content_block` 只认 `type=="text"`/reasoning 类） |
| 2 | **AgentResponse（domain）** | frozen 值对象，4 字段；`artifacts: list[Artifact] \| None = None`（默认 None） | **承载点**（可选字段） | `app/domain/agent_response.py:28-35` |
| 3 | **Dispatch 校验** | `_valid_response`（`:132-151`）→ `_valid_artifact`（`:153-173`）：`artifacts is None` 合法；非 list 拒；逐项校验 `artifact_id`/`kind`/`name` 非空 str、`uri`/`mime_type` str、`size_bytes` int、`metadata` dict | **唯一权威校验点（Fail Closed）**；非法 → `AgentResponseInvalidError`（`AGENT_RESPONSE_INVALID`），**不产生 OutboundResponse** | `app/application/agent_dispatch.py:132-173` |
| 4 | **Interaction 快照（持久化）** | `record_success` 在既有事务内写 `response_payload`，含 `"artifacts": _snapshot_artifacts(...)`（显式字段映射，`None → None`） | **持久化点**（唯一）；`record_failure` 不写 response_payload | `app/application/interaction_recording.py:126-149, 311-316` |
| 5 | **Coordinator** | 构造 `OutboundResponse(channel_id, external_conversation_id, content, metadata)`——**只取 `response.content` 与 `response.metadata`** | **★ 丢失点**：`response.artifacts` **从未被读取** | `app/application/coordinator.py:164-171` |
| 6 | **OutboundResponse** | frozen 值对象，4 字段，**无 `artifacts`** | **契约缺口**（D2 的对象） | `app/domain/outbound_response.py:59-73` |
| 7 | **DeliveryExecutor** | Fail Closed 校验 `channel_id` / `external_conversation_id` / `content` 非空 / `metadata` dict，然后整体转发 `outbound` | **天然透传**（不感知 Artifact） | `app/application/delivery.py:40-71` |
| 8 | **Channel Adapter** | 三个适配器**只读 `outbound.content`（+ `metadata`）**；**零** artifact / image / file / media 相关代码 | **呈现缺口**（D4 的对象） | `wecom.py:563-611`、`wecom_bot.py:649-734`（`deliver`）、`local.py:140-172` |
| 9 | **出站介质** | ① WeCom 自建应用：`POST /cgi-bin/message/send`，`{"msgtype":"text","text":{"content":…}}`<br>② WeCom 智能机器人：POST `metadata["response_url"]`，`{"msgtype":"markdown","markdown":{"content":…}}`<br>③ WeCom Bot WS：`aibot_respond_msg` 帧，`body.msgtype=markdown`<br>④ Local：`data/local_outbox.jsonl` 追加一行 JSON | **呈现终点** | `wecom.py:212-242`（compose 纯函数）、`:599`、`:591`；`wecom_bot.py:311-330`；`local.py:52, 158-172` |

**结论**：Artifact 的**完整生命周期目前是「进得来、看得见、存得下、传不出」**——第 1 层不解析、第 2 层可承载、第 3 层有闸门、第 4 层已落库、**第 5 层丢弃**、第 6 层无字段、第 7 层透传、第 8/9 层无呈现能力。

### 3.2 三条出站路径能否正确呈现引用（逐条）

| 路径 | 现状可否呈现 | 载体与现有限制 | Phase 1 最小接口 | 结论 |
|---|---|---|---|---|
| **WeCom 自建应用**（`message/send`） | ❌ | 仅 `msgtype=text`；**无** image / file / media / `upload_media` / `media_id` 任何代码路径 | 在 `deliver()` 内、调用 `_send_text_message` 前拼装 `content`；**不改** `compose_message_payload` 签名 | ✅ 可呈现（纯文本引用行） |
| **WeCom 智能机器人**（`response_url`） | ❌ | 仅 `msgtype=markdown`；无 template_card 使用；纯文本以 markdown 承载 | 同上，在 `_deliver_via_response_url` 前拼装 | ✅ 可呈现 |
| **WeCom Bot WebSocket** | ❌ | 仅 `body.msgtype=markdown`；WS 帧需 `req_id` 关联并等 `errcode==0` ACK | 在 `compose_respond_frame` 调用前拼装 `content` | ✅ 可呈现（markdown 承载纯文本行） |
| **Local outbox** | ❌ | outbox 行仅 `channel_id` / `external_conversation_id` / `content` / `metadata`；**不落库**、纯本地文件 sink | **有 Artifact 时**追加 `artifacts` 键；**无 Artifact 时不写该键**（D5） | ✅ 可结构化透传 |

### 3.3 是否改变旧 Agent / 无 artifact 响应 / 旧 outbox 消费者的行为

| 消费方 | 影响 | 保障方式 |
|---|---|---|
| 旧 Agent（`AgentResponse(content=…)`） | **无影响** | `artifacts` 默认 `None`；`_valid_response` 仍接受 `None` 与 `[]` |
| 无 artifact 响应 | **无影响** | 三条出站路径在 `artifacts` 为空时**不改写 `content`**（不改 compose 纯函数签名）；Local **不写** `artifacts` 键 |
| 旧 outbox 消费者 | **无影响** | 行结构在无 artifact 时**逐字节不变**；有 artifact 时新增键为**追加**，旧消费者忽略未知键即可（D5 的正/负回归覆盖） |
| `minimal` wire / Tencent SSE 消费者 | **无影响** | 不改 `agent_runtime_sdk/**`；`encode_response` 仍严格 `{"content","session_id"}` |

---

## 4. Phase 1 预计修改的文件清单

> 以下为**计划**，本轮**未实施**。

### 4.1 生产代码（4 个文件）

| # | 文件 | 拟修改内容 | 必要性 | 冻结面影响 |
|---|---|---|---|---|
| P1 | `backend/app/domain/outbound_response.py` | **末尾**追加 `artifacts: list \| None = None` | D2 契约缺口 | ⚠️ 8.6 契约字段面变更（见 §5.1） |
| P2 | `backend/app/application/coordinator.py` | `OutboundResponse(...)` 增加 `artifacts=response.artifacts` | 唯一丢失点（§3.1 第 5 段） | ❗**零新 import**（属性读取，非调用）；不改变执行顺序与六/七段编排 |
| P3 | `backend/app/adapters/channels/{wecom,wecom_bot}.py` | 在既有发送点前拼装引用行 | D4 呈现 | ❗`compose_*` 纯函数**签名不改**（其 wire 单测继续有效） |
| P4 | `backend/app/adapters/channels/local.py` | 有 artifact 时追加 `artifacts` 键 | D5 | ❗`content` 不变 |

**新增（1 个文件）**：

| # | 文件 | 拟新增内容 | 必要性 |
|---|---|---|---|
| P5 | `backend/app/domain/artifact_reference.py`（新） | 纯函数渲染器：把 `list[Artifact]` 渲染为**确定性、安全、非可执行**的引用文本块；**全函数（永不抛异常）**；**不 import** 任何 I/O / 框架 / `app.persistence` | D4 的安全呈现需要一个**唯一**实现点，避免三个适配器各写一套（漂移风险）；放在 `app/domain/` 可被 `app.adapters.*` 直接 import（`app.domain.*` 前缀在适配器 import 白名单内，实证 `tests/test_step10_2_2_outbound_adapter_contract.py:52-58`） |

**明确不修改**：`backend/app/application/delivery.py`、`backend/app/application/agent_dispatch.py`、`backend/app/application/interaction_recording.py`、`backend/app/domain/{agent_response,inbound_interaction,activity_record,runtime_execution_result}.py`、`backend/app/repositories/channel_adapter.py`、`backend/app/adapters/channels/registry.py`、`backend/agent_runtime_sdk/**`、`backend/app/api/**`、`backend/app/persistence/**`、`backend/alembic/**`、`backend/app/main.py`。

### 4.2 测试文件（1 新 + 6 调整）

| # | 文件 | 动作 | 原因（实测断言） |
|---|---|---|---|
| T0 | `backend/tests/test_acp_v0_3_artifact_outbound.py`（新） | 新增 | D4 / D5 的正负回归 + 安全回归 + 三路径呈现 |
| T1 | `tests/test_acp_v0_2_artifact_implementation.py` | **调整**（有意翻转 1 条断言 + 保留其余 11 条） | `:274` `assert "artifacts" not in {f.name for f in fields(OutboundResponse)}` → D2 授权后必须翻转 |
| T2 | `tests/test_step8_6_delivery_contract.py` | **调整**（3 条断言） | 见 §5.2 表 |
| T3 | `tests/test_step10_1_1_acp_adapter_contract.py` | **调整**（1 条断言） | `:91-93` `_field_names(OutboundResponse) == {4 字段}` |
| T4 | `tests/test_step9_A_runtime_slice_closure.py` | **调整**（1 条断言） | `:165-167` `fields(OutboundResponse) == {4 字段}` |
| T5 | `tests/test_step10_5_1_interaction_identity_audit.py` | **调整**（1 条断言） | `:256-258` `names == {4 字段}`（该测试**意图**是「出站不带 interaction 身份」，`:257` 的 `"external_interaction_id" not in names` 继续有效） |
| T6 | `tests/test_step10_5_3_duplicate_interaction_decision_semantics.py` | **调整**（1 条断言） | `:585-586` `names == {4 字段}`（`:571-572` 的 `"external_interaction_id" not in names` **不受影响**） |

**合计 8 条断言需调整，分布于 6 个文件**（T1–T6）。

### 4.3 ⚠️ 本轮新发现的 3 条**实现约束**（非破坏，但限制实现写法）

| # | 约束 | 证据 | 对实现的约束 |
|---|---|---|---|
| C1 | `outbound_response.py` **必须保持纯 stdlib**（import 根 == `{"dataclasses"}`） | `tests/test_step8_6_delivery_contract.py:166` 与 `:249` | ❗**不得** `from app.domain.artifact import Artifact` → 字段类型写 `list \| None`，元素类型在 docstring 声明为 `list[Artifact]`（与 `AgentResponse` 的写法差异需在契约中说明） |
| C2 | `delivery.py` 的 import 根**白名单极窄** | `tests/test_step9_6_delivery_execution.py` `_root_allowed` = {`app.repositories.channel_adapter`, `app.application.errors`, `app.domain.outbound_response`, `collections`, `dataclasses`, `typing`} | ❗`delivery.py` **不得** import `app.domain.artifact` → **不在 Delivery 层做 Artifact 校验**（校验留在 dispatch 侧，见 §6.1） |
| C3 | 新增符号名**不得**含 `retry` / `replay` / `redeliver` / `outbox` | `tests/test_step10_5_3_duplicate_interaction_decision_semantics.py:574-584`（扫描 `APP` 下 ClassDef/FunctionDef 名） | ❗渲染器与新增函数**不得**命名为含 `outbox` / `redeliver` 等词的符号（例如 `_render_outbox_block` 会触发失败） |

> C3 是本轮**新识别**的约束：既有 Gate 草案未提及。Phase 1 的函数命名必须避开这 4 个 token。

---

## 5. 契约与兼容性计划

### 5.1 契约字段差异（D2 / S1）

| 契约 | 当前 | Phase 1 后 | 差异性质 |
|---|---|---|---|
| `InboundInteraction` | 6 字段 | **6 字段（不变）** | — |
| `OutboundResponse` | 4 字段 | **5 字段**（+ `artifacts`，末尾，默认 `None`） | **显式**加性差异（D2 授权） |
| `AgentResponse` | 4 字段 | **4 字段（不变）** | — |
| `RuntimeExecutionResult` | 3 字段 | **3 字段（不变）** | — |
| `AgentRequest` | 5 字段 | **5 字段（不变）** | — |
| SDK `AgentResponse` / minimal wire | 镜像 4 字段 / `{content,session_id}` | **不变** | — |
| `InteractionModel` | 16 列 | **16 列（不变）** | — |

**向后兼容**：新字段置于**末尾**且默认 `None` → 所有既有位置参 / 关键字参构造式**保持语义不变**（`metadata` 仍是第 4 位；实证 `tests/test_3b_2_2_wecom_bot_websocket_transport.py:325-333` 使用 4 位位置参构造）。

**禁止项落实**：**不走 `metadata`**（D2 明令）。理由亦为技术性：`metadata` 在 Coordinator 处被**渠道侧覆盖**（`coordinator.py:164` `{**response.metadata, **inbound.metadata}`），Agent 侧键可能被渠道同名键遮蔽；且 `metadata` 无 schema（违背「结构化字段面冻结」纪律），并与渠道语义混用（`wecom.py:245-259` 从 `metadata` 取 `response_url`）。

### 5.2 既有契约测试的精确影响（实测逐条）

| 文件 | 断言位置 | 现状断言 | 是否被 `artifacts` 破坏 | 处置 |
|---|---|---|---|---|
| `test_acp_v0_2_artifact_implementation.py` | `:274` | `"artifacts" not in fields(OutboundResponse)` | ❌ **破坏** | **有意翻转**（D2 授权）：改为「artifact 进 Delivery 且内容含引用」+ 补「无 artifact 时投递内容逐字节不变」 |
| `test_step8_6_delivery_contract.py` | `:49` + `:176` | `OUTBOUND_FIELDS`（4 集合）+ `names == OUTBOUND_FIELDS` | ❌ **破坏** | 常量扩为 5 字段；断言文本「恰好 4 个」同步修订 |
| `test_step8_6_delivery_contract.py` | `:245` | `outbound == inbound - {external_interaction_id, external_identity_id}` | ❌ **破坏** | 改为 D2 双向对称差（§1.2.2） |
| `test_step8_6_delivery_contract.py` | `:262` | `agent & outbound == {"content","metadata"}` | ❌ **破坏** | ✅ **已裁决（Q1 = A）**：改为 `{"content","metadata","artifacts"}`，**并保留同函数另两条断言**（§1.2.3 裁决记录；§8 A6） |
| `test_step8_6_delivery_contract.py` | `:166` / `:249` | `_roots(_tree(OUTBOUND_PY)) == {"dataclasses"}` | ✅ 不破坏 | **约束 C1**：实现必须遵守 |
| `test_step8_6_delivery_contract.py` | `:178` | `not (names & FORBIDDEN_FIELDS)` | ✅ 不破坏 | `artifacts` ∉ `FORBIDDEN_FIELDS`（实测该集合 13 项均不含 `artifact`） |
| `test_step8_6_delivery_contract.py` | `:221-225` | 源码体不得含 `user_id`/`identity_id`/`session_id`/`connection_id`/`agent_id`/`routing_rule` | ✅ 不破坏 | `artifacts` 不含这些 marker；⚠️ 新增 docstring 不得引入这些词 |
| `test_step10_1_1_acp_adapter_contract.py` | `:91-93` | `_field_names(OutboundResponse) == {4 字段}` | ❌ **破坏** | 扩为 5 字段 |
| `test_step10_1_1_acp_adapter_contract.py` | `:129-136` | `PROTOCOL_INTERNAL_BAN` 泄漏扫描 | ✅ 不破坏 | 实测该 12 项 ban token（`transport`/`headers`/`auth`/`capability`/`negotiation`/`skill`/`stream`/`endpoint`/`protocol`/`websocket`/`initialize`/`connect`）均不含 `artifact` |
| `test_step9_A_runtime_slice_closure.py` | `:165-167` | `fields(OutboundResponse) == {4 字段}` | ❌ **破坏** | 扩为 5 字段 |
| `test_step9_A_runtime_slice_closure.py` | `:172-179` | Coordinator import 根 ⊆ `app.domain.*` / {errors,runtime}；`_call_attrs == {"execute"}` | ✅ 不破坏 | **约束**：P2 改动为 1 行属性透传，**零新 import、零新调用** → 自动满足 |
| `test_step10_5_1_interaction_identity_audit.py` | `:256-258` | `names == {4 字段}` | ❌ **破坏** | 扩为 5 字段（`:257` 的 `external_interaction_id not in names` 继续有效） |
| `test_step10_5_3_duplicate_interaction_decision_semantics.py` | `:571-572` | `"external_interaction_id" not in names` | ✅ 不破坏 | 保持 |
| `test_step10_5_3_duplicate_interaction_decision_semantics.py` | `:585-586` | `names == {4 字段}` | ❌ **破坏** | 扩为 5 字段 |
| `test_step10_5_3_duplicate_interaction_decision_semantics.py` | `:574-584` | 符号名 ban（retry/replay/redeliver/outbox） | ✅ 不破坏 | **约束 C3** |
| `test_acp_v0_2_contract_gate.py` | 全 6 项 | InteractionModel 16 列 / SDK 镜像 / minimal wire / Artifact 字段面 | ✅ **全部不破坏** | **无需改动** |

**净结论**：**8 条断言需调整**（6 文件），**3 条实现约束**需遵守，**`test_acp_v0_2_contract_gate.py` 完全不需要改动**。

### 5.3 Local outbox 契约（D5）

| 场景 | 期望 outbox 行 | 保障 |
|---|---|---|
| 无 Artifact | `{channel_id, external_conversation_id, content, metadata}` —— **与现状逐字节相同**，**无 `artifacts` 键** | 键**条件性**追加（`if outbound.artifacts:`），非 `artifacts: None` |
| 有 Artifact | 同上 4 键 **+ `artifacts: [ {artifact_id, kind, name, uri, mime_type, size_bytes, metadata}, ... ]`** | 完整结构化字段（与 `response_payload` 的 `_snapshot_artifacts` 形状对齐） |
| 无效 Artifact | **不产生 outbox 行** | 既有 Fail Closed 在 dispatch 侧拦截（`agent_dispatch.py:145-150`）→ 抛 `AGENT_RESPONSE_INVALID` → **根本不构造 OutboundResponse** |
| 旧消费者 | 无 artifact 时行结构不变；有 artifact 时为**新增键**，旧消费者按「忽略未知键」处理 | 正/负回归测试覆盖 |

---

## 6. 安全审查

### 6.1 校验位置：保持单一权威闸门（不新增 Delivery 侧校验）

- **权威闸门**：`agent_dispatch.py:132-173`（`_valid_response` / `_valid_artifact`）——**已存在，Phase 1 不改**
- **不在 Delivery 层加第二道闸门**：受**约束 C2** 限制（`delivery.py` 不得 import `app.domain.artifact`）；且生产上 Artifact **不可能**绕过——`OutboundResponse` 的唯一构造点是 `coordinator.py:165`，其 `response` 来自已被 `_valid_response` 校验的 dispatch 结果
- **纵深防御**：渲染器（P5）为**全函数 + 输入净化** → 即使有畸形 artifact 进入出站，也**不会崩溃**、**不会注入内容**
- **D5 第 3 项（无效 Artifact 不绕过 Fail Closed）的可测形态**：畸形 artifact → dispatch 抛 `AGENT_RESPONSE_INVALID` → **OutboundResponse 从未构造** → 适配器 `deliver()` 从未被调用 → outbox **无新行**（回归测试须断言此链路，而非仅断言异常）

### 6.2 URI 处理（不得视为已验证的下载地址）

| 要求 | 处置 |
|---|---|
| 不自动抓取 / 不下载 URI | 渲染器为纯函数，**无任何 I/O**；全仓无「以 artifact uri 为网络目标」的调用点 |
| 不生成 Markdown 链接 | 渲染输出**不得**含 `[text](url)` 形态 |
| 不生成 HTML 链接 | 渲染输出**不得**含 `<a href` / 任何标签 |
| 不声称已验证 / 已上传 / 已存储 / 已下载 | 呈现文案**必须**含「Agent 提供的文件引用，ACP 未验证其可用性或安全性」等价语义；**禁用词表**机器化断言（`已上传`/`已存储`/`已下载`/`已发送文件`/`下载成功`/`已验证` 等） |
| `uri=None` | 显示**明确的「未提供可用引用」提示**，**不生成空链接**、**不静默丢弃该 artifact**、**不抛异常** |
| **URI 原值完整性**（L2） | 消息中出现的 URI 必须与 `Artifact.uri` **逐字节相同**——**不截断、不改写、不消毒**。要么原样呈现，要么**整体省略**（§6.4.2） |
| **URI 不可分割**（L3） | URI 是**原子令牌**：超长或含控制字符时**整体省略**，**绝不**输出「被截断的 URI 片段」并将其暗示为可用地址 |
| 含控制字符的 URI | **整体省略**并说明「引用格式非法」——**不**先消毒再呈现（消毒会改写原值，既破坏完整性，也可能产出错误地址） |

> 残余说明：聊天客户端可能对**裸 URL** 自行加链接——该行为由客户端决定，ACP 无法控制。呈现层以代码跨度/纯文本形态降低可点击性，并在文案中标注「未验证」。此为**已登记的残留风险**，非本轮可解。

### 6.3 `Artifact.metadata` 与日志 / Activity / 错误信息

| 面 | 处置 |
|---|---|
| `Artifact.metadata`（dict，agent 提供） | **完全不渲染**（不进入任何出站消息） |
| `uri`（可能含签名 token） | **不写入普通日志**；**不写入 Activity**（Phase 1 无新 Activity 事件，D3）；**不写入错误消息** |
| 出站消息中的 `uri` | 仅以「未验证引用」形态呈现——**这是呈现给最终用户的业务内容**，与「日志泄露」是不同面；须在契约中显式承认：**该 URI 会被发送给终端用户**，这是 D4 授权的直接后果 |
| `response_payload` 快照 | **既有暴露**：`interactions.response_payload` 含完整 `uri` 与原始 `metadata`，经 `GET /api/interactions/{id}` **原样透出**（租户作用域内）。**Phase 1 不新增脱敏，且不得宣称已脱敏** |
| **展示层 vs 快照层** | 展示预算（§6.4.1）**只**作用于出站**消息文本**；快照 `response_payload["artifacts"]` 与 Local outbox 的 `artifacts` 均保留**完整原值**（§6.4.3）——展示降级**不得**回溯影响持久化 |

### 6.4 展示长度限制、URI 原值完整性、超长 URI 行为与快照语义

#### 6.4.0 四个必须区分的概念（本阶段设计的核心区分）

| # | 概念 | 定义 | 作用面 |
|---|---|---|---|
| **L1** | **展示长度限制** | 出站**消息文本**的呈现预算（artifact 数量 / 名称长度 / 块总长） | **仅** WeCom / Bot 的消息文本 |
| **L2** | **URI 原值的完整性** | 消息中出现的 URI 必须与 `Artifact.uri` **逐字节相同**（不截断 / 不改写 / 不消毒） | 呈现层 |
| **L3** | **超长 URI 的行为** | URI 是**原子令牌**：要么原样呈现，要么**整体省略**并说明原因 | 呈现层 |
| **L4** | **快照持久化语义** | `response_payload["artifacts"]` 保持**完整原值**，不截断、不改写 | 持久化层（**不受 L1 影响**） |

**三者作用面不同、互不替代**：
- 展示预算（L1）**不得回溯影响持久化**——快照永远保留完整原值；
- 保真规则（L2 / L3）**不得被展示预算降级**——不因超预算而输出「被截断的地址」；
- 持久化事实（L4）**不是**呈现层的降级兜底通道（见 §6.4.2 第 5 点的诚实边界）。

#### 6.4.1 设计基线（Q2 已批准；Q3 已裁决）

**核实结论**：仓库**当前不存在**任何针对 `content` 长度、artifact 数量、URI 长度、名称长度的既有统一限制。实测现存限制仅 3 类：`DEFAULT_LIST_LIMIT = 50`（`interaction_observability.py:33`）、`DRIVER_PICKUP_LIMIT` 默认 10（`config.py:52`）、`DEFAULT_OUTBOUND_TIMEOUT_S = 10.0`（`wecom.py:124`）。**故下列数值只能是 Phase 1 显式设计值，不得宣称已有统一限制。**

| 参数 | Phase 1 设计值 | 来源 / 理由 | 超限处置 |
|---|---|---|---|
| 单次响应最多呈现的 artifact 数 | **10** | **显式设计**（新引入）。理由：与仓库现存最小批量常量（`DRIVER_PICKUP_LIMIT` 默认 10）同量级，且引用块需在移动端可读 | 呈现前 10 条 + 明确计数提示「另有 N 条引用未呈现」（**不静默丢弃**） |
| 单个名称呈现长度上限 | **128 字符** | **显式设计**（新引入）。理由：名称是**展示标签**，截断后**不是地址**，标注即可 | 截断 + 显式标注（`…`） |
| 渲染引用块总长度上限 | **2000 字符** | **显式设计**（新引入）。**Q3 已裁决：2000 是 ACP 的显式设计值；本仓库未固化任何企业微信平台侧长度上限，因此不得声称 2000 已被证明符合平台的所有相关长度限制**（平台侧上限仍未固化，持续登记为待确认项） | 达到预算即停止追加 + 「引用块已截断」提示（**不抛错、不丢文本主体**） |
| 控制字符（自由文本字段） | `artifact_id` / `kind` / `name` / `mime_type` 一律替换为空格 | **显式设计**。理由：防止换行 / 制表 / 零宽字符伪造结构或注入 | 替换后呈现 |
| **单条 URI 长度** | **不设独立上限**（原提案的「512 字符截断」**已撤销**） | 见 §6.4.2 | 见 §6.4.2 |

#### 6.4.2 超长 URI 的最终处理策略（**不截断 URI**）

**被撤销的提案**：原提案为「单条 URI 呈现长度上限 512 字符，超出则截断并标注」。**该提案已撤销**——截断后的 URI **不是地址**；把它作为引用输出，等同于**把一个不可用值描述成可用地址**，与 D4「不得把 URI 当作已验证的安全下载地址」直接冲突。

**最终策略：URI 是原子令牌（atomic token）——整体呈现，或整体省略。**

1. **原样呈现（verbatim）**，当且仅当同时满足：
   - URI **不含控制字符**（`\r` `\n` `\t` 及其他不可见控制字符）；**且**
   - URI 能在**剩余展示预算**内**完整**放入（`len(uri) + 该行固定开销 <= 剩余预算 − 省略提示预留量`）。

   → 消息中的 URI 与 `Artifact.uri` **逐字节相同**（满足 L2）；呈现前**不消毒、不截断、不改写**。
2. **整体省略（omit，非截断）**：上述任一条件不满足时，**不输出该 URI**，改为输出一条**明确说明省略原因**的提示：
   - 含控制字符 → 「引用格式非法，未在消息中呈现」
   - 超出剩余展示预算 → 「引用超出可展示预算，未在消息中呈现」

   提示**不得**包含 URI 的任何片段（前缀 / 后缀 / 省略号形式均不可），**不得**暗示存在一个可点击地址（满足 L3）。
3. **不静默丢弃**：省略必须**可见**（逐条原因提示 + 计数），符合 D2「不静默丢弃该 artifact」。
4. **不抛错**：省略是**正常呈现结果**而非失败；渲染器保持**全函数**（§8 C10）。
5. **完整原值的落点与诚实边界**：被省略的 URI，其**完整原值**保存在 Interaction 快照 `response_payload["artifacts"][i]["uri"]`（L4），可经**既有的 tenant-scoped 管理读路径**（`GET /api/interactions/{id}`）由**具备该 interaction 读取权的操作者**取得。
   ⚠️ **必须如实登记**：**终端用户（WeCom / Bot 收件人）无法从该消息中获得被省略的引用**——完整原值只在 Interaction 快照，面向的是有读取权的操作者，**不是**终端用户。**不得**在文案或契约中暗示终端用户也能取得。
6. **未引入任何新能力**：上述策略**不需要**下载、URL 代理、对象存储或任何新持久化位置——**未呈现**的 URI 本就完整地留在既有快照里。

**为什么这不构成实施前阻塞项**：该行为在现有架构内**完全可定义且无歧义**——
- 判定所需信息**全部**来自 `Artifact` 自身（`uri` 字符串 + 其余字段）与渲染器的**确定性预算累计**，无需外部状态；
- **不需要**新的 I/O、存储或跨系统交互；
- 与既有持久化语义**正交**（呈现层不写库、不改快照）；
- 不引入 §4 禁止清单中的任何能力（无下载 / 无代理 / 无存储）。

唯一残留是**平台侧长度上限未固化**（Q3，已持续登记为待确认项）——它影响的是「2000 是否留足余量」的**经验判断**，**不影响**本策略的**正确性**（本策略不依赖任何平台上限值即可成立）。

#### 6.4.3 快照与结构化记录保持完整原值（L4）

| 面 | 处置 |
|---|---|
| `interactions.response_payload["artifacts"]` | **完整原值**：`uri` 与 `metadata` **不截断、不改写**（沿用 `_snapshot_artifacts` 的显式字段映射，**既有持久化语义不变**）；展示预算**不作用于此** |
| Local outbox 的 `artifacts` | **完整原值**（结构化记录）→ 展示预算**不作用于此**；`uri` 与 `metadata` 与 `Artifact` **逐字节相同** |
| Local outbox 的 `content` | **Agent 原文**（**不**渲染引用块）：Local 是**结构化 sink**——用户可见的引用呈现由 **WeCom / WeCom Bot Adapter** 负责（§4.1 P3）；`content` 保持原文 ⇒ outbox 记录的正是交给渠道的正文 |
| WeCom / Bot 消息文本 | **唯一**受展示预算约束的载体 |
| 新增存储 / 下载 / URL 代理 | **零** |

> **修订原因（2026-10-26，实施后对账）**：本节原表述为「outbox 的 `content` 字段仍是**呈现层**文本（可能含省略提示）」，
> 与 §4.1 **P4**（Local：「有 artifact 时追加 `artifacts` 键」+「❗`content` 不变」）**冲突**。
> **以 §4.1 P4 为准并据此修正本节**：Local 是**结构化 sink**，不做用户可见呈现——
> P3（WeCom / WeCom Bot）在既有发送点前拼装引用行，P4（Local）只追加 `artifacts` 键，
> **P3 / P4 的不对称是有意的**。本次修订**只改本节的表述**，**不改任何生产代码**、
> **不放宽任何验收标准**（§8 的 B4 / D1 / D2 / C21 原本就只约束 `artifacts` 键与
> 「无 artifact 时 4 键兼容」，与本修订一致）。
>
> **一致性说明**：Local outbox 行的两个字段角色**不同**且**不构成矛盾**：`content` =
> 交给渠道的消息正文（Local 原样记录 Agent 原文，不做呈现）；`artifacts` =
> 随消息透传的**结构化完整原值**（展示预算不作用于它）。

> **实施前阻塞项提示（复核结论）**：D4 规定「若现有代码无法证明安全边界，列为实施前阻塞项，不得假设已安全」。复核结论：
> - **可证明**：不抓取 URI（无 I/O 路径）、不渲染 metadata、URI 原值完整性可保证（原子呈现或整体省略）、租户隔离读取边界不变（§6.5）、出站呈现不读数据库（§6.5）、快照与结构化记录保留完整原值（§6.4.3）
> - **不可证明（需 Phase 2 解决，Phase 1 明确不做）**：URI 的**可达性 / 有效期 / 权限**——ACP 从不解析 URI，因此**无法**证明 URI 指向的资源存在、可访问、未过期或属当前租户。Phase 1 **只能**如实呈现「Agent 提供的未验证引用」，不得声称任何可达性。**这不是 Phase 1 的阻塞项**（Phase 1 不取回字节），但必须写入契约与出站文案。
> - **本轮新增复核项已结案**：超长 URI 的行为已按 §6.4.2 定义为可实现、无歧义、无新能力依赖 → **不构成实施前阻塞项**。

### 6.5 持久化与租户隔离读取边界（是否因展示而扩大访问）

| 项 | 核实结果 |
|---|---|
| Artifact 快照持久化 | `interactions.response_payload["artifacts"]`（JSON 列，`interaction.py:60`）；由 `record_success` 在**既有事务**内写入 → Phase 1 **不新增任何写入点 / 事务** |
| 快照读取边界 | `SqlAlchemyInteractionRepository.__init__(session, tenant_id)` → 写入盖章 `tenant_id`（`:29-36`）、读取按 `InteractionModel.tenant_id == self._tenant_id` 过滤（`:57-58, :116-118`）；`InteractionObservabilityService.get_interaction` 经 tenant-scoped repo（`interaction_observability.py:51-54`）；API 经「TenantScope → tenant-scoped Service」注入（`api/management/interactions.py:52-58`） |
| **Phase 1 出站呈现的数据来源** | **内存中的 `outbound.artifacts`**（来自 dispatch 返回值），**不读 `response_payload`** → **零新增数据库读取**，**零数据访问面扩大** ✅ |
| API 暴露面 | **不变**（不新增端点、不改 schema）；`GET /api/interactions/{id}` 的 `response_payload` 原样透出行为保持 —— **既有暴露，如实记录，不宣称已脱敏** |
| `Artifact` 的 `tenant_id` | **不新增**（Runtime 契约面租户无关的既有红线；`docs/step-cloud-0.1.1-B-tenant-isolation-gate.md:249-258`） |

### 6.6 安全审查结论

| 要求 | 判定 |
|---|---|
| 零 URI 抓取 / 零字节下载 / 零字节上传 | ✅ 可满足（渲染器纯函数） |
| 不新增表 / 列 / 迁移 / 对象存储 / 文件服务 | ✅ 可满足 |
| 不新增 signed URL / ACL / 预览 / 下载端点 | ✅ 可满足 |
| 不改 Runtime 执行语义 / 冻结协议 | ✅ 可满足（唯一契约变更 = D2 授权的 OutboundResponse 加性字段） |
| Artifact 不新增 `tenant_id`，不突破租户隔离 | ✅ 可满足（出站不读库，见 §6.5） |
| URI / metadata 视为不可信 | ✅ 可满足（净化 + 不渲染 metadata） |
| 不将完整 URI / token / 敏感 metadata 写入普通日志 | ✅ 可满足（Phase 1 无新日志点；Activity 不新增；错误消息不含 URI） |
| 如实记录 `response_payload` 暴露，不宣称已脱敏 | ✅ 可满足（§6.3 明列） |
| URI 不作为 HTML/Markdown 可执行内容 | ✅ 可满足 |
| Artifact 失败不误改已成功的文本交付为失败 | ✅ 可满足（渲染器全函数 → 无失败模式） |
| 不支持呈现的渠道显式降级 | ✅ 可满足（当前 3 条路径**全部支持**引用呈现；降级契约作为**扩展点**定义，见 §6.7） |
| URI 可达性 / 有效期 / 权限 | ⚠️ **不可证明** —— ACP 从不解析 URI。**Phase 1 明确不做**，只能如实标注「未验证」 |

### 6.7 不支持呈现的 Adapter 的显式降级契约（扩展点）

当前 3 条出站路径**全部支持**引用呈现 → 降级路径当前**无生产实例**。契约定义（供未来渠道使用）：

- 每个适配器声明模块级能力常量（布尔）；值为 False 时：**`content` 逐字节不变**、**不呈现**任何引用、**不得**声称已交付
- 降级是**声明式、可审计**的（非运行期静默丢弃）；以机器化测试断言「渠道注册表 → 能力声明」映射完整
- 降级**不产生** Activity 事件（D3）、**不改变** `delivery_status` 语义

---

## 7. 事务与运行时影响分析

| 维度 | Phase 1 影响 | 证据 / 保障 |
|---|---|---|
| **新增 Activity 事件** | ❌ **零** | D3；`activity_record.py:28-31` 类型集合不变；`activities` 表 `extra_metadata` 列不写 |
| **新增 Activity 事务** | ❌ **零** | `interaction_recording.py` 不改动；Phase 1 唯一新增的写入是 `interactions.response_payload` —— **该键在既有 `record_success` 事务内早已写入**（`:311-316`），非新增 |
| **新增数据库写入 / 迁移** | ❌ **零** | `InteractionModel` 16 列不变；`alembic/versions/*` 不动 |
| **`processing_status` / `delivery_status` 语义** | ❌ **不变** | 4.2/5.3 冻结的两轴独立语义保持；**不新增** `delivery_status` 取值（如 `delivered_partial`） |
| **Attempt Fence** | ❌ **不变** | `OutcomeCommit.REJECTED` 语义与 `expected_started_at` 等式不动；`record_success` 不改 |
| **重试 / 幂等** | ❌ **不变** | 既有 `retryable` 映射（`interaction_recording.py:104-118`）不动；Phase 1 不引入 retry / replay / redeliver 概念（并受**约束 C3** 的符号名 ban 保护） |
| **事务边界** | ❌ **不变** | Phase 1 不新增事务；出站呈现发生在 `deliver()` 内（**事务外**），与既有投递语义一致 |
| **Runtime 执行顺序** | ❌ **不变** | `RUNTIME_EXECUTION_ORDER`（`app/application/runtime.py:78-86`，7 段）不动；Coordinator 仅在既有第 7 段多传一个字段 |
| **失败路径语义** | ❌ **不变** | 失败路径仍走 typed 异常通道，不产 Result；`record_failure` 不写 response_payload |
| **可观察性** | ℹ️ **沿用既有** | D3：Artifact 明细由 `response_payload` 承载；投递结果由 `delivery_status` 承载；**不新增** artifact 专属事件 |

---

## 8. 验收标准（可测试 PASS / FAIL）

> 以下为**Phase 1 实施后的验收条件**。**已于 2026-10-26 实施并逐条验收**——
> 逐项证据见 **§8.1**，残留项见 **§8.2**。
>
> **规范编号与计数（唯一事实源）**：A **6** 条（A1–A6）｜B **6** 条（B1–B6）｜
> C **22** 条（C1–C22）｜D **3** 条（D1–D3）｜E **7** 条（E1–E7）｜F **3** 条（F1–F3）
> —— **合计 47 条**。任何实施报告 / 测试映射**一律以本表的编号与计数为准**。
>
> **验收结论（2026-10-26，R-1 加固后）**：**47 条全部 PASS**。
> R-1（`Zl` / `Zp` 行/段分隔符未净化）**已闭合**：呈现层字符分类扩至
> `Cc` / `Cf` / `Zl` / `Zp` —— 自由文本→**替换为空格**；URI→**整体省略**并给出明确原因
> 「引用包含行/段分隔符」（**绝不**先消毒再把改写值当地址呈现）；快照与 Local outbox 的
> **原值保真不受影响**（L4）。C6 / C17 / C20 / C21 的文字**已同步收紧为明确口径**
> （见下），**未降低任何标准、未删除任何测试**。

### A. 契约（D2）

| # | 条件 | PASS | FAIL |
|---|---|---|---|
| A1 | `inbound - outbound == {"external_interaction_id","external_identity_id"}` | 集合精确相等 | 任一多/少 |
| A2 | `outbound - inbound == {"artifacts"}` | 集合精确相等 | 任一多/少 |
| A3 | `InboundInteraction` 仍 6 字段、`AgentResponse` 仍 4、`RuntimeExecutionResult` 仍 3、`AgentRequest` 仍 5、SDK 镜像对等、minimal wire == `{content,session_id}` | 全部成立 | 任一漂移 |
| A4 | `InteractionModel` 仍 16 列、无 artifact 表 | 成立 | 任一变化 |
| A5 | `outbound_response.py` import 根 == `{"dataclasses"}` | 成立 | 出现任何其他根 |
| A6 | **Q1 已裁决（A）**：`agent & outbound == {"content","metadata","artifacts"}`；`external_session_id not in outbound`；`not (agent & {channel_id, external_conversation_id, agent_id})` —— **三条断言全部存在且成立**（无删除） | 三条均成立 | 任一缺失或不成立 |

### B. 兼容（无 Artifact 零变化）

| # | 条件 | PASS | FAIL |
|---|---|---|---|
| B1 | 无 artifact 时，WeCom 自建应用 `message/send` payload **逐字节**等于改动前 | 字节一致 | 任一字节不同 |
| B2 | 无 artifact 时，WeCom 智能机器人 `response_url` payload **逐字节**一致 | 字节一致 | 任一不同 |
| B3 | 无 artifact 时，WeCom Bot `aibot_respond_msg` 帧 **逐字节**一致 | 字节一致 | 任一不同 |
| B4 | 无 artifact 时，Local outbox 行 **逐字节**一致且**无 `artifacts` 键** | 字节一致且无该键 | 出现该键或内容不同 |
| B5 | 旧 Agent（`AgentResponse(content=…)`）端到端仍成功，`artifacts is None` | 成立 | 失败 |
| B6 | `compose_message_payload` / `compose_robot_reply_payload` / `compose_respond_frame` **签名与输出未变** | 未变 | 任一变化 |

### C. 呈现与安全（D4）

| # | 条件 | PASS | FAIL |
|---|---|---|---|
| C1 | 渲染输出**不含** `[text](url)` 形态、**不含** `<a href` / 任何 HTML 标签 | 正则扫描零命中 | 任一命中 |
| C2 | 渲染输出**不含** `Artifact.metadata` 的任何键或值 | 零命中 | 任一命中 |
| C3 | 渲染输出**不含**禁用完成态措辞（`已上传`/`已存储`/`已下载`/`已发送文件`/`下载成功`/`已验证` 等） | 零命中 | 任一命中 |
| C4 | 渲染输出**必含**「未验证」等价语义提示 | 含 | 缺 |
| C5 | `uri=None` → 含「未提供可用引用」提示，**无**空链接，**不抛异常** | 成立 | 抛错或静默丢弃 |
| C6 | **自由文本字段**（`artifact_id`/`kind`/`name`/`mime_type`）中的**不安全字符**（Unicode 类别 `Cc` 控制符 / `Cf` 格式符 / `Zl` 行分隔符 / `Zp` 段分隔符 —— 含 `\r` `\n` `\t`、零宽字符、RTL override、**U+2028 / U+2029**）→ **替换为空格** | 成立 | 残留 |
| C7 | 数量 > 10 → 呈现前 10 + 明确「另有 N 条」提示 | 成立 | 静默丢弃或全量呈现 |
| C8 | **名称**超 128 → 截断 + **显式标注**，**不抛错**（名称是展示标签，截断后不是地址） | 成立 | 抛错或静默截断（无标注） |
| C9 | 渲染为**确定性**：同输入两次调用逐字节一致 | 一致 | 不一致 |
| C10 | 渲染器为**全函数**：对 `None` / `[]` / 畸形元素 / 超长字符串 / 非 str 字段均不抛异常 | 不抛 | 任一抛错 |
| C11 | **零网络请求**：渲染过程无 socket / HTTP 调用；全仓无以 artifact uri 为网络目标的调用 | 零调用 | 任一调用 |
| C12 | 渲染器 import 根仅 stdlib | 仅 stdlib | 含任何 I/O / 框架 / `app.persistence` |
| C13 | 新增符号名不含 `retry`/`replay`/`redeliver`/`outbox` | 零命中 | 任一命中（约束 C3） |
| C14 | 出站消息 / 错误消息 / 日志中**不含** Activity `metadata` 内容 | 零命中 | 任一命中 |
| **C15** | **URI 原值完整性（L2）**：消息中出现的 URI 与 `Artifact.uri` **逐字节相同** | 逐字节相等 | 任何改写 / 消毒 / 转义 |
| **C16** | **URI 原子性（L3）**：URI 超出剩余展示预算 → **整体省略** + 明确「超出可展示预算」提示；输出**不含**该 URI 的任何片段（前缀 / 后缀 / 省略号形式均不可） | 整体省略且零片段 | 输出被截断的 URI 片段 |
| **C17** | **含不安全字符的 URI**（`Cc` / `Cf` / `Zl` / `Zp` —— 含 `\r` `\n` `\t`、零宽字符、**U+2028 / U+2029**）→ **整体省略** + 明确原因提示（「引用格式非法」/「引用包含行/段分隔符」）；输出**不含**该 URI 任何片段；**未**先消毒再呈现 | 整体省略且零片段 | 消毒后呈现 / 片段输出 |
| **C18** | **省略可见性**：被省略的 artifact 产生**逐条原因提示 + 计数**，**不静默丢弃** | 有提示与计数 | 静默丢弃 |
| **C19** | **块预算**：引用块超 2000 → 停止追加 + 「引用块已截断」提示，**不抛错**、**不丢文本主体** | 成立 | 抛错 / 主体丢失 |
| **C20** | **快照保真（L4）**：`response_payload["artifacts"][i]["uri"]` 与原始 `Artifact.uri` **逐字节相同**（**未**因展示预算截断或改写）；`metadata` 同样完整保留；**无论**该 URI 能否在消息层呈现（含 `Zl` / `Zp` 的 URI 亦然——呈现层的净化 / 省略**不得**回溯影响持久化） | 逐字节相等 | 任一被截断 / 改写 / 丢弃 |
| **C21** | **结构化记录保真**：Local outbox `artifacts[i]["uri"]` 与原始 `Artifact.uri` **逐字节相同**（展示预算**不作用于**结构化记录；含 `Zl` / `Zp` 的 URI 亦保持**原值**，**未**被呈现层净化改写） | 逐字节相等 | 被截断 / 改写 |
| **C22** | **诚实边界**：被省略的引用**不得**在文案 / 契约 / 错误信息中被描述为终端用户可取得或可点击 | 零命中 | 任一命中 |

### D. Local outbox（D5）

| # | 条件 | PASS | FAIL |
|---|---|---|---|
| D1 | 无 artifact：行结构与旧版逐字节一致 | 一致 | 不同 |
| D2 | 有 artifact：行含 `artifacts` 键，元素为 7 字段完整映射 | 成立 | 字段缺失 |
| D3 | 无效 artifact → dispatch 抛 `AGENT_RESPONSE_INVALID` → **OutboundResponse 未构造** → 适配器未调用 → outbox **无新行** | 三段均成立 | 任一段不成立 |

### E. 运行时与事务（D3）

| # | 条件 | PASS | FAIL |
|---|---|---|---|
| E1 | Activity 类型集合仍为 4 个 | 未变 | 新增 |
| E2 | `activities` 表写入路径与事务数**不变**（无新事务） | 不变 | 新增事务 |
| E3 | `delivery_status` 取值集合**不变**（未新增 `delivered_partial` 等） | 不变 | 新增 |
| E4 | `processing_status` / `delivery_status` 两轴独立语义不变；失败路径不产 Result | 不变 | 变化 |
| E5 | Attempt Fence：`OutcomeCommit.REJECTED` 路径不写任何 Evidence | 成立 | 破坏 |
| E6 | `RUNTIME_EXECUTION_ORDER` 7 段不变；`RuntimeExecutionCoordinator` Protocol 不变 | 不变 | 变化 |
| E7 | 渲染失败（若经注入强制触发）**不**把已成功的文本交付改判为失败 | 成立 | 改判 |

### F. 回归

| # | 条件 | PASS | FAIL |
|---|---|---|---|
| F1 | 本轮基线全部仍绿：`test_acp_v0_2_artifact_implementation.py` + `test_acp_v0_2_contract_gate.py` —— **实施前基线 27 passed**；实施后 **28 passed**（+1 = 有意把 1 个测试拆为 2 个，**无断言删除**） | 实施后 28 passed | 任一失败 |
| F2 | 定向回归全绿（见 §9.4 命令） | 全绿 | 任一失败 |
| F3 | 全量 `pytest tests -q` —— **实施前基线 3 failed / 2839 passed / 41 skipped**；实施后 **3 failed / 2879 passed / 41 skipped**（passed **+40** = 新增 39 + 有意拆分 1），**3 项失败逐条相同**、**零新增失败** | 完全一致 | 新增失败或数字漂移 |

### 8.1 逐项证据映射（2026-10-26 实施后）

| 组 | 编号 | 条数 | 证据（文件::测试函数） |
|---|---|---|---|
| A 契约 | A1–A6 | 6 | A1 / A2 / A5 → `tests/test_step8_6_delivery_contract.py::test_c_inbound_outbound_symmetric`（内含 `_roots == {"dataclasses"}`）；A6 → `::test_c_agent_response_not_merged_with_outbound`（原三条断言 + 两条归属不交叉）；A3 → `tests/test_step9_A_runtime_slice_closure.py::test_frozen_contract_field_faces_unchanged` + `tests/test_acp_v0_2_contract_gate.py`（6 项，**未改动**）+ `tests/test_step15_5_5_5_sdk_contract_regression.py`；A4 → `tests/test_acp_v0_2_contract_gate.py::test_interaction_model_columns_unchanged` + `tests/test_acp_v0_2_artifact_implementation.py::test_no_expansion_artifact_not_in_db_schema` |
| B 兼容 | B1–B6 | 6 | B1 → `tests/test_acp_v0_3_artifact_outbound.py::test_b1_wecom_text_payload_unchanged_without_artifact`；B2 → `::test_b2_wecom_robot_payload_unchanged_without_artifact`；B3 → `::test_b3_bot_frame_unchanged_without_artifact`；B4 → `::test_b4_local_outbox_unchanged_without_artifact`；B5 → `tests/test_acp_v0_2_artifact_implementation.py::test_no_artifact_delivery_is_byte_identical`（+ 既有 `::test_dod6_old_agent_without_artifacts_still_succeeds`）；B6 → 三个 `compose_*` **函数体逐字节未变**（`git diff` 实证：本 Phase 对 `wecom.py` / `wecom_bot.py` 的改动**不涉及**这三个函数）+ 既有 wire 测试 `tests/test_step10_2_2_wecom_outbound_wire.py::test_a1_payload_mapping`、`tests/test_3b_2_2_wecom_bot_websocket_transport.py::test_a3_compose_respond_frame_matches_official_shape` |
| C 呈现与安全 | C1–C22 | 22 | C1–C19 / C22 → `tests/test_acp_v0_3_artifact_outbound.py::test_c01_…test_c19_` / `::test_c22_…`；C20 → `::test_c20_snapshot_keeps_full_original_uri`（DB 实证）+ `::test_c20_snapshot_keeps_unicode_separator_uri_verbatim`（R-1 加固）；C21 → `::test_c21_outbox_artifact_uri_is_verbatim_even_if_over_budget` + `::test_c21_outbox_keeps_unicode_separator_uri_verbatim`（R-1 加固）。**R-1 加固补充用例**：C6 → `::test_c06_unicode_separators_sanitised_in_free_text`；C17 → `::test_c17_unicode_separator_uri_omitted_entirely` |
| D Local outbox | D1–D3 | 3 | → `tests/test_acp_v0_3_artifact_outbound.py::test_d1_local_outbox_without_artifact_keeps_legacy_shape` / `::test_d2_local_outbox_with_artifact_full_original_fields` / `::test_d3_invalid_artifact_never_reaches_outbox`（DB + outbox 实证） |
| E 运行时与事务 | E1–E7 | 7 | E1 → `::test_e1_no_new_activity_type_introduced`；E2 → `::test_e2_no_new_persistence_write_site_for_artifacts`；E3 → `::test_e3_delivery_status_values_unchanged`；E4 → `::test_e4_artifacts_reach_outbound_response_through_coordinator`（DB 实证）；E7 → `::test_e7_artifact_processing_failure_does_not_flip_text_delivery`；**E5 → 既有 `tests/test_step12_9_4_12_attempt_fence.py`**；**E6 → 既有 `tests/test_step9_A_runtime_slice_closure.py`**（`CANONICAL_ORDER` 7 段 + Coordinator 纯度 + Protocol 不变） |
| F 回归 | F1–F3 | 3 | F1 → `test_acp_v0_2_artifact_implementation.py` + `test_acp_v0_2_contract_gate.py` = **28 passed**；F2 → §9.4 第 2 组 = **243 passed**；F3 → §9.4 第 3 组 = **3 failed / 2879 passed / 41 skipped**（失败项 = 实施前存量，逐条相同） |
| **合计** | A1–F3 | **47** | **47 条全部 PASS**（R-1 已闭合，2026-10-26 加固）；残留项 R-2–R-6 见 §8.2 |

**编号口径说明（2026-10-26 对账）**

- §8 的 B 组为 **B1–B6（6 条）**。实施期新增的**正向**呈现用例
  （`test_b5_wecom_text_payload_contains_reference_with_artifact`、
  `test_b6_wecom_robot_payload_contains_reference_with_artifact`、
  `test_b7_bot_frame_contains_reference_with_artifact`）是 **§8 之外**的**补充**覆盖
  （"有 artifact 时确实呈现了引用"），**不是** §8 的第 7 条 B 标准；且其中
  `test_b5_*` / `test_b6_*` 的**测试名**与 §8 的 B5 / B6 **语义不同**
  （§8 B5 = 旧 Agent 端到端回归；§8 B6 = `compose_*` 签名与输出未变）。
- **对账修正记录**：首轮《Phase 1 实施报告》把 B 组写作「7/7」并与「47/47」并列，
  致分组求和 48 ≠ 47 —— 属**报告的统计口径错误**（把 1 个 §8 之外的补充用例误计为
  §8 标准）。**§8 的规范计数 47 未变**；**以本表为准**。本轮**未删除任何测试**、
  **未合并任何安全要求**、**未降低任何验收标准**。

### 8.2 残留项（2026-10-26 实施后复核）

| # | 残留项 | 实测事实 | 影响 | 状态 |
|---|---|---|---|---|
| **R-1** | **U+2028 / U+2029（Zl / Zp 行/段分隔符）未被净化** | ✅ **已闭合（2026-10-26 加固）**：`app/domain/artifact_reference.py` 的 `_UNSAFE_TEXT_CATEGORIES` 扩为 `{"Cc","Cf","Zl","Zp"}`；URI 侧拆出 `_URI_FORMAT_CATEGORIES = {"Cc","Cf"}` 与 `_URI_SEPARATOR_CATEGORIES = {"Zl","Zp"}`，判定顺序**固定**（格式非法 > 行/段分隔符 > 标记字符）⇒ 省略原因与 URI 内字符顺序**无关**、**确定** | 自由文本 → **替换为空格**；URI → **整体省略** + 「引用包含行/段分隔符」，**零片段**、**绝不先消毒**；快照 / Local outbox 的**原值保真不受影响**（L4） | ✅ **已解决**：C6 / C17 的文字**已同步收紧**为明确口径（`Cc`/`Cf`/`Zl`/`Zp`）；证据 `test_c06_unicode_separators_sanitised_in_free_text`、`test_c17_unicode_separator_uri_omitted_entirely`、`test_c20_snapshot_keeps_unicode_separator_uri_verbatim`、`test_c21_outbox_keeps_unicode_separator_uri_verbatim` + 只读对抗性复验（U+2028 / U+2029 自由文本零泄漏、URI 零片段、原因顺序无关、正常 URI 仍逐字节原样） |
| R-2 | 客户端可能把**裸 URL** 自行加链接 | WeCom `msgtype=text` **无 markdown** → 代码跨度不生效，URI 为裸文本 | 客户端行为，ACP 无法控制 | ✅ 已登记于 §6.2 残余说明（既有） |
| R-3 | 平台侧文本长度上限**未在本仓库固化** | 2000 为 ACP 显式设计值；最坏情况实测（5000 artifact / 9990 条省略计数）总长 **≤ 2000** ✅ | 不影响策略正确性 | ✅ 已登记（§9.5 禁止宣称） |
| R-4 | URI 可达性 / 有效期 / 权限**不可证明** | ACP 从不解析 URI | 归 Phase 2 | ✅ 已登记（§6.4.3） |
| R-5 | 结构化记录映射与 `_snapshot_artifacts` **形状对齐但分处两地** | Artifact 未来扩字段需同步两处显式映射 | 既有「显式映射」纪律的固有代价 | ✅ 已登记 |
| **R-6** | **Local outbox 是以 `\n` 分隔的 JSONL** —— 消费者若用 `str.splitlines()` 读取，会连 **U+2028 / U+2029** 一并切开，把一条记录切成两半 | **写端（唯一生产者）**：`app/adapters/channels/local.py:170-174` —— `json.dumps(record, ensure_ascii=False)` + `handle.write(line + "\n")` ⇒ 分隔符**仅 `\n`**，且 `ensure_ascii=False` 使 U+2028 / U+2029 **原样落盘**。<br>**读端实测清单（2026-10-26 最终验收审计，全仓搜索）**：<br>① **既有测试 3 处 `splitlines()`** —— `backend/tests/test_step15_2_3_local_first_success.py:232`、`:272`、`:364`。**属既有测试，不在 Phase 1 授权范围内，本轮未修改**；其 payload 均不含 U+2028 / U+2029 ⇒ **当前无失败**，属**潜在（latent）**不一致。<br>② **Phase 1 新增测试文件内 0 处 `splitlines()`** —— `backend/tests/test_acp_v0_3_artifact_outbound.py` 的 4 处读点（`:467` / `:513` / `:523` / `:764`）已改为 `split("\n")`（与 R-1 用例 `:779` 口径一致）；另有 `:712` 为 `.strip()` **空检查**（非切分，不受影响）。<br>③ **生产代码读取 outbox：未发现** —— outbox 在生产路径上是**只写** sink（`local.py` 为唯一写入者）。<br>④ **仓库外消费者：未核实** —— 本次搜索覆盖工作树全部文件（排除 `.git` / `node_modules` / `__pycache__`），**未**覆盖开发者本地脚本、外部工具或未来下游读取方 ⇒ **不得**据此宣称「所有消费者均安全」。<br>**该性质是既有行为**（Agent 原文 `content` / `metadata` 本就可能含这些字符），**非 R-1 引入** | 消费者须按 `\n` 切分（`split("\n")` 或文件迭代）。本 Phase **未改** outbox 序列化（保持 `ensure_ascii=False` + `\n` 分隔）——改用 `ensure_ascii=True` 会把中文一并转义、**破坏 B4 / D1 的逐字节兼容**，故**不采用**。既有 3 处 `splitlines()` 读点的改造**不在 Phase 1 授权范围**内，留待后续独立授权 | ✅ **已登记**（**未**验证）—— 上述清单为 2026-10-26 最终验收审计的**实测**结果；「读者须按 `\n` 切分」目前**仅有文档约定，无机器化守卫**（可静默回退）→ 登记为已知可审计性缺口 |

---

## 9. 审查结论与决策项状态

### 9.1 已核实通过的项

| 项 | 结论 |
|---|---|
| D2 不变式与实测字段集合是否相符 | ✅ **相符**：`inbound - outbound` 实测恰为 `{external_interaction_id, external_identity_id}`；加入 `artifacts` 后 `outbound - inbound` 恰为 `{artifacts}`。**无额外差异** |
| 三条出站路径是否都能呈现引用 | ✅ 均可（WeCom 自建应用 text / 智能机器人 markdown / Bot WS markdown / Local 结构化 JSON） |
| 是否改变旧 Agent / 无 artifact 响应 / 旧 outbox 消费者 | ✅ **不改变**（B1–B6 覆盖） |
| 是否改变 Interaction 成功/失败语义、Attempt Fence、重试、事务边界 | ✅ **不改变**（§7 逐项 + E1–E7） |
| 是否新增 Activity 事件 / 事务 | ✅ **零新增**（D3） |
| 是否有数据访问面扩大 | ✅ **无**（出站呈现不读库，§6.5） |
| 现有代码能否证明安全边界 | ✅ 大部分可证明（§6.6）；**唯一不可证明项**是 URI 可达性/有效期/权限——已明确归 Phase 2，Phase 1 只如实标注「未验证」，**不构成 Phase 1 阻塞** |
| 超长 URI 能否在现有架构内定义出安全且无歧义的行为 | ✅ **能**（§6.4.2）：URI 为原子令牌——原样逐字节呈现或整体省略并说明原因；判定信息全部来自 `Artifact` 自身 + 渲染器确定性预算累计；**无**新 I/O / 存储 / 跨系统交互；与持久化语义正交 → **不构成实施前阻塞项** |
| 展示降级是否污染持久化 | ✅ **否**（§6.4.3）：快照与 Local outbox `artifacts` 均保留**完整原值**（§8 C20 / C21 机器化锁定） |
| 全量测试基线是否仍为任务书所述数字 | ✅ **是**（本轮实测 3 failed / 2839 passed / 41 skipped） |

### 9.2 决策项状态（**全部结案，2026-10-26**）

| # | 决策 | 裁决 | 落点 | 状态 |
|---|---|---|---|---|
| **Q1** | `test_c_agent_response_not_merged_with_outbound` 的 `agent & outbound` 在 D2 下破裂 | **选项 A** | §1.2.3 裁决记录；§5.2 处置行；§8 **A6** | ✅ **已结案** |
| **Q2** | 呈现限制的数值与超限处置 | **采纳基线**：最多 10 个 artifact / 名称 128 字符 / 引用块 2000 字符 / 控制字符安全处理；**并按补充要求撤销「URI 截断至 512」** | §6.4.1（设计基线）；§6.4.2（URI 原子策略）；§6.4.3（快照保真）；§8 **C15–C22** | ✅ **已结案** |
| **Q3** | 渠道平台侧文本长度上限未固化，2000 余量是否足够 | **采纳报告建议**：2000 是 ACP 显式设计值；**不得**声称已被证明符合企业微信平台所有相关长度上限；持续登记「平台侧上限未固化」 | §6.4.1（2000 行）；§6.4.2 末段 | ✅ **已结案**（残留：平台上限未固化，属待确认项，不影响策略正确性） |
| **Q4** | 是否登记进内部索引文档（内部治理文档，不随 OSS 发布） | **选项 A（不登记）**，保留报告依据 | §11（内部索引文档行） | ✅ **已结案** |
| **Q5** | Phase 1 实施分支 | **post-V0 阶段**，不得混入当前冻结的 Cloud V0 | §0；§9.3 约束 11；§9.4 | ✅ **已确认** |

**Q2 补充要求的落实（逐条对照）**：

| 补充要求 | 落实位置 | 状态 |
|---|---|---|
| 保留「最多 10 个 Artifact / 名称 128 字符 / 引用块 2000 字符 / 控制字符安全处理」为设计基线 | §6.4.1 设计基线表 | ✅ |
| **不得**未经分析就把超过 512 字符的 URI 直接截断后作为引用输出 | §6.4.2：**URI 为原子令牌 —— 原样呈现或整体省略**；原「512 截断」提案**已撤销**并留痕 | ✅ |
| 明确区分：展示长度限制 / URI 原值的完整性 / 超长 URI 无法完整呈现时的行为 / 原始快照持久化语义 | §6.4.0 的 **L1 / L2 / L3 / L4** 四概念表 + §6.4.1 / §6.4.2 / §6.4.3 | ✅ |
| 原始 Artifact 快照保持现有持久化语义，不新增存储或下载机制 | §6.4.3：`response_payload["artifacts"]` 保留**完整原值**，既有语义不变；新增存储 / 下载 / URL 代理 = **零** | ✅ |
| 超长 URI 的处理须明确避免把截断值描述成可用地址 | §6.4.2 第 2 点（省略提示**不含 URI 任何片段**、不暗示可点击地址）+ §8 **C16 / C22** | ✅ |
| 若无法定义安全且无歧义的行为 → 列为实施前阻塞项 | §6.4.2 末段：**在现有架构内完全可定义且无歧义**（判定信息全部来自 `Artifact` 自身 + 渲染器确定性预算累计；**无**新 I/O / 存储 / 跨系统交互；与持久化语义**正交**）→ **不构成实施前阻塞项** | ✅ 已判定**非阻塞** |

**结论：§9.2 无剩余待裁决项；Phase 1 无实施前阻塞项。**

### 9.3 实施约束汇总（实施 PR 必须遵守）

1. **零** `backend/` 之外的契约变更；**零** schema / 迁移；**零** Activity 事件 / 事务
2. `OutboundResponse.artifacts` **末尾**追加、默认 `None`、**不 import** `Artifact`（约束 C1）
3. **不**在 `delivery.py` 加 Artifact 校验（约束 C2）；校验留在 dispatch 侧
4. **不**用 `metadata` 承载 artifacts（D2 明令）
5. 新增符号名**不**含 `retry`/`replay`/`redeliver`/`outbox`（约束 C3）
6. 三条出站路径的 `compose_*` 纯函数**签名与输出不变**
7. 无 artifact 时**三条出站消息 + Local outbox 行逐字节不变**
8. 呈现文案**不**含完成态措辞，**必含**「未验证」语义；`metadata` 不渲染；`uri=None` 有明确提示
9. 端到端**零** URI 抓取 / 字节传输
10. **不**新增 API 端点、**不**改 `response_payload` 暴露行为、**不**宣称已脱敏
11. **URI 不截断、不改写、不消毒**：要么**原样逐字节**呈现，要么**整体省略**并给出明确原因；输出**不得**含该 URI 的任何片段（前缀 / 后缀 / 省略号形式），**不得**把被省略或被截断的值描述为可用地址（§6.4.2）
12. **展示预算只作用于消息文本**：`response_payload["artifacts"]` 与 Local outbox `artifacts` 必须保留**完整原值**，**不得**因展示预算而截断或改写（§6.4.3）
13. **省略必须可见**：被省略的 artifact 逐条给出原因 + 计数，**不静默丢弃**；省略**不抛错**（渲染器保持全函数）
14. **不**把被省略的引用描述为终端用户可取得或可点击——完整原值只在 Interaction 快照，经既有 tenant-scoped 管理读路径面向**有读取权的操作者**（§6.4.2 第 5 点）

### 9.4 拟执行的测试命令（实施后）

```
# 新增 + 调整（6 文件）
cd backend && python -m pytest tests/test_acp_v0_3_artifact_outbound.py \
  tests/test_acp_v0_2_artifact_implementation.py tests/test_acp_v0_2_contract_gate.py \
  tests/test_step8_6_delivery_contract.py tests/test_step9_A_runtime_slice_closure.py \
  tests/test_step10_1_1_acp_adapter_contract.py \
  tests/test_step10_5_1_interaction_identity_audit.py \
  tests/test_step10_5_3_duplicate_interaction_decision_semantics.py -q

# 定向回归（出站链 / 既有 E2E）
cd backend && python -m pytest tests/test_step10_2_2_outbound_e2e.py \
  tests/test_step10_2_2_outbound_adapter_contract.py \
  tests/test_step10_2_2_wecom_outbound_wire.py tests/test_step10_2_2_wecom_outbound_wire.py \
  tests/test_step12_8_2_wecom_response_url.py \
  tests/test_3b_2_2_wecom_bot_websocket_transport.py \
  tests/test_3b_3_3_wecom_bot_group_chat.py tests/test_step8_6_delivery_contract.py \
  tests/test_step9_6_delivery_execution.py tests/test_step15_2_3_local_first_success.py -q

# 全量
cd backend && python -m pytest tests -q -p no:cacheprovider --basetemp=<fresh-dir>
# PASS 判据：3 failed / 2839 passed / 41 skipped（且 3 项失败仍为记录的存量项）
```

### 9.5 明确不得宣称的事项（契约与实现均须遵守）

- ❌ 不得声称「文件已发送 / 已上传 / 已存储 / 已下载」
- ❌ 不得声称 ACP 已验证 URI 可用、安全或属当前租户
- ❌ 不得声称用户已收到或已显示该引用
- ❌ 不得声称 ACP 抓取、校验或代理了 URI 指向的资源
- ❌ 不得把**被省略**或**被截断**的引用描述成可用地址 / 可点击链接
- ❌ 不得暗示**终端用户**能取得被省略的引用（完整原值只在 Interaction 快照，经既有 tenant-scoped 管理读路径面向有读取权的操作者）
- ❌ 不得声称 2000 字符的引用块已被证明符合企业微信平台的所有相关长度上限（**平台侧上限未在本仓库固化**）
- ✅ 唯一允许的表述：**「Agent 提供的文件引用；ACP 未验证其可用性或安全性；引用已透传给渠道。」**
  （若某条引用因展示预算或格式原因被省略，追加：**「该引用未在消息中呈现。」**——**不得**附上任何 URI 片段）

---

## 10. 审查结论

# READY FOR APPROVAL

**含义**：范围、调用链核实、契约差异、安全边界、事务影响、决策收敛与验收条件**均已明确且可测试**；**Q1–Q5 全部结案，无实施前阻塞项**。本文**不构成 Phase 1 实施授权**——仍需项目负责人下一轮明确授权后方可编码。

| 维度 | 判定 |
|---|---|
| D2 契约差异与实测一致性 | ✅ 通过（不变式与实测字段集合**精确自洽**，无额外差异） |
| D3 事务与可观察性 | ✅ 通过（零新增 Activity 事件 / 零新增事务） |
| D4 安全边界 | ✅ 通过（**唯一不可证明项** URI 可达性/有效期/权限已明确归 Phase 2，Phase 1 只如实标注「未验证」，**不构成 Phase 1 阻塞**） |
| D5 Local 兼容 | ✅ 通过（正/负回归策略明确） |
| D1 版本边界 | ✅ 通过（未改 Freeze、未加 release-blocker、post-V0 分支已约束） |
| 调用链与三条出站路径 | ✅ 通过（逐段实测，呈现能力与限制已核实） |
| 既有契约测试影响 | ✅ 通过（8 条断言 / 6 文件已逐条定位；**无断言被删除**；`test_acp_v0_2_contract_gate.py` 无需改动） |
| 全量基线 | ✅ 通过（本轮实测复核，与任务书一致） |
| **Q1**（`agent & outbound` 不变式修订） | ✅ **已结案（选项 A）** —— §1.2.3 裁决记录 + §8 A6（**三条断言全部保留**） |
| **Q2**（呈现限制与超限处置） | ✅ **已结案** —— 基线（10 / 128 / 2000 / 控制字符）保留；「512 截断」**已撤销**，改为 **URI 原子呈现或整体省略**（§6.4.2）+ 快照保真（§6.4.3）+ §8 C15–C22 |
| **Q3**（平台侧长度上限未固化） | ✅ **已结案** —— 2000 为 ACP 显式设计值；**持续登记**平台上限未固化；§9.5 新增禁止宣称条目 |
| **Q4**（INDEX 登记） | ✅ **已结案（选项 A）** —— 不登记，依据保留于 §11 |
| **Q5**（实施分支） | ✅ **已确认** —— post-V0 阶段，不得混入冻结的 Cloud V0 |
| 实施前阻塞项 | ✅ **无**（超长 URI 行为已判定为可实现、无歧义、无新能力依赖 —— §6.4.2 末段） |

**授权实施时须逐项满足**：
1. §9.3 的 **14 条实施约束**
2. §8 的验收标准（A 6 条 / B 6 条 / C 22 条 / D 3 条 / E 7 条 / F 3 条，合计 **47 条**）
3. §9.4 的三组测试命令留证（新增 + 调整 / 定向回归 / 全量）

**边界重申**：**本文不构成 Phase 1 实施授权。** 即使结论为 READY，仍需项目负责人下一轮明确授权后方可编码；实施须在 **post-V0** 分支进行，PR 描述标注「非 V0 范围变更」而非 `release-blocker`。

---

## 11. 本轮工作树与边界审计

| 项 | 状态 |
|---|---|
| `backend/` 任何代码 / 测试 | ✅ **零改动**（`git diff --name-only` 实证） |
| 数据库模型 / schema / 迁移 / 数据库操作 | ✅ **零改动、零执行** |
| V0 冻结治理文档（内部治理文档，不随 OSS 发布） | ✅ **零改动**（实证） |
| 版本号 / 标签 / 分支 | ✅ 未改、未切 |
| commit / push / tag / 部署 / 公开发布 | ✅ **均未执行** |
| 既有 `README.md` 修改 | ✅ **未触碰**（仍为 12 insertions / 1 deletion） |
| Phase 0 的两份未提交文档修改 | ✅ **未触碰**（`docs/step-acp-v0.2-artifact-contract-gate.md` 仍为 140 / `docs/step-cloud-0.1.1-B-tenant-isolation-implementation-boundary.md` 仍为 2 —— 与决策收敛轮开始时**逐项一致**） |
| 未跟踪文件 `0`、`build` | ✅ **未触碰、未清理、未纳入变更** |
| **决策收敛轮修改的文件** | ✅ **仅本文件**（`docs/step-acp-v0.3-artifact-outbound-implementation-gate.md`）—— 本轮授权范围内唯一文件 |
| 本轮新增文件 | ✅ **无**（本文件于独立审查轮创建，本轮为**修改**） |
| 本轮修改的 Artifact 既有文档 | ❌ **无** —— D1–D5 全部记录于本文件；`docs/step-acp-v0.2-artifact-contract-gate.md` 作为 **V0.2 冻结记录**保持不动（其 §8「Delivery = DEFERRED」对 V0.2 仍然成立，Phase 1 属 post-V0，不回改 V0.2 契约） |
| 内部索引文档（内部治理文档，不随 OSS 发布） | ✅ 未改（**Q4 已裁决 = 选项 A**：不登记；依据见 §9.2 Q4 与本表） |
| 内部项目概览文档（内部治理文档，不随 OSS 发布） | ✅ 未改（实测零 Artifact 引用，无需对账） |
