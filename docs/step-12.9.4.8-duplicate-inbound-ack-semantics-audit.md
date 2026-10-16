# Step 12.9.4.8 — Duplicate Inbound ACK Semantics Audit

> **只读审计，不改任何代码**（含不顺手修）。
> 背景：12.9.4.6 已真实切到 ACK-first，WeCom 重推从「同步时代代价有限」升级为
> **production correctness issue**。本步只回答：当前实际发生了什么、修复该放哪层、
> 是否需要 schema change。

---

## 0. 结论摘要（先给答案）

| 问题 | 答案 |
| --- | --- |
| 当前是否「状态感知的 duplicate ACK」 | ❌ **否**。无任何 lookup、无任何状态检查 |
| 控制流 | `create → flush → UNIQUE 冲突 → IntegrityError → 未捕获 → HTTP 500` |
| IntegrityError 在 HTTP 边界是否被转换 | ❌ **否**。HTTP 只映射 3 个协议异常 |
| 五种状态行为是否不同 | ❌ **完全相同**（都是 500）——因为压根不查状态 |
| 是否产生第二个 Interaction | ❌ 否（UNIQUE 拦住，行数恒为 1） |
| 是否重新执行 Agent | ❌ 否（异常在 `add_task` 之前抛出） |
| 是否重复消费 response_url | ❌ 今天不会 —— **但这是"朴素修复"的头号风险** |
| 是否 HTTP 500 | ✅ **是**（唯一实际缺陷） |
| 最小修复层 | **`ChannelInboundService`**（入站用例层） |
| 是否需要 schema change | ❌ **否**；且所需读方法**已存在且是死代码** |

**性质判定：当前行为是「安全但错」——Fail Closed 没有双执行/双投递，但对渠道返回 500，
触发重推风暴，且对该 MsgId 形成永久阴影。**

---

## 1. Q1：Duplicate `external_interaction_id` 当前的控制流

逐步（全部为当前真实代码）：

```
①  HTTP  POST /api/channels/{id}/wecom/webhook            channels.py:101
②  service.handle_message(...)                            channels.py:103
③  inbound = adapter.parse_message(...)                   channel_inbound.py:134
      external_interaction_id = MsgId                     wecom.py:335 / 385
④  return self._intake.create_pending(inbound)            channel_inbound.py:140
⑤  create_pending：                                        interaction_recording.py:185
      uow.interactions.create(InteractionRecord(...))      interaction_recording.py:186
⑥  Repository.create：                                     interaction_record.py:28
      session.add(row);  session.flush()   ← INSERT 立刻发出
⑦  UNIQUE(channel_id, external_interaction_id) 冲突        models/interaction.py:43-47
      → sqlalchemy.exc.IntegrityError  ← 在 flush 处抛出
```

后果链：

- `activities.append(...)`（`:195`）与 `uow.commit()`（`:198`）**均未执行**；
- UoW `__exit__` 见到异常 → `rollback()` + `close()`（`unit_of_work.py:80-83`）
  → **无半截写入、无孤儿 Activity**（这点是对的）；
- IntegrityError 向上传播，穿过 `handle_message`，到达 HTTP 端点。

**关键：没有任何一步做 lookup。** `ChannelInboundService` 只 `create_pending`；
`PersistingCoordinator.create_pending` 只 `create`。

---

## 2. Q2：IntegrityError 在 HTTP 边界有没有被转换

**没有。**

```python
# channels.py:108-109
except (InboundChannelConfigError, InboundSignatureError, InboundFormatError) as exc:
    raise _map_protocol_errors(exc) from exc
```

只映射 3 个**入站协议**异常。`IntegrityError` 不在其中 → 未捕获 → FastAPI
→ **HTTP 500**。

并且在 500 路径下：

```python
# channels.py:110-112
if interaction_id is not None:
    background_tasks.add_task(worker.process, interaction_id)
```

`handle_message` 抛异常 → 这两行**根本没执行** → 重复入站**不会**产生 Worker 任务。
（这是"不会重新执行 Agent"的直接原因。）

补充事实：`IntegrityError` 在 `app/` 全树**零命中**（仅测试 import 它）——
生产代码没有任何一处处理 DB 冲突。

---

## 3. Q3：五种状态分别发生什么

### 3.1 当前（未修复）：五种状态**行为完全相同**

因为 UNIQUE 约束是 `(channel_id, external_interaction_id)`，**不含任何状态列**，
且流程不查状态 → 已存在行的 `processing_status` / `delivery_status` / `retryable`
**完全不参与判定**：

| 既有行状态 | 当前实际结果 |
| --- | --- |
| `PENDING × PENDING` | IntegrityError → **500** |
| `PROCESSING × PENDING` | IntegrityError → **500** |
| `SUCCEEDED × DELIVERED` | IntegrityError → **500** |
| `FAILED × PENDING` / `retryable=False` | IntegrityError → **500** |
| `FAILED × PENDING` / `retryable=True` | IntegrityError → **500** |

五者**不可区分** —— 这正是"非状态感知"的定义。

### 3.2 应有的语义（修复目标）

