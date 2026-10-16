# Cloud-0.1.1-C —— Browser Authentication Architecture Gate

> 阶段：Architecture Gate（设计 + 影响审计）。**本次只交付设计，不实现。**
> 方法：只读审计 + 机器化护栏（`tests/test_cloud_0_1_1_c_browser_authentication_gate.py`）。
> 配套契约：ACP V0.1 RC → ACP V0.2 Artifact Contract → Cloud-0.1.1-A（PG + Alembic + 8 Core Tables）→
> Cloud-0.1.1-B（Tenant Isolation）→ **本 Gate（Browser Authentication）**。
>
> **本 Gate 的中心问题（单一裁决标尺）**：
>
> ```
> Browser
>   ↓
> Who is this user?          ← L2：本 Gate 唯一净新增职责
>   ↓
> Which Workspace/Tenant?    ← L3：复用 B 的 TenantScope（Browser Auth 是 tenant scope 的「生产者」，不是隔离本身）
>   ↓
> What Cloud resources can this request access?  ← L4：复用 B 的两层隔离，统一回答「本租户全量」，不建授权引擎
> ```

---

## 0. 结论先行（GO / BLOCKED）

**GO。** 在严格约束下，Browser Authentication **只负责 L1 + L2**，L3/L4 完全复用 Cloud-0.1.1-B 已建成的 Tenant Isolation，因此：

- **问题链可闭合且不加新维度**：Browser 凭据 → 识别操作员（`AuthenticatedUser`）→ 取 `user.tenant_id` → `TenantScope` → 经 B 的 `functools.partial(UoW, tenant_id=scope.tenant_id)` 桥获得租户作用域 UoW → 该用户访问其租户的**统一全量**切片。
- **四者严格分离**可证成（§2）：Browser Authentication ≠ WeCom Authentication ≠ Agent Connection Authentication ≠ Tenant Isolation。本 Gate 只拥有人类 Web 控制台操作员身份识别；其余三者不在本 Gate 范围，且不得被 Browser Auth 污染。
- **七大禁区以静态护栏锁定**（§3 + `test_cloud_0_1_1_c_*`）：OAuth Platform / SSO / RBAC / Organization / SCIM / Enterprise IAM / JWT complexity 不会被顺手引入。

最终结构（与用户给定目标一致）：

```
            Browser (Web 控制台人类操作员)
                  │
                  │  L1 凭据 / 会话 Cookie
                  ▼
        BrowserAuthenticator.verify()        ← 本 Gate 唯一净新增（L2: Who）
                  │
                  ▼
        AuthenticatedUser(user_id, tenant_id)
                  │
                  │  L3 派生（复用 B，不重建）
                  ▼
        TenantScope(tenant_id = user.tenant_id)
                  │
                  │  functools.partial(UoW, tenant_id=scope.tenant_id)   ← B §7 桥
                  ▼
        tenant-scoped UoW  ──►  Repository 显式过滤 ⊕ PostgreSQL RLS   ← B 两层隔离（L4: What）
                  │
                  ▼
        本租户全量切片（无角色 / 无资源级 ACL）
```

---

## 1. 问题链（本 Gate 的唯一裁决标尺）

| 环节 | 由谁负责 | 本 Gate 是否净新增 | 说明 |
|---|---|---|---|
| **L1** Browser 呈交操作员身份证明 | `BrowserAuthenticator` + 登录端点 | ✅ 净新增 | 凭据 / 会话 Cookie |
| **L2** Who is this user? | `BrowserAuthenticator.verify()` → `AuthenticatedUser` | ✅ 净新增 | **本 Gate 唯一真正的新职责** |
| **L3** Which Workspace/Tenant? | `TenantScope(tenant_id = user.tenant_id)` | ❌ 复用 B | Browser Auth 是 tenant scope 的**生产者**，不是隔离机制 |
| **L4** What Cloud resources? | tenant-scoped UoW（B 两层隔离） | ❌ 复用 B | **统一回答「本租户全量」**，不建任何授权/角色引擎 |

**核心裁决**：C 只拥有 L1+L2。L3/L4 已在 B 中解决，**禁止在 C 内重建隔离或授权机制**。「这个请求能访问哪些 Cloud 资源」的答案，有且仅有：**它所属租户的统一全量切片**（与租户内任何代码路径拿到的一致）。没有 admin / 没有 reader / 没有 per-resource ACL。

