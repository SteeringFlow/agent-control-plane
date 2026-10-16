# M6.6-C3-CP2 — Inbound Trust Gate Audit（wecom / wecom_bot / local）

> 性质：**READ-ONLY AUDIT**。未修改任何代码、DB、migration、配置、测试；未创建/删除数据；
> 未 commit / 未 tag；未执行任何写入型验证（不跑会写 SQLite / PostgreSQL / local_outbox 的检查）。
> 本轮唯一产出物 = 本文件。所有结论均来自源码 / Adapter / 路由 / nginx 模板 / 既有测试的静态阅读。
>
> 上游：`M6_6_C3_CP1_...md` §14（CP-2 = 硬前置）与 `M6_6_C3_R_...md` §9.4 / §14 R-2。
>
> **结论先行：GAP CONFIRMED（FIX REQUIRED，非 BLOCKED）。**
> CP-1 §9.4 记录的是 **1 条**旁路（`wecom_bot` via `POST /wecom/webhook`）；
> 本轮取证发现真实结构是 **3 条**未认证注入面（同一根因：**HTTP 路由按 `channel.type` 分派，
> 却不对「该类型是否拥有 HTTP 回调语义」做任何约束**）。
> 修复面极小且**不需要新增任何 secret**；但**修复落地前，CP-3（Arm / Identity Bootstrap）不得进入实现**。

---

## 1. Executive Summary

| # | 问题 | 结论 |
|---|---|---|
| 1 | `wecom_bot` 经 HTTP 能否被无凭据注入？ | **能**。`POST /api/channels/{id}/wecom/webhook` 与 `POST /api/channels/{id}/local/message` **两条**路由都可注入（§7 矩阵） |
| 2 | 是否只有 `wecom_bot` 受影响？ | **不止**。`local` 类型经 `POST /wecom/webhook` 同样可注入（同根因）；`local` 本身在 Cloud 拓扑下**完全无认证** |
| 3 | 该缺口当前危害是什么？ | 一条**真实 Interaction**（PENDING 落库 + ACK 200 + Worker 执行 → Identity Resolution Fail Closed）。当前**无授权写入**（Grant/Identity 零创建） |
| 4 | 启用 Bootstrap 后的危害？ | 若该渠道处于 arm 窗口内 → **匿名请求即可触发 Identity + Grant 创建**（未认证授权写入）→ 这就是 CP-1 判 CONDITIONAL 的那一条 |
| 5 | 是否可在 Adapter 层修复？ | **不能**。WS 合法路径与 HTTP 注入路径调用**同一个入口、同一组空参数**（§8-Q2），Adapter 无法区分 → 修复必须在 **HTTP 路由边界** |
| 6 | 官方是否有 per-request 认证材料？ | **没有**。长连接协议只在**连接建立期**认证（`aibot_subscribe`），帧上无签名 → HTTP 接受 `aibot_msg_callback` **原理上不可认证**，必须拒绝 |
| 7 | 是否需要新增 secret / 配置？ | **不需要**。修复 = 路由钉死渠道类型（纯类型判定） |
| 8 | 是否影响既有 ACK / `aibot_respond_msg`？ | **不影响**（§9）。ACK 边界、CAS claim、出站 `deliver` 全部不经过被修改的那一行 |
| 9 | 最小修复边界？ | **3 行判定 + 1 个新参数（默认 None，向后兼容）**（§9.3） |
| 10 | 修复回归风险？ | **低**。既有测试对 `wecom`（走 `/wecom/webhook`）与 `local`（走 `/local/message`）都是**类型自洽**的，不触发新判定 |

**一句话**：当前**不是**「WeCom webhook 有 bug」，而是
**入站 HTTP 路由没有声明自己服务哪种信任模型**。
`wecom`（每请求验签）与 `wecom_bot`（订阅连接）是两个**不同的信任模型**，
但当前两条 HTTP 路由都按 `channel.type` 无条件分派 —— 于是「无验签模型」的渠道被塞进了
「有验签模型」的入口，反向亦然。CP-2 = 把「路由 ↔ 信任模型」重新钉成一一对应。

---

## 2. Scope & Method

**已读（只读）**：

| 类别 | 文件 |
|---|---|
| 路由 / 组合根 | `app/api/runtime/channels.py`、`app/main.py`（`:384-385` 装配、`:419-425` WS 帧入口） |
| 端口 / 分派 | `app/repositories/channel_inbound.py`、`app/adapters/channels/registry.py` |
| 三个 Adapter | `app/adapters/channels/wecom.py`、`wecom_bot.py`、`local.py` |
| 入站用例 | `app/application/channel_inbound.py` |
| 渠道类型集合 | `app/application/channels.py:44`（`ALLOWED_CHANNEL_TYPES`） |
| 部署边界 | `_deploy/nginx/acp-pilot-vhost.conf.template:20,77-86`、`_deploy/nginx/README.md` |
| 鉴权开关 | `app/core/config.py:40`、`app/api/auth.py:179-203` |
| 既有测试 | `test_step10_2_1_wecom_webhook.py`、`test_3b_2_2_wecom_bot_websocket_transport.py`、`test_3b_3_3_wecom_bot_group_chat.py`、`test_cloud_0_1_3_3_multitenant_wecom_e2e.py`、`test_step15_2_3_local_first_success.py`、`test_package_2_step_7_real_wecom_e2e.py` |

**方法**：以「外部可达入口 → 信任判定点 → Interaction 落库」为轴逐行取证；
所有断言均给出 `文件:行`。

**未做**：未连服务器核对实际渲染后的 nginx 配置（本审计只覆盖**仓库内模板**；
生产实配是否与模板一致，必须在修复轮一并核对 —— 见 §9.6 验证项 V-3）。

---

