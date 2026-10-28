# Artifact Phase 2 —— URI 策略与安全呈现（实施契约）

> 状态：**已实施并已验收**（ACP V0.4 Phase 2，post-V0 范围变更）—— 项目负责人于
> **2026-10-26** 接受本阶段实施结果（验收决定、验收范围、已登记未解决项与 Git 状态见
> **§13 验收记录**）；**未 commit / push / tag / 部署**。
> 上游：`docs/step-acp-v0.3-artifact-outbound-implementation-gate.md`（Phase 1 契约与 47 条验收标准）。
> 本阶段是**只做策略判定**的一层：在 Phase 1 已交付的「引用透传 + 安全呈现」之上，
> 把**可呈现的 URI** 收窄为**符合策略的绝对 HTTP(S) URL**，并把**不可呈现**的引用
> **整体省略**（不输出任何片段）。
>
> **本阶段不引入任何新能力**：无网络请求、无字节取回、无存储、无下载端点、
> 无签名 URL、无 ACL、无新表 / 新列 / 新迁移、无新 Activity 事件。

---

## 1. 目标与范围

| 项 | 内容 |
|---|---|
| **目标** | 把「Agent 提供的 URI」从「原样呈现」收紧为「**符合策略才呈现**」：仅当 URI 是**符合策略的绝对 HTTP(S) URL** 时才作为可点击引用呈现；否则**整体省略**并给出明确原因 |
| **实现的载体** | `backend/app/domain/artifact_uri_policy.py`（**新增**：纯标准库分类器）+ `backend/app/domain/artifact_reference.py`（**修改**：接入策略闸门） |
| **不做** | URI 可达性探测、内容 / 类型 / 大小验证、文件级访问授权、下载 / 代理 / 缓存 / 存储、签名 URL、预览 / 下载端点、入站附件、Agent→Agent 引用转发 |
| **影响面** | **零**契约字段面变更、**零** schema / 迁移、**零** Port / Worker / 执行顺序变更、**零** SDK wire 变更 |

### 1.1 五项能力的严格分离（不得合并为笼统的「URI 验证」）

| # | 能力 | 本阶段 | 说明 |
|---|---|---|---|
| 1 | **URI 格式检查** | ✅ **本阶段实现** | 结构 / 协议 / 长度 / 字符类别 |
| 2 | **URI 可达性检查** | ❌ 不做 | 探活本身即 SSRF 面；须先交付 §8 的取回前置条件 |
| 3 | **文件内容与类型验证** | ❌ 不做 | `mime_type` / `size_bytes` 是 **Agent 声明值**，呈现时必须标注未验证 |
| 4 | **用户 / 租户访问授权** | ❌ 不做 | ACP **不提供**文件级 ACL 模型（现有 Authorization 是 Conversation→连接 许可集） |
| 5 | **文件下载 / 缓存 / 代理交付** | ❌ 不做 | 仓库无 blob / 无文件写 / 无代理基础设施 |

> **红线（不可混淆）**：**URL 可访问 ≠ 用户有权访问；HTTP 200 ≠ 文件可信；
> URI 中的租户标识 ≠ 授权依据。**「可呈现」**不等于**「可访问」**不等于**「可信」
> **不等于**「已授权」。

---

## 2. URI 分类规则与拒绝原因

判定入口：`classify_artifact_uri(uri, *, max_length=2048) -> UriClassification`
（`presentable: bool` + `reason: str | None`；**全函数**、**确定性**、**零 I/O**）。

判定用标准库 `urllib.parse.urlsplit`（**不**用过宽的正则代替 URL 解析）；
**无法可靠分类的异常输入一律 fail closed**（拒绝呈现）。

