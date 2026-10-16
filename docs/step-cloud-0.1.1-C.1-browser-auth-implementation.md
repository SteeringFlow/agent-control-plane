# Step Cloud-0.1.1-C.1 —— Browser Authentication Minimal Implementation

技术基线：Cloud-0.1.1-A（PostgreSQL + Alembic）→ Cloud-0.1.1-B（Tenant Isolation）→
Cloud-0.1.1-C（Browser Authentication Architecture Gate，GO）→ **本步（最小实现）**。

一句话目标：**让一个 Browser 请求可靠地产生 `AuthenticatedUser → TenantScope`，
并无缝进入 B 已冻结的 Tenant Isolation**——只做这一件事，不做别的。

```
Browser
  ↓  Cookie（opaque session id）
BrowserAuthenticator.verify()
  ↓  Who is this user?
AuthenticatedUser(user_id, tenant_id)
  ↓  Which Workspace/Tenant?（复用 app.core.tenant）
TenantScope(tenant_id)
  ↓  What resources?（复用 Cloud-0.1.1-B 两层隔离）
existing tenant-scoped UoW
```

---

## 1. 关键裁决（与 Gate 一致）

1. **凭据只落 `user_credentials`，绝不进 `users`**：`User` 域实体（`name/id/status`）
   与 `users` ORM 列集均保持干净（与 B 的 `tenant_id` 同纪律：持久层承载，域不感知）。
2. **auth 表不用 per-tenant RLS（bootstrap 访问）**：登录 / 会话解析发生在 tenant
   解析**之前**，且 `users` 受 RLS 保护（bootstrap 时不可读），故 `tenant_id` 直接由
   auth 记录给出。第二层隔离的真正对象仍是 8 张 Core Table（B，零重写）。
3. **opaque 会话，无 JWT**：`session_id` = URL-safe base64 随机字节；无签名声明链 /
   无 JWKS / 无 refresh_token。Cookie = `HttpOnly + SameSite=Strict + Path=/`（生产加 `Secure`）。
4. **口令用 stdlib pbkdf2**：`hashlib.pbkdf2_hmac(sha256)` + 随机 salt +
   `hmac.compare_digest`；不引入 bcrypt/passlib/JWT 等依赖。
5. **唯一认证开关 `AUTH_REQUIRED`**（默认 `False`）：
   - `False`（OSS / 本地单租户）→ 无 Cookie 回退 `TENANT_LOCAL`，行为与隔离前逐字节一致；
   - `True`（Cloud 多租户）→ 无 Cookie / 无效 Cookie → **401**。
6. **tenant 来源＝已认证用户**：请求经 `TenantScope` → `functools.partial(UoW, tenant_id=...)`
   → 管理面服务每请求构造（`service_provider` seam，复用 B §7 桥）。Runtime 面不动。
7. **401 ≠ 403**：Browser Auth 失败一律 401；403 保留给「已认证但无权限 / tenant 解析失败」
   （WeCom 入站验签用 403）。两者不混。

---

## 2. 变更文件

**NEW**

| 文件 | 内容 |
|---|---|
| `app/core/browser_auth.py` | `AuthenticatedUser` / `BrowserAuthError` / `CredentialRecord` / `SessionRecord` / `BrowserAuthenticator` Port（纯契约） |
| `app/core/passwords.py` | `hash_password` / `verify_password`（stdlib pbkdf2） |
| `app/persistence/models/credential.py` | `UserCredentialModel`（`user_credentials`） |
| `app/persistence/models/browser_session.py` | `BrowserSessionModel`（`browser_sessions`） |
| `app/repositories/browser_auth.py` | `BrowserAuthRepository` Port（bootstrap 查找，不按 tenant 过滤） |
| `app/persistence/repositories/browser_auth.py` | `SqlAlchemyBrowserAuthRepository`（Adapter） |
| `app/application/browser_auth.py` | `BrowserAuthService`（register / login / verify / authenticate_session / logout） |
| `app/schemas/auth.py` | `RegisterRequest` / `LoginRequest` / `AuthUserResponse` |
| `app/api/auth.py` | `build_auth_api` + `build_tenant_scope_provider`（Cookie → TenantScope） |
| `alembic/versions/0003_browser_auth.py` | 0002 → 0003：新增 2 张加性表（无 RLS） |
| `tests/test_cloud_0_1_1_c_browser_auth_implementation.py` | 实现验证门（13 项） |