---

## 2. 四者严格区分（不交叉污染）

| 概念 | 是什么 | 入口 | 信任依据 | 本 Gate 是否负责 |
|---|---|---|---|---|
| **Browser Authentication** | 识别使用 Web 控制台的人类操作员 | Web UI / `/api/auth/*` | 操作员凭据（密码 / 预共享密钥）→ 服务端会话 | ✅ **本 Gate** |
| **WeCom Authentication** | 识别入站 WeCom 消息来源 | `/api/channels/{id}/wecom/webhook` | WeCom 签名校验（`WeComAdapter.parse_message` → 失败抛 `InboundSignatureError`→403，已存在） | ❌ 不在本 Gate |
| **Agent Connection Authentication** | 识别入站 ACP / Agent 连接 | Agent 连接握手 | 连接凭据 / mTLS（未来 Step） | ❌ 不在本 Gate |
| **Tenant Isolation** | 数据分区隔离栅栏 | — | 持久层 `tenant_id` + RLS（B 已建成） | ❌ 已建成，复用 |

**交叉污染护栏（机器锁定）**：
- Browser Auth **绝不**在 WeCom webhook 路径或 Agent 连接路径上被调用，反之亦然。
- 三者必须落在**不同模块命名空间**：Browser Auth 在 `app/core/browser_auth.py`（或 `app/security/`）；WeCom 验签在 `app/adapters/channels/wecom.py`；Agent 连接鉴权在 agent adapter 层。互不 import 对方。
- 401（未登录，Browser 语境）与 403/404（`IngressTenantError`，B 的 tenant resolver 语境）是**不同语义**：Browser Auth 失败 → 401；tenant 解析失败 → 403/404。不得混用。

---

## 3. 明确 NOT building（禁止清单 + 理由）

以下七类**本次及 V0 均不引入**。一旦引入即视为违反本 Gate 红线，由 §10 静态护栏拦截：

| 禁区 | 为何不引入（V0 不需要 / 已被替代） |
|---|---|
| **OAuth Platform** | 不建授权服务器 / OP。仅做本地凭据校验（用户名+验证器）。第三方联合登录非 V0 需求。 |
| **SSO** | 不做跨系统单点登录 / 联合。单一 Control Plane 实例内的操作员认证即可。 |
| **RBAC** | 不建角色 / 权限引擎。「能访问哪些资源」由租户边界（B）统一回答，**无角色区分**。引入 RBAC 即重建 L4 授权，违反 §1。 |
| **Organization** | 不建组织层级模型。`tenant` 已是数据隔离根目录（B 的 `tenants` root catalog），不需要 org 树。 |
| **SCIM** | 不做用户供给协议。V0 的用户/凭据由 `TenantProvisioner`（B）或本地管理面创建，无需 SCIM 同步。 |
| **Enterprise IAM** | 不接入企业身份目录 / LDAP / AD。V0 自包含凭据即可。 |
| **JWT complexity** | 不用刷新令牌 / JWKS / 签名声明链。用**简单服务端会话 Cookie**（HttpOnly + SameSite=Strict + 服务端会话存储）。`session id` 不透明、非 JWT。无签名 claim 传递。 |

**判据（铁律）**：若实现里出现了 `oauth` / `sso` / `rbac` / `role` / `permission` / `scim` / `organization` / `iam` / `jwt.encode` / `jwks` 等符号，即判定为越界。

---

## 4. 最小 Browser Auth 设计（L1 + L2）

**组件（`app/core/browser_auth.py`，NEW）**：

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class AuthenticatedUser:
    """Browser Auth 的输出（L2）。仅携带身份事实，不含业务语义。"""
    user_id: int
    tenant_id: int          # ← L3 的直接输入（复用 B 的 TenantScope）

