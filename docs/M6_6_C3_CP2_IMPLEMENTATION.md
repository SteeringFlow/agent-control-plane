# M6.6-C3-CP2-IMPL — Inbound Trust Gate Hardening（Implementation Report）

> 性质：**实现 + 验证**（非只读）。
> 上游：`docs/M6_6_C3_CP2_INBOUND_TRUST_GATE_AUDIT.md`（只读审计：3 条未认证注入面）。
> 本步范围：**CP-2 ONLY**。未实现 Bootstrap / Arm / First Message Runtime Semantics（属 CP-3）。
>
> 最终 Gate：**PASS**（§10）。

---

## 1. Scope

### 1.1 目标（唯一）

确保**每一种 Channel 入站路径只能进入其拥有有效 Trust Model 的 Adapter**：

```
段 1 · Trust Gate（路由信任模型）   本 HTTP 入口是否服务该 channel.type？
段 2 · 渠道信任材料                 wecom 逐请求验签 / wecom_bot 订阅连接 / local 无认证
```

两段缺一不可；段 1 位置固定在 `_load_channel` 之后、`parse_message` 之前。

### 1.2 交付物

| # | 文件 | 类型 |
|---|---|---|
| 1 | `backend/app/application/channel_inbound.py` | 实现（Trust Gate 判定点） |
| 2 | `backend/app/api/runtime/channels.py` | 实现（路由声明信任模型） |
| 3 | `backend/tests/test_m6_6_c3_cp2_inbound_trust_gate.py` | 新增测试（12 格矩阵 + 源码 Guard） |
| 4 | `docs/M6_6_C3_CP2_IMPLEMENTATION.md` | 本文件 |

改动范围（`git diff --stat`）：`2 files changed, 124 insertions(+), 10 deletions(-)` + 新增测试文件。
**未触碰**：Identity / Grant 语义、Runtime、Session、Authorization、migration / DB、错误码、
认证机制、ADR-12、Package 2-C、Gate 2.6、`main.py`、`wecom.py`、`wecom_bot.py`、nginx 模板、
生产配置。

---

## 2. Root Cause

CP-2 审计 §7 结论（一句话）：

> **入站 HTTP 路由没有声明自己服务哪种信任模型。**
> `MultiChannelAdapter._for_channel_type(channel_type)` 是唯一分派依据，而
> `wecom_bot` 与 `local` 的 Adapter **不做逐请求验签**——于是「无验签模型」的渠道
> 被塞进了「有验签模型」的入口（反向亦然）。

结构证据（修复前）：

```python
# app/adapters/channels/registry.py:53-54（修复前后均未改动）
def _for_channel_type(self, channel_type):
    return self._adapters.get(channel_type)      # ← 唯一分派依据
```

三条未认证注入面（修复前，`channel_id` 只需是整数主键）：

```
POST /api/channels/{id}/wecom/webhook  + wecom_bot 渠道   → 接受 aibot_msg_callback（无验签）
POST /api/channels/{id}/local/message  + wecom_bot 渠道   → 同上
POST /api/channels/{id}/wecom/webhook  + local      渠道  → 接受 local wire（无验签）
```

`aibotid` 归属校验（`wecom_bot.py:604-608`）三重短路，**省略 `aibotid` 即可稳定绕过**——
审计 §7.3 已证明它不是可依赖的信任判据。

**为何修复不能落在 Adapter**：WS 合法路径（`main.py::_on_bot_frame`）与 HTTP 注入路径
调用**同一个** `parse_message`、**同一组空参数**，Adapter 无法区分二者。
⇒ 修复必须落在 HTTP 路由边界（本步据此实施）。

---

## 3. Changes

### 3.1 `app/application/channel_inbound.py`（段 1 判定点）

1. 新增私有判定（位置：`_load_channel` 之后、租户解析之前）：

```python
def _assert_route_trust(self, channel, expected_channel_type) -> None:
    if expected_channel_type is None:
        return
    if channel.type != expected_channel_type:
        raise InboundSignatureError(...)      # → HTTP 403（既有错误码）
```

2. `handle_handshake` / `accept_message` / `handle_message` 新增
   `expected_channel_type: str | None = None`，并在
   `_load_channel` **之后**、Adapter 调用**之前**执行判定。
3. 模块 docstring 新增「Inbound Trust Gate（两段式）」小节（含
   「不得下移至 Adapter」「不得搬进 Bootstrap」两条纪律）。

