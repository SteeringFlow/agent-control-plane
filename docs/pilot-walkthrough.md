# ACP Pilot Walk-through（Operator 指南，14.2.3.C.3 闭环版）

> 目标：一个**未参与 ACP 开发**的 Operator，从零开始完成部署、配置、真实消息
> 收发、观察与安全访问。全程不读源码。

## 0. 前置条件

| 项 | 要求 |
| --- | --- |
| 主机 | 一台有公网 IP 的 Linux 服务器（Docker + Docker Compose 已装） |
| 域名 | 一个指向该 IP 的域名（**不含下划线**，Let's Encrypt 拒签） |
| 端口 | 80 / 443 对公网开放（nginx 用）；8000 **不**对公网开放 |
| WeCom | 企业微信管理后台权限（创建自建应用或智能机器人，拿到回调凭据） |
| ACP Agent | 一个可用的 ACP Agent endpoint + 凭据引用 |

## 1. 准备域名与解析

把域名 A 记录指向服务器 IP，`ping <domain>` 确认解析生效。

## 2. 拉取代码

```bash
git clone <repo-url> ~/acp/repo && cd ~/acp/repo
```

## 3. 启动 ACP 容器（仅绑本机回环）

```bash
cd ~/acp/repo
docker compose up -d --build
docker compose ps          # 期望：Up (healthy)，PORTS = 127.0.0.1:8000->8000
curl -s http://127.0.0.1:8000/health   # 期望 {"status":"ok"}（仅本机可达）
```

说明：数据库 schema 在启动时自动初始化（8 张核心表）；数据落在 named volume
`acp_data`，**容器重建后仍在**。

## 4. 部署 Nginx（TLS + exposure boundary）

```bash
ACP_PILOT_PASSWORD='<你的Operator口令>' \
  bash _deploy/setup_nginx_pilot.sh <你的域名>
```

脚本自动完成：ACME webroot vhost → Let's Encrypt 证书 → **Pilot 路由 vhost**
（仅 webhook 公网直通；其余 Basic Auth；/health 404）→ htpasswd → `nginx -t` → reload。

公网行为自检（可自行复核）：

```bash
# 应穿透到 ACP（422 = FastAPI 参数校验，说明无 auth 拦截）
curl -sk "https://<域名>/api/channels/1/wecom/webhook" -o /dev/null -w "%{http_code}\n"
# 应 401
curl -sk "https://<域名>/api/agents" -o /dev/null -w "%{http_code}\n"
# 应 404（不公开）
curl -sk "https://<域名>/health" -o /dev/null -w "%{http_code}\n"
# 带凭据应 200
curl -sk -u <username>:<password> "https://<域名>/api/agents"
```

## 5. 配置 Agent（Management，带 Basic Auth）

浏览器打开 `https://<域名>/`（用户 `operator` + 你的口令）→ Agent Management →
新建 Agent：`name`（自定义）、`type=acp`、`endpoint`（ACP Agent 地址）、
`credential_ref`（写入后不可读，仅引用）。

## 6. 配置 Channel（WeCom 凭据）

Channel Management → 新建 Channel：`type=wecom`，`config` 填入企业微信回调凭据：

```json
{
  "token": "<回调Token>",
  "encoding_aes_key": "<43位EncodingAESKey>",
  "corp_id": "<企业ID>",
  "corpsecret": "<应用Secret>",
  "agentid": 1000002
}
```

（智能机器人场景可省 `corpsecret`/`agentid`，改用 `response_url`。）

## 7. 创建 Connection（绑定 Channel × Agent）

Connection Management → 新建：选择第 5 步的 Agent 与第 6 步的 Channel。

## 8. 配置 WeCom 回调 URL

在企业微信后台把回调地址设为：

```
https://<域名>/api/channels/<channel_id>/wecom/webhook
```

`<channel_id>` = 第 6 步创建的 Channel 数字 id（Channel 列表可见）。
保存时 WeCom 会发 GET 验证握手——**此请求不需要 Basic Auth**（公网直通面）。

## 9. 建立 Identity（把 WeCom 成员绑定到 ACP User）

Identity Management → 新建：选 active User + 第 6 步的 Channel + 填 `external_id`
= **该 WeCom 成员的 userid**（FromUserName；通常可从企业微信通讯录后台取得）。

Runtime 入站按 `(channel_id, external_id)` 解析身份，**未绑定即 Fail Closed**。

> 若暂时不知道 userid：先跳到 §10 发一条消息，再从 §11 的
> `request_payload.external_identity_id` 读回，然后回到本步补 Identity。

## 10. 发第一条消息 / 发现 external conversation

在 WeCom 中给该应用发一条消息。**它不会收到回复**——这是 V0 的正常 bootstrap：

- 若 §9 的 Identity 尚未建立 → 终态失败 `failure_stage=identity_resolution /
  failure_code=IDENTITY_RESOLUTION_FAILED`；
- 若 Identity 已建立、但该会话尚无 Grant → `failure_stage=authorization /
  failure_code=UNAUTHORIZED`。

两种情况 HTTP 层都是 **200 ACK**（WeCom 后台不显示超时错误），失败事实落在
Interaction 终态上。**这不代表系统故障**（bootstrap 说明见 §13）。

## 11. 检查 Interaction.request_payload（provisioning 依据）