## 3. Current Inbound Topology（外部可达入口只有 3 个）

```
① GET  /api/channels/{id}/wecom/webhook   → ChannelInboundService.handle_handshake
② POST /api/channels/{id}/wecom/webhook   → ChannelInboundService.accept_message
③ POST /api/channels/{id}/local/message   → ChannelInboundService.accept_message（三参数传空串）
④ WS   aibot_msg_callback 帧              → main.py::_on_bot_frame → accept_message（三参数传空串）
```

四条入口**全部汇合**到同一个方法
（`channel_inbound.py:196-238`，内部顺序写死为
`_load_channel（仅定位）→ adapter.parse_message（信任闸门）→ tenant scope → 幂等回查 → create_pending`）。

关键事实：**②③④ 三条入口调用的是同一个 `accept_message`，入参形状完全相同**
（② 传 URL query，③④ 传空串）——**入口身份（transport）没有进入任何一层**。

> 因此「信任闸门」在代码里是一个**位置**（`adapter.parse_message` 那一行），
> 而**不是一个可判定的谓词**：谁是闸门由「谁被分派到」决定，
> 而「谁被分派到」由 `channel.type` 决定（`registry.py:53-54, 75-90`）。

---

## 4. Trust Model A — `wecom`（HTTP webhook）：每请求验签

**所需材料**（`wecom.py:458-468`）：`channel.config` 的 `token` / `encoding_aes_key` / `corp_id`；
任一缺失 → `InboundChannelConfigError` → **HTTP 500**（Fail Closed，不猜、不回退）。

**POST 信任链（逐行）**：

```
api/runtime/channels.py:119-125   body 原文 → accept_message(msg_signature, timestamp, nonce, body)
channel_inbound.py:220            _load_channel(channel_id)          ← 仅定位，不构成信任
channel_inbound.py:221-224        adapter.parse_message(...)         ← 信任闸门
wecom.py:520                      _material(channel)                 ← 缺配置 → Fail Closed
wecom.py:521-522                  signature/body 形状校验 → InboundFormatError（400）
wecom.py:525                      _envelope_encrypt(body)            ← XML <Encrypt> / JSON {"encrypt"}
wecom.py:528-529                  compute_signature(token, timestamp, nonce, encrypt) != signature
                                     → InboundSignatureError（403）   ← ★ 认证点
wecom.py:530-531                  decrypt_message(..., expected_receiveid) → AES-CBC 解密 + PKCS7 + ReceiveId 校验
                                     → 任一不符 → InboundSignatureError（403）
wecom.py:534-541                  明文（XML/JSON）→ InboundInteraction（缺字段 → 400）
```

**判据（可机器化）**：
`SHA1(sort(token, timestamp, nonce, Encrypt)) == msg_signature`
**且** AES 解密成功 **且** `receiveid == 期望值`。
→ 这是**逐请求**的密码学校验；`channel_id` 只是**定位键**（伪造它只会命中 404/500 或该渠道真实租户）。

**GET 握手**：`wecom.py:491-499` 同一算法校验 `echostr` 后解密回显。

---

## 5. Trust Model B — `wecom_bot`（WS long connection）：订阅连接

**协议事实**（`wecom_bot.py:16-43`，模块自述）：
连接建立期 `aibot_subscribe` 携带 `headers.req_id + body{bot_id, secret}`，
响应 `errcode == 0` 即认证完成；**此后帧上没有任何逐条签名**。

**材料**（`wecom_bot.py:214-240`）：`channel.config` 的 `bot_id` / `bot_secret`（缺 → Fail Closed），
`ws_url` 可选。认证**只发生在建连**，且该事实**不外泄**到帧处理路径。

**入站信任链（WS 合法路径）**：

```
main.py:399-417   discover_bot_connections()：bootstrap 只读投影（type==wecom_bot 且 status==active）
main.py:427-431   WeComBotTransport(registry, binding_source, on_frame=_on_bot_frame)
main.py:419-425   on_frame(binding, raw) → accept_message(binding.channel_id, signature="", timestamp="", nonce="", body=raw)
channel_inbound.py:221-224  adapter.parse_message(...)      ← 分派到 WeComBotAdapter
wecom_bot.py:592  channel.id 校验
wecom_bot.py:595  parse_frame(raw)                          ← 仅 JSON 形状校验
wecom_bot.py:600-608  aibotid 归属校验（见 §6.2 短路分析）
wecom_bot.py:610  inbound_from_callback(payload, channel.id)
wecom_bot.py:370-371  cmd != aibot_msg_callback → None（acknowledge-ignore）
wecom_bot.py:372+  body.msgid / from.userid / text.content / headers.req_id 缺失 → InboundFormatError
                   ★ 全部字段都是**帧自称**，无一可凭外部材料验证
```

**判据（当前代码）**：**不存在**。`parse_message` 对 `signature/timestamp/nonce` 三参数
`wecom_bot.py:589-590` 明写「对本 transport 无意义，保留参数只为满足 Port 形状」，
且测试 `test_3b_2_2...py:413-420`（`test_c2b`）**主动断言**其不受签名参数影响。
→ 这**不是** bug，而是**记录在案的信任模型**：信任来自 `registry` 里那条活连接。

**问题不在 Adapter，而在「谁允许 HTTP 路由分派到它」**（§7）。

---

## 6. Trust Model C — `local`（HTTP 开发渠道）

