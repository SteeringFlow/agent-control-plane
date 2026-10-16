"""应用配置入口（Step 5.1：最小集）。

第一阶段只暴露三个变量：APP_NAME / DATABASE_URL / ENVIRONMENT。
默认使用本地 SQLite 文件，Step 5.1 不连接数据库。

刻意暂不引入：Redis、Kafka、Celery、JWT、OAuth、Vault、Kubernetes 相关配置。
它们当前没有真实需求，等对应 Vertical Slice 出现时再按最小集加入。

同样地，core/errors.py 尚未建立：目前没有任何错误语义需要被统一表达，
等出现第一个跨层错误类型时再补，避免为了「架构完整」留下无用代码。
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """进程配置。环境变量与 backend/.env 均可覆盖默认值。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    APP_NAME: str = "Agent Control Plane"
    # 14.2.3.C.2：默认值与 Pilot 持久化路径统一为 ./data/agent_control_plane.db
    # （该子目录即 compose named volume 的挂载点；本地开发时相对 WORKDIR/backend）。
    # 仍允许经 backend/.env 或环境变量显式覆盖。
    DATABASE_URL: str = "sqlite:///./data/agent_control_plane.db"
    ENVIRONMENT: str = "development"

    # Cloud-0.1.1-C：Browser Auth 强制开关（唯一的认证模式开关）。
    #   False（默认）→ OSS / 本地单租户：无 Cookie 时回退 TENANT_LOCAL，
    #                  行为与隔离前逐字节一致（既有测试零改动）；
    #   True（Cloud 多租户）→ 无 Cookie / 无效 Cookie → 401。
    # 刻意只保留这一个开关：不引入 OAuth / SSO / RBAC / JWT 相关配置。
    AUTH_REQUIRED: bool = False

    # 12.9.4.16：Production Driver 的节奏配置——只有节奏与批量，没有任何业务语义。
    # 注意 DRIVER_RECLAIM_EVERY 是「每多少轮做一次回收」的 cadence，与
    # DRIVER_STALE_THRESHOLD_SECONDS（「多老算孤儿」，Core 语义）严格分离。
    #
    # 12.9.4.18A：fail-fast 边界校验（pydantic 约束，Settings 实例化即裁决）——
    # 非法值必须在进程启动时立刻失败，而不是等 Driver 在运行期表现出实测病理：
    #   - 间隔 <= 0  → Event.wait(负数) 忙等 CPU 空转；
    #   - pickup_limit < 1 → LIMIT 0 静默不捡 / SQLite 负 LIMIT 整表入内存；
    #   - stale_threshold < 5 → 回收刚刚还在执行的行（假孤儿）。
    DRIVER_POLL_INTERVAL_SECONDS: float = Field(default=2.0, gt=0)
    DRIVER_PICKUP_LIMIT: int = Field(default=10, ge=1)
    DRIVER_RECLAIM_EVERY: int = Field(default=30, ge=1)
    DRIVER_STALE_THRESHOLD_SECONDS: int = Field(default=300, ge=5)
    DRIVER_BACKOFF_INTERVAL_SECONDS: float = Field(default=5.0, gt=0)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """返回进程级单例配置（测试内可直接构造 Settings 以避免缓存污染）。"""
    return Settings()