| 既有行状态 | 应有 ACK 语义 | 是否重新执行 |
| --- | --- | --- |
| `PENDING × PENDING` | 返回既有 id + 200（已排队，勿重复入队） | 否（且再入队也由 CAS 兜住） |
| `PROCESSING × PENDING` | 返回既有 id + 200（in-flight） | **否**（再入队时 claim=False） |
| `SUCCEEDED × DELIVERED` | 返回既有 id + 200（终态成功） | **否——绝对不可**（见 §4） |
| `FAILED` / `retryable=False` | 返回既有 id + 200（重推无意义） | 否 |
| `FAILED` / `retryable=True` | 返回既有 id + 200；**是否"重推=重试机会"是未决产品语义** | 默认否（见 §6） |

---

## 4. Q4：重复入站是否导致四类问题

| 风险 | 当前是否发生 | 说明 |
| --- | --- | --- |
| **第二个 Interaction** | ❌ 否 | UNIQUE 拦截，`_interaction_rows` 恒为 1（10.5.3 D/E 组测试已锁定） |
| **重新执行 Agent** | ❌ 否 | 异常在 `add_task` 之前；10.5.3 `test_d4` 已锁"Agent 绝不重跑" |
| **重复消费 response_url** | ❌ 今天不会 | ⚠️ **但这是修复的头号陷阱** |
| **HTTP 500** | ✅ **是** | 唯一真实缺陷 |

### 4.1 为什么 response_url 是修复的头号陷阱

```
wecom.py:585 —— 官方明确「每个 response_url 只能调用一次」→ 本路径失败 retryable=False
```

`SUCCEEDED × DELIVERED` 的行意味着 `response_url` **已被消费过一次**。
任何"为了去重而允许重新入队/重新执行"的天真修复，都会：

1. 让 Worker 再次 `Runtime.execute`；
2. 再次 POST 一个**已失效**的 `response_url` → `ChannelDeliveryFailedError`；
3. 把一条已成功投递的交互**污染成** `SUCCEEDED × FAILED`。

→ **修复必须显式禁止对终态成功行重新执行**，去重与重试是两个**不同**语义，
不能由一个"允许重跑"的开关同时表达。

### 4.2 500 的放大效应（为何比同步时代更严重）

- WeCom 对非 2xx 会重推。首条消息 ACK-first 下本应 200ms 内返回 200（不触发重推）；
  但一旦**任何一次**返回 500，该 MsgId 的后续每次重推**都会** 500 →
  重推不收敛，形成对该 MsgId 的**永久阴影**。
- 10.5.3 `test_e3` 已记录："卡死的 PROCESSING 行是该 MsgId 的永久阴影"。
- **与 12.9.4.7 §5 耦合**：PROCESSING 孤儿 + 非状态感知 ACK =
  该 MsgId 永久 500。状态感知 ACK 至少能让它回到 200（并成为未来 reclaim 的挂钩点）。

---

## 5. Q5：最小修复究竟放哪一层

### 5.1 四层逐层排除（带证据）

| 层 | 可否 | 证据 |
| --- | --- | --- |
| **HTTP adapter** (`channels.py`) | ❌ | 12.9.4.6 已把 HTTP 削薄为"协议异常→状态码"；修在此处要让 HTTP 认识 interaction 状态机，且异常时连 id 都拿不到 |
| **Repository** (`persistence/repositories/interaction_record.py`) | ❌ | 10.5.3 冻结：**"写入边界无 lookup→decide、无 IntegrityError 处理、无 suppress/retry 入口"**（`test_f2` docstring，:509-511） |
| **Persistence Phase API** (`PersistingCoordinator.create_pending`) | ❌ **被两条冻结断言直接禁止** | `test_step10_5_3.py:526-527`：<br>`assert "IntegrityError" not in recording`<br>`assert "interactions.find_by_channel_and_external_id" not in recording`<br>（`recording` = `application/interaction_recording.py`） |
| **`ChannelInboundService`** (`application/channel_inbound.py`) | ✅ | 见下 |

### 5.2 为什么是 `ChannelInboundService`

它**恰好**满足全部约束：

1. **它已经是"是否接纳这条入站"的决策者** —— 12.9.4.6 后它拥有完整的 ACK 边界
   （`handle_message` → `create_pending` → 返回 id → 200）。
2. **它已持有 `uow_factory` 并已在做 Repository 读** —— `_load_channel` 走
   `uow.channels.get_by_id`（`channel_inbound.py:85-86`），再加一次
   `uow.interactions.find_by_channel_and_external_id(...)` 属同一纪律。
3. **不触碰 10.5.3 的两条冻结断言** —— 那两条只约束
   `interaction_recording.py`（持久化写入边界），**不约束** `channel_inbound.py`。
   决策留在用例层，**持久化层保持"哑" create / claim / record_outcome**，
   正是 10.5.3 "不把 conflict 翻译成决策" 的原本意图。
4. **不破坏 6 条既有行为测试** —— `test_d1/d2/d3/d4/e2/e3` 断言
   `pytest.raises(IntegrityError)`，它们走的是**同步** `PersistingCoordinator.execute`
   路径。修复只改 `ChannelInboundService` 后，同步路径行为不变 →
   **这 6 条测试无需改动**。（若改在 `create_pending`，则这 6 条 + 2 条冻结断言
   全部要重写，代价与风险显著更大。）

