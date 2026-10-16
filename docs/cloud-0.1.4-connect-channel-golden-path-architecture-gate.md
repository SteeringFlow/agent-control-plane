# Cloud-0.1.4 — Connect Channel Golden Path · Architecture Gate

> 阶段性质：**仅审计，不实现**。目标是在进入 Connect Channel UI 之前，把「用户把一个
> WeCom Channel 接入自己 Workspace 并最终 First Success」这条 Golden Path 的产品流程与
> 现有 Domain / Runtime 边界锁死。
>
> 结论先行：**Gate PASS，Backend = 0 changes。** 现有 ACP Core 的
> `Channel + Connection + Agent` 三个实体已足以表达该 Golden Path，无需新增任何后端
> API、实体、状态体系或认证命名空间。

---

## 一、Golden Path 与现有边界

最终用户路径（Agent 部分已完成，本阶段只解决 Channel）：

```
Sign Up            →  Tenant 创建（0.1.2.1，注册即建租户 = Workspace）
Workspace          →  Tenant 的产品化表达（无 Workspace 实体，0.1.2）
Connect Agent      →  POST /api/agents + POST /api/connections（0.1.2.2，已完成）
Connect Channel    →  POST /api/channels → 选已有 Agent → POST /api/connections → Probe
Send WeCom message →  GET|POST /api/channels/{channel_id}/wecom/webhook
Agent Response     →  Runtime 同 0.1.3 已闭环
First Success      →  由 Interaction(processing=SUCCEEDED, delivery=DELIVERED) 推导
```

真实数据关系（Channel 不引入 Workspace 中间实体）：

```
Tenant
 ├── Agent
 ├── Connection   (channel_id ↔ agent_id 的受控 Access Path)
 └── Channel      (tenant_id 为唯一归属事实)
```

---

## 二、Architecture Gate DoD

| 裁决 | 结论 | 依据 |
|------|------|------|
| **G1**  Channel 所有权 = `tenant_id` | **PASS** | `ChannelModel.tenant_id` 是唯一归属列；Channel **Domain 类刻意 tenant-blind**（0.1.3.2 Gate Q10），无 `workspace_id`/`user_id`；隔离经 tenant-scoped UoW 落地 |
| **G2**  WeCom 凭证归属 `Channel.config` | **PASS** | `WeComAdapter._material` / `_outbound_material` 只从 `Channel.config` 读 `token/encoding_aes_key/corp_id/corpsecret/agentid`；`Connection.config` 只住 ACP access path（`bearer_token` 等）；二者语义正交，无「Integration Configuration」合并 |
| **G3**  Channel / Connection 是两个事实 | **PASS** | 无聚合端点 `POST /api/integrations` / `POST /api/workspace/connect`；两条独立端点 + 独立 Service，前端 orchestration |
| **G4**  Connect Channel UI 选 Agent | **PASS** | `Connection(channel_id, agent_id)` 自带绑定，无需新增绑定实体；`create_connection` 已校验 Channel 与 Agent 存在性，UI「选已有 Agent」在 Domain 层天然成立 |
| **G5**  Webhook URL 中 `channel_id` 仅路由 | **PASS** | runtime `/{channel_id}/wecom/webhook`；`channel_id` 仅 locator，认证由 `verify_handshake`（WeCom 签名）承担，绝不从 URL 推 tenant/认证 |
| **G6**  Secret 显示规则 | **PASS** | `sanitize_channel_config` 对 `token/encoding_aes_key/corpsecret` 一律脱敏（响应模型强制）；非敏感键 `corp_id/agentid/api_base/timeout_s` 管理可见 |
| **G7**  Channel Health 不引入 | **PASS** | Channel 域/模型无 `health`/`status=connected`/`last_checked_at`；全 app 无 `ChannelHealthState` |
| **G8**  Channel「Connected」语义 | **PASS** | 不做 `Channel.connected` 持久状态；区分 `configured → WeCom probe → transient success` 与真实 First Success |
| **G9**  First Success 仍 Derived | **PASS** | 交互结果在 `Interaction.processing_status` / `delivery_status`；无 `onboarding_status`/`first_success_at`/`workspace_setup_state` 表或列 |
| **G10** 是否需要新后端 API | **PASS** | Golden Path 完全由既有端点支撑（见下），**Backend = 0 changes** |