- `local.py:65-78` `verify_handshake` → 原样回显 `echostr`（明写「不存在『不可信请求』概念」）。
- `local.py:80-129` `parse_message` → **无任何验签**，只要 `external_id` / `text` 非空即产出 `InboundInteraction`。
- `local.py:7-8` 自述用途：「让 OSS V0.x 在**无企业微信、无公网 HTTPS、无回调验签**的前提下拥有一个真实入站入口」。
- **`local` 的信任模型 = 无认证（开发渠道语义）**，这是**设计如此**，但只在「路由只服务它自己」时成立。
- 鉴权事实：`main.py:384-385` 装配 `build_channel_inbound_api(...)` **无任何 auth 依赖**；
  `app/core/config.py:40` 的 `AUTH_REQUIRED` 只作用于 `build_tenant_scope_provider`
  （`api/auth.py:179-203`）即**管理面**依赖，**不覆盖 runtime 入站路由**。
  → `/api/channels/{id}/local/message` 在应用层**未认证可达**；
  nginx 侧当前模板把它归入「prefix 兜底：公开代理（授权归应用层）」（模板 `:22`）→ 亦即**公网可 POST**。

---

## 7. ★ 核心 Finding：HTTP 路由按类型分派，却无「信任模型 ↔ 类型」约束

`registry.py:53-54`：

```python
def _for_channel_type(self, channel_type):
    return self._adapters.get(channel_type)      # ← 唯一的分派依据
```

`MultiChannelAdapter` 映射（`main.py:106-113`）：`{"wecom": WeComAdapter, "wecom_bot": WeComBotAdapter, "local": LocalChannelAdapter}`。

于是 **任一条 HTTP 路由都会把请求交给「该 channel.type 对应的 Adapter」**，
而**不检查**「该 Adapter 的信任模型是否与这条路由的语义一致」。

### 7.1 结构矩阵（4 路由 × 3 类型，逐格取证）

| 入口 \ `channel.type` | `wecom` | `wecom_bot` | `local` |
|---|---|---|---|
| `GET /{id}/wecom/webhook`（握手） | ✅ 正确：验签 + 解密回显（`wecom.py:491-499`） | ✅ Fail Closed：`InboundChannelConfigError` → 500（`wecom_bot.py:560-576`） | ⚠️ 语义错位：**原样回显 `echostr`**（`local.py:74-78`）——无副作用，但握手语义被伪造 |
| `POST /{id}/wecom/webhook`（消息） | ✅ 正确：逐请求验签（`wecom.py:501-541`） | ❌ **旁路 1**：无验签，接受任意 `aibot_msg_callback` JSON（`wecom_bot.py:578-610`） | ❌ **旁路 3**：无验签，接受任意 local wire JSON（`local.py:80-129`） |
| `POST /{id}/local/message`（消息） | ✅ Fail Closed：空签名 → `InboundSignatureError` → 403（`wecom.py:528-529`） | ❌ **旁路 2**：同旁路 1 的 Adapter 路径 | ✅ 正确（按其设计如此） |
| WS `aibot_msg_callback`（④） | —— | ✅ 正确：仅由持有活连接的 transport 投递（`main.py:419-431`） | —— |

**归因**：4 条入口 × 3 类型 = 12 格，其中 **3 格是无认证注入面**（同一根因）；
另有 1 格（GET 握手 on `local`）是**语义错位但无副作用**。

### 7.2 三条旁路的可达性证明（缺一不可）

以旁路 1（`wecom_bot` via `POST /wecom/webhook`）为例，逐条满足：

```
1. 渠道存在且 active       ← 攻击者只需知道 channel_id（整数主键，可枚举）
2. 路由公开                ← nginx 模板 :20/:77-86 唯一 PUBLIC 面，无 auth_basic
3. 应用层无鉴权            ← main.py:384-385 无 Depends；AUTH_REQUIRED 不覆盖 runtime 入站
4. query 参数可满足        ← msg_signature/timestamp/nonce 是 str 且**无最小长度**，
                             `?msg_signature=&timestamp=&nonce=` 即通过 FastAPI 校验
5. Adapter 不验签          ← wecom_bot.py:589-590 明写三参数无意义
6. 归属校验可短路          ← 见 §7.3
7. body 字段全部自称       ← wecom_bot.py:370-372+（msgid/from.userid/text.content/req_id）
```

### 7.3 `aibotid` 归属校验的三重短路（旁路可稳定复现）

`wecom_bot.py:604-608`：

```python
if (isinstance(expected_bot_id, str) and expected_bot_id.strip()
        and isinstance(claimed_bot_id, str) and claimed_bot_id.strip()
        and claimed_bot_id.strip() != expected_bot_id.strip()):
    raise InboundFormatError(...)
```

**四个条件同时成立才拒收**。因此：

| 情形 | 判定 |
|---|---|
| `channel.config` 缺 `bot_id` | 跳过校验 → **通过** |
| body 省略 `aibotid`（`claimed is None`） | 跳过校验 → **通过** |
| body 填任意非空 `aibotid` | 进入比较（可被猜到/试出）→ 可能拒 |
| body 填与 config 一致的 `aibotid` | 通过（但该值**不是秘密**） |

→ **攻击者只要省略 `aibotid` 即可 100% 稳定绕过**该「纵深防御」。
（该设计在**合法 WS 路径**上是合理的容错，因为那里信任已由连接建立；
但一旦被 HTTP 路由复用，它就从「容错」变成「可绕过的唯一检查」。）

> 这是本审计与 CP-1 §9.4 的**关键差别**：CP-1 说「aibotid 校验是防御」；
> 本轮取证证明它**不是**可依赖的信任判据（三重短路 + 值非秘密）。

---

## 8. 逐条回答 CP-2 审计问题

### Q1 — `wecom_bot` 当前 HTTP callback 的完整信任链是什么？

**答：不存在信任链。** HTTP 路径上 `wecom_bot` 的判定只有三件事，且都不是认证：

```
① channel.id 存在 且 channel.status == "active"        （channel_inbound.py:125-141）
② body 是合法 JSON 对象                                 （wecom_bot.py:304-314）
③ aibotid 三重短路校验（可被省略绕过）                    （wecom_bot.py:604-608）
```

