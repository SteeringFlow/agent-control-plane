# M6.6-E3-B — WorkBuddy Provider-native Adapter Implementation Report

> **OSS 发布说明**：本文为历史实现报告；文中引用的 `frontend/`（Web UI 源码）**不包含在当前 OSS 发行内容中**（见 README「Project status」）。

> 前序：
> - `M6.6-D Provider Adapter & Real First Success Audit` — CONDITIONAL PASS（F3/F4 = WorkBuddy GAP）
> - `M6.6-E3-A WorkBuddy Provider-native Feasibility Audit` — CONDITIONAL PASS（可行，Core + Adapter 边界足以支撑）
>
> 本轮：**实现** WorkBuddy Provider-native 接入（F3/F4），把用户 first-use 从
> `endpoint / bearer_token / runtime_id` 手动粘贴，收敛为 `Agent ID + API Key + Connect`。
> **架构红线**：不新增 Domain Entity / migration / ACP Core 改动；WorkBuddy 复杂性全部停在 Adapter 边界内。

---

## 1. Scope

实现（in-scope）：
- 新增 WorkBuddy **Management API 客户端**（`POST /v2/agentos/runtimes`）；
- **Lazy Runtime provisioning on Verify**（Connect 不创建）；
- Runtime 缓存写入既有 `Connection.config`（reuse 既有 tencent-acp-sse 键）；
- 一个 **Provider verify 端点**（`POST /api/connections/{id}/verify`），其余复用既有 API；
- Frontend WorkBuddy first-use 字段（Agent ID + API Key），隐藏 `runtime_id / acpLink / endpoint / bearer_token`；
- 契约 / 行为测试 T1–T12。

非目标（out-of-scope，见 §16）：
- 不新增 `WorkBuddyAgent / WorkBuddyRuntime / WorkBuddySession` 等 Domain 实体；
- 不新增 migration / Core model / Runtime stage / Bootstrap / Session / Authorization / Delivery 改动；
- 不实现 Agent discovery（V0：用户提供 Agent ID）；
- 不新增 ACP 错误 taxonomy。

---

## 2. Implementation Summary

新增 / 修改文件（**本轮唯一改动面**）：

| 变更 | 文件 | 说明 |
|---|---|---|
| 新增 | `backend/app/adapters/agents/workbuddy.py` | Management API 客户端 + lazy provisioning 组合（Adapter 层） |
| 新增 | `backend/tests/test_m6_6_e3_b_workbuddy_provider_native.py` | T1–T12 契约/行为测试 |
| 修改 | `backend/app/api/management/connections.py` | 新增 `POST /{id}/verify`（+ `verify` 注入参数） |
| 修改 | `backend/app/main.py` | 装配 `WorkBuddyProvisioner` 到 connections API |
| 修改 | `frontend/connect.html` | WorkBuddy first-use 字段（Agent ID + API Key） |
| 修改 | `frontend/connect.js` | 构造 provider config；WorkBuddy 走 verify 端点 |
| 修改（守卫同步） | 11 个 `backend/tests/test_step*.py` / `test_persistence_smoke.py` | Adapter 文件清单白名单随新增适配器同步（见 §16） |

不变式（本轮**零改动**）：`application/`（Router / Worker / Session / Interaction / Identity /
Grant / Authorization / Bootstrap / Delivery / Runtime pipeline）、`domain/`、`persistence/`、
migration、ACP Core。

---

## 3. Management API Client

`backend/app/adapters/agents/workbuddy.py`：

- `WorkBuddyManagementClient.create_runtime(*, api_key, agent_id, runtime_name) -> AcpProvision`
  - `POST {base}/v2/agentos/runtimes`；
  - headers：`Authorization: Bearer <API Key>` + `Content-Type: application/json` +
    可选 `X-Source-App`（见 §17 条件）；
  - body：`runtimeName` + `agentManifest{id, name, manifestVersion:"1.0"}`；
    **不含 `secrets`**；
  - 响应解析：`data.id` / `data.links.acpLink.url` / `data.links.acpLink.token` →
    `AcpProvision(runtime_id, url, token)`（Adapter 内部 frozen 值对象，**非 Domain Entity**）；
  - 失败一律 `WorkBuddyManagementError`（Adapter 内部；不泄漏成 ACP 错误码）。
