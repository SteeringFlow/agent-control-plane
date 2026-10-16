# Step 3B-2.6 — WeCom Bot Delivery Result Validation (ACK / errcode)

> 只做一件事：**把 WeCom 两条 transport 的「送达成功」判据，从 transport 层
> （HTTP 2xx / 帧写出 socket）收紧为应用层（服务端 `errcode == 0`）**。
> 不做：Retry / Outbox / 重投 / fallback / 多副本连接路由；不改 ACP Core /
> Domain / Persistence / Repository Port / DB schema。
>
> 结论前置：3B-2.5 的真实探测**证伪**了 12.8.2 的推断「官方未定义响应 schema，
> 故只判 HTTP 层」——同一条路径的 HTTP 200 可以是**未送达**（`errcode=60140`）。
> 只判 HTTP 层会产生「记为 DELIVERED 而用户没收到」的**假绿**。本步据此收紧
> 判据，并已在**两条 transport 上各自完成真实闭环验证**。

---

## A. 驱动本步的真实平台事实

同一条 `response_url` 路径的两次真实调用：

| 情形 | HTTP | 响应体 | 真实含义 |
| --- | --- | --- | --- |
| TTL 内 | **200** | `{"errcode":0,"errmsg":"ok"}` | 送达 |
| 过期后 | **200** | `{"errcode":60140,"errmsg":"invalid response code"}` | **未送达** |

WS 侧同理：`aibot_respond_msg` 会被服务端 ACK
（`{"headers":{"req_id":...},"errcode":0,"errmsg":"ok"}`）。

推论（本步的全部实质）：**Transport-level success ≠ Application-level success**。
凡无法确证 `errcode == 0` 的响应一律 Fail Closed——宁可误报交付失败，绝不假绿。

---

## B. 两个时间维度必须彻底分开（最易被误读的一点）

`DEFAULT_ACK_WAIT_TIMEOUT_S = 10.0` 是 **WeCom application-level delivery
acknowledgement timeout**，**不是 Agent 响应超时**，两者互不叠加、互不换算：

```
Agent execution          ← 归 Worker / Runtime，本模块不感知、不设限
        │  产出 response（8s / 15s / 60s … 皆可）
        ▼
发 aibot_respond_msg
        │  ← DEFAULT_ACK_WAIT_TIMEOUT_S 只覆盖这一段
        ▼
ACK（errcode == 0）
```

| 场景 | 结果 | 判定 |
| --- | --- | --- |
| Agent 8s + ACK ≤10s | DELIVERED | 正常 |
| Agent 15s + ACK 1s | DELIVERED | **正常**（两个维度无关） |
| Agent 8s + ACK 11s | delivery failure | 真正的失败模式 |

因此本常量**不得**被解读为「Agent 最多只能跑 10s」。该说明同时写死在
`app/adapters/channels/wecom_bot.py` 的模块 docstring 与常量定义处，防止未来
在别处被二次误读。

---

## C. 实现

### C.1 三层成功语义（WS 路径）

```
Transport Success   = 帧写进 socket（send 未抛错）
        ↓
Application Success = 服务端响应 errcode == 0 且 headers.req_id 与本次一致
        ↓
Delivery Success    = 两层都成立，才允许落 delivery=DELIVERED
```

收发线程所有权：`recv` 属于 transport 线程（websockets 同步连接不允许两个线程
同时 `recv`）。交付线程（Worker）只**发送**并在 registry 登记等待槽，由 transport
线程收到响应帧时**兑现**该槽。

| 组件 | 位置 |
| --- | --- |
| `DEFAULT_ACK_WAIT_TIMEOUT_S = 10.0` | `app/adapters/channels/wecom_bot.py` |
| `_AckSlot` | 同上 |
| `expect_ack` / `fulfil_ack` / `forget_ack` | 同上 |

Fail Closed 分支（均有测试覆盖）：非 0 errcode、ACK 永不到达、`req_id` 不匹配 →
一律投递失败，**不内置重试**（遵守 10.5.3「不做 Retry / Outbox」）。