浏览器 → `https://<域名>/interactions.html`（operator 登录）→ 打开刚失败的那条
Interaction 详情 → 读 `request_payload`：

- `external_identity_id` = 发消息的 WeCom 成员（FromUserName）
- `external_conversation_id` = 该会话键（自建应用单聊 = 同一个 FromUserName）

**这是唯一可靠的首次会话键来源。** `GET /api/channels/{id}/known-conversations`
**不能**用于首次发现——它由 `sessions` 投影，而 Session 只在授权成功、执行通过
之后才产生；首条成功之前它必为空。

## 12. 创建 Grant（授权该会话访问该 Connection）

用第 11 步读到的 `external_conversation_id` 建授权（Basic Auth 管理面）：

```
POST /api/connection-conversations
{"channel_id": <C>, "external_conversation_id": "<上一步读到>", "connection_id": <P>}
```

授权是 **default deny**：没有这一行，该会话永远在 authorization 阶段被拒。

## 13. 重新发送 → First Success

在 WeCom 中再发一条消息。此时 Identity 命中、Grant 命中 → 六段执行
（Identity → Authorization → Routing → Session → Agent Dispatch → Delivery）→
用户在 WeCom 收到 Agent 回复。

**First Success 判定**：Observation 中该 Interaction 的
`processing_status=succeeded` **且** `delivery_status=delivered`。
HTTP 200 / `status=accepted` **不是**成功（ACK ≠ 执行）。

> **bootstrap 错误语义**：首次接入时出现 `IDENTITY_RESOLUTION_FAILED` 或
> `UNAUTHORIZED` **不一定**意味着系统故障——若尚未完成 provisioning，它就是
> V0 的正常 bootstrap 过程：被拒 Interaction 的 `request_payload` 提供建立
> Identity / Grant 所需的两个键，补齐后重发即成功。只有
> `succeeded × delivered` 才叫 First Success。

## 14. 观察（Observation）

浏览器 → `https://<域名>/interactions.html`（operator 登录）→ 列表最新一条即本次
Interaction → 点行进详情：

- **双状态**：执行 / 投递 两枚独立状态；
- **失败三元组**（若有）：failure_stage / failure_code / retryable；
- **payload**：入站快照 + Agent 回复快照（默认折叠）；
- **Activity Timeline**：`interaction_received → … → channel_delivered` 完整发生序。

若 Agent 成功但投递失败，会看到 `succeeded × failed` 合法组合——这不是 bug，
是事实呈现；失败原因看时间线 `stage_failure` 行的 metadata。

## 15. 验证持久化

```bash
cd ~/acp/repo && docker compose down && docker compose up -d
```

刷新 Observation：历史 Interaction 与 Management 配置**仍在**（named volume）。

---

## 附录 A — WeCom Bot（群聊）变体

群聊使用 `type=wecom_bot` 渠道（WebSocket 长连接），**不与自建应用 webhook 混用**。
授权键与单聊不同，务必区分：

```
群成员 userid  ≠  群 chatid
external_identity_id     = 发件人 from.userid   ← 每个成员各一条 Identity
external_conversation_id = 群 body.chatid       ← 授权用这个
```

- 授权使用 `external_conversation_id = chatid`，**不是** `external_identity_id = userid`；
- 同一群（同一 chatid）→ **一条** Conversation Grant 覆盖全体成员；
- 不同群 → 不同 chatid → **各自**的 Grant。

```
Group A ── member A ──┐
      └── member B ──┴── same chatid ──► one Conversation grant

Group B ─────────────── new chatid  ──► different grant
```

群聊 `chatid` 同样**只有运行时才出现**（官方仅群聊返回）；缺失 / 空白时入站直接
`InboundFormatError`，绝不回退用 userid 当会话键。发现路径同 §10 / §11：发一条 @Bot
消息 → 在被拒 Interaction 的 `request_payload.external_conversation_id` 读回 chatid。
每个发件成员需各自建立 Identity（键 = `from.userid`）。

---

## 常见问题

| 现象 | 处置 |
| --- | --- |
| WeCom 保存回调失败（握手超时） | 检查 80/443 是否放行、域名解析、`nginx -t` 是否通过；`GET webhook` 手动 curl 应得 4xx/5xx 之外的非 401 响应 |
| 消息发出但 Observation 无记录 | `docker compose logs backend`；确认 POST webhook 可达（401 = 配置打到了 auth 边界，检查 URL 是否带 `/wecom/webhook` 后缀） |
| `/api/agents` 返回 401 | 正常——所有非 webhook 路径都需要 operator 凭据 |
| 忘记 Operator 口令 | 重跑 stage 2.5：重新生成 htpasswd（脚本内命令）后 `nginx -s reload` |
| 执行中长时间无结果 | 列表页该行会有「已执行 Ns（疑似卡住）」提示；孤儿由 Driver 自动回收重跑 |
| 第一条消息无回复，Interaction 显示 `IDENTITY_RESOLUTION_FAILED` | **正常 bootstrap**：该成员尚未绑定 Identity——按 §9 / §11 补齐后重发 |
| 第一条消息无回复，Interaction 显示 `UNAUTHORIZED` | **正常 bootstrap**：该会话尚无 Grant——按 §11 / §12 建授权后重发 |
| 想用 known-conversations 预建 Grant 却发现列表为空 | 正常——它依赖已存在的 Session；首次会话键只能从被拒 Interaction 的 `request_payload` 读取（§11） |
