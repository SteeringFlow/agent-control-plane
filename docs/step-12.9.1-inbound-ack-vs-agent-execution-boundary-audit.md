# Step 12.9.1 — Inbound ACK vs Agent Execution Boundary Audit

> 纯审计（A–E）。**不写代码、不新增状态机、不引入 queue/worker**——只回答
> 「Inbound HTTP 的 ACK 与 Agent 执行之间，今天有没有真正的边界」。
> 结论前置：**没有边界。ACK == 执行结束，二者在时间上完全耦合。** 唯一的真异步
> 边界是「ACK 必须在 Agent 执行完成前返回」，而它今天不存在（这正是 12.8.6 里
> 用户标注为「下一阶段产品/运行模型问题、不要与 ACP content normalization 混合」
> 的那件事——此处只是把它作为审计结论显式摆出，不进入实现）。

引用事实面（行号来自本轮读取）：`app/api/runtime/channels.py`、
`app/application/channel_inbound.py`、`app/application/coordinator.py`、
`app/application/interaction_recording.py`、`app/domain/interaction_record.py`、
`app/adapters/channels/wecom.py`、`app/main.py`、ORM `interaction.py`、
决策 `step-10.5.3-duplicate-interaction-decision-semantics.md`。

---

## A. 当前 Inbound HTTP 生命周期

**请求进入**（`channels.py:70` `wecom_message`）：WeCom `POST /api/channels/{id}/wecom/webhook`，
4 个 query 参数 `msg_signature/timestamp/nonce` + body。Router 读 body →
`service.handle_message(...)` → 无论成功/失败统一 `return Response(status_code=200)`
（仅当入站协议异常被 `_map_protocol_errors` 拦截时才返回非 200）。

**Runtime 调用**（`channel_inbound.py:94` `handle_message`）：
`load_channel` → `WeComAdapter.parse_message`（归一化出 `InboundInteraction`，
或返回 `None` = acknowledge-ignore）→ `runtime.execute(inbound)`。
`runtime` = `PersistingCoordinator`（持久化观察装饰器）→ `inner` `RuntimeCoordinator`
（`coordinator.py:85` 六段同步编排：Identity→Routing→SessionResolution→[SessionCreation]
→AgentDispatch→Delivery）。
**关键：整条链路在 HTTP 请求线程内同步执行**，无 queue、无 background task、无
`accepted` 中途返回。

**Delivery**（`coordinator.py:149` `delivery.execute` → `WeComAdapter.deliver`
`wecom.py:525`）：智能机器人走 `response_url` POST，自建应用走 `message/send`
（独立取 access_token）。仍在同一个 HTTP 请求线程内同步完成。

**HTTP response**（`channels.py:88`）：`200` 空体。注意：**200 = 已接收并已完成
（或已 acknowledge-ignore），不是「Agent 成功」的中间态**；本链路没有任何
`202/accepted` 形态。

**超时行为**：
- 代码侧**没有** server 超时配置（`main.py` 仅 `create_app()`，无 timeout/keepalive
  设置）；handler 是同步 `def` → FastAPI 在线程池里跑，长执行会**占用该线程**；
- 唯一的外部超时是 **WeCom 自身 ~5s 重推窗口**（10.2.1/10.5.3 记录）；
- 若 Agent 执行 >5s（真实 wire 上可达 ~16s，10.1.3/10.2.1 记录），首推在 WeCom 视角
  下已超时 → WeCom 重推，而此时首推仍在自己线程里跑——两条线程并发，靠 UNIQUE 保证
  不产生双行/双执行。

---

## B. 当前 HTTP status 语义

唯一可达状态：`200 / 400 / 403 / 500`（无 404、无 202、无 429）。映射点唯一：
`channels.py:41` `_map_protocol_errors`。

| 状态 | 触发条件 | 语义 | Runtime 是否触发 |
| --- | --- | --- | --- |
| **200** | 无入站协议异常，`handle_message` 返回 | ACK：已接收并已（同步）处理完 / acknowledge-ignore | 总是返回（无论 Agent 成功失败，除非抛异常） |
| **403** | `InboundSignatureError`（坏签名） | 不可信请求 | 不触发 |
| **400** | `InboundFormatError`（畸形体/缺字段） | 可信渠道但 payload 非法 | 不触发 |
| **500** | `InboundChannelConfigError`（未知/未启用渠道、adapter 缺验签材料） | 服务端配置错误 | 不触发 |
| **500** | **任何 Runtime typed error**（`Identity/Routing/Session/Dispatch/Delivery` 的 `ApplicationError`）原样冒泡，FastAPI 兜底 | 服务端处理失败 / 需上游重试 | **触发** |
| **500** | **`IntegrityError`**（重复 MsgId 命中 UNIQUE，见 D） | 重复推送，未做 suppress | 不触发（在 `_start` 的 create 一步炸，未到 execute） |

