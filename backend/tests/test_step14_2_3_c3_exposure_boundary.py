"""Step 14.2.3.C.3 —— Exposure Boundary machine checks（STATIC contract guards）。

守卫对象：`_deploy/nginx/acp-pilot-vhost.conf.template`（Pilot 公网面的唯一事实源）
与 `_deploy/setup_nginx_pilot.sh`（渲染 + nginx -t 纪律）。

公网行为契约（M5.2 演进：应用层 session 鉴权成为产品面唯一授权层）：
    GET /                                     → 302 /workspace.html（Product Entry）
    GET/POST /api/channels/[0-9]+/wecom/webhook  → 直通 ACP（无 auth_basic）
    /health                                   → 404（不公开、不代理、不鉴权）
    /docs /redoc /openapi.json                → 专属 location + BasicAuth（运维面）
    其余一切（product UI / /api/**）           → 公开代理；授权归应用层
      （Cloud 生产 AUTH_REQUIRED=true：未登录管理 API → 应用层 401；
        普通 Cloud 用户流程绝不依赖 nginx BasicAuth —— M5.2 §六/§七）

Integrity 语义（16.1.3.4 起冻结，取代 15.1 的 SKIP 策略）：
    * deployment artifact 缺失 → **FAIL**（不再是 SKIP）——
      部署边界是产品契约，fresh clone 缺工件即回归红；
    * 静态守卫不依赖 Docker / 网络 / 运行中的 ACP，fresh clone 必跑；
    * 请求级 LIVE 验证（历史 G9 公网模拟）不属于本文件——由 16.1.3.5 的
      live-only 流程统一执行。本文件没有任何请求级测试，因此不存在
      「Docker 不可用导致 STATIC contract SKIP」的路径。

G7 语义演进（16.1.3.4，经 16.1.3.2 audit 批准）：
    旧：compose 必须绑定 127.0.0.1:8000:8000（I3 local-only host binding）；
    新：backend **不得存在任何 host port**，仅 `expose: ["8000"]` 于 acp_net 内部
        可达；nginx（:443）是唯一公网入口。旧断言若保留会使版本化边界无法表达。

M5.2 语义演进（Cloud Product Entry，本文件守卫同步演进）：
    旧：兜底 `location /` 带 auth_basic（一切非 webhook 走 Operator 凭据）；
    新：产品面公开代理（应用层 session 鉴权守卫，Gate 1 已验证 401 边界）；
        /docs /redoc /openapi.json 获得**专属** BasicAuth location（运维面）——
        旧断言「docs 不得有专属 location」随兜底 BasicAuth 的移除一并失效。
"""

import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_DIR = Path(__file__).resolve().parents[2]
DEPLOY_DIR = REPO_DIR / "_deploy"
TEMPLATE = DEPLOY_DIR / "nginx" / "acp-pilot-vhost.conf.template"
SETUP_SCRIPT = DEPLOY_DIR / "setup_nginx_pilot.sh"
README = DEPLOY_DIR / "nginx" / "README.md"
RUNTIME_DIR = DEPLOY_DIR / "nginx" / "runtime"
COMPOSE_FILE = REPO_DIR / "docker-compose.yml"

WEBHOOK_LOCATION = "location ~ ^/api/channels/[0-9]+/wecom/webhook$ {"

# 必须存在的 deployment artifacts（任一缺失 = 部署边界不可 fresh-clone 复现 → FAIL）
REQUIRED_ARTIFACTS = (DEPLOY_DIR, TEMPLATE, SETUP_SCRIPT, README)


def _require_artifacts() -> None:
    """Integrity 核心：artifact 缺失必须 FAIL（永不 SKIP）。每个静态守卫前置调用。"""
    missing = [str(p) for p in REQUIRED_ARTIFACTS if not p.exists()]
    assert not missing, (
        f"deployment artifacts 缺失，exposure boundary 不可复现（FAIL，而非 SKIP）：{missing}"
    )


def _template_text() -> str:
    _require_artifacts()
    return TEMPLATE.read_text(encoding="utf-8")


def _semantic_text() -> str:
    """模板的**语义文本**（剔除整行注释）。

    M5.2 坑：模板头部注释新增了 precedence 说明（含 `docs|redoc|openapi` 字样），
    若对原文做 location 解析，注释块会被吞进伪 header，污染断言。location
    解析必须只看语义行——注释是文档，不是配置。
    """
    return "\n".join(
        line for line in _template_text().splitlines()
        if not line.lstrip().startswith("#")
    )


def _script_text() -> str:
    _require_artifacts()
    return SETUP_SCRIPT.read_text(encoding="utf-8")


