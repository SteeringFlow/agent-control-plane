# M7-AUI-10.8 — Pilot Operations Checklist（运营方托管部署 · 受控 Pilot 交付清单）

> 适用范围：**运营方托管部署**的第一批 3–5 名真实用户（M7-AUI-10.8 项目决策）。
> 用户通过浏览器完成 Channel / Agent 接入；Pilot 首次接入路径 = **企业微信单聊**。
> 本清单不适用于客户自助部署；**客户自助部署（cloud-0.2.3 Gate 模型）未通过验收**，
> `docs/pilot-walkthrough.md` 与 16.1.3 部署边界的失真问题对托管模式不构成阻断，
> 但仍待修（见 M7-AUI-10.7 §5 差异清单）。
>
> 证据约定：每节标注事实来源（【代码】/【部署文件】/【运行证据】）；
> 标注【未验证】的项不得当作已确认事实使用。

---

## 1. 认证与生产环境配置

| 检查项 | 要求 | 来源 |
|---|---|---|
| `AUTH_REQUIRED` | 必须为 `true`——多租户鉴权；`false` 时无 Cookie 请求回退 `TENANT_LOCAL`（不可归属主体） | 【部署文件】`docker-compose.production.yml:57`；【代码】`app/core/config.py:40`、`app/main.py:203-204` |
| `ENVIRONMENT` | 必须为 `production`——Browser Cookie 才带 `secure=True` | 【部署文件】`docker-compose.production.yml:56`；【代码】`config.py:33` |
| 认证形态 | Cookie = opaque session id（落库，`browser_sessions` 表）；**无 cookie secret 需求**；注册即建租户 + 会话（`POST /api/auth/signup`，HttpOnly + SameSite=Strict） | 【代码】`app/api/auth.py`、alembic `0003_browser_auth` |

## 2. 启动必需环境变量（缺一即启动失败或裸奔）

组合命令（【部署文件】`docker-compose.production.yml:6-11`）：

```bash
GIT_SHA=$(git rev-parse HEAD) docker compose \
  -f docker-compose.cloud.yml \
  -f docker-compose.yml \
  -f docker-compose.production.yml \
  up -d --build
```

| 变量 | 必需性 | 说明 |
|---|---|---|
| `POSTGRES_PASSWORD` | **必需**（缺失启动失败 `:?`） | 迁移/admin 角色（`docker-compose.production.yml:61`） |
| `POSTGRES_APP_PASSWORD` | **必需**（缺失启动失败 `:?`） | backend 运行时非-superuser 角色（RLS 第二层防线对 superuser 失效，故必须分离） |
| `POSTGRES_USER` / `POSTGRES_APP_USER` | 可选（有默认值 `acp` / `acp_app`） | |
| `GIT_SHA` | 强烈建议 | build-arg；缺失回退 `unknown`，`/api/version` 将无法建立 runtime→commit 溯源 |
| 凭据注入方式 | 仅宿主 shell / 项目根 `.env`（已 gitignore），绝不入 git | `docker-compose.production.yml:24-29` |

Schema 由**启动命令内 `alembic upgrade head`** 管理（迁移角色 = admin，运行时角色 = app）——Cloud 模式**绝不 create_all**（`docker-compose.production.yml:65-68`、`docker-compose.cloud.yml:54-57`）。

## 3. TLS / Nginx / htpasswd 前置条件

事实来源：【部署文件】`_deploy/nginx/README.md`（本文档与实现一致，为准绳）。