唯一要注意的隐含语义：**500 既是「配置错误」也是「Runtime 执行失败」也是
「重复推送」**——三类不同成因共用一个状态码；WeCom 侧无法区分，一律按「需重试」处理。

---

## C. Interaction 状态语义

冻结双状态机（`interaction_record.py:7-13`，`interaction.py` ORM）：

| 字段 | 取值 | 写入点（`interaction_recording.py`） |
| --- | --- | --- |
| `processing_status` | `PENDING→PROCESSING→SUCCEEDED\|FAILED` | `_start` 写 PENDING+mark_processing；成功 `_record_success` 写 SUCCEEDED；非 delivery 段失败写 FAILED |
| `delivery_status` | `PENDING→DELIVERED\|FAILED` | 成功写 DELIVERED；delivery 段失败写 FAILED；其他段失败保持 PENDING |
| `failure_stage` | `identity_resolution / routing / session_resolution / agent_dispatch / delivery`（成功为 `None`） | `STAGE_FAILURE_MAP`（`interaction_recording.py:98`） |
| `retryable` | `bool` | `STAGE_FAILURE_MAP`：transport/timeout 类（`AGENT_DISPATCH_FAILED`/`CHANNEL_DELIVERY_FAILED`）= `True`；契约/配置/身份类 = `False`；实例级 `exc.retryable` 覆盖（如 response_url 一次性 → `False`，`interaction_recording.py:240`） |

`STAGE_FAILURE_MAP` 完整映射（`interaction_recording.py:98-108`）：
- `IdentityResolutionFailedError` → identity_resolution / `IDENTITY_RESOLUTION_FAILED` / **retryable=False**
- `RoutingFailedError` → routing / `ROUTING_FAILED` / **False**
- `SessionResolutionFailedError` → session_resolution / `SESSION_RESOLUTION_FAILED` / **False**
- `AgentDispatchFailedError` → agent_dispatch / `AGENT_DISPATCH_FAILED` / **True**
- `AgentResponseInvalidError` → agent_dispatch / `AGENT_RESPONSE_INVALID` / **False**
- `ChannelDeliveryFailedError` → delivery / `CHANNEL_DELIVERY_FAILED` / **True**

`processing=FAILED` 与 `delivery=FAILED` **可独立组合**（4.2/5.3 冻结）：
`SUCCEEDED(processing) + FAILED(delivery)` 是合法态（Case B），且此刻
`response_payload` 仍为 NULL（10.4.2/10.8.4 诚实修订——Delivery Retry 无承载物）。

---

## D. WeCom 重试实际行为

事实链（10.5.3 §1 已确立，本轮代码复核一致）：

```
同 Channel + 同 MsgId 的第二次 create
   ↓ PersistingCoordinator._start（interaction_recording.py:152）
   ↓ uow.interactions.create(...)   ← INSERT 命中 UNIQUE 冲突
   ↓ IntegrityError（在 inner Runtime 执行之前抛出，interaction.py:43-47）
```

- **第一次 webhook**：POST 进入 → `_start` 先落 PENDING 并提交（事务①）→ mark_processing
  （事务②）→ 才进入 `inner.execute`（Agent+Delivery）。若 <5s 完成且未抛异常 → 200，
  产生 **1 行** interaction（PENDING→PROCESSING→SUCCEEDED+DELIVERED）。
- **5s 后（若首推超时或返回 500）**：WeCom 用**同一 MsgId** 重推。
- **第二次**：`_start` 的 `create` 命中 `UNIQUE(channel_id, external_interaction_id)`
  → `IntegrityError`，**在 `self._inner.execute` 之前抛出** → 绝不产生第二次副作用，
  也绝不产生第二行 interaction（UNIQUE 是 DB 级硬护栏，包含首推仍在 in-flight 的
  E2 探针场景，因为 PENDING 行在事务①后就已存在）。
- **UNIQUE 的性质**：它是「身份物化约束」，**不是幂等/suppress 语义**（10.5.3 §1）。
  命中后 CP 没有决策层——`IntegrityError` 未被任何一层捕获 → 冒泡为 **HTTP 500**。