### 5.3 修复形状（仅规格，不实现）

```
handle_message:
    inbound = adapter.parse_message(...)          # 已有
    if inbound is None: return None               # 已有

    # 新增：状态感知 lookup（仅当 external_interaction_id 非 None）
    existing = uow.interactions.find_by_channel_and_external_id(
        inbound.channel_id, inbound.external_interaction_id)
    if existing is not None:
        return existing.id        # ← 状态感知 ACK：不 create、不执行、200
    return intake.create_pending(inbound)
```

要点：
- **`create_pending` 保持"盲 create"** 不变（持久化层零改动、Port 面零改动）；
- **默认对全部五种状态都只回既有 id + 200**，不重新执行；
- `external_interaction_id is None` 时跳过 lookup（SQL 对 NULL 不去重，语义吻合
  `models/interaction.py:28`），直接 create；
- **DB UNIQUE 仍是并发硬保证**：lookup→create 存在 TOCTOU 窗口（并发重推时两者都
  查无 → 都 create → 一个仍会 IntegrityError）。可接受为已知残差；若要求"永不
  500"，可在**用例层**捕获冲突后回查一次（注意：这会引入 IntegrityError 处理，
  属超出最小修复的增量决策）。

---

## 6. `retryable=True` 的未决产品语义（必须显式裁决，不能由修复顺带决定）

`FAILED × PENDING / retryable=True`（如 `AGENT_DISPATCH_FAILED`）是**唯一**
值得讨论"重推是否 = 重新执行机会"的状态。

- 10.5.3 `test_d3` 已明示：**「重推 = 重新执行的机会」是未决产品语义，未编码。**
- 10.5.3 `test_d4` 已定：**任何"因为 Delivery 失败而重跑 Agent"都是语义错误**
  （Agent 业务副作用不可重复）。
- 且 re-execute 对 `response_url` 有 §4.1 的硬约束。

→ **建议默认：不重新执行，只 ACK 200。** 把"retryable → 允许重入队"留作独立裁决，
需要时再引入显式机制（且必须与"不重跑 Agent"边界一并裁决）。

---

## 7. Q6：是否需要 schema change

**不需要。零 schema change、零新 Port 方法。**

| 所需能力 | 现状 |
| --- | --- |
| 身份去重约束 | ✅ 已有 `UNIQUE(channel_id, external_interaction_id)`（`models/interaction.py:43-47`） |
| 按身份读回 | ✅ 已有 `find_by_channel_and_external_id`（Port `:75` / 实现 `:82`） |
| 状态字段 | ✅ 已有 `processing_status` / `delivery_status` / `retryable` |
| 新列 | ❌ 不需要 |

**值得注意的事实**：`InteractionRecordRepository.find_by_channel_and_external_id`
在 `app/` 下 **零生产调用者** —— 它目前只被测试使用（10.5.2 物化后一直闲置）。
修复的本质是**激活一个早已存在的 primitive**，而不是新增能力。

---

## 8. 需要的守卫测试（修复时一并补）

1. 五种状态各自：重复入站 → 返回**既有 id**、`interactions` 行数仍为 1、
   **HTTP 200**（非 500）；
2. `SUCCEEDED × DELIVERED` 重复 → `agent.calls` 仍为 1、`channel.deliveries` 仍为 1
   （**锁死 response_url 不被二次消费**）；
3. `PROCESSING × PENDING` 重复 → 不产生第二次执行（再入队时 claim=False）；
4. 并发重复（两请求同时首达）→ 行数仍为 1（UNIQUE 硬保证）；
5. `external_interaction_id=None` 路径不受影响（仍 create，不去重）；
6. 反向守卫：`create_pending` 仍不含 lookup / IntegrityError 处理
   （10.5.3 冻结断言保持成立）。

---

## 9. 与 12.9.4.7 的关系（建议的下一步顺序）

- **12.9.4.8 优先级高于 orphan reclaim** — 同意。理由补充：orphan 是"低频、
  不扩散"的故障（单条消息永久挂起）；而 duplicate→500 是"高频、会扩散"的
  （一次 500 引发重推链，并在监控/渠道侧表现为回调失败）。
- 两者**相交**于 §4.2：PROCESSING 孤儿 + 非状态感知 ACK = 该 MsgId 永久 500。
  先做状态感知 ACK 可把"永久 500"降级为"永久 PENDING + 200"，
  为后续 reclaim 创造干净的挂钩点。
- 因此建议顺序：**12.9.4.8（状态感知 ACK）→ 12.9.4.7 的 orphan reclaim**。

---

## 10. 明确不做（本步）

- 不修改任何代码（含不顺手修）；
- 不引入 queue / Redis / lease / heartbeat / scheduler / cron / daemon loop；
- 不新增 Port 方法、不加列、不加状态值；
- 不实现 retry / replay / outbox；
- 不重写 10.5.3 的冻结断言（修复方案已刻意避开它们）。
