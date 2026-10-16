"""配置最小集：默认值可用，环境变量可覆盖。"""

from app.core.config import Settings


def test_defaults_are_development_friendly() -> None:
    settings = Settings(_env_file=None)
    assert settings.APP_NAME == "Agent Control Plane"
    # 14.2.3.C.2：默认值与 Pilot 持久化路径统一
    assert settings.DATABASE_URL == "sqlite:///./data/agent_control_plane.db"
    assert settings.ENVIRONMENT == "development"


def test_environment_variables_override_defaults(monkeypatch) -> None:
    monkeypatch.setenv("APP_NAME", "ACP Dev")
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./tmp.db")

    settings = Settings(_env_file=None)
    assert settings.APP_NAME == "ACP Dev"
    assert settings.ENVIRONMENT == "production"
    assert settings.DATABASE_URL == "sqlite:///./tmp.db"


def test_unknown_env_variables_are_ignored(monkeypatch) -> None:
    """避免某个无关环境变量直接把进程打挂。"""
    monkeypatch.setenv("SOME_UNRELATED_FLAG", "1")
    settings = Settings(_env_file=None)
    assert settings.APP_NAME == "Agent Control Plane"
