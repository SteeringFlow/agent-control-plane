# Step 10.2.2 — WeCom Outbound Delivery Boundary / Wire Calibration

> **核心选择**：正式 `ChannelAdapter.deliver()` 路径 = **主动消息 API**（WeCom
> Message API），不是被动回复。这一选择由三个已验证事实共同锁定：
>
> 1. WeCom webhook 有 **5 秒同步响应窗口** —— `webhook → 长时间 Agent execution`
>    不是可靠生产模型；
> 2. 当前 Runtime 是同步执行（`RuntimeCoordinator.execute() -> None`）——
>    AgentResponse 无法从 Runtime 返回给 HTTP 层；
> 3. `ChannelAdapter.deliver()` 已是**独立出站边界**（9.6 冻结）。
>
> 三者叠加后，主动消息 API 比被动 Response 更符合已冻结的架构，且与 Port
> 语义天然匹配：**deliver = 把消息交付给 Channel**，而不是「生成一个稍后由
> HTTP 层取走的 Response」。

```
Runtime → OutboundResponse → DeliveryExecutor → WeComAdapter.deliver()
                                                       ↓
                                              WeCom Message API
```

---

## 1. Wire Contract（10.2.2 校准并锁定）

**Step 1 — 获取 access_token**：

```
GET {api_base}/cgi-bin/gettoken?corpid={corp_id}&corpsecret={corpsecret}
→ 200 {"errcode": 0, "errmsg": "ok", "access_token": "...", "expires_in": 7200}
```

**Step 2 — 发送应用消息（text）**：

```
POST {api_base}/cgi-bin/message/send?access_token={token}
{"touser": "<external_conversation_id>", "msgtype": "text",
 "agentid": <int>, "text": {"content": "<OutboundResponse.content>"}}
→ 200 {"errcode": 0, "errmsg": "ok", "msgid": "..."}
```

失败语义：非 200 / JSON 不可解 / `errcode != 0` / `access_token` 缺失 →
一律 `ChannelDeliveryFailedError`（出站 Port 契约：**不新造错误码**，
`ChannelNotFound/ChannelAuthFailed/ChannelTimeout/...` 禁入——AST 守卫）。

**Recipient 映射（唯一裁决点 = `compose_message_payload`）**：

```
OutboundResponse.external_conversation_id ──→ touser
Runtime 不知道 touser；WeCom 不知道 external_conversation_id
```

这再次证明 8.6 契约合理：OutboundResponse 恰好只带 delivery 需要的字段。

---

## 2. corpsecret 裁决（显式记录，不偷偷塞入）

`corpsecret` 是 **WeCom outbound capability 的新增 Channel credential**；
它**不属于 Runtime Contract，也不修改 Channel domain schema**。MVP 暂存于
`Channel.config`，credential_ref 化留待后续生产化决策——与 10.2.1 既有安全
裁决一致：

```
Channel.config（完整键面）
├── token / encoding_aes_key / corp_id    # inbound（10.2.1）
├── corpsecret / agentid                  # outbound（10.2.2 新增）
├── api_base                              # 可选，默认官方域名（测试替身/私有化）
└── timeout_s                             # 可选，默认 10s
```

**凭据解析**：OutboundResponse 刻意不带凭据（9.6：DeliveryExecutor 不读库），
因此出站实现按 `channel_id` 经 **UoW Port**（构造注入 `uow_factory`）解析
渠道——未装配 / 渠道不存在 / config 缺键 → 全部 `ChannelDeliveryFailedError`。

---

## 3. 被动回复：降级为 wire capability（不删除）

```
Passive Reply Wire  = 已验证的协议能力（build_passive_reply 纯函数）
Active Message API  = 正式 Delivery 路径（ChannelAdapter.deliver）
```

- `build_passive_reply(...)` 保留：明文回复 XML → `encrypt_message` →
  加密信封（Encrypt + MsgSignature + TimeStamp + Nonce），供测试与未来兼容场景；
- **`deliver()` 不走被动回复路径**（AST 守卫：deliver 方法体不引用
  `build_passive_reply`）——HTTP request 生命周期不属于 Port 语义。