def _location_blocks(template: str) -> list[tuple[str, str]]:
    """解析 vhost 的 (location 头, 块体) 列表（本模板无嵌套 block，可安全非贪婪匹配）。

    默认在**语义文本**上解析（剔除注释）；调用方显式传原文则自行负责。
    """
    blocks = []
    for match in re.finditer(r"location\s+([^\{]+?)\{(.*?)\}", template, re.DOTALL):
        blocks.append((match.group(1).strip(), match.group(2)))
    return blocks


def _compose() -> dict:
    assert COMPOSE_FILE.exists(), f"docker-compose.yml 缺失：{COMPOSE_FILE}"
    return yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))


def _compose_backend() -> dict:
    services = (_compose().get("services") or {})
    assert "backend" in services, "compose 缺少 backend 服务"
    return services["backend"]


def _compose_nginx() -> dict:
    services = (_compose().get("services") or {})
    assert "nginx" in services, "compose 缺少 nginx 服务（唯一公网入口）"
    return services["nginx"]


# ---------------------------------------------------------------- artifact 存在性（integrity 核心）


def test_artifacts_exist_and_fail_is_not_skip() -> None:
    """_deploy/ 三工件必须存在于 fresh clone；缺失 = FAIL（15.1 SKIP 策略废除）。"""
    _require_artifacts()


# ---------------------------------------------------------------- compose 拓扑（G7 新语义）


def test_g7_compose_backend_publishes_no_host_ports() -> None:
    """G7（新语义）：backend 不得存在任何 host ports 映射。

    旧语义（127.0.0.1:8000:8000 local binding）已废弃：backend 现只存在于
    acp_net 内部，公网唯一入口是 nginx。
    """
    backend = _compose_backend()
    assert "ports" not in backend, (
        "backend 不得 publish 任何 host port（G7 新语义：nginx 才是公网入口）"
    )


def test_compose_backend_exposes_only_8000() -> None:
    """backend 仅对内部网络 expose 8000。"""
    backend = _compose_backend()
    assert backend.get("expose") == ["8000"], (
        f"backend expose 必须恰为 [\"8000\"]，实际：{backend.get('expose')}"
    )


def test_compose_backend_and_nginx_on_acp_net() -> None:
    """backend 与 nginx 必须同处专用内部网络 acp_net。"""
    net_name = "acp_net"
    for name, svc in (("backend", _compose_backend()), ("nginx", _compose_nginx())):
        networks = svc.get("networks")
        assert networks, f"{name} 未加入任何网络"
        assert net_name in networks, f"{name} 必须加入 {net_name}（实际：{list(networks)}）"
    compose_nets = _compose().get("networks") or {}
    assert net_name in compose_nets, "compose 顶层必须声明 acp_net"


def _published_host_ports(svc_ports: list) -> set[str]:
    """把 compose ports 条目归一为 published host port 集合（支持 str 与 mapping 形态）。"""
    published = set()
    for entry in svc_ports or []:
        if isinstance(entry, str):
            parts = entry.split(":")
            published.add(parts[-2] if len(parts) >= 3 else parts[0])
        elif isinstance(entry, dict):
            published.add(str(entry["published"]))
        else:  # pragma: no cover - compose 形态防御
            pytest.fail(f"未知 ports 条目形态：{entry!r}")
    return published


def test_compose_nginx_publishes_only_443() -> None:
    """nginx 对外只发布 HTTPS :443（公网唯一入口；:80 仅 ACME 时由运营者临时开放）。"""
    nginx = _compose_nginx()
    assert _published_host_ports(nginx.get("ports")) == {"443"}, (
        f"nginx 只允许发布 443，实际：{nginx.get('ports')}"
    )


# ---------------------------------------------------------------- G1 webhook 精确匹配


def test_g1_webhook_exact_regex_location_exists() -> None:
    """webhook 必须以完整路径正则声明（不能按 /api/channels 前缀放行）。"""
    template = _template_text()
    assert WEBHOOK_LOCATION in template, (
        "缺少 webhook 精确匹配 location（G1）")


def test_g1_no_broader_channel_passthrough() -> None:
    """不允许任何更宽的 /api/channels 直通（前缀放行会连带 Management channels API）。"""
    template = _semantic_text()
    offenders = [
        header for header, _body in _location_blocks(template)
        if "api/channels" in header and "wecom/webhook" not in header
    ]
    assert not offenders, f"存在过宽的 channels 放行（G1 违规）：{offenders}"


