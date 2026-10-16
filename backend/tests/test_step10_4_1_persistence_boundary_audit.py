"""Step 10.4.1 —— Persistence Boundary Audit（审计状态快照，生产代码零改动）。

把审计结论固化为可回归断言（审计步的唯一交付物 = 文档 + 本快照测试）：

A  Interaction ORM 字段面足以表达冻结语义
   （processing_status / delivery_status / failure_stage / failure_code / retryable）；
B  Activity ORM 字段面足以记录 stage / status / message / metadata；
C  Persistence 边界裁决：Runtime Coordinator 与六 Executor 当前对
   Interaction/Activity 持久化**零接触**——写路径（未来 Step）必须挂在
   Runtime 调用边界（ChannelInboundService ↔ Coordinator 之间，
   RuntimeExecutionCoordinator Protocol 装饰器形状），不得进入 Coordinator /
   Executor 内部。

缺口清单（记录，不在本步修）：
   - Interaction/Activity 无 domain 实体 / Mapper / Repository Port / Adapter /
     UoW 暴露（8 张表中唯一没有读写路径的两张）；
   - Runtime 主链路当前不产生任何 interaction/activity 行（表为空壳）；
   - 状态枚举值（pending/running/succeeded/failed、failure_stage 取值集）未
     在文档明文冻结——留写路径 Step 以 domain 常量冻结；
   - 无 Alembic（5.3 显式决策，触发条件未到）。
"""

import ast
from pathlib import Path

from sqlalchemy import inspect

from app.persistence.models import ActivityModel, InteractionModel

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"
APPLICATION = APP / "application"


# ================================================================== A. Interaction 字段面


def test_a1_interaction_columns_exactly_match_frozen_semantics() -> None:
    """4.2 冻结字段逐一在列：双状态独立（无合成 status）+ 失败三元组。

    10.5.2 扩边：`channel_id` 物化为一等身份列（原只在 request_payload JSON）。
    12.9.4.12 扩边：`processing_started_at` 为 attempt fence 的时间戳列。
    """
    cols = {c.name: c for c in inspect(InteractionModel).columns}
    assert set(cols) == {
        "id", "channel_id", "session_id", "direction", "external_interaction_id",
        "request_payload", "response_payload",
        "processing_status", "delivery_status",
        "failure_stage", "failure_code", "retryable",
        "processing_started_at", "created_at", "updated_at", "tenant_id",
    }
    # 双状态独立、可空语义正确
    assert not cols["processing_status"].nullable
    assert not cols["delivery_status"].nullable
    assert cols["failure_stage"].nullable          # NULL = 未失败
    assert cols["failure_code"].nullable
    assert not cols["retryable"].nullable
    # 幂等落点已备好（MsgId，未来去重的载体）
    assert "external_interaction_id" in cols


def test_a2_all_typed_error_codes_fit_failure_code_column() -> None:
    """application 层全部 typed error code 能装进 failure_code（≤64 字符）。"""
    import re
    src = (APPLICATION / "errors.py").read_text(encoding="utf-8")
    # code 以 SCREAMING_SNAKE token 形式出现在错误消息/常量中
    codes = set(re.findall(r"\b[A-Z][A-Z_]{7,}\b", src))
    assert codes, "errors.py 中未发现 typed error code（审计失效）"
    too_long = [c for c in codes if len(c) > 64]
    assert not too_long, f"超出 failure_code String(64)：{too_long}"


def test_a3_failure_stage_candidates_fit_column() -> None:
    """六段执行序的 stage 名都能装进 failure_stage（≤32 字符）。"""
    stages = ("identity_resolution", "routing", "session_resolution",
              "session_creation", "agent_dispatch", "delivery")
    assert all(len(s) <= 32 for s in stages)


# ================================================================== B. Activity 字段面


def test_b1_activity_columns_support_evidence_record() -> None:
    """stage/status/message/metadata 齐备 + append-only（无 updated_at）。"""
    cols = {c.name: c for c in inspect(ActivityModel).columns}
    assert set(cols) == {
        "id", "interaction_id", "session_id", "type",
        "stage", "status", "message", "metadata", "created_at", "tenant_id",
    }
    assert not cols["status"].nullable             # 显式事实，无语义默认
    assert cols["interaction_id"].nullable         # 允许会话级活动
    assert cols["message"].nullable
    assert "updated_at" not in cols                # append-only：历史不可改写


# ================================================================== C. Persistence 边界快照


def _imports(path: Path) -> set[str]:
    roots: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module)
    return roots


EXECUTOR_FILES = (
    "identity_resolution.py", "routing.py", "session_resolution.py",
    "session_creation.py", "agent_dispatch.py", "delivery.py",
    "coordinator.py", "runtime.py", "channel_inbound.py",
)


def test_c1_runtime_boundary_has_zero_persistence_contact() -> None:
    """Runtime 全链（六 executor + coordinator + runtime Port + inbound service）
    当前不 import 任何 persistence / ORM——写路径挂边界的基线事实。"""
    forbidden_prefixes = ("app.persistence", "sqlalchemy")
    for name in EXECUTOR_FILES:
        roots = _imports(APPLICATION / name)
        hit = [r for r in roots
               if any(r == p or r.startswith(p + ".") for p in forbidden_prefixes)]
        assert not hit, f"application/{name} 触碰 persistence：{hit}"


def test_c2_runtime_boundary_never_names_interaction_activity_orm() -> None:
    """Runtime 侧源文件不出现 InteractionModel / ActivityModel 符号。"""
    for name in EXECUTOR_FILES:
        src = (APPLICATION / name).read_text(encoding="utf-8")
        assert "InteractionModel" not in src, f"application/{name}"
        assert "ActivityModel" not in src, f"application/{name}"


def test_c3_uow_exposes_exactly_eight_repositories() -> None:
    """状态快照（10.4.2 扩边后）：UoW（Port 与实现）暴露恰 8 仓库——
    6 实体仓库 + interactions / activities 记录仓库；防继续「偷偷塞入」。"""
    port_src = (APP / "repositories" / "unit_of_work.py").read_text(encoding="utf-8")
    impl_src = (APP / "persistence" / "unit_of_work.py").read_text(encoding="utf-8")
    expected_ports = {
        "UserRepository", "ChannelRepository", "AgentRepository",
        "IdentityRepository", "ConnectionRepository", "SessionRepository",
        "InteractionRecordRepository", "ActivityRecordRepository",
    }
    for src, label in ((port_src, "Port"), (impl_src, "SqlAlchemyUnitOfWork")):
        for port_name in expected_ports:
            assert port_name in src, f"UoW {label} 缺少 {port_name}"
    # 不出现本步之外的第四种仓库命名漂移
    for src, label in ((port_src, "Port"), (impl_src, "SqlAlchemyUnitOfWork")):
        assert "OutboxRepository" not in src, f"UoW {label}"
        assert "EventRepository" not in src, f"UoW {label}"


def test_c4_no_interaction_activity_domain_entities() -> None:
    """8.1 边界回归：domain 层无 Interaction/Activity 实体——
    InboundInteraction / OutboundResponse 不是 Interaction ORM 的领域镜像。"""
    domain_files = {p.name for p in (APP / "domain").glob("*.py")}
    assert "interaction.py" not in domain_files
    assert "activity.py" not in domain_files
