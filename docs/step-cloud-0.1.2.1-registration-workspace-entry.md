# Step Cloud-0.1.2.1 —— Registration → Workspace Entry

技术基线：Cloud-0.1.2（Workspace & Golden Path Architecture Gate，GO）→ **本步（最小实现）**。

一句话目标：**让 Cloud 用户「注册即拥有自己的 Workspace」**——一次 Sign Up 完成
`TenantProvisioner → User + Tenant → Browser Session → AuthenticatedUser → Workspace UI`，
用户不需要额外经历「创建 Workspace / 填 Workspace Name / Confirm」。

```
Browser
  ↓  POST /api/auth/signup {login, password}
TenantProvisioner.provision(name=login)      ← 唯一写 tenants 入口（bootstrap）
  ↓  TenantScope(tenant_id)                  ← 复用 Cloud-0.1.1-B
register(tenant_id=scope.tenant_id)
  ↓  User + user_credentials
Browser Session                              ← opaque session，注册即登录
  ↓
AuthenticatedUser(user_id, tenant_id)
  ↓
GET /api/auth/me                             ← Workspace UI 证明「已进入自己的 Workspace」
  ↓
Workspace UI（唯一核心 CTA：Connect Agent）
```

---

## 1. 本步验证的产品化假设

> **Cloud 用户注册之后，不需要额外「创建 Workspace」。**

即：

```
Sign Up → 我的 Workspace 已经存在 → Connect Agent
```

而**不是**：

```
Sign Up → Create Workspace → Workspace Name → Confirm → Dashboard
```

这条假设把 Golden Path 压到最短：Sign Up 天然产出一个 Tenant，用户随即可
`Connect Agent`（复用既有 Agent / Channel / Connection 管理面）。

---

## 2. 关键裁决（与 Gate 0.1.2 一致）

1. **Workspace ≡ Tenant，不新增表**：`TenantProvisioningService` 只写 `tenants`
   root catalog，**不创建** `workspaces` / `workspace_members` / `organizations` /
   `teams` 任何表或域实体。`Workspace` 只存在于前端（UX 抽象）。
2. **`TenantProvisioner` 是 `tenants` 的唯一写入口**：Sign Up 时 `app.tenant_id`
   未设 → 命中 bootstrap RLS 分支；普通业务事务（已 `SET LOCAL`）无权写 `tenants`。
   因此不引入 `WorkspaceProvisioner` / `OrganizationProvisioner` 第二套归属创建器。
3. **注册即登录**：`signup` 在一次用例内完成「建租户 → 建用户 + 凭据 → 建 opaque
   会话」，直接返回可写 Cookie 的 `BrowserSessionGrant`；用户落地即成已认证态。
4. **无需 Workspace Name**：租户名由注册输入派生（= `login`），用户不额外填写。
5. **失败不留孤儿租户**：`signup` 在 `provision` **之前**预检 `login` 唯一性
   （重复注册 → 409，不产生空租户）。
6. **后端零 Workspace 标识符**：`app/**` 真实代码标识符中不出现 `workspace`
   （由 Cloud-0.1.2 护栏锁定）——Workspace 是 UX 概念，不是后端实体。
7. **Cookie / 会话语义沿用 C.1**：opaque session id（非 JWT）+ `HttpOnly +
   SameSite=Strict + Path=/`；`GET /api/auth/me` 只回 `{user_id, tenant_id}`。

---

## 3. 变更文件

**NEW（后端）**

| 文件 | 内容 |
|---|---|
| `app/repositories/tenant.py` | `TenantRepository` 端口（唯一能力：`create`，返回 tenant_id） |
| `app/persistence/repositories/tenant.py` | `SqlAlchemyTenantRepository`（只 flush，不 commit） |
| `app/application/tenant_provisioning.py` | `TenantProvisioningService`（实现 `TenantProvisioner` 端口） |

**MODIFY（后端，最小面）**

- `app/repositories/unit_of_work.py`：Port 增加 `tenants: TenantRepository`。
- `app/persistence/unit_of_work.py`：`__enter__` 构造 `SqlAlchemyTenantRepository(session)`
  （root catalog，不带 tenant），`close` 清理。
