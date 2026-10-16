"""Step 14.2.3.C.1 —— Dependency Reproducibility（确定性依赖）。

只验证一件事：依赖文件形态保证「干净环境获得确定的运行时依赖集」，且
**不**引入 Poetry / uv / Pipenv 等新工具链（14.1 拍板：MVP 只用 pip + `==`）。

守卫的不变量：
1. `requirements.txt`（运行时直接依赖，人维护的事实源）：全部 `==` pin，
   绝不含 dev 依赖（pytest / httpx）；
2. `requirements.lock.txt`（Docker 实际消费）：全部 `==`，包含全部直接依赖
   且版本一致，绝不含 dev 依赖；
3. `requirements-dev.txt`：显式包含运行时 + dev 依赖；
4. `Dockerfile`：只装锁定集，不装 dev 依赖。
"""

import re
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]

RUNTIME_REQUIREMENTS = BACKEND_DIR / "requirements.txt"
LOCK_FILE = BACKEND_DIR / "requirements.lock.txt"
DEV_REQUIREMENTS = BACKEND_DIR / "requirements-dev.txt"
DOCKERFILE = BACKEND_DIR.parent / "Dockerfile"

RUNTIME_DIRECT_DEPS = [
    "fastapi", "uvicorn", "pydantic-settings", "SQLAlchemy", "cryptography",
    # Cloud-0.1.1-A：Cloud schema evolution（alembic）+ PostgreSQL driver（psycopg）
    "alembic", "psycopg",
]
DEV_ONLY_DEPS = ["pytest", "httpx"]

PIN_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9,._-]+\])?==[A-Za-z0-9][A-Za-z0-9._-]*$"
)


def _pinned_lines(path: Path) -> list[str]:
    """非注释、非 -r 引用的需求行（**只看内容行，忽略注释文字**）。"""
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-r "):
            continue
        lines.append(line)
    return lines


# ---------------------------------------------------------------- 1. 直接依赖文件


def test_runtime_requirements_fully_pinned() -> None:
    """requirements.txt：每一行都是 `name==version`，无任何浮动约束。"""
    lines = _pinned_lines(RUNTIME_REQUIREMENTS)
    assert lines, "requirements.txt 不应为空"
    offenders = [ln for ln in lines if not PIN_PATTERN.match(ln)]
    assert not offenders, (
        f"requirements.txt 存在非 `==` pin 的行（确定性缺口）：{offenders}")


def test_runtime_requirements_exclude_dev_deps() -> None:
    """dev 依赖（pytest / httpx）绝不进入运行时直接依赖文件（只看内容行）。"""
    names = [ln.split("==")[0].split("[")[0].lower() for ln in _pinned_lines(RUNTIME_REQUIREMENTS)]
    offenders = [d for d in DEV_ONLY_DEPS if d in names]
    assert not offenders, f"运行时 requirements 混入了 dev 依赖：{offenders}"


def test_runtime_direct_deps_present() -> None:
    """运行时直接依赖必须存在且 `==` pin（ACP 的最小运行面 + Cloud PG/Alembic）。"""
    lines = _pinned_lines(RUNTIME_REQUIREMENTS)
    names = {ln.split("==")[0].split("[")[0].lower(): ln for ln in lines}
    missing = [d for d in RUNTIME_DIRECT_DEPS if d.lower() not in names]
    assert not missing, f"运行时直接依赖缺失：{missing}"


# ---------------------------------------------------------------- 2. 锁定文件


def test_lock_fully_pinned_and_contains_direct_deps() -> None:
    """requirements.lock.txt：全部 `==`；包含全部直接依赖且版本一致。"""
    lines = _pinned_lines(LOCK_FILE)
    assert lines, "lock 文件不应为空"
    offenders = [ln for ln in lines if not PIN_PATTERN.match(ln)]
    assert not offenders, f"lock 文件存在非 `==` pin 的行：{offenders}"

    lock_map = {ln.split("==")[0].split("[")[0].lower(): ln for ln in lines}
    for dep in RUNTIME_DIRECT_DEPS:
        assert dep.lower() in lock_map, f"lock 缺少直接依赖：{dep}"

    # 版本一致性：requirements.txt 中的直接依赖版本必须与 lock 完全一致。
    # 比较时剥离 extras（freeze 会把 `uvicorn[standard]` 规范化为 `uvicorn`）。
    for ln in _pinned_lines(RUNTIME_REQUIREMENTS):
        req_name, _, req_version = ln.partition("==")
        name = req_name.split("[")[0].lower()
        lock_name, _, lock_version = lock_map[name].partition("==")
        assert lock_version == req_version, (
            f"直接依赖与 lock 版本不一致：{name}: "
            f"requirements={req_version} lock={lock_version}")


def test_lock_excludes_dev_deps() -> None:
    """dev 依赖绝不进入锁定集（只看内容行，它们不进镜像）。"""
    names = [ln.split("==")[0].split("[")[0].lower() for ln in _pinned_lines(LOCK_FILE)]
    offenders = [d for d in DEV_ONLY_DEPS if d in names]
    assert not offenders, f"锁定集混入了 dev 依赖：{offenders}"


# ---------------------------------------------------------------- 3. dev 文件


def test_dev_requirements_includes_runtime_and_dev() -> None:
    """requirements-dev.txt：显式 `-r requirements.txt` + pin 的 dev 依赖。"""
    text = DEV_REQUIREMENTS.read_text(encoding="utf-8")
    assert "-r requirements.txt" in text, "dev 文件应引用运行时依赖集"

    dev_lines = _pinned_lines(DEV_REQUIREMENTS)
    dev_names = {ln.split("==")[0].split("[")[0].lower() for ln in dev_lines}
    for dep in DEV_ONLY_DEPS:
        assert dep in dev_names, f"dev 依赖缺失：{dep}"
    floating = [ln for ln in dev_lines if not PIN_PATTERN.match(ln)]
    assert not floating, f"dev 依赖存在非 `==` pin：{floating}"


# ---------------------------------------------------------------- 4. Dockerfile


def test_dockerfile_installs_lock_not_dev() -> None:
    """Dockerfile：消费锁定集；不装 dev 依赖（只看**非注释**行）。"""
    code_lines = [
        ln.strip()
        for ln in DOCKERFILE.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    code = "\n".join(code_lines)
    assert "requirements.lock.txt" in code, "Dockerfile 应消费 requirements.lock.txt"
    assert "-r /app/backend/requirements.lock.txt" in code
    assert "requirements-dev" not in code, "dev 依赖不得进入运行时镜像"


def test_no_new_package_manager_toolchain() -> None:
    """不引入 Poetry / uv / Pipenv 等新工具链（14.1 拍板：MVP 只用 pip）。

    只扫内容行；注释里对工具链的**排除性提及**不算引入。
    """
    for path in (RUNTIME_REQUIREMENTS, LOCK_FILE, DEV_REQUIREMENTS):
        content = "\n".join(_pinned_lines(path)).lower()
        for marker in ("poetry", "pipenv", "uv ", "uv==", "pyproject.toml"):
            assert marker not in content, f"{path.name} 引入了新工具链痕迹：{marker}"
