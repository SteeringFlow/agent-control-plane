# ACP V0.2 Artifact Contract Gate — Contract Design + Impact Audit

> 状态：**设计 / 测试 ONLY，未落地任何生产实现。**
> 范围：本 Gate 只在既有契约骨架上做**加性扩展 + 镜像**，不改变任何已冻结的字段面 /
> 执行语义 / schema / migration。每一项 DoD 在下方 §1–§13 给出裁决、字段面规格与证明。
> 机器化锁定测试见 `tests/test_acp_v0_2_contract_gate.py`（对当前代码即可跑，全部为回归护栏）。

---

## 0. 全局约束（来自既有冻结裁决）

| 既有裁决 | 来源 | 对 V0.2 的硬限制 |
|---|---|---|
| `RuntimeExecutionResult` 字段面**恰好三**，禁止未经审计扩展 | `app/domain/runtime_execution_result.py:35-37`（裁决 8） | **artifact 不得进入 Result** |
| 项目状态语义全部是 `str` 常量，不新建 Enum | `runtime_execution_result.py:17-19` | `Artifact.kind` 用 `str` 常量，不建 Enum |
| `InteractionModel` 演进只走 `create_all`，Alembic 工具链推迟 | `persistence/models/interaction.py:15-16` | **不新增列 / 表 / migration** |
| SDK 绝不 `import app.*`，是 domain 独立副本镜像 | `agent_runtime_sdk/models.py:1-7` | `Artifact` 在 SDK 侧**重新镜像**，非 import |
| minimal wire 响应 `{"content","session_id"}`，`metadata` 不进 | `agent_runtime_sdk/minimal.py:10-21` | `artifacts` 同样**不进 minimal wire** |
| 值对象 frozen，不内嵌校验 | `runtime_execution_result.py:39-41` | `Artifact` frozen，校验在生产者侧 |

**核心设计结论**：artifact 以「`AgentResponse` 上的可选加性字段」形式存在（不触碰
`RuntimeExecutionResult`、不触碰 `Interaction` 列 / migration、不触碰 `AgentRequest`、
不触碰 Core Runtime 执行顺序）。它是 `AgentResponse` 数据面的富化，**不是**新的一等
持久化实体、不是新执行阶段、不是新 wire 原语。

---

## 1. Artifact value-object contract（DoD #1）

**新文件**：`app/domain/artifact.py`（纯 domain frozen 值对象，不 import SQLAlchemy /
FastAPI / app.*，与 `AgentResponse` 同层）。

**字段面（加性、开放）**：

```python
from dataclasses import dataclass

# 复用项目 str 常量风格（不建 Enum；单一事实源）
ARTIFACT_KIND_FILE = "file"      # 落盘 / 可下载文件引用
ARTIFACT_KIND_DATA = "data"      # 结构化数据对象（JSON 等）
ARTIFACT_KIND_LINK = "link"      # 外链 / 资源指针
ARTIFACT_KIND_IMAGE = "image"    # 图片类资源
# kind 不穷举：未知 kind 由消费侧按 "file-like" 默认处理，不抛错。

@dataclass(frozen=True)
class Artifact:
    artifact_id: str                       # 必填：不透明唯一标识（CP 或 Agent 提供）
    kind: str                             # 必填：类型判别（用上方 str 常量）
    name: str                             # 必填：人类可读名
    uri: str | None = None                # 可解析位置（可为 None：延迟交付，见 §8）
    mime_type: str | None = None          # 可选：MIME
    size_bytes: int | None = None         # 可选：字节大小
    metadata: dict | None = None          # 可选：protocol-neutral 辅助（同 AgentResponse.metadata）
```

**契约要点**：
- `artifact_id` 必填且为唯一事实；其余全部可选（加性、向后兼容）。
- `kind` 用 `str` 常量（与 `session_mode` / `processing_status` 单一事实源纪律一致）。
- frozen，校验在生产者侧（与 `RuntimeExecutionResult` 同风格）。
- 不引入新持久化表 / 列；artifact 随 `AgentResponse` 一起被快照进 `response_payload` JSON。

---

## 2. AgentResponse additive extension（DoD #2）

**文件**：`app/domain/agent_response.py:20-26`（**加性修改字段面，不删不改既有字段**）。

```python
@dataclass(frozen=True)
class AgentResponse:
    content: str
    external_session_id: str | None = None
    metadata: dict | None = None
    artifacts: list[Artifact] | None = None   # ← V0.2 加性字段，默认 None
```

**加性证明**：既有构造式 `AgentResponse(content=..., external_session_id=..., metadata=...)`
（见 `adapters/agents/acp.py:653`、`adapters/agents/local.py`、`agent_dispatch.py:108-115`）
**全部仍合法**——`artifacts` 有默认值 `None`，不要求调用方改动。字段面向后兼容。

**`RuntimeExecutionResult.agent_response` 复用不变**：`AgentResponse` 内部增设 `artifacts`
**不**改变 `RuntimeExecutionResult` 的三字段面（§10）。

---

## 3. SDK mirror contract（DoD #3）