### C.2 HTTP 路径判据收紧

新增纯函数 `_response_errcode(body)`：非 JSON / 非对象 / 无 `errcode` /
`errcode` 非 int → `None`（**不算成功**）。`deliver()` 判据由「HTTP 2xx」改为
**HTTP 2xx 且 `errcode == 0`**。失败语义不变（URL 一次性 → `retryable=False`）。

### C.3 未触碰面

`app/domain/*`、`app/persistence/*`、`app/repositories/*`、
`app/application/{coordinator,worker,driver,tenant_fanout,delivery,routing,
identity_resolution,session_*,agent_dispatch,interaction_recording,
channel_inbound}.py`、`adapters/agents/acp.py`、`alembic/*` 均未改动。
`req_id` 仍只经 `InboundInteraction.metadata["bot_reply"]` 走既有通路，
**Domain 契约字段面一行未改**。

---

## D. 真实闭环验证（两条 transport）

### D.1 WS 路径（真实企业微信智能机器人，Bot ID + Secret）

帧级证据（本地 WS 客户端 DEBUG 日志）：

| 时刻 | 方向 | 帧 | 大小 |
| --- | --- | --- | --- |
| 13:31:17,500 | ← 收 | `aibot_msg_callback` … `"content":"afternoon"` | 469 B |
| 13:31:17,608 | → 发 | `aibot_respond_msg` … `"Echo: afternoon"` | 156 B |
| 13:31:18,038 | ← 收 | `{"headers":{"req_id":"JLj0o0A2SB6m7ZiE2YUv5wAA"},"errcode":0,"errmsg":"ok"}` | 75 B |

落库终态：

```
interaction #6: processing_status=succeeded  delivery_status=delivered
                failure_stage=null  failure_code=null  retryable=false
activities:     interaction_received(recorded)
                → interaction_completed(succeeded, stage=delivery)
```

**决定性交叉证据**：ACK 回显的 `req_id` 与交互快照中记录的完全相同
（`request_payload.metadata.bot_reply.req_id = "JLj0o0A2SB6m7ZiE2YUv5wAA"`）
→ `delivered` 是**经 ACK 校验真赚来的**，而非旧版「帧发出去即成功」。

运行期健康：`connected=1  session_failed=0  inbound_failed=0`
（30s 应用层 ping 全部 `errcode:0`；服务端协议级 PING 已正常 PONG）。

### D.2 HTTP 路径

过期 `response_code` → HTTP 200 + `errcode=60140` → 正确落 `delivery=failed`
（不再假绿）。回归见 `tests/test_step12_8_2_wecom_response_url.py` 的 D4b / D4c。

---

## E. 测试记录（严谨表述）

```
3B-2.6 targeted tests                                      PASS
full suite（收尾前首跑）   2055 passed / 7 skipped / 2 failed
full suite（收尾后复跑）   2057 passed / 7 skipped / 0 failed
```

首跑的 2 failures 是 **External Tencent ACP live dependency transient failure**：
失败点都在 `adapters/agents/acp.py` 的 SSE 建连（真实端点 live 测试），而本次变更
文件为渠道侧（`wecom.py` / `wecom_bot.py` / 组合根 / 策略常量），`acp.py` 全程未被
触碰——从变更因果关系看**无证据指向 3B-2.6**。**本步不处理、不为变绿而改代码**：

| 失败项 | 失败点 | 判定 |
| --- | --- | --- |
| `tests/test_step10_1_3_tencent_acp_wire.py::test_live_tencent_acp_wire_smoke` | `adapters/agents/acp.py` SSE 建连失败 → Fail Closed | 真实端点 live 测试；`acp.py` 本步未改 |
| `tests/test_step10_1_4_real_agent_runtime.py::test_runtime_e2e_create_new_with_real_tencent` | 同上 | 同上 |

**收尾复跑（同一命令、同一工作区、无代码变更）→ 2057 passed / 7 skipped / 0 failed**，
两条失败项自行消失。**据此可下结论**：首跑失败**不是由本次代码变更导致**；但究竟
属于「endpoint 瞬时不可达」还是「token / 远端服务状态异常」，证据**不足以进一步
区分**。

