"""Bootstrap Arm 管理面 HTTP API（M6.6-C3 / ADR-12）。

端点面（V0 只有一个动作 ——「开窗口」；Arm 不是 CRUD 资源）：

    POST /api/connections/{connection_id}/bootstrap-arm    201 / 401 / 404

职责边界（与 api/management/* 同一注入纪律）：

    FastAPI（本模块：HTTP 事实提取 + 应答序列化 + Application 错误→状态码映射）
        ↓
    BootstrapArmApplicationService（业务规则唯一裁决点：存在性、scope 解析、supersede）
        ↓
    UoW Port → Persistence Adapter → DB

**Authorization Source 必须是真实、authenticated、auditable 的管理动作**（ADR-12 §16）:

- 认证由**依赖层**提供（`AuthenticatedUser`）——无有效会话 ⇒ 401，**绝不**回退到
  OSS 缺省租户身份（O-4：不可归属的 initiator 一律禁止）；
- 请求体**不接受** tenant_id / channel_id / initiator_user_id；
- 不可用 hidden flag / DB 直写 / 前端态伪造 Arm。

用户可见文案必须是「验证连接 / Verify Connection」，**不得**出现 Arm / Bootstrap /
授权 / Tenant 等词汇（Cloud-0.1.5 G3）——端点名是内部 API 名，不是产品语言。

本模块不 import SQLAlchemy / app.persistence（服务由 main.py 组合根装配）。
"""

from fastapi import APIRouter, Depends, HTTPException

from app.application.bootstrap_arms import (
    ArmConnection,
    BootstrapArmApplicationService,
)
from app.application.errors import (
    ApplicationError,
    ConnectionNotFoundError,
    InvalidConnectionInputError,
)
from app.schemas.bootstrap_arms import BootstrapArmResponse


def build_bootstrap_arms_api(
    service: BootstrapArmApplicationService | None = None, *,
    service_provider=None,
) -> APIRouter:
    """构造 Bootstrap Arm 管理 router（依赖注入）。

    `service_provider`（FastAPI 依赖）：组合根用它把「已认证操作者 + tenant-scoped
    UoW」接进来；不传则退回注入的 `service`（测试路径）。
    """
    router = APIRouter(prefix="/api/connections", tags=["management-bootstrap-arms"])

    if service_provider is None:
        def _provider() -> BootstrapArmApplicationService:
            assert service is not None, \
                "build_bootstrap_arms_api 需要 service 或 service_provider"
            return service
    else:
        _provider = service_provider
    service_dep = Depends(_provider)

    def _map_application_error(exc: ApplicationError) -> HTTPException:
        """Application 错误 → HTTPException（唯一映射点）。

        刻意**没有** 409 分支：重复 Verify = supersede（201），不是冲突。
        不校验 `Connection.status`（与既有 Grant 创建同形）⇒ 不需要新的 422
        错误类 —— 本端点**不新增任何错误类**（CP3-F §20）。
        """
        if isinstance(exc, ConnectionNotFoundError):
            return HTTPException(status_code=404, detail=str(exc))
        if isinstance(exc, InvalidConnectionInputError):
            return HTTPException(status_code=422, detail=str(exc))
        return HTTPException(status_code=500, detail="internal management error")

    # ------------------------------------------- POST /api/connections/{id}/bootstrap-arm

    @router.post("/{connection_id}/bootstrap-arm",
                 status_code=201, response_model=BootstrapArmResponse)
    def arm_connection(
        connection_id: int,
        service: BootstrapArmApplicationService = service_dep,
    ) -> BootstrapArmResponse:
        """验证连接：开启（或取代）一条受限 Bootstrap 窗口。

        无请求体 —— scope 与 initiator 全部由服务端从「已认证会话 + Connection」解析。
        """
        try:
            arm = service.arm_connection(ArmConnection(connection_id=connection_id))
        except ApplicationError as exc:
            raise _map_application_error(exc) from exc
        assert arm.id is not None and arm.expires_at is not None
        return BootstrapArmResponse(
            arm_id=arm.id,
            connection_id=arm.connection_id,
            status=arm.status,
            expires_at=arm.expires_at,
        )

    return router
