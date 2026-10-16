"""B3.2 —— 无 frontend 运行/构建兼容性回归（A1：初始 OSS 不含 frontend/）。

背景：docs/strategy/oss-vs-cloud-boundary.md 决策 A1 —— 初始 OSS 快照排除
frontend/；main.py 的静态挂载改为条件式（`if FRONTEND_DIR.is_dir():`），
Docker 构建提供无前端变体 `Dockerfile.oss`。

本文件只覆盖：
  A. frontend 存在（Cloud/源仓库现状）：挂载保留、health 可用；
  B. frontend 缺失（OSS 导出形态）：组合根可创建 app、子进程可完整启动、
     health 可用、不伪造任何前端目录/文件；
  C. 构建变体静态守卫（Cloud Dockerfile 保持 COPY frontend；Dockerfile.oss
     不含 frontend COPY）；可选的 docker build 实测（env 门控）。

不修改 37 个前端依赖测试文件；不处理 F2；不改 README。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.oss_export_boundary import oss_ui_required

BACKEND = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND.parent
MAIN_PY = BACKEND / "app" / "main.py"
DOCKERFILE_CLOUD = REPO_ROOT / "Dockerfile"
DOCKERFILE_OSS = REPO_ROOT / "Dockerfile.oss"

docker_available = shutil.which("docker") is not None


# ============================================================ A. frontend 存在


@oss_ui_required()
def test_frontend_present_app_registers_static_mount() -> None:
    """源仓库 frontend/ 存在（Cloud/开发现状）→ 静态挂载必须保留。"""
    import app.main as app_main

    mounts = [r for r in app_main.app.routes if getattr(r, "name", "") == "frontend"]
    assert len(mounts) == 1, "frontend 存在时必须恰好注册一个静态挂载"
    # Starlette 会把 Mount("/") 的 path 规范化为 ""
    assert mounts[0].path in ("/", "")


def test_frontend_present_health_ok() -> None:
    """frontend 存在时 health 行为不变（现有 Cloud 行为保持）。"""
    import app.main as app_main

    client = TestClient(app_main.create_app())
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


# ============================================================ B. frontend 缺失


def test_frontend_absent_create_app_skips_mount_and_fabricates_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """frontend 目录缺失 → create_app 成功、无静态挂载、health 路由仍在。

    用 backend/app 下一个保证不存在的路径模拟导出形态；并断言组合根
    **不伪造**任何前端目录/文件（B1：不造假 UI）。
    """
    import app.main as app_main

    fake_missing = Path(app_main.__file__).parent / "no-such-frontend-dir"
    assert not fake_missing.exists()
    monkeypatch.setattr(app_main, "FRONTEND_DIR", fake_missing)

    app = app_main.create_app()

    assert not [r for r in app.routes if getattr(r, "name", "") == "frontend"], \
        "frontend 缺失时不得注册静态挂载"
    # health / API 路由不受影响（具名路由先于 catch-all，11.5 冻结顺序不变）
    assert any(getattr(r, "path", None) == "/health" for r in app.routes)
    # 不伪造：目录仍未被创建
    assert not fake_missing.exists()


def test_frontend_absent_subprocess_full_startup_and_health() -> None:
    """端到端：把 backend/app 复制到无 frontend 的临时树，子进程完整启动
    （lifespan 含 DB init）并请求 /health —— 证明 OSS 导出形态可真实运行。

    注：不用 pytest tmp_path 夹具——本机该夹具存在已知 WinError 5 权限问题
    （见 M6_6 留档与既有 63 个环境类 error），改用 tempfile 自管临时树。
    """
    import tempfile

    try:
        tree = Path(tempfile.mkdtemp(prefix="nofe_b32_")) / "no-fe"
        shutil.copytree(BACKEND / "app", tree / "app")
    except PermissionError:
        pytest.skip("Windows tmpdir 权限问题（本机已知环境限制，见 M6_6 留档）")

    script = (
        "import sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from fastapi.testclient import TestClient\n"
        "import app.main as m\n"
        "assert not [r for r in m.app.routes if getattr(r, 'name', '') == 'frontend'], "
        "'frontend mount must be absent'\n"
        "with TestClient(m.app) as client:\n"
        "    r = client.get('/health')\n"
        "    assert r.status_code == 200, r.text\n"
        "    assert r.json()['status'] == 'ok', r.text\n"
        "print('NOFE_OK')\n"
    )
    # DATABASE_URL 指向临时树内（隔离，不触碰开发库）；data/ 由启动流程自建
    env = dict(os.environ)
    env["DATABASE_URL"] = "sqlite:///./nofe_smoke.db"
    proc = subprocess.run(
        [sys.executable, "-c", script, str(tree)],
        cwd=str(tree),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    assert proc.returncode == 0, (
        f"无 frontend 子进程启动失败\nstdout={proc.stdout[-2000:]}\n"
        f"stderr={proc.stderr[-2000:]}"
    )
    assert "NOFE_OK" in proc.stdout
    shutil.rmtree(tree.parent, ignore_errors=True)


# ============================================================ C. 源码/构建守卫


def test_main_source_keeps_conditional_mount_and_frozen_literals() -> None:
    """main.py 守卫文本：条件挂载存在；既有冻结字面量不被破坏
    （test_step11_5 要求 `StaticFiles`/`app.mount`；test_step12_6 要求
    `StaticFiles(directory` 位于 worker_for 装配之后）。"""
    src = MAIN_PY.read_text(encoding="utf-8")
    assert "if FRONTEND_DIR.is_dir():" in src
    assert "StaticFiles" in src
    assert "app.mount(" in src
    assert "StaticFiles(directory" in src
    worker_pos = src.index("worker_for=_worker_for))")
    mount_pos = src.index("StaticFiles(directory")
    assert worker_pos < mount_pos, "webhook/worker 装配必须先于静态 mount（11.5）"


def test_docker_variants_cloud_keeps_frontend_oss_excludes_it() -> None:
    """Cloud Dockerfile 保持 `COPY frontend/`；Dockerfile.oss 不含任何 frontend
    COPY（A1），且两者 FROM/CMD 契约一致。"""
    cloud = DOCKERFILE_CLOUD.read_text(encoding="utf-8")
    assert DOCKERFILE_OSS.exists(), "Dockerfile.oss 必须存在"
    oss = DOCKERFILE_OSS.read_text(encoding="utf-8")
    assert re.search(r"(?m)^COPY frontend/ ", cloud), "Cloud Dockerfile 不得移除 frontend COPY"
    for line in oss.splitlines():
        assert not (line.strip().startswith("COPY") and "frontend" in line), \
            f"Dockerfile.oss 不得 COPY frontend：{line}"
    # 布局/运行契约一致：同 base、同 WORKDIR、同启动命令
    for token in ("FROM python:3.14-slim", "WORKDIR /app/backend",
                  'CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]'):
        assert token in cloud and token in oss, f"两变体契约不一致：{token}"


@pytest.mark.skipif(
    not docker_available or os.environ.get("ACP_B32_BUILD_CHECK") != "1",
    reason="需要 docker CLI + ACP_B32_BUILD_CHECK=1（全量构建数分钟，默认跳过）",
)
def test_oss_dockerfile_builds_from_no_frontend_context() -> None:
    """可复现构建检查（env 门控）：构造无 frontend 的构建上下文，
    `docker build -f Dockerfile.oss` 必须成功。"""
    import tempfile

    ctx = Path(tempfile.mkdtemp(prefix="nofe_b32_build_"))
    (ctx / "backend").mkdir(parents=True)
    shutil.copytree(BACKEND / "app", ctx / "backend" / "app")
    for name in ("requirements.lock.txt", "requirements.txt", "requirements-dev.txt"):
        src = BACKEND / name
        if src.exists():
            shutil.copy2(src, ctx / "backend" / name)
    shutil.copy2(REPO_ROOT / "VERSION", ctx / "VERSION")
    shutil.copy2(DOCKERFILE_OSS, ctx / "Dockerfile.oss")
    proc = subprocess.run(
        ["docker", "build", "-f", "Dockerfile.oss",
         "-t", "acp-oss-nofe-b32-check", "."],
        cwd=str(ctx),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1200,
    )
    assert proc.returncode == 0, (
        f"无前端构建失败\nstdout={proc.stdout[-2000:]}\nstderr={proc.stderr[-2000:]}"
    )