# Protocol / 组件：verify(proof) -> AuthenticatedUser
#   - 成功：返回 AuthenticatedUser
#   - 失败：抛 BrowserAuthError（HTTP 边界映射 401）
```

**凭据存储（加性，不污染 `User` 域）**：

> 纪律（与 B 的 `tenant_id` 同）：凭据只落持久层，**绝不进 `User` 域实体 / Mapper**。`User` 域实体（现存 `app/domain/user.py`：`name/id/status`）保持干净。

- **NEW `user_credentials` 表**：`(user_id PK/FK→users.id, verifier, ...)`。存放验证器（如 bcrypt 哈希）。与 `users` 正交，保持 `users` 模型与 User 域零改动。
- **NEW `browser_sessions` 表**：`(session_id PK, user_id FK, tenant_id, created_at, expires_at)`。服务端会话存储；Cookie 只携带**不透明 session_id**。无刷新令牌、无 JWKS。
- **OSS / 本地单租户回退**：两表存在但为空；未启用认证时回退 `TenantScope(TENANT_LOCAL)`（行为等同隔离前，既有 100+ 测试零改动）。

**登录流程（NEW `app/api/auth.py`）**：

```
POST /api/auth/login  {username, password}
   → 以 bootstrap 模式（跨租户按 name 定位 user，类比 B 的 tenant bootstrap 读）
   → 取该 user 的 user_credentials.verifier 校验
   → 校验成功：写 browser_sessions 一行，Set-Cookie(session_id, HttpOnly, SameSite=Strict[, Secure in prod])
   → 校验失败：401

POST /api/auth/logout
   → 删 browser_sessions 行，清 Cookie
```

**每请求依赖（`require_browser_session`，NEW）**：

```
读取 Cookie session_id → browser_sessions → user_id → users.tenant_id
   → TenantScope(tenant_id=user.tenant_id)        # L3 复用 B
   → uow_factory = functools.partial(SqlAlchemyUnitOfWork, tenant_id=scope.tenant_id)  # B §7 桥
