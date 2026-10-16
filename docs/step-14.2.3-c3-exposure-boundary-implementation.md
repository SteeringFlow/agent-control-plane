# Step 14.2.3.C.3 — Pilot Exposure Boundary（实施报告）

> 经批准的方向 + 一处收敛：**`/health` 也不公开**（健康验证归 Docker healthcheck / 本地）。
> Pilot 公网面收敛为：**仅 webhook 直通，其余 Basic Auth，无任何匿名面**。

## 1. 公网行为契约（已实测验证）

```
Internet → Nginx :443
  ├── GET/POST /api/channels/[0-9]+/wecom/webhook → ACP（唯一公网直通面）
  ├── /health                                     → 404（不公开）
  └── 其余一切（Management/Observation/UI/docs/openapi）→ Basic Auth
```

## 2. 交付物

| 文件 | 说明 |
| --- | --- |
| `_deploy/nginx/acp-pilot-vhost.conf.template` | **新增**。参数化 Pilot vhost 模板（6 个占位符）；文件头写明 location precedence 语义（本模板的安全性核心） |
| `_deploy/setup_nginx_pilot.sh` | **新增**。复用历史 3 阶段骨架（ACME webroot → certbot → 完整 vhost）+ stage 2.5（htpasswd 生成）+ 占位符残留自检 + `nginx -t` 纪律 + 服务器侧 401 自检。**历史脚本零改动** |
| `backend/tests/test_step14_2_3_c3_exposure_boundary.py` | **新增**。G1–G8 静态机器守卫 10 项 |
| `docs/pilot-walkthrough.md` | **新增**。Operator 从零到闭环的 12 步操作指南 |
| 历史脚本 / docker-compose.yml / 应用代码 / frontend | **0 改动**（compose I3 绑定即正确形态，无需动） |

## 3. location precedence（安全性核心，模板头已冻结）

1. `location = /health`（exact）最先命中 → `return 404`，永不代理；
2. `location ~ ^/api/channels/[0-9]+/wecom/webhook$`（regex）先于前缀匹配，**不含**
   `auth_basic`——WeCom 回调不携带 Operator 凭据；
3. `location /`（prefix 兜底）**含** `auth_basic`——覆盖 Management API、Observation
   API、静态 UI、`/docs`、`/redoc`、`/openapi.json`。

## 4. 机器守卫（pytest，10 项全绿）

| # | 守卫 |
| --- | --- |
| G1 | webhook 完整路径正则存在；**无任何更宽的** `/api/channels` 放行（防误伤 Management channels API） |
| G2–G5 | 凡含 `proxy_pass` 的 location：webhook **不得**有 auth_basic（WeCom 无凭据）；其余**必须**有；代理型 `location /` 兜底块恰好 1 个且带 auth |
| G6 | `location = /health` 恰好 1 个：`return 404`，无 proxy_pass、无 auth |
| G7 | 全部 `proxy_pass` 只指向 `__ACP_UPSTREAM__` 占位符；compose 仍绑 `127.0.0.1:8000:8000`（I3 不回退） |
| G8 | 脚本含 `set -e` + `nginx -t` 纪律 + 占位符残留自检；模板每个占位符都有渲染规则；**历史脚本语义未被破坏** |

## 5. G8 + G9 实测（真实 nginx，本会话证据）

**G8 `nginx -t`**（`nginx:alpine` 容器，渲染后的真实配置 + 自签证书）：
```
nginx: the configuration file /etc/nginx/nginx.conf syntax is ok
nginx: configuration file /etc/nginx/nginx.conf test is successful
```

**G9 公网模拟**（nginx 容器 :8443 → 上游 `host.docker.internal:8000` = ACP Pilot 容器）：

| 请求（匿名） | 结果 | 契约 |
| --- | --- | --- |
| GET `/api/channels/1/wecom/webhook` | **422** | ✅ 穿透 nginx 到达 ACP（FastAPI 参数校验），无 auth 拦截 |
| POST `/api/channels/1/wecom/webhook` | **422** | ✅ 同上 |
| GET `/health` | **404** | ✅ 不公开 |
| GET `/api/agents` | **401** | ✅ auth boundary |
| GET `/api/interactions` | **401** | ✅ |
| GET `/`（UI） | **401** | ✅ |
| GET `/docs` | **401** | ✅ |
| GET `/openapi.json` | **401** | ✅ |

| 请求（带 Operator 凭据） | 结果 |
| --- | --- |
| GET `/api/agents`（-u operator:***） | **200** ✅ |
| GET `/interactions.html`（UI） | **200** ✅ |

模拟用容器与临时目录已清理；`nginx -t`（G8）同时验证了 404/regex/auth 结构的真实可加载性。

## 6. 边界核对

❌ Application Auth ❌ JWT ❌ RBAC ❌ Permission/Policy Engine ❌ Runtime/webhook handler/
Worker/Driver 修改 ❌ DB 修改 ❌ frontend 修改 ❌ 云厂商部署平台 ❌ TLS 自动化改造
（沿用历史 certbot HTTP-01 方式）❌ WAF ❌ Rate Limiting ❌ Redis ❌ K8s —— **全部为零**。

`docker-compose.yml`：本步**未修改**（I3 绑定即正确形态；`/health` 收敛在 vhost 层完成）。

## 7. 遗留

- walk-through 步骤 1–2（域名 / DNS）与真实证书签发属部署日操作（脚本已参数化）；
- Pilot walk-through 本身见 `docs/pilot-walkthrough.md`（12 步闭环）。