| 顺序 | 规则 | 原因码 | 呈现层文案 |
|---|---|---|---|
| 1 | `None` / 非 str / 纯空白 | `no_reference` | （沿用 Phase 1「未提供可用引用」） |
| 2 | 长度 > **2048** | `too_long` | 引用地址过长 |
| 3 | 含 `Cc` / `Cf` / `Zl` / `Zp` 字符 | `unsafe_characters` | 引用包含不适合展示的字符 |
| 4 | 含空白（`isspace`） | `invalid_structure` | 引用地址结构无效 |
| 5 | 含反斜杠 `\`（异常分隔符） | `invalid_structure` | 引用地址结构无效 |
| 6 | `urlsplit` 抛错（括号不配对等） | `invalid_structure` | 引用地址结构无效 |
| 7 | 无 scheme（相对 / 协议相对 URL） | `not_absolute` | 引用不是绝对地址 |
| 8 | scheme ∉ {`https`, `http`}（大小写归一后） | `unsupported_scheme` | 引用使用了不受支持的协议 |
| 9 | netloc 含 `@` 或有 userinfo | `contains_userinfo` | 引用地址包含用户名或密码 |
| 10 | 主机缺失 / 结构非法 | `invalid_host` | 引用地址缺少有效主机 |
| 11 | 端口非法或越界（非数字 / 0 / >65535） | `invalid_port` | 引用地址端口无效 |
| 12 | 通过 | `presentable` | 原样呈现（代码跨度内） |

**允许的 scheme 是固定安全策略**（`ALLOWED_URI_SCHEMES = {"https", "http"}`）——
**不提供**任何放开 `javascript:` / `data:` / `file:` 等危险协议的开关，
**不**通过配置放宽。

### 2.1 主机与端口判定细则

- 主机为 **IPv6 字面量**（`hostname` 已剥离方括号）时，用 `ipaddress.IPv6Address` 校验；
- 其余主机按 **reg-name** 校验：仅 ASCII 字母 / 数字 / `-` / `.`，且不得以 `.` 或 `-` 起止、
  不得含 `..`；
- **不判断网络位置**——私网 / 环回 / 元数据端点的判定属 §8 的取回前置条件，
  **本阶段不做**（本模块从不发起请求）。

### 2.2 `Artifact.kind` 与 URI 形态

**不为 `file` / `data` / `link` / `image` 发明新的 URI 格式要求**：既有契约
（`app/domain/artifact.py`）只规定 `kind` 是类型判据、`uri` 是「可解析位置」，
**未**定义 per-kind 的 URI 形态 ⇒ 策略对所有 kind **一致**。

---

## 3. 与 Phase 1 的兼容边界

| 项 | 边界 |
|---|---|
| **Phase 1 的 47 条验收标准** | **一条未删、无一放宽**；仅 C12 的**措辞**被精化（见 §3.1） |
| **闸门优先级** | Phase 1 的**字符 / 标记**闸门在前（文案更贴近「可能拼出链接结构」），**策略**闸门在后 |
| **无 artifact 时** | 出站 content **逐字节**不变（Phase 1 B1–B4 继续全绿） |
| **可呈现的 HTTP(S) URI** | 呈现形态**逐字节**沿用 Phase 1（代码跨度 + 「未验证」提示 + 原值） |
| **新增呈现** | `mime_type` / `size_bytes` 呈现时标注「**Agent 声明值，未验证**」 |
| **新增拒绝** | 不合规的 URI ⇒ 整体省略 + 明确原因 + 计入省略计数 |

### 3.1 C12 的措辞精化（**实质保护未降低**）

Phase 1 的 C12 断言「渲染器 import 根**仅 stdlib**」。Phase 2 依 §3 要求新增
`app.domain.artifact_uri_policy` 并由渲染器导入 ⇒ 渲染器新增**唯一**一个仓内依赖。

**精化后的守卫（实质增强）**：
1. 渲染器的**非 stdlib** 依赖**恰为** `app.domain.artifact_uri_policy`（不多不少）；
2. **该策略模块自身**的 import 根**仅 stdlib**（**新增**守卫 ⇒ 保护面扩大）。

⇒ 「渲染层 = 纯函数、无 I/O、无框架、无持久化依赖」这一**实质**完全保留，
且策略模块被**额外**纳入 stdlib 纯净性守卫。

---

## 4. 原始快照 / outbox 保真原则（**呈现层收紧不回溯**）

| 面 | 处置 |
|---|---|
| `interactions.response_payload["artifacts"]` | **完整原值**：`uri` 与 `metadata` **不截断、不改写、不消毒**（`_snapshot_artifacts` 未改）⇒ 被策略拒绝的 URI 在快照中**仍是原值** |
| Local outbox 的 `artifacts` 键 | **完整原值**（7 字段；`artifact_reference_records()` 未改）⇒ 同上 |
| Local outbox 的 `content` | Agent 原文（Local 不做呈现） |
| 呈现层 | **唯一**做策略判定与省略的地方 |

⇒ **「消息里没出现」≠「数据里被改了」**：策略只影响**呈现**，**不污染持久化事实**。

---

## 5. 明确声明（写入契约与文案）

- ACP **不取回** URI 指向的资源：**零**网络请求、**零**字节下载、**零**字节上传。
- ACP **不验证** URI 的**可用性 / 真实性 / 权限**；呈现文案恒含
  「Agent 提供的文件引用（ACP 未验证其可用性或安全性）」。
- `mime_type` / `size_bytes` 是 **Agent 声明值** ⇒ 呈现时标注「Agent 声明值，未验证」。
- ACP **不提供**任何**文件级访问授权**（无 ACL、无签名 URL、无下载端点）。
- **URL 可访问不代表用户有权访问；HTTP 200 不代表文件可信。**
- 被策略拒绝的引用：**整体省略 + 明确原因**；**绝不**输出 URI 的任何片段（前缀 / 后缀 /
  省略号形式均不可），**绝不**把它描述成可用地址。

---

## 6. 明确排除项

| 排除 | 理由 |
|---|---|
| URI 可达性探测（HEAD/GET 探活） | 探活本身即 SSRF 面；须先交付 §8 |
| 文件下载 / 缓存 / 代理 | 仓库**零**基础设施；三条出站路径**无字节承载能力** |
| 对象存储 / artifact 表 / Alembic 迁移 | 本阶段**零持久化** |
| 签名 URL / ACL / 下载端点 / 预览 | 属方案 C（受控存储 + 受控链接） |
| 文件级授权模型 | 现有 Authorization 是 Conversation→连接，**不可**复用为文件 ACL |
| `uri` / `metadata` 脱敏 | 会破坏 L4 原值保真且与既有 `response_payload` 暴露事实矛盾 ⇒ 独立议题 |
| 入站附件 / Agent→Agent 引用转发 | Phase 4 / Phase 3 |
| 新 Activity 事件 / 新事务 / 新 `delivery_status` 取值 | 沿用 Phase 1 裁决（D3） |
| 改 `Artifact` 7 字段 / `AgentResponse` / `OutboundResponse` / SDK wire / minimal wire / Tencent SSE | 策略是**派生分类**，**无需**新字段 |
| 改 DeliveryExecutor / `ChannelAdapter` Port / Worker / 主执行顺序 | 本阶段**零 I/O** ⇒ 零冲突 |
| 改 Release Freeze / 版本 / 标签 / 分支 | 约束 |

---

## 7. 配置

**本阶段未新增任何配置项。** 长度上限是**固定常量** `DEFAULT_MAX_URI_LENGTH = 2048`
（`app/domain/artifact_uri_policy.py`）。

**理由**（对齐 Phase 2 任务书 §6「只增加实现确实需要的最少配置…不要为了让简单纯函数
可配置而引入多余抽象」）：长度上限作用于一个**简单纯函数**；把它做成配置项需把值注入
渲染路径（三个 Channel Adapter 构造函数 + 组合根守卫），属**超出本阶段范围**的接线工作；
且渲染层必须保持**纯 stdlib**（§3.1）。

**待有真实调参需求时**，再以**独立 Gate** 引入配置项，并**同时**补上
**启动期 fail-fast** 校验（沿用 `app/core/config.py` 既有范式：
`Field(default=2048, ge=1)` + `get_settings()` 在 `create_app()` 启动路径读取）。

**危险协议拒绝策略与字符安全策略永远不可被配置覆盖**（无论是否引入配置项）。

---

## 8. 后续**真正实现文件取回**时必须满足的**前置条件**（本阶段全部未做）

> 取回（无论代理还是导入存储）**必须**先交付下列**全部**条件，缺一不可。
> 这是**先于需求**存在的门槛，而不是可选项。

| # | 前置条件 | 要求 |
|---|---|---|
| 1 | **scheme 白名单** | 仅 `https`（`http` 需显式按部署开启）；**禁止** `file:` / `data:` / `javascript:` / `gopher:` 等 |
| 2 | **DNS 与私网地址防护** | 解析**每个**候选地址；拒绝环回 / 私网 / 链路本地 / 元数据端点 / 保留段；**不得**只校验 URL 字面量 |
| 3 | **DNS 重绑定防护** | 连接必须**固定到已校验的解析结果**（不得二次解析），否则前置条件 2 可被绕过 |
| 4 | **重定向控制** | **不跟随**重定向（或逐跳重新执行 1–3 校验）；**不得**使用会自动跟随的默认 opener |
| 5 | **TLS 校验** | 证书链与主机名校验**不得**关闭；`https` 降级到 `http` 一律拒绝 |
| 6 | **大小与超时限制** | `Content-Length` **与**实际读取量**双上限**；连接 / 读取超时；**流式**读取并硬性截断 |
| 7 | **资源限流** | 每租户 / 每 Agent / 全局的并发与带宽配额；**不得**在无配额的情况下取回 |
| 8 | **内容类型与内容校验** | 声明 MIME **与**实际内容**交叉验证**（magic number / 实际 MIME）；拒绝伪装 |
| 9 | **交付失败与重试语义设计** | 「文本已送达 + 文件未送达」**必须可观测**（当前 `ChannelAdapter.deliver()` **无返回值** ⇒ 需显式设计 Port 契约或独立事件）；重试**不得**导致重复投递（注意 Worker 回收阈值后的**整体重跑**） |
| 10 | **凭据与租户边界** | 取回凭据的存放与解析（当前 `credential_ref` 是**不透明外部引用**，ACP **不解析**）；**不得**以 URI 中的租户标识作为授权依据；跨租户取回必须 Fail Closed |
| 11 | **生命周期** | 若导入存储：容量配额、保留期、GC、删除语义、导出 / 审计 |
| 12 | **可观测与审计** | 记录**事实**（取回成功 / 失败 / 跳过 / 大小），**不得**记录完整签名 URL 或敏感 query |

---

## 9. 机器化验收

**新增**：`backend/tests/test_acp_v0_4_artifact_uri_policy.py`（30 用例）——
分类规则（正常 HTTP(S) / scheme 大小写 / 危险协议 / userinfo / 相对 URL / 缺主机 /
非法端口与越界 / IPv6 正常与异常 / `Cc`·`Cf`·`Zl`·`Zp` 字符 / 超长 / `None` /
异常输入 fail closed / kind 语义 / 确定性）、呈现层（零片段、明确原因、原值逐字节、
「Agent 声明值，未验证」、`uri=None` 语义、顺序与计数、无 artifact 逐字节不变、
零网络调用）、导入边界（策略模块 stdlib 纯净、零 I/O）、原值保真（快照 / outbox /
dispatch Fail Closed）。

### 9.1 Phase 1 既有断言的最小调整（逐项列明）

| # | 断言 | 调整 | 理由 |
|---|---|---|---|
| 1 | `test_c12_renderer_imports_stdlib_only` | 守卫**精化**：非 stdlib 依赖**恰为**策略模块，且**新增**「策略模块自身仅 stdlib」断言 | 本阶段要求新增纯 stdlib 策略模块并接入 ⇒ 渲染器新增**唯一**仓内依赖；实质保护**增强** |
| 2 | `test_e2_no_new_persistence_write_site_for_artifacts` | 同上的 import 根断言同步精化 | 与 #1 同一事实的重复守卫 |
| 3 | `test_c16_over_budget_uri_omitted_entirely` | **仅**用例长度取值 3000 → **1900**（**三条断言逐字未改**） | 策略层新增 URI 长度上限 2048（超过即拒绝、不截断）⇒ 原用例的 3000 字符 URI 现被**策略**拦下而非**渲染预算**；改用 1900 字符后**仍然超出渲染可用预算**（块预算 2000 − 提示预留 240 ⇒ 可用 ≈1760），仍精确检验「渲染预算路径」；「策略长度上限」路径由 `test_a12_too_long_uri_is_rejected_without_truncation` 单独覆盖 |

---

## 10. 冻结边界（本阶段未触碰）

| 冻结面 | 状态 |
|---|---|
| V0 冻结治理文档（内部治理文档，不随 OSS 发布） | ✅ **未改** |
| `Artifact` 7 字段 | ✅ 未改（仅 docstring 修正过时表述） |
| `AgentResponse` / `OutboundResponse` 字段面 | ✅ 未改 |
| SDK wire / minimal wire / Tencent SSE | ✅ 未改 |
| `InteractionModel` 16 列 / DB schema / Alembic | ✅ 未改（无迁移） |
| Activity 类型 / `delivery_status` 取值 | ✅ 未改 |
| `DeliveryExecutor` / `ChannelAdapter` Port / Worker 主执行顺序 | ✅ 未改 |
| Phase 1 快照与 Local outbox 的**原值保真** | ✅ 未改 |
| URI 网络请求 / 下载 / 存储 | ✅ **零** |

---

## 11. 实施面清单

| # | 文件 | 动作 |
|---|---|---|
| 1 | `backend/app/domain/artifact_uri_policy.py` | **新增**：纯标准库分类器（`classify_artifact_uri` / `unsafe_character_kind` / 原因码与文案表） |
| 2 | `backend/app/domain/artifact_reference.py` | **修改**：接入策略闸门（`omit_policy` 状态 + 明确原因）、`mime_type`/`size_bytes` 的「Agent 声明值，未验证」标注、`max_uri_length` 参数透传、移除已被策略模块取代的两个私有常量 |
| 3 | `backend/app/domain/artifact.py` | **修改**：**仅 docstring** —— 修正被 Phase 1 取代的「不投递」表述，并补上 Phase 2 的「不取回 / 不授权」声明 |
| 4 | `backend/tests/test_acp_v0_4_artifact_uri_policy.py` | **新增**：30 个验收用例 |
| 5 | `backend/tests/test_acp_v0_3_artifact_outbound.py` | **修改**：§9.1 的 3 处**最小**断言调整 |
| 6 | `docs/step-acp-v0.4-artifact-uri-policy.md` | **新增**：本契约 |

**未修改**：`app/core/config.py`（§7）、三个 Channel Adapter（呈现接入在渲染层完成，
无需改适配器）、`app/main.py`、任何 schema / 迁移 / Port / Worker / SDK 文件。

---

## 12. 与 Phase 1 Gate 的关系

Phase 1 Gate（`docs/step-acp-v0.3-artifact-outbound-implementation-gate.md`）的
**47 条验收标准在本阶段全部仍然成立**（Phase 1 的 43 个用例与 47 条标准的对应关系
见该文档 §8.1）。本阶段**新增**的是**收紧**（策略闸门）与**标注**（声明值），
不**放宽**任何既有要求；唯一的**措辞**变化是 C12 的 import 守卫精化（§3.1），
已在 §9.1 逐项列明。

---

**本阶段不做**（再次明确）：文件字节交付、URI 探活、下载 / 代理 / 存储、签名 URL、
文件级 ACL、入站附件、Agent→Agent 引用转发（Phase 2 / 3 / 4）。

---

## 13. 验收记录（2026-10-26）

### 13.1 验收决定

| 项 | 内容 |
|---|---|
| **决定** | 项目负责人**接受** Artifact Phase 2 的实施结果 |
| **日期** | 2026-10-26 |
| **验收范围** | **仅限**「URI 策略检查与安全呈现」（方案 A′）—— §1–§6 所述范围 |
| **明确不在验收范围内** | URI 可达性探测、文件内容 / 类型 / 大小验证、文件级访问授权、下载 / 代理 / 缓存 / 存储、签名 URL、下载 / 预览端点、Phase 3、Phase 4 |
| **验收方式** | 实施报告 + **最终治理审计**（两处治理守卫专项审计 + URI 策略只读安全复核 + 兼容性复核） |

### 13.2 治理守卫专项审计结论（通过）

| 守卫 | 结论 | 认可的例外 |
|---|---|---|
| `tests/test_step6_7_management_closure.py::test_m_no_rbac_capability_policy_gateway_infra` | ✅ **通过**——例外为**精确集合成员**判定；7 类禁用概念、ClassDef 检查、扫描范围**全部保留**；突变注入证明仅「同一模块的 3 种导入形式」改变判定 | `app.domain.artifact_uri_policy`（**仅此一个完整模块名**） |
| `tests/test_step10_1_4_runtime_vertical_slice_closure.py::test_runtime_core_has_no_transport_or_protocol` + `::test_protocol_implementation_lives_only_in_adapter_layer` | ✅ **通过**——`urllib.request` / `http.client` / `socket` / `ssl` / `httpx` / `requests` / `aiohttp` / `websockets` / `grpc` **全部仍拦**；传输归属白名单**逐字未变** | **仅** `app/domain/artifact_uri_policy.py` 中的 `urllib.parse`（**文件 + 完整模块名**二元组） |

> ⚠️ **禁止扩大例外范围**：上述两项例外**不得**扩展到任何其他模块、文件或 import 形式；
> 扩大需重新走治理审计。

### 13.3 已登记未解决项（**保留**，本轮**不**解决）

| 编号 | 项 | 性质 |
|---|---|---|
| **OBS-1** | `https://example.com:/x`（**空端口**）被判为 presentable | 非缺陷（RFC 3986 允许空端口；ACP **从不取回**，无安全影响）。若将来做取回，空端口须走 §8 前置条件 |
| **OBS-2** | `http://:::1/x` 的原因码为 `invalid_host`（而非 `invalid_structure`） | 仅原因码粒度；结论一致（拒） |
| **OBS-3** | 含 Tab 的 URI 走 `unsafe_characters`（Cc）而非 `invalid_structure` | 仅闸门顺序；结论一致（拒） |
| **L-1** | A1 守卫检查**导入模块路径**，**不检查被导入的符号名** | **既有**词面局限（对任何模块名成立），本阶段**未引入、未扩大** |
| **L-2** | A1 守卫的禁用词为**字面子串**匹配，不处理词形（`policies` 等） | **既有**词面局限，本阶段**未引入、未扩大** |
| **R-2 … R-6** | 见 `docs/step-acp-v0.3-artifact-outbound-implementation-gate.md` §8.2 | ⚠️ **仍为「已登记」**，**不得**标记为**已解决**或**已验证**；本阶段**未**改变其状态措辞 |

