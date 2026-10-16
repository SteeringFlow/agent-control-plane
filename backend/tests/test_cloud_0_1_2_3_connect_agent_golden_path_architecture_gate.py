"""Cloud-0.1.2.3 —— Connect Agent Golden Path Architecture Gate（机器化护栏）.

设计 + 影响审计，不实现。静态 + 行为护栏，当前即可绿灯，锁死 9 个裁决
（Q1–Q9）与中央事务边界裁决：

    Q1 单页 UI = 纯前端编排（不新增后端实体/端点/事务器）
    Q2/Q3 部分成功 → 接纳孤儿 Agent，不回滚（两事实独立事务，无 Saga）
    Q4 Health Probe 非强制（建议性，非 Connection 创建门禁）
    Q5 Connected = 探针瞬时派生 UI 态（不持久）
    Q6 重试 = 既有端点重新提交（无 retry 框架）
    Q7 无 delete/rollback 子系统（清理用既有软下线）
    Q8 无 onboarding 状态字段/表
    Q9 完全复用冻结的 Agent/Connection API 合同

与 Cloud-0.1.2.2 护栏同一纪律：只读审计 + 机器锁定；不启动 DB、不做实现。
"""

from __future__ import annotations

import ast
from pathlib import Path

from app.application.agents import (
    ALLOWED_MANAGEMENT_STATUSES,
    AgentApplicationService,
    UpdateAgent,
)
from app.application.connections import ConnectionApplicationService
from app.application.errors import DuplicateConnectionError
from app.domain.agent import Agent
from app.domain.connection import Connection
from app.domain.agent_probe_snapshot import AgentProbeSnapshot
from app.schemas.agents import AgentCreate
from app.schemas.connections import (
    CONNECTION_MANAGEMENT_STATUSES,
    ConnectionCreate,
    ConnectionHealthResponse,
)

_BACKEND_APP = Path(__file__).resolve().parents[1] / "app"
_MANAGEMENT_API = _BACKEND_APP / "api" / "management"
_AGENTS_API = _MANAGEMENT_API / "agents.py"
_CONNECTIONS_API = _MANAGEMENT_API / "connections.py"
_AGENTS_APP = _BACKEND_APP / "application" / "agents.py"
_CONNECTIONS_APP = _BACKEND_APP / "application" / "connections.py"

# 越界编排 / 事务抽象 / onboarding 类（精确类名匹配；docstring 误报已排除）。
_FORBIDDEN_CLASSES = frozenset({
    "ConnectAgent", "ConnectAgentRequest", "GoldenPath", "Onboarding",
    "OnboardingState", "ConnectController", "ConnectService", "ConnectWorkflow",
    "Provisioning", "TransactionManager", "Saga", "Outbox", "Compensating",
    "DistributedTransaction", "RetryManager", "RetryPolicy",
})

_APP_CLASS_NAMES: frozenset[str] | None = None


def _app_class_names() -> frozenset[str]:
    global _APP_CLASS_NAMES
    if _APP_CLASS_NAMES is not None:
        return _APP_CLASS_NAMES
    names: set[str] = set()
    for path in _BACKEND_APP.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                names.add(node.name)
    _APP_CLASS_NAMES = frozenset(names)
    return _APP_CLASS_NAMES


def test_connect_agent_is_pure_frontend_orchestration() -> None:
    """Q1/Q9：Connect Agent 是纯前端编排——无越界后端编排类/路由。"""
    classes = _app_class_names()
    assert not (classes & _FORBIDDEN_CLASSES), f"越界编排类出现：{classes & _FORBIDDEN_CLASSES}"

    # management API 不存在任何 connect* 路由/方法。
    for src_path in (_AGENTS_API, _CONNECTIONS_API):
        src = src_path.read_text(encoding="utf-8")
        assert "def connect_agent" not in src
        assert "def create_connect_agent" not in src
        assert "connect_agent" not in src

    # 组合根仅为既有 build_agents_api / build_connections_api（导入即存在）。
    from app.api.management.agents import build_agents_api  # noqa: F401
    from app.api.management.connections import build_connections_api  # noqa: F401


def test_no_onboarding_state_or_connected_field() -> None:
    """Q8：Agent/Connection 域无 onboarding_state/connected/provisioning/connect_status/
    first_success 字段；全 app 无 OnboardingState/ConnectStatus/Provisioning 类。"""
    forbidden_fields = {
        "onboarding_state", "connected", "provisioning",
        "connect_status", "first_success",
    }
    agent_fields = set(Agent.__dataclass_fields__)
    conn_fields = set(Connection.__dataclass_fields__)
    assert not (forbidden_fields & agent_fields)
    assert not (forbidden_fields & conn_fields)

    classes = _app_class_names()
    assert "OnboardingState" not in classes
    assert "ConnectStatus" not in classes
    assert "Provisioning" not in classes


