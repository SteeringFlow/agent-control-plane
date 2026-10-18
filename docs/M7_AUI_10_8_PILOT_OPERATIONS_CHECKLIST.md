# M7-AUI-10.8 — Self-Hosting Runbook（自托管部署与运行手册）

> 适用范围：**自行部署和运维** SteeringFlow（ACP）的用户。
>
> 文档沿革：本路径由内部运维清单改写为公开自托管视角；原始内部清单完整保留于
> 私有治理文档（不随 OSS 发布）。本文只保留经核实且适用于自托管场景的内容。
>
> 证据约定：每条要求标注来源（【部署文件】/【代码】/【公开文档】）；
> 无法验证的事项标注【未验证】或列为前置条件，**不得当作已验证功能使用**。
>
> 重要范围说明：本仓库当前**不含 Web 控制台**（`frontend/` 不在发布范围内），
> 接入与管理经 Management API 与 CLI 完成；凡涉及浏览器页面的步骤均不适用于本发布形态。

---

## 0. 部署形态与公网边界

nginx 是唯一公网业务入口，TLS 在 nginx 终止；backend 仅 HTTP 内部通信，不发布宿主端口。

| 公网面 | 语义 |
|---|---|
| `GET/POST /api/channels/{id}/wecom/webhook` | **唯一 PUBLIC**（无 BasicAuth，WeCom 回调不携带 Operator 凭据） |
| `/health` | 经 nginx **永不代理**（公网 404）；健康验证归 Docker healthcheck（容器内直达） |
| 其余一切（`/api/**`、`/docs`、`/redoc`、`/openapi.json` 等） | BasicAuth（`auth_basic` + proxy_pass） |

- 禁止任何更宽的 `/api/channels/` 前缀放行（会误伤 Management channels API）。
- 依据：【公开文档】`_deploy/nginx/README.md` §1/§5；`_deploy/nginx/acp-pilot-vhost.conf.template`。

## 1. 前置条件

1. **服务器侧证书**：certbot HTTP-01 签发；首签需临时发布 `:80`（compose 默认只发布 443）。
   【公开文档】`_deploy/nginx/README.md` §3。
2. **setup 脚本 5 个必需环境变量**（fail-closed，任一缺失即退出，无隐式默认）：

   | 变量 | 说明 |
   |---|---|
   | `ACP_DOMAIN` | 公网域名（server_name） |
   | `ACP_TLS_CERT_PATH` | TLS fullchain 路径（certbot 产物） |
   | `ACP_TLS_KEY_PATH` | TLS 私钥路径（**永不入 git**） |
   | `ACP_BASIC_AUTH_USER` | BasicAuth 用户名 |
   | `ACP_BASIC_AUTH_PASS` | BasicAuth 口令（仅经环境变量注入，绝不入 git） |

   可选变量（有默认值）：`ACP_UPSTREAM`（默认 `http://backend:8000`）、
   `ACP_ACME_WEBROOT`（默认 `/var/www/certbot`）、`ACP_HTPASSWD_PATH`、
   `NGINX_BIN`。【公开文档】`_deploy/nginx/README.md` §2。
3. Docker 与 compose 可用。

## 2. 环境变量完整性与 fail-closed 约束

| 变量 | 必需性 | 说明 |
|---|---|---|
| `POSTGRES_PASSWORD` | **必需**（缺失启动失败 `${VAR:?}`） | 迁移/admin 角色 |
| `POSTGRES_APP_PASSWORD` | **必需**（缺失启动失败 `${VAR:?}`） | backend 运行时非-superuser 角色（RLS 第二层防线对 superuser 失效，故必须分离） |
| `POSTGRES_USER` / `POSTGRES_APP_USER` | 可选 | 默认 `acp` / `acp_app` |
| `ENVIRONMENT` | 生产应为 `production` | Browser Cookie 才带 `secure=True` |
| `AUTH_REQUIRED` | 生产应为 `true` | 多租户鉴权；`false` 时无 Cookie 请求回退 `TENANT_LOCAL`（不可归属主体） |
| `GIT_SHA` | 强烈建议 | build 期注入；缺失回退 `unknown`，`/api/version` 将无法建立 runtime→commit 溯源 |