**设计裁决**：

- **用 `InboundSignatureError`（403）**：请求形状合法、服务端配置合法，唯一问题是
  「本请求在本入口不可认证」——与「坏签名」同语义类。**不新造错误码**（Package 2-C 纪律）。
- **默认 `None` = 不施加该段**：WS 长连接与既有调用方（含全部既有测试替身）
  行为与隔离前逐字节一致 —— 这是「合法 WS 路径不被破坏」的结构性保证。

### 3.2 `app/api/runtime/channels.py`（信任模型声明处）

1. 新增路由层常量（**唯一事实源**）：

```python
WECOM_WEBHOOK_CHANNEL_TYPE = "wecom"     # 信任模型 = 每请求验签
LOCAL_MESSAGE_CHANNEL_TYPE = "local"     # 信任模型 = 无认证（开发渠道语义）
```

2. 三条路由全部显式传参：

| 路由 | 传入 | 未匹配时 |
|---|---|---|
| `GET  /{id}/wecom/webhook` | `WECOM_WEBHOOK_CHANNEL_TYPE` | 403（`wecom_bot` / `local`） |
| `POST /{id}/wecom/webhook` | `WECOM_WEBHOOK_CHANNEL_TYPE` | 403（`wecom_bot` / `local`） |
| `POST /{id}/local/message` | `LOCAL_MESSAGE_CHANNEL_TYPE` | 403（`wecom` / `wecom_bot`） |

3. 模块 docstring 新增 CP-2 小节（说明为何必须由路由声明、为何不能由分派器决定）。

### 3.3 未做但值得记录的一处「改写而非改测」决策

`tests/test_step11_7_channel_management_vertical_slice_audit.py::test_c3` 用**原始字符串**
断言 `"channel.type" not in <路由源码>`（它守的是「路由不按类型分派」这一真意图）。
本步初稿的 docstring 含该字面量 → 该用例转红。

**选择：改写本步 docstring 的措辞**（改为「按**渠道类型字段**自行选择 Adapter」），
**而不是修改 11.7 的用例** —— 因为 `main.py` 与其它步骤的测试都不在本轮允许修改清单内，
且 11.7 的断言意图（路由不做类型分派）在新实现下**依然成立**（见 §8 I-6 的 AST Guard）。
建议后续把该断言改为 AST 判定（§9 D-5）。

### 3.4 一处既有行为的收紧（显式登记）

| 格 | 修复前 | 修复后 | 判定 |
|---|---|---|---|
| `GET /wecom/webhook` × `local` | 200（原样回显 `echostr`） | **403** | 收紧。审计 §7.1 已标记该格为「语义错位（无副作用）」；`local` 渠道不存在 WeCom 握手语义 ⇒ 拒绝 |
| `GET /wecom/webhook` × `wecom_bot` | 500（Adapter 抛 `InboundChannelConfigError`） | **403** | 收紧且语义更准（「本入口不可认证」≠「服务端配置错」）。两者均 Fail Closed，无安全差异 |

两格均无既有测试依赖（全量回归 0 失败实证）；如需保留旧的 500，需显式裁决（§9 D-3）。

---

## 4. Trust Gate Matrix

12 格 = 4 入口 × 3 渠道类型（单一事实源 = 测试内 `ROUTE_MATRIX`）。

| # | 入口 | `channel.type` | 期望状态 | `parse_message`/握手运行？ | 产生 Interaction？ | 结果 |
|---|---|---|---|---|---|---|
| 1 | `GET /{id}/wecom/webhook` | `wecom` | 200（回显明文） | 握手 ✔ | ✘ | ✅ |
| 2 | `GET /{id}/wecom/webhook` | `wecom_bot` | **403** | ✘（段 1 拒绝） | ✘ | ✅ |
| 3 | `GET /{id}/wecom/webhook` | `local` | **403** | ✘（段 1 拒绝） | ✘ | ✅ |
| 4 | `POST /{id}/wecom/webhook` | `wecom` | 200（密文+验签） | ✔ | ✔ | ✅ |
| 5 | `POST /{id}/wecom/webhook` | `wecom_bot` | **403** | ✘（段 1 拒绝） | ✘ | ✅ |
| 6 | `POST /{id}/wecom/webhook` | `local` | **403** | ✘（段 1 拒绝） | ✘ | ✅ |
| 7 | `POST /{id}/local/message` | `local` | 200（ACK） | ✔ | ✔ | ✅ |
| 8 | `POST /{id}/local/message` | `wecom` | **403** | ✘（段 1 拒绝） | ✘ | ✅ |
| 9 | `POST /{id}/local/message` | `wecom_bot` | **403** | ✘（段 1 拒绝） | ✘ | ✅ |
| 10 | WS `aibot_msg_callback` | `wecom_bot` | 已接纳（无 HTTP 状态） | ✔ | ✔ | ✅ |
| 11 | WS `aibot_msg_callback` | `wecom` | Fail Closed（段 2：`InboundFormatError`） | ✔ | ✘ | ✅ |
| 12 | WS `aibot_msg_callback` | `local` | **结构不可达**（discovery 只枚举 `wecom_bot`） | — | ✘ | ✅ |

