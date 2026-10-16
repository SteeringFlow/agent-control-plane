# Step 10.6.2 — Session Resolution Identity Hardening

> **决策 / 验证步，生产代码改动：0 行。**
> 交付物 = 本报告 + 验证探针测试
> （`tests/test_step10_6_2_session_resolution_identity_hardening.py`）。
>
> 目标只有一个：让 Session Resolution / Persistence Observation 在
> **多 Connection / 多 Agent** 场景下拥有**确定的查询语义**。
>
> 核心验证（用户指定）：先不改 Runtime，只验证 Case A / Case B / Case C。

---

## 0. 范围与不做

**做**：

- 确定 Session 的真正 Identity：`(identity_id, connection_id,
  external_conversation_id)`（8.3 已冻结，本步硬化其语义）；
- 验证 **agent_id 不是 Session 的选择键**（Connection 才是 Agent 隔离键）；
- 用真实探针验证 Case A / B / C 的查询确定性；
- 记录 Persistence Observation（10.4.2 Interaction Wrapper 弱键）在多
  Connection 场景下的歧义现状与收敛方向。

**不做（本步明确排除）**：

- 不改 Runtime（Coordinator / 六 Executor / ChannelInboundService）；
- 不给 SessionRepository 加 agent_id 维度的查询（`find_active_by_agent` /
  `find_active` 加 agent 参数一律不做）；
- 不加 DB UNIQUE 约束（9.7 evidence-first：真实并发重复未出现，不提前引入；
  §6 记录触发时机）；
- 不改 Interaction Wrapper 的写路径（弱键收敛需要先裁决连接事实从哪来）。

---

## 1. 为什么现在需要硬化

10.6.1 审计确立了多 Agent 场景的语义坐标：换 Agent = 换 Connection = 新
Session。但「查询语义是否确定」没有被验证过——在单 Channel 单 Connection
（MVP）下不存在二义；一旦同一 Channel 有多条 Connection、同一 (identity,
conversation) 可能出现多条 active Session，查询就必须能**唯一确定地**回答
「这条 Conversation 在哪个 Agent 上的会话」。

本步不引入任何新机制，只把三个核心场景的确定性钉成回归事实。

---

## 2. Session 的真正 Identity（三维键）

```python
(identity_id, connection_id, external_conversation_id)
```

三个维度的语义（缺一不可，各自不可替代）：

| 维度 | 含义 | 边界来源 |
| --- | --- | --- |
| `identity_id` | channel-scoped 的外部 actor | 8.2 Identity Resolution（`Identity.id`，不是 `User.id`） |
| `connection_id` | **selected Agent boundary**：这条会话走哪条 Access Path | 8.4 Routing（Connection 在 Routing 中被选择） |
| `external_conversation_id` | channel-side context（单聊 / 群聊） | Inbound 契约原样透传 |

> 三者合起来才是当前 Session 的真正作用域（8.3 隔离键的硬化表述）。

这**不是**给 Repository「再塞一个字段」——三维键自 8.3/9.3 起就是
`find_active` 的签名（Port / Adapter / Executor 逐层一致），本步只是把它
确认为 **Identity**，并验证它足够确定。

---

## 3. agent_id 不是 Session 的第二个选择键

关键裁决：

> **`connection_id → agent_id`：Agent 已经由 Connection 派生。
> Connection 是 Session 的 Agent 隔离键。绝不让 agent_id 成为 Session 的
> 另一个选择维度。**

原因：

1. 若查询变成 `identity + connection + agent + conversation`，等于把同一事实
   （Agent = 所选 Connection 的 Agent）**重复表达**两次——两份输入可能互相
   矛盾（选了 Connection A 却传 agent B），产生新的不一致面；
2. `sessions.agent_id` 是 **派生冗余**（Session Creation 从 RoutingResult /
   Connection 写入，方便读取），不是身份的一部分——它不参与 `find_active`
   的 WHERE，也不出现在任何唯一键里；
3. 若未来真的需要「按 Agent 查会话」，那也是 **先经 Connection 表达**
   （`connection_id`），而不是给仓储加一个 agent 维度方法。

**因此 Port 面保持不变**：`find_active(identity_id, connection_id,
external_conversation_id)` —— 不加第四个参数，不加 `find_active_by_agent`。

---

## 4. 核心验证：Case A / B / C

### Case A —— 单条 Session 的确定性

```
Identity X + Conversation C + Connection A
        → Session A（唯一）

查询 X + A + C → 只能得到 Session A
```

验证：同一三维键只可能存在一条 active Session（REUSE 复用），查询稳定返回
同一条，不出现 MultipleResultsFound。

### Case B —— 同 Conversation 跨 Connection 的隔离

```
Identity X + Conversation C
    Connection A → Session A
    Connection B → Session B

查询 X + A + C → Session A
查询 X + B + C → Session B
绝不能：MultipleResultsFound
```

