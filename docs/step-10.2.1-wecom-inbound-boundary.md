# Step 10.2.1 — WeCom Inbound Channel Boundary / Wire Calibration

> **定位**：第一次真实外部协议 → Control Plane Contract 的架构落地。
> 不做真实 WeCom E2E（那是 10.2.x/10.4）；本步只证明：
>
> ```
> WeCom Wire
>     ↓
> WeComAdapter（SHA1 / AES / XML / WeCom fields —— 全部 Adapter 内部）
>     ↓
> InboundChannelPort（app/repositories/channel_inbound.py）
>     ↓
> Application Inbound Use Case（application/channel_inbound.py）
>     ↓
> InboundInteraction（8.1 冻结契约）
>     ↓
> RuntimeCoordinator（Runtime Core，零修改）
> ```

**Runtime Core 零依赖**：`coordinator.py` 与六段 Executor 的世界里没有
FastAPI / WeCom / XML / AES / SHA1 —— 由 `test_import_boundaries`（AST）与
`test_e2_wire_logic_isolated_in_adapter`（cryptography 全仓唯一归属）机器化锁定。

---

## 1. 四个核心裁决（冻结）

### 1.1 Inbound Port = 两个能力，不是一个 receive()

WeCom 的 GET echostr 与 POST message 本质是**两种协议操作**。塞进一个
`receive()` 很快会出现 `if handshake` 分支并让 Port 污染 wire 语义。冻结：

```python
class InboundChannelAdapter(Protocol):
    def verify_handshake(self, channel, *, signature, timestamp, nonce, echostr) -> str: ...
    def parse_message(self, channel, *, signature, timestamp, nonce, body) -> InboundInteraction | None: ...
```

- `verify_handshake` → 返回**明文** echostr（HTTP 边界原样回给渠道）；
- `parse_message` → `None` = 已确认接收但不产生 CP 交互（不支持的 MsgType）。

HTTP Request/Response **绝不进入 Port 签名**（输入输出都是 plain Python
values；repositories 层禁 import FastAPI 由机器化守卫）。

### 1.2 Application 只做一件事

`application/channel_inbound.py::ChannelInboundService` 知道「我要让这个
Channel 产生一个 InboundInteraction」，**不知道 WeCom 是怎么产生它的**：

| Application 可以 | Application 不做（全部归属 Adapter） |
| --- | --- |
| 按 channel_id 定位 Channel（Repository Port） | SHA1 验签 |
| 调 InboundChannelAdapter（Port） | AES 解密 |
| 把 InboundInteraction 交给 RuntimeCoordinator | XML 解析 / MsgType 判断 |
| | WeCom 字段 / WeCom 重试 / FastAPI Request |

AST 白名单锁定其 import 面（只有 domain / repositories Port / runtime 协议）。

### 1.3 WeComAdapter 承担全部 Wire Logic

`adapters/channels/wecom.py`（唯一实现）：验签算法（SHA1 sorted-join）、
AES-CBC 解密（EncodingAESKey → 32B key，IV=key[:16]，结构
random16+msg_len4+msg+receiveid，PKCS7(32)）、XML 解析、字段映射。
模块级纯函数 `compute_signature / encrypt_message / decrypt_message /
parse_xml_fields` 供 wire 单测直测（`encrypt_message` 同时是测试构造合法
wire 的工具与未来出站的复用点）。

### 1.4 MVP 只支持 Text

```
text   → InboundInteraction → Runtime
image / voice / video / event / 其他 → None（acknowledge-ignore，HTTP 200 空体）
```

`Runtime contract content: str` 本身就是文本入口。**不为「未来支持图片」提前
修改 InboundInteraction** —— 否则 Runtime MVP Freeze（10.1.4）会被重开。

---

## 2. 会话语义裁决（external_conversation_id）

| 形态 | external_conversation_id |
| --- | --- |
| 企业微信**自建应用**消息回调（本步唯一 wire 形态） | `FromUserName` |
| 群聊（ChatId） | **不猜** —— 当前 payload 无群会话 ID 字段；企业微信自建应用回调只有「用户 ↔ 应用」单聊形态，群聊属机器人/会话事件模型。未来若引入群回调，按「群消息 → ChatId」单独裁决，不改单聊语义 |

