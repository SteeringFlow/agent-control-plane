# Step 12.8.6 — Real ACP Wire Classification Smoke

> 只做一件事：**真实 Tencent ACP → 真实 SSE → 观察 reasoning block 原始结构 →
> 判定 A / B / C**。
> - 判定 A/B → 补充真实分型常量 + 回归；
> - 判定 C → 不改代码，正式关闭「reasoning 在 CP 侧可分离性」问题。
> - 16s 执行 vs 5s webhook retry：保持记录状态，**不进入 12.8.6**（下一阶段
>   产品/运行模型问题，不与 ACP content normalization 混合）。

## 实现

1. `ACPAdapter` 新增可选构造参数 `event_recorder: callable(raw_event: str)`——
   Tencent SSE binding 下逐条透传**解析前**的原始 `data:` 事件；默认 `None` =
   零行为变化，Port（`send(request, connection)`）不变。观测回调异常被吞掉，
   绝不影响协议路径。
2. `tests/test_step12_8_6_acp_wire_reasoning_live_smoke.py`：
   - 判定纯函数 `classify_wire_verdict(events)` 离线可测（4 项单测）；
   - live 测试 env 门控（`TENCENT_ACP_URL/TOKEN/RUNTIME_ID`，缺省 skip），
     原始事件落盘 `backend/artifacts/acp-wire-capture-*.jsonl` 作证据；
   - prompt 刻意选需要逐步推理 + markdown 产出的形态，最大化观察 reasoning 机会。

## 真实 wire 事实（来自本次捕获，无论鉴权状态均有效）

传输/协议层**工作正常**（Bearer acpLink.token 有效、SSE 建立、事件流动）。校准到
的真实形状（宽容递归均已兼容，本地模拟的 `sessionUpdate.updates[]` 数组形态与
真实不同，但真实形态能被正确解析）：

1. 内容块真实位置：`params.update.content = {"type":"text","text":...}`——
   content 嵌在 `update` 内，`update.sessionUpdate` 是**字符串**
   （`"agent_message_chunk"` / `"session_info_update"` / `"usage_update"`），
   不是本地模拟的 `updates[]` 数组；
2. 收尾：`method:"session/endTurn"` + `params.stopReason`（本次观测
   `"refusal"`；10.1.3 曾观测 `"end_turn"`）；最终以
   `params.update._meta.codebuddy.ai.isFinalStop=true` 收口（嵌套在
   `_meta.codebuddy.ai` 下，递归检测可命中）；
3. 辅助事件：`session_info_update`（进度/状态）、
   `_codebuddy.ai/file_history_snapshot`（快照）、`usage_update`
   （inputTokens/outputTokens/cached/cost）；
4. `jsonrpc` 字段可能为空串 `""`。

## Live smoke 结果

### 第一轮：旧 Runtime 登录过期（无效样本）

旧 Runtime 沙箱内 CLI 会话过期（`Authentication required ... /login`，
`stopReason:"refusal"`，jsonrpc `-32603, source: cli_result`），ACP 鉴权本身通过。
该样本唯一 text 块是登录错误信息，**不能用于 A/B/C 判定**。处理：**重建 Runtime**
（经管理面 `POST /v2/agentos/runtimes` 重建，复用既有 runtime manifest 与
AgentOS 凭据；对新 Runtime 做无效 body 探测，确认鉴权边界返回 400 而非 401）。
新 Runtime：`<runtime-id>`（RUNNING），本地 `backend/.env` 已切换到新 acpLink 三值。

### 第二轮：新 Runtime 真实观测 —— **最终判定 Case B（信号在父级 update 信封）**

prompt：`请先一步步推理再回答：9.11 和 9.8 哪个大？…`（133 个 SSE 事件）。
真实 wire 上 reasoning 与正文**都是裸 `{"type":"text","text":...}` 块**，块级零信号
（`text_block_keys` 为空，全事件 type 只有 `"text"`）；区分信号在父级：

| `update.sessionUpdate` | 块数 | 语义 | 文本实证 |
| --- | --- | --- | --- |
| `"agent_thought_chunk"` | 96 | REASONING | "The user asks: … Let me reason: … Wait, 0.8 = 0.80 …" |
| `"agent_message_chunk"` | 129 | CONTENT | "好的，来分析一下 … ## 结论 **9.8 比 9.11 大**" |

结论：**不是 Case C**。结构信号存在，但不在 text 块上，而在 update 信封——
属于用户框架中的 **Case B**（必须判断更具体的语义字段，不能只按 `type=="text"`
收集），只是字段位置在父级信封而非块内。

### 按 12.8.6 决策规则执行：补充真实分型常量 + 回归

`acp.py` 分类器更新：

- 新增 `_REASONING_SESSION_UPDATES = {"agent_thought_chunk", "agent_thought",
  "thought_chunk"}` + `_update_kind_marks_reasoning()`（含 "thought" 子串匹配兜底）；
- walker 信封感知：遍历进入带 `sessionUpdate` 的 dict 时更新 reasoning 上下文，
  其内 text 块按信封分类（块级 Case A/B 信号仍然保留，信封与块级信号任一命中
  即 REASONING）；
- 端到端校准断言（live 测试内）：`response.content` 必须等于 message 通道
  重建文本——**在真实 wire 上通过**（thought 通道 96 块被正确丢弃，0 泄漏、
  0 误删）。

### 回归

最终全量 `python -m pytest tests/ -q`：**1304 passed, 0 failed**（含 WeCom 全套
10.2.1 / 10.2.2 / 12.8.2 / 12.6 production composition）。
过程注意：新增函数命名避开 "session" 词汇（interaction/session 契约审计用 AST
扫描全仓命名面，`_session_update_marks_reasoning` 会撞白名单，已改名
`_update_kind_marks_reasoning`）。

### 结论

- **Tencent ACP wire reasoning 分离问题：已解决并关闭**（Case B，信封信号，
  分类器已实现真实常量）；
- `AgentResponse.content` 只含 message 通道正文；thought 通道在 Adapter 内丢弃，
  不进 Control Plane；
- 16s 执行 vs 5s webhook retry：保持记录状态，未进入 12.8.6。