要点：

- **第 5/9 格是本步的核心安全目标**（`wecom_bot` HTTP POST）：两条 HTTP 入口均 403，
  `parse_message` **未被调用**（spy 观测），Interaction / Identity / Grant 增量全 0。
- 第 11 格说明「段 1 通过但段 2 拒绝」与「段 1 拒绝」是**不同机制**：前者 Adapter 被调用
  （spy 记录 `wecom`），后者 Adapter 从未被触及。
- 第 12 格「不可达」不是断言省略：它由 `main.py::discover_bot_connections` 的
  `type != wecom_bot → continue` 过滤保证，并由 §8 I-7 的源码 Guard 锁定。
- 第 4/7/10 格同时断言**全链真实跑通**（Agent + Delivery + 1 条 Interaction），
  证明合法路径未被收紧（不变量 2/3/4）。

---

## 5. Test Results

新文件：`backend/tests/test_m6_6_c3_cp2_inbound_trust_gate.py`。

```
cd backend
python -m pytest tests/test_m6_6_c3_cp2_inbound_trust_gate.py -q
→ 20 passed
```

### 5.1 用例分组

| 组 | 用例 | 断言要点 |
|---|---|---|
| §A 矩阵 GET 行 | `test_a_get_wecom_webhook_matrix[wecom\|wecom_bot\|local]` | 200/403/403；握手调用集合；边界矩阵一致 |
| §B 矩阵 POST 行 | `test_b_post_routes_matrix[...×6]` | 200/403；`parse_message` 调用集合；Interaction 增量；Identity/Grant **零增量** |
| §C 矩阵 WS 行 | `test_c_ws_bot_callback_matrix[...×3]` | 合法 WS 全链不变；误绑定 Fail Closed；不可达格由源码保证 |
| §D 源码 Guard | `test_d1` ~ `test_d6` | 见 §8 |
| §E Bootstrap 顺序 | `test_e1` / `test_e2` | 「Arm 有效」语境下仍拒绝且授权写入为 0；仅用既有错误码 |

### 5.2 关键断言（原样摘录语义）

- **负例（第 5/9 格）**：
  `resp.status_code == 403` ∧ `spy.parse_calls == []` ∧ `counts == before`
  （`interactions` / `identities` / `connection_conversations` **三者全等**）。
- **§E-1「即使 Arm 有效」**：以测试局部集合 `ARMED_CHANNELS` 标记该渠道处于 arm 窗口，
  随后两条 HTTP 入口各注入一次 → 双 403、`parse_calls == []`、
  `identities` / `grants` / `interactions` 增量全 0。
  本轮**不实现 Arm**（生产代码不读取该集合）——本用例钉的是「顺序不变量」：
  若未来有人在 Trust Gate 之前插入授权写入，本用例转红。
- **§E-2**：拒绝走既有 `InboundSignatureError`（文案含「不经本入站入口接入」），
  未声明类型时行为与隔离前一致（仍能正常接纳）。

---

## 6. Regression Results

### 6.1 全量回归

```
cd backend
python -m pytest tests -q --basetemp=.pytest_tmp_run
→ 2266 passed, 33 skipped, 0 failed, 0 errors in 146.58s
```

### 6.2 环境噪声（已定性，非回归）

首次全量执行（不带 `--basetemp`）出现 `31 errors`，原因：

```
PermissionError: [WinError 5] 拒绝访问:
  C:\Users\<user>\AppData\Local\Temp\pytest-of-<user>
（pytest tmpdir basetemp 目录不可用）
```

即**宿主 Windows 临时目录权限问题**，与被测代码无关。证据：