**MODIFY（最小面）**

- `app/persistence/models/__init__.py`：导出 2 个新模型。
- `app/persistence/unit_of_work.py` + `app/repositories/unit_of_work.py`：
  UoW 增加 `browser_auth` 仓库（构造不带 tenant）。
- `app/application/errors.py`：+`DuplicateBrowserLoginError` / `InvalidBrowserAuthInputError`。
- `app/api/management/{agents,channels,connections,identities,users,interactions}.py`：
  各 router 增加可选 `service_provider`（组合根注入 tenant-scoped 服务；不传退回注入 service，
  既有测试零改动）。
- `app/core/config.py`：+`AUTH_REQUIRED`（默认 False）。
- `backend/.env.example`：+`AUTH_REQUIRED=false`。
- `app/main.py`：装配 auth router + tenant 解析依赖；管理面服务改为**每请求**由
  `TenantScope` 派生（Runtime 面保持 `TENANT_LOCAL` 不变）。

---

## 3. 验证结果（owner 清单逐条机器化）

`tests/test_cloud_0_1_1_c_browser_auth_implementation.py`：

| # | 验证项 | 测试 |
|---|---|---|
| 1 | 注册 / 创建最小 Browser User | `test_register_creates_user_and_credential_separately` |
| 2 | credential 存储不进入 users | 同上（`users` 列集无凭据列） |
| 3 | 登录成功建立 opaque browser session | `test_login_creates_opaque_browser_session` |
| 4 | Cookie 属性正确 | `test_login_sets_cookie_attributes` |
| 5 | session → user 正确解析 | `test_verify_resolves_session_to_user_and_tenant` |
| 6 | user → tenant 正确产生 TenantScope | `test_provider_derives_tenant_scope_from_session` |
| 7 | 请求进入既有 tenant-scoped UoW | `test_request_enters_existing_tenant_scoped_uow_and_isolates` |
| 8 | A tenant 无法访问 B tenant | 同上（tenant 2 看不到 tenant 1 数据） |
| 9 | 未认证 → 401 | `test_unauthenticated_returns_401_when_auth_required` |
| 10 | 不涉及授权的 403 语义不被混入 | `test_bad_credentials_return_401_not_403` / `test_wecom_403_semantics_still_distinct` |
| 11 | OSS TENANT_LOCAL fallback 行为不变 | `test_oss_local_fallback_uses_tenant_local` |
| 12 | Runtime / Agent / SDK / Worker / Driver 零改动 | `test_runtime_worker_driver_not_coupled_to_browser_auth` |
| 13 | B 的 RLS/UoW 实现 零重写 | `test_b_tenant_isolation_implementation_untouched` / `test_auth_migration_0003_additive_no_rls` / `test_uow_pg_connection_pool_justifies_set_local_unchanged` |

**全量回归**：`1776 passed, 5 skipped, 0 failed`（含 schema 基线锁按 B §19.2 方法论的合法演进）。

---

## 4. 明确 NOT DOING（登录体验优化 —— 本步不碰）

密码重置 / 邮箱验证 / Remember Me / OAuth 登录 / SSO / 设备管理 / Session 管理 UI /
多凭据 / 会话过期轮换 / 角色权限（RBAC）。

C 当前要证明的**不是**「做了一个完整账号系统」，而是「Browser 请求能否可靠产生
`AuthenticatedUser → TenantScope` 并进入 B 的隔离」——这才是 Cloud-0.1.1-C 的技术闭环。

## 5. NOT TOUCHED（冻结边界）

`app/domain/**`（域不感知 tenant / 凭据）、Runtime 契约、六段执行 / Coordinator /
Worker / Driver、Agent / Channel Adapter、`agent_runtime_sdk/**`、
B 的 `TenantScope` / UoW `SET LOCAL` / 迁移 0002 RLS。

## 6. 后续（不属于本步）

登录体验优化（若真实需求出现）、Cloud 多租户的入口凭据解析（Browser / WeCom / Agent
connection 各自独立）、`TenantProvisioner` 的 bootstrap 落地。
