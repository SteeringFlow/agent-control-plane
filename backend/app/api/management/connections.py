"""Connection Management HTTP API（Step 11.11，按 11.10 审计裁决落地）。

端点面（11.11 冻结 + Cloud-0.1.2.2.2.1 健康诊断；无 DELETE——软下线 =
PATCH status=inactive；不做 Health Check **状态机/持久化健康子系统** /
Webhook / Routing / Credential Vault / 换绑）：

    POST   /api/connections                201 / 404 / 409 / 422
    GET    /api/connections                200
    GET    /api/connections/{id}           200 / 404
    PATCH  /api/connections/{id}           200 / 404 / 422
    GET    /api/connections/{id}/health    200 / 404 / 503
    POST   /api/connections/{id}/verify    200 / 404 / 503   （M6.6-E3-B Provider Verify）

〔M6.6-E3-B〕`POST /{id}/verify` 与 `GET /{id}/health` 的关系：

- `GET /{id}/health` **保持严格只读**（Gate 0.1.2.2.2 §2/§7 原样）；
- `POST /{id}/verify` 是 **Provider execution verification**：允许在 Provider
  （WorkBuddy）**尚无缓存 Runtime** 时 lazy provisioning，并把结果缓存写回
  `Connection.config`（通过既有 `ConnectionApplicationService.update_connection`
  整段替换）。非 WorkBuddy 连接由注入的 verifier 退化为只读探针（无写入）。

Cloud-0.1.2.2.2.1：`GET /{id}/health` 是**即时诊断探针**（回答「配置存在 ≠ 当前
能连」的后半句），**不是**持久化 Health 状态系统：
tenant-scoped 读 Connection → 注入的 `ACPAdapter.probe(connection)`
（只读 Connection.config，Fail-Closed）→ `ConnectionHealthResponse`。
**不写库、不新增 health 字段/表、不改 status、不建状态机、不缓存、不重试、
不刷新 token**（Gate 0.1.2.2.2 §2/§7）。

职责边界（与 api/management/agents.py、channels.py 同一注入纪律）：

    FastAPI（本模块：HTTP 事实提取 + 应答序列化 + Application 错误→状态码映射）
        ↓
    ConnectionApplicationService（业务规则唯一裁决点：Channel/Agent 存在性
    预检、绑定去重、输入校验——含 credential_ref 非空）
        ↓
    UoW Port → Persistence Adapter → DB

本模块不做业务判断——错误映射是唯一的 API 层职责：

    DuplicateConnectionError                    → 409 Conflict
    ConnectionNotFoundError                     → 404 Not Found
    ChannelNotFoundError / AgentNotFoundError   → 404 Not Found（绑定目标缺失）
    InvalidConnectionInputError                 → 422 Unprocessable Entity

合同要点（11.10 五焦点 / 11.11）：
- 响应字段面 == 6 个可见字段（id/name/channel_id/agent_id/status/config）；
  credential_ref 写-only（引用不出）；无 channel_name/agent_name——列表只返回
  绑定 ID，名称解析由 UI 经 /api/channels、/api/agents 解耦完成；
- 创建仍 existence-only：Channel/Agent 只校验存在，不做 status 门禁
  （下拉 active-only 是 UI 过滤，11.10 焦点1）；
- config 是 Access Path 参数：不脱敏、无哨兵（刻意不复制 Channel 合同）；
- 绑定不可变：Update 模型无 channel_id/agent_id 且 extra="forbid"——改绑定
  在合同面直接 422（换绑 = 旧 inactive + 新建）。

与 api/runtime（入站 webhook / 9.4 Routing 的 find_active_by_channel 读路径）
互不混合：管理面归本模块，运行时协议归 Runtime。

本模块不 import SQLAlchemy / app.persistence（经构造注入装配好的 Service，
由 main.py 组合根绑定 SqlAlchemyUnitOfWork；测试注入内存库实例）。
"""

from collections.abc import Callable
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from app.application.connections import (
    ConnectionApplicationService,
    CreateConnection,
    UpdateConnection,
)
from app.application.errors import (
    AgentNotFoundError,
    ApplicationError,
    ChannelNotFoundError,
    ConnectionNotFoundError,
    DuplicateConnectionError,
    DuplicateExternalAgentError,
    InvalidConnectionInputError,
)
from app.domain.agent_probe_snapshot import AgentProbeSnapshot
from app.domain.connection import Connection
from app.schemas.connections import (
    ConnectionCreate,
    ConnectionHealthResponse,
    ConnectionListResponse,
    ConnectionResponse,
    ConnectionUpdate,
)