- 以工作区内 `--basetemp` 复跑同批文件 → `24 passed`（原 31 errors 全部通过）；
- 全量复跑 → **0 error**；
- 33 skipped 均为需要真实凭据/环境的门控用例（如 `ACP_REAL_WECOM_*` 未配置、PG 不可达等），
  与 CP-2 无关。

### 6.3 重点回归面（用户 §8 四类）

| 类别 | 文件 | 结果 |
|---|---|---|
| WeCom webhook | `test_step10_2_1_wecom_webhook.py`、`test_step10_2_2_outbound_e2e.py`、`test_step12_6_runtime_production_composition.py`、`test_step12_9_4_6/9/17`、`test_step13_1`、`test_cloud_0_1_3_3`、`test_cloud_0_1_3_1b` | ✅ |
| WeCom Bot 长连接 | `test_3b_2_2_wecom_bot_websocket_transport.py`（含 `test_c2b`「签名参数不影响 parse_message」——**保持绿**）、`test_3b_3_3_wecom_bot_group_chat.py` | ✅ |
| local | `test_step15_2_3_local_first_success.py`（24 passed） | ✅ |
| authorization / runtime | `test_adr_authorization_and_access_model.py`、`test_step11_7_channel_management_vertical_slice_audit.py`、`test_step9_*`、`test_step10_2_*` | ✅ |

> `test_3b_2_2::test_c2b` 保持绿是关键证据：本步**没有**去改 Adapter 的「三参数无意义」
> 语义（那是 WS 路径的正当容错），而是把判定上移一层。

---

## 7. Production V-3 Verification

> **全部为只读核对**：未修改任何生产配置/数据，未执行写入型请求（探测仅用 GET；
> 未做任何 POST 注入探测）。凭据一律不入档。

### 7.1 拓扑与容器

```
主机      <prod-host>（生产主机，地址不入公开档案）
容器      repo-backend-1   Up 7 hours (healthy)   8000/tcp（未发布到宿主）
          repo-nginx-1     Up 11 hours            0.0.0.0:80, 0.0.0.0:443
          repo-postgres-1  Up 5 days (healthy)   5432/tcp
```

### 7.2 生效的 nginx 配置 = 仓库模板（**一致，已验证**）

| 文件 | MD5 |
|---|---|
| 工作区 `_deploy/nginx/acp-pilot-vhost.conf.template` | `06EA35B1F4A28ADBD9FE91FCBE9FC559` |
| 生产 `<repo-root>/_deploy/nginx/acp-pilot-vhost.conf.template` | `06ea35b1f4a28adbd9fe91fcbe9fc559` |

**逐字节相同。** 且生产**实际加载**的
`<repo-root>/_deploy/nginx/runtime/rendered/acp-pilot-vhost.conf`
（挂载至容器 `/etc/nginx/conf.d`，与 `website-vhost.conf` 并列）其 location 结构
与模板**逐行一致**：

```
48: location /                                       → HTTP 301
66: location = /health                               → return 404
73: location = /                                     → return 302 /workspace.html
79: location ~ ^/api/channels/[0-9]+/wecom/webhook$  → PUBLIC 直通（无 auth_basic）
90: location ~ ^/(docs|redoc|openapi\.json)$         → Operational BasicAuth
107: location /                                      → 前缀兜底：公开代理（授权归应用层）
```

**结论：repository template 与 production effective ingress 一致。**

### 7.3 实际暴露的应用路由（只读 GET 探测，HTTPS 入口）

| 探测 | 结果 | 解读 |
|---|---|---|
| `GET /api/channels/1/wecom/webhook` | **422** | 路由命中、公网可达、无边缘鉴权（缺必需 query 参数 → 参数校验先于渠道查找）——**与设计一致**（WeCom 必须能回调） |
| `GET /api/channels/999999/wecom/webhook` | **422** | 同上（进一步证明 422 发生在渠道查找之前） |
| `GET /api/interactions` | **401** | 管理/观测面：应用层 session 鉴权生效 |
| `GET /api/channels/1/local/message` | **404** | 该路径只注册 POST；GET 落到 `app.mount("/", StaticFiles(html=True))` 静态兜底 → 文件不存在 → 404。**不用于任何安全判定** |

生产路由表（只读源码核对，`<repo-root>/backend/app/api/runtime/channels.py`）：

```
 86:    @router.get("/{channel_id}/wecom/webhook")
105:    @router.post("/{channel_id}/wecom/webhook")
137:    @router.post("/{channel_id}/local/message")
```