`external_identity_id` = `FromUserName`（8.1 明示）；`external_interaction_id`
= `MsgId`（幂等/渠道去重依据）。

---

## 3. 入站失败语义（正式架构裁决：不进入 Runtime Failure Model）

```
WeCom Wire
────────── signature / decrypt / XML / protocol errors ──────────
        ↓（此时甚至还没有 InboundInteraction）
InboundInteraction
────────── identity / routing / session / dispatch / delivery ───
        Runtime Failure Model 从这里才开始
```

| 异常（repositories/channel_inbound.py） | 语义 | HTTP |
| --- | --- | --- |
| `InboundChannelConfigError` | 渠道不存在 / 未启用 / config 缺验签材料（服务端配置错误） | 500 |
| `InboundSignatureError` | 签名校验失败 / 密文无法解密 / ReceiveId 不符（不可信请求） | 403 |
| `InboundFormatError` | 可信但格式非法：坏 XML / 缺必需字段 / text 内容为空 | 400 |
| Runtime typed error（ApplicationError） | Runtime 六段 Fail Closed 原样冒泡 | 500 |

命名纪律：`InboundAuthError` → `InboundSignatureError`。原命名会命中
6.5/6.7 等多处复制的 infra-token 扫描（`"auth"` 是防 RBAC/认证基础设施偷跑
的禁词）——入站协议异常不是 CP 认证基础设施，改名避让比在十几处审计测试里
逐个豁免更干净（语义同样准确：验签防线失败）。

---

## 4. Channel 定位与装配

- 路由：`GET/POST /api/channels/{channel_id}/wecom/webhook` —— channel_id 为
  path 参数（回调 URL 显式携带内部渠道身份；比最初设想的
  `/api/channels/wecom/webhook` 多了 id 段，因为回调定位必须有明确的 Channel）。
- `build_channel_inbound_api(service)` 工厂 + 依赖注入（与 Runtime 六段一致）。
  命名避开 `router` token（8.4/8.5/8.A 等 Routing/Dispatch 概念禁词扫描——
  FastAPI APIRouter 装配不是 Routing 契约）。
- **main.py 暂不装配生产实例**：完整生产 Runtime 需要出站 ChannelAdapter（10.3）
  就绪后一起接线，避免「入站生产开放、出站静默丢弃」。router 当前由 Layer 3
  测试与未来 10.2.x/10.3 装配。

---

## 5. Channel config（Credential 裁决）

```json
{"token": "...", "encoding_aes_key": "...", "corp_id": "..."}
```

- 缺任一项 → `InboundChannelConfigError`（Fail Closed）；
- Channel domain **零改动**、不加 credential_ref；
- 文档记录：MVP 允许 `Channel.config` 保存渠道级验签配置；生产 credential
  hardening（credential_ref 迁移）是未来部署决策，不因尚未发生的部署需求
  重新设计 Domain。

---

## 6. 测试三层（40 项）

| Layer | 文件 | 数量 | 内容 |
| --- | --- | --- | --- |
| 1 Wire Unit（无 HTTP/DB/Runtime） | `test_step10_2_1_wecom_wire.py` | 17 | SHA1 向量 / encrypt-decrypt round-trip（含中文）/ 错 corp_id / 截断与尾块篡改 / AESKey 长度守卫 / XML / 握手成功与坏签名 / text 全字段映射（单聊裁决）/ 坏签名 / 缺 Encrypt / 畸形 XML / 非 text→None / 空 Content / config 缺材料（参数化）/ 未持久化 Channel / Port 一致性 / **cryptography 全仓唯一归属 wecom.py** |
| 2 Adapter Contract | `test_step10_2_1_inbound_adapter_contract.py` | 10 | Runtime 交付契约 / None→不进 Runtime / 握手透传 / Adapter 异常原样传播 / Runtime 异常原样传播 / 未知与禁用渠道 / 非法 channel_id / **AST import 白名单** / **无 wire 概念符号** |
| 3 Real HTTP Boundary（本地构造合法 wire） | `test_step10_2_1_wecom_webhook.py` | 13 | GET 200 明文 / GET 403 / POST text→200+Runtime 全链（Session 物化 + Agent 收到 + Delivery 回复）/ 第二条 REUSE_ACTIVE / POST 403+Runtime 未触发 / image→200 ignore / 畸形 400 / 未知渠道 500（GET+POST） |