### 13.4 验证结果（验收时点实测）

| 范围 | 结果 |
|---|---|
| Phase 1 测试文件 | ✅ **43 passed** |
| Phase 2 测试文件 | ✅ **30 passed** |
| 治理守卫 + 目录清单（审计确认跑） | ✅ **116 passed** |
| Gate §9.4 第 1 组（含 Phase 2 文件） | ✅ **194 passed** |
| Gate §9.4 第 2 组（出站链回归） | ✅ **221 passed** |
| **全量** `pytest tests -q` | **3 failed / 2913 passed / 41 skipped** —— 3 项失败与既有基线**逐条相同**，**零新增失败**；passed **+30** 恰为新增用例数（**无既有测试丢失**） |

**冻结面核验**：`Artifact` 仍 7 字段；13 表 / **0** artifact 表；`InteractionModel` 16 列；
Alembic `0001…0008`（**无新增**）；Activity 4 类型 + 7 阶段；`DeliveryExecutor` /
`ChannelAdapter` Port / Worker 主执行顺序**未改**；**无 URI 网络请求 / 下载 / 存储**；
V0 冻结治理文档（内部治理文档，不随 OSS 发布）**未改**；Phase 1 **47 条**标准编号连续、**无一删除 / 跳过 / 降低**。

### 13.5 Git 状态（验收时点）