- `transport` 可注入（测试确定性桩，不触网）；默认 `_default_transport` 用
  `http.client`（与 `acp.py` 同族，无新依赖）。
- `WorkBuddyManagementError` 消息**只含状态码 / 类型名**，绝不含 API Key / token / 完整 URL。

契约来源：`docs/step-10.1.3-tencent-acp-wire.md`（仓库已保存的 Tencent ACP Runtime 事实）。

---

## 4. Connection Config

**最小兼容**（沿用既有 `tencent-acp-sse` 键，**未新增 config schema**）：

用户 Connect 输入（Provider-native）：
```json
{ "provider": "workbuddy", "workbuddy_agent_id": "<Agent ID>", "api_key": "<API Key>" }
```

Verify 后缓存写回（reuse 既有键 → 通信路径零改动）：
```json
{ "provider": "workbuddy", "workbuddy_agent_id": "...", "api_key": "<masked>",
  "protocol": "tencent-acp-sse", "endpoint": "<acpLink.url>",
  "bearer_token": "<acpLink.token，masked>", "runtime_id": "<runtime.id>" }
```

- `Connection` 模型**未改**（无新列）；Runtime 仅作 config 缓存值，**未提升为 Domain Entity**。
- `protocol` 复用 `tencent-acp-sse` → 既有 `ACPAdapter` 通信路径直接消费（§8/§9）。

---

## 5. Credential Boundary

- **API Key**：只用于 Management API `Authorization: Bearer`（管理面）。
- **acpLink.token**：只用于 ACP 通信面 `Authorization: Bearer`（缓存为 config `bearer_token`）。
- 二者均命中既有 `SENSITIVE_CONNECTION_CONFIG_KEYS`（`api_key`, `bearer_token`）→
  读时脱敏 `********`；**未新增敏感键**（无需改脱敏系统）。
- **绝不**进入 `agentManifest.secrets`（T3 断言请求体无 secrets、无 API Key）。
- 不写日志（仅记失败类型名）、不进 URL、不进 Interaction / Activity、不进普通 GET 响应。
- User-visible：Agent ID、API Key（password 输入框，保存后不回显）；Internal-only：
  `runtime_id / acpLink / endpoint / bearer_token / Connection ID`。

---

## 6. Runtime Lifecycle

- **Connect**（`POST /api/connections`）= 只建立 `Channel ↔ Agent` 绑定 + 存 `api_key`；
  **不创建 Runtime**（T4）。
- **Verify**（`POST /api/connections/{id}/verify`）= Provider execution verification：
  读 config → 无缓存则调 Management API 创建 Runtime（lazy）→ 探针 → 缓存写回（T5）。
- 复用：config 已有完整缓存（`endpoint + bearer_token + runtime_id`）时**只探针、不重复创建**
  （T6，§17）。
- Fail closed：已缓存但不可达 → **不重复创建**（无可靠状态信息，`test_t7c`）。

---

## 7. Runtime Cache

`WorkBuddyProvisioner.verify(connection) -> (snapshot, updated_config | None)`：
- `updated_config is None` → 无写入（非 WorkBuddy / 缓存已存在）；
- `updated_config` 非空 → 由 **管理 API** 经既有
  `ConnectionApplicationService.update_connection(UpdateConnection(config=...))` 写回
  （config 整段替换）——**Provisioner 本身不写库**（与 ACPAdapter 只读纪律一致）。

---

## 8. ACPAdapter Integration

- **`ACPAdapter` 本体零改动**（`send` / `probe` / tencent-acp-sse binding 原样）。
- WorkBuddy provider path = `WorkBuddyProvisioner`（组合既有 `ACPAdapter.probe` + 新
  Management client）；**Router / Worker / Session / Interaction 完全不认识 WorkBuddy**。
- 结构：
  ```
  ACP Core → Agent(type="acp") → Connection
                                   ↓
                              ACPAdapter（既有通信）
                              WorkBuddyProvisioner（仅 Verify 供给 Runtime）
                                   ↓
                              WorkBuddy Management API → Runtime
  ```

---

## 9. ACP SSE Reuse

Verify 生成的缓存使用 `protocol="tencent-acp-sse"` + `endpoint/bearer_token/runtime_id`，
因此 WorkBuddy 执行**直接进入既有 `acp.py`**（GET SSE + jsonrpc `session/prompt` +
`agent_message_chunk` + `isFinalStop`），**未重新实现 ACP SSE**（T9 断言命中既有 binding）。

