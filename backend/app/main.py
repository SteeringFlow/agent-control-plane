"""FastAPI 应用装配入口。

Step 5.1 的最小运行链：

    HTTP -> FastAPI -> app.main -> /health -> 200 OK

Step 11.3 起：装配 Agent Management Vertical Slice（/api/agents 4 端点）。
Step 12.6 起：装配 Runtime 生产组合根（/api/channels/{id}/wecom/webhook）。
本模块是组合根：把 Application Service / Runtime 组件与生产 UoW（SessionLocal）
绑定。**只做装配，不含业务逻辑**——组件与测试装配范式
（test_step10_1_4_real_agent_runtime / test_step10_2_1_wecom_webhook /
test_step10_8_3_runtime_coordinator_result_integration）逐一对齐。
"""

import functools
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles

from app.adapters.agents.acp import ACPAdapter
from app.adapters.agents.local import LocalAgentAdapter
from app.adapters.agents.registry import MultiAgentAdapter
from app.adapters.agents.workbuddy import (
    WorkBuddyManagementClient,
    WorkBuddyProvisioner,
)
from app.adapters.channels.local import LocalChannelAdapter
from app.adapters.channels.registry import MultiChannelAdapter
from app.adapters.channels.wecom import WeComAdapter
from app.adapters.channels.wecom_bot import (
    CHANNEL_TYPE as WECOM_BOT_CHANNEL_TYPE,
    BotConnectionSpec,
    WeComBotAdapter,
    WeComBotConnectionRegistry,
    WeComBotTransport,
    connection_spec_from_channel,
)
from app.adapters.connection_verify import build_connection_verifier
from app.api.auth import (
    build_auth_api,
    build_authenticated_user_provider,
    build_tenant_scope_provider,
)
from app.api.health import router as health_router
from app.api.management.agents import build_agents_api
from app.api.management.bootstrap_arms import build_bootstrap_arms_api
from app.api.management.channels import build_channels_api
from app.api.management.connections import build_connections_api
from app.api.management.connection_conversations import (
    build_connection_conversations_api,
)
from app.api.management.identities import build_identities_api
from app.api.management.interactions import build_interactions_api
from app.api.management.users import build_users_api
from app.api.oauth_callback import build_workbuddy_redirect_api
from app.api.runtime.channels import build_channel_inbound_api
from app.api.version import build_version_api, read_commit, read_version
from app.application.access_authorization import AccessAuthorizationExecutor
from app.application.agent_dispatch import AgentDispatchExecutor
from app.application.agents import AgentApplicationService
from app.application.bootstrap import BootstrapCoordinator
from app.application.bootstrap_arms import BootstrapArmApplicationService
from app.application.browser_auth import BrowserAuthService
from app.application.channel_inbound import ChannelInboundService
from app.application.channels import ChannelApplicationService
from app.application.connections import ConnectionApplicationService
from app.application.connection_conversations import (
    ConnectionConversationApplicationService,
)
from app.application.coordinator import RuntimeCoordinator
from app.application.delivery import DeliveryExecutor
from app.application.driver import DriverConfig, ProductionDriver
from app.application.identities import IdentityApplicationService
from app.application.identity_resolution import IdentityResolutionExecutor
from app.application.interaction_observability import InteractionObservabilityService
from app.application.interaction_recording import PersistingCoordinator
from app.application.routing import RoutingExecutor
from app.application.session_creation import SessionCreationExecutor
from app.application.session_resolution import SessionResolutionExecutor
from app.application.delivery_recovery import (
    DeliveryRecoveryConfig,
    DeliveryRecoveryDriver,
)
from app.application.tenant_fanout import TenantFanoutDriver, TenantComposition
from app.application.tenant_provisioning import TenantProvisioningService
from app.application.users import UserApplicationService
from app.application.worker import Worker
from app.core.browser_auth import AuthenticatedUser
from app.core.config import get_settings
from app.core.tenant import TENANT_LOCAL, TenantScope
from app.persistence.database import init_database
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork
from app.repositories.channel_inbound import InboundChannelConfigError