def build_connections_api(
    service: ConnectionApplicationService | None = None,
    *,
    service_provider=None,
    probe: Callable[[Connection], AgentProbeSnapshot] | None = None,
    verify: Callable[[Connection], tuple[AgentProbeSnapshot, dict | None]] | None = None,
    verify_provider=None,
) -> APIRouter:
    """构造 Connection 管理 router（依赖注入：传入装配好的 ConnectionApplicationService）。

    Cloud-0.1.1-C：可选 `service_provider`（FastAPI 依赖）——组合根用它把
    「请求解析出的 TenantScope → tenant-scoped Service」接进来；不传则退回
    注入的 `service`（测试 / OSS 单租户路径，行为不变）。

    Cloud-0.1.2.2.2.1：可选 `probe`——即时诊断探针（组合根注入
    `ACPAdapter.probe`）。它只读 Connection.config、Fail-Closed、无副作用；
    未装配时 `GET /{id}/health` 返回 503（探针不可用），路由始终登记以保证
    合同面稳定。

    M6.6-E3-B：可选 `verify`——Provider execution verification（组合根注入
    `WorkBuddyProvisioner.verify`）。返回 `(snapshot, updated_config | None)`；
    `updated_config` 非空时经既有 Service 写回 `Connection.config`。未装配时
    `POST /{id}/verify` 返回 503（与 health 探针同一「未装配」纪律）。

    M6.6-F3：可选 `verify_provider`——**FastAPI 依赖**（镜像 `service_provider`），
    组合根用它把「请求解析出的 TenantScope → tenant-scoped provider 分派器」接进来
    （WeCom 的凭据住 Channel.config，分派前需按 `Connection.channel_id` 做
    **tenant-scoped** Channel 解析）。提供 `verify_provider` 时优先于 `verify`；
    HTTP 契约 / 响应模型 / 前端契约均不变。未装配任何 verifier → 503。
    """
    router = APIRouter(prefix="/api/connections", tags=["management-connections"])

    if service_provider is None:
        def _provider() -> ConnectionApplicationService:
            assert service is not None, "build_connections_api 需要 service 或 service_provider"
            return service
    else:
        _provider = service_provider
    service_dep = Depends(_provider)

    # M6.6-F3：verify 经依赖解析（优先 verify_provider；否则退回静态 verify）。
    # 两者都是 `Depends(...)`，故 `Callable` 注解不会被当作请求字段解析。
    if verify_provider is not None:
        verify_dep = Depends(verify_provider)
    else:
        def _static_verify_provider():
            return verify
        verify_dep = Depends(_static_verify_provider)

    def _map_application_error(exc: ApplicationError) -> HTTPException:
        """Application 错误 → HTTP 状态码（唯一映射点，11.11 裁决表）。"""
        if isinstance(exc, DuplicateConnectionError):
            return HTTPException(status_code=409, detail=str(exc))
        if isinstance(exc, DuplicateExternalAgentError):
            # M7-AUI-10.5：WorkBuddy 外部身份已被租户内其它 Agent 代表 → 409
            # （Home Flow 身份随 Connection.config 落库的唯一后端强制点）。
            return HTTPException(status_code=409, detail=str(exc))
        if isinstance(exc, (ConnectionNotFoundError, ChannelNotFoundError,
                            AgentNotFoundError)):
            return HTTPException(status_code=404, detail=str(exc))
        if isinstance(exc, InvalidConnectionInputError):
            return HTTPException(status_code=422, detail=str(exc))
        return HTTPException(status_code=500, detail="internal management error")

    # ------------------------------------------------------------ POST /api/connections

    @router.post("", status_code=201, response_model=ConnectionResponse)
    def create_connection(
        payload: ConnectionCreate, service: ConnectionApplicationService = service_dep
    ) -> ConnectionResponse:
        """创建 Connection（绑定 Channel↔Agent）。

        existence-only：Channel/Agent 不存在 → 404；绑定已存在 → 409；
        响应不含 credential_ref（引用不出）。
        """
        try:
            connection = service.create_connection(
                CreateConnection(**payload.model_dump()))
        except ApplicationError as exc:
            raise _map_application_error(exc) from exc
        return ConnectionResponse.model_validate(connection)

    # ------------------------------------------------------------ GET /api/connections

    @router.get("", response_model=ConnectionListResponse)
    def list_connections(
        service: ConnectionApplicationService = service_dep,
    ) -> ConnectionListResponse:
        """列出全部 Connection（含 inactive；MVP 无分页/过滤）。

        只返回绑定 ID（channel_id/agent_id）——名称解析归 UI（解耦）。
        """
        connections = service.list_connections()
        items = [ConnectionResponse.model_validate(c) for c in connections]
        return ConnectionListResponse(items=items, total=len(items))

    # ------------------------------------------------------------ GET /api/connections/{id}

    @router.get("/{connection_id}", response_model=ConnectionResponse)
    def get_connection(
        connection_id: int, service: ConnectionApplicationService = service_dep
    ) -> ConnectionResponse:
        """读取单个 Connection；不存在 → 404。响应不含 credential_ref。"""
        connection = service.get_connection(connection_id)
        if connection is None:
            raise HTTPException(
                status_code=404, detail=f"Connection {connection_id} 不存在")
        return ConnectionResponse.model_validate(connection)

    # ------------------------------------------------------------ PATCH /api/connections/{id}

    @router.patch("/{connection_id}", response_model=ConnectionResponse)
    def patch_connection(
        connection_id: int, payload: ConnectionUpdate,
        service: ConnectionApplicationService = service_dep,
    ) -> ConnectionResponse:
        """局部更新 Connection 可变字段（缺省/null = 不修改）。

        可变字段仅 name / status / config / credential_ref；
        channel_id / agent_id 不可变（schema extra=forbid → 改绑定 = 422）；
        status 白名单 active/inactive（schema 级，error 在合同面无法进入）；
        config = 整段替换（11.11 冻结，刻意不同于 Channel 的 merge）。
        """
        try:
            connection = service.update_connection(
                connection_id, UpdateConnection(**payload.model_dump()))
        except ApplicationError as exc:
            raise _map_application_error(exc) from exc
        return ConnectionResponse.model_validate(connection)

    # ------------------------------------------------------------ GET /api/connections/{id}/health

    @router.get("/{connection_id}/health", response_model=ConnectionHealthResponse)
    def get_connection_health(
        connection_id: int, service: ConnectionApplicationService = service_dep
    ) -> ConnectionHealthResponse:
        """即时诊断「此刻能否连上 Agent」（Cloud-0.1.2.2.2.1）。

        tenant-scoped 读 Connection（不存在 → 404）；再调用注入的探针
        （`ACPAdapter.probe`：只读 Connection.config，Fail-Closed），返回一次性快照。

        行为冻结（Gate 0.1.2.2.2）：**不写库**、不新增 health 字段/表、不改
        status、无状态机、不缓存、不重试、不刷新 token、不修改
        Agent/Runtime/Interaction/Driver；`checked_at` 请求时刻生成，绝不落库。
        未装配探针 → 503（探针不可用，非连接结论）。
        """
        if probe is None:
            raise HTTPException(
                status_code=503, detail="connection health probe 未装配")
        connection = service.get_connection(connection_id)  # 只读；tenant-scoped
        if connection is None:
            raise HTTPException(
                status_code=404, detail=f"Connection {connection_id} 不存在")
        result = probe(connection)  # ACPAdapter.probe：只读 config，无副作用
        return ConnectionHealthResponse(
            connection_id=connection_id,
            reachable=bool(getattr(result, "reachable", False)),
            checked_at=datetime.now(timezone.utc),
            detail=getattr(result, "detail", None),
            probe=getattr(result, "probe", None),
        )

    # ------------------------------------------------------------ POST /api/connections/{id}/verify

    @router.post("/{connection_id}/verify", response_model=ConnectionHealthResponse)
    def verify_connection(
        connection_id: int,
        service: ConnectionApplicationService = service_dep,
        verifier: Callable[[Connection], tuple[AgentProbeSnapshot, dict | None]] | None = verify_dep,
    ) -> ConnectionHealthResponse:
        """Provider execution verification（M6.6-E3-B；M6.6-F3 provider 分派）。

        注入的 verifier 由组合根决定「谁来验」（M6.6-F3 薄分派，见
        `app/adapters/connection_verify.py`）：

        - `Connection.config["provider"] == "workbuddy"` → `WorkBuddyProvisioner.verify`
          （可在无缓存 Runtime 时 **lazy provisioning**，返回 `updated_config`）；
        - `Channel.type == "wecom"` → `WeComAdapter.verify_connection`
          （只读 gettoken 凭据校验，`updated_config=None` → 无写入）；
        - 其它 → `ACPAdapter.probe`（既有只读探针，行为不变）。

        `updated_config` 非空时经既有 `service.update_connection`（config 整段替换）
        缓存回 `Connection.config`，供既有 ACP 通信路径复用。

        - tenant-scoped 读 Connection（不存在 → 404）；
        - 未装配 verifier → 503（与 health 探针同一纪律）；
        - 不新增 health 字段 / 表 / 状态机；Connection.status 不变；
        - HTTP 契约不变：响应仍是 `ConnectionHealthResponse` 五字段。
        """
        if verifier is None:
            raise HTTPException(
                status_code=503, detail="connection verify 未装配")
        connection = service.get_connection(connection_id)  # 只读；tenant-scoped
        if connection is None:
            raise HTTPException(
                status_code=404, detail=f"Connection {connection_id} 不存在")
        snapshot, updated_config = verifier(connection)
        if updated_config is not None:
            # Provider 缓存的唯一写回点：复用既有 update（config 整段替换）。
            service.update_connection(
                connection_id, UpdateConnection(config=updated_config))
        return ConnectionHealthResponse(
            connection_id=connection_id,
            reachable=bool(getattr(snapshot, "reachable", False)),
            checked_at=datetime.now(timezone.utc),
            detail=getattr(snapshot, "detail", None),
            probe=getattr(snapshot, "probe", None),
        )

    return router