---

## 4. 范围控制（10.2.2 只解决一件事）

**只解决**：CP 能否通过 WeComAdapter 把一个 OutboundResponse 正确转换并发送
到 WeCom。

**不引入**（等真实测试证明需要，再单独演进）：

```
❌ access_token cache        ❌ token refresh framework
❌ credential manager        ❌ OAuth
❌ MessageQueue / Celery / Redis / BackgroundWorker
❌ IdempotencyStore / Outbox / RetryFramework
```

每次 `deliver` 独立获取 token（与 10.1.3「不缓存连接」同一纪律）——行为侧
由测试锁定：两次交付 → 两次 gettoken。

---

## 5. WeCom retry / idempotency（known limitation，10.2.2 明确不做）

```
WeCom retry → 同一 MsgId → CP 当前无 inbound idempotency
            = known limitation
            = MsgId already available（external_interaction_id）
            = CP deduplication not implemented
```

不做的原因：去重一旦实现立即要回答「何时算 duplicate / duplicate 是否直接
ack / 之前执行失败怎么办 / Agent 已执行但 delivery 失败怎么办 / Session 怎么
处理」——这是 **Inbound Interaction Identity + Idempotency + Execution
Semantics** 领域，会重新进入 Runtime / Interaction persistence。不是 Channel
Adapter 的纯 wire 问题。真实 Channel E2E 若证明必须解决，再开专门 Step。

---

## 6. 测试三层（33 项）

| Layer | 文件 | 数量 | 内容 |
| --- | --- | --- | --- |
| A Wire Unit（无 HTTP/DB） | `test_step10_2_2_wecom_outbound_wire.py` | 12 | payload 映射（touser/agentid/content/JSON 序列化）/ 非法输入 ×7 / 无 uow_factory / 未知渠道 / 缺 corp_id/corpsecret / 非法 agentid / 非法 timeout / 双 Port 无串扰 / 被动回复信封签名可重算 + 密文解密回读 |
| B Adapter Contract | `test_step10_2_2_outbound_adapter_contract.py` | 5 | 双 Port / OutboundResponse frozen / **AST import 白名单**（Runtime/persistence 零接触）/ **无新错误码**（AST）/ **deliver 不引用被动回复** |
| C Fake WeCom API E2E | `test_step10_2_2_outbound_e2e.py` | 5 | 全链（Runtime→Delivery→deliver→fake API）+ recipient/agentid/content 真实 HTTP 验证 / **两次交付两次 gettoken（无缓存正验证）** / webhook 全闭环（WeCom→CP→WeCom）/ errcode 81013 → delivery 失败冒泡 / corpsecret 错 → token 失败收口 |

Fake WeCom API（本地 ThreadingHTTPServer）：`gettoken` 校验 corpid/corpsecret、
`message/send` 校验 access_token、可编程失败模式——不连真实 WeCom（10.2.2
与 10.2.1 同一变量控制纪律）。

守卫扩边（1 处）：10.1.4「传输实现全仓唯一归属」从 `["acp.py"]` 扩为
`["acp.py", "wecom.py"]`（出站 wire 的 urllib 归属 Adapter 层——架构演进，
语义从「唯一文件」更新为「唯一 Adapter 层」）。

---

## 7. 验收

| 项 | 结果 |
| --- | --- |
| Layer A + B + C | 33 passed |
| Full regression | **743 passed**（710 基线 + 33 新增） |
| Runtime Core 改动 | **0**（OutboundResponse / DeliveryExecutor / Coordinator / 六 Executor / 错误码零改动） |
| Channel domain 改动 | **0**（corpsecret/agentid 走 config，schema 不变） |
| 新依赖 | 0（urllib 标准库） |

**本步意义**：第一次拥有**没有 Test Adapter 的完整 Channel Vertical Slice**——

```
WeCom ──inbound webhook──► WeComAdapter ──► InboundInteraction ──► Runtime
WeCom ◄──Message API──────  WeComAdapter ◄── OutboundResponse ◄── Delivery
```
