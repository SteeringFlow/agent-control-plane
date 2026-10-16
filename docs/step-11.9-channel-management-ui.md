# Step 11.9 — Channel Management UI

> **严格对齐 11.5（Agent UI）的第二条 Management UX Vertical Slice。**
> 顺序裁决（用户 11.9）：先 Channel UI、后 Connection——因为 Connection 的
> API/UI 需要引用 Channel，而 Channel 的 4 条管理 API（11.8）已经就绪，
> 现在就能把它变成管理员真正可用的管理能力。
>
> 交付物 = `frontend/channels.html` + `frontend/channels.js`（复用 styles.css）
> + Agent 页 1 处导航入口 + 本报告 + 语义守护测试（11 项）。

---

## 1. 范围（严格按用户 11.9 指令）

```
Channel Management
├── Channel List        Name / Type / Status / Actions（编辑）+ 刷新 / 新建
└── Channel Form        Name / Type / Status / Config
```

用户能力闭环：**查看 → 创建 → 修改 → 重新查看**。

**不做（锁死）**：Channel Registry / Plugin Marketplace / Secret Vault /
Credential Management Center / Health Check / Connection UI / Routing UI /
Webhook 配置中心 / 多租户 / RBAC / Network Graph。
（守护：`test_f_no_forbidden_features` 对 UI 源码逐词零出现断言。）

## 2. 技术形态

- 与 11.5 同一 vanilla 三件套形态、零构建链；Channel 页为**独立文件**
  （`channels.html` + `channels.js`），复用 `styles.css`——Agent 页与
  Channel 页互链（顶栏导航），互不耦合；
- 无新增后端改动：组合根零改动（11.5 的 StaticFiles mount 自动托管新页面），
  全部能力消费 11.8 的 `/api/channels`（GET/POST/PATCH）。

## 3. 冻结 UI 合同：Secret 哨兵（本步核心，区别于 Agent UI）

Channel 没有 `credential_ref` 字段——凭据住在 `Channel.config` 内，GET 返回的
敏感键已被 `sanitize_channel_config()` 脱敏为 `********`（11.8 合同 #1/#4）。

**`********` 表示「已配置」，不是真实 secret**：

```
GET /api/channels/1
{ "config": { "corp_id": "xxx", "token": "********",
              "encoding_aes_key": "********", "corpsecret": "********",
              "agentid": 100001, "api_base": "...", "timeout_s": 10 } }
```

UI 编辑时的三态语义：

| 用户行为 | UI 提交 | 后端（11.8 合同 #3） |
| --- | --- | --- |
| 不修改敏感键 | 值仍为 `********` 的键**自动从 PATCH config 中省略** | merge → **真实 secret 保留** |
| 只改非密字段（如 timeout_s） | `{"config": {"timeout_s": 15}}` | merge → secret 保留 |
| 主动轮换 | 直接把 `********` 改写为新值 → `{"config": {"corpsecret": "new-secret"}}` | merge → **覆盖** |

**为什么必须这样**：若 UI 把 `********` 当真实值回发，后端 merge 会把真实
secret **覆盖成占位符**——比泄漏更隐蔽的破坏（下一次出站交付 Fail Closed）。
因此 `channels.js` 的提交路径强制经过 `buildConfigPayload(isEdit)` 哨兵过滤：
`if (value === SECRET_PLACEHOLDER) continue;`。

**哨兵按值匹配、与键名无关**：UI 不预设敏感键集合（那是 schemas 层的冻结事实，
11.8）；换类型（feishu / ...）出现新敏感键时，UI 逻辑零改动。

## 4. 与 Agent UI 的语义差异（逐条成文）

| 维度 | Agent UI（11.5） | Channel UI（11.9） |
| --- | --- | --- |
| credential | `credential_ref` 写-only：**永不回显**（留空 = 保留） | config 内敏感键：**回填 `********` 标记**（可见「已配置」），未修改自动省略 = 保留 |
| type | 受控 `<select>`（仅 acp） | **自由文本输入**——刻意不做下拉/registry，等第二个 Adapter 出现再重新审计 |
| status | 双轴：表单仅 active/inactive，error 只展示 + 列表「启用」清障 | **纯管理态**：表单仅 active/inactive（Channel 无运行态 error 轴，11.6 Q4） |
| config | 不透明 JSON 整体替换 | 不透明 JSON + **merge 语义**（哨兵省略未改键） |
| List 列 | Name/Type/Endpoint/Status/Actions | Name/Type/Status/Actions（Channel 无 endpoint） |

## 5. 守护测试（`tests/test_step11_9_channel_management_ui.py`，11 项）

```
A1 文件存在              A2 可达 + mount 恒最后 + /api/channels 在册    A3 两页互链
B1 fetch 只访问 /api/channels            B2 无 DELETE/删除
C1 Secret 哨兵语义（省略未改占位键）      C2 回填脱敏值但绝不回发 ********
D1 status 仅管理态                       D2 type 自由文本（无 registry）
E3 config 不透明（JS 零预设键字面量）     F  不做清单零出现
```

既有守护翻转（事实推进，非放松）：
- `test_step11_7.test_b2`：`channel UI missing` → `channels.html/channels.js 已落地`；
- `test_step11_6.test_c1`：`frontend 不应有 channel UI` → `11.9：应已有`。

## 6. 已知限制（记录，不偷渡）

- 若用户把某键的值**恰好**写成字面 `********`，编辑保存时会被哨兵省略
  （= 保留旧值）——占位符即哨兵的固有语义，已接受；
- config 无「删除单个键」语义（与 11.8 API 一致）；
- 无分页/过滤（与 11.3/11.8 同一裁决）；
- 渠道连通性测试（发送测试消息）属 Runtime 能力，不做。

## 7. 后续触发点（记录，不预建）

- 出现第二个真实 Channel Adapter → 由真实问题决定是否引入 Adapter Registry
  （届时的自然落点：type 校验收口 + UI 引导）；
- Connection Management（后端已完整）→ Channel 列表即将成为其
  channel_id 下拉数据源。
