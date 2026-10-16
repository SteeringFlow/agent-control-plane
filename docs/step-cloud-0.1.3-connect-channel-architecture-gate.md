# Cloud-0.1.3 —— Connect Channel Architecture Gate

> 阶段：Architecture Gate（只读审计 + 架构裁决 + 机器化护栏）。**本次只交付设计裁决
> 与护栏，不写产品代码。**
> 方法：只读审计 + 机器化护栏
> （`backend/tests/test_cloud_0_1_3_connect_channel_architecture_gate.py`，当前 17 项全绿）。
> 配套契约：Cloud-0.1.1-A/B/C（已 GO 且落地）→ Cloud-0.1.2（Workspace & Golden Path
> Gate，GO）→ Cloud-0.1.2.1（Registration → Workspace Entry，已落地）→
> Cloud-0.1.2.2（Connect Agent Architecture Gate，GO）→ Cloud-0.1.2.2.1/2.2（Credential
> 可选化 / Connection Health，已落地）→ Cloud-0.1.2.3（Connect Agent Golden Path，GO）→
> **本 Gate（Connect Channel）**。
>
> **本 Gate 的中心问题（单一裁决标尺）**：
>
> ```
> AuthenticatedUser(user_id, tenant_id)
>   ↓  TenantScope(tenant_id)                       ← 复用 B，唯一 tenant 原语
>   ↓  Connect Channel（产品单页）
>        ├─ Create Channel(type="wecom", config{平台凭据})   ← 入口身份/配置
>        └─ Create Connection(channel_id, agent_id, config{ACP 凭据})
>   ↓  Channel  = 外部消息入口/出口的身份与配置
>   ↓  Connection = Channel ↔ Agent 受控访问路径（承载 ACP 凭据）
>   ↓  WeComAdapter（从 Channel.config 取平台凭据，从 Connection 不取）
>   ↓  Runtime → Interaction
> ```
>
> 一句话：**Cloud 不要因为「Connect Channel / Connect WeCom」这个产品动作，就偷偷长出
> 第二套 Integration / Channel / Connector / ChannelHealth / Onboarding 模型，或把
> WeCom 平台凭据错误地安放到 Connection / Agent。本次把这条红线机器化锁死。**

---

## 0. 结论先行（GO / BLOCKED）

**GO。** 对现有代码库做只读审计后确认，9 个待裁决问题（G1–G9）的最小安全边界全部可定：

- **G1 Channel 是「入口身份/配置」实体**：域只有 `name/type/id/status/config`，
  无 `endpoint`、无 `credential_ref`、无绑定；`config` 是唯一的凭据容器（读时脱敏）。
- **G2 WeCom 凭据 ownership 已由现有 Adapter 裁决**：`token`/`encoding_aes_key`/
  `corp_id`/`corpsecret`/`agentid` **全部住在 `Channel.config`** —— 这是已有
  `WeComAdapter._material`/`_outbound_material` 与 `app/schemas/channels.py` 显式
  写死的边界。WeCom 平台凭据属于 **Channel↔WeCom 平台的身份认证**，与 Agent 侧
  「Connection 承载 ACP 访问凭据」语义正交。不复制到 Connection，不造
  `Channel.credential_ref`。
- **G3 Channel ↔ Connection ↔ Agent 三实体**：Channel 1 ── N Connection N ── 1 Agent；
  WeCom 经 `Channel(type="wecom")` + `Connection(channel_id, agent_id)` 接入
  `Agent(type="acp")`，与 Connect Agent 同一「两事实分离」纪律。
- **G4 Webhook / Long Connection 是 transport，不污染 Channel.status**：webhook 端点是
  runtime intake/deployment concern，不是 Channel 域状态；Long Connection 当引入时是
  新 Adapter 注册，绝不成为 `Channel.status` 的「connected/disconnected」轴。
- **G5 Channel 三轴分离**：`status`（纯管理态：启用/停用）≠ Runtime Health（派生、
  不持久、无 ChannelHealth subsystem）≠ Interaction Outcome（processing_status/
  delivery_status，已在 Interaction 上）。**V0 不需要新的 Channel Health subsystem。**
- **G6 WeCom Authentication 与 Browser Authentication 是两个信任边界**：WeCom 验签/解密
  全在 `WeComAdapter`，绝不 import browser auth / cookie / session；WeCom 平台凭据
  绝不流向 Browser User / AuthenticatedUser。