- 注入方式：仅宿主 shell / 项目根 `.env`（已 gitignore），**绝不入 git**。
- 依据：【部署文件】`docker-compose.production.yml`（env 段与启动注释）、
  `docker-compose.cloud.yml`；【代码】`backend/app/core/config.py`。

## 3. 启动

```bash
# 1) 渲染 vhost + 生成 htpasswd + nginx -t（任何一步失败即中止）
export ACP_DOMAIN=...                 # 真实域名，只在服务器 shell 注入
export ACP_TLS_CERT_PATH=...          # certbot fullchain 路径
export ACP_TLS_KEY_PATH=...           # 私钥路径（永不入 git）
export ACP_BASIC_AUTH_USER=...
export ACP_BASIC_AUTH_PASS=...        # 仅环境变量注入
bash _deploy/setup_nginx_pilot.sh

# 2) 启动（迁移角色 = admin，运行时角色 = app）
GIT_SHA=$(git rev-parse HEAD) docker compose \
  -f docker-compose.cloud.yml \
  -f docker-compose.yml \
  -f docker-compose.production.yml \
  up -d --build
```

- Schema 由启动命令内 `alembic upgrade head` 管理；Cloud 模式**绝不 `create_all`**。
- 产物落在 `_deploy/nginx/runtime/`（**不入 git**）：渲染后 vhost、TLS 材料、htpasswd。
- 依据：【部署文件】`docker-compose.production.yml` 启动注释；【公开文档】`_deploy/nginx/README.md` §3。

## 4. 服务与数据库健康检查

| 检查 | 命令 / 期望 | 依据 |
|---|---|---|
| 容器健康 | `docker compose ... ps` → backend / postgres / nginx 均 healthy | 【部署文件】`docker-compose.yml`（healthcheck：interval 30s、retries 3） |
| 健康探针语义 | backend healthcheck 打**容器内** `http://127.0.0.1:8000/health`；DB 不可达即 503 → unhealthy | 【代码】`backend/app/api/health.py`、`docker-compose.yml` |
| 迁移到位 | `docker compose ... exec backend alembic current` == `alembic heads`（单一 head） | 【部署文件】启动命令内 `alembic upgrade head` |
| 部署版本溯源 | `GET /api/version` 返回的 `commit` == 构建时注入的 `GIT_SHA` | 【代码】`backend/app/api/version.py`（公开端点，无认证依赖） |
| 日志无异常 | `docker compose ... logs backend` 无迁移失败、无订阅反复重连刷屏 | 运维常规 |

- `/health` 只回答一个问题：应用进程能否访问数据库（200 / 503）；刻意不检查外部依赖；
  公网不可达（nginx 404）属预期语义，非故障。【代码】`backend/app/api/health.py`。

## 5. Channel / Agent 接入（API / CLI 路径）

本发布形态不含 Web 控制台，接入经 **Management API** 与 **CLI**（`cli/acp_cli.py`，stdlib-only）完成。

**CLI 能力（冻结合同，勿假设范围外功能）**：五资源 `users / agents / channels /
identities / connections` 的 `list / get / create` 与 `status` 观测；
目标地址经 `--base` 或 `ACP_BASE_URL` 指定。CLI **不含** `send / update / delete /
init / start / webhook`（冻结排除项）——发消息需经 API/curl。【代码】`cli/acp_cli.py`。

**接入顺序（铁律）**：

1. 创建 Agent（Agent ID / API Key；凭据只写不回显，`sanitize_channel_config` 守护）并经测试连接通过。
2. 创建 Channel（WeCom Bot 形态：Bot ID / Bot Secret；同样不回显）并经测试连接通过。
3. 创建 Connection（`channel_id` + `agent_id`）。
4. `POST /api/connections/{connection_id}/bootstrap-arm`（201 / 401 / 404）。
5. 在企业微信给该机器人发一条**单聊**消息 → Arm 在 600s 窗口内被该消息原子消费，
   建立 Identity（`user_id=NULL`）+ Grant。

