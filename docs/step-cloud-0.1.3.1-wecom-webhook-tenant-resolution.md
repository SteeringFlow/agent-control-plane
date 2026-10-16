# Cloud-0.1.3.1 —— WeCom Webhook Tenant Resolution（实现）

> 阶段：**实现**（窄安全边界）。上游：Cloud-0.1.3 Connect Channel Architecture Gate
> （CLOSED，其 F2 即本步）→ **本步** → 真实多租户 WeCom Webhook 验证 → 0.1.3.2
> Connect Channel Golden Path。
> 方法：只读审计 → 最小实现 → 机器化验证
> （`tests/test_cloud_0_1_3_1_wecom_webhook_tenant_resolution.py`，12 项全绿）→ 全量回归
> （1908 passed / 5 skipped / 0 failed）。
>
> **本步的中心问题**：Cloud-0.1.3 Gate 发现的缺口——`main.py` 把 webhook UoW 硬绑
> `TENANT_LOCAL`，导致多租户 Cloud 下「用户发第一条消息时，Webhook 仍然走
> TENANT_LOCAL」。本步把 WeCom 入站接纳变成 tenant-scoped：

```
WeCom Webhook
   ↓
Channel lookup          ← bootstrap 定位读（URL channel_id 仅定位，不构成信任）
   ↓
signature verification  ← WeCom 验签（信任闸门，协议零改动）
   ↓
trusted Channel.tenant_id  ← tenant 取自已认证对象，绝不取自 URL
   ↓
TenantScope             ← 复用 B 的唯一租户原语
   ↓
tenant-scoped UoW       ← 幂等回查 + PENDING 落库（可信写入）
```

---

## 1. 顺序写死（安全性质，机器化锁定）

| # | 性质 | 落点 |
|---|---|---|
| A | **URL `channel_id` 只用于定位**：它是「加载 Channel 的键」，不是 authentication proof、不是 tenant proof | `_load_channel`（bootstrap）；未知/非法 → `InboundChannelConfigError` |
| B | **验签前不得建立可信 TenantScope**：`parse_message`（验签/解密）先于 `_tenant_scope`，验签失败不落任何行 | `accept_message` 内部顺序；`test_e3_verify_before_tenant_resolution_order_is_static` 静态锁 + `test_b1` 行为锁 |
| C | **Tenant 必须来自经过认证的 Channel**：`find_tenant_id_by_id(channel.id)` 从已认证对象读 `tenant_id` | `_tenant_scope`；`test_c1`/`test_c2`（不同 Channel → 各自租户落库） |
| D | **复用 B 的 TenantScope / UoW**：`TenantScope` + `functools.partial(UoW, tenant_id=…)` 工厂桥，与 Browser Auth 面完全同构 | `tenant_scoped_uow_factory`（main.py） |
| E | **不创建第二套 TenantResolver**：不新增 Resolver 类/隔离机制，只以一个工厂提供者把「已认证租户」绑进既有 UoW | 见 §3 决策 2 |

---

## 2. 实现面（逐文件）

| 文件 | 变更 | 说明 |
|---|---|---|
| `app/repositories/channel.py` | **+1 Port 方法** `find_tenant_id_by_id(channel_id) -> int \| None` | bootstrap 路由投影（Port 上唯一显式豁免 tenant 过滤的方法） |
| `app/persistence/repositories/channel.py` | 实现同上 | 按 PK 读 `tenant_id`，**刻意不过滤**——tenant 解析发生在 `SET LOCAL` 之外（B §12） |
| `app/application/channel_inbound.py` | ① `+InboundAcceptance` 值对象（`interaction_id` + `tenant_scope`）；② `__init__` **+可选** `tenant_scoped_factory: (tenant_id) -> uow_factory`；③ `+_tenant_scope` / `+_write_factory`；④ `+accept_message`（tenant-aware 接纳）；⑤ `handle_message` 保留原签名作为兼容入口；⑥ `_existing_row_id` 支持可选 scoped 工厂 | 顺序锁死在 `accept_message`：定位（bootstrap）→ 验签 → 可信租户 → scoped 回查 + 写入 |
| `app/application/interaction_recording.py` | `create_pending` **+可选** `uow_factory` 覆盖 | PENDING 行带上已认证租户（而非入口缺省租户） |
| `app/main.py` | 组合根接线：`bootstrap_uow_factory = partial(UoW, tenant_id=None)` + `tenant_scoped_uow_factory(tenant_id)`，注入 `ChannelInboundService` | `ChannelInboundService` 不再吃 `runtime_uow_factory`（它是租户解析点，不是 Runtime 执行组件）；Runtime 执行组件单一工厂贯穿的不变量**保持不变** |