1. **证书先于脚本**：服务器侧完成 certbot（HTTP-01）签发；首签需临时发布 :80（compose 默认只发布 443）。`setup_nginx_pilot.sh` **不做证书签发**（仅检查存在）。
2. 脚本 5 个必需环境变量（fail-closed）：`ACP_DOMAIN` / `ACP_TLS_CERT_PATH` / `ACP_TLS_KEY_PATH` / `ACP_BASIC_AUTH_USER` / `ACP_BASIC_AUTH_PASS`（`_deploy/nginx/README.md` §2、脚本 L37-41）。
3. 运行 `bash _deploy/setup_nginx_pilot.sh`（无位置参数）→ 渲染 vhost + 生成 htpasswd + `nginx -t`。
4. 公网面语义（冻结）：`/api/channels/{id}/wecom/webhook` 唯一 PUBLIC；`/health` 经 nginx 永不代理（404）；其余一切 BasicAuth。**注意：`wecom_bot` 是 WebSocket 长连接（backend 出站），不经 nginx 公网入口**——nginx 直通面只服务 `wecom` webhook 形态。
5. `runtime/`（TLS 私钥/htpasswd/渲染 vhost）永不入 git；服务器磁盘上存在属正常。

## 4. `wecom_bot` 单实例部署红线

- **backend 必须单实例运行**（当前 compose 即单副本，勿扩容/多副本）。
- 原因（【代码】`app/adapters/channels/wecom_bot.py` 模块 docstring，既有冻结前提）：`aibot_respond_msg` 必须经**本进程持有的活连接**发送；多副本时持有连接的进程未必是执行 Delivery 的进程 → 投递 Fail Closed。
- `WeComBotTransport.reconcile()` 周期调和只做 ADD/KEEP/REMOVE（按 channel_id），不具备多副本连接所有权能力（M6.6-F2 冻结边界）。

## 5. 数据库与服务启动检查

| 检查 | 命令 / 期望 | 来源 |
|---|---|---|
| 容器健康 | `docker compose -f ... ps` → backend / postgres / nginx 均 healthy（backend healthcheck 打 `/health`，DB 不可达即 503→unhealthy） | 【部署文件】`docker-compose.yml:58-69`、【代码】`app/api/health.py` |
| 迁移到位 | `docker compose ... exec backend alembic current` == `alembic heads`（单一 head） | 【部署文件】启动命令内 `alembic upgrade head` |
| 部署版本溯源 | `GET /api/version` 返回的 commit == `GIT_SHA` | 【代码】`app/api/version.py`（M5.2） |
| 日志无异常 | `docker compose ... logs backend` 无迁移失败 / 订阅反复重连刷屏 | 运维常规 |

## 6. Channel / Agent 接入步骤（用户经浏览器完成）

1. 用户浏览器打开站点 → `/workspace.html` 注册（注册即建租户）→ 自动进入 `/home.html` Flow Home。【代码】`app/api/auth.py:110-126`、`frontend/workspace.js`
2. Home「连接你的 Agent」→ 选 WorkBuddy → 填 Agent ID / API Key（凭据只写不回显）→ 测试连接（Verify）。【代码】`frontend/home.js`（M7-AUI-7/10.2/10.4）
3. Home「连接你的渠道」→ 接入模式选企业微信机器人（`wecom_bot`）→ 填 Bot ID / Bot Secret → 测试连接（保存渠道）。【代码】`home.js` 渠道面板；凭据经 `sanitize_channel_config` 永不回显（【代码】`app/schemas/channels.py`）
4. Home「把它们连接起来」→ verify 通过后自动 `POST /bootstrap-arm`（顺序铁律）。【代码】`home.js`（`verify → bootstrap-arm`）
5. **在企业微信里给该机器人发一条单聊消息** → Bootstrap Arm 在窗口内（600s）被该消息原子消费：建立 Identity(user_id=NULL) + Grant。【代码】`app/application/bootstrap.py:103-137`
   - ⚠️ **单聊限定**：Bootstrap Arm 不支持群聊（`chat_type != single` 不消费）；群聊授权路径不在本轮 Pilot 范围。
   - 同一外部 Bot 身份只能有一条启用中的渠道（重复创建会被 409 拒绝并给出复用指引）——**不要**为同一机器人建第二条渠道。

## 7. 首次真实消息与 delivered 验证

