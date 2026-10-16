"""Agent Management HTTP API（Step 11.3，按 11.2 冻结契约落地）。

端点面（11.2 §0 冻结；无 DELETE——软下线 = PATCH status=inactive）：

    POST   /api/agents         201 / 409 / 422
    GET    /api/agents         200
    GET    /api/agents/{id}    200 / 404
    PATCH  /api/agents/{id}    200 / 404 / 409 / 422

职责边界（与 api/runtime/channels.py 同一注入纪律）：

    FastAPI（本模块：HTTP 事实提取 + 应答序列化 + Application 错误→状态码映射）
        ↓
    AgentApplicationService（业务规则唯一裁决点：type/status 白名单、
    endpoint URL、credential_ref 非空、name 去重守卫）
        ↓
    UoW Port → Persistence Adapter → DB

本模块**不做**业务判断（用户 11.3 指令）——错误映射是唯一的 API 层职责：

    DuplicateAgentError     → 409 Conflict
    AgentNotFoundError      → 404 Not Found
    InvalidAgentInputError  → 422 Unprocessable Entity

本模块不 import SQLAlchemy / app.persistence（经构造注入装配好的 Service，
由 main.py 组合根绑定 SqlAlchemyUnitOfWork；测试注入内存库实例）。
"""

from fastapi import APIRouter, Depends, HTTPException

from app.application.agents import AgentApplicationService, CreateAgent, UpdateAgent
from app.application.errors import (
    AgentNotFoundError,
    ApplicationError,
    DuplicateAgentError,
    DuplicateExternalAgentError,
    InvalidAgentInputError,
)
from app.schemas.agents import (
    AgentCreate,
    AgentListResponse,
    AgentResponse,
    AgentUpdate,
)


def build_agents_api(
    service: AgentApplicationService | None = None, *, service_provider=None
) -> APIRouter:
    """构造 Agent 管理 router（依赖注入：传入装配好的 AgentApplicationService）。

    Cloud-0.1.1-C：可选 `service_provider`（FastAPI 依赖）——组合根用它把
    「请求解析出的 TenantScope → tenant-scoped Service」接进来；不传则退回
    注入的 `service`（测试 / OSS 单租户路径，行为不变）。
    """
    router = APIRouter(prefix="/api/agents", tags=["management-agents"])

    if service_provider is None:
        def _provider() -> AgentApplicationService:
            assert service is not None, "build_agents_api 需要 service 或 service_provider"
            return service
    else:
        _provider = service_provider
    service_dep = Depends(_provider)

    def _map_application_error(exc: ApplicationError) -> HTTPException:
        """Application 错误 → HTTP 状态码（唯一映射点，11.2 §6 裁决表）。"""
        if isinstance(exc, DuplicateExternalAgentError):
            # M7-AUI-10.5：WorkBuddy 外部身份租户内重复 → 409（与同名 409
            # 文案可区分，detail 含「WorkBuddy Agent 已存在」标记 + 冲突 Agent 定位）。
            return HTTPException(status_code=409, detail=str(exc))
        if isinstance(exc, DuplicateAgentError):
            return HTTPException(status_code=409, detail=str(exc))
        if isinstance(exc, AgentNotFoundError):
            return HTTPException(status_code=404, detail=str(exc))
        if isinstance(exc, InvalidAgentInputError):
            return HTTPException(status_code=422, detail=str(exc))
        return HTTPException(status_code=500, detail="internal management error")

    # ------------------------------------------------------------ POST /api/agents

    @router.post("", status_code=201, response_model=AgentResponse)
    def create_agent(
        payload: AgentCreate, service: AgentApplicationService = service_dep
    ) -> AgentResponse:
        """创建 Agent。credential_ref 可入（写-only）；响应不含它。"""
        try:
            agent = service.create_agent(CreateAgent(**payload.model_dump()))
        except ApplicationError as exc:
            raise _map_application_error(exc) from exc
        return AgentResponse.model_validate(agent)

    # ------------------------------------------------------------ GET /api/agents

    @router.get("", response_model=AgentListResponse)
    def list_agents(
        service: AgentApplicationService = service_dep,
    ) -> AgentListResponse:
        """列出全部 Agent（MVP 无分页/过滤）。响应不含 credential_ref。"""
        agents = service.list_agents()
        items = [AgentResponse.model_validate(a) for a in agents]
        return AgentListResponse(items=items, total=len(items))

    # ------------------------------------------------------------ GET /api/agents/{id}

    @router.get("/{agent_id}", response_model=AgentResponse)
    def get_agent(
        agent_id: int, service: AgentApplicationService = service_dep
    ) -> AgentResponse:
        """读取单个 Agent；不存在 → 404。响应不含 credential_ref。"""
        agent = service.get_agent(agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id} 不存在")
        return AgentResponse.model_validate(agent)

    # ------------------------------------------------------------ PATCH /api/agents/{id}

    @router.patch("/{agent_id}", response_model=AgentResponse)
    def patch_agent(
        agent_id: int, payload: AgentUpdate,
        service: AgentApplicationService = service_dep,
    ) -> AgentResponse:
        """局部更新 Agent（缺省/null = 不修改；提供 = 覆写）。

        credential_ref：缺省/null → 不变；非空串 → 覆写；空白串 → 422（服务层）。
        status 只接受管理态 {active, inactive}；error 由 Runtime 写入（服务层拒绝）。
        """
        try:
            agent = service.update_agent(agent_id, UpdateAgent(**payload.model_dump()))
        except ApplicationError as exc:
            raise _map_application_error(exc) from exc
        return AgentResponse.model_validate(agent)

    return router
