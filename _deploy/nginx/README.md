# ACP Pilot nginx Deployment（Step 16.1.3.3）

Pilot 公网边界的可版本化部署材料。语义冻结自 **Step 14.2.3.C.3**（未放宽）。

## 1. Deployment Topology

```
Internet
  ↓  HTTPS :443
nginx (compose service, nginx:alpine)
  ├── GET/POST /api/channels/{id}/wecom/webhook → PUBLIC（无 BasicAuth，直通）
  ├── /health                                    → 404（internal-only，永不代理）
  └── 其余一切（/api/**、/、/docs、/redoc、/openapi.json）→ BasicAuth
  ↓  acp_net（Docker 内部网络）
backend:8000（expose only，无任何 host port）
```

- backend 不再 publish host port；只在 `acp_net` 内部可达。
- nginx 是唯一公网业务入口，TLS 在 nginx 终止；backend 仅 HTTP 内部通信。
- 不引入 service mesh / mTLS / K8s / 防火墙编排（Compose MVP 冻结）。

## 2. Required Environment Variables（setup_nginx_pilot.sh）

| 变量 | 必需 | 说明 |
| --- | --- | --- |
| `ACP_DOMAIN` | 是 | 公网域名（server_name） |
| `ACP_TLS_CERT_PATH` | 是 | TLS fullchain 路径（服务器侧 certbot 产物） |
| `ACP_TLS_KEY_PATH` | 是 | TLS 私钥路径（永不入 git） |
| `ACP_BASIC_AUTH_USER` | 是 | Operator 用户名 |
| `ACP_BASIC_AUTH_PASS` | 是 | Operator 口令（仅经环境变量注入，绝不入 git） |
| `ACP_UPSTREAM` | 否（默认 `http://backend:8000`） | proxy_pass 目标 |
| `ACP_ACME_WEBROOT` | 否（默认 `/var/www/certbot`） | ACME HTTP-01 webroot |
| `ACP_HTPASSWD_PATH` | 否（默认 `/etc/nginx/acp-auth/acp.htpasswd`） | htpasswd 输出路径 |
| `NGINX_BIN` | 否（默认 `nginx`） | nginx 二进制（容器内测试时指向容器） |

## 3. Provisioning Steps（fresh clone → 运行）

```bash
# 1. 服务器侧完成证书 provisioning（certbot HTTP-01，沿用历史方式；
#    首签时需临时发布 :80 —— compose 默认只发布 443）
export ACP_DOMAIN=...            # 真实域名，只在服务器 shell 注入
export ACP_TLS_CERT_PATH=/etc/letsencrypt/live/<domain>/fullchain.pem
export ACP_TLS_KEY_PATH=/etc/letsencrypt/live/<domain>/privkey.pem
export ACP_BASIC_AUTH_USER=operator
export ACP_BASIC_AUTH_PASS=...   # 仅环境变量注入

# 2. 渲染 + 生成 htpasswd + nginx -t 纪律（任何一步失败即中止）
bash _deploy/setup_nginx_pilot.sh

# 3. 启动
docker compose up -d
```

产物全部落在 `_deploy/nginx/runtime/`（**不入 git**，见根 `.gitignore`）：

```
runtime/rendered/acp-pilot-vhost.conf   # 渲染后 vhost（无占位符残留）
runtime/tls/                            # 证书/私钥（provisioning 放置）
runtime/auth/acp.htpasswd               # runtime 生成
```

## 4. TLS / BasicAuth 责任边界

| 项 | 责任方 |
| --- | --- |
| TLS 终止 | nginx（backend 仅内部 HTTP） |
| 证书签发/续期 | 服务器侧 certbot（HTTP-01，沿用 12.8.x 方式，未改造） |
| BasicAuth credential | 运营者经环境变量注入；脚本 runtime 生成 htpasswd |
| credential 入库 | **禁止**：htpasswd/私钥/口令绝不提交 git（secret scrub 守卫覆盖） |
| 应用层 Auth/JWT/RBAC | **不做**（13.5 冻结：网络层隔离即为 Pilot 的安全边界） |

## 5. Path Semantics（location precedence，冻结）

1. `location = /health` → `return 404`：健康验证归 Docker healthcheck（容器内直达
   backend:8000），公网永不代理 `/health`；
2. `location ~ ^/api/channels/[0-9]+/wecom/webhook$` → **唯一 PUBLIC 面**，无
   `auth_basic`（WeCom 回调不携带 Operator 凭据）。`$` 锚定保证
   `/api/channels/123/wecom/webhook/xxx` 等后缀变体不命中，回退兜底块；
3. `location /`（prefix 兜底）→ `auth_basic` + proxy_pass：覆盖 Management
   （agents/channels/connections/identities/users）、Observation（interactions）、
   runtime local message、静态 UI、`/docs`、`/redoc`、`/openapi.json`。

禁止任何更宽的 `/api/channels/` 前缀放行（会误伤 Management channels API）。

## 6. Fresh Clone 检查清单

1. `docker compose config` 校验拓扑（backend 无 `ports:`、有 `expose: ["8000"]`、
   `acp_net`、nginx 仅发布 443）；
2. `_deploy/nginx/acp-pilot-vhost.conf.template` 与 `_deploy/setup_nginx_pilot.sh`
   存在；
3. 仓库内无 htpasswd / 私钥 / 证书 / 明文口令（`git grep` scrub）；
4. 服务器侧注入环境变量后运行 setup 脚本，全部检查通过才 `docker compose up -d`。

## 7. Website Deployment（M8.1.3-A，自部署线并入）

# --- M8.1.3-A B1 (appended) ---
# Website deployment is now versioned and template-driven:
#   * _deploy/nginx/website-vhost.conf.template  (rendered by deploy-website.sh)
#   * _deploy/deploy-website.sh                  (git-archive artifact + nginx -t gate)
#   * docker-compose.website.yml                 (website-dist / acme-webroot / website-tls mounts)
#   * _deploy/certbot/*                          (renew + deploy hooks; website TLS out of webroot)
# The previous out-of-band host-local compose override is superseded by this
# versioned, in-repo deployment flow.

- 组合顺序（硬约束）：`docker-compose.website.yml` 必须作为**最后一个** `-f`
  传入（在 production 之后），仅向 nginx 服务合并 `:80` 发布与三个只读挂载。
- Website 源仓库独立于 ACP 仓库，`deploy-website.sh` 要求显式
  `--repo/--src`，fail-closed，无隐式来源。
- 三个宿主挂载路径经环境变量配置（无生产专属默认值，命名与
  `deploy-website.sh` 对齐）：`WEBSITE_DST`（artifact 目录）、
  `WEBSITE_ACME_WEBROOT`（ACME HTTP-01 webroot）、`WEBSITE_TLS_HOSTDIR`
  （website TLS 证书宿主目录）。
- `runtime/rendered/website-vhost.conf` 由脚本渲染（占位符 `__WEBSITE_TLS_*`），
  不得入库。
