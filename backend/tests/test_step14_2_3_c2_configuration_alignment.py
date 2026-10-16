"""Step 14.2.3.C.2 —— Configuration Source of Truth（三层各归其位）。

三层不是三个互相竞争的配置源，而是三个层次（14.2.3.C.2 冻结）：

    1. app/core/config.py     —— 配置契约（字段面 + 默认值 + 边界校验）
    2. backend/.env.example   —— 开发配置模板
    3. docker-compose.yml     —— Pilot deployment overrides

守卫的不变量：
1. `.env.example` 的键集合 == `Settings` 的字段集合（8 == 8，一一对齐）；
2. `DATABASE_URL` 默认值 / 模板值统一为 `./data/agent_control_plane.db`
   （与 Pilot 持久化路径一致）；compose overrides 同值；
3. test-only 的 `TENCENT_*` 键不出现在 `.env.example`（不冒充 runtime configuration）；
4. compose 的 Pilot overrides 块保持冻结（3 键原样）。
"""

import re
from pathlib import Path

from app.core.config import Settings

BACKEND_DIR = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = BACKEND_DIR / ".env.example"
COMPOSE_FILE = BACKEND_DIR.parent / "docker-compose.yml"

PILOT_DB_URL = "sqlite:///./data/agent_control_plane.db"


def _env_example_keys() -> set[str]:
    keys = set()
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\s*([A-Z][A-Z0-9_]*)\s*=", line)
        if match:
            keys.add(match.group(1))
    return keys


def _settings_field_names() -> set[str]:
    return set(Settings.model_fields.keys())


# ---------------------------------------------------------------- 1. 键面对齐


def test_env_example_aligned_with_settings_fields() -> None:
    """.env.example 的键集合与 Settings 字段集合**完全一致**（9 == 9）。"""
    keys = _env_example_keys()
    fields = _settings_field_names()

    assert keys == fields, (
        f"配置模板与 Settings 字段面未对齐："
        f"模板多出={sorted(keys - fields)}，模板缺少={sorted(fields - keys)}"
    )
    assert len(fields) == 9, f"Settings 字段面预期 9 个，实际 {len(fields)}"


# ---------------------------------------------------------------- 2. DATABASE_URL 统一


def test_database_url_default_matches_pilot_path() -> None:
    """Settings 默认值 = Pilot 持久化路径（./data/agent_control_plane.db）。"""
    settings = Settings(_env_file=None)
    assert settings.DATABASE_URL == PILOT_DB_URL


def test_database_url_template_matches_pilot_path() -> None:
    """.env.example 模板值 = Pilot 持久化路径。"""
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("DATABASE_URL"):
            assert line.split("=", 1)[1].strip() == PILOT_DB_URL
            break
    else:
        raise AssertionError(".env.example 缺少 DATABASE_URL 键")


def test_compose_override_value_unchanged() -> None:
    """compose 的 Pilot overrides 块冻结：DATABASE_URL 同值（本步不动 compose）。"""
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    assert "DATABASE_URL: sqlite:///./data/agent_control_plane.db" in text
    # Pilot overrides 三键原样保留
    for key in ("APP_NAME:", "ENVIRONMENT:", "DATABASE_URL:"):
        assert key in text


# ---------------------------------------------------------------- 3. test-only 键隔离


def test_env_example_contains_no_test_only_keys() -> None:
    """`TENCENT_*`（live-test 专用）不得出现在开发配置模板中。"""
    keys = _env_example_keys()
    offenders = [k for k in keys if k.startswith("TENCENT")]
    assert not offenders, f"test-only 配置不得冒充 runtime 配置：{offenders}"


def test_tencent_keys_still_available_for_live_tests() -> None:
    """TENCENT_* 的**真实消费面**（env-gated live test）不受影响：
    它们经环境变量/测试自身配置读取，只需要仓库里仍有消费点即可（不因模板清理而丢失语义）。"""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c",
         "from pathlib import Path; "
         "hits = [p for p in Path('.').rglob('test_step10_1_3_tencent_acp_wire.py')]; "
         "print('found' if hits else 'missing')"],
        cwd=BACKEND_DIR, capture_output=True, text=True,
    )
    assert "found" in result.stdout, "TENCENT live-test 消费文件不应被本步清理"