- **G7 Webhook 路由 `channel_id` 仅是定位，tenant 取自「签名校验后的 `Channel.tenant_id`」**：
  已锁死于 `TenantResolver` 端口契约（"绝不取自 URL"）。**但发现一个实现缺口**
  （F2，见 §13）：当前 `main.py` 把 webhook UoW 硬绑 `TENANT_LOCAL`，且 `Channel` 域
  dataclass 缺 `tenant_id` —— 多租户 Cloud 实现期需补齐，不阻塞本次架构 GO（原则已定）。
- **G8 Connect Channel 复用既有端点/Adapter**：`POST /api/channels`（type=wecom,
  config 含平台凭据）、`POST /api/connections`、`/api/channels/{id}/wecom/webhook`、
  `WeComAdapter` 双端口均已存在，**不新增任何端点/Adapter/类**。
- **G9 禁止第二套模型**：全 `app` tokenize 扫描确认无 `WeComIntegration` /
  `ChannelIntegration` / `Connector` / `CloudChannel` / `WeComChannel` /
  `ChannelHealthState` / `OnboardingState` 等越界类。

---

## 1. 链路固定（冻结）

Cloud V0 「Connect Channel / Connect WeCom」目标链：

```
Browser
  ↓  Browser Authentication (C.1, L1+L2)
  ↓  AuthenticatedUser(user_id, tenant_id)
  ↓  TenantScope(tenant_id)                          ← 复用 B，唯一 tenant 原语
  ↓  Connect Channel（产品单页）
        ├─ Create Channel(name, type="wecom",
        │     config{corp_id, agentid, corpsecret,   ← WeCom 平台凭据，住 Channel.config
        │            token, encoding_aes_key})
        └─ Create Connection(channel_id, agent_id,
              config{endpoint, bearer_token, ...})     ← ACP 访问凭据，住 Connection.config
  ↓  Channel  = 外部消息入口/出口的身份与配置
  ↓  Connection = Channel ↔ Agent 受控访问路径（承载 ACP 凭据）
  ↓  WeCom webhook → ChannelInboundService → WeComAdapter（验签+解密）
  ↓  InboundInteraction → intake.create_pending（PENDING + COMMIT）→ HTTP 200（ACK）
  ↓  （响应之后）Worker.process → RuntimeCoordinator → AgentDispatch(ACPAdapter)
  ↓  Delivery(WeComAdapter.deliver) → WeCom 平台
```

核心裁决（与 B/C/0.1.2/0.1.2.2 一脉相承）：

> **Channel 是「外部消息入口/出口的身份与配置」，Connection 是「Channel ↔ Agent 的
> 受控、已认证访问路径」。凭据归属由「它认证什么」决定：认证 Channel↔Agent 访问路径
> → Connection.config；认证 Channel↔WeCom 平台 → Channel.config。两者语义正交，不互相
> 复制，也不合并。**

---

## 2. G2（核心）WeCom Secret 到底属于哪里？—— 由代码审计裁决

### 裁决

**WeCom 平台凭据（`token` / `encoding_aes_key` / `corp_id` / `corpsecret` /
`agentid`）归 `Channel.config`，不归 Connection、不归 Agent、不造第二套
`Channel.credential_ref`。**

这正是用户强调「不要先假定 Channel credential 必然属于 Channel，也不要假定所有
credential 必属于 Connection」后，由代码审计给出的**非对称但原则统一的**答案：

| 凭据 | 认证什么 | 落点 |
|---|---|---|
| WorkBuddy Bot ID / Secret / ACP endpoint | Channel↔Agent 访问路径 | `Connection.config`（0.1.2.2 ③） |
| WeCom `token` / `encoding_aes_key` / `corp_id` | Channel↔WeCom 平台（入站验签/解密） | `Channel.config` |
| WeCom `corpsecret` / `agentid` / `api_base` | Channel↔WeCom 平台（出站 message/send） | `Channel.config` |
| WeCom `bearer_token` / `runtime_id` / `endpoint`（ACP 侧） | Channel↔Agent 访问路径 | `Connection.config` |

**所有权由语义决定，不靠复制上一阶段结论。** Agent 侧「credential → Connection」是因为
它代表 ACP 访问 Agent 的凭据；WeCom 的 `token`/`corpsecret` 等代表 Channel 与 WeCom
平台之间的身份认证，是不同语义，故归 Channel。