且 `grep Depends` **零命中**、无 `expected_channel_type`（即线上仍是 **CP-2 之前**的代码）
——与工作区修复前的源码形状一致。`main.py:384` 装配入站 router、`main.py:441`
`app.mount("/", StaticFiles(...))`（后者解释了上表的 404）。

**推定（配置 + 路由表共同确定，非注入探测）**：
`POST /api/channels/{id}/local/message` 在 nginx 归入 `location /`（公开代理），
且应用层无 session 鉴权依赖 ⇒ **该 POST 面在公网可达且无认证**。
其安全性因此**完全依赖「production 不存在 local 渠道」**（见 7.4）。登记为 §9 D-1。

### 7.4 生产数据现状（只读 SELECT）

```
channels = 0 ；identities = 0 ；connection_conversations = 0 ；interactions = 0
（库 acp；连接串中的口令不入档）
```

⇒ **当前不存在任何 `local` 渠道**，D-1 的残余暴露**未激活**；
同时 ⇒ 即使线上仍是 CP-2 前的代码，三条旁路**今天也无可注入对象**
（任意注入都停在 `channel not found` → 500，不产生任何写入）。

### 7.5 部署版本

```
GET /api/version → {"version":"0.1.0-rc.6","commit":"6445b26b40ae8e18a624a7d6a6c839a27cce3781"}
```

⇒ **CP-2 修复尚未部署到生产**（预期：本轮不涉及部署）。
登记为 §9 D-6；它**不改变**本步 Gate 判定，但**必须在启用 Bootstrap（CP-3 生效）之前完成部署**。

### 7.6 V-3 结论

| V-3 项 | 结论 |
|---|---|
| 1. rendered nginx config | ✅ 已读取并与仓库模板逐行一致 |
| 2. actual exposed HTTP routes | ✅ 已探测（422 / 401）+ 生产路由表只读核对 |
| 3. application route | ✅ 三路由 + 无 `Depends` + WS 由 `main.py` 驱动 |
| 4. HTTPS ingress | ✅ 443 发布、TLS 挂载、HTTPS 探测全部成功（422/401 由 TLS 端口返回） |
| 5. template ≡ effective ingress | ✅ **一致**（模板 MD5 相同；渲染配置 location 逐行一致） |
| 生产修改 | **无**（未改任何配置/数据） |

---

## 8. Security Invariants

| # | 不变量 | 由谁保证 |
|---|---|---|
| I-1 | 未通过 Trust Gate 的请求：**不产生 Interaction**（因此不进入 Bootstrap、不产生 Identity/Grant） | §B/§E 断言 `counts == before`（三表全等） |
| I-2 | Trust Gate 位于 `_load_channel` 之后、`parse_message` 之前 | `test_d3`（AST 行号顺序） |
| I-3 | 段 1 只做类型比对，不依赖 Agent / Identity / Grant / Authorization / Router | `test_d4`（import 白名单 + `_assert_route_trust` 属性集合 ⊆ {type,id} + 禁符号表） |
| I-4 | 每条 HTTP 入站路由**显式声明**允许的 channel type | `test_d1`（AST：每个 `service.*` 调用必须带 `expected_channel_type=<常量>`） |
| I-5 | 任何 HTTP 入站路由**不得**声明 `wecom_bot` | `test_d2`（常量值 + 非 docstring 字面量 + 路由数 == 3） |
| I-6 | 路由不得自行按类型查 Adapter（根因防复发） | `test_d6`（AST 标识符 `MultiChannelAdapter` / `_for_channel_type` 零命中 + 无 `channel.type` 读取） |
| I-7 | WS 入口不被改动；discovery 只枚举 `wecom_bot` | `test_d5`（`_on_bot_frame` 无 `expected_channel_type`；`discover_bot_connections` 含 `NotEq` 过滤） |
| I-8 | 拒绝使用**既有**错误码（403），不新造 | `test_e2` |
| I-9 | Trust Gate 是 Bootstrap 的**前提**（即使 Arm 有效） | `test_e1` |
| I-10 | 合法 `wecom` / `wecom_bot`(WS) / `local` 行为不变 | §A/§B/§C 的 accepted 格 + 全量回归 |
| I-11 | Runtime 管线不新增 stage；Authorization 不变；本轮无 Bootstrap | 未改 `RUNTIME_EXECUTION_ORDER` / `STAGE_*` / authorization / 无 BootStrap 代码（`test_d4` 禁符号表含 `bootstrap` / `identities`） |