| 项 | 值 |
|---|---|
| 分支 / HEAD / VERSION | `master` / `a5fd8ab`（`chore(release): prepare 0.1.0-rc.10 candidate`）/ `0.1.0-rc.10` |
| 工作树条目 | **27**（17 已跟踪修改 + 8 未跟踪 + 既有 `0` / `build`） |
| 已跟踪 diffstat | **19 files, 297 insertions(+), 78 deletions(-)** |
| 暂存区 | **空**（未 `git add`） |
| 标签 | 仅 `v0.1.0-rc.1` … `rc.8`（**未打标**） |
| **Phase 1 / Phase 2 交付物** | **全部未跟踪 ⇒ 均保持未提交**（6 个：2 个生产/渲染文件、2 个测试文件、2 份文档） |
| 既有未跟踪文件 | `0`、`build` —— **未清理 / 未覆盖 / 未纳入** |
| Release Freeze | **未改** |

### 13.6 阶段进展索引（交叉引用）

| 阶段 | 记录 | 状态 |
|---|---|---|
| ACP V0.2 | `docs/step-acp-v0.2-artifact-contract-gate.md` | 契约 + 实现已落地（字节交付 / 专属表列仍 DEFERRED） |
| ACP V0.3 Phase 1 | `docs/step-acp-v0.3-artifact-outbound-implementation-gate.md` | 已实施并验收（47 条全部 PASS；R-1 已闭合；R-2…R-6 **已登记**） |
| **ACP V0.4 Phase 2** | **本文** | **已实施并已验收**（本 §13） |

### 13.7 本轮（验收记录）性质

**仅文档标注**：本轮**只**在既有阶段记录中标注 Phase 2 的验收结论、验收范围、
已登记未解决项与 Git 状态；**未修改任何实现代码**、**未新增或修改任何测试**、
**未扩大功能范围**、**未启动 Phase 3 / 4**、**未 commit / push / tag / 部署**。