④ 之后 `cmd == aibot_msg_callback` 即产出 `InboundInteraction`
（`wecom_bot.py:370-371`；字段缺失只判 400，不判可信性）。

→ **「HTTP 上到达的 wecom_bot 帧」与「订阅连接上到达的同一帧」在代码中完全不可区分。**

### Q2 — 为什么当前 `parse_message` 把 signature 当空占位？

**答：因为 Port 是「HTTP 入站」的形状，而 `wecom_bot` 没有 HTTP 入站语义；Adapter 用空占位向 Port 妥协。**

三层原因，均有代码/文档依据：

1. **Port 形状统一**：`InboundChannelAdapter.parse_message(channel, *, signature, timestamp, nonce, body)`
   （`repositories/channel_inbound.py:83-101`）为 WeCom wire 定义；`wecom_bot` 必须实现同一 Protocol。
2. **协议事实**：长连接帧**不携带**签名（`wecom_bot.py:39-43`）→ 没有可填的值。
3. **既有先例**：`local` 已用同一手法（`api/runtime/channels.py:156-159` 对 local 传空串），
   `wecom_bot.py:41-42` 明确引用该先例为自己的正当性依据。

**判定**：这在**「只有 ④ 一条入口」**时是自洽的（信任来自订阅连接）。
**但④不是唯一入口**——②③ 两条 HTTP 路由也能分派到它，于是「空占位」在语义上被滥用为
「无验签即可通过」。**问题由此从 Adapter 上移到路由。**

### Q3 — 是否存在官方/协议层可验证的认证材料？

**答：连接建立期有；每请求/每帧没有。**

| 层 | 认证材料 | 可验证性 |
|---|---|---|
| `wecom`（HTTP 回调） | `token` + `encoding_aes_key` + `corp_id`；`msg_signature`（SHA1）+ AES-CBC + ReceiveId | ✅ **逐请求可验证**（`wecom.py:130-184, 528-531`） |
| `wecom_bot`（长连接）**建连** | `bot_id` + `bot_secret` → `aibot_subscribe` 响应 `errcode == 0`（`wecom_bot.py:16-43, 214-240`） | ✅ 建连期可验证（**但结果不外泄到帧路径**） |
| `wecom_bot`（长连接）**每帧** | **无**（协议无逐帧签名 / 无 HMAC / 无 nonce） | ❌ **原理上不可验证** |
| `local` | **无** | ❌ 无（开发渠道语义） |

**推论（本审计最重要的一条）**：
既然 `wecom_bot` **没有 per-request 认证材料**，那么
**任何接受 `aibot_msg_callback` 的 HTTP 入口都必然是「不可认证的入口」**——
它不可能通过「补一个校验」变成安全入口。
→ 唯一正确的处理是 **拒绝**（fail closed），而不是「加强校验」。
这直接决定了 §9 的修复形状（路由钉死，而非给 Adapter 加校验）。

### Q4 — 当前 HTTP endpoint 能否做到 fail-closed？

**答：能，且已有现成的对称先例。**

`wecom_bot.py:560-576` 的 `verify_handshake` 对**同一条路由的 GET 分支**已经这样做：

```
WS transport 没有 URL 握手语义 → raise InboundChannelConfigError
（自述：对它给出「成功」等于伪造一次不存在的握手）
```

即：**同一路由的「另一半点」已经 fail-closed，只有消息分支缺失**。
修复所需的能力（在路由/用例层判定「该类型是否拥有本入口的语义」）**不新增任何机制**，
只是把 `verify_handshake` 已有的纪律**对称地施加到 `parse_message` 路径**。

### Q5 — long connection 与 HTTP callback 的信任模型分别是什么？

| 维度 | `wecom`（HTTP callback） | `wecom_bot`（long connection） |
|---|---|---|
| 认证时机 | **每次请求** | **连接建立期**（`aibot_subscribe`） |
| 认证材料 | token + AES key + corp_id | bot_id + bot_secret |
| 认证对象 | 单条报文（签名覆盖 `Encrypt` 密文） | 整条连接（此后所有帧继承） |
| 帧/请求级签名 | 有 | **无** |
| 认证强度 | 密码学（SHA1+AES-CBC+ReceiveId） | 传输层会话 + 连接持有事实 |
| 泄露面 | 单条报文不可伪造 | **连接一旦建立，帧不可再验证** |
| 「谁持有它」是否可判 | 不需要（报文自证） | ✅ **是唯一判据**：本进程 `WeComBotConnectionRegistry` 中该 channel 的活连接（`wecom_bot.py:78-81`） |
| 因此 HTTP 是否可承载 | ✅ 可 | ❌ **不可**（HTTP 无「连接持有」这一事实） |

**结论**：两个信任模型**正交且不可互换**；
一条 HTTP 路由**只能**承载 `wecom`，承载 `wecom_bot` 在语义上等于「用无认证入口冒充有认证入口」。

### Q6 — 修复是否会影响已有 ACK / `aibot_respond_msg`？

**答：不会。三条都经取证。**

| 面 | 是否经过被修改的行 | 依据 |
|---|---|---|
| **ACK（HTTP 200 / `{"status":"accepted"}`）** | ❌ 不经过 | ACK 由 `api/runtime/channels.py:128-135,162-168` 在 `accept_message` 返回后产生；修复在 `accept_message` **之前**的判定行失败即 403，成功路径**逐字节不变** |
| **WS 合法入站** | ❌ 不经过 | `_on_bot_frame`（`main.py:419-425`）调用 `accept_message`；修复参数**默认 None = 既有行为**，WS 路径不传该参数 |
| **`aibot_respond_msg`（出站）** | ❌ 不经过 | 出站走 `MultiChannelAdapter.deliver` → `_load_channel_type` → `WeComBotAdapter.deliver`（`registry.py:94-118`、`wecom_bot.py:614-699`）；**入站修复不触碰出站任何一行** |
| **CAS claim / Worker / Runtime 七段** | ❌ 不经过 | 修复发生在 `create_pending` **之前**；被拒请求**不产生 Interaction**，因此 Worker 连一行都读不到 |

