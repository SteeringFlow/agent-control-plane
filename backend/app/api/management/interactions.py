"""Interaction Observability HTTP API（Step 13.1，**纯读 Vertical Slice**）。

端点面（13.1 冻结；无 POST / PATCH / DELETE——Observability 面不存在写用例）：

    GET  /api/interactions                      200（bounded limit，最新在前）
    GET  /api/interactions/{id}                 200 / 404（详情 + Evidence 时间线）
    GET  /api/interactions/{id}/activities      200 / 404（发生序时间线）

职责边界（与 api/management/agents.py 等同一纪律）：

    FastAPI（本模块：HTTP 事实提取 + 应答序列化 + 404 映射）
        ↓
    InteractionObservabilityService（只读用例：List / Get / Timeline）
        ↓
    UoW Port → Persistence Adapter → DB

10.7.1 §Q6 记录的边界张力在本步落定：Evidence 是 Runtime 数据面的产物，
但读它的是管理员（Management 面用户）——证据读端点落在 **Management 面**
（api/management/），且读路径绝不经过 Runtime Coordinator / Worker /
PersistingCoordinator（读不改执行，与 PersistingCoordinator 的写边界对称）。

本模块不做业务判断——错误映射是唯一的 API 层职责（本面只有一种）：

    Interaction / 活动所属 Interaction 不存在            → 404 Not Found

**不重新解释状态**：响应字段 == 数据库事实（原生双状态、失败三元组、
attempt fence 时间戳、Evidence 原文）；无 overall status、无派生结果、
无「成功与否」的单字段合成（4.2 冻结：双状态绝不合并）。

本模块不 import SQLAlchemy / app.persistence（经构造注入装配好的 Service，
由 main.py 组合根绑定 SqlAlchemyUnitOfWork；测试注入内存库实例）。
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from app.application.interaction_observability import (
    DEFAULT_LIST_LIMIT,
    InteractionObservabilityService,
)
from app.schemas.interactions import (
    ActivityListResponse,
    ActivityResponse,
    InteractionDetailResponse,
    InteractionListResponse,
    InteractionSummaryResponse,
)

# bounded limit 的合同上限（防误用大 limit 拖库；不是翻页——13.1 不做游标）
MAX_LIST_LIMIT = 200


def build_interactions_api(
    service: InteractionObservabilityService | None = None, *, service_provider=None
) -> APIRouter:
    """构造 Interaction Observability router（依赖注入：传入装配好的只读 Service）。

    Cloud-0.1.1-C：可选 `service_provider`（FastAPI 依赖）——组合根用它把
    「请求解析出的 TenantScope → tenant-scoped Service」接进来；不传则退回
    注入的 `service`（测试 / OSS 单租户路径，行为不变）。
    """
    router = APIRouter(prefix="/api/interactions", tags=["observability-interactions"])

    if service_provider is None:
        def _provider() -> InteractionObservabilityService:
            assert service is not None, "build_interactions_api 需要 service 或 service_provider"
            return service
    else:
        _provider = service_provider
    service_dep = Depends(_provider)

    # ------------------------------------------------------------ GET /api/interactions

    @router.get("", response_model=InteractionListResponse)
    def list_interactions(
        limit: int = Query(
            default=DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
        service: InteractionObservabilityService = service_dep,
    ) -> InteractionListResponse:
        """最近 N 条 Interaction（最新在前；陈列面，不含 payload 快照）。

        原生双状态 / 失败三元组原样透出；`limit` 是固定上限不是翻页参数
        （越界 → 422，由合同面拒绝，而不是静默截断）。
        """
        records = service.list_interactions(limit)
        items = [InteractionSummaryResponse.model_validate(r) for r in records]
        return InteractionListResponse(items=items, total=len(items), limit=limit)

    # ------------------------------------------------- GET /api/interactions/{id}

    @router.get("/{interaction_id}", response_model=InteractionDetailResponse)
    def get_interaction(
        interaction_id: int,
        service: InteractionObservabilityService = service_dep,
    ) -> InteractionDetailResponse:
        """单条 Interaction 详情：事实字段 + 载荷快照 + Evidence 时间线。

        一次调用回答「这次请求发生了什么」：Who / Which Channel / 双状态 /
        失败三元组 / Agent 回了什么（response_payload）/ 执行故事时间线
        （interaction_received → … → interaction_completed / stage_failure）。
        不存在 → 404（隐藏存在性）。**不合成任何派生状态字段**。
        """
        record = service.get_interaction(interaction_id)
        if record is None:
            raise HTTPException(
                status_code=404, detail=f"Interaction {interaction_id} 不存在")
        activities = service.list_activities(interaction_id) or []
        detail = InteractionDetailResponse.model_validate(record)
        return detail.model_copy(update={
            "activities": [ActivityResponse.model_validate(a) for a in activities],
        })

    # ------------------------- GET /api/interactions/{id}/activities

    @router.get("/{interaction_id}/activities",
                response_model=ActivityListResponse)
    def list_activities(
        interaction_id: int,
        service: InteractionObservabilityService = service_dep,
    ) -> ActivityListResponse:
        """Evidence 时间线（发生序：created_at 升序、id 升序 tiebreak）。

        Evidence 原样返回（append-only，无更新语义）；Interaction 不存在 →
        404；存在但尚无 Evidence → 空列表（received 尚未落库的瞬间窗口，
        是真实事实而非错误）。
        """
        activities = service.list_activities(interaction_id)
        if activities is None:
            raise HTTPException(
                status_code=404, detail=f"Interaction {interaction_id} 不存在")
        items = [ActivityResponse.model_validate(a) for a in activities]
        return ActivityListResponse(items=items, total=len(items))

    return router