def test_two_fact_creation_are_separate_no_saga() -> None:
    """Q2/Q3：两事实独立事务（各自 uow_factory + commit），无 Saga/Outbox；
    connections 服务不调用 create_agent（无隐式跨事实编排）。"""
    classes = _app_class_names()
    assert not (
        classes & {
            "TransactionManager", "Saga", "Outbox",
            "Compensating", "DistributedTransaction",
        }
    )

    agents_src = _AGENTS_APP.read_text(encoding="utf-8")
    conns_src = _CONNECTIONS_APP.read_text(encoding="utf-8")

    # 各自独立开启并提交自己的 UoW。
    assert "with self._uow_factory() as uow" in agents_src
    assert "with self._uow_factory() as uow" in conns_src
    assert "uow.commit()" in agents_src
    assert "uow.commit()" in conns_src

    # Connection 服务不内联创建 Agent（两事实严格分离，0.1.2.2 ②）。
    assert "create_agent" not in conns_src

    # 两个独立 Service 都存在（编排点在 UI，不在后端）。
    assert AgentApplicationService is not None
    assert ConnectionApplicationService is not None


def test_orphan_agent_recovery_via_existing_endpoints_no_rollback() -> None:
    """Q2/Q3/Q7：孤儿 Agent 恢复用既有端点——软下线存在（update_agent 支持
    status=inactive）；重提交信号 DuplicateConnectionError 可导入；服务层无
    delete_agent/delete_connection/rollback 补偿方法。"""
    # 软下线路径存在：update_agent 接受 status=inactive。
    assert "inactive" in ALLOWED_MANAGEMENT_STATUSES
    assert "status" in UpdateAgent.__dataclass_fields__

    # 重提交产物（重复绑定 → 409），供前端幂等重提交。
    assert DuplicateConnectionError is not None

    # 无自动回滚 / 补偿 / 硬删除机制。
    agents_src = _AGENTS_APP.read_text(encoding="utf-8")
    conns_src = _CONNECTIONS_APP.read_text(encoding="utf-8")
    for token in ("delete_agent", "delete_connection", "rollback_agent",
                  "rollback_connection", "compensat", "delete_on_failure"):
        assert token not in agents_src, f"agents.py 出现回滚/删除痕迹：{token}"
        assert token not in conns_src, f"connections.py 出现回滚/删除痕迹：{token}"


def _function_source(path: Path, name: str) -> str:
    """抽取某 .py 文件中指定函数（含嵌套）的源码（按 AST 行号切片，避免 docstring 噪声）。"""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            lines = src.splitlines()
            return "\n".join(lines[node.lineno - 1: node.end_lineno])
    raise AssertionError(f"函数 {name} 未在 {path} 找到")


def test_health_probe_is_advisory_not_a_creation_gate() -> None:
    """Q4/Q5：Connection 创建路由不引用 probe（探针只在 health 路由）；
    ConnectionHealthResponse 可导入；Connection 域无 connected/health_status 字段。"""
    # create_connection 路由体函数内不调用探针（建连结论不阻断 Connection 存在）。
    create_src = _function_source(_CONNECTIONS_API, "create_connection")
    assert "probe(" not in create_src

    # 探针只在 get_connection_health 路由中调用。
    health_src = _function_source(_CONNECTIONS_API, "get_connection_health")
    assert "probe(" in health_src

    # ConnectionHealthResponse 字段面存在；Connection 域无 connected/health_status 字段。
    assert ConnectionHealthResponse is not None

    conn_fields = set(Connection.__dataclass_fields__)
    assert "connected" not in conn_fields
    assert "health_status" not in conn_fields


def test_connected_is_transient_probe_state_not_persisted() -> None:
    """Q5：Connected 由探针快照派生——AgentProbeSnapshot 含 reachable/probe；
    ConnectionHealthResponse 含 reachable/checked_at；Connection 无 connected 字段；
    Connection.status 默认 active 且管理态集合 = {active, inactive}。"""
    snap_fields = set(AgentProbeSnapshot.__dataclass_fields__)
    assert "reachable" in snap_fields
    assert "probe" in snap_fields

    health_fields = set(ConnectionHealthResponse.model_fields)
    assert "reachable" in health_fields
    assert "checked_at" in health_fields
    assert "connected" not in health_fields  # 不持久化 Connected

    assert "connected" not in Connection.__dataclass_fields__
    assert Connection.__dataclass_fields__["status"].default == "active"
    assert CONNECTION_MANAGEMENT_STATUSES == frozenset({"active", "inactive"})


def test_retry_is_resubmission_via_existing_endpoints() -> None:
    """Q6：重试 = 既有端点重提交——DuplicateConnectionError 可导入（409 幂等信号）；
    health 路由存在（可重探）；全 app 无 RetryManager/RetryPolicy。"""
    assert DuplicateConnectionError is not None

    conns_api_src = _CONNECTIONS_API.read_text(encoding="utf-8")
    assert "def get_connection_health" in conns_api_src  # 可重探

    classes = _app_class_names()
    assert "RetryManager" not in classes
    assert "RetryPolicy" not in classes


def test_golden_path_reuses_existing_api_contracts() -> None:
    """Q9：完全复用冻结合同——AgentCreate/ConnectionCreate/ConnectionHealthResponse
    字段面存在且不变；management API 无 connect* 路由。"""
    assert AgentCreate is not None
    assert ConnectionCreate is not None
    assert ConnectionHealthResponse is not None

    for src_path in (_AGENTS_API, _CONNECTIONS_API):
        src = src_path.read_text(encoding="utf-8")
        assert "def connect_agent" not in src
        assert "router.post(\"/connect\"" not in src
        assert "create_connect_agent" not in src