向后兼容：`tenant_scoped_factory` 缺省 `None` → 行为与隔离前逐字节一致（legacy/单租户）；
`handle_message` 仍返回 `int | None`，既有调用方零改动（`test_e1`/`test_e2` 锁定）。

---

## 3. 关键实现决策

### 3.1 bootstrap 投影 = Port 方法，**不是** Channel 域字段

0.1.3 Gate F2 曾表述为「给 Channel 域补 tenant_id」。**实现时修正**：B 红线 A 明令
「tenant_id 绝不进入 Domain / Mapper」（§14：`to_domain(row)` 不读 tenant）。因此
tenant_id 以**独立投影方法**返回，Domain 对 `Channel` 域面零改动
（`test_f2_bootstrap_projection_does_not_leak_domain_tenant` 锁定域内无 tenant 字段）。

### 3.2 为什么不造第二套 TenantResolver

B 的 `TenantResolver` 是「入口把 routing hint + 凭据解析为 TenantScope」的**端口**；
Browser 面已由 `build_tenant_scope_provider` 承接。WeCom 面的对应物不是新 Resolver 类，
而是同一个解析结果的**工厂桥**：`_tenant_scope`（解析）+ `tenant_scoped_uow_factory`
（桥接），两者都直接消费 B 的 `TenantScope` / `SqlAlchemyUnitOfWork`。入口对称关系因此
成立，且机制唯一：

```
Browser： Cookie → BrowserAuthenticator → AuthenticatedUser.tenant_id → TenantScope → UoW
WeCom  ： URL   → WeComAdapter 验签    → Channel.tenant_id          → TenantScope → UoW
```

### 3.3 `ChannelInboundService` 移出「Runtime 执行组件单一工厂」集合

它是**租户解析点**（ingress），不是 Runtime 执行组件：定位读必须无 tenant 过滤
（bootstrap），可信写入必须 per-tenant。Runtime 执行组件（Coordinator / 六 Executor /
PersistingCoordinator / Worker / adapters）的「同一 `runtime_uow_factory` 贯穿」不变量
**原样保留**（test_step12_6 仅把它移出该集合，并为入站边界新增专断言）。

---

## 4. NOT TOUCHED（冻结边界）

| 类别 | 说明 |
|---|---|
| **WeCom Authentication 协议** | `WeComAdapter` 验签/解密/AES/XML **零改动**（仅作为解析的下游被调用） |
| **Runtime semantics** | 六 Executor / Coordinator / Worker / Driver 逻辑与签名零改动；未新增 Runtime 语义 |
| **Browser Auth** | `browser_auth` / `app/api/auth.py` 零改动；两个信任边界不合并 |
| **Agent / Connection / ACPAdapter** | 零改动（0.1.2.2 裁决不变） |
| **Domain / Mapper** | `Channel` 域无 tenant 字段（B 红线 A）；`to_orm`/`to_domain` 零改动 |
| **schema** | 零迁移（复用既有 `channels.tenant_id` 列与两层防线） |
| **Channel Health / Integration Entity** | 未新增（0.1.3 Gate G5/G9 维持） |
| **Runtime 执行组件组合不变量** | Coordinator/Executor/Worker 恰好一次、单一 uow factory 贯穿——全部保持 |