**副作用（正向）**：被拒的注入请求**不再产生 Interaction 行**（当前会落一条 PENDING → 终态 FAILED）。
这减少噪声，且使「Interaction 表里的每一行都来自通过信任闸门的入站」成为**可断言的不变量**。

### Q7 — 哪些代码是必须改的？

**最小集（3 处）**：

```
M-1  app/api/runtime/channels.py
     ① 声明「本路由服务的渠道类型」常量（每路由一个）；
     ② 两个 POST 路由 + 一个 GET 路由把该常量传给 service。

M-2  app/application/channel_inbound.py
     ③ accept_message(...) 与 handle_handshake(...) 新增
        expected_channel_type: str | None = None（默认 None = 既有行为，零破坏）
     ④ _load_channel 之后立即判定：
        channel.type != expected_channel_type → 抛 InboundSignatureError（语义见 §9.4）
     ⑤ 类 docstring 的「信任闸门」小节补一句：闸门 = 路由信任模型 + Adapter 验签（两段式）

M-3  （可选，非必须）app/adapters/channels/wecom_bot.py 模块 docstring
     在「安全审计要点」处补一行：本 transport 的帧不得经 HTTP 入口进入 → 由路由钉死（CP-2）
```

**明确不改**：

```
❌ 不改 InboundChannelAdapter Port 签名（不加 transport 参数——见 §9.2 为何不需要）
❌ 不改 wecom_bot.parse_message 的三参数容错语义（test_c2b 的正当性依赖它）
❌ 不改 MultiChannelAdapter 的分派实现（分派本身没错，错在无人声明「本路由允许哪些类型」）
❌ 不改 wecom.py（其信任链本就正确）
❌ 不改 nginx 模板（webhook 必须保持 PUBLIC，WeCom 才能回调）
❌ 不改 ACK / Worker / Coordinator / Executor / Domain / Persistence / migration / DB
❌ 不为 local 渠道新增共享密钥（属独立裁决，见 §14）
```

### Q8 — 哪些现有测试必须新增？

**新增（1 个文件，把 §7.1 矩阵变成断言）**：

```
N-1  矩阵负例（每条旁路一个断言）：
     · wecom_bot 渠道 POST /wecom/webhook  → 403，且 interactions 行数 == 0
     · wecom_bot 渠道 POST /local/message  → 403，且 interactions 行数 == 0
     · local     渠道 POST /wecom/webhook  → 403，且 interactions 行数 == 0
N-2  矩阵正例（修复不得误伤）：
     · wecom     渠道 POST /wecom/webhook  → 200（既有语义）
     · local     渠道 POST /local/message  → 200（既有语义）
     · wecom     渠道 GET  /wecom/webhook  → 200 明文 echostr
N-3  arm 语境下的加强断言（CP-2 的核心性质）：
     · 同一条旁路请求，在一个**处于 arm 窗口内**的渠道上执行 →
       Identity 行数 == 0 且 Grant 行数 == 0（即「未通过 Trust Gate → 即便 Arm 存在也不 Bootstrap」）
N-4  不变量（机器化）：
     · wecom_bot 渠道经任意 HTTP 入口的请求，Agent 从未被调用（agent adapter 收到空列表）
     · 被拒请求**不产生 Interaction**（对比台账：interactions 行数不变）
N-5  源码级守卫（AST / 文本）：
     · api/runtime/channels.py 的两条路由各自声明了 channel-type 常量并传入 service
     · main.py 的 _on_bot_frame 是唯一以空三参数调用 accept_message 的生产调用点
```

**更新（回归核对，预计无需改断言）**：
`test_step10_2_1_wecom_webhook.py`（wecom，自洽）、
`test_cloud_0_1_3_3_multitenant_wecom_e2e.py`（wecom + local，各自自洽）、
`test_step15_2_3_local_first_success.py`（local，自洽）、
`test_3b_2_2_wecom_bot_websocket_transport.py::test_c2b`（**必须保持绿**：它守的是 Adapter 层语义）。

> 修复轮必须**全量跑**上述文件 + `test_cloud_0_1_3_*` + `test_step12_9_4_*`（它们经 `/wecom/webhook` 驱动 `wecom` 渠道）。

### Q9 — 是否需要配置 / secret？

**答：不需要。**

- 修复是**纯类型判定**（`channel.type` vs 路由声明的类型），**零新配置、零新 secret、零迁移**。
- `wecom` 既有材料（`token` / `encoding_aes_key` / `corp_id`）**不动**；
  `wecom_bot` 既有材料（`bot_id` / `bot_secret`）**不动**。
- **明确否定**的替代方案：给 `local` 加共享密钥 / 给 HTTP 入口加 HMAC / 新增 `webhook_secret` 配置
  → 均属**新机制**，超出 CP-2 最小边界（登记为 §13 的 deferred，不做）。

### Q10 — 最小安全修复边界是什么？

**答：把「路由 ↔ 信任模型」钉成一一对应；判定点放在 `_load_channel` 之后、`parse_message` 之前。**

```
POST /api/channels/{id}/wecom/webhook   → 允许且仅允许 channel.type == "wecom"
POST /api/channels/{id}/local/message   → 允许且仅允许 channel.type == "local"
GET  /api/channels/{id}/wecom/webhook   → 允许且仅允许 channel.type == "wecom"
WS   aibot_msg_callback                 → 不传该参数（既有行为不变）
```

