"""ACP V0.2 Artifact Implementation —— 实施验收（DoD 8 项 + 无扩张证明）。

本模块证明「已冻结的 Contract 被实现」，且**没有**产生
Delivery / Storage / Runtime 语义扩张：

    旧 Agent:  AgentResponse("hello")            → 仍然成功（backward compat）
    新 Agent:  AgentResponse("hello", artifacts=[Artifact(...)])
               → 成功 → Interaction snapshot → artifacts 保真存在

    Artifact → 不进 Delivery
             → 不进 Tencent SSE
             → 不进 DB schema
             → 不进 RuntimeExecutionResult

参考：docs/step-acp-v0.2-artifact-contract-gate.md（Contract Design + Impact Audit）。
"""

from dataclasses import fields
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy.orm import Session as SASession, sessionmaker

import agent_runtime_sdk.minimal as sdk_minimal
import agent_runtime_sdk.tencent_acp_sse as sdk_tencent
from agent_runtime_sdk import AgentResponse as SdkAgentResponse
from agent_runtime_sdk import Artifact as SdkArtifact
from agent_runtime_sdk import encode_response
from app.application import RuntimeCoordinator
from app.application.agent_dispatch import AgentDispatchExecutor
from app.application.delivery import DeliveryExecutor
from app.application.errors import AgentResponseInvalidError
from app.application.access_authorization import AccessAuthorizationExecutor
from app.application.identity_resolution import IdentityResolutionExecutor
from app.application.interaction_recording import PersistingCoordinator
from app.application.routing import RoutingExecutor
from app.application.session_creation import SessionCreationExecutor
from app.application.session_resolution import SessionResolutionExecutor
from app.domain.agent import Agent
from app.domain.agent_response import AgentResponse
from app.domain.artifact import ARTIFACT_KIND_FILE, Artifact
from app.domain.channel import Channel
from app.domain.connection import Connection
from app.domain.connection_conversation import ConnectionConversation
from app.domain.identity import Identity
from app.domain.inbound_interaction import InboundInteraction
from app.domain.outbound_response import OutboundResponse
from app.domain.runtime_execution_result import RuntimeExecutionResult
from app.domain.user import User
from app.persistence.models.interaction import InteractionModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

# ================================================================== 测试底座


class _EchoAgentAdapter:
    """旧 Agent：只返回 content，invariants 与 8.5 一致。"""

    def send(self, request, connection) -> AgentResponse:  # noqa: ARG002
        return AgentResponse(content=f"echo:{request.content}")


class _ArtifactAgentAdapter:
    """新 Agent：返回 content + artifacts。"""

    def __init__(self, artifacts: list[Artifact] | None) -> None:
        self._artifacts = artifacts

    def send(self, request, connection) -> AgentResponse:  # noqa: ARG002
        return AgentResponse(
            content=f"echo:{request.content}", artifacts=self._artifacts)


class _RecordingChannelStub:
    def __init__(self) -> None:
        self.deliveries: list[OutboundResponse] = []

    def deliver(self, outbound: OutboundResponse) -> None:
        self.deliveries.append(outbound)


def _uow_factory(db_engine):
    return lambda: SqlAlchemyUnitOfWork(
        session_factory=sessionmaker(bind=db_engine, expire_on_commit=False))


def _seed(db_engine) -> dict[str, int]:
    with _uow_factory(db_engine)() as uow:
        alice = uow.users.create(User(name="alice"))
        channel = uow.channels.create(Channel(name="wecom-1", type="wecom"))
        agent = uow.agents.create(Agent(name="agent-a", type="acp"))
        ident = uow.identities.create(Identity(
            user_id=alice.id, channel_id=channel.id, external_id="wx_a"))
        path = uow.connections.create(Connection(
            channel_id=channel.id, agent_id=agent.id, name="path-a",
            status="active",
            config={"endpoint": "http://agent.internal", "timeout_s": 5.0},
            credential_ref="cred-a"))
        # Implementation Package 2 / Step 2：显式授权该会话（default-deny 前提）
        uow.connection_conversations.create(ConnectionConversation(
            channel_id=channel.id, external_conversation_id="conv-1",
            connection_id=path.id))
        uow.commit()
        return {"channel": channel.id, "identity": ident.id}


def _wrapped_runtime(db_engine, agent_adapter=None, channel_adapter=None):
    uow = _uow_factory(db_engine)
    inner = RuntimeCoordinator(
        identity_resolution=IdentityResolutionExecutor(uow),
        access_authorization=AccessAuthorizationExecutor(uow),
        routing=RoutingExecutor(uow),
        session_resolution=SessionResolutionExecutor(uow),
        session_creation=SessionCreationExecutor(uow),
        agent_dispatch=AgentDispatchExecutor(
            agent_adapter=agent_adapter or _EchoAgentAdapter(), uow_factory=uow),
        delivery=DeliveryExecutor(
            channel_adapter=channel_adapter or _RecordingChannelStub()),
    )
    return PersistingCoordinator(inner=inner, uow_factory=_uow_factory(db_engine))