---

## 10. Connect UX

`frontend/connect.html` / `connect.js`（统一 M6.3 Connect Flow，**冻结页 connect-agent.* 未改**）：

- first-use 显示：**WorkBuddy Agent ID** + **API Key**（`f-wb-agent-id` / `f-wb-api-key`）；
- 构造 config：`{provider:"workbuddy", workbuddy_agent_id, api_key}`（与高级 JSON 合并）；
- 隐藏：`runtime_id / acpLink / endpoint / bearer_token`（不再要求用户理解/填写）；
- 约束保持：base API 字面仍恰为 4 个（`test_b1`）、health 派生路由仍在（`test_b2`）、
  仅 GET/POST（`test_m1`）、无 new machinery 标签（`test_j2/j3`）。

---

## 11. Verify UX

- 非 WorkBuddy：`GET /{id}/health`（只读探针，行为不变）；
- WorkBuddy：`POST /{id}/verify`（Provider verify，可 lazy 供给 Runtime）；
- `503 = 验证暂不可用（非连接结论）`；成功文案 `WorkBuddy 已就绪`。
- **Connect ≠ Verify**：Connect 成功=绑定建立；仅 Verify 成功才表示 Provider ready（§6）。

---

## 12. Error Mapping

- Provider 失败 → **既有** 错误模型：`ACPAdapter` 保留 `AgentDispatchFailedError` /
  `AgentResponseInvalidError`；Provisioner 失败 → `AgentProbeSnapshot(reachable=False,
  detail="workbuddy connection could not be verified")`。
- **未新增** `WORKBUDDY_RUNTIME_CREATE_FAILED` / `WORKBUDDY_API_KEY_INVALID` 等错误码。
- 用户可见错误文案**泛化**，不含 HTTP 401 / runtime_id / acpLink / token。

---

## 13. Tests

`backend/tests/test_m6_6_e3_b_workbuddy_provider_native.py`（18 项）：

| # | 覆盖 | 测试 |
|---|---|---|
| T1 | Provider-native config / 缓存写回 | `test_t1_workbuddy_config_namespace_and_cache_writeback`, `test_t1_non_workbuddy_connection_is_read_only_probe` |
| T2 | API Key / token 脱敏 | `test_t2_api_key_and_runtime_token_masked_on_read` |
| T3 | 不注入 `agentManifest.secrets` | `test_t3_api_key_never_in_agent_manifest_secrets`, `test_t3b_source_app_header_only_when_configured` |
| T4 | Connect 不创建 Runtime | `test_t4_connect_does_not_create_runtime` |
| T5 | Verify 创建 Runtime | `test_t5_and_t6_verify_creates_then_reuses_runtime` |
| T6 | 二次 Verify 复用 | 同上 |
| T7 | 响应解析（id/url/token）/ Fail closed | `test_t7_...`, `test_t7b_...`, `test_t7c_...` |
| T8 | acpLink token 不泄漏 | `test_t8_verify_never_leaks_runtime_token_in_http_response` |
| T9 | 复用既有 ACP SSE client | `test_t9_cached_runtime_uses_existing_acp_sse_binding` |
| T10 | 失败映射既有 error model | `test_t10_provider_failure_maps_to_existing_error_model` |
| T11 | 不新增 Core / Domain 分叉 | `test_t11_workbuddy_lives_only_in_adapter_layer` |
| T12 | 既有 ACP path 回归 | `test_t12_existing_acp_bindings_unchanged` |
| — | HTTP 边界 | `test_verify_endpoint_503_when_not_wired`, `test_verify_endpoint_404_missing_connection`, `test_health_endpoint_stays_read_only_for_workbuddy` |

结果：**18 passed**。

---

## 14. Regression

- 新测试 + 关键守卫批（M6.3 UX / health probe / architecture gate / redaction）：
  **133 passed**。
- 受新增 Adapter 影响的守卫批（13 文件）：**258 passed**。
- **全量** `python -m pytest tests/ -q`：**2291 passed, 36 skipped, 0 failed**（49 `ERROR at
  setup` 为**环境级** `PermissionError`——陈旧且无写权限的 pytest 临时目录
  `...\Temp\pytest-of-<user>`，与本次改动无关；以隔离 `--basetemp` 复跑同一批文件 →
  `52 passed, 6 skipped, 0 errors` 证明）。

