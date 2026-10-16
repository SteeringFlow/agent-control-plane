# Example: 接入真实企业微信渠道（wecom-workbuddy · 配方）

> 这是一个**配置配方（recipe）**，不是可独立运行的脚本：接入真实企业微信需要
> 你自己的企业微信自建应用与一个公网可达的 HTTPS 回调地址。本目录**不包含、
> 也绝不要求提交任何真实凭据**。

`wecom` 是 ACP 已内置的真实渠道类型（入站 SHA1 验签 + AES-CBC 解密，出站主动
消息 API），接入它**不需要写任何代码**——只需要配置。

## 1. 企业微信侧准备

1. 企业微信管理后台 → 创建**自建应用**，拿到 `agentid` 与 `corpsecret`；
2. 应用 → 接收消息 → 设置 API 接收：
   - URL：`https://<你的公网域名>/api/channels/<channel_id>/wecom/webhook`
     （channel_id 是第 2 步创建后返回的 id，所以先建 Channel 再回来填 URL）；
   - Token / EncodingAESKey：企业微信生成，即下文 `token` / `encoding_aes_key`。

## 2. 在 ACP 创建渠道（首次配置允许提交完整 config）

```bash
curl -s -X POST http://127.0.0.1:8000/api/channels \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "wecom-workbuddy",
    "type": "wecom",
    "config": {
      "corpsecret": "<你的应用 corpsecret>",
      "agentid":    "<你的应用 agentid>",
      "token":      "<回调 Token>",
      "encoding_aes_key": "<回调 EncodingAESKey>",
      "corp_id":    "<你的企业ID>"
    }
  }'
# -> {"id": <channel_id>, "type": "wecom", "status": "active", "config": {...脱敏...}}
```

说明：

- `config` 里的五个键即企业微信自建应用回调 + 主动消息所需的全部凭据
  （入站验签/解密用 `token`/`encoding_aes_key`；出站 `gettoken`/`message/send` 用
  `corpsecret`/`agentid`）。
- **凭据不会回显**：Management API 对 `Channel.config` 做读时脱敏，响应里的
  secret 只能看到掩码——「引用入、掩码出」是合同保证。
- `channel_id` 拿到后，回到企业微信后台把回调 URL 里的 `<channel_id>` 补上，
  并完成 URL 验证（`GET` 握手由 ACP 的 `wecom/webhook` 端点自动应答 `echostr`）。

## 3. 绑定 Agent

```bash
curl -s -X POST http://127.0.0.1:8000/api/agents -H 'Content-Type: application/json' \
  -d '{"name": "workbuddy", "type": "acp"}'
curl -s -X POST http://127.0.0.1:8000/api/connections -H 'Content-Type: application/json' \
  -d '{"channel_id": <channel_id>, "agent_id": <agent_id>, "name": "wecom-path",
       "config": {"protocol": "tencent-acp-sse",
                  "endpoint": "<acpLink.url>", "bearer_token": "<acpLink.token>",
                  "runtime_id": "<runtime.id>"}}'
```

之后在企微单聊里给应用发消息：入站 → Runtime → Agent → 出站主动消息回到
同一个会话。每条消息的处理过程可在 `GET /api/interactions` 里观察。

## 安全清单

- ❌ 不要把真实 `corpsecret` / `token` / `encoding_aes_key` 提交进任何仓库/示例；
- ✅ 凭据只放 `Channel.config`（数据库内），或经你的运维通道注入；
- ✅ 回调 URL 必须是 HTTPS 且公网可达（企业微信 5s 超时窗口；ACP 入站是
  「先 ACK 后异步执行」，不会超时卡死）。