def test_g1_docs_redoc_openapi_keep_dedicated_basic_auth() -> None:
    """/docs、/redoc、/openapi.json 必须拥有**专属** BasicAuth location（M5.2 运维面）。

    M5.2 演进：兜底块已转为产品面公开代理（授权归应用层），运维面因此需要
    专属受保护 location——旧断言「docs 不得有专属 location」随兜底 BasicAuth
    的移除一并失效。新契约：专属 location 必须存在且带 auth_basic。
    """
    template = _semantic_text()
    ops_blocks = [
        (header, body) for header, body in _location_blocks(template)
        if re.search(r"docs|redoc|openapi", header, re.IGNORECASE)
    ]
    assert ops_blocks, "缺少 /docs /redoc /openapi.json 专属运维 location（M5.2 §七）"
    for header, body in ops_blocks:
        assert "auth_basic " in body, (
            f"运维 location 缺少 BasicAuth（M5.2 §七 违规）：{header}"
        )


# ---------------------------------------------------------------- G2-G5 auth boundary


def _webhook_blocks(template: str) -> list[tuple[str, str]]:
    return [
        (h, b) for h, b in _location_blocks(template)
        if "wecom/webhook" in h and "proxy_pass" in b
    ]


def test_g2_g5_webhook_location_no_auth_basic() -> None:
    """webhook 是 PUBLIC 直通面：不得挂 auth_basic（WeCom 不携带 Operator 凭据）。"""
    template = _semantic_text()
    blocks = _webhook_blocks(template)
    assert len(blocks) == 1, f"webhook 代理 location 必须恰好 1 个，实际 {len(blocks)}"
    header, body = blocks[0]
    assert "auth_basic" not in body, f"webhook location 不得挂 auth_basic：{header}"


def test_g2_g5_product_catchall_is_public_app_layer_authz() -> None:
    """M5.2 新语义：产品兜底 `location /` **公开代理且不挂 auth_basic**
    ——普通 Cloud 用户流程绝不依赖 BasicAuth（M5.2 §六）。

    授权归应用层：Cloud 生产 ENVIRONMENT=production + AUTH_REQUIRED=true，
    未登录管理 API 由 TenantScope 依赖裁决 401（Gate 1 已机器验证）。
    """
    template = _semantic_text()
    proxying_catchall = [
        (h, b) for h, b in _location_blocks(template)
        if h.strip() == "/" and "proxy_pass" in b
    ]
    assert len(proxying_catchall) == 1, "必须恰好一个代理到 ACP 的 `location /` 兜底块"
    header, body = proxying_catchall[0]
    assert "auth_basic" not in body, (
        f"产品兜底块不得挂 auth_basic（M5.2 §六 违规）：{header}"
    )


def test_g2_g5_auth_basic_only_on_operational_surface() -> None:
    """M5.2 新语义：auth_basic 只允许出现在运维面 location
    （/docs /redoc /openapi.json 专属块）——产品面 / webhook / /health 一律无。"""
    template = _semantic_text()
    ops_headers = [
        h for h, _b in _location_blocks(template)
        if re.search(r"docs|redoc|openapi", h, re.IGNORECASE)
    ]
    for header, body in _location_blocks(template):
        if "auth_basic" not in body:
            continue
        assert re.search(r"docs|redoc|openapi", header, re.IGNORECASE), (
            f"auth_basic 越界出现在非运维 location（M5.2 违规）：{header}"
        )
    assert ops_headers, "运维面专属 location 缺失（M5.2 §七）"


# ---------------------------------------------------------------- G6 /health 不公开


def test_g6_health_not_proxied() -> None:
    """/health 必须 exact-match 404：无 proxy_pass、无 auth（它根本不是入口）。"""
    template = _semantic_text()
    health_blocks = [
        (h, b) for h, b in _location_blocks(template) if h.strip() == "= /health"
    ]
    assert len(health_blocks) == 1, "必须恰好一个 `location = /health` 块"
    header, body = health_blocks[0]
    assert "return 404" in body, "/health 应直接 404"
    assert "proxy_pass" not in body, "/health 不得代理到 ACP（G6 违规）"
    assert "auth_basic" not in body, "/health 不是入口，无需 auth"


# ---------------------------------------------------------------- G7 upstream 纪律


def test_g7_upstream_is_localhost_placeholder_only() -> None:
    """所有 proxy_pass 只指向唯一 __ACP_UPSTREAM__ 占位符（渲染值 = 完整 URL，
    16.1.3.3 实现选择 scheme 由 ACP_UPSTREAM 环境值携带，如 http://backend:8000）；
    模板中不得出现任何硬编码地址。只检查配置语义行（注释中的文档示例不算）。"""
    template = _template_text()
    semantic_lines = [
        line for line in template.splitlines() if not line.lstrip().startswith("#")
    ]
    passes = [
        m.group(1).strip()
        for line in semantic_lines
        if (m := re.match(r"\s*proxy_pass\s+([^;]+);\s*$", line))
    ]
    assert passes, "模板应包含 proxy_pass"
    for p in passes:
        assert p == "__ACP_UPSTREAM__", (
            f"proxy_pass 必须使用 __ACP_UPSTREAM__ 占位符，发现：{p}"
        )