---

## 5. 测试演进面（诚实记录）

| 测试 | 演进 | 理由 |
|---|---|---|
| `test_step12_6` `test_production_single_uow_factory_across_all_components` | `ChannelInboundService` 移出 Runtime 执行组件集合 | §3.3：入站边界是租户解析点，用 bootstrap + tenant-scoped 双工厂 |
| `test_step12_6` `test_production_inbound_service_wiring_matches_test_composition` | kwargs 集合 `+ tenant_scoped_factory`（并断言两个工厂的表达式） | 组合根接线演进 |
| `test_step10_2_1_inbound_adapter_contract` `test_c1` import 白名单 | `+ dataclasses`、`+ app.core.tenant` | `InboundAcceptance` 值对象 + `TenantScope`（app.core 不在 application 禁入清单） |
| `test_cloud_0_1_3_..._gate` `test_tenant_resolution_derived_from_validated_channel_not_url` | 「源码不含 tenant」启发式 → 断言真实不变量 `find_tenant_id_by_id(channel.id)` | Gate 锁的是原则；实现落地后锁其实现形态 |
| `test_step5_7` / `test_step6_5` / `test_step6_7` / `test_step7_3` 的 Port 方法面基线 | channel 集合 `+ find_tenant_id_by_id` | Port 方法面基线锁（B §19.2 的合法演进面） |

新增：`tests/test_cloud_0_1_3_1_wecom_webhook_tenant_resolution.py`（12 项）——A 定位拒绝 /
B 验签拒绝 / C 落到已认证租户 / D 幂等+ack-ignore / E 兼容与顺序 / F bootstrap 投影与
Domain 不泄漏。

---

## 6. 已知限制与下一增量（不阻塞本步 GO）

| # | 限制 | 下一增量 |
|---|---|---|
| L1 | **异步续接的 tenant fan-out**：Worker / Driver 的 `uow_factory` 仍绑定 `runtime_uow_factory`（TENANT_LOCAL）。本步已使 PENDING 行带上正确租户，但多租户 PG 下 Worker 尚不能跨租户 pickup | 演进 single-runtime 组合为 per-tenant runtime fan-out（B §8 末）——需要重构 test_step12_6 的「恰好一次」组合锁，属独立架构增量，即「真实多租户 WeCom Webhook 验证」步的核心 |
| L2 | **PG RLS bootstrap 读**：`tenant_id=None` 定位读在 PG 上受 `channels` 的 RLS policy 约束（fail-closed） | 按 B §9 备选引入 `SECURITY DEFINER` 解析函数或 channels bootstrap 分支 |
| L3 | 本地 SQLite 无 RLS，跨租户隔离由第一层 Repository 过滤承担 | 已有两层防线纪律（B §0），无需变更 |

---

## 7. GO / 闭合

**GO。** 中心问题「一个 Cloud Tenant 如何安全地拥有一个 WeCom Channel，并让这个
Channel 通过既有 Connection 接入已有 Agent Runtime」的**入站接纳段**已经闭合：

- URL `channel_id` 只是定位键（A1/A2）；
- 验签是唯一信任闸门，失败不落任何行（B1）；
- 验签之后才由已认证 Channel 解析 `TenantScope`（E3 静态顺序锁）；
- 幂等回查 + PENDING 落库都在该租户的 tenant-scoped UoW 上（C1/C2/D1）；
- 全部复用 B 的 `TenantScope` / UoW，未造第二套 Resolver / 隔离机制 / Domain 污染
  （F1/F2）；
- WeCom 协议、Runtime 语义、Browser Auth、Agent/Connection 全部未触碰（§4）。

遗留：异步执行段的 tenant fan-out（L1）是下一增量——那是把单一 Runtime 组合演进为
per-tenant fan-out 的独立架构步，正是用户规划的「真实多租户 WeCom Webhook 验证」。