### 实证（readonly 审计）

- `app/schemas/channels.py` 文档与 `SENSITIVE_CHANNEL_CONFIG_KEYS`：
  *"Channel 没有 credential_ref 字段——凭据材料（token / encoding_aes_key /
  corpsecret）直接住在 Channel.config 内"*；响应经 `sanitize_channel_config()` 脱敏。
- `app/adapters/channels/wecom.py::_material`：`config = channel.config ...
  _require_str(config, "token", channel) / "encoding_aes_key" / "corp_id"` —— 入站平台
  凭据只从 `Channel.config` 取。
- `app/adapters/channels/wecom.py::_outbound_material`：`config = channel.config ...
  req("corp_id") / req("corpsecret") / config.get("agentid") / config.get("api_base")` ——
  出站平台凭据同样只从 `Channel.config` 取。
- 全代码库 `WeComAdapter` **无任何** `connection.credential_ref` / `connection.config`
  引用：WeCom 平台凭据与 Connection 完全无关。
- Adapter 文档明确：*`corpsecret 不属于 Runtime Contract，也不修改 Channel domain
  schema——MVP 暂存于 Channel.config，credential_ref 化留待生产化决策`*。

### 护栏（见 §12）

- `test_wecom_credentials_live_in_channel_config_not_connection`：静态确认
  `WeComAdapter` 从 `channel.config` 取 `token`/`encoding_aes_key`/`corp_id`/`corpsecret`/
  `agentid`，且不引用 `connection.credential_ref` / `connection.config`。
- `test_wecom_credentials_are_redacted_on_channel_config_read`：`sanitize_channel_config`
  脱敏 `token`/`encoding_aes_key`/`corpsecret`，保留 `corp_id`/`agentid`/`api_base`/
  `timeout_s`。
- `test_channel_has_no_credential_ref_field`：Channel 域与 `ChannelResponse` 均无
  `credential_ref`。

### 生产化 follow-up（不阻塞 GO）

F4：`Channel.config` 内平台凭据的 Vault/`credential_ref` 化，应作为**独立 Gate** 与
Connection 侧**统一**演进（不提前在 0.1.3 造 `Channel.credential_ref`，避免双套凭据面）。

---

## 3. G1 Channel Domain 真实语义审计

### 裁决

**Channel = 外部消息入口/出口的身份与配置。** 它不承担「到外部系统的真实连接」（那是
Adapter/Runtime 的事），也不承担「到 Agent 的访问路径」（那是 Connection 的事）。

实测 `app/domain/channel.py`（`@dataclass(frozen=True)`）：

```python
class Channel:
    name: str
    type: str
    id: int | None = None
    status: str = "active"
    config: dict | None = None
```

- 无 `endpoint`（那是 Agent 的公开可达字段，Channel 不需要）。
- 无 `credential_ref`（平台凭据直接住 `config`，见 G2）。
- 无 `channel_id`/`agent_id` 绑定（那是 Connection）。
- `config` 是**单一配置容器**：平台凭据（敏感，读时脱敏）+ 非敏感配置
  （`corp_id`/`agentid`/`api_base`/`timeout_s`），不分裂成 permissions/routing 等。

`ChannelModel`（ORM）比域多 `tenant_id` + 时间戳 —— `tenant_id` 是隔离根（B），不是
凭据或配置。**注意**：`Channel` 域 dataclass 当前**不携带 `tenant_id`**（见 G7/F2）。

### 护栏

- `test_channel_domain_is_identity_config_not_connection`：域字段恰为
  `{name,type,id,status,config}`；无 `endpoint`/`credential_ref`/绑定。
- `test_channel_model_has_no_plaintext_secret_column`：`channels` 表无明文凭据列
  （仅 `config` JSON + `tenant_id`）。

---

## 4. G3 Channel ↔ Connection 关系裁决

### 裁决

**Channel 1 ── N Connection N ── 1 Agent。** Connection 仍是「Channel ↔ Agent 的受控
访问路径」（7.1 冻结），承载 ACP 侧凭据（`Connection.config` + 可选
`credential_ref`）；Channel 仍是独立入口实体。

WeCom 进入模型的方式：

