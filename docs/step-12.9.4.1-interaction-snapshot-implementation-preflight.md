# Step 12.9.4.1 — Interaction Snapshot Implementation Preflight

> 实现前只读探查。目标：为 12.9.3.5 发现的 `request_payload` 漏存
> `inbound.metadata` 缺口，确认「加 `"metadata": inbound.metadata`」是否可安全落地，
> 并锁定其真实影响面。仅探查，不改代码。

证据锚点：
- `domain/inbound_interaction.py:66`（metadata 定义）
- `application/interaction_recording.py:166-172`（`_start` 的 request_payload 构造）
- `adapters/channels/wecom.py:333-339, 383-390`（InboundInteraction 仅有的两处构造）
- `persistence/mappers/interaction_record.py:17,34`（request_payload 透传）
- `persistence/models/interaction.py:58`（JSON 列，`none_as_null=True`）
- `adapters/channels/wecom.py:234-248`（deliver 消费 metadata["response_url"]）
- `tests/test_step10_5_2_*.py:196`、`tests/test_step10_7_1_*.py:173`（精确字典相等断言）

---

## 1. `InboundInteraction.metadata` 的真实类型与定义位置

- **位置**：`domain/inbound_interaction.py:66`
  ```python
  @dataclass(frozen=True)
  class InboundInteraction:
      channel_id: int
      external_interaction_id: str
      external_conversation_id: str
      external_identity_id: str
      content: str
      metadata: dict | None = None      # ← 定义处
  ```
- **类型**：`dict | None`，默认 `None`。契约明确「可选结构化扩展，不做万能容器，
  不承载凭据/策略/路由信息」（inbound_interaction.py:50-51）。
- **事实面**：当前仅 WeCom 智能机器人路径写入
  `metadata={RESPONSE_URL_METADATA_KEY: response_url}`（wecom.py:389）；XML 自建应用
  路径 `metadata` 保持默认 `None`（wecom.py:333-339 不传）。

→ **类型安全**：写入 `request_payload["metadata"] = inbound.metadata` 合法，值
  为 `dict` 或 `None`，JSON 列可承载（含 `None`）。

---

## 2. `request_payload` 的完整写入路径

```
PersistingCoordinator._start(inbound)              # interaction_recording.py:152
  └─ with uow_factory() as uow:
       InteractionRecord(request_payload={
           "channel_id":               inbound.channel_id,
           "external_interaction_id":  inbound.external_interaction_id,
           "external_conversation_id": inbound.external_conversation_id,
           "external_identity_id":     inbound.external_identity_id,
           "content":                  inbound.content,
           # ← 缺 "metadata": inbound.metadata   (12.9.3.5 缺口)
       })
       uow.interactions.create(record)              # Port 调用
         └─ SqlAlchemyInteractionRecordRepository.create
              └─ to_orm(record) → InteractionModel.request_payload   # mapper 透传
                   └─ InteractionModel.request_payload : JSON(none_as_null=True)  # 模型列
       uow.activities.append(ACTIVITY_RECEIVED)
       uow.commit()
```

- **当前构造字典 = 5 个键，无 metadata**（interaction_recording.py:166-172 实测）。
- 这是**唯一的 request_payload 写入点**（全仓库 `request_payload={` 仅此一处）。
- 存储层完全透传（`mappers/interaction_record.py:17,34`），不做结构改造。

→ 修复 = 在 interaction_recording.py:166-172 的字典字面量中加一行
  `"metadata": inbound.metadata`（零结构改动）。

---

## 3. `Interaction → InboundInteraction` 的完整 reconstruction 路径

**结论：当前不存在该重建路径。这是本缺口修复的隐藏前置项。**

- `InboundInteraction(` 全仓库**仅两处**出现，且都在 `wecom.py` 的**入站解析**
  （wecom.py:333 `_inbound_from_xml`、wecom.py:383 `_inbound_from_robot_json`）。
- 在当前**同步**架构中，`inbound` 由 adapter 构造后**直接在内存中**流经
  `ChannelInboundService → PersistingCoordinator.execute(inbound) →
  RuntimeCoordinator.execute(inbound)`。`request_payload` 只是 **Evidence 快照**，
  **从未被读回用于执行**。