验证：三维键查询**按 Connection 精确隔离**——同一 (identity, conversation)
在两条 Connection 上的两条 Session 互不干扰，各自的查询都恰好返回自己的那
一条。这是「Persistence Observation」在多 Agent 场景确定性的核心。

### Case C —— 两条 Session 可同时 active

```
同一个 Conversation：
    Connection A → Session A
    Connection B → Session B
    两者可以同时 active。
```

验证：这恰好证明 **切换 Agent = 新 Session，而不是修改旧 Session**——Session A
在 Session B 建立后原样保持（connection / identity / conversation / status
不变）；不存在「一个 Conversation 同一时刻只能有一条 Session」的约束。

---

## 5. Persistence Observation：弱键是今天的唯一歧义源

10.4.2 Interaction Wrapper（Persistence Observation）用弱键
`find_active_by_identity_and_conversation(identity_id, external_conversation_id)`
回填 Interaction 的 session 归属（省略 connection 一维，Port docstring 自称
「有记录的近似」）。

**今天（单 active Connection/Channel）**：无歧义，弱键恰好命中。

**多 Connection 场景（Case B/C）**：同 (identity, conversation) 两条 active
Session 存在时，弱键查询必然 `MultipleResultsFound`（实现是
`scalar_one_or_none`，10.6.1 B5 / 本步 B 探针实证）。而三维键查询免疫。

结论与收敛方向（记录，不实现）：

- **弱键不是 Session 身份**——它是 MVP 单 Connection 假设下的观察捷径；
- 若多 Connection 真实落地，Persistence Observation 必须收敛到三维键
  （= 需要知道本次交互走哪条 Connection）。这要求 Interaction 持久化层能观察
  到连接事实（当前 wrapper 拿不到 RoutingResult，9.1 无 RuntimeResult 边界），
  属于 10.6.x 的接线裁决，不是 SessionRepository 的职责；
- 在收敛前，弱键保持 `scalar_one_or_none` 的 **fail-loud** 语义：真到歧义时
  显式报错，绝不静默返回错误的一条。

---

## 6. 未来裁决（不拍板，只记录触发时机）

| 裁决 | 当前状态 | 触发时机 |
| --- | --- | --- |
| sessions 三维键 DB UNIQUE | 无（9.7 evidence-first 冻结） | 真实并发重复被测试/线上暴露后（双 CREATE_NEW 同键）；到那时再加 `UNIQUE(identity_id, connection_id, external_conversation_id)`（NULL connection 行不参与，语义与 interaction 外部 ID 一致） |
| 弱键去向 | Interaction Wrapper 使用中 | 多 Connection 落地接线时收敛为三维键（需先裁决连接事实如何被观察） |
| agent_id 的会话级一致性 | Session Creation 从 RoutingResult 写入 | 若出现 connection.agent_id 与会话 agent_id 漂移，由真实证据触发派生校验 |

本步维持 9.7 的 evidence-first：**先让真实状态语义出来，不提前加锁/约束。**

---

## 7. 验收（验证探针钉住的事实）

| # | 条件 | 测试 |
| --- | --- | --- |
| 1 | 三维键是 find_active 的完整签名（无 agent_id 参数） | A1 |
| 2 | Adapter 查询 WHERE 不含 agent_id（agent 不参与选择） | A2 |
| 3 | sessions 表当前无 UNIQUE（9.7 快照） | A3 |
| 4 | Session Creation 的 agent_id 来自 RoutingResult，不自查 Agent | A4 |
| 5 | Case A：单键查询确定（稳定返回同一 Session） | B1 |
| 6 | Case B：跨 Connection 查询精确隔离、绝不 MultipleResultsFound | B2 |
| 7 | Case C：两条 Session 同时 active，旧行原样不变 | B3 |
| 8 | 切换 Connection（Agent）→ 先 CREATE_NEW，后 REUSE 自己的那条 | B4 |
| 9 | Persistence Observation 弱键在 Case B/C 下歧义（唯一歧义源） | B5 |
| 10 | 同键重复创建今天允许（无 DB UNIQUE）→ 三维查询 fail-loud | B6 |
| 11 | 契约成文 | C1 |

---

## 8. 一句话版

| 问题 | 答案 |
| --- | --- |
| Session 的真正 Identity？ | `(identity_id, connection_id, external_conversation_id)` —— identity=channel-scoped actor，connection=selected Agent boundary，conversation=channel-side context |
| agent_id 是不是选择键？ | **不是**。Connection 是 Session 的 Agent 隔离键；agent_id 是 Connection 的派生冗余，绝不做第二个查询维度 |
| Case A？ | 同一三维键只有一条 active Session，查询唯一确定 |
| Case B？ | 同 Conversation 跨 Connection 的两条 Session 各自精确命中，绝无 MultipleResultsFound |
| Case C？ | 两条 Session 可同时 active——切换 Agent = 新 Session，不改旧 Session |
| Persistence Observation？ | 弱键是 MVP 单 Connection 假设下的观察捷径；多 Connection 下是唯一歧义源，收敛方向 = 三维键（接线留 10.6.x） |