def _inbound(channel_id: int) -> InboundInteraction:
    return InboundInteraction(
        channel_id=channel_id, external_interaction_id="msg-1",
        external_conversation_id="conv-1",
        external_identity_id="wx_a", content="hello")


def _interaction_rows(db_engine) -> list[InteractionModel]:
    return list(SASession(db_engine)
                .query(InteractionModel).order_by(InteractionModel.id).all())


def _sample_artifact() -> Artifact:
    return Artifact(
        artifact_id="a-1", kind=ARTIFACT_KIND_FILE, name="spec.pdf",
        uri="https://example/spec.pdf", mime_type="application/pdf",
        size_bytes=1234, metadata={"rev": 2})


# ================================================================== DoD 1 Artifact value object


def test_dod1_artifact_is_frozen_value_object_with_frozen_field_face() -> None:
    names = {f.name for f in fields(Artifact)}
    assert names == {
        "artifact_id", "kind", "name",
        "uri", "mime_type", "size_bytes", "metadata",
    }
    assert Artifact.__dataclass_params__.frozen is True
    a = _sample_artifact()
    import dataclasses
    with pytest.raises(dataclasses.FrozenInstanceError):
        a.kind = "data"  # type: ignore[misc]
    # 必填仅 artifact_id / kind / name
    assert Artifact("id", "file", "n").uri is None


# ================================================================== DoD 2 CP additive field


def test_dod2_agent_response_additive_field_default_none() -> None:
    names = {f.name for f in fields(AgentResponse)}
    assert names == {"content", "external_session_id", "metadata", "artifacts"}
    assert AgentResponse.__dataclass_fields__["artifacts"].default is None


def test_dod6_plain_agent_response_is_still_legal() -> None:
    """DoD 6：`AgentResponse("hello")` 继续完全合法（含位置参数，旧调用形式）。"""
    r = AgentResponse("hello")
    assert (r.content, r.external_session_id, r.metadata, r.artifacts) == \
        ("hello", None, None, None)


# ================================================================== DoD 3 SDK mirror parity


def test_dod3_sdk_artifact_mirror_field_parity() -> None:
    assert {f.name for f in fields(SdkArtifact)} == {f.name for f in fields(Artifact)}
    assert {f.name for f in fields(SdkAgentResponse)} == \
        {f.name for f in fields(AgentResponse)}
    assert SdkAgentResponse.__dataclass_fields__["artifacts"].default is None


# ================================================================== DoD 4 _valid_response


def test_dod4_valid_response_accepts_none_and_valid_artifacts() -> None:
    assert AgentDispatchExecutor._valid_response(AgentResponse(content="hi")) is True
    assert AgentDispatchExecutor._valid_response(
        AgentResponse(content="hi", artifacts=[_sample_artifact()])) is True
    assert AgentDispatchExecutor._valid_response(
        AgentResponse(content="hi", artifacts=[])) is True


@pytest.mark.parametrize("bad_artifacts", [
    "not-a-list",
    [object()],                                            # 非 Artifact
    [Artifact(artifact_id="", kind="file", name="n")],     # artifact_id 空
    [Artifact(artifact_id="a", kind="  ", name="n")],      # kind 空白
    [Artifact(artifact_id="a", kind="file", name="")],     # name 空
    [Artifact(artifact_id="a", kind="file", name="n", uri=1)],        # uri 类型错
    [Artifact(artifact_id="a", kind="file", name="n", size_bytes="big")],  # size 类型错
    [Artifact(artifact_id="a", kind="file", name="n", metadata="no")],     # metadata 类型错
])
def test_dod4_valid_response_rejects_malformed_artifacts(bad_artifacts) -> None:
    assert AgentDispatchExecutor._valid_response(
        AgentResponse(content="hi", artifacts=bad_artifacts)) is False


def test_dod4_malformed_artifacts_fail_closed_end_to_end(db_engine) -> None:
    """非法 artifacts → AGENT_RESPONSE_INVALID（Dispatch Fail Closed），不落快照。"""
    g = _seed(db_engine)
    bad = [Artifact(artifact_id="", kind="file", name="n")]
    runtime = _wrapped_runtime(db_engine, agent_adapter=_ArtifactAgentAdapter(bad))
    with pytest.raises(AgentResponseInvalidError):
        runtime.execute(_inbound(g["channel"]))
    assert _interaction_rows(db_engine)[0].response_payload is None


# ================================================================== DoD 5/6 end-to-end snapshot


def test_dod6_old_agent_without_artifacts_still_succeeds(db_engine) -> None:
    """旧 Agent（AgentResponse(content=...)）端到端仍成功；快照 artifacts=None。"""
    g = _seed(db_engine)
    result = _wrapped_runtime(db_engine).execute(_inbound(g["channel"]))

    assert isinstance(result, RuntimeExecutionResult)
    assert result.agent_response.artifacts is None
    row = _interaction_rows(db_engine)[0]
    assert row.processing_status == "succeeded"
    assert row.response_payload["content"] == "echo:hello"
    assert row.response_payload["artifacts"] is None