- 因此：**没有任何代码**从 `InteractionRecord.request_payload` 还原
  `InboundInteraction`。对于 12.9.3.3/12.9.3.4 的 async Worker 模型，Worker 必须新增
  一段重建代码（例如 `InboundInteraction(**stored_request_payload)`），目前**尚不存在**。

**重建可行性（已验证）**：
- `InboundInteraction` 字段顺序 = `channel_id, external_interaction_id,
  external_conversation_id, external_identity_id, content, metadata`
  （inbound_interaction.py:61-66）。
- 加上 metadata 后，`request_payload` 的键集**恰好等于** dataclass 全部字段 →
  `InboundInteraction(**request_payload)` 可直接、无歧义重建。
- 旧行（无 metadata 键）重建时 `metadata` 走 dataclass 默认值 `None`，不报错。

→ **修复 metadata 缺口本身只是 _start 加一行；但要让它「有意义」，必须配套新增
  重建函数（新代码，非改动）。重建函数应放在 repository/新 port 方法（如
  `reconstruct_inbound(interaction_id)`），并随 12.9.3.4 的 Worker pickup 一起落地。**

---

## 4. `metadata=None` 旧数据兼容性

| 维度 | 结论 |
| --- | --- |
| **Domain 层** | `metadata` 默认 `None`，`InboundInteraction(**payload)` 对缺键旧行自动取 `None` → 兼容 |
| **DB 旧行** | 既有 `request_payload` 无 metadata 键 → 重建得 `metadata=None` → 行为等同自建应用 XML 路径（deliver 走 `message/send`） |
| **机器人旧行** | 同步时代机器人消息**从未异步重建**，故「旧机器人行丢 response_url」在现状下不可观察；前向看，未来 Worker 重建旧机器人行会得到 `metadata=None` → 静默回退 message/send（与 10.2.1 群聊裁决一致，非错误）。修复后**新**机器人行才会持久化 response_url |
| **序列化** | 模型列 `JSON(none_as_null=True)`（interaction.py:58）——`"metadata": None` 是合法 JSON，写入/读出无碍 |

→ **纯增量 + 默认值向后兼容，零迁移、零数据风险。**

---

## 5. 是否存在其他 Channel Adapter 使用 metadata

- **全仓库 `adapters/channels/` 中，仅 `wecom.py` 触碰 `metadata`**（grep 命中 9 处，
  全部在 wecom.py；其它 channel adapter 不存在）。
- WeCom 的用法唯一：`metadata["response_url"]` 承载智能机器人回执地址
  （wecom.py:389 写入、wecom.py:234-248 `_response_url_from` 读取）。
- 其它渠道（telegram 等）入站不产 metadata → 修复后其 `request_payload["metadata"]`
  为 `None` → 重建无害。

→ **修复是 channel-agnostic 的；仅 WeCom 机器人路径会真正受益，其余渠道静默无感。**

---

## 6. 当前相关测试

| 测试 | 触及点 | 对「加 metadata 键」的敏感度 |
| --- | --- | --- |
| `test_step10_4_2_interaction_persistence.py:233` | `row.request_payload["content"] == "hello"`（仅查单键） | **安全**（子集断言，不校验完整字典） |
| `test_step10_5_2_interaction_identity_materialization.py:196` | `row.request_payload == {5 键精确相等}` | **会断**（精确字典相等，多出的 metadata 键破坏） |
| `test_step10_7_1_runtime_observability_evidence_audit.py:173` | `row.request_payload == {5 键精确相等}` | **会断**（同上） |
| `test_step10_5_3_duplicate_interaction_decision_semantics.py:182` | 直接 `uow.interactions.create(InteractionRecord(request_payload={2 键}))`——不走 `_start` | **安全**（不断言 `_start` 输出形状） |
| `test_models.py:325` | 直接 `InteractionModel(request_payload={"text":...})`——不走 `_start` | **安全** |
| `test_step12_8_2_wecom_response_url.py` | 测试 metadata **内存传播**（inbound→outbound），不持久化、不断言 `request_payload` | **安全**（但**缺**「持久化往返」用例，需补） |
| `test_step12_6_runtime_production_composition.py` | 仅引用 `PersistingCoordinator` 装配，无 `request_payload` 精确断言 | **安全** |