**性质（逐条对应本审计）**：

- 修复面 = **1 个新参数（默认 None）+ 3 处传参 + 1 处判定**；Port / Adapter / Runtime 零改动。
- **零认证旁路**：修复后，`wecom_bot` 帧**只能**来自活连接（④），
  `local` 消息**只能**来自其自身入口。
- **可回落**：不匹配即 403，**不产生 Interaction**（比今天更严格）。
- **不新增机制**：与 `verify_handshake` 已有的 fail-closed **对称**，不是新范式。
- **可机器化**：矩阵 12 格全部可断言（§8-Q8）。

---

## 9. Trust Gate 正式定义

### 9.1 两段式安全模型（与 CP-1 的接口）

CP-1 §7 定义 Bootstrap Trigger = 「第一条**通过信任闸门**的入站消息」，
但**没有定义信任闸门是什么**。CP-2 补上这一半：

```
                     ┌─────────────────────────────────────────┐
   入站请求 ────────►│ 段 1 · Trust Gate（transport-authenticated）│
                     │   = 「这条消息是否由它自称的渠道、经其唯一   │
                     │      合法 transport、以可验证的方式送达？」 │
                     └──────────────────┬──────────────────────┘
                                        │
                        ┌───────────────┴───────────────┐
                        │ NO                            │ YES
                        ▼                               ▼
                  DROP（403）                  ┌──────────────────────────┐
                  · 不产生 Interaction         │ 段 2 · Bootstrap eligibility │
                  · 不进入 Runtime             │   = 「是否存在针对本      │
                  · 绝不 Bootstrap             │      (tenant, channel,    │
                  （即使 Arm 有效）            │      connection) 的有效   │
                        │                      │      Arm？」             │
                        │                      └────────────┬────────────┘
                        │                    ┌──────────────┴────────────┐
                        │                    │ NO                        │ YES
                        │                    ▼                           ▼
                        │          既有 Fail Closed              Bootstrap
                        │          （IDENTITY_RESOLUTION_FAILED）  Identity + Grant
                        │                                                 │
                        └─────────────────────────────────────────────────┘
                                                     ▼
                                          Normal Authorization → Router
                                          → Agent → Delivery
```

**必须冻结的性质**：

> **Trust Gate 是 Bootstrap 的硬前提，不是并列条件。**
> 一条未通过 Trust Gate 的入站，**即使对应 Arm 存在、未过期、未消费**，
> **也绝不触发 Bootstrap**。

（这正是用户要求的「两个条件缺一不可」，且**顺序不可交换**：
先认证「这是不是可信的入站」，再判断「这条可信消息是否被允许触发首次授权建立」。）

### 9.2 Trust Gate 的可判定定义（逐渠道）

| `channel.type` | 合法 transport | Trust Gate 判据（可机器化） | 未通过时 |
|---|---|---|---|
| `wecom` | HTTP webhook（②/①） | `SHA1(sort(token, ts, nonce, Encrypt)) == msg_signature` ∧ AES 解密成功 ∧ `receiveid` 匹配 | 403（`InboundSignatureError`） |
| `wecom_bot` | **WS 活连接（④）且仅此一条** | 帧由本进程 `WeComBotConnectionRegistry` 中该 channel 的活连接投递（`main.py:419-431`）；**HTTP 入口一律 403** | 403（路由钉死） |
| `local` | HTTP `/local/message`（③） | **无认证**（开发渠道语义）；因此其合法性**完全依赖**「不得暴露给不可信网络」 | 见 §14（部署裁决 + 可选独立加固） |

**注**：上表对 `wecom_bot` 的判据实现上**不需要新增 marker**——
因为修复后 HTTP 已无法到达它（§7.1 矩阵闭合），
「通过 Trust Gate」等价于「经 ④ 到达」，
而 ④ 的调用点在生产中**唯一**（`main.py:419-425`，可用 N-5 守卫固化）。

### 9.3 为什么不给 Port 加 `transport` 参数（一次否证，避免返工）

需求是「让 Adapter / 用例知道请求来自哪条 transport」——看似要给
`InboundChannelAdapter.parse_message` 或 `accept_message` 加 `transport=` 参数。**不必要，理由三条**：

1. **入口身份在路由层已知且唯一**：4 条入口各在**一个**函数里，路由本身就是 transport 的身份；
   向下传参只增加耦合面（Port 会开始出现 `if transport == ...` 分支，与 `channel_inbound.py:29-31`
   的既有纪律「wire 语义不进端口」冲突）。
2. **钉死类型即可等价表达**：「本路由只服务 type=X」与「本路由只服务 transport=Y」
   在**当前渠道集合**下一一对应（wecom↔②、wecom_bot↔④、local↔③），且判定成本更低。
3. **默认 None 保证向后兼容**：WS 与既有测试完全不受影响（§8-Q6）。

**登记**：若未来出现「一个类型拥有多条 transport」的形态（例：`wecom_bot` 同时支持 WS 与官方 HTTP 回调），
则需重新裁决（届时**必须**有 per-request 认证材料，否则同样不可承载 HTTP）。
该条件今天不成立（§8-Q3）。

---

## 10. 最小安全修复边界（供修复轮直接执行）

### 10.1 修复形状（示意，非最终代码）

