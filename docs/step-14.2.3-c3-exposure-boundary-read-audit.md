# Step 14.2.3.C.3 — Exposure Boundary（Read-only Audit）

> 本步只审计不修改。回答 5 个问题，为「**只有 webhook 能从公网进入 ACP**」的
> 可部署、可验证、可复制实施提供全部事实。

---

## Q1. 实际 webhook endpoint 到底是什么？

**恰好两个端点，共用同一前缀**（`api/runtime/channels.py`）：

```
router = APIRouter(prefix="/api/channels", tags=["runtime-inbound"])   # :56

GET  /api/channels/{channel_id}/wecom/webhook    # :68  WeCom URL 验证握手
                                                 #      （验签 + 解密 echostr → 明文应答）
POST /api/channels/{channel_id}/wecom/webhook    # :87  消息回调（ACK 边界）
```

`{channel_id}` 是**数字 id**（DB 主键）——公网代理层可以用
`^/api/channels/[0-9]+/wecom/webhook$` 精确匹配，不误伤 Management 的
`/api/channels/*`（后者路径形如 `/api/channels`、`/api/channels/{id}`，不带
`/wecom/webhook` 后缀）。

## Q2. 是否已经存在 proxy artifact？

**存在，且有两份历史 nginx 部署脚本**（`_deploy/`，真实服务器上用过）：

| 文件 | 内容 |
| --- | --- |
| `setup_nginx.sh` | 3 阶段：HTTP-only vhost（ACME webroot）→ certbot HTTP-01 → 完整 vhost（80→443 redirect + TLS proxy）+ 自检 |
| `setup_nginx_stage3.sh` | 仅第 3 阶段（复用已有证书） |
| `deploy_acp.sh:44` | 历史容器启动：`-p 127.0.0.1:8002:8000`——**「容器只绑 127.0.0.1、由 nginx 对外」的做法已有先例** ✅ |

**关键发现（与拍板矛盾）**：历史 vhost 是 **`location / → proxy_pass 127.0.0.1:8002`
的全量代理**——即 12.8.x 时代真实部署的 exposure decision 是「**全部面经 HTTPS
公网可达**」，没有路径级隔离。它可作为 TLS/ACME 骨架复用，但 **vhost 路由部分必须重写**。

## Q3. Management / Observation 的 route surface

| 面 | 前缀 | 方法 | 数量 |
| --- | --- | --- | --- |
| Management | `/api/agents` `/api/channels` `/api/connections` `/api/identities` `/api/users` | POST/GET/PATCH（无 DELETE，软下线=PATCH status） | 20 |
| Observation | `/api/interactions`（`/{id}`、`/{id}/activities`） | GET only | 3 |
| Runtime | `/api/channels/{id}/wecom/webhook` | GET/POST | 2 |
| 静态 UI | `/`（StaticFiles catch-all，`html=True`） | GET | 全部前端页 |
| 基础设施 | `/health`、`/docs`、`/redoc`、`/openapi.json` | GET | 4 |

注意：`/api/channels` 前缀被 **management 与 runtime 两个 router 共用**——
公网放行规则必须用完整路径正则（Q1），不能按前缀放行。

## Q4. 当前是否还有其他必须公开的 endpoint？

**没有。** 公网必须可达的只有：

- `GET + POST /api/channels/{id}/wecom/webhook`（WeCom 回调，唯一硬性公开项）；
- （可选）`GET /health`——供运维探活；给不给公网是策略选择，非必须。

其余全部不应公网：Management API（写配置）、Observation API（含对话原文 payload）、
前端静态页、`/docs` `/redoc` `/openapi.json`（Swagger 契约公开 = 攻击面地图）。

## Q5. 文档里是否已有既定 exposure decision？

**有，且三处一致**（均已在更早步骤拍板，本步只需落实）：

| 文档 | 决策 |
| --- | --- |
| `step-13.5` Q4 | Single Operator Pilot：**无需应用层 Auth**；必须网络层隔离（反代 Basic Auth / IP allowlist / 内网）。webhook 天然公网；Management + Observation 含 payload，绝不能裸公网 |
| `step-14.1` Q5 | 容器只绑 `127.0.0.1`；反代只放行 webhook 路径；对其余施 Basic Auth 或 IP allowlist；不加 JWT/RBAC |
| `step-14.2.3.A` | I3 已落地：compose `127.0.0.1:8000:8000`（宿主机以外已不可直达，缺口只剩反代层） |

## C.3 落地实施形态（结论，待批准后实施）

**缺口 = 一个路径级隔离的 proxy 配置 + 让它可验证**。历史脚本提供 TLS/ACME 骨架，
vhost 路由部分按以下规则重写：

```nginx
# 公网：仅 webhook（完整路径正则，避免误伤 /api/channels 管理面）
location ~ ^/api/channels/[0-9]+/wecom/webhook$ {
    proxy_pass http://127.0.0.1:8000;   # 或参数化端口
    ...（复用历史 proxy_set_header / timeout 骨架）
}

# 可选公开：健康探针
location = /health { proxy_pass http://127.0.0.1:8000; ... }

# 其余一切（Management/Observation/UI/docs/openapi）：
location / {
    # 方案 A（默认）：Basic Auth（单 Operator 一组口令，htpasswd 一行）
    # 方案 B：satisfy any + IP allowlist（运维内网固定出口时）
    auth_basic "ACP Pilot";
    auth_basic_user_file /etc/nginx/acp.htpasswd;
    proxy_pass http://127.0.0.1:8000; ...
}
```

交付物清单（实施步）：
1. `deploy/`（或复用 `_deploy/`）新增**参数化** nginx vhost 模板（复用历史 3 阶段骨架）；
2. `docker-compose.yml` 不改（I3 已是正确形态；端口参数化在模板层做）；
3. walk-through 文档：域名/DNS → `setup_nginx.sh <domain>` → WeCom 回调 URL
   填 `https://<domain>/api/channels/<id>/wecom/webhook` → Operator 经 Basic Auth 访问 Management/Observation；
4. 验证：公网模拟（改 Host 头打 80/443）断言 webhook 200、`/api/agents` 401、
   `/` 401；`nginx -t` 纪律沿用历史脚本。

**明确不做**（C.3 冻结）：应用 Auth / JWT / RBAC / Policy Engine / TLS 自动化改造 /
WAF / Rate Limiting / 云平台 / K8s。

---

## 附：审计命令原始结论索引

| 审计项 | 结果 |
| --- | --- |
| 1. proxy/deploy 工件 | `docker-compose.yml`、`_deploy/{acp_deploy.zip, deploy_acp.sh, setup_nginx.sh, setup_nginx_stage3.sh}`、step-14.1 文档 |
| 2. 路由声明 | 29 个端点（见 Q3 表） |
| 3. webhook | `api/runtime/channels.py` GET:68 / POST:87，prefix `/api/channels` |
| 4. Management/Observation 前缀 | 6 个 router prefix（见 Q3 表） |
| 5. 发布端口 | `host_ip: 127.0.0.1, target: 8000, published: "8000"`（I3 生效） |
| 6. 文档决策 | 13.5 Q4 / 14.1 Q5 / 14.2.3.A I3 三处一致 |
