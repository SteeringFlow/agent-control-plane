"""M7-AUI-10.8.12 —— Compose fail-closed 安全守卫 + 部署资产整合守卫。

背景（10.8.10 对账 / 10.8.11 决策）：部署线 00f9b76 将 docker-compose.cloud.yml
的 POSTGRES_PASSWORD 从弱默认 `${POSTGRES_PASSWORD:-acp}` 改为必填 `:?`。
本测试把该语义固化为本地发布线的守卫，并守护 rc.8 并入的部署资产。

全部测试为静态文件断言或 `docker compose config` 纯解析验证——
不连接任何数据库，不启动任何容器（config 只做渲染校验）。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CLOUD_YML = REPO_ROOT / "docker-compose.cloud.yml"
PRODUCTION_YML = REPO_ROOT / "docker-compose.production.yml"
WEBSITE_YML = REPO_ROOT / "docker-compose.website.yml"

def _deploy_script_paths(provenance: str) -> list[Path]:
    """按仓库来源返回应守卫的部署脚本清单（B3.3 / F2 解法）。

    - ``source``：全量（含 ``certbot-renew.sh`` —— 生产续期脚本为生产专属
      工件，按导出治理**不进 OSS 导出**，其安全断言仅在源仓库执行）；
    - ``oss_export``：剔除 ``certbot-renew.sh``（该文件被有意排除——绝不为了
      满足测试而重新纳入），其余部署资产照常守卫；
    - ``unknown``：**保守返回全量**——部分检出/矛盾树上缺失文件将显式失败，
      绝不静默缩小检查面（与 tests/oss_export_boundary.py 的保守契约一致）。
    """
    paths = [
        REPO_ROOT / "_deploy" / "deploy-website.sh",
        REPO_ROOT / "_deploy" / "certbot" / "certbot-deploy.sh",
        REPO_ROOT / "_deploy" / "certbot" / "certbot-renew.sh",
    ]
    if provenance == "oss_export":
        return [p for p in paths if p.name != "certbot-renew.sh"]
    return paths


from tests.oss_export_boundary import repo_provenance  # noqa: E402  # 契约见 oss_export_boundary

DEPLOY_SCRIPTS = _deploy_script_paths(repo_provenance())

DEPLOY_ASSETS = [
    *DEPLOY_SCRIPTS,
    REPO_ROOT / "_deploy" / "nginx" / "website-vhost.conf.template",
]

SECRET_PATTERNS = [
    re.compile(r"BEGIN [A-Z ]*PRIVATE KEY"),
    re.compile(r"apr1\$"),  # htpasswd 哈希特征
    re.compile(r"sk-[A-Za-z0-9]{20}"),
]

docker_available = shutil.which("docker") is not None


# ---------------------------------------------------------------- A. fail-closed


def test_cloud_yml_has_no_weak_password_default() -> None:
    """cloud.yml 不得再包含 `${POSTGRES_PASSWORD:-` 弱默认语义。"""
    text = CLOUD_YML.read_text(encoding="utf-8")
    assert "POSTGRES_PASSWORD:-" not in text, (
        "cloud.yml 仍存在 POSTGRES_PASSWORD 弱默认（fail-closed 修复被回退）"
    )


def test_cloud_yml_password_refs_are_required() -> None:
    """cloud.yml 的两处密码引用（postgres env + backend DATABASE_URL）均为必填。"""
    text = CLOUD_YML.read_text(encoding="utf-8")
    required = len(re.findall(r"\$\{POSTGRES_PASSWORD:\?", text))
    assert required == 2, f"期望 2 处必填引用，实际 {required}"


def test_production_yml_required_constraints_kept() -> None:
    """production.yml 的既有必填约束（app 角色 + admin 角色）不得回退。"""
    text = PRODUCTION_YML.read_text(encoding="utf-8")
    assert "POSTGRES_APP_PASSWORD:?" in text
    assert "POSTGRES_PASSWORD:?" in text


@pytest.mark.skipif(not docker_available, reason="需要 docker CLI 做 compose config 解析")
def test_cloud_yml_config_fails_without_required_env() -> None:
    """未设置 POSTGRES_PASSWORD 时，cloud.yml 单文件组合的 config 解析必须失败。"""
    env = {k: v for k, v in os.environ.items() if k != "POSTGRES_PASSWORD"}
    result = subprocess.run(
        ["docker", "compose", "-f", str(CLOUD_YML), "config", "--quiet"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert result.returncode != 0, "缺少 POSTGRES_PASSWORD 时 compose config 仍解析成功（fail-closed 失效）"


@pytest.mark.skipif(not docker_available, reason="需要 docker CLI 做 compose config 解析")
def test_cloud_yml_config_parses_with_required_env() -> None:
    """显式提供（隔离假值）POSTGRES_PASSWORD 后，config 解析必须成功。"""
    env = dict(os.environ, POSTGRES_PASSWORD="isolated-dummy-not-a-real-secret")
    result = subprocess.run(
        ["docker", "compose", "-f", str(CLOUD_YML), "config", "--quiet"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert result.returncode == 0, f"提供必需变量后解析失败: {result.stderr[-300:]}"


# ---------------------------------------------------------------- B. 部署资产整合守卫


@pytest.mark.parametrize("path", DEPLOY_SCRIPTS, ids=lambda p: p.name)
def test_deploy_assets_exist_with_shebang(path: Path) -> None:
    """并入的部署脚本必须存在且以 bash shebang 开头。"""
    assert path.is_file(), f"部署资产缺失: {path}"
    assert path.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash"), (
        f"{path.name} 缺少 bash shebang"
    )


@pytest.mark.parametrize("path", [*DEPLOY_ASSETS, WEBSITE_YML], ids=lambda p: p.name)
def test_deploy_assets_contain_no_secrets(path: Path) -> None:
    """并入资产不得携带私钥 / htpasswd 哈希 / API Key 形态内容。"""
    text = path.read_text(encoding="utf-8")
    for pattern in SECRET_PATTERNS:
        assert pattern.search(text) is None, f"{path.name} 疑似包含秘密形态内容"


def test_website_yml_contract() -> None:
    """website profile 契约：最后一个 -f；仅合并 nginx 的 :80 与三个只读挂载。

    B4.1（G1）：三个宿主挂载已参数化为环境变量（无生产专属默认值）——
    契约断言同步为「挂载变量存在且目标容器路径/只读语义不变」，
    意图不变：website profile 必须恰好携带这三个契约挂载。
    """
    text = WEBSITE_YML.read_text(encoding="utf-8")
    assert '"80:80"' in text, "website.yml 必须发布 :80（ACME HTTP-01）"
    for mount in ("${WEBSITE_DST:-", "${WEBSITE_ACME_WEBROOT:-",
                  "${WEBSITE_TLS_HOSTDIR:-"):
        assert mount in text, f"website.yml 缺少契约挂载变量: {mount}"
    # 容器内目标路径与只读语义不变（B4.1 G1：仅通用化宿主侧）
    for target in (":/usr/share/nginx/html/website:ro",
                   ":/var/www/certbot:ro", ":/etc/nginx/website-tls:ro"):
        assert target in text, f"website.yml 容器内挂载语义漂移: {target}"
    assert "services:" in text and "nginx:" in text
    # 只允许 touches nginx 服务（profile 不得新增/覆盖 backend/postgres）
    services_block = text.split("services:", 1)[1]
    top_level_services = re.findall(r"^  ([a-z_-]+):", services_block, re.MULTILINE)
    assert top_level_services == ["nginx"], f"website.yml 只应含 nginx 服务，实际 {top_level_services}"


def test_website_yml_combination_order_documented() -> None:
    """组合顺序契约必须写明 website.yml 为最后一个 -f。"""
    text = WEBSITE_YML.read_text(encoding="utf-8")
    assert "LAST -f" in text or "最后一个" in text
