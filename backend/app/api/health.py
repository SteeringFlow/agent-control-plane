"""进程级存活 + **最小** readiness 探针。

Step 5.1：`GET /health` → `{"status": "ok"}`，纯存活，**不查依赖**。

Step 14.2.3.B：按 14.1 审计结论，给 `/health` 增加**唯一一个** readiness 信号：

    应用进程  →  SELECT 1  →  数据库可达  →  200
                                  不可达    →  503

**只回答一个问题**：应用进程能否访问数据库？

刻意**不**检查（14.1 冻结——否则 /health 会逐步演变成复杂的系统状态机）：

- Agent / Channel / Connection 是否已配置；
- ACP / WeCom / 任何外部服务是否在线；
- Driver 是否有 pending、是否在跑；
- 外部网络是否可达。

实现约束：api 层**不得 import SQLAlchemy**（由 `tests/test_import_boundaries.py`
锁死），因此这里不做 `text("SELECT 1")` 构造，而是经 FastAPI 依赖注入拿到 Session
后取**底层 DBAPI 连接**执行原语——不引入任何 ORM / 语句构造依赖。

DB 不可达时返回 503（HTTP 语义）：Docker healthcheck 因此能真实判定
「进程起来了但数据库不可用」这一 Pilot 阶段最需要的最小信号。
**不引入** metrics / Prometheus / OpenTelemetry / tracing / 新状态字段。
"""

from fastapi import APIRouter, Depends, HTTPException, status

from app.persistence.database import get_db
from app.schemas.health import HealthResponse

router = APIRouter(tags=["health"])

# 最小 readiness 原语：不需要任何表，只需能连上并执行一次。
_READINESS_SQL = "SELECT 1"


def _database_is_reachable(db) -> bool:
    """执行一次 `SELECT 1`；可达 True，任何异常一律 False（不解释异常语义）。

    刻意不区分异常类型——readiness 只回答「能不能访问」，不做错误分类。
    """
    try:
        # api 层不 import SQLAlchemy：取 Session 的底层 DBAPI 连接直接执行原语
        dbapi_connection = db.connection().connection
        dbapi_connection.execute(_READINESS_SQL)
        return True
    except Exception:  # noqa: BLE001  探针：任何失败都只是「不可达」
        return False


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="存活 + 最小 readiness（DB 可达性）",
    description=(
        "进程存活且能访问数据库 → 200 {\"status\": \"ok\"}；"
        "数据库不可达 → 503（Docker healthcheck 据此判 unhealthy）。"
        "只检查数据库可达性，不检查任何外部服务或配置状态。"
    ),
    responses={503: {"description": "数据库不可达"}},
)
def health(db=Depends(get_db)) -> HealthResponse:
    """存活 + 一次 `SELECT 1`；不可达即 503。"""
    if not _database_is_reachable(db):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database unreachable",
        )
    return HealthResponse(status="ok")