- **单聊限定**：Arm 不支持群聊（`chat_type != single` 不消费）。
- **同一外部 Bot 身份只能有一条启用中的渠道**：重复创建返回 409 并给出复用指引
  （`DuplicateBotChannelError`）；不要为同一机器人建第二条渠道。
- 依据：【代码】`backend/app/api/management/bootstrap_arms.py`、
  `backend/app/application/bootstrap.py`、`backend/app/schemas/channels.py`。

**【未验证】**：fresh 环境从零到首条消息的端到端部署尚未作为公开流程完整演练；
首次执行时请以 §4 检查表逐项核验，并先在小规模验证后再扩大使用。

## 6. WeCom Bot 单实例约束

- **backend 必须单实例运行**（勿扩容/多副本）：`aibot_respond_msg` 必须经本进程持有的
  活 WebSocket 连接发送；多副本时持有连接的进程未必是执行 Delivery 的进程 → 投递 Fail Closed。
- `WeComBotTransport.reconcile()` 周期调和只做 ADD / KEEP / REMOVE（按 `channel_id`），
  **不解决**多副本 / 分布式连接所有权。
- 依据：【代码】`backend/app/adapters/channels/wecom_bot.py` 模块 docstring（既有冻结前提，
  现状仍成立）。

## 7. 常见失败与排查入口

| 现象 | 排查 / 恢复入口 | 依据 |
|---|---|---|
| 首条消息失败，stage=`identity_resolution` / `authorization`（如 Arm 过期未消费） | 重新调用 `POST /api/connections/{id}/bootstrap-arm` 刷新窗口（重复 arm = supersede），并让用户**立刻重发一条新的单聊消息**（600s 窗口内） | 【代码】`bootstrap_arms.py`、`application/bootstrap.py` |
| 渠道保存 409（同名 / 同一企业微信机器人） | Bot 冲突 → 复用已有渠道或先停用；同名 → 更换渠道名 | 【代码】`DuplicateBotChannelError` / `DuplicateChannelName` 语义 |
| WorkBuddy 身份已被其它 Agent 记录使用 | 409；选择"使用已有 Agent"或取消，后端绝不自动重试 | 【代码】connections 管理 API |
| 认证失效（401） | 会话过期，重新登录（`/api/auth/*`；注册即建租户 + 会话） | 【代码】`backend/app/api/auth.py` |
| Worker 执行卡住 / 孤儿 | Driver 自动回收重跑，**无需人工改库** | 【代码】异步执行所有权机制 |

**任何正常场景都不需要人工修改数据库**；仅"清理误建对象"需经 Management API 显式操作。

## 8. 运维纪律

- `runtime/`（TLS 私钥 / htpasswd / 渲染 vhost）**永不入 git**；服务器磁盘上存在属正常。
- 凭据只经环境变量注入；仓库内不得出现 htpasswd / 私钥 / 证书 / 明文口令。
- **备份与恢复**：本仓库**未内置**备份 / 恢复流程；数据库备份策略（dump 周期、保留、
  异地副本）与恢复演练由部署方自行决定、实施并验证。本文不提供恢复保证。

## 9. 已知限制与未验证项

| 项 | 状态 |
|---|---|
| Web 控制台 | 不在本发布范围（`frontend/` 不含）；UI 流程不适用，接入走 API/CLI |
| CLI 覆盖面 | 无 `send / update / delete`（冻结排除项）；发消息经 API/curl |
| 多实例 / 扩容 | **不支持**（见 §6 单实例约束） |
| 群聊接入 | 不支持（Arm 仅消费单聊） |
| 端到端首部署 | 【未验证】（见 §5）——请按 §4 检查表逐项核验 |
| 备份 / 恢复 | 【未定义于本仓库】——部署方责任（见 §8） |