```
Channel(type="wecom", config{WeCom 平台凭据})   ← 入口身份/配置
        │  (Connection.channel_id)
        ▼
Connection(channel_id, agent_id, config{ACP 凭据})  ← 访问路径
        │  (Connection.agent_id)
        ▼
Agent(type="acp", {name})                           ← 目标身份/注册（无 secret）
```

「Connect WeCom」= 先 `Create Channel(type="wecom", config{平台凭据})`，再
`Create Connection(channel_id, agent_id, config{ACP 凭据})` —— 与 Connect Agent 的
「两事实分离」纪律完全一致。绝不合成为 `WeComChannel`/`WeComConnection` 单一实体。

### 护栏

- `test_channel_and_connection_are_separate_entities`：Channel 不含绑定，Connection
  含 `channel_id`/`agent_id`。
- `test_connection_create_requires_pre_existing_channel_and_agent`：
  `create_connection` 在 Channel/Agent 缺失时分别抛 `ChannelNotFoundError` /
  `AgentNotFoundError`，并做 `(tenant_id, channel_id, agent_id)` UNIQUE 预检。

---

## 5. G4 Webhook / Long Connection transport 边界

### 裁决

**Webhook 与 Long Connection 是 transport mode，绝不污染 `Channel.status`。**

- **Webhook endpoint 是 runtime intake / deployment concern，不是 Channel 域状态**：
  `/api/channels/{id}/wecom/webhook` 是部署期存在的回调地址；`Channel` 域不持有
  endpoint 字段（`test_webhook_transport_is_not_a_channel_status_axis` 已锁）。URL 中
  的 `channel_id` 仅用于路由定位（§8）。
- **Long Connection 当引入时 = 新 Adapter 注册**（按 `Channel.type` 或新增 transport
  选择字段路由到长连接实现），**不是** `Channel.status` 的新轴。绝不能出现
  `Channel.status = "webhook_connected"` / `"longconn_connected"`。
- WeComAdapter 当前为**无状态**（10.2.2 "不缓存连接"），入站 `verify_handshake` +
  `parse_message`、出站 `deliver` 都是独立调用 —— 与「transport 不构成持久连接态」一致。

### 护栏

- `test_webhook_transport_is_not_a_channel_status_axis`：Channel 域无
  `webhook_status`/`longconn_status`/`transport_status`/`connected`/`disconnected` 等字段。
- `test_channel_status_default_is_active_no_runtime_connectivity`：`Channel.status`
  默认 `"active"`，入站只把它当「是否启用」开关（`channel.status != "active"` → 拒收）。

---

## 6. G5 Channel status / health / interaction 三轴

### 裁决

**区分三件不同的事，绝不把它们揉进一个 Channel 状态机：**

```
Configuration State  (配置态)   → Channel.status（V0 默认 "active"；建议加 active/inactive 白名单，F1）
                                 纯管理态；管理员开关；不表达连接/健康
Runtime Health       (运行健康) → Can WeCom send into Agent? Can Agent reply reach WeCom?
                                 派生诊断；由 Interaction 的 processing_status/delivery_status
                                 提供事实；不持久化、不建 ChannelHealth subsystem
Interaction Outcome  (交互结果) → processing_status / delivery_status（已在 InteractionRecord）
                                 Channel 不吸收
```

- **Configuration State**：沿用既有 `Channel.status`（域默认 `"active"`）。建议实现期
  加 `CHANNEL_MANAGEMENT_STATUSES == {"active","inactive"}` 白名单（与 Connection 对称，
  F1），防「connected/healthy」污染。
- **Runtime Health**：**V0 不需要新的 Channel Health subsystem**（用户倾向已验证）。
  用户真正关心的事实——「WeCom 能否把消息打进我的 Agent」「Agent 回复能否到达
  WeCom」——最终由 `Interaction.processing_status` / `delivery_status` 给出，不另立
  Channel 健康态。若未来需要「渠道连通性探针」，应复用 Connection 侧已冻结的
  「即时诊断快照、不持久化」纪律（0.1.2.2.2），且只在 Interaction/Delivery 失败面体现。
- **Interaction Outcome**：已在 `InteractionRecord` 双轴，Channel 不吸收。

### 护栏

- `test_channel_has_no_health_subsystem`：Channel 域无 `health`/`runtime_health`/
  `last_health`；全 app 无 `ChannelHealthState` 类。