---

## 15. Real E2E Status

**NOT VERIFIED**——本仓库无真实 WorkBuddy `API Key` / `Agent ID`，故：
- 未对真实 `POST /v2/agentos/runtimes` 发起调用；
- 未跑通「真实 Runtime → 真实 Agent 执行 → Delivery」。

不伪造真实 E2E，不以静态/桩测试声称 PASS。真实 E2E 待凭据就绪后重跑（无需改代码）。

---

## 16. Scope Integrity

`git diff` 确认**本轮改动**：

```
backend/app/adapters/agents/workbuddy.py               (新增)
backend/app/api/management/connections.py              (+verify 端点)
backend/app/main.py                                    (装配 provisioner)
frontend/connect.html                                  (WorkBuddy 字段)
frontend/connect.js                                    (provider config + verify)
backend/tests/test_m6_6_e3_b_workbuddy_provider_native.py (新增)
+ 11 个既有守卫测试：Adapter 文件清单白名单同步（设计即「随新增适配器增长」）
```

确认**无**：
- ❌ migration（`alembic/versions/` 无新增）
- ❌ Domain Entity（无 `WorkBuddy*` 分叉类；无 `Runtime` 实体）
- ❌ Core model / Runtime stage
- ❌ Bootstrap / Session / Authorization / Delivery 改动
- ❌ `application/` / `domain/` / `persistence/` 改动

> 说明：仓库工作树在本轮前已有大量未提交改动（M6.6-C3/C4/D 等其他轮次），不属于本轮；
> 上表为本轮实际改动面。守卫更新属「白名单随实现增长」的既定语义（`test_step10_1_4`
> 原文：「白名单随『已实现的传输 Adapter』同步增长——这正是本守卫的语义」）。

---

## 17. 最终验证矩阵

| 项目 | 结果 |
|---|---|
| Provider-native Agent config | **PASS**（`provider/workbuddy_agent_id/api_key` → 缓存写回既有键） |
| API Key authentication | **PASS**（管理面 `Authorization: Bearer`） |
| API Key masking | **PASS**（`api_key`/`bearer_token` 读时 `********`） |
| No API Key in secrets | **PASS**（请求体无 `agentManifest.secrets`） |
| Lazy Runtime creation | **PASS**（Connect 不创建；Verify 创建） |
| Runtime reuse | **PASS**（二次 Verify 不重复创建） |
| ACP SSE reuse | **PASS**（复用既有 tencent-acp-sse binding，零重实现） |
| Runtime token protection | **PASS**（不回显 / 不日志 / 不进响应 / 不进 Interaction） |
| Existing ACP path regression | **PASS**（minimal / tencent binding 未变） |
| Core unchanged | **PASS**（application/domain/persistence/migration 零改动） |
| WorkBuddy real E2E | **NOT VERIFIED**（无真实凭据） |

---

## 18. Final Gate

**CONDITIONAL PASS**

理由：
- **代码层实现完成、架构边界全部成立、测试全绿**：Core 零改动、WorkBuddy 复杂性全部停在
  Adapter 边界内（`ACPAdapter` 本体未改；无 Domain 分叉）；Lazy creation / reuse /
  credential boundary / error model 均按 E3-A 冻结规则落地。
- **明确的前置条件（需外部确认，非代码缺口）**：
  1. **`X-Source-App` 官方取值未确认**（仓库仅有单份转录文档，`docs/step-10.1.3`）——
     实现**不猜生产值**，仅由 `Connection.config["source_app"]` / 环境变量
     `WORKBUDDY_SOURCE_APP` 提供；缺失时不发送该头（`test_t3b`）。
  2. **Management API schema 与 Runtime readiness（CREATING/READY）需以官方文档复核**
     （当前契约来自单份转录文档）。
- **真实 WorkBuddy E2E = NOT VERIFIED**（无凭据）；按规则不阻止代码层交付。

> 即：Provider-native 实现已就绪，剩余为**官方 API / runtime behavior 的外部确认** +
> **真实凭据 E2E**，均无需改动 ACP Core / Domain。若官方确认后 `X-Source-App` 等取值固定，
> 可直接进入真实 E2E（不改代码）。

---

*本报告为 M6.6-E3-B 实现交付说明。代码改动仅限 §2 / §16 所列文件；未新增 Domain Entity /
migration / ACP Core 改动；未 commit / tag。*