# Step 11.5：frontend/ 目录由本应用直接托管（同源访问 /api，无 CORS、无构建链）。
FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"

LOGGER = logging.getLogger(__name__)


def _build_tenant_runtime(
    tenant_id: int, *, settings, acp_adapter, bot_registry,
) -> TenantComposition:
    """Cloud-0.1.3.3 —— per-tenant Runtime 组合（0.1.3.2 Gate 中心命题的落地）。

    **同一份单租户组合按 tenant 忠实实例化**：组件清单与既有测试装配范式
    （test_step10_1_4 / test_step10_2_1 / test_step10_2_2）逐一相等——同
    RuntimeCoordinator（六个冻结 kwarg）/ 同六 Executor / PersistingCoordinator
    包装 / Worker / ProductionDriver / Adapter 集合（3B-2.2 起渠道侧为
    wecom / wecom_bot / local）。唯一 delta 是 UoW 工厂经
    B §7 桥（`partial(SqlAlchemyUnitOfWork, tenant_id=tenant_id)`）绑定到本租户。

    红线兑现（0.1.3.2 Gate Q10）：tenant 不进任何组件签名——只经 uow_factory
    流入；Runtime Domain（Worker/Driver/Coordinator/Executor）保持 tenant-blind。
    """
    runtime_uow_factory = functools.partial(SqlAlchemyUnitOfWork, tenant_id=tenant_id)
    wecom_adapter = WeComAdapter(uow_factory=runtime_uow_factory)
    # 3B-2.2：WeCom 智能机器人 WebSocket transport（与 webhook 并列的第二形态）。
    # 连接是**进程级物理资源**（不属于租户），故 registry 由组合根持有并跨租户共享。
    wecom_bot_adapter = WeComBotAdapter(
        registry=bot_registry,
        # P0：ACK 等待窗口配置化（默认 10.0s = 既有硬编码常量，零行为变化）。
        ack_wait_timeout_s=settings.WECOM_BOT_ACK_WAIT_TIMEOUT_SECONDS,
    )
    channel_adapter = MultiChannelAdapter(
        {
            "wecom": wecom_adapter,
            "wecom_bot": wecom_bot_adapter,
            "local": LocalChannelAdapter(),
        },
        uow_factory=runtime_uow_factory,
    )
    agent_adapter = MultiAgentAdapter(
        {
            "acp": acp_adapter,  # 无状态实例跨租户共享；健康探针同源（0.1.2.2.2.1）
            "local": LocalAgentAdapter(),
        },
        uow_factory=runtime_uow_factory,
    )
    runtime_coordinator = RuntimeCoordinator(
        identity_resolution=IdentityResolutionExecutor(runtime_uow_factory),
        # Implementation Package 2 / Step 2：第 7 依赖——Access Authorization 与
        # 其余六段**同形注入**（不允许为少一个参数而让 Coordinator 自建执行器）。
        access_authorization=AccessAuthorizationExecutor(runtime_uow_factory),
        routing=RoutingExecutor(runtime_uow_factory),
        session_resolution=SessionResolutionExecutor(runtime_uow_factory),
        session_creation=SessionCreationExecutor(runtime_uow_factory),
        agent_dispatch=AgentDispatchExecutor(
            agent_adapter=agent_adapter, uow_factory=runtime_uow_factory),
        delivery=DeliveryExecutor(channel_adapter=channel_adapter),
    )
    phases = PersistingCoordinator(
        inner=runtime_coordinator, uow_factory=runtime_uow_factory)
    # M6.6-C3 / ADR-12：Bootstrap 随 Worker 注入 —— HTTP / WS / Driver 三条 Trigger
    # 共用同一边界（不存在 transport-specific divergence）。
    bootstrap = BootstrapCoordinator(uow_factory=runtime_uow_factory)
    worker = Worker(runtime=runtime_coordinator, phases=phases,
                    uow_factory=runtime_uow_factory, bootstrap=bootstrap)
    driver = ProductionDriver(
        source=phases,
        worker=worker,
        config=DriverConfig(
            poll_interval_seconds=settings.DRIVER_POLL_INTERVAL_SECONDS,
            pickup_limit=settings.DRIVER_PICKUP_LIMIT,
            reclaim_every=settings.DRIVER_RECLAIM_EVERY,
            stale_threshold_seconds=settings.DRIVER_STALE_THRESHOLD_SECONDS,
            backoff_interval_seconds=settings.DRIVER_BACKOFF_INTERVAL_SECONDS,
        ),
    )
    # P2：per-tenant 投递恢复驱动（**默认不启动**——组合根按 RECOVERY_ENABLED 决定）。
    # source/executor 都是同一个 PersistingCoordinator：候选发现与租约回收走它的
    # 只读/回收 phase，单条恢复走它的原子 claim ⇒ 与正常投递**共用**同一闸门。
    recovery_driver = DeliveryRecoveryDriver(
        source=phases,
        executor=phases,
        config=DeliveryRecoveryConfig(
            poll_interval_seconds=settings.RECOVERY_POLL_INTERVAL_SECONDS,
            pickup_limit=settings.RECOVERY_PICKUP_LIMIT,
            reclaim_every=settings.RECOVERY_RECLAIM_EVERY,
            lease_seconds=settings.RECOVERY_LEASE_SECONDS,
            max_attempts=settings.RECOVERY_MAX_ATTEMPTS,
        ),
    )
    return TenantComposition(
        tenant_id=tenant_id, worker=worker, driver=driver,
        phases=phases, channel_adapter=channel_adapter,
        recovery_driver=recovery_driver)