1. 用户在 Home 点击「检查首次成功」（`GET /api/interactions` 按当前渠道过滤）。
2. 判定 = 该渠道存在 `processing_status === "succeeded" && delivery_status === "delivered"` 的 Interaction（双状态，ACK≠执行）。【代码】`home.js` checkFirstSuccess（与 0.1.5 Gate G5 语义一致）
3. 后台复核（运营者）：Observation 面（interactions.html）查看双状态 + Activity Timeline；或 `GET /api/interactions?limit=200`。【代码】`app/api/management/interactions.py`（13.x 读面）

## 8. 常见失败的恢复入口

| 现象 | 恢复入口 | 来源 |
|---|---|---|
| 首条消息失败，stage=`identity_resolution` / `authorization`（如 Arm 过期未消费） | Home「检查首次成功」→「重新验证并引导」（只复用既有 Connection，verify → 重新 arm → 用户**立刻**重发一条新的单聊消息，600s 窗口内） | 【代码】`home.js:1341-1395`；`bootstrap_arms.py:73-99`（重复 arm = supersede 刷新窗口） |
| 渠道与 Agent 之间没有 Connection | 恢复入口会明确提示「请先在下方完成连接」（绝不自动创建） | 【代码】`home.js:1363-1371` |
| 渠道保存 409（同名 / 同一企业微信机器人） | 面板内显示对应指引：Bot 冲突 → 复用已有渠道或先停用；同名 → 换名或到「高级 → Channels」改名 | 【代码】`home.js` `channelErrorMessage`（10.8 P1-1）；后端 `DuplicateBotChannelError` / `DuplicateChannelError` |
| WorkBuddy 身份已被其它 Agent 记录使用 | Home 冲突块「使用已有 Agent / 取消」双按钮；后端 409 兜底绝不自动重试 | 【代码】`home.js`（M7-AUI-10.5）、`connections.py` |
| Worker 执行卡住 / 孤儿 | Driver 自动回收重跑（Observation 面有「已执行 Ns」提示）；无需人工改库 | 【代码】12.9.4.x 异步可靠性（机器测试覆盖） |
| 认证失效 | Home 401 → 回 `/workspace.html` 重新登录 | 【代码】`home.js:1476-1482` |

**无任何正常场景需要人工修改数据库**（Arm 过期可 re-arm、消费+授权原子、Grant/Connection 缺失均有 UI 出路）——仅「清理误建对象」需到「高级 → Channels / Agents」手动处理。

## 9. Pilot 结束：问题记录与反馈收集

- **问题记录**：以 Interaction / Activity 数据为准（Observation 面只读导出/截图）；每次 Check-in 记录「现象 + Interaction 双状态 + failure 三元组」。
- **反馈收集**：跟随 0.2.3 Gate §5 的 Check-in 方式（开放问题、被动观察），不引入功能问卷；证据标注 prompted/unprompted。
- **数据保留**：Pilot 期间**不清库、不删历史 Interaction**；结束后是否导出/归档由运营决策（当前无既定清理流程——【未定义】）。
- **部署环境处置**：Pilot 结束后环境保留或回收属运营决策（【未定义】）；无论何种处置，`runtime/` 凭据材料按 nginx README 纪律处理（不入 git）。

## 10. 明确的未知项 / 未验证项

| 项 | 状态 |
|---|---|
| 真实域名 / 证书签发 / nginx 具体运维操作 | 属服务器侧运维（nginx README 为准绳），本清单不重复；**首次在 Pilot 环境执行的端到端部署演练【未验证】**（现有托管环境为既有部署，升级到含 10.6.7/10.8 前端改动的版本后需重启生效） |
| 多实例 / 扩容 | 红线禁止（§4），无支持 |
| 备份 / 恢复（PG dump 策略） | 【未定义】无既定流程，Pilot 前建议运营方自行决定最低要求 |
| Pilot 用户配额 / License 约束 | 当前代码未见配额层（【未验证】是否需要）；3–5 人规模依赖运维纪律 |
| 真实浏览器矩阵（Safari/Firefox 等） | 【未验证】——前端验收基于 Node VM 行为级 + 静态守卫，非真实浏览器证据 |
| 群聊接入 | 本轮范围外（Arm 不支持群聊） |