- `test_interaction_outcome_lives_on_interaction_not_channel`：`InteractionRecord` 含
  `processing_status`/`delivery_status`；Channel 不含。

---

## 7. G6 WeCom Authentication ≠ Browser Authentication

### 裁决

**两个完全不同的信任边界，绝对不合并：**

```
Browser Auth:   Cookie → BrowserAuthenticator.verify() → AuthenticatedUser
                → TenantScope → 管理面（/api/channels, /api/connections ...）
WeCom Auth:     WeCom signature/token → WeComAdapter.verify_handshake/parse_message
                → InboundInteraction（Runtime 入口）
```

- WeCom 验签（SHA1）/ AES 解密 / XML 解析**全部在 `WeComAdapter` 内部**；浏览器认证
  在 `app/core/browser_auth.py` + `app/application/browser_auth.py` + `app/api/auth.py`。
- WeCom webhook 是**外部服务器回调**，无浏览器会话、无 Cookie —— 它**绝不**经浏览器
  认证路径；其「认证」完全是 WeCom 签名校验。
- WeCom 平台凭据（存在于 `Channel.config`）**绝不**流向 `AuthenticatedUser` /
  `TenantScope` 的前端载体；两者唯一共享的只是 `TenantScope` 这个隔离原语（B 已冻结），
  而非认证机制。

### 护栏

- `test_wecom_auth_isolated_from_browser_auth`：AST 确认 `wecom.py` 不 import 任何
  `browser_auth` / `app.api.auth` 模块；其认证事实只来自 `channel` + 入站参数
  （`signature` / `verify_handshake`）。

---

## 8. G7 Webhook 路由的 Tenant Resolution + Authentication 顺序

### 裁决

**URL 中的 `channel_id` 只是路由定位，不是 authentication proof，也不是 tenant proof。
Tenant 取自「WeCom 签名校验后的 `Channel.tenant_id`」，绝不取自 URL。**

这直接继承 B 的部署边界裁决，并锁死于 `TenantResolver` 端口契约
（`app/core/tenant.py`）：

> *"routing hint ≠ authentication proof：tenant 必须取自已校验对象（如校验签名后的
> `Channel.tenant_id`），绝不取自 URL"*

正确的多租户 webhook 顺序：

```
1. 从 URL 取 channel_id（仅路由）
2. 加载 Channel（按 id 定位）
3. WeCom 签名校验（adapter 内部；失败 → 403，拒绝不可信请求）
4. 【校验通过后】读 Channel.tenant_id（已校验对象的 tenant）
5. 用该 tenant_id 绑定 UoW → 写 PENDING → Worker 执行同样继承该 tenant 隔离
```

### 实证与缺口（F2，不阻塞 GO）

- `app/core/tenant.py` 端口契约已写死「绝不取自 URL」「校验签名后的 Channel.tenant_id」
  （`test_tenant_resolution_derived_from_validated_channel_not_url` 锁死）。
- `ChannelInboundService._load_channel` 只用 `channel_id` 做 `get_by_id` + `status`
  校验（定位），并把认证委托给 `self._adapter.verify_handshake/parse_message`
  （`test_webhook_channel_id_is_routing_only_not_auth_or_tenant` 锁死）。
- **缺口**：`main.py` 当前把 webhook UoW 硬绑 `TENANT_LOCAL`
  （`tenant_uow_factory = partial(SqlAlchemyUnitOfWork, tenant_id=TENANT_LOCAL)`，
  OSS 单租户缺省，行为等同隔离前）；且 `Channel` 域 dataclass **不携带 `tenant_id`**，
  故校验通过后的 `Channel.tenant_id` 目前无法在 runtime 路径传播。

→ **F2（实现期核心工作项）**：多租户 Cloud 下，webhook 须改为「定位 → 验签 → 读
`Channel.tenant_id`（来自 ORM 或补入域）→ 重绑 UoW」。此缺口不影响本次架构 GO（原则已
定，护栏已锁），但 0.1.3 实现期必须落地，否则多租户 webhook 会错用 TENANT_LOCAL。

---

## 9. G8 Cloud Connect Channel 是否复用现有端点/Adapter

### 裁决

**完全复用，零新增端点/Adapter/类。** 这正是「Golden Path 薄建立在既有 ACP Core 上」的
体现（与 Connect Agent 一致）：