```python
# app/api/runtime/channels.py
#: 每条入站路由**声明自己服务的渠道类型** = 声明自己的信任模型。
#: 这不是「路由参数」而是**边界事实**：wecom 的信任是每请求验签，
#: wecom_bot 的信任是订阅连接（无 per-request 材料），local 无认证。
WECOM_WEBHOOK_CHANNEL_TYPE = "wecom"
LOCAL_MESSAGE_CHANNEL_TYPE = "local"

# GET /{id}/wecom/webhook
service.handle_handshake(channel_id, ..., expected_channel_type=WECOM_WEBHOOK_CHANNEL_TYPE)
# POST /{id}/wecom/webhook
service.accept_message(channel_id, ..., expected_channel_type=WECOM_WEBHOOK_CHANNEL_TYPE)
# POST /{id}/local/message
service.accept_message(channel_id, ..., expected_channel_type=LOCAL_MESSAGE_CHANNEL_TYPE)

# app/application/channel_inbound.py
def accept_message(self, channel_id, *, ..., expected_channel_type: str | None = None):
    channel = self._load_channel(channel_id)
    if expected_channel_type is not None and channel.type != expected_channel_type:
        raise InboundSignatureError(
            f"channel_id={channel.id} type={channel.type!r} 不经本入站入口接入"
            f"（本入口服务 {expected_channel_type!r}）")   # → HTTP 403
    inbound = self._adapter.parse_message(...)            # ← 信任闸门（逐请求验签）
    ...
```

### 10.2 状态码裁决（需在修复轮显式确认）

| 候选 | 语义 | 评估 |
|---|---|---|
| **403**（`InboundSignatureError`） | 「本请求在本入口不可认证」 | ✅ **推荐**：与 `wecom` 坏签名同码；不伪装成格式错（400）或服务端错（500）；且已被 `_map_protocol_errors` 支持 |
| 404 | 隐藏渠道存在性 | ⚠️ 可辩，但 channel_id 已是公开定位键（URL 语义如此），「隐藏」收益小、且会让运维排障误判为「渠道不存在」 |
| 400 | 「格式非法」 | ❌ 语义错：请求形状合法，只是**走错了入口** |
| 500 | 「服务端配置错」 | ❌ 语义错：这不是服务端配置问题；且会诱导运维去改配置 |
| 与 GET 握手现状对齐 | GET 目前对 `wecom_bot` 返回 **500**（`InboundChannelConfigError`） | ⚠️ **建议一并对齐为 403**（同一纪律、同一语义），但**属可选**；若为控制回归面而保留 500，**不构成安全缺口**（两者都 Fail Closed）。**必须显式裁决，不得静默保留两种码** |

### 10.3 修复轮的验证项（代码之外）

```
V-1  全量跑 §8-Q8 列出的既有测试 + 新增矩阵测试（12 格）
V-2  arm 语境负例（N-3）：Trust Gate 未通过 → Identity/Grant 行数 0
V-3  【必须】连生产服务器核对**实际渲染后**的 nginx 配置：
       · /api/channels/{id}/wecom/webhook 仍为 PUBLIC（WeCom 需回调）
       · /api/channels/{id}/local/message 的实际归属
         （仓库模板 :22 的 prefix 兜底 = 公开代理；若服务器实配为 BasicAuth，则 local 面已被边缘保护）
       · 结论写入修复轮报告（仓库模板 ≠ 服务器实配时，以实配为准并登记差异）
V-4  部署后以一条**匿名**注入请求做验收（对某 wecom_bot 渠道 → 期望 403 且无新 Interaction）
```

---

## 11. 与既有冻结文档的一致性核对

| 冻结物 | 本修复是否冲突 | 依据 |
|---|---|---|
| 「入站协议异常不进入 Runtime Failure Model」（10.2.1） | **不冲突** | 修复抛的是既有 `InboundSignatureError` → 既有 403 映射（`api/runtime/channels.py:80-81`） |
| 「URL channel_id 仅作定位，不构成信任」（Cloud-0.1.3.1） | **强化** | 修复使「类型」也不可被 URL 选择，进一步收窄信任面 |
| 「wire 语义不进 Port」（`channel_inbound.py:29-31`） | **不冲突** | 判定用的是 `channel.type`（Domain 字段），不是 wire 概念 |
| 「不新增错误码 / 不新增实体」（Package 2-C 纪律） | **不冲突** | 零新错误码、零新实体、零 migration |
| 「ACK ≠ 执行」（12.9.4.6） | **不冲突** | 被拒请求**不产生 Interaction** → ACK 边界不变、更严格 |
| nginx「webhook 是唯一 PUBLIC 面」（14.2.3.C.3 / M5.2） | **不冲突** | 不改模板；只是让该 PUBLIC 面**只服务声明过的类型** |
| ADR-6「Discovery ≠ Authorization」 | **支持** | 修复使「能被投递」进一步不等于「可信」；Bootstrap 仍需 Arm（段 2） |

---

## 12. Frozen Security Red Lines（CP-2 建议冻结）

```
R1  wecom_bot 帧**不得**经任何 HTTP 入口进入（唯一合法入口 = 本进程活连接）
R2  local 消息**不得**经 wecom webhook 入口进入；wecom 消息**不得**经 local 入口进入
R3  未通过 Trust Gate 的入站：不产生 Interaction、不进入 Runtime、**绝不 Bootstrap**
R4  Trust Gate 是 Bootstrap 的**前提**而非并列条件（顺序不可交换）
R5  不得为 wecom_bot 的 HTTP 承载「补一个校验」当作解决方案（无 per-request 材料 → 只能拒绝）
R6  不得让 Trust Gate 的判定绕过 channel.status / tenant 解析等既有校验（顺序：类型 → 验签 → 租户）
R7  不得为实现本修复而新增 secret / 配置项 / migration / 错误码 / 实体
R8  不得改动 wecom 的验签链、wecom_bot 的出站路径、ACK 语义、Runtime 七段
R9  不得把 nginx 的 webhook PUBLIC 面收窄（WeCom 必须可达）；收窄必须发生在应用层类型判定
R10 不得把「local 无认证」当作可在 Cloud 生产承载正式业务的前提（见 §14）
```

---

## 13. Executive Verdict

```
CP-2 AUDIT GATE = GAP CONFIRMED（FIX REQUIRED；非 BLOCKED）
```

