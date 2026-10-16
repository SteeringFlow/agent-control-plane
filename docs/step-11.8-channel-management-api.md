# Step 11.8 — Channel Management API

> **严格一个 Vertical Slice：把已存在的 Channel 后端能力安全地暴露成稳定的
> HTTP Management API。** 镜像 11.3（Agent API），前置 = 11.7（Channel 审计）。
>
> 端点面（冻结）：
>
>     GET    /api/channels
>     POST   /api/channels
>     GET    /api/channels/{id}
>     PATCH  /api/channels/{id}
>
> **不做**：DELETE / Health Check / Webhook / Routing / Connection /
> Channel Registry / Credential Vault / Channel Secret 独立表。
>
> **Webhook 边界**：入站 webhook 已存在于 runtime 方向
> （`api/runtime/channels.py`，10.2.1）。本步**不**因做 Channel Management API
> 就把 inbound webhook 混进管理面——管理面（`api/management/channels.py`）与
> 运行时入站边界继续保持隔离。

---

## 1. 交付物

| 层 | 文件 | 内容 |
| --- | --- | --- |
| Repository Port | `repositories/channel.py` | + `list_channels()`（List 用例；仍无 delete） |
| Repository Adapter | `persistence/repositories/channel.py` | + `list_channels`（按 id 升序，MVP 无分页/过滤） |
| Application Service | `application/channels.py` | + `list_channels` 用例；+ `_require_dict_config`；+ PATCH 改名/重分类撞 (type,name) → `DuplicateChannelError`（self 除外）；**`_apply_change` 改为 config merge 语义** |
| API Schema | `schemas/channels.py`（新） | `ChannelCreate / ChannelUpdate / ChannelResponse / ChannelListResponse` + `sanitize_channel_config()` |
| HTTP API | `api/management/channels.py`（新） | `build_channels_api(service)`：4 路由 + Application 错误→状态码映射 |
| 组合根 | `main.py` | 装配 `build_channels_api(ChannelApplicationService(...))` |
| 验收测试 | `tests/test_step11_8_channel_management_api.py` | 30 项（真实 HTTP 探针 + AST 边界 + 生产装配） |

错误映射（与 11.3 同一裁决表）：

    DuplicateChannelError     → 409 Conflict
    ChannelNotFoundError      → 404 Not Found
    InvalidChannelInputError  → 422 Unprocessable Entity

## 2. 五条冻结合同（逐条落实）

### 合同 #1 — Response contract

响应字段面 == `{id, name, type, status, config}`（恰好 5 个，单/列表元素共用）。
`config` 一律经 **`sanitize_channel_config()`** 脱敏：

| 键 | 处理 |
| --- | --- |
| `token` / `encoding_aes_key` / `corpsecret` | → `********`（匹配大小写不敏感） |
| `corp_id` / `agentid` / `api_base` / `timeout_s` | 原样返回（管理可见） |

脱敏在 `ChannelResponse` 的 `field_validator` 层强制——GET / List / PATCH / POST
四条响应路径同源，**无法绕过**。敏感键集合的唯一事实来源 =
`app/adapters/channels/wecom.py` 真实读取键（11.7 §2.2）。

### 合同 #2 — Create contract

创建允许提交**完整 config**（首次配置需要录入 secret）：

- `POST /api/channels` 请求体：`{name, type, config?}`；
- secret 真实落库（DB 行可验证），响应侧脱敏回显；
- `config` 可省略（先建渠道、后补配置的管理路径合法）。

### 合同 #3 — Patch contract（本步最重要）

```
PATCH config
    ↓
load current config
    ↓
merge incoming config        ← _apply_change：{**(current.config or {}), **change.config}
    ↓
persist merged config
```

**不是** `incoming config → replace current config`。

- 提供的键覆写、未提供的键保留——管理员只 PATCH `{agentid: ...}` 时，
  `token / encoding_aes_key / corpsecret` 不会被清空（否则下一次出站交付即
  Fail Closed）；
- 密键也可**显式覆写**（secret 轮换合法路径）：PATCH `{"config": {"corpsecret": "new"}}`；
- 缺省 / 显式 `null` config = 不修改；空 body = noop 200；
- 该语义由 **Application Service 单一裁决**（`_apply_change`），API 层零重复。
  11.7 记录的「整段替换缺口」就此闭合（`test_step11_7` D3 冻结同步翻转）。

### 合同 #4 — Secret 不可通过 GET/List 泄漏（与 Agent 的明确区别）

```
Agent      credential_ref            → write-only（引用入、引用不出）
Channel    sensitive config keys     → read-sanitized（完整入、脱敏出）
```

Channel 没有 `credential_ref` 字段——凭据直接住在 `Channel.config` 内，
所以护栏从「字段面排除」变为「**config 键级脱敏**」（11.7 §2.3 裁决的落地）。
验证：Create/Update 后直接读 DB 行确认 secret 落库，而任何响应中密键恒为
`********`。

### 合同 #5 — type 继续保持 str

不趁机把 `wecom` 做成 Enum。当前真实事实不变：

```
Channel.type = str
        │
        └── 当前 Adapter 实现集合
                 └── wecom
```

Schema 无 Enum、API 无 `ALLOWED_`/`Literal[` 白名单；任意非空字符串 type 均可
创建（如 `feishu`——有 (type,name) 去重守卫，但无 Adapter 接管）。等真正出现
第二个 Channel Adapter、或 Adapter Registry 成为实际需求时再重新审计。

## 3. 边界守护（测试锁定）

- AST：API/Schema 层不 import SQLAlchemy / app.persistence；API 层无业务判断；
- 无 DELETE 端点（软下线 = PATCH status，DROP 裁决维持）；
- 生产装配：`/openapi.json` 暴露 4 条 `/api/channels` 路由，无 DELETE，
  且**不含** `/api/channels/{id}/wecom/webhook`（runtime webhook 不混入管理面）；
- 冻结守卫翻转（本步合法翻转，均已在测试中注明「11.8」）：
  `test_step5_7 / 6_1 / 6_5 / 6_7 / 7_1 / 7_3 / persistence_smoke` 的
  管理 API / schemas / Port 方法面集合；`test_step11_6` b1/c1；
  `test_step11_7` a3/b1/d3（缺口闭合）。

## 4. 已知限制（记录，不偷渡）

- name 去重（(type,name)）仍为**应用级守卫**，DB 无 UNIQUE——TOCTOU 窗口与
  Agent 11.3 同一事实，是否加 DB 约束等真实环境证明需要再迁移；
- config merge 无「删除单个键 / 清空 config」语义（本 MVP 不需要；出现真实
  管理需求再定义）;
- 无分页/过滤（与 11.3 list_agents 同一裁决）；
- Channel 响应不含 created_at/updated_at（domain 未声明，11.7 §2.2 附注；
  需要 UI 展示时随 11.9 Channel UI 一并补回显）。

## 5. 下一步

Step 11.9 — Channel Management UI（镜像 11.5 Agent UI：List + Form +
停用/启用 + 密钥只写不显）；之后 11.10 — Connection Management
（后端已完整，仅剩 API+UI）。