```

---

## 5. 与 B 的接线（L3 复用，不重写）

- Browser Auth 路径对 B 而言就是 **`TenantResolver` 端口的一个浏览器实现**：它先认证操作员（L2），再从已认证用户的 `tenant_id` 产出 `TenantScope`。B 的 `TenantScope` / `TenantResolver` / `TenantProvisioner` / UoW `SET LOCAL` / RLS **全部复用，不重写**。
- **OSS/本地回退**：若 Cookie 缺失且未启用认证 → `TenantScope(TENANT_LOCAL)`（== 今日行为）。`main.py` 当前 `tenant_uow_factory = functools.partial(SqlAlchemyUnitOfWork, tenant_id=TENANT_LOCAL)` 在无认证模式下保持不变。
- **服务签名不变**：浏览器面 router 仍以 `service(uow_factory=...)` 注入；`require_browser_session` 在**请求作用域**解析并注入 tenant-scoped UoW。Application / Runtime / Domain / Adapter / SDK 全部零改动（同 B §13.2）。

---

## 6. API 接线（main.py 组合根变更，最小面）

- NEW `app/api/auth.py`：`/api/auth/login`、`/api/auth/logout` + `require_browser_session` 依赖。
- `main.py`：浏览器面 router（`/api/users`、`/api/channels`、`/api/agents`、`/api/connections`、`/api/identities`、`/api/interactions`）改为经 `require_browser_session` 注入 tenant-scoped UoW。**router / service 构造签名不变**（B 注入纪律）。
- WeCom webhook router（`/api/channels/{id}/wecom/webhook`）**不接** Browser Auth 依赖——它走自己的签名校验 + B 的 tenant 解析（§2 护栏）。
- Agent 连接鉴权路径独立（§2），不在本 Gate 改动。

---

## 7. Schema 影响（加性、本地兼容）

| 表 | 变更 | 说明 |
|---|---|---|
| `users` | **不变**（不加班内凭据列） | User 域与 ORM 保持干净；凭据在 `user_credentials` |
| `user_credentials` | NEW | `(user_id FK→users.id, verifier, ...)`，加性 |
| `browser_sessions` | NEW | `(session_id PK, user_id FK, tenant_id, created_at, expires_at)`，加性 |
| `tenants` / 8 Core Table | **不变** | L3/L4 复用 B，无 schema 变更 |

- `tenant_id` 本地兼容策略继承 B §2.1：`browser_sessions.tenant_id` 在本地可 `default=1`；OSS 路径行为与隔离前逐字节一致。
- **Alembic**：NEW `0003_browser_auth.py`（`down_revision="0002"`），仅建 2 张加性表。B 的 `0002_tenant_isolation.py` **不动**。

---

## 8. 红线（写死）

- **A（范围）**：Browser Auth 只回答 L1+L2；L3/L4 复用 B，禁止在 C 内重建隔离 / 授权机制（尤禁 RBAC）。
- **B（分离）**：Browser Auth 不得用于 WeCom webhook / Agent 连接路径；四者模块分离（§2）。
- **C（禁区）**：禁止引入 OAuth Platform / SSO / RBAC / Organization / SCIM / Enterprise IAM / JWT complexity（§3 机器护栏）。
- **D（域干净）**：凭据仅落 `user_credentials` 持久层；`User` 域实体与 `users` ORM 列集保持干净（同 B 红线：域不感知租户/凭据）。
- **E（会话）**：Cookie 必须 `HttpOnly` + `SameSite=Strict`（生产加 `Secure`）；session id 不透明、非 JWT；无刷新令牌 / JWKS。
- **F（本地兼容）**：OSS/本地未启用认证时回退 `TENANT_LOCAL`，既有 100+ 测试零改动（守 §5 / 同 B §14）。

---

## 9. NOT TOUCHED（冻结边界，复用 B §13.2 并扩展）

| 类别 | 文件 / 范围 | 理由 |
|---|---|---|
| **Domain 全树** | `app/domain/**`（含 `user.py`） | `User` 域实体不加凭据字段；`tenant_id` 不进 Domain（B 红线保留）。 |
| **Runtime 契约 / Application / Adapters / SDK / Worker / Driver** | 同 B §13.2 | 经 tenant-scoped UoW 获得隔离，逻辑与签名不变。 |
| **WeCom 验签 / Agent 连接鉴权** | `app/adapters/channels/wecom.py` + agent adapter 层 | 独立关注，不在此 Gate（§2）。 |
| **B 的 TenantScope / TenantResolver / RLS / UoW SET LOCAL** | `app/core/tenant.py`、`app/persistence/unit_of_work.py`、0002 | 复用，不重写（§5）。 |
| **8 Core Table + tenants schema** | B 已建 | L3/L4 复用，无 schema 变更（§7）。 |

---

## 10. 机器化护栏（Verification Gate）

新增 `tests/test_cloud_0_1_1_c_browser_authentication_gate.py`（**静态护栏，当前即可绿灯**，锁死 §3 禁区与 §2 分离）：

1. `test_no_forbidden_auth_concepts_in_backend`：扫描 `backend/app` 全文，禁止出现 `oauth` / `sso` / `rbac` / `role` / `permission` / `scim` / `organization` / `iam` / `jwt.encode` / `jwks` 等越界符号。
2. `test_browser_auth_distinct_from_wecom_and_agent`：断言 `app/adapters/channels/wecom.py`（WeCom 认证）不 import 任何 browser-auth 模块；三者命名空间分离。
3. `test_user_domain_has_no_credential_field`：`User` 域实体字段恰为 `{name, id, status}`，无密码/密钥/凭据字段（锁 §8 红线 D）。
4. `test_users_orm_has_no_inline_credential_column`：`users` ORM 列集不含 password/secret/credential（加性纪律，锁 §7）。
5. `test_browser_session_uses_opaque_cookie_not_jwt`：backend 无 `import jwt` / `jwt.encode` / `jwks` / `pyjwt` 使用（锁 §3 JWT 禁区）。
6. `test_tenant_isolation_reused_not_rebuilt`：浏览器认证路径复用 B 的 `TenantScope`（从 `app.core.tenant` 导入），且 backend 无为 Browser 新建的「第二套 tenant 原语」。
7. `test_local_single_tenant_fallback_preserved`：`TENANT_LOCAL == 1` 且 `default_tenant_scope` 可导入（锁 §8 红线 F）。

进入实现期时，本护栏保持绿；并按 B 的演进纪律，在落地后把 §4/§5/§6/§7 的「实现后不变量」（表存在、`require_browser_session` 产出 `TenantScope`、Cookie 标志位）补入该测试。

---

## 11. GO / 边界闭合

**GO。** C 仅拥有 L1+L2（操作员身份 + 派生 tenant_id）；L3/L4 复用 B（TenantScope + 两层隔离）。四者（Browser / WeCom / Agent Connection / Tenant Isolation）严格分离；七大禁区（OAuth Platform / SSO / RBAC / Organization / SCIM / Enterprise IAM / JWT complexity）以 §10 静态护栏机器锁定，不会被顺手引入。

进入实现期时，按 §4/§5/§6/§7 接线 + §10 验证门落地，且不得破坏 §8 红线与 §9 冻结清单。
