"""Step 12.9.4.18A — GA Configuration & Trigger Declaration Closure（A1 部分）。

只锁定一件事：Driver 的五个节奏配置在 **Settings 实例化时** fail-fast——

    poll_interval_seconds      > 0
    backoff_interval_seconds   > 0
    pickup_limit               >= 1
    reclaim_every              >= 1
    stale_threshold_seconds    >= 5

背景（12.9.4.18 Q4 实测病理）：这些字段原先无任何校验，非法值会等到 Driver
运行期才暴露——间隔 <= 0 忙等 CPU 空转、pickup_limit < 1 静默不捡 / 负数整表
入内存、stale_threshold < 5 回收假孤儿。校验放在组合根之前，进程一起动就死。

边界裁决：不改 Driver / Worker / Core（校验属于配置面，不属于执行面）。
"""

import pytest
from pydantic import ValidationError

from app.core.config import Settings

# 五个字段全部显式给出，屏蔽 .env / 环境变量的干扰（init kwargs 优先级最高）。
_VALID: dict = {
    "DRIVER_POLL_INTERVAL_SECONDS": 2.0,
    "DRIVER_PICKUP_LIMIT": 10,
    "DRIVER_RECLAIM_EVERY": 30,
    "DRIVER_STALE_THRESHOLD_SECONDS": 300,
    "DRIVER_BACKOFF_INTERVAL_SECONDS": 5.0,
}

# (字段名, 合法边界值)。取约束的**最小合法值**作为边界——比它小一格就非法。
_BOUNDARY: list[tuple[str, object]] = [
    ("DRIVER_POLL_INTERVAL_SECONDS", 0.001),
    ("DRIVER_BACKOFF_INTERVAL_SECONDS", 0.001),
    ("DRIVER_PICKUP_LIMIT", 1),
    ("DRIVER_RECLAIM_EVERY", 1),
    ("DRIVER_STALE_THRESHOLD_SECONDS", 5),
]


def _settings(**overrides) -> Settings:
    return Settings(**{**_VALID, **overrides})


# ------------------------------------------------------------------ 合法值


@pytest.mark.parametrize(("field", "value"), _BOUNDARY)
def test_a1_valid_boundary_value_is_accepted(field: str, value) -> None:
    """每个字段的最小合法值必须被接受，且值原样保留（不静默钳制改写）。"""
    settings = _settings(**{field: value})
    assert getattr(settings, field) == value


def test_a1_all_defaults_are_valid() -> None:
    """默认配置必须整体合法——否则任何未配置环境都无法启动。"""
    settings = Settings(
        **{k: v for k, v in _VALID.items()})  # 显式五项，隔离 env 干扰
    assert settings.DRIVER_POLL_INTERVAL_SECONDS == 2.0
    assert settings.DRIVER_PICKUP_LIMIT == 10
    assert settings.DRIVER_RECLAIM_EVERY == 30
    assert settings.DRIVER_STALE_THRESHOLD_SECONDS == 300
    assert settings.DRIVER_BACKOFF_INTERVAL_SECONDS == 5.0


# ------------------------------------------------------------------ 非法零值


@pytest.mark.parametrize(
    "field", [f for f, _ in _BOUNDARY], ids=[f for f, _ in _BOUNDARY])
def test_a1_zero_is_rejected_for_every_field(field: str) -> None:
    """五个字段的 0 全部非法（间隔/阈值语义上 0 无意义，批量 0 = 静默不捡）。"""
    with pytest.raises(ValidationError):
        _settings(**{field: 0})


# ------------------------------------------------------------------ 非法负值


@pytest.mark.parametrize(
    "field", [f for f, _ in _BOUNDARY], ids=[f for f, _ in _BOUNDARY])
def test_a1_negative_is_rejected_for_every_field(field: str) -> None:
    """五个字段的 -1 全部非法——负间隔会忙等空转，负阈值会回收假孤儿。"""
    with pytest.raises(ValidationError):
        _settings(**{field: -1})


# ------------------------------------------------------------------ 下界之下

_INT_FIELDS: list[tuple[str, int]] = [
    (f, v) for f, v in _BOUNDARY if isinstance(v, int)
]


@pytest.mark.parametrize(
    ("field", "value"), _INT_FIELDS, ids=[f for f, _ in _INT_FIELDS])
def test_a1_below_boundary_is_rejected(field: str, value: int) -> None:
    """整数字段在最小合法值之下一格必须被拒（边界收紧一格 → fail-fast）。

    （float 间隔的约束是 gt=0，无离散下界；其「下界之下」即 0，
    已由 zero 用例覆盖。）
    """
    with pytest.raises(ValidationError):
        _settings(**{field: value - 1})