def create_app() -> FastAPI:
    """构造应用实例（工厂函数，便于测试与后续装配扩展）。"""
    settings = get_settings()

    # 3B-2.2：WeCom 智能机器人 WebSocket transport 的**进程级**连接注册表。
    # 连接是进程内物理资源（既不属于租户、也不属于 Runtime 域），故由组合根
    # 唯一持有，并注入每个 per-tenant 组合的渠道 Adapter（deliver 经它取活连接）。
    bot_registry = WeComBotConnectionRegistry()

    # Cloud-0.1.3.3：Runtime 面不再绑定单一租户——per-tenant Runtime 由下方
    # fan-out（ticker）按租户实例化；TENANT_LOCAL 只保留 OSS 缺省租户语义
    # （0.1.3.2 Gate Q7：消失于 Runtime 接线，保留为组合根缺省）。

    # ============================================================ Cloud-0.1.1-C
    # Browser Authentication（最小闭环）：
    #   Cookie → BrowserAuthenticator.verify() → AuthenticatedUser
    #         → TenantScope → existing tenant-scoped UoW
    #
    # auth UoW 工厂以 tenant_id 为入参：bootstrap（登录 / 会话解析）用 None，
    # register 用目标租户——这是 auth 边界的天然需要（Gate §4/§5）。
    def auth_uow_factory(tenant_id: int | None = None) -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(tenant_id=tenant_id)

    # Cloud-0.1.2.1：注册即建租户（Workspace Entry）——TenantProvisioner 是
    # tenants 表的唯一写入入口；Sign Up 后用户直接进入自己的 Tenant Workspace，
    # 无需额外「创建 Workspace」步骤（Gate 0.1.2：Workspace ≡ Tenant）。
    tenant_provisioner = TenantProvisioningService(uow_factory=auth_uow_factory)

    auth_service = BrowserAuthService(
        uow_factory=auth_uow_factory, provisioner=tenant_provisioner)

    # tenant 解析依赖：Cookie → TenantScope（有效 Cookie → 其租户；无效 → 401；
    # 无 Cookie → AUTH_REQUIRED ? 401 : OSS TENANT_LOCAL 回退）。
    tenant_scope_provider = build_tenant_scope_provider(
        auth_service, auth_required=settings.AUTH_REQUIRED)

    def _tenant_scoped_factory(scope: TenantScope):
        """把请求解析出的 TenantScope 绑成 tenant-scoped UoW 工厂（复用 B §7 桥）。"""
        return functools.partial(SqlAlchemyUnitOfWork, tenant_id=scope.tenant_id)

    # 12.9.4.16：per-tenant fan-out 在下方装配完成后才绑定；lifespan 到应用
    # 启动时才运行（届时 schema 已初始化，tenant 枚举可安全执行）。start/stop
    # 直接挂在 FastAPI lifespan 上——**不另造一套生命周期机制**。
    tenant_fanout: TenantFanoutDriver | None = None

    # 3B-2.2：WeCom Bot WS transport 同样「下方装配完成后才绑定」，start/stop 挂在
    # 同一条 lifespan 上（**不另造第二套生命周期机制**）。
    bot_transport: WeComBotTransport | None = None

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        """per-tenant Driver fan-out 生命周期 = 应用生命周期（唯一生命周期机制）。

        14.2.2：进入正常运行之前的**一次性 schema 初始化**——全新环境的 SQLite
        文件在此获得当前 ORM schema（8 张核心表），之后 Driver / Worker 只读写
        既有表，绝不承担建表责任（分工见 `persistence.database.init_database`）。
        """
        init_database()
        if tenant_fanout is not None:
            tenant_fanout.start()
        # P2：投递恢复 ticker（默认 None ⇒ 不启动；启用需先完成 runbook 步骤）。
        if recovery_fanout is not None:
            recovery_fanout.start()
        # 3B-2.2：WS 长连接在 schema 初始化之后启动——它要枚举 wecom_bot 渠道，
        # 且 `init_database()` 之前表可能尚不存在（全新环境 SQLite）。
        if bot_transport is not None:
            bot_transport.start()
        try:
            yield
        finally:
            if bot_transport is not None:
                bot_transport.stop()
            if tenant_fanout is not None:
                tenant_fanout.stop()
            # P2：投递恢复 ticker 的停止（与执行 fan-out 对称）。
            if recovery_fanout is not None:
                recovery_fanout.stop()

    app = FastAPI(
        title=settings.APP_NAME,
        description="Agent Control Plane —— 渠道 / Agent / 会话的统一控制面。",
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    app.include_router(health_router)

    # M5.2 —— Deployment Provenance（公开面）：Cloud runtime → version + commit。
    app.include_router(build_version_api())

    # M7-WB —— WorkBuddy Onboarding：重定向 URI 落地端点（公开，无 OAuth 逻辑）。
    # 客户把 GET /oauth/callback 填入 WorkBuddy「开放平台 → 第三方应用 → 重定向 URI」；
    # 端点不读 session / 不解析 tenant / 不解释 query（详见 api/oauth_callback.py）。
    app.include_router(build_workbuddy_redirect_api())
    LOGGER.info(
        "Agent Control Plane starting: version=%s commit=%s auth_required=%s",
        read_version(), read_commit(), settings.AUTH_REQUIRED,
    )

    # Cloud-0.1.1-C：Browser Auth router（register / login / logout）。
    # M7-AUI-10.8.23-C.5（ISS-07 最小修复）：Cloud（AUTH_REQUIRED=True）不暴露
    # POST /api/auth/register——公网自助注册只保留 /signup（注册即建新租户，
    # 调用者无法选择租户），杜绝匿名向既有租户创建凭据；OSS（False）保持原行为。
    app.include_router(build_auth_api(
        auth_service,
        cookie_secure=settings.ENVIRONMENT == "production",
        expose_register=not settings.AUTH_REQUIRED,
    ))

    # 11.3 / 11.8 / 11.11 / 12.4 / 13.1：管理面 Vertical Slice 装配。
    # Cloud-0.1.1-C：管理面服务**每请求**由解析出的 TenantScope 派生
    # （service_provider）——浏览器请求由此无缝进入 B 已冻结的 tenant-scoped UoW。
    # 无 Cookie 的 OSS / 本地单租户路径回退 TENANT_LOCAL（行为 == 隔离前）；
    # 测试经 build_xxx_api(service) 注入内存库实例，不经此路径。
    def _agents_service(
        scope: TenantScope = Depends(tenant_scope_provider),
    ) -> AgentApplicationService:
        return AgentApplicationService(uow_factory=_tenant_scoped_factory(scope))

    app.include_router(build_agents_api(service_provider=_agents_service))

    def _channels_service(
        scope: TenantScope = Depends(tenant_scope_provider),
    ) -> ChannelApplicationService:
        return ChannelApplicationService(uow_factory=_tenant_scoped_factory(scope))

    app.include_router(build_channels_api(service_provider=_channels_service))

    # Cloud-0.1.2.2.2.1：Connection Health 即时诊断探针。
    # ACPAdapter 无状态，单例同时用于「Runtime dispatch 出口」与「管理面健康探针」
    # ——probe 只读 Connection.config，与 send 同一条凭据纪律（Gate 0.1.2.2.2 §3/§4）。
    acp_adapter = ACPAdapter()

    # M6.6-E3-B：WorkBuddy Provider-native lazy provisioning。Verify 时（无缓存）
    # 才用 Management API 创建 Runtime 并把 acpLink 缓存回 Connection.config；
    # 非 WorkBuddy 连接退化为只读探针（无写入）。ACP 通信仍由既有 ACPAdapter 承担。
    workbuddy_provisioner = WorkBuddyProvisioner(
        probe=acp_adapter.probe, client=WorkBuddyManagementClient())

    # M6.6-F3：Provider-specific Verify 分派（薄 wiring，非 framework）。
    #   WorkBuddy（Agent 侧 provider，住 Connection.config）→ WorkBuddyProvisioner；
    #   WeCom（Channel 侧集成身份，Channel.type=="wecom"）→ WeComAdapter.verify_connection
    #     （只读 gettoken 凭据校验；WeCom 凭据住 Channel.config，故须先解析 Channel）；
    #   其它 → 既有 ACPAdapter.probe（F3-8，行为不变）。
    # Channel 解析必须 **tenant-scoped**（经请求解析出的 TenantScope 绑定的 UoW 工厂），
    # 跨租户绝不解析到他租户 Channel。WeCom 凭据校验是 `WeComAdapter.verify_connection`
    # 一个 **staticmethod**（只读 Channel.config、不触库）——直接经类引用注入，
    # **不新增 WeComAdapter 构造点**（Step 12.6：双端口共用唯一构造点）。

    def _verify_provider(
        scope: TenantScope = Depends(tenant_scope_provider),
    ):
        uow_factory = _tenant_scoped_factory(scope)

        def _load_channel(channel_id: int):
            with uow_factory() as uow:
                return uow.channels.get_by_id(channel_id)

        # M7-AUI-10.3：Agent credential fallback 的 tenant-scoped loader——
        # 与 _load_channel 同一纪律（同请求 TenantScope 绑定的 UoW 工厂），
        # 跨租户绝不可见（None → fail closed 回退探针）。
        def _load_agent(agent_id: int):
            with uow_factory() as uow:
                return uow.agents.get_by_id(agent_id)

        # M7-AUI-10.5.2：兄弟连接凭据发现的 tenant-scoped loader——同一纪律；
        # 只返回本租户连接（跨租户结构性不可见），按 target Agent 过滤在
        # verifier 内完成（绝不借用其它 Agent 的凭据）。
        def _load_sibling_connections(agent_id: int):
            with uow_factory() as uow:
                return uow.connections.list_connections()

        return build_connection_verifier(
            probe=acp_adapter.probe,
            workbuddy_verify=workbuddy_provisioner.verify,
            wecom_verify=WeComAdapter.verify_connection,
            channel_loader=_load_channel,
            agent_loader=_load_agent,
            sibling_loader=_load_sibling_connections,
        )

    def _connections_service(
        scope: TenantScope = Depends(tenant_scope_provider),
    ) -> ConnectionApplicationService:
        return ConnectionApplicationService(uow_factory=_tenant_scoped_factory(scope))

    app.include_router(build_connections_api(
        service_provider=_connections_service,
        probe=acp_adapter.probe,
        verify_provider=_verify_provider))

    # Implementation Package 2 / Step 3：Grant Management（B4.3 独立资源 +
    # B4.5 known-conversations 投影；同一 tenant-scoped 注入纪律）。
    def _grants_service(
        scope: TenantScope = Depends(tenant_scope_provider),
    ) -> ConnectionConversationApplicationService:
        return ConnectionConversationApplicationService(
            uow_factory=_tenant_scoped_factory(scope))

    app.include_router(build_connection_conversations_api(
        service_provider=_grants_service))

    # M6.6-C3 / ADR-12：Bootstrap Arm 端点。
    #
    # **刻意不复用 `tenant_scope_provider`**：它在「无 Cookie + AUTH_REQUIRED=False」
    # 时回退 TENANT_LOCAL（一个**不可归属**的主体）。Arm 的 initiator 是合法性的
    # 组成部分（O-4）⇒ 必须由**已认证会话**给出，否则 Arm 完全不可用（401）。
    # tenant 与 channel 均由服务端解析：tenant 取自会话、channel 取自 Connection。
    authenticated_user_provider = build_authenticated_user_provider(auth_service)

    def _bootstrap_arms_service(
        user: AuthenticatedUser = Depends(authenticated_user_provider),
    ) -> BootstrapArmApplicationService:
        return BootstrapArmApplicationService(
            uow_factory=_tenant_scoped_factory(TenantScope(tenant_id=user.tenant_id)),
            initiator_user_id=user.user_id,
        )

    app.include_router(build_bootstrap_arms_api(
        service_provider=_bootstrap_arms_service))

    def _users_service(
        scope: TenantScope = Depends(tenant_scope_provider),
    ) -> UserApplicationService:
        return UserApplicationService(uow_factory=_tenant_scoped_factory(scope))

    app.include_router(build_users_api(service_provider=_users_service))

    def _identities_service(
        scope: TenantScope = Depends(tenant_scope_provider),
    ) -> IdentityApplicationService:
        return IdentityApplicationService(uow_factory=_tenant_scoped_factory(scope))

    app.include_router(build_identities_api(service_provider=_identities_service))

    def _observability_service(
        scope: TenantScope = Depends(tenant_scope_provider),
    ) -> InteractionObservabilityService:
        return InteractionObservabilityService(
            uow_factory=_tenant_scoped_factory(scope))

    app.include_router(build_interactions_api(service_provider=_observability_service))

    # ==================================================== Cloud-0.1.3.3 fan-out
    # per-tenant Runtime 组合根（0.1.3.2 Gate 中心命题的落地；B §19.3 契约）：
    #
    #   bootstrap 读 tenants（Gate Q1：tenant 枚举归组合根；空表回退 OSS 缺省）
    #     → _build_tenant_runtime(tenant_id)（同一单租户组合忠实实例化）
    #     → TenantFanoutDriver（每租户各一套 Driver，start/stop 对称）
    #
    # OSS（N=1）：fan-out 退化为恰好一套（TENANT_LOCAL）——行为 == 隔离前；
    # Cloud（N>1）：lifespan 启动时为每个已知租户各建一套；运行期新租户先由
    # Trigger 1（下方 _runtime_for 惰性构建）覆盖，durable Driver 重启后补全。
    # 双触发声明（12.9.4.18A）不变：
    #   Trigger 1 — webhook BackgroundTasks（low-latency，经 _worker_for
    #               落到已认证租户的 Runtime）；
    #   Trigger 2 — TenantFanoutDriver（durable/recovery，各租户独立线程，
    #               DISCOVER/RECOVER 都在该租户的 tenant-scoped UoW 内）。
    # 重叠安全性由 Core 提供——两路触发的是同一租户的同一个 Worker.process，
    # 执行权由 claim_processing（CAS）唯一裁决，Attempt Fence 防 stale outcome。
    bootstrap_uow_factory = functools.partial(
        SqlAlchemyUnitOfWork, tenant_id=None)

    # per-tenant Runtime 注册表：Trigger 1 / Trigger 2 共用同一份组合实例。
    tenant_runtimes: dict[int, TenantComposition] = {}

    def _runtime_for(tenant_id: int) -> TenantComposition:
        """按租户取（或惰性构建）其忠实单租户组合（运行期新租户由 Trigger 1 触发）。"""
        runtime = tenant_runtimes.get(tenant_id)
        if runtime is None:
            runtime = _build_tenant_runtime(
                tenant_id, settings=settings, acp_adapter=acp_adapter,
                bot_registry=bot_registry)
            tenant_runtimes[tenant_id] = runtime
        return runtime

    def discover_tenant_scopes() -> list[TenantScope]:
        """活跃租户集合（bootstrap 只读投影；空回退 OSS 缺省租户——Gate Q7）。"""
        with bootstrap_uow_factory() as uow:
            ids = uow.tenants.list_active_ids()
        return [TenantScope(i) for i in (ids or [TENANT_LOCAL])]

    tenant_fanout = TenantFanoutDriver(
        tenant_source=discover_tenant_scopes,
        driver_factory=lambda scope: _runtime_for(scope.tenant_id).driver,
    )
    # P2：投递恢复的 per-tenant fan-out（**复用 TenantFanoutDriver 的生命周期
    # 机制**，独立的 ticker 与职责）。**默认不启用**（RECOVERY_ENABLED=False）——
    # 启用需先完成 runbook：迁移 → 部署 → 排空旧 Worker → 回填 → 核验。
    recovery_fanout: TenantFanoutDriver | None = None
    if settings.RECOVERY_ENABLED:
        recovery_fanout = TenantFanoutDriver(
            tenant_source=discover_tenant_scopes,
            driver_factory=lambda scope: _runtime_for(
                scope.tenant_id).recovery_driver,
        )

    def _worker_for(scope: TenantScope) -> Worker:
        """已认证 TenantScope → 该租户的 Worker（scope 取自 0.1.3.1 验签后解析）。"""
        return _runtime_for(scope.tenant_id).worker

    # OSS 缺省租户（Tenant #1）的 Runtime 预构建：legacy intake / 入站 Adapter
    # 依赖（行为 == 隔离前）；fan-out start() 复用同一实例，不重复构建。
    oss_runtime = _runtime_for(TENANT_LOCAL)

    # Cloud-0.1.3.1 — WeCom Webhook Tenant Resolution（入站边界租户解析）：
    #
    #   URL channel_id（仅定位）
    #     → _load_channel（bootstrap 定位读：**无 tenant 过滤**，不构成信任）
    #     → WeCom 验签（信任闸门）
    #     → 已认证 Channel.tenant_id（绝不取自 URL）
    #     → TenantScope → tenant-scoped UoW（幂等回查 + PENDING 落库）
    #     → Trigger 1 经 _worker_for 落到同一租户的 Runtime（0.1.3.3）
    def tenant_scoped_uow_factory(tenant_id: int):
        return functools.partial(SqlAlchemyUnitOfWork, tenant_id=tenant_id)

    inbound_service = ChannelInboundService(
        uow_factory=bootstrap_uow_factory,
        intake=oss_runtime.phases,
        inbound_adapter=oss_runtime.channel_adapter,
        tenant_scoped_factory=tenant_scoped_uow_factory,
    )
    app.include_router(build_channel_inbound_api(
        inbound_service, oss_runtime.worker, worker_for=_worker_for))

    # ==================================================== 3B-2.2 WeCom Bot WS
    # 长连接 supervisor 的装配。核心纪律：**入站事实进入 Core 的路径与 HTTP
    # Trigger 1 完全同构**——不是「另开一条入站通道」：
    #
    #   WS 帧（aibot_msg_callback）
    #     → inbound_service.accept_message(channel_id, body=raw)
    #         ← 与 HTTP 入站同一入口：Adapter 归一化 → 已认证 Channel 解析租户
    #           → tenant-scoped 状态感知去重 → PENDING + COMMIT（ACK 边界）
    #     → _worker_for(acceptance.tenant_scope).process(interaction_id)
    #         ← Trigger 1（执行权仍由 Core 的 CAS claim 唯一裁决）
    #
    # 本 transport 不执行 Runtime、不写库、不去重、不解释状态——那些全部归既有边界。
    def discover_bot_connections() -> list[BotConnectionSpec]:
        """bootstrap 只读投影：进程级 bot 连接清单（物理 socket ⇒ 不按租户分组）。

        与 `discover_tenant_scopes`（Gate Q1）同性质：组合根负责枚举，tenant
        不进 Runtime 域。单个渠道 config 不完整 → 跳过该渠道并记日志（单点配置
        错误绝不阻止其它 bot 连接、也绝不回退缺省凭据）。
        """
        with bootstrap_uow_factory() as uow:
            channels = uow.channels.list_channels()
        specs: list[BotConnectionSpec] = []
        for channel in channels:
            if channel.type != WECOM_BOT_CHANNEL_TYPE or channel.status != "active":
                continue
            try:
                specs.append(connection_spec_from_channel(channel))
            except InboundChannelConfigError as exc:
                LOGGER.warning(
                    "wecom_bot channel skipped (config incomplete): %s", exc)
        return specs

    def _on_bot_frame(binding: BotConnectionSpec, raw: str) -> None:
        """WS 消息帧 → 既有入站边界（Trigger 1）。异常向上抛给 transport 记日志。"""
        acceptance = inbound_service.accept_message(
            binding.channel_id,
            signature="", timestamp="", nonce="", body=raw)
        if acceptance.interaction_id is not None:
            _worker_for(acceptance.tenant_scope).process(acceptance.interaction_id)

    bot_transport = WeComBotTransport(
        registry=bot_registry,
        binding_source=discover_bot_connections,
        on_frame=_on_bot_frame,
    )

    # Trigger 1/2 的句柄：供诊断 / E2E 确定性驱动（生产由 lifespan start/stop）。
    app.state.tenant_fanout = tenant_fanout
    app.state.tenant_runtime_for = _runtime_for
    app.state.bot_registry = bot_registry
    app.state.bot_transport = bot_transport

    # 11.5：静态托管 Agent Management UI（挂在最后——/api、/docs、/openapi.json、
    # /health 均为先前注册的具名路由，优先匹配，不受 catch-all mount 影响）。
    # A1（OSS vs Cloud 产品边界，docs/strategy/oss-vs-cloud-boundary.md）：初始
    # OSS 导出**不含 frontend/**——目录缺失时跳过 mount，后端必须照常启动
    # （API/health 完全可用；绝不伪造占位 UI 掩盖缺失，B1）。Cloud 部署中
    # frontend/ 恒存在（Dockerfile COPY），本分支不改变任何现有行为。
    if FRONTEND_DIR.is_dir():
        app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")

    return app


app = create_app()
