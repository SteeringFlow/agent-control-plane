# Step 8.2 —— Identity Resolution Contract & Application Boundary

一句话目标：**先不写 IdentityResolver**，只冻结「Identity Resolution 到底解决
什么问题、成功结果长什么样、失败怎么 Fail Closed、和 6.6 Management 怎么分界」。

    InboundInteraction
          │  channel_id / external_identity_id   ← 最小输入（恰好两个字段）
          ▼
    IdentityResolver（未来 Runtime 组件，本 Slice **不实现**）
          │
          ▼
    Identity
          │
          └── User（**可选**的管理关联；缺失不影响解析成立）

## 1. 唯一核心问题

> 这个 Channel 上的 `external_identity_id`，到底对应 Control Plane 中哪个
> `Identity` / `User`？

它正好接住 8.1 的 InboundInteraction：Adapter 归一化只认渠道侧标识，解析归属
（external identity → 内部 Identity → User）由 Runtime 完成。

## 2. 契约（冻结在 `app/domain/identity_resolution.py`）

| 契约件 | 内容 |
| --- | --- |
| 最小输入 | `channel_id` + `external_identity_id`（InboundInteraction 的字段，不新增查询意图） |
| 唯一查找 | 复用 6.6 冻结键与 Port：`IdentityRepository.find_by_channel_and_external_id()` |
| 成功结果 | `IdentityResolution`（frozen dataclass，`identity: Identity` + `user: User \| None`） |
| 失败语义 | **未找到 Identity** → `IDENTITY_RESOLUTION_FAILED` → **Fail Closed**（绝不猜一个 User） |

### 2.1 成功条件（Gate 2.6 D3 修订 / Implementation Package 1 落地）

```
Identity exists + Channel matches + External ID matches   →   RESOLVED
User exists / does not exist                              →   不再决定成败
```

- 「未绑定 ACP User 的 External Identity」是**合法的一等运行时身份**
  （Gate 2.6 D1/D2）：`IdentityResolution.user is None` **不是**失败；
- User 缺失**不触发任何创建**（ADR-5 / 9.2 源码级守卫继续有效）；
- 本修订**显式、可追溯**（来源：Gate 2.6 D3），不是静默改字。

## 3. 复用 6.6 冻结键，Runtime 不重定义唯一性

- Identity Repository 方法面**不增不减**（`get_by_id/create/update/
  find_by_channel_and_external_id/list_identities`）；
- 查找键 = DB `UNIQUE(tenant_id, channel_id, external_id)`
  （`uq_identities_tenant_channel_external_id`；Cloud-0.1.1-B 在 4.2 的
  `(channel_id, external_id)` 前导了 tenant_id），唯一约束不新增；
- 结论：唯一性语义只在 6.6 定义一次，Runtime 只消费。

## 4. Management ≠ Resolution（Application Boundary 不变）

```
6.6 Identity Management：Admin → CreateIdentity → User + Channel + Identity（写 + commit）
8.2 Identity Resolution ：InboundInteraction → … → Identity → User（只读 + Fail Closed）
```

不是同一个 Service：前者**管理身份绑定**，后者**运行时解析身份**。合成一个
IdentityService 会双向职责污染。

机器化锁定：
- `application/` 文件面不变（6 个管理文件），没有新的 Identity* 解析文件混入；
- `IdentityApplicationService` 方法面仍是 `create_identity/get_identity/
  update_identity`，**没有** `resolve_identity`。

## 5. 仍然不做（含 Fail-Closed 语境）

❌ IdentityResolver（本 Slice 只冻结契约） ❌ 自动 Identity Linking
❌ email/phone fuzzy matching ❌ User matching（无 email/phone finder）
❌ Session ❌ Router ❌ Connection selection ❌ Agent dispatch
❌ Authorization ❌ RBAC ❌ Channel Adapter ❌ API

`external_identity_id` 找不到 Identity 时**不能猜一个 User**：
Identity not found → `IDENTITY_RESOLUTION_FAILED` → Fail Closed。

## 6. 验收

`tests/test_step8_2_identity_resolution_contract.py`：14 项全绿（契约模块边界 /
结果形状 / 失败码冻结 / 运行时行为 / Identity Port 面不变 / 唯一键不变 /
输入对齐 InboundInteraction / application 边界不变 / 管理 Service 无解析方法 /
无猜 User 能力 / 无 Resolver / User Port 无匹配抓手 / mutation 探测）。

`python -m pytest -q` → **316 passed**（302 回归 + 8.2 新增 14）。

后续：IdentityResolver（Runtime 组件）→ SessionResolver → Router（8.3+）。