**文件**：`agent_runtime_sdk/models.py`（**独立镜像**，不 import `app.*`）。

- 新增 `Artifact` 镜像（字段面与 `app/domain/artifact.py` **逐字段相等**）。
- `SDK AgentResponse` 加性镜像 `artifacts: list[Artifact] | None = None`。
- `agent_runtime_sdk/__init__.py` 导出 `Artifact`。

**字段面对等断言（机器化锁定）**：`set(SDK.AgentResponse.__dataclass_fields__) ==
set(domain.AgentResponse.__dataclass_fields__)`，且 `SDK.Artifact` 字段面 == `domain.Artifact`
字段面。任一漂移 → 测试失败（见 `tests/test_acp_v0_2_contract_gate.py`）。

---

## 4. backward compatibility proof（DoD #4）

| 既有消费点 | 位置 | 兼容性结论 |
|---|---|---|
| `_normalize_acp_response` 构造 `AgentResponse(content, external_session_id)` | `adapters/agents/acp.py:653` | ✅ `artifacts` 默认 None，不需改 |
| local adapter 构造 `AgentResponse` | `adapters/agents/local.py` | ✅ 同上 |
| `_valid_response` 校验（content 非空 / session_id str\|None / metadata dict\|None） | `application/agent_dispatch.py:131-142` | ✅ 不校验 `artifacts`，加性字段不影响 |
| `RuntimeExecutionResult(... agent_response=response)` | `application/coordinator.py:159-163` | ✅ 复用 `agent_response`，字段面不变 |
| `SDK AgentResponse` 镜像 | `agent_runtime_sdk/models.py:40-50` | ✅ 同步加性 `artifacts`，旧用法仍合法 |

**结论**：所有既有构造 / 校验 / 快照点均不强制要求 `artifacts`，加性扩展**零破坏性**。

---

## 5. persistence snapshot contract（DoD #5）

**文件**：`application/interaction_recording.py:268-272`（`record_success` 的 `response_payload`）。

**加性 key**：`response_payload` dict 在原三项（`content` / `external_session_id` /
`metadata`）之外，新增可选 key `artifacts`：

```python
response_payload={
    "content": result.agent_response.content,
    "external_session_id": result.agent_response.external_session_id,
    "metadata": result.agent_response.metadata,
    "artifacts": [asdict(a) for a in (result.agent_response.artifacts or [])],  # 新增
}
```

**向后兼容读取**：旧行无 `artifacts` 键 → 视作空列表（与 `interaction_snapshot.py:31-32`
「旧数据缺键取默认」纪律一致）。`InteractionDetailResponse`（`schemas/interactions.py:73-78`）
对 `response_payload` 是**原样透传**（只读事实面、不重新解释），artifact 自动随快照流出，
**无需改 schema**。

---

## 6. Minimal binding compatibility（DoD #6）

**文件**：`agent_runtime_sdk/minimal.py`（**不变**）。

- `encode_response` 输出仍严格为 `{"content", "session_id"}`（`minimal.py:78`）。
- `artifacts` **不进 minimal wire**（与 `metadata` 同一冻结规则 `minimal.py:21`）。
- `decode_request` 不变（`AgentRequest` 字段面不变，§9）。
- 因此 minimal binding 对外契约**零改动**；artifact 通过更富的 wire 契约（非 V0.2 范围，
  见 §7）才上 wire，V0.2 下它只在内部 `AgentResponse` / `response_payload` 快照中存在。

**锁定断言**：`set(encode_response(AgentResponse(content="x")).keys()) == {"content","session_id"}`，
且 `encode_response` 不引用 `artifacts` 属性。

---

## 7. Tencent streaming boundary = DEFERRED（DoD #7）

`agent_runtime_sdk/tencent_acp_sse.py` **不变**。V0.2 不定义 artifact 的流式承载原语；
artifact 在 V0.2 仅以 `AgentResponse.artifacts` 同步字段存在，不进入 SSE chunk 流。
流式暴露作为后续 Step（与 15.5.5.3 冻结边界一致）单独审计。

## 8. Delivery boundary = DEFERRED（DoD #8）

`app/adapters/.../delivery`（任何 Channel delivery）**不变**。V0.2 不把 artifact 投递到
终端 channel；artifact 随 `AgentResponse` 进 `response_payload` 快照（§5），但渠道投递内容
仍是 `content`（既有 `OutboundResponse` / delivery 契约不受影响）。延迟交付（如 `uri=None`
待回填）属后续 Step。

---

## 9. AgentRequest unchanged（DoD #9）

`app/domain/agent_request.py:34-42` **不变**。字段面锁定为：
`{agent_id, control_plane_session_id, content, external_session_id, metadata}`（5 字段，
`metadata` 之外全必填，且「禁止携带」结构锁定 `user_id/identity_id/...`，见 `agent_request.py:20-28`）。
**V0.2 不新增任何字段**（artifact 不反向流入请求）。

## 10. RuntimeExecutionResult unchanged（DoD #10）

