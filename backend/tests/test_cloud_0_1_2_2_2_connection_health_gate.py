"""Cloud-0.1.2.2.2 —— Connection Health Gate（机器化护栏）.

设计 + 影响审计，不实现。静态 + 行为护栏，当前即可绿灯，锁死关键裁决：

    ① Connection 三轴分离：
       status（持久化配置态 ∈ {active,inactive}）
       health（派生诊断，**不持久化**）
       interaction outcome（processing/delivery，在 Interaction 不在 Connection）
    ② GET /api/connections/{id}/health = 即时诊断探针，非持久化健康状态系统
       —— 由代码审计证明（非假定）：
          · 进程 /health 已显式冻结「不进化成状态机」(F1)
          · core/config 不引入 Redis/Celery/Kafka，无持久化基座 (F4)
          · driver.py 唯一后台循环是 outbound pickup，非 health poller (F5)
    ③ 探针能力在 Adapter（读 Connection.config），不在 Management Service (F2)
    ④ 不引入新基础设施（redis/celery/kafka/apscheduler）

与 Cloud-0.1.2 护栏同一纪律：只读审计 + 机器锁定；不启动 DB、不做实现。
"""

from __future__ import annotations

from pathlib import Path

from app.adapters.agents.acp import __file__ as _ACP_FILE
from app.domain.connection import Connection
from app.domain.interaction_record import InteractionRecord
from app.persistence.models.connection import ConnectionModel
from app.schemas.connections import CONNECTION_MANAGEMENT_STATUSES

_BACKEND_APP = Path(__file__).resolve().parents[1] / "app"


def _read_app(rel: str) -> str:
    return (_BACKEND_APP / rel).read_text(encoding="utf-8")


def _iter_app_sources():
    for p in _BACKEND_APP.rglob("*.py"):
        try:
            yield p, p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue


# ================================================================== ① 三轴分离

def test_connection_domain_has_no_health_field() -> None:
    """Connection 域无 health* 字段——健康不落域实体。"""
    fields = set(Connection.__dataclass_fields__)
    assert not ({"health", "health_status", "last_health_check",
                 "health_checked_at", "last_health"} & fields)


def test_connection_model_has_no_health_column() -> None:
    """Connection 表无 health* 列——健康不落持久化。"""
    cols = [c.name for c in ConnectionModel.__table__.columns]
    flagged = [c for c in cols if "health" in c]
    assert flagged == [], f"出现健康持久化列：{flagged}"


def test_status_is_only_persisted_connection_state() -> None:
    """status ∈ {active,inactive} 是唯一持久化 Connection 状态；无 health/error 混入。"""
    assert CONNECTION_MANAGEMENT_STATUSES == frozenset({"active", "inactive"})
    assert "health" not in CONNECTION_MANAGEMENT_STATUSES
    assert "error" not in CONNECTION_MANAGEMENT_STATUSES
    assert Connection.__dataclass_fields__["status"].default == "active"


def test_interaction_outcome_stays_on_interaction() -> None:
    """processing/delivery 在 Interaction 上，Connection 不吸收运行态结果。"""
    interaction_fields = set(InteractionRecord.__dataclass_fields__)
    assert "processing_status" in interaction_fields
    assert "delivery_status" in interaction_fields
    conn_fields = set(Connection.__dataclass_fields__)
    assert "processing_status" not in conn_fields
    assert "delivery_status" not in conn_fields


# ================================================================== ② 健康非持久系统（反证）

def test_application_service_does_not_persist_health() -> None:
    """ConnectionApplicationService 不持久化 health（F2：连接性测试归 Runtime/Adapter）。"""
    src = _read_app("application/connections.py")
    forbidden = ("health_status", "last_health_check", "update_connection_health",
                 "record_health", "ConnectionHealth", "health_store", "schedule_health")
    for token in forbidden:
        assert token not in src, f"ConnectionApplicationService 不应持久化 health：{token}"
    # 该 Service 已声明连接性测试不在本层
    assert "Runtime/Adapter" in src


def test_no_health_scheduler_or_state_infra() -> None:
    """全 app 不引入健康存储/调度基座（F4）；driver 唯一后台循环非 health poller（F5）。"""
    driver_src = _read_app("application/driver.py")
    assert "health_status" not in driver_src
    assert "last_health_check" not in driver_src
    assert "ConnectionHealth" not in driver_src

    infra = ("import redis", "from redis", "import celery", "from celery",
             "import kafka", "from kafka", "apscheduler", "BackgroundScheduler")
    for path, src in _iter_app_sources():
        lowered = src.lower()
        for marker in infra:
            assert marker not in lowered, f"发现健康存储/调度基础设施：{path} ({marker})"


def test_connections_api_health_route_is_derived_not_stored() -> None:
    """锁：connections API 的 health 路由**派生不落库**——不写库/不调度/无状态模型。

    0.1.2.2.2.1 已实现该端点，本锁转为 active 验证：允许 health 路由与
    `ConnectionHealthResponse`（响应 schema），但不得出现持久化/调度健康状态的痕迹。
    """
    src = _read_app("api/management/connections.py")
    assert "health" in src.lower()  # 端点已实现：本锁处于 active 状态
    forbidden = ("uow.connections.update", "ConnectionHealthModel", "health_status",
                 "last_health_check", "BackgroundScheduler", "schedule_health", "add_job")
    for token in forbidden:
        assert token not in src, f"connection health 路由不应持久化/调度健康状态：{token}"


# ================================================================== ② 即时快照先例（正证）

def test_process_health_discipline_precedent() -> None:
    """进程 /health 已是即时快照、且显式拒绝「状态机」——connection health 须同纪律。"""
    from app.api import health as proc_health  # noqa: F401  模块可导入（无副作用）
    from app.schemas.health import HealthResponse

    assert set(HealthResponse.model_fields) == {"status"}  # 仅存活快照字段，无存储
    src = _read_app("api/health.py")
    assert "状态机" in src          # 14.1：明确冻结「不进化成系统状态机」
    assert "SELECT 1" in src       # 仅最小 readiness 原语


# ================================================================== ③ 探针依据 Connection.config ④ 不引入基础设施

def test_probe_basis_reads_connection_config_only() -> None:
    """acp 适配器凭据只来自 Connection.config / connection.credential_ref；
    未来 probe(connection) 必须复用同一条路径（0.1.2.2 ①）。"""
    src = Path(_ACP_FILE).read_text(encoding="utf-8")
    assert "connection.config" in src
    assert "connection.credential_ref" in src
    assert "agent.credential_ref" not in src   # Agent 上的引用不被消费
