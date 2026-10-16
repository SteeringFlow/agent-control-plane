"""WorkBuddy 重定向 URI 端点（M7-WB）。

唯一职责：为 WorkBuddy 企业开放平台「第三方应用」提供一个**合法、稳定、可公网访问**
的 Redirect URI —— `GET /oauth/callback`，只返回一个极简落地页。

这**不是** OAuth 实现（刻意边界，不扩展成 OAuth 系统）：
- 不实现 authorization flow / authorization code exchange / access_token / refresh_token；
- 不建立账号绑定 / 第三方授权体系 / 状态持久化 / 回调数据库；
- 不接管 WorkBuddy Client ID / Client Secret；
- 不读取、不解释任何 query parameter（`code` / `state` 等原样忽略）。

WorkBuddy Agent 接入链路（Agent ID + AgentOS API Key → Verify → Runtime → ACP）
**完全不变**；本端点只把客户问题「重定向 URI 要填什么」变成一个真实可访问的地址。

公开性：**无认证依赖**（不读 session / 不解析 tenant / 不要求登录）——第三方平台在
配置校验或跳转时会直接访问该 URI，任何鉴权都会使其 401。
响应**不含**任何内部信息（无 tenant / connection / 内部 id / secret / 堆栈）。

公网可达性由部署边界决定（`_deploy/nginx` 的产品面兜底 `location /` 公开代理，
M5.2 起产品面不挂 BasicAuth）——本模块不引入任何 nginx / 配置改动。
"""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

#: 极简落地页（用户语言；无内部术语；可被第三方平台安全访问）。
_REDIRECT_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>SteeringFlow</title>
</head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;padding:48px;color:#111827;">
  <h1 style="font-size:20px;margin:0 0 12px;">SteeringFlow</h1>
  <p style="font-size:14px;color:#374151;margin:0 0 8px;">此页面用于 WorkBuddy 接入配置。</p>
  <p style="font-size:14px;color:#374151;margin:0;">你可以关闭本页。</p>
</body>
</html>"""


def build_workbuddy_redirect_api() -> APIRouter:
    """构造 WorkBuddy Redirect URI 端点（与 build_version_api 同一装配纪律）。"""
    router = APIRouter(tags=["workbuddy"])

    @router.get(
        "/oauth/callback",
        response_class=HTMLResponse,
        include_in_schema=False,
        summary="WorkBuddy 重定向 URI 落地页",
    )
    def workbuddy_redirect_uri() -> HTMLResponse:
        """WorkBuddy「第三方应用」重定向 URI 的落地页（无 OAuth 逻辑、无认证）。"""
        return HTMLResponse(content=_REDIRECT_PAGE)

    return router