- **最终 DB**：每个 MsgId 恰好 **1 行**（首推那次执行的真实终态）。重复推送 0 新增行，
  但每次都被 500 打回 → WeCom 持续重推（直到其自身放弃）。

两个未决（10.5.3 显式留白）：
1. **响应层缺失**：重复请求以 500 暴露，WeCom 无法感知「已处理」→ 持续重推；真正
   suppress 需要把 UNIQUE 命中转成对上游的「已处理」ACK（10.5.3 §1/L6）。
2. **in-flight / retry 决策缺失**：`DUPLICATE_TERMINAL`/`DUPLICATE_IN_FLIGHT`/
   `RETRYABLE_FAILURE`/`DELIVERY_FAILURE` 是内部裁决 token，**不落库**（10.5.3 §3），
   今天没有读取「已存在行状态」并据此 suppress-vs-retry 的逻辑。

---

## E. 当前架构结论

### 哪些生命周期已经解耦
- **入站协议完全外置**：SHA1/AES/XML 全在 `WeComAdapter`；HTTP 事实提取在 `channels.py`；
  编排在 `RuntimeCoordinator`；持久化是装饰器 `PersistingCoordinator`——六段 Executor
  零持久化依赖（9.A/10.3.1/10.4.1 守卫成立）。
- **processing 与 delivery 彻底双状态**（`interaction_record.py`），Delivery 失败
  ≠ Processing 失败（4.2/5.3 冻结）。
- **ACK 语义与 Agent 成功语义解耦（概念上）**：200 = 已接收并处置，不承诺 Agent 成功。
- **InboundInteraction 与持久化 ORM 解耦**（8.1）：归一化值对象不落库，身份在 Channel
  Scope 内解释（10.5.2）。

### 哪些仍然绑定（核心发现）
- **HTTP 请求生命周期 与 完整 Agent 执行+Delivery 强绑定**：整条 Runtime 同步跑在
  单个 webhook 请求线程内，**ACK（200）是在 Agent 执行 + Delivery 完成后才返回的**
  （`channels.py:88` 在 `handle_message` 返回之后）。**Inbound ACK 与 Agent Execution
  之间今天没有时间边界**——ACK 就是执行结束。
- **无 async 边界的任何基础设施**：无 queue、无 worker、无 lease、无 `accepted` 状态、
  无「先 ACK 后执行」的早返回。

### 是否需要新增状态
- **DB 状态机不需要新增**：`processing × delivery × retryable` 三值已足够表达所有可达
  组合；10.5.3 已裁决「duplicate」由 `UNIQUE` 键表达，**不新增 duplicate 列/duplicate
  行/duplicate 状态**。
- **缺的是响应层语义**：把 UNIQUE 命中 / 已知终态行 映射为对 WeCom 的「已处理」ACK
  （200 而非 500），让上游停止重推。这是 mapping 层改动，不是新状态。

### 是否需要新增持久化事实
- 身份物化已完备，**无需新 schema**；
- 真正缺的持久化事实是「**已存在行的状态读取能力**」以支撑 suppress-vs-retry 决策
  （10.5.3 §4 探针已定义，但读取+决策逻辑未编码）；
- Case B（`SUCCEEDED × FAILED`）的 `response_payload` 仍为 NULL（10.8.4 诚实修订）
  → **Delivery Retry 今天没有承载物**（10.5.3 §情况B 现实）。

### 是否存在真正需要异步化的边界
**有，且唯一**：**Inbound HTTP ACK 必须在 Agent 执行完成前返回**（把 ACK 与执行解耦）。
这是整个入站链路唯一的真异步边界，今天不存在。WeCom ~5s 重推窗口 × Agent 执行
可达 ~16s（10.1.3/10.2.1 实证）= 真实而非理论化的缺口：首推超时 → WeCom 重推 →
UNIQUE 拦下双执行但回 500 → WeCom 持续重推。

**本审计不做实现**，与 12.8.6「记录、不混入 content normalization」纪律一致。
建议的演进形态（留待产品/运行模型裁决，不在本步）：把 `_start`（PENDING 已落库、已
拿到 `interaction_id`）之后的全部执行迁入后台（queue/worker 或至少线程池早返回 200），
配合 UNIQUE 防双 enqueue，并把 UNIQUE 命中 / 已知终态映射为对 WeCom 的 200「已处理」
ACK。
