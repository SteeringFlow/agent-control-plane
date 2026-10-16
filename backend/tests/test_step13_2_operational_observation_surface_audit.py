"""Step 13.2 — Operational Observation Surface Audit（生产代码改动：0 行）。

审计拍板 = docs/step-13.2-operational-observation-surface-audit.md。本文件只做
**形状级守卫**：锁死 13.2 的三个设计不变量（读路径现状，不新增任何功能）。

不变量（对应 13.2 §3）：

1. 只读面：Observability router 只暴露 GET，无 POST/PUT/PATCH/DELETE；
2. 双状态不合成：Summary/Detail 响应同时含 processing_status 与 delivery_status，
   且不含任何名为 status / overall_status / result / state 的合成字段；
3. 导航不嵌入管理实体：详情响应携带 channel_id / session_id 引用，但不嵌入
   Channel / Agent / Session 响应模型（Observation 与 Management 写域解耦）。

> 注意：本审计同时复核 13.1 已落地的事实面，确认 Observation 面没有在 13.2 之后
> 漂移成「控制面」。
"""

from pathlib import Path
from typing import get_args

from fastapi import APIRouter

from app.api.management.interactions import build_interactions_api
from app.application.interaction_observability import InteractionObservabilityService
from app.schemas.interactions import (
    ActivityResponse,
    InteractionDetailResponse,
    InteractionSummaryResponse,
)

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"


# ====================================================================== A. 只读面

def test_observation_router_is_read_only() -> None:
    """13.2 §Q10 / §3-1：Observation 是只读 Operational Surface——router 无任何写形状。"""
    router: APIRouter = build_interactions_api(
        InteractionObservabilityService(uow_factory=lambda: None))
    methods: set[str] = set()
    for route in router.routes:
        for m in getattr(route, "methods", set()):
            methods.add(m)
    # 真实暴露的 HTTP 动词（FastAPI 把 HEAD/OPTIONS 视作派生，不作为业务动词）
    business = {m for m in methods if m not in {"HEAD", "OPTIONS"}}
    assert business == {"GET"}, f"Observation 面出现非 GET 动词：{business}"


def test_observation_module_has_no_write_decorators() -> None:
    """13.2 §Q9 / §Q10：api/management/interactions.py 不出现写端点装饰器。"""
    src = (APP / "api" / "management" / "interactions.py").read_text(encoding="utf-8")
    for write in ("@router.post", "@router.put", "@router.patch",
                  "@router.delete", "HTTPException(4", "HTTPException(5"):
        # 404 是合法（隐藏存在性），5xx 由异常处理器产生，不在此显式构造写；
        # 仅检查写动词装饰器的显式出现。
        if write.startswith("@router"):
            assert write not in src, f"Observation 模块出现写装饰器：{write}"


# ====================================================================== B. 双状态不合成

def test_dual_status_present_and_not_synthesized() -> None:
    """13.2 §Q2 / §3-2：双状态并行透出，无合成字段。

    4.2 冻结：processing_status × delivery_status 绝不合并；
    13.1 契约：失败三元组 + attempt fence 原样透出。
    """
    for model in (InteractionSummaryResponse, InteractionDetailResponse):
        fields = set(model.model_fields.keys())
        assert "processing_status" in fields, f"{model.__name__} 缺 processing_status"
        assert "delivery_status" in fields, f"{model.__name__} 缺 delivery_status"
        synthesized = fields & {
            "status", "overall_status", "result", "state",
            "outcome", "final_status", "combined_status",
        }
        assert not synthesized, \
            f"{model.__name__} 出现合成状态字段（4.2 冻结双状态）：{synthesized}"


def test_failure_triple_is_nullable_fact_not_state() -> None:
    """13.2 §Q4：失败三元组是 nullable 事实，不发明新状态。"""
    fields = set(InteractionSummaryResponse.model_fields.keys())
    for f in ("failure_stage", "failure_code", "retryable"):
        assert f in fields, f"Summary 缺失败字段 {f}"

    def _is_optional(model, name: str) -> bool:
        ann = model.model_fields[name].annotation
        return type(None) in get_args(ann)

    # 失败三元组：failure_stage / failure_code 是 nullable 事实（成功时为 NULL =
    # 没有失败点，不是字段缺失）；retryable 是布尔提示（非 nullable，但其语义是
    # 事实而非动作，13.2 §Q4）。
    assert _is_optional(InteractionSummaryResponse, "failure_stage")
    assert _is_optional(InteractionSummaryResponse, "failure_code")


# ====================================================================== C. 导航不嵌入管理实体

def test_detail_carries_references_not_embedded_management_entities() -> None:
    """13.2 §Q5 / §3-3：详情带 id 引用，但不嵌入 Channel/Agent/Session 模型。

    Observation 与 Management 写域解耦——导航靠 id deep-link（UI 关切），
    不在响应里嵌套管理实体。
    """
    fields = set(InteractionDetailResponse.model_fields.keys())
    # 引用存在
    assert "channel_id" in fields
    assert "session_id" in fields
    # 不嵌入管理实体
    embedded = fields & {
        "channel", "agent", "session", "connection",
        "channel_detail", "agent_detail", "session_detail",
    }
    assert not embedded, f"Detail 嵌入了管理实体（应只带 id 引用）：{embedded}"


def test_activity_is_1_to_1_evidence_projection() -> None:
    """13.2 §Q3：时间线元素 = Evidence 1:1 投影，不改写。"""
    fields = set(ActivityResponse.model_fields.keys())
    # Evidence 原样字段（与 activities 列对应），无「故事化」派生
    for f in ("type", "stage", "status", "message", "metadata",
              "created_at", "interaction_id", "session_id"):
        assert f in fields, f"ActivityResponse 缺字段 {f}"
    synthesized = fields & {"phase", "narrative", "summary", "story"}
    assert not synthesized, f"ActivityResponse 出现故事化派生字段：{synthesized}"