**判定依据**：

1. **缺口已证实且可复现**：3 条未认证注入面，根因单一（路由按类型无条件分派），
   判定链 7 步全部有代码依据（§7.2/§7.3），`aibotid` 校验可稳定绕过（§7.3）。
2. **不是「不可调和的架构冲突」**：同一路由的 GET 分支**已经**是 fail-closed
   （`wecom_bot.py:560-576`），修复只是把该纪律对称施加到消息分支 → 属**缺口补齐**，
   非架构变更。
3. **修复面极小且零新依赖**：1 参数（默认 None）+ 3 传参 + 1 判定；零 secret / 零 migration / 零新错误码 / 零 Port 变更。
4. **不阻断、不放行 CP-3**：
   - 本审计**不阻断** CP-2 的修复（缺口明确、边界明确）；
   - 但**CP-3（Arm / Identity Bootstrap）在 CP-2 修复落地前不得进入实现**——
     否则 arm 窗口会把「无害失败路径」升级为「未认证授权写入」（CP-1 §14 R-2 / §9.4）。

**为什么不是 BLOCKED**：BLOCKED 需要「本质上无法安全实现」。
本缺口**有现成的对称范式、有明确判定点、有零成本判据**，且**不需要**任何新机制。

**为什么不是 PASS**：PASS 需要「当前已安全」。当前**不是**：
`wecom_bot` 渠道经 HTTP 可被匿名注入并产生真实 Interaction；
启用 Bootstrap 后即成为未认证授权写入面。

---

## 14. 明确不做 / 登记项（Deferred，不在 CP-2 边界内）

```
D-1  【不做】给 local 渠道加共享密钥 / HMAC 头部
     理由：属新机制；且 local 定位为 OSS/开发渠道。
     登记：**Cloud 生产环境不应存在 active local 渠道**；若必须存在，
           其信任模型需独立裁决（属 Cloud 部署裁决，不属 CP-2）。

D-2  【不做】为入站加速率限制 / IP 白名单 / WAF
     理由：属部署层加固，不解决「无认证入口」这一根因；根因由 R1/R2 解决。
     登记：可在运维层另行叠加（不冲突）。

D-3  【不做】把 GET 握手的 500 统一改为 403（§10.2 已给建议）
     理由：两者都 Fail Closed，无安全差异；为避免扩大回归面，**建议**对齐但**不强制**。
     登记：修复轮显式裁决并写入报告。

D-4  【不做】修改 InboundChannelAdapter Port（加 transport 参数）
     理由：§9.3 已否证；触发条件（一个类型多条 transport）今天不成立。

D-5  【不做】修复 SessionCreationExecutor 的唯一约束冲突（C3-R M3 / F2）
     理由：属 CP-4（并发与原子性），与信任闸门无关。

D-6  【不做】实现 Arm / Identity Bootstrap（CP-3）
D-7  【不做】加固 M3 之外的 at-least-once 语义（CP-4）
D-8  【不做】新增 failure stage / failure code（CP-5）
D-9  【不做】修改 nginx 模板 / 部署材料
```

---

## 15. Handoff

### 15.1 给 CP-2 修复轮（本轮之后的实现轮）

| # | 动作 | 出口判据 |
|---|---|---|
| 1 | 实现 §10.1（参数 + 3 传参 + 1 判定） | 矩阵 12 格全绿（§8-Q8 N-1/N-2） |
| 2 | 裁决状态码（§10.2）并记录 | 报告写明选择与理由 |
| 3 | 新增矩阵测试文件（N-1~N-5） | 含 arm 语境负例（N-3：Identity/Grant 行数 0） |
| 4 | 全量回归 §8-Q8 列出的既有文件 | `test_c2b` 保持绿；`wecom`/`local` 自洽路径不误伤 |
| 5 | 核对生产 nginx 实配（V-3） | 差异登记 |
| 6 | 匿名注入验收（V-4） | 403 且零新 Interaction |

### 15.2 给 CP-3（Arm / Bootstrap 实现）

1. **前置**：CP-2 修复**已落地并验收**（R3 才成立）。
2. **可依赖的不变量（本审计交付）**：
   - 「通过 Trust Gate」在 `wecom_bot` 上 ⟺ 「经活连接到达」（HTTP 已闭合）；
   - Interaction 表**只**由通过 Trust Gate 的入站产生 → **任何 `Worker.process` 的入站都已过闸门**；
     因此 `Bootstrap.ensure(inbound)` 放在执行边界（CP-1 §10.2）是**安全的**，
     不需要在 Bootstrap 内部重复做认证判定。
3. **Bootstrap 必须继承的两段式**：Arm 只在段 2 判定；段 1 已由 intake 完成。
4. **不得在本步顺手做**：不得为「让 Bootstrap 更顺」而放宽任何 Trust Gate 判定（R1/R2/R5）。

### 15.3 移交 CP-4 / CP-5 的登记项

```
→ CP-4：M3（Session 唯一约束冲突 → 回读复用）、M1/M2（Identity/Grant find-or-create）
→ CP-5：Guard 扩展（本审计 N-5 的源码守卫并入）、RUNTIME_EXECUTION_ORDER / STAGE_* 不变式
```

---

## 附：本审计未做的事（只读声明）

未修改任何生产代码 / DB / migration / 配置 / 测试 / 部署材料；
未创建或删除任何数据；未执行任何写入型检查（不跑 SQLite / PostgreSQL / local_outbox）；
未 commit / 未 tag；未连线生产服务器；未设计最终代码字面量（§10.1 的形状为示意，
参数名/常量名/文案由修复轮裁决）。

本文件为**只读审计产出**，**不构成实现批准**；
CP-3 的实现批准需以修复轮的验收结论为前置。