# ---------------------------------------------------------------- G8 渲染与纪律


def test_g8_script_has_nginx_t_discipline_and_placeholder_check() -> None:
    """渲染脚本必须：set -euo pipefail、nginx -t 通过才放行、残留占位符即失败。"""
    script = _script_text()
    assert script.startswith("#!"), "缺少 shebang"
    assert "set -euo pipefail" in script, "缺少 fail-closed shell 选项（G8）"
    assert "nginx -t" in script, "缺少 nginx -t 纪律（G8）"
    assert "${ACP_DOMAIN:?" in script, "必需参数缺失必须 fail closed"
    assert re.search(r"grep[^\n]*'__ACP_'", script), "缺少残留占位符自检"


def test_template_placeholders_complete() -> None:
    """模板使用的全部占位符都必须在脚本中有 sed 渲染规则。"""
    template = _template_text()
    script = _script_text()
    used = set(re.findall(r"__ACP_[A-Z_]+__", template))
    assert used, "模板应包含参数化占位符"
    for placeholder in used:
        assert f"s|{placeholder}|" in script, f"占位符 {placeholder} 缺少渲染规则"


def test_htpasswd_permission_worker_readable() -> None:
    """htpasswd 必须 chmod 644（nginx:alpine 非 root worker uid 101 可读）。

    16.1.3.5 F1：640 使 worker 无法读取 auth file，带凭据请求全部 500。
    `nginx -t` 以 root 执行、无法暴露此问题，因此这里用静态守卫钉死。
    同时断言 .gitattributes 强制 *.sh / *.conf.template LF（F2：
    Windows autocrlf fresh clone 曾产生 CRLF 脚本导致 bash 崩溃）。
    """
    script = _script_text()
    htpasswd_section = script.split("已生成 htpasswd")[0]
    assert "chmod 644" in htpasswd_section, (
        "htpasswd 必须为 644（nginx 非 root worker 可读）"
    )
    assert "chmod 640" not in script, "不得再对 htpasswd 使用 640"

    gitattributes = REPO_DIR / ".gitattributes"
    assert gitattributes.exists(), "缺少 .gitattributes（F2：强制部署工件 LF）"
    attrs = gitattributes.read_text(encoding="utf-8")
    assert re.search(r"\*\.sh\s+text\s+eol=lf", attrs), (
        ".gitattributes 必须强制 *.sh eol=lf"
    )
    assert re.search(r"\*\.conf\.template\s+text\s+eol=lf", attrs), (
        ".gitattributes 必须强制 *.conf.template eol=lf"
    )


# ---------------------------------------------------------------- secret boundary


def test_artifacts_contain_no_real_credentials() -> None:
    """模板/脚本/README 不得包含真实 credential（TLS 私钥、htpasswd hash、
    硬编码口令字面量、bearer token）。真实值只能经环境变量/服务器侧 provisioning。"""
    credential_patterns = {
        "TLS 私钥": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
        "htpasswd hash": r"\$apr1\$",
        "硬编码口令": r"(?i)pass(word)?\s*[=:]\s*[\"'][^\"']{4,}[\"']",
        "bearer token": r"Bearer [A-Za-z0-9_\-\.]{20,}",
        "云 API key": r"AKID[0-9A-Z]{16,}",
    }
    for path in (TEMPLATE, SETUP_SCRIPT, README):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for label, pattern in credential_patterns.items():
            hit = re.search(pattern, text)
            assert not hit, f"{path.name} 疑似包含真实 {label}（secret boundary 违规）"


def test_runtime_dir_is_not_tracked_artifact() -> None:
    """runtime/（渲染产物 / TLS / htpasswd）必须被 git 忽略且从未被 track
    ——它是服务器侧 provisioning 产物，绝不能成为可提交 deployment artifact。"""
    probe = RUNTIME_DIR / "rendered" / "acp-pilot-vhost.conf"
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", probe.as_posix()],
        cwd=REPO_DIR,
    )
    assert ignored.returncode == 0, (
        f"runtime 产物未被 .gitignore 忽略（可能被误提交）：{probe}"
    )
    tracked = subprocess.run(
        ["git", "ls-files", "--", "_deploy/nginx/runtime"],
        cwd=REPO_DIR, capture_output=True, text=True,
    )
    assert tracked.returncode == 0, tracked.stderr
    assert tracked.stdout.strip() == "", (
        f"runtime/ 下存在被 track 的文件：{tracked.stdout.strip()}"
    )
