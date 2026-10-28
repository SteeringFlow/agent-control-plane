"""Step 10.1.5 —— Repository Baseline & Credential Safety 机器化断言。

定位：这不是"安全平台"，只是一组**防呆断言**——防止日后有人 `git add .`
把 `backend/.env`（真实 Tencent ACP credentials）一起提交。

目标链（10.1.5 契约）：

    当前已验证 Runtime → Git 可回滚基线 → Credential 安全边界 → 可继续开发产品层

断言面（纯静态 + git 索引/历史，不执行任何生产代码路径）：

    A. git 仓库已初始化；**internal 源仓库**根提交是 BASELINE-001
       （Clean Export 仓库不继承内部 history，改验发布隔离 —— 见「仓库来源边界」）
    B. `.gitignore` 覆盖 credential 边界（.env / *.env / !.env.example 三层规则）
    C. `backend/.env` 存在于磁盘（LOCAL ONLY）且被 git ignore
    D. `backend/.env.example` 被 track，且是 SAFE TEMPLATE
       （全部赋值 = 已知白名单默认值或空值，TENCENT_* 四键必须空值）
    E. 版本库索引与全部历史中都不存在真实 `.env`
    F. `git add --dry-run .` 永远不会暂存真实 `.env`（复刻误操作场景）

边界（10.1.5.4 冻结）：本测试只守护 `.gitignore + .env.example` 这一层；
不引入 Secret Manager / Vault / OAuth / CI / Secret Rotation。
git 相关检查在非 git 环境下 fail 是**预期行为**（基线丢失 = 工程状态不可审计）。

仓库来源边界（D1 Release Export）：同一份测试会被 internal 源仓库与 Clean Export
仓库两处执行，而两者承载**互不相同**的 repository invariant，故显式分离、互不承担：

    internal 源仓库 → A 断言根提交 = BASELINE-001（内部治理：基线不可丢）
    export   仓库  → 断言单一 root commit / 未继承内部 history（发布隔离）

来源判定复用发布流程**既有**控制面文件 `.oss-export-exclude.txt`：按
`.oss-export-` 前缀规则它永不进入 Public Export，故「存在 ⇒ internal 源仓库」。
不依赖绝对路径，不新增环境变量或额外机制；BASELINE-001 断言本身未被削弱。
"""

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
GITIGNORE_PATH = REPO_ROOT / ".gitignore"
ENV_EXAMPLE_PATH = BACKEND_DIR / ".env.example"
LOCAL_ENV_PATH = BACKEND_DIR / ".env"

# ---------------------------------------------------------------------------
# 仓库来源（provenance）：B3.3 起统一委托共享契约 tests/oss_export_boundary.py
#
# 三态（保守契约）：source（完整源仓库，正证据 = 内部控制面文件存在）/
# oss_export（有效导出树，正证据 = OSS_EXPORT_MANIFEST.txt 且无 frontend/）/
# unknown（部分检出或矛盾状态）。
# **unknown 不再静默归入 export**——需要特定来源的测试在 unknown 下显式失败
# （见本文件两处 provenance 门），绝不静默跳过检查。
# 本文件保留 `_repo_provenance`/`INTERNAL_PROVENANCE`/`EXPORT_PROVENANCE` 兼容名，
# 值映射：source→internal，oss_export→export，unknown→"unknown"。
# ---------------------------------------------------------------------------
from tests.oss_export_boundary import UNKNOWN_PROVENANCE as _UNKNOWN  # noqa: E402
from tests.oss_export_boundary import repo_provenance as _shared_repo_provenance  # noqa: E402

INTERNAL_PROVENANCE = "internal"
EXPORT_PROVENANCE = "export"


def _repo_provenance() -> str:
    """判定当前仓库来源（B3.3 三态映射；兼容旧二值调用点）。"""
    prov = _shared_repo_provenance()
    return {("source"): INTERNAL_PROVENANCE,
            ("oss_export"): EXPORT_PROVENANCE}.get(prov, _UNKNOWN)


