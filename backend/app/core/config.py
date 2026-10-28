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

    # P0（Delivery 可靠性 · 观测与配置）：WeCom 智能机器人的 **ACK 等待窗口**
    # （发帧 → 等待渠道 errcode 回执的上限）配置化。
    #
    # - 默认 10.0s = `wecom_bot.DEFAULT_ACK_WAIT_TIMEOUT_S` 的**既有硬编码值**
    #   逐字搬来 ⇒ 零行为变化（仅把常量变成可配置项）；
    # - 该窗口**只**覆盖「发帧→ACK」这一段，与 Agent 执行时长**互不叠加、
    #   互不换算**（见 wecom_bot.py 模块 docstring）；
    # - gt=0：非正值会让 Event.wait 立即返回（等于关掉 ACK 校验 → 假绿），
    #   故非法值在进程启动时立刻失败（与 DRIVER_* 的 fail-fast 纪律同款）。
    WECOM_BOT_ACK_WAIT_TIMEOUT_SECONDS: float = Field(default=10.0, gt=0)

    # P2 Delivery Recovery（投递恢复驱动）——**默认关闭**。
    #
    # 启用顺序（runbook，见 docs/step-p2-delivery-recovery-implementation.md）：
    #   迁移 → 部署 P2 代码（驱动保持关闭）→ 排空旧 Worker → 一次性回填 →
    #   核验查询 → **另行授权**后开启本开关。
    RECOVERY_ENABLED: bool = False
    RECOVERY_POLL_INTERVAL_SECONDS: float = Field(default=10.0, gt=0)
    RECOVERY_PICKUP_LIMIT: int = Field(default=10, ge=1)
    RECOVERY_RECLAIM_EVERY: int = Field(default=30, ge=1)
    # 投递 attempt 的**租约**长度（秒）。⚠️ **保守默认值，未经生产实测**（待 P4）：
    # 必须大于「发送耗时上界 + ACK 等待窗口 + 余量」。⚠️ 租约**不能**消除重复
    # 发送风险——旧 attempt 可能仍在外部调用中（详见 P2 设计报告 §5）。
    RECOVERY_LEASE_SECONDS: int = Field(default=300, ge=30)
    # **总** attempt 次数上限（含首次投递）；达上限后不再重投（转人工裁决）。
    RECOVERY_MAX_ATTEMPTS: int = Field(default=3, ge=1)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """返回进程级单例配置（测试内可直接构造 Settings 以避免缓存污染）。"""
    return Settings()