| 产品动作 | 复用端点 / 组件 | 说明 |
|---|---|---|
| 选 WeCom | `ALLOWED_CHANNEL_TYPES` 含 `"wecom"` | 受控集合，已有 |
| 配置（Corp ID / Agent ID / Secret / Token / EncodingAESKey） | `POST /api/channels` `{name, type="wecom", config{...}}` | 平台凭据住 `Channel.config` |
| 绑定 Agent | `POST /api/connections` `{channel_id, agent_id, config{ACP 凭据}}` | 复用 Connect Agent 路径 |
| Validate | 复用 `WeComAdapter` 的 Fail-Closed 材料检查（`_material`/`_outbound_material` 缺料即抛） | 可作为 dry-run 校验端点，不新造 |
| 入站 | `POST /api/channels/{id}/wecom/webhook` | 既有 runtime intake |
| 出站 | `WeComAdapter.deliver`（已实现双端口） | 既有 |

「Channel Ready」UX 状态 = Channel 存在 + `config` 含必需键（adapter Fail-Closed 材料
检查通过）+ `status="active"`；**不新增持久化的 ready-state**（F5）。

Long Connection 当引入时，复用同一 `Create Channel` + 一个 transport 选择字段；webhook
端点已存在，无需新端点。

### 护栏

- `test_connect_channel_reuses_existing_endpoints_and_adapter`：`"wecom" in
  ALLOWED_CHANNEL_TYPES`；无 `WeComChannel`/`CloudWeCom` 类；runtime 含
  `/{channel_id}/wecom/webhook`。

---

## 10. G9 禁止第二套 Integration / Channel / Tenant / Health 模型

### 裁决

**禁止任何 `WeComIntegration` / `ChannelIntegration` / `WeComConnector` / `CloudChannel` /
`WeComChannel` / `ChannelHealthState` / `OnboardingState` / `WeComConnection` /
`CloudConnector` / `SecretManager` / `CredentialVault` 等第二套模型类。**

WeCom 仅是既有 `Channel(type="wecom")` + 既有 `WeComAdapter`（实现 `InboundChannelAdapter`
+ `ChannelAdapter` 双端口）+ 既有 `Connection` 的一个实例。新增任何「WeCom 专属」类都是
对 ACP Core 的破坏。

### 护栏

- `test_no_second_channel_or_integration_model`：对全 `app` tokenize 后扫描类名，
  确认无上述越界类（docstring 误报已被 AST 排除）。

---

## 11. 最终结构（与用户给定目标一致，且已被现有代码兑现）

```
                 Browser
                    │
                    ▼
          Browser Authentication (C.1, L1+L2)
                    │
                    ▼
            AuthenticatedUser(user_id, tenant_id)
                    │
                    ▼
              TenantScope(tenant_id)            ← 复用 B，唯一 tenant 原语
                    │
          ┌─────────┴─────────┐
          ▼                   ▼
      Workspace            Resources (tenant-scoped)
      (UX only)           Channel / Connection / Agent
                              │
            ┌─────────────────┼─────────────────┐
            ▼                 ▼                 ▼
          Channel           Connection ◄── ACP 凭据 ──┐
        (入口身份/配置)     (Channel↔Agent       │
        type="wecom"        访问路径)            │
        config{             channel_id+agent_id │
          WeCom 平台凭据 ←── 住这里（G2）        │
          token/encoding_aes_key/               │
          corp_id/corpsecret/agentid}           │
                              │                 │
                              ▼                 ▼
                        WeComAdapter ◄─ 只从 Channel.config 取平台凭据
                          (双端口)            ACPAdapter ◄─ 只从 Connection 取 ACP 凭据
                              │                 │
                              ▼                 ▼
                         InboundInteraction → Runtime → Interaction
                                                    (processing_status /
                                                     delivery_status = 事实源)
                              │
                    WeCom webhook (外部回调)
                    channel_id = 路由定位（非认证/非 tenant）
                    签名校验后 → Channel.tenant_id → 重绑 UoW (F2)
```

---

## 12. 机器化护栏（Verification Gate）

新增 `tests/test_cloud_0_1_3_connect_channel_architecture_gate.py`（**静态护栏，当前 17
项全绿**，锁死 G1–G9）：

1. `test_channel_domain_is_identity_config_not_connection`：G1 —— 域字段恰为
   `{name,type,id,status,config}`；无 `endpoint`/`credential_ref`/绑定。