---

## 7. 加入 `"metadata": inbound.metadata` 预计影响

- **必改（精确相等断言被破坏）**：
  1. `test_step10_5_2_interaction_identity_materialization.py:196` — 期望字典需补
     `"metadata": None`（XML 路径）或 `"metadata": {...}`（机器人路径）。
  2. `test_step10_7_1_runtime_observability_evidence_audit.py:173` — 同上。
  → 两处断言需同步扩成 6 键（或放宽为「包含 5 原键 + metadata 键存在」的子集断言）。
- **不受影响**：`test_step10_4_2`、`test_step10_5_3`、`test_models.py`、
  `test_step12_8_2`、`test_step12_6`（见 §6）。
- **需新增**（让修复可验证）：
  - 一个 **持久化往返** 测试：机器人 inbound → `_start` 落库 → 读回
    `request_payload` → 断言 `metadata` 含 `response_url`；
  - 一个 **重建** 测试：经新增的 `reconstruct_inbound` 从存储还原
    `InboundInteraction` → 断言 `metadata["response_url"]` 完整复现，且 deliver 走
    POST（非 message/send）（呼应 12.9.3.5 的静默错投风险）。

→ **影响面 = 2 个既有断言需小幅更新 + 2 个新测试需补。整体低风险。**

---

## 8. 能否做到 zero schema change / zero Runtime contract change

**request_payload 加 metadata 键这一行本身：**

- **✅ Zero schema change**：`request_payload` 是 `JSON(none_as_null=True)` 列
  （interaction.py:58），加键是纯数据变更，无迁移、无列增、无模型改动。
  （对比 12.9.3.2 的 `processing_started_at` 需真加列——那是另一回事，本缺口不涉及。）
- **✅ Zero Runtime contract change**：`RuntimeCoordinator.execute(inbound)` 签名、
  返回、`RuntimeExecutionResult` 全部不变；`_start` 仅在它本就在构造的字典里多放一个
  键。Agent / Channel Adapter / Delivery 契约零改动。

**但需明确边界（避免误读「零改动」）：**

- 让 metadata「真正被 Worker 消费」**必须新增** 重建函数（§3）——那是**新增代码**，
  不是改动现有契约；且属于 12.9.3.3/12.9.3.4 的 async 运行模型范围，与「加键」是
  两个独立 PR。
- 12.9.3.4 的 `find_pending` / CAS claim 同样是**新增**，不属本缺口。
- 因此：**「加键修复」独立即可做到 zero schema + zero Runtime 契约变更**；而「让修复
  在 async Worker 下生效」需要一批 additive 新代码（重建 + pickup + claim），这些
  是 12.9.3.3/12.9.3.4 的范畴，且均为新增而非契约破坏。

---

## 9. 实现前结论（preflight verdict）

| 问题 | 结论 |
| --- | --- |
| metadata 类型/位置 | `dict \| None`，`domain/inbound_interaction.py:66` |
| 写入路径 | 唯一：`interaction_recording.py:166-172`（`_start`），缺 metadata 键 |
| 重建路径 | **不存在**；需随 Worker 新增 `InboundInteraction(**request_payload)` 重建函数 |
| 旧数据兼容 | 默认 `None` + 缺键兼容，零迁移风险 |
| 其他 adapter | 仅 wecom 用 metadata，其余无感 |
| 相关测试 | 见 §6；2 个精确相等断言会断，其余安全 |
| 影响 | 2 处断言小改 + 2 新测试；低风险 |
| zero schema / zero 契约 | 加键本身 ✅；让修复生效需一批 additive 新代码（属 12.9.3.3/4，非契约变更） |

**建议落地顺序（仍待真实需求触发，遵循 12.8.6）：**
1. `interaction_recording.py:166-172` 加 `"metadata": inbound.metadata`（1 行）；
2. 更新 `test_step10_5_2:196` 与 `test_step10_7_1:173` 的精确字典断言；
3. 补持久化往返 + 重建往返测试；
4. 重建函数与 `find_pending`/CAS claim 一并归入 12.9.3.4 的 Worker PR（不在此 PR）。