### G10 端点映射（既有，零新增）

| Golden Path 步骤 | 端点 | 状态 |
|---|---|---|
| 创建 WeCom Channel | `POST /api/channels` | 既有（config 含平台凭据，响应脱敏） |
| 列出已有 Agent 供选择 | `GET /api/agents` | 既有 |
| 绑定 Channel↔Agent | `POST /api/connections` | 既有（existence-only 校验） |
| 即时 Probe | `GET /api/connections/{id}/health` | 既有（0.1.2.2.2.1 即时快照，生产组合根已注入 `acp_adapter.probe`） |
| 真实 WeCom 入站 | `GET|POST /api/channels/{channel_id}/wecom/webhook` | 既有（0.1.3 runtime） |

---

## 三、变更面汇总

```
Runtime semantic diff:   ZERO   （Runtime Domain 保持 tenant-blind，无新语义）
Domain changes:          ZERO   （Channel / Connection / Agent 域类不变）
New entities:            ZERO   （无 WorkspaceChannel / Integration / Bot ...）
New endpoints:           ZERO   （无聚合 / 专用端点）
Auth boundary changes:   ZERO   （WeCom 验签 与 Browser Auth 仍是两个独立信任边界）
Tenant isolation changes: ZERO  （tenant 仍经 tenant-scoped UoW 注入，未进 Domain）
```

### 生产代码变更

**无。** 本 Gate 不要求任何后端实现变更。Connect Channel UI 阶段只需在前端做
orchestration（顺序调用上述既有端点），不应推动后端新增 API / 实体 / 状态。

---

## 四、机器化护栏（machine guards）

测试文件：`backend/tests/test_cloud_0_1_4_connect_channel_golden_path_gate.py`

```
16 passed in 0.61s
```

覆盖：G1（tenant 归属 + 无 workspace_id/user_id）、G2（WeCom 凭证只读 Channel.config /
config 语义正交）、G3（无聚合端点 + 两独立 Service）、G4（Connection 自带绑定 / 存在性
预检）、G5（webhook channel_id 仅路由）、G6（secret 读时脱敏）、G7+G8（无 health/
connected 状态体系）、G9（First Success 由 Interaction 推导 / 无禁止列）、G10（Golden
Path 全用既有端点 + Probe 已装配）、以及总闸（无禁止类 + Runtime tenant-blind）。

---

## 五、明确禁止事项（forbidden）

```
❌ Integration entity
❌ WorkspaceChannel entity
❌ WorkspaceAgent entity
❌ OAuth platform
❌ Channel health subsystem
❌ onboarding state machine
❌ FirstSuccess persistence
❌ Runtime tenant_id
❌ Runtime fork
❌ new TenantResolver
❌ new authentication namespace
```

护栏已通过 AST 扫描 `app/` 全部类与 ORM 列，确认上述禁止项**均不存在**。

---

## 六、给下一步（Connect Channel UI）的约束

1. UI 只做 orchestration，禁止要求后端新增聚合端点；
2. Channel 创建与 Connection 创建分两步，顺序依赖 Channel 先存在；
3. Agent 来自 `GET /api/agents`（已有 Agent 选择），不新建 Agent 绑定实体；
4. Webhook URL 展示时仅标注 `channel_id` 为路由定位，不得暗示 URL 即安全凭证；
5. 展示当前配置时，secret 字段只显示脱敏占位符（`********`），前端不得缓存明文；
6. 「Connected」仅来自 `GET /api/connections/{id}/health` 的 transient 快照或
   First Success 推导，不得把任何持久 `channel.connected` 状态呈现给用户；
7. First Success 由真实 Interaction 结果推导，UI 不写入任何 onboarding/first_success 表。