2. `test_channel_model_has_no_plaintext_secret_column`：G1/G2 —— `channels` 表无明文
   凭据列（仅 `config` + `tenant_id`）。
3. `test_wecom_credentials_live_in_channel_config_not_connection`：G2 —— `WeComAdapter`
   从 `channel.config` 取 `token`/`encoding_aes_key`/`corp_id`/`corpsecret`/`agentid`，
   不引用 `connection.credential_ref`/`connection.config`。
4. `test_wecom_credentials_are_redacted_on_channel_config_read`：G2 ——
   `sanitize_channel_config` 脱敏敏感键、保留非敏感键。
5. `test_channel_has_no_credential_ref_field`：G2 —— Channel 域与 `ChannelResponse`
   均无 `credential_ref`。
6. `test_connection_holds_acp_path_credential_not_wecom_platform_credential`：G2/G3 ——
   Connection 含绑定、不含平台级 WeCom 凭据字段；响应不含 `bearer_token` 明文。
7. `test_channel_and_connection_are_separate_entities`：G3 —— 两实体独立，Channel 无绑定。
8. `test_connection_create_requires_pre_existing_channel_and_agent`：G3 —— 创建依赖已
   存在 Channel/Agent，做 UNIQUE 预检。
9. `test_webhook_transport_is_not_a_channel_status_axis`：G4 —— Channel 域无 transport
   连接态字段、无 `endpoint`。
10. `test_channel_status_default_is_active_no_runtime_connectivity`：G4/G5 —— `status`
    默认 `"active"`，入站只当启用开关。
11. `test_channel_has_no_health_subsystem`：G5 —— 无 `health` 字段、无 `ChannelHealthState`。
12. `test_interaction_outcome_lives_on_interaction_not_channel`：G5 —— 结果轴在
    Interaction 上。
13. `test_wecom_auth_isolated_from_browser_auth`：G6 —— WeComAdapter 不 import
    browser_auth / app.api.auth。
14. `test_webhook_channel_id_is_routing_only_not_auth_or_tenant`：G7 —— `channel_id` 仅
    定位，认证委托 adapter。
15. `test_tenant_resolution_derived_from_validated_channel_not_url`：G7 —— `TenantResolver`
    契约锁死「绝不取自 URL」「校验签名后的 Channel.tenant_id」。
16. `test_connect_channel_reuses_existing_endpoints_and_adapter`：G8 —— `wecom` 在受控
    集合、无专属类、既有 webhook 端点存在。
17. `test_no_second_channel_or_integration_model`：G9 —— 全 app 无第二套模型类。

---

## 13. 已知偏差与 follow-up（均不阻塞 GO）

| # | 偏差 / 缺口 | 影响 | 修复（实现期） |
|---|---|---|---|
| F1 | `Channel.status` 无 `active/inactive` 白名单（当前自由字符串，入站只查 `!= "active"`） | 比 Connection 弱一档，存在被写入 `connected/healthy` 等运行态的隐患 | 实现期加 `CHANNEL_MANAGEMENT_STATUSES == {"active","inactive"}` + schema 校验（镜像 Connection 11.11），防污染 |
| F2 | **多租户 webhook tenant 解析缺口**：`main.py` 硬绑 `TENANT_LOCAL`；`Channel` 域缺 `tenant_id` | 当前 OSS 单租户无碍；多租户 Cloud 下 webhook 会错用 TENANT_LOCAL，破坏隔离 | 实现期：定位→验签→读 `Channel.tenant_id`→重绑 UoW；给 `Channel` 域补 `tenant_id`（来自 ORM），让「已校验对象」能携带 tenant |
| F3 | Long Connection 未实现（仅 Webhook） | 当前 V0 仅 Webhook transport | 引入时作为新 Adapter 注册，按 `type`/transport 字段路由，不新增 `Channel.status` 轴 |
| F4 | WeCom 平台凭据暂存 `Channel.config`（与 Connection 同一纪律），未 `credential_ref` 化 | 满足 V0；生产化需 Vault 集成 | 作为独立 Gate 与 Connection 侧**统一**演进，不提前造 `Channel.credential_ref` |
| F5 | 「Channel Ready」无持久化状态 | UX 需表达就绪 | = Channel 存在 + config 含必需键 + `status="active"`；不新增 ready-state 列 |