> **状态**：observed, non-blocking —— 已关闭（closed without code changes）。不复活
> 服务、不重新接入真实环境去区分 endpoint 与 token，避免重新打开已经关闭的联调边界。
>
> 最终记录（英文定稿）：
> External Tencent ACP live dependency transient failure observed on first
> full-suite run; subsequent identical rerun passed without code changes.
> Root cause not further discriminated between endpoint reachability and
> credential/remote-service state. Non-blocking and closed without code changes.

> 记录纪律：**不得**简写为「全量测试 2055 passed」，也**不得**只保留复跑结果而
> 抹掉首跑事实——两次结果都必须留痕。

新增/调整的测试：

| 文件 | 覆盖 |
| --- | --- |
| `tests/test_step12_8_2_wecom_response_url.py` | D4 改名（要求 errcode==0）、D4b（过期 60140）、D4c（6 例无法确证 → Fail Closed）、D4d（helper 参数化） |
| `tests/test_3b_2_2_wecom_bot_websocket_transport.py` | `b3`（ACK slot 生命周期）、`c6b`（非 0 errcode）、`c6c`（ACK 不到达）、`c6d`（req_id 不匹配）、`g2`/`g3`（订阅 ACK 校验）、`f2`（ACK 拒绝状态矩阵） |

---

## F. 遗留约束与未决

1. **单实例硬约束**：`aibot_respond_msg` 必须经**本进程持有**的活连接发送 →
   backend 必须单实例；多副本时执行 Delivery 的进程未必持有 socket
   （`response_url` 是无状态 POST，故此前无此约束）。在禁止 Redis/Kafka 的
   前提下无法规避。
2. **wire 字段名为校准点**：`cmd` 名与 `secret` / `msgid` / `markdown` 等全部
   收口在 `compose_subscribe_frame` / `compose_ping_frame` /
   `compose_respond_frame` / `inbound_from_callback` 四处，联调时只改这四处。
3. **群聊仍为 acknowledge-ignore**（`chattype != single` → 返回 `None`），
   沿用「不猜 `chatid`」纪律，**未裁决**。
4. **信任模型差异**：webhook 每请求验签；WS 的信任来自「用 `bot_secret` 成功
   建立的订阅连接」本身，帧上无逐条签名。
5. **ACK 等待引入的新失败模式**：ACK 超过 10s 窗口 → `delivery=FAILED` 且不重投；
   已由 `c6c` 覆盖并冻结，属设计而非缺陷（见 §B）。
6. **未做 fallback**：长执行 + ACK 超时后不自动改走 `response_url`
   （其 TTL ≤1h，且「不做重投」纪律在先）。

---

## G. Release Closure 记录

按 **停进程 → 清理 → 残留确认 → 轮换密钥 → 提交** 顺序执行。

| 步骤 | 动作 | 结果 |
| --- | --- | --- |
| A | 停止持有真实长连接的 backend 进程 | 已停止；`18080` 无监听 |
| B | 删除临时脚本（`_probe_*` / `_live_*` / `_verify_*`，共 8 个）与含凭据的临时目录 | 已删除并确认不存在 |
| C | 凭据残留扫描（本地 DB + 工作区 + 临时目录） | 仓库内 **零残留**：6 个本地 `.db` 的 `channels` 表均为空；唯一含真实 `bot_secret` 的文件即 B 步删除的临时库 |
| D | 轮换 `bot_secret`（平台侧） | 由运维在平台控制台执行；**新 Secret 不入库、不落盘、不进对话** |
| E | 提交（仅本步预期代码 + 测试 + 文档） | 见提交记录 |

**副效应（需知悉）**：真实联调所用的本地库在 B 步已删除，故本地**不再存有**任何
Bot 渠道配置。轮换后若需再次联调，须重新创建 `type=wecom_bot` 渠道并写入新
Secret——这是刻意的凭据卫生结果，不是缺陷。
