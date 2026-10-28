"""ACP V0.2 Artifact Contract Gate —— 机器化锁定回归（设计/测试-only）。

本测试对 **当前（未实现 artifact 生产代码）** 的仓库即可运行，全部为护栏：
锁定「V0.2 不得意外改动」的既有契约边界（DoD #6/#9/#10/#11/#13 与 SDK 镜像对等 #3）。

当生产代码按 docs/step-acp-v0.2-artifact-contract-gate.md 落地后，第 6 项前瞻断言
会自动从 skip 转为硬锁，无需改本文件。

运行：backend/ 下 `pytest tests/test_acp_v0_2_contract_gate.py -q`
"""

import importlib.util
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))


def _module(name: str):
    spec = importlib.util.find_spec(name)
    if spec is None:
        return None
    return importlib.util.module_from_spec(spec)


# ---- domain ----
from app.domain.agent_request import AgentRequest
from app.domain.runtime_execution_result import RuntimeExecutionResult
from app.persistence.models.interaction import InteractionModel

# ---- sdk mirror ----
from agent_runtime_sdk.models import AgentResponse as SdkAgentResponse
from agent_runtime_sdk.minimal import encode_response


AGENT_REQUEST_FIELDS = {
    "agent_id", "control_plane_session_id", "content",
    "external_session_id", "metadata",
}
RUNTIME_RESULT_FIELDS = {"session_mode", "session_id", "agent_response"}
INTERACTION_COLUMNS = {
    "id", "channel_id", "session_id", "direction", "external_interaction_id",
    "request_payload", "response_payload", "processing_status", "delivery_status",
    "failure_stage", "failure_code", "retryable", "processing_started_at",
    # P2 Delivery Lifecycle：投递 attempt 计数 / 租约起点 / 协议世代
    "delivery_attempts", "delivery_started_at", "delivery_protocol",
    "created_at", "updated_at", "tenant_id",  # Cloud-0.1.1-B 加性列
}


def test_agent_request_field_face_unchanged():
    """DoD #9: AgentRequest 字段面 == 5 字段基线，且不含 artifacts。"""
    assert set(AgentRequest.__dataclass_fields__) == AGENT_REQUEST_FIELDS
    assert "artifacts" not in AgentRequest.__dataclass_fields__


def test_runtime_execution_result_field_face_locked():
    """DoD #10: RuntimeExecutionResult 恰好三字段，且不含 artifacts。"""
    assert set(RuntimeExecutionResult.__dataclass_fields__) == RUNTIME_RESULT_FIELDS
    assert "artifacts" not in RuntimeExecutionResult.__dataclass_fields__


def test_interaction_model_columns_unchanged():
    """DoD #11: interactions 表列集合不变，无 artifacts 列（无 schema/migration 改动）。"""
    cols = set(InteractionModel.__table__.columns.keys())
    assert cols == INTERACTION_COLUMNS
    assert "artifacts" not in cols


def test_sdk_agent_response_mirrors_domain_field_face():
    """DoD #3: SDK AgentResponse 字段面与 domain AgentResponse 字段面对等。

    前瞻：落地后 domain AgentResponse 会加性加入 artifacts，SDK 镜像必须同步，
    否则本条失败。
    """
    from app.domain.agent_response import AgentResponse as DomainAgentResponse
    assert set(SdkAgentResponse.__dataclass_fields__) == set(
        DomainAgentResponse.__dataclass_fields__
    )


def test_minimal_encode_response_wire_keys():
    """DoD #6: minimal wire 响应严格为 {content, session_id}，artifact 不进 wire。"""
    out = encode_response(SdkAgentResponse(content="hello"))
    assert set(out.keys()) == {"content", "session_id"}
    assert out == {"content": "hello", "session_id": None}


def test_artifact_contract_when_implemented():
    """DoD #1/#2 前瞻硬锁：若 Artifact 已落地，断言字段面 == 规格。

    未实现时 skip，保证本 Gate（设计/测试-only）在当前仓库全绿；
    生产落地后自动激活为硬锁，无需改本文件。
    """
    try:
        from app.domain.artifact import Artifact
    except ImportError:
        pytest.skip("Artifact 未实现（V0.2 设计阶段）—— 落地后本条自动转为硬锁")

    expected = {
        "artifact_id", "kind", "name",
        "uri", "mime_type", "size_bytes", "metadata",
    }
    assert set(Artifact.__dataclass_fields__) == expected

    # AgentResponse 加性 artifacts 字段已就位（DoD #2）
    from app.domain.agent_response import AgentResponse as DomainAgentResponse
    assert "artifacts" in DomainAgentResponse.__dataclass_fields__
    assert DomainAgentResponse.__dataclass_fields__["artifacts"].default is None