F2 是本次 Gate 最重要的实现期工作项，但它**不阻塞 GO**——架构原则（tenant 来自签名校验
后的 Channel，绝不来自 URL）已通过 `TenantResolver` 契约与护栏锁死，缺口只是落地细节。

---

## 14. NOT TOUCHED（冻结边界，与 B/C/0.1.2/0.1.2.2 一致并扩展）

| 类别 | 文件 / 范围 | 理由 |
|---|---|---|
| **Runtime 全树** | `app/application/*`（IdentityResolver/SessionResolver/Router/AgentDispatcher/DeliveryService/Worker/Driver）、`app/adapters/*`、`agent_runtime_sdk/**` | 经 tenant-scoped UoW 获得隔离，逻辑与签名不变；`WeComAdapter` 仅从 Channel 取平台凭据，从 Connection 不取 |
| **Inbound/Identity/Session/Router/Dispatch/Delivery** | `app/application/channel_inbound.py` 及其调用链 | ACK 边界（12.9.4.6）已冻结，本次不改 |
| **Agent / Connection / ACPAdapter** | `app/domain/{agent,connection}.py`、`app/adapters/agents/acp.py` | 0.1.2.2 已裁决：ACP 凭据在 Connection，本次不回退 |
| **Tenant Isolation** | `app/core/tenant.py`、`app/persistence/unit_of_work.py`、tenant 迁移 | 复用 B，仅 F2 实现期在 webhook 组合根补 tenant 重绑 |
| **Browser Authentication** | `app/core/browser_auth.py`、`app/application/browser_auth.py`、`app/api/auth.py` | 复用 C/C.1，与 WeCom Auth 绝不合并 |
| **8 Core Table + tenants + channels + connections 现有列面** | 已建 schema | 本 Gate **无 schema 变更**（不新增 secret/health/ready 列，G2/G5/F5） |
| **config 脱敏纪律** | `app/schemas/{channels,connections}.py` 的 `sanitize_*` | 沿用既有，不重写 |
| **禁止第二套模型** | 全 `app` | G9 机器化锁死 |

---

## 15. GO / 边界闭合

**GO。** 9 个待裁决问题全部给出最小安全边界，且能证明：

- **G1** Channel 是入口身份/配置实体（`name/type/status/config`），`config` 为唯一凭据容器。
- **G2** WeCom 平台凭据归 `Channel.config`（由现有 `WeComAdapter` 写死，语义正交于
  Connection 的 ACP 凭据）；不复制到 Connection，不造 `Channel.credential_ref`。
- **G3** Channel ↔ Connection ↔ Agent 三实体独立；WeCom = `Channel(type="wecom")` +
  `Connection(channel_id, agent_id)` 接入 `Agent(type="acp")`。
- **G4** Webhook/Long Connection 是 transport，不污染 `Channel.status`；webhook 端点是
  runtime intake，不在域状态。
- **G5** 三轴分离；V0 不需要 Channel Health subsystem。
- **G6** WeCom Auth 与 Browser Auth 是两个信任边界，绝不合并。
- **G7** `channel_id` 仅路由；tenant 来自签名校验后的 `Channel.tenant_id`（契约已锁，
  实现缺口 F2 不阻塞 GO）。
- **G8** Connect Channel 完全复用既有端点/Adapter，零新增。
- **G9** 无第二套 Integration/Channel/Tenant/Health 模型（机器化扫描确认）。

机器化护栏（§12）当前 17 项全绿，锁死 G1–G9 与「不建第二套模型」。5 个 follow-up
（F1–F5）已列入，均**不阻塞 GO**——它们不改变安全边界，仅补齐管理态白名单、多租户
webhook tenant 解析、transport 扩展、凭据 Vault 化与就绪态表达。

**本 Gate 核心问题之裁决**：一个 Cloud Tenant 安全地拥有一个 WeCom Channel 的方式是——
把 WeCom **平台凭据**放入 tenant-scoped 的 `Channel.config`，通过既有 `Connection`
（承载 ACP 侧访问凭据）把该 Channel 绑定到既有 `Agent(type="acp")`，由既有
`WeComAdapter` + Runtime 管线完成入站验签与出站投递；tenant 隔离在 webhook 路径上由
「签名校验后的 `Channel.tenant_id`」保证。此路径薄薄地建立在现有 ACP Core 上，与
Connect Agent Golden Path 同构。