`app/domain/runtime_execution_result.py:59-77` **不变**。字段面严格为：
`{session_mode, session_id, agent_response}`（恰好三，裁决 8 禁止扩展）。
**artifact 不进入 Result**；它经由 `agent_response.artifacts` 顺带携带（§2），Result 的
三字段面与「成功路径不变量」（session_id 必填、session_mode 两值、agent_response 非 None）
**完全不变**。既有机器化锁定测试 `tests/test_step10_8_2_runtime_execution_result_contract.py`
继续有效，并追加「`artifacts` 不在字段面」断言。

## 11. Interaction schema / migration unchanged（DoD #11）

`app/persistence/models/interaction.py:42-87` **不变**，`alembic/versions/0001_baseline_core_tables.py`
**不变**。artifact 以 **JSON key** 形式进入既有 `response_payload`（`JSON` 列，
`interaction.py:60`），**不新增列、不新增表、不新增 migration**。交互表列集合保持原 15 列：
`id, channel_id, session_id, direction, external_interaction_id, request_payload,
response_payload, processing_status, delivery_status, failure_stage, failure_code,
retryable, processing_started_at, created_at, updated_at`。

---

## 12. exact implementation file set（DoD #12）

**设计 / 测试允许的改动**（落地实现时执行，本次 Gate 不实现）：

| 动作 | 文件 | 内容 |
|---|---|---|
| 新建 | `app/domain/artifact.py` | `Artifact` frozen 值对象 + `kind` str 常量 |
| 修改 | `app/domain/agent_response.py` | 加性 `artifacts: list[Artifact] \| None = None` |
| 修改 | `app/domain/__init__.py` | 导出 `Artifact` |
| 修改 | `app/application/interaction_recording.py` | `record_success` 快照加 `artifacts` key（§5） |
| 新建 | `agent_runtime_sdk/artifact_mirror.py` 或并入 `models.py` | `Artifact` SDK 镜像 + `AgentResponse.artifacts` 镜像 |
| 修改 | `agent_runtime_sdk/__init__.py` | 导出 `Artifact` |
| 新建 | `tests/test_acp_v0_2_contract_gate.py` | 机器化锁定（本 Gate 已交付，可立即跑） |

**明确 NOT touched（DoD #7–#11, #13）**：
`agent_request.py`、`runtime_execution_result.py`、`persistence/models/interaction.py`、
`alembic/versions/*`、`app/domain/interaction_snapshot.py`、`agent_runtime_sdk/minimal.py`、
`agent_runtime_sdk/tencent_acp_sse.py`、任意 delivery adapter、`app/schemas/interactions.py`、
`app/application/coordinator.py`（执行语义）、`app/application/agent_dispatch.py`（仅其
`_valid_response` 被动兼容，无需改）。

---

## 13. no accidental Core Runtime semantic change（DoD #13）

- `RuntimeExecutionCoordinator.execute(inbound) -> RuntimeExecutionResult` 形状不变
  （`application/runtime.py:85-112`）。
- `RuntimeCoordinator.execute` 仍按 `RUNTIME_EXECUTION_ORDER`
  （identity_resolution, routing, session_resolution, session_creation, agent_dispatch,
  delivery）串六段，最后构造 `RuntimeExecutionResult(session_mode, session_id, agent_response)`
  （`application/coordinator.py:75-82, 159-163`）——**无新执行阶段、无新顺序、无新异常路径**。
- artifact 只在**成功路径**经 `agent_response.artifacts` 顺带携带；失败路径仍走 typed 异常
  通道，不产 Result 对象（与既有 `runtime_execution_result.py:25-27` 一致）。
- `session_mode` / `session_id` 语义、session 绑定（`interaction_recording.py:282-294`）
  完全不变。

**锁定断言**：`RuntimeCoordinator` 公开符号与 `RuntimeExecutionCoordinator.Protocol` 字段面
不变；`agent_dispatch.AgentDispatchExecutor.execute -> AgentResponse` 返回类型不变。

---

## 14. 机器化锁定测试（交付物）

`tests/test_acp_v0_2_contract_gate.py`（对当前代码可运行）覆盖：

1. `RuntimeExecutionResult` 字段面 == `{session_mode, session_id, agent_response}`，且 `'artifacts' not in`。
2. `AgentRequest` 字段面 == 5 字段基线，且 `'artifacts' not in`。
3. `InteractionModel` 列集合 == 15 列基线，且 `'artifacts' not in`。
4. SDK `AgentResponse` 字段面 == domain `AgentResponse` 字段面（镜像对等，前瞻 artifact 字段）。
5. `minimal.encode_response` 输出 key 集合 == `{"content","session_id"}`（artifact 不进 wire）。
6. 前瞻：若 `app.domain.artifact.Artifact` 已落地，断言其字段面 == §1 规格；否则 skip
   （保证测试在当前「未实现」状态下仍全绿，落地后自动激活为硬锁）。

**Gate PASS 判据**：以上全部为绿 + §7/§8 两个 DEFERRED 边界确认无改动 + §12 文件集与
实际 diff 一致。