---

## 9. Deferred Items

| # | 事项 | 类型 | 说明 / 建议 |
|---|---|---|---|
| **D-1** | `local` 入站面在 Cloud 公网可达且无认证 | **暴露（未激活）** | 由 7.3/7.4 确定：nginx 归入公开代理 + 应用层无鉴权；缓解 = 生产当前 0 个 `local` 渠道。**建议**：Cloud 不创建 `local` 渠道；或为 `local` 定义显式信任模型（**独立裁决**，属新机制，不在 CP-2 范围） |
| **D-2** | WS 入口未声明 `expected_channel_type="wecom_bot"` | 纵深防御 | `main.py` 不在本轮允许清单；该面**非外部可达**（discovery 过滤 + I-7 锁定）。建议后续顺手补一行 |
| **D-3** | `GET` 握手对 `wecom_bot` 由 500 → 403 | 行为收紧 | 两者均 Fail Closed，无安全差异；如运维依赖 500 需显式裁决 |
| **D-4** | `wecom_bot.py` 模块 docstring 未加 CP-2 说明 | 文档 | 不在本轮允许清单；审计 §10.1 M-3 标为可选 |
| **D-5** | 11.7 审计用例仍用 raw-string `"channel.type" not in src` | 测试健壮性 | 建议改 AST（与 I-6 同源）。本轮为不触碰他步用例，选择改写 docstring 措辞（§3.3） |
| **D-6** | 含 CP-2 的版本**尚未部署**到生产 | **部署** | 线上 `rc.6`；启用 Bootstrap（CP-3 生效）**之前**必须部署本修复 |
| **D-7** | 为 `local` 加共享密钥 / HMAC | 不做 | 属新机制（审计 §14 D-1） |
| **D-8** | 速率限制 / IP 白名单 / WAF | 不做 | 运维层加固，不解决根因 |
| **D-9** | Session 唯一约束冲突（M3）、Identity/Grant find-or-create（M1/M2） | 移交 **CP-4** | 与信任闸门无关 |
| **D-10** | Guard 扩展 / `RUNTIME_EXECUTION_ORDER` 不变式 | 移交 **CP-5** | 本步 Guard 可并入 |
| **D-11** | Arm / Identity Bootstrap / First Message Runtime | 移交 **CP-3** | **前置：本修复（CP-2）已实现 ✅；生产启用前需部署（D-6）** |

---

## 10. Final Gate

```
PASS
```

逐条对照判定标准：

| 判据 | 状态 | 证据 |
|---|---|---|
| 所有 Trust Gate matrix 通过 | ✅ | 12/12 格（§4）+ `20 passed`（§5） |
| 现有合法入口无回归 | ✅ | 全量 `2266 passed / 33 skipped / 0 failed / 0 errors`（§6）；`test_c2b` 保持绿 |
| `wecom_bot` HTTP 无法产生 Interaction | ✅ | 第 5/9 格 403 ∧ `parse_message` 未调用 ∧ Interaction/Identity/Grant 零增量（§5.2、§8 I-1） |
| 源码 Guard 存在 | ✅ | `test_d1`~`test_d6`（§8 I-2/I-4/I-5/I-6/I-7） |
| V-3 实际 Cloud ingress 验证完成 | ✅ | §7：模板 ≡ 生效 ingress（MD5 相同 + location 逐行一致），路由/鉴权/暴露面均已核对 |

**判定说明**：不存在「影响安全边界的环境验证缺口」——V-3 已完成且无未决项；
已知的 D-1（`local` 公网可达）是**修复前即存在、且本轮未激活**的独立暴露，
已明确登记并给出缓解事实（生产 0 个 `local` 渠道），不构成本步 Gate 的敞口。
D-6（未部署）属发布动作，不改变「修复已在代码与测试层成立」的判定。

**本步边界（再次确认）**：CP-2 **不是** Bootstrap 实现。
CP-2 只回答「**什么样的入站消息才有资格进入 SteeringFlow Runtime**」；
CP-3 才回答「**已经可信的入站消息，什么时候有资格触发首次 Bootstrap**」。
本轮**未**实现 Bootstrap / Arm，未改 Identity / Grant / Session / Authorization / Runtime，
未新增 migration / 表 / 错误码 / 认证机制。