# .env.example 的完整白名单：SAFE TEMPLATE 的每一行都必须精确命中这里。
# 新增键时必须显式更新本表（值为空串 = 该键只提供键名、不含任何秘密）。
ENV_EXAMPLE_WHITELIST = {
    # 14.2.3.C.2：模板与 Settings 字段面一一对齐（10 键 = 10 字段）；
    # DATABASE_URL 与 Pilot 持久化路径统一为 ./data/agent_control_plane.db。
    # TENCENT_* 属 env-gated live test 的 test-only 配置，**不在**本模板。
    # Cloud-0.1.1-C：+ AUTH_REQUIRED（Browser Auth 唯一开关，默认 false = OSS 行为不变）。
    # P0（Delivery 可靠性）：+ WECOM_BOT_ACK_WAIT_TIMEOUT_SECONDS
    # （ACK 等待窗口，默认 10.0 = 既有硬编码常量逐字搬来，零行为变化）。
    "APP_NAME": "Agent Control Plane",
    "ENVIRONMENT": "development",
    "DATABASE_URL": "sqlite:///./data/agent_control_plane.db",
    "AUTH_REQUIRED": "false",
    "DRIVER_POLL_INTERVAL_SECONDS": "2.0",
    "DRIVER_PICKUP_LIMIT": "10",
    "DRIVER_RECLAIM_EVERY": "30",
    "DRIVER_STALE_THRESHOLD_SECONDS": "300",
    "DRIVER_BACKOFF_INTERVAL_SECONDS": "5.0",
    "WECOM_BOT_ACK_WAIT_TIMEOUT_SECONDS": "10.0",
    # P2 Delivery Recovery（默认关闭；runbook 见 docs/step-p2-*.md）
    "RECOVERY_ENABLED": "false",
    "RECOVERY_POLL_INTERVAL_SECONDS": "10.0",
    "RECOVERY_PICKUP_LIMIT": "10",
    "RECOVERY_RECLAIM_EVERY": "30",
    "RECOVERY_LEASE_SECONDS": "300",
    "RECOVERY_MAX_ATTEMPTS": "3",
}


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    """在仓库根运行只读 git 命令（本测试绝不执行任何会改动 git 状态的命令）。"""
    return subprocess.run(
        ["git", *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _gitignore_rules() -> list[str]:
    """解析 .gitignore 的有效规则行（去空行 / 注释）。"""
    text = GITIGNORE_PATH.read_text(encoding="utf-8")
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


# ====================================================================== A. Git Baseline


def test_git_repository_initialized() -> None:
    """git 仓库已初始化（BASELINE-001 的前提）。"""
    assert (REPO_ROOT / ".git").is_dir(), "仓库根缺少 .git —— Git 基线丢失"
    r = _git("rev-parse", "--git-dir")
    assert r.returncode == 0, "git rev-parse 失败：当前目录不是有效 git 仓库"


def test_baseline_commit_exists() -> None:
    """internal 源仓库：根提交存在且是 BASELINE-001（第一个可审计的工程状态）。

    仅适用于 internal 源仓库。Clean Export 仓库按发布边界**不继承**内部 history
    （其唯一 root commit 是公开发布的初始提交），故在该来源下 skip。
    **BASELINE-001 断言本身未被削弱**：它在 internal 源仓库仍是硬断言。
    """
    provenance = _repo_provenance()
    if provenance == EXPORT_PROVENANCE:
        pytest.skip(
            "Clean Export 仓库不继承内部 BASELINE-001（history isolation）；"
            "发布隔离由 test_export_repository_history_isolation 独立守护"
        )
    if provenance == _UNKNOWN:
        pytest.fail(
            "provenance unknown（部分检出/缺导出标记）——BASELINE-001 内部断言无法"
            "安全执行；请在完整源仓库（含 .oss-export-exclude.txt）或带 "
            "OSS_EXPORT_MANIFEST.txt 的有效导出树中运行（B3.3 保守契约）"
        )
    r = _git("rev-list", "--max-parents=0", "HEAD")
    assert r.returncode == 0, "无法读取提交历史"
    roots = r.stdout.split()
    assert roots, "仓库没有任何提交 —— baseline 丢失"
    subject = _git("log", "-1", "--format=%s", roots[0]).stdout.strip()
    assert "BASELINE-001" in subject, f"根提交不是 BASELINE-001：{subject!r}"


def test_export_repository_history_isolation() -> None:
    """Clean Export 仓库：发布隔离 invariant —— 恰一个提交、恰一个 root commit，
    且历史中不含内部 baseline 标记。

    与 test_baseline_commit_exists 分属**两个不同 repository** 的 invariant，
    互不承担：internal 管「BASELINE-001 存在」，export 管「内部 history 未继承」。
    """
    provenance = _repo_provenance()
    if provenance == "internal":
        pytest.skip("内部源仓库不适用发布隔离断言（其历史包含 BASELINE-001 baseline）")
    if provenance == _UNKNOWN:
        pytest.fail(
            "provenance unknown（部分检出/缺导出标记）——发布隔离断言无法安全执行；"
            "请在有效导出树（含 OSS_EXPORT_MANIFEST.txt）运行（B3.3 保守契约）"
        )

    count = _git("rev-list", "--count", "HEAD")
    assert count.returncode == 0, "无法读取提交历史"
    n = count.stdout.strip()
    assert n == "1", (
        f"Clean Export 仓库必须恰有 1 个提交（内部 history 未被隔离）：实际 {n} 个"
    )

    roots = _git("rev-list", "--max-parents=0", "HEAD").stdout.split()
    assert len(roots) == 1, (
        f"Clean Export 仓库必须恰有 1 个 root commit：实际 {len(roots)} 个"
    )

    subjects = _git("log", "--all", "--format=%s").stdout
    assert "BASELINE-001" not in subjects, (
        "Clean Export 历史中出现内部 baseline 标记 —— 内部 history 被继承"
    )


# ====================================================================== B. .gitignore 规则


def test_gitignore_covers_credential_boundary() -> None:
    """.gitignore 必须三层覆盖：.env / *.env / !.env.example。

    - .env          —— 精确匹配任意目录下的 .env
    - *.env         —— 覆盖 .env.local / .env.production 等所有变体
    - !.env.example —— 安全模板白名单（必须保持可入库）
    """
    assert GITIGNORE_PATH.exists(), "缺少 .gitignore"
    rules = _gitignore_rules()
    assert ".env" in rules, ".gitignore 缺少精确规则 .env"
    assert "*.env" in rules, ".gitignore 缺少通配规则 *.env（无法覆盖 .env.local 等变体）"
    assert "!.env.example" in rules, ".gitignore 缺少 !.env.example 白名单"


# ====================================================================== C. 本地 .env = LOCAL ONLY


def test_local_env_is_ignored() -> None:
    """backend/.env 存在于磁盘（LOCAL ONLY）且被 git ignore。

    本机未配置凭证（.env 不存在）时 skip —— ignore 语义无对象可验。
    """
    if not LOCAL_ENV_PATH.exists():
        pytest.skip("backend/.env 不存在（本机未配置凭证），ignore 检查无对象")
    r = _git("check-ignore", "-v", "backend/.env")
    assert r.returncode == 0, (
        "backend/.env 未被 .gitignore 忽略 —— 凭证可能进入暂存区！"
        f" stdout={r.stdout!r} stderr={r.stderr!r}"
    )


# ====================================================================== D. .env.example = SAFE TEMPLATE


def test_env_example_is_safe_template() -> None:
    """backend/.env.example 被 track，且每个赋值都精确命中白名单。

    白名单外任何键、或 TENCENT_* 键携带非空值，都视为模板被污染。
    """
    # 1) 必须被版本库 track（新人照抄的来源）
    r = _git("ls-files", "--", "backend/.env.example")
    assert r.returncode == 0
    assert "backend/.env.example" in r.stdout.split(), (
        "backend/.env.example 未被版本库跟踪 —— 安全模板丢失"
    )

    # 2) 逐行白名单校验
    assert ENV_EXAMPLE_PATH.exists(), "缺少 backend/.env.example"
    seen: dict[str, str] = {}
    for raw in ENV_EXAMPLE_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        assert sep == "=", f".env.example 存在非法行（缺 =）：{raw!r}"
        key = key.strip()
        assert key in ENV_EXAMPLE_WHITELIST, (
            f".env.example 出现白名单外的键：{key!r} —— 模板被污染或需更新白名单"
        )
        assert value == ENV_EXAMPLE_WHITELIST[key], (
            f".env.example 的 {key} 值与安全模板不符（疑似写入真实值）：{value!r}"
        )
        seen[key] = value

    # 3) 白名单全部键必须出现（防止键被删导致新人环境缺配置）
    missing = sorted(set(ENV_EXAMPLE_WHITELIST) - set(seen))
    assert not missing, f".env.example 缺少键：{missing}"

    # 4) test-only 键（TENCENT_*，live test 专用）不得出现在模板中——
    #    不让测试配置冒充 application runtime configuration（14.2.3.C.2）
    test_only = [key for key in seen if key.startswith("TENCENT")]
    assert not test_only, (
        f".env.example 不得包含 test-only 键（它们由 live test 自身配置管理）：{test_only}")


# ====================================================================== E. 索引与历史零泄漏


def test_repository_does_not_track_local_env() -> None:
    """版本库索引中不存在任何真实 .env（用户要求的防呆断言本体）。"""
    r = _git("ls-files")
    assert r.returncode == 0, "git ls-files 失败"
    tracked = r.stdout.splitlines()
    bad = [
        p for p in tracked
        if Path(p).name == ".env"
        or (Path(p).name.startswith(".env.") and Path(p).name != ".env.example")
    ]
    assert not bad, f"以下 .env 文件被版本库跟踪：{bad}"


def test_git_history_never_contains_real_env() -> None:
    """全部提交历史中 backend/.env 从未被提交（含被删的历史）。"""
    r = _git("log", "--all", "--format=%H", "--", "backend/.env")
    assert r.returncode == 0, "git log --all 失败"
    assert r.stdout.strip() == "", (
        "backend/.env 曾出现在提交历史中 —— 需要立即轮换 Token 并清洗历史"
    )


# ====================================================================== F. 误操作场景复刻


def test_git_add_dry_run_would_not_stage_env() -> None:
    """`git add --dry-run .` 的输出永不包含真实 .env。

    直接复刻最高频事故场景：有人随手 `git add .` 提交全部工作树。
    --dry-run 不改动 git 状态，只读预演。
    """
    r = _git("add", "--dry-run", ".")
    assert r.returncode == 0, f"git add --dry-run 失败：{r.stderr!r}"
    staged = [line.split("'")[1] for line in r.stdout.splitlines() if "add '" in line]
    bad = [
        p for p in staged
        if Path(p).name == ".env"
        or (Path(p).name.startswith(".env.") and Path(p).name != ".env.example")
    ]
    assert not bad, f"`git add .` 将会暂存以下 .env 文件：{bad}"
