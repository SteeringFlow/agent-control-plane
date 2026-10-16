# Step 12.8.5 — ACP Wire Content Classification

> 目标只有一个：**确定 Tencent ACP wire 上 `reasoning` 到底以什么结构存在**，并据此把
> 流式 SSE 的 content 块分类为 REASONING（丢弃）或 CONTENT（归一为
> `AgentResponse.content`）。
> 铁律：**只按结构信号分类，绝不按自然语言启发式过滤。**

## 1. 问题重述

`ACPAdapter` 收 SSE `session/update` → `agent_message_chunk` → `message.content[]`。
`message.content` 是一组 content 块。我们要决定：

- 哪些块是 **reasoning**（思维链 / 内部思考），应丢弃，不进 `content`；
- 哪些块是 **content**（用户可见正文），应保留、拼接。

不能做的：靠「`The user wants...`」「`Let me...`」之类的文本模式判断 reasoning——
那属于**启发式污染协议层**，明确反对。适配器无法凭空知道哪一句是 reasoning。

## 2. 三种结构可能性（及结论）

### Case A — 独立 `type`（最干净）

```json
{ "type": "reasoning", "text": "The user wants..." }
```

- **结构可识别**：`type == "reasoning"`（及 `reasoning_content` / `thinking` / `thought` 等同义命名）。
- **结论**：`reasoning → discard`，`text → content` 不收。干净，无歧义。

### Case B — 不同字段，但仍属 text node

```json
{ "type": "text", "role": "reasoning", "text": "..." }
```

- **结构可识别**：`type` 仍是 `text`，但存在更具体的语义字段（`role` / `reasoning: true`
  / `metadata.reasoning: true`）。
- **结论**：不能写 `if type == "text": collect()`，必须判断具体语义字段后再决定。
  命中语义字段 → `reasoning → discard`。

### Case C — reasoning 与正文 fused

```json
{ "type": "text", "text": "The user wants...\nLet me create this...\n## 示例" }
```

- **结构上无法区分**：wire 只有单个 `type:text` 块，没有 reasoning 标志。
- **结论**：适配器**不能**知道哪句是 reasoning，**必须整体作为 CONTENT 透传**。
  宁可泄漏 reasoning，也绝不误删用户可见正文。任何「按自然语言文字模式过滤」都属于
  启发式污染，明确不做。

## 3. 实现决策

分类器**并入 `app/adapters/agents/acp.py`**（保持 10.1.1 冻结的架构不变量：
`adapters/agents` 唯一非空文件 = `acp.py`；分类是 ACP wire 专有逻辑，不外溢新文件）：

- `classify_content_block(block) -> CONTENT | REASONING`：只按结构信号分类
  （Case A：`type` 命中 reasoning 类型集合；Case B：`type==text` + `role`/`reasoning`
  语义标志）。识别不到信号 → `CONTENT`（Case C 兜底）。
- `extract_content_texts(node)`：递归遍历 SSE 事件 JSON，仅收集 CONTENT 块
  （REASONING 丢弃）——**生产路径使用**。
- `extract_all_texts(node) -> (contents, reasonings)`：两路分离，供测试 / 可观测性。

`acp.py` 的 `_json_collect_text` 改为委托 `extract_content_texts`，签名不变
（10.1.3 既有测试全绿）。Port / Domain / Dispatch / Coordinator 一律不动。

**识别不到 reasoning 信号时 = Case C = 整块透传**，这是安全兜底，也是该模块存在的
唯一意义：它在「能干净区分时丢弃 reasoning」与「不能区分时绝不猜」之间划清边界。

## 4. 验收

测试 `tests/test_step12_8_5_acp_wire_content_classification.py`（9 项，全绿）：

| 用例 | 覆盖 |
| --- | --- |
| `test_classify_case_a_independent_type` | Case A 结构识别 → REASONING |
| `test_classify_case_b_text_with_semantic_field` | Case B 语义字段识别 → REASONING |
| `test_classify_case_c_fused_passthrough` | Case C 无信号 → CONTENT |
| `test_extract_drops_reasoning_keeps_content` | 混合块：丢弃 reasoning、保留 content |
| `test_extract_all_texts_splits` | content / reasoning 两路分离 |
| `test_extract_case_c_fused_not_dropped` | Case C 文本（含 `Let me`）不被删除 |
| `test_e2e_adapter_drops_reasoning_case_a` | 端到端：wire 混推，content 只含正文 |
| `test_e2e_adapter_drops_reasoning_case_b` | 端到端：Case B 同样丢弃 |
| `test_e2e_case_c_fused_passthrough` | 端到端：Case C 完整透传 |

## 5. 真实 wire 校准结论（12.8.6 已完成）

**Tencent ACP = Case B（信号在父级 update 信封）**：reasoning 与正文都是裸
`{"type":"text","text":...}` 块（块级零信号），区分靠父级
`update.sessionUpdate`——`"agent_thought_chunk"` → REASONING、
`"agent_message_chunk"` → CONTENT（真实捕获 96/129 块双向验证）。真实分型常量与
信封感知 walker 已补入 `acp.py`，端到端校准断言在真实 wire 上通过。详见
`docs/step-12.8.6-real-acp-wire-classification-smoke.md`。