- `app/application/browser_auth.py`：+`SignUpBrowserUser`；`BrowserAuthService.__init__`
  可选注入 `provisioner`；+`signup()`（建租户 + 建用户 + 建会话）。
- `app/schemas/auth.py`：+`SignUpRequest {login, password}`。
- `app/api/auth.py`：+`POST /api/auth/signup`（201 + Set-Cookie）、
  +`GET /api/auth/me`（200 / 401）。
- `app/main.py`：装配 `TenantProvisioningService` 并注入 `BrowserAuthService`。

**NEW（前端 —— Workspace UI，UX 抽象）**

| 文件 | 内容 |
|---|---|
| `frontend/workspace.html` | 未认证：Sign Up（仅 login + password）；已认证：Workspace 已就绪 + 唯一 CTA「Connect Agent」 |
| `frontend/workspace.js` | 只读 `GET /api/auth/session`；Sign Up 调 `POST /api/auth/signup`；无任何 Workspace 持久化 |

**MODIFY（样式，加性）**

- `frontend/styles.css`：+`.signup-lead` / `#cta-connect-agent` 两条最小样式（复用既有变量）。

---

## 4. 验证结果（owner 清单逐条机器化）

`tests/test_cloud_0_1_2_1_registration_workspace_entry.py`：

| # | 验证项 | 测试 |
|---|---|---|
| 1 | Sign Up 自动建租户（无需 tenant_id / 无需 Workspace Name） | `test_signup_provisions_new_tenant_without_explicit_tenant` |
| 2 | 建租户 → 建 User + 凭据（同租户） | 同上 |
| 3 | 注册即建 opaque 会话 | `test_signup_creates_opaque_session_and_grant` |
| 4 | `SignUpBrowserUser` 只有 login / password（无创建 Workspace 步骤） | `test_signup_input_has_no_workspace_step` |
| 5 | API：signup → 201 + Cookie + 已认证 Workspace | `test_signup_via_api_lands_authenticated_in_workspace` |
| 6 | `GET /api/auth/me` 返回会话的 user/tenant | 同上 |
| 7 | 未认证 → 401 | `test_me_endpoint_requires_authentication` |
| 8 | 重复注册 → 409，且不留孤儿租户 | `test_signup_duplicate_login_conflict_and_no_orphan_tenant` |
| 9 | `tenants` 唯一写入口 = `TenantProvisioner` | `test_tenant_provisioner_is_only_tenants_writer` |
| 10 | Workspace UI：唯一 CTA = Connect Agent | `test_workspace_ui_has_single_connect_agent_cta` |
| 11 | Workspace UI 无 Settings / Rename / Members / Invite / RBAC / Dashboard / Billing / Profile | `test_workspace_ui_excludes_forbidden_surfaces` |
| 12 | Workspace UI 只经 `/api/auth`（无 Dashboard 端点越界） | `test_workspace_ui_only_talks_to_auth_api` |
| 13 | 不引入 Workspace 持久化 / 第二套归属模型 | `test_no_workspace_persistence_introduced` |
| 14 | Workspace UI 由组合根静态托管且可达 | `test_workspace_page_is_served_by_app` |

**全量回归**：见本步执行结果（含 Cloud-0.1.2 护栏保持绿）。

---

## 5. 明确 NOT DOING（0.1.2.1 锁死）

Workspace settings / Workspace rename / Workspace members / Invite / RBAC /
Dashboard / Billing / Profile system / Organization / onboarding state persistence。

> 第一版 Workspace UI 只证明「用户已经进入自己的 Tenant Workspace」，并给出
> **唯一核心 CTA：Connect Agent**——这已足够。

---

## 6. NOT TOUCHED（冻结边界）

`app/domain/**`（域不感知 tenant / 凭据）、Runtime 契约、六段执行 / Coordinator /
Worker / Driver、Agent / Channel Adapter、`agent_runtime_sdk/**`、
Cloud-0.1.1-B 的 `TenantScope` / UoW `SET LOCAL` / 迁移 0002 RLS、
Cloud-0.1.1-C 的 opaque 会话与凭据表（0003）。

---

## 7. 后续（不属于本步）

`TenantResolver` 的多入口落地（Browser / WeCom / Agent connection 各自独立）、
按租户的 Runtime fan-out（`TenantFanoutDriver`）、登录体验优化（若真实需求出现）。