def test_dod5_new_agent_artifacts_land_in_snapshot_faithfully(db_engine) -> None:
    """新 Agent：artifacts 保真进入 Interaction snapshot（显式字段映射）。"""
    g = _seed(db_engine)
    artifacts = [_sample_artifact()]
    result = _wrapped_runtime(
        db_engine, agent_adapter=_ArtifactAgentAdapter(artifacts)
    ).execute(_inbound(g["channel"]))

    assert result.agent_response.artifacts == artifacts
    row = _interaction_rows(db_engine)[0]
    assert row.response_payload["artifacts"] == [{
        "artifact_id": "a-1", "kind": "file", "name": "spec.pdf",
        "uri": "https://example/spec.pdf", "mime_type": "application/pdf",
        "size_bytes": 1234, "metadata": {"rev": 2},
    }]
    # 快照键面 == AgentResponse 字段面（单一事实源，无多余/缺失键）
    assert set(row.response_payload.keys()) == {f.name for f in fields(AgentResponse)}


# ================================================================== DoD 7 minimal wire unchanged


def test_dod7_minimal_wire_unchanged_with_artifacts_present() -> None:
    """artifacts 不进 minimal wire（同 metadata，15.5.5.2 冻结）。"""
    out = encode_response(SdkAgentResponse(
        content="c", artifacts=[SdkArtifact(artifact_id="a", kind="file", name="n")]))
    assert out == {"content": "c", "session_id": None}
    assert set(out.keys()) == {"content", "session_id"}


# ================================================================== 无扩张证明（Delivery/SSE/DB/Runtime）


def test_artifact_enters_delivery_as_reference(db_engine) -> None:
    """ACP V0.3 Phase 1：Artifact **进** Delivery（引用式），不改变文本投递语义。

    翻转自 V0.2 的「Artifact 不进 Delivery」——该边界由 ACP V0.3 契约取代
    （`docs/step-acp-v0.3-artifact-outbound-implementation-gate.md` §1.2.2 / §5.2 /
    §9.2 Q1）。**翻转的是「进不进 Delivery」这一条**；其余保护性断言**全部保留**：
    - 文本投递内容**不变**（`content` 仍是 Agent 的文本主体）；
    - 引用以**结构化引用**随交付载荷透传（`artifacts`），**不**渲染进 `content`
      ——**逐渠道呈现由 Channel Adapter 决定**（见 Gate §4.1 P3 / P4）；
    - Artifact 快照语义不变（`response_payload` 仍是完整原值）。
    """
    assert "artifacts" in {f.name for f in fields(OutboundResponse)}
    g = _seed(db_engine)
    channel = _RecordingChannelStub()
    artifacts = [_sample_artifact()]
    _wrapped_runtime(
        db_engine, agent_adapter=_ArtifactAgentAdapter(artifacts),
        channel_adapter=channel,
    ).execute(_inbound(g["channel"]))

    assert len(channel.deliveries) == 1
    delivered = channel.deliveries[0]
    assert delivered.content == "echo:hello"          # 文本主体不变
    assert delivered.artifacts == artifacts             # 引用被透传（保真）
    assert "spec.pdf" not in delivered.content          # 引用**未**被渲染进文本


def test_no_artifact_delivery_is_byte_identical(db_engine) -> None:
    """无 Artifact 时：出站载荷**逐字节**不变（`artifacts is None`，无新增键）。"""
    g = _seed(db_engine)
    channel = _RecordingChannelStub()
    _wrapped_runtime(db_engine, channel_adapter=channel).execute(
        _inbound(g["channel"]))

    assert len(channel.deliveries) == 1
    delivered = channel.deliveries[0]
    assert delivered.content == "echo:hello"
    assert delivered.artifacts is None


def test_no_expansion_artifact_not_in_tencent_sse_or_minimal() -> None:
    """Artifact 不进 Tencent SSE / minimal binding（源码面零引用）。"""
    for mod in (sdk_tencent, sdk_minimal):
        src = Path(mod.__file__).read_text(encoding="utf-8").lower()
        assert "artifact" not in src, f"{mod.__name__} 泄漏 artifact"


def test_no_expansion_artifact_not_in_db_schema(db_engine) -> None:
    """Artifact 不进 DB schema：interactions 无 artifacts 列，无 artifact 表。"""
    cols = {c.name for c in inspect(InteractionModel).columns}
    assert "artifacts" not in cols
    table_names = set(InteractionModel.metadata.tables.keys())
    assert not any("artifact" in t for t in table_names)


def test_no_expansion_artifact_not_in_runtime_execution_result() -> None:
    """Artifact 不进 RuntimeExecutionResult：字段面恒三字段。"""
    assert {f.name for f in fields(RuntimeExecutionResult)} == {
        "session_mode", "session_id", "agent_response"}