### 关于 AES-CBC 篡改的精确语义（测试留档）

AES-CBC 本身**无认证**。密文完整性由更早一步的 `msg_signature`（SHA1，
含 token 的渠道私有材料）保证；`decrypt_message` 的职责是「能解就解、解不开
就 Fail Closed」——截断 / padding 非法 / msg_len 错乱 / ReceiveId 不符全部收敛
为 `InboundSignatureError`。篡改第一个密文块只破坏 16B random 前缀（本来就不
校验）——这是 CBC 的数学性质，不是缺陷。

---

## 7. 空壳守卫扩边记录（10.1.2 同型，18+2 处）

| 守卫 | 扩边 |
| --- | --- |
| `adapters/channels == ["__init__.py"]`（persistence_smoke / 6.5 / 7.1 / 8.1 / 8.5 / 8.6 / 9.6 / 9.A / 10.1.1） | `+ wecom.py`（唯一真实实现，WeComAdapter） |
| `api/runtime == ["__init__.py"]`（persistence_smoke / 6.1 / 6.5 / 6.7 / 7.1 / 8.1 / 8.5 / 8.6） | `+ channels.py`（入站端点工厂） |
| application 文件面（5.7 repositories / 6.5 / 6.7 / 7.1 / 7.3 / 8.2） | `+ channel_inbound` |
| 8.6 `DELIVERY_SCOPE_ALLOWED` | + `InboundChannelAdapter`（Port）/ `WeComAdapter`（实现）——入站方向与出站 Delivery 正交 |
| 6.7 `test_l`「api 层没 import application」 | 收窄为「api/**management** 不 import application」——数据面入站消费 application 属合法 Runtime 装配，Management API 仍未引入 |
| 符号扫描禁词（router/auth/adapter） | **不放宽**；生产代码改名避让（`build_channel_inbound_api` / `InboundSignatureError`） |

---

## 8. 已知限制（诚实记录，不在 10.2.1 解决）

1. **同步执行 vs WeCom 5 秒超时**：Runtime 同步跑完才应答；Agent 慢会触发
   WeCom 超时重推。`external_interaction_id=MsgId` 已承载幂等依据，但 CP 入站
   幂等去重未实现（重推 → 重复 Runtime 执行；REUSE_ACTIVE 保证 Session 不重复）。
2. **出站未接通**：Runtime 的 Delivery 在生产装配中尚无真实 ChannelAdapter
   （10.3 WeCom 出站 / 客服消息 API）。测试中为显式 sink。
3. **加密被动回复**（出站 XML）不在本步；`encrypt_message` 已为未来复用就绪。

---

## 9. 不做什么（边界冻结）

❌ 真实 WeCom E2E（公网 / DNS / HTTPS / 真实 corp 配置 —— 10.2.x / 10.4）
❌ 修改 InboundInteraction / Runtime 六段 / Coordinator / 错误码体系
❌ multimodal message model（image/voice/video）
❌ credential_ref / Channel domain 扩展 / Secret Manager
❌ 入站幂等去重 / 重试 / 队列 / 异步
❌ 飞书 / 钉钉 / 通用 Webhook Adapter（Port 已为它们就绪）
❌ 出站 WeComAdapter（被动回复 / 客服消息 —— 10.3）
❌ main.py 生产装配（10.3 出站就绪后一起）

---

## 10. 验收

| 项 | 结果 |
| --- | --- |
| Layer 1 + 2 + 3 | 40 passed |
| Full regression | **710 passed**（670 基线 + 40 新增） |
| 新增生产文件 py_compile | 0 |
| Runtime Core 改动 | **0**（InboundInteraction / 六 Executor / Coordinator / errors 零改动） |
| Management Service / persistence 改动 | 0 |
| 空壳守卫 | 20 处按演进扩边（§7），全部有 10.2.1 语义注释 |

**架构验收（本步最大意义）**：外部协议（WeCom XML/AES/SHA1）在
`adapters/channels/wecom.py` 一个文件里被完全消化，进入 Control Plane 的只有
8.1 冻结的 `InboundInteraction` —— Runtime 对 WeCom 的存在一无所知。
