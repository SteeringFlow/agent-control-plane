"""Step 12.9.4.3 —— Interaction Snapshot & Reconstruction。

建立并锁定这条闭环：

    InboundInteraction
            │ persist（create_pending → inbound_to_snapshot）
            ▼
    Interaction.request_payload
            │ reconstruct（inbound_from_snapshot）
            ▼
    InboundInteraction

验收：
A  快照形状：create_pending 保存**完整**入站事实（含 metadata）
B  往返等价：重建结果与原始 inbound 在运行所需字段上等价（无事实丢失）
C  旧数据兼容：缺 metadata 键的旧行 → None，不抛错、不猜测
D  Fail Closed：快照不完整 / metadata 类型非法 → ValueError（绝不伪造入站）
E  response_url 往返：机器人回执地址经落库 + 重建后仍驱动正确投递通道
F  重建边界：snapshot 是纯 domain，不依赖 SQLAlchemy / persistence

边界（本步不做）：Worker、ChannelInboundService 改造、移除 Runtime.execute。
"""

from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from app.adapters.channels.wecom import (
    RESPONSE_URL_METADATA_KEY,
    _response_url_from,
)
from app.application.interaction_recording import PersistingCoordinator
from app.domain.channel import Channel
from app.domain.inbound_interaction import InboundInteraction
from app.domain.interaction_record import InteractionRecord
from app.domain.interaction_snapshot import (
    RUNTIME_REQUIRED_FIELDS,
    inbound_from_snapshot,
    inbound_to_snapshot,
)
from app.domain.outbound_response import OutboundResponse
from app.domain.user import User
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

BACKEND = Path(__file__).resolve().parents[1]
ROBOT_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=robot-1"


# ================================================================== 测试底座


class _StubRuntime:
    def execute(self, inbound: InboundInteraction) -> None:
        return None


def _uow_factory(db_engine):
    return lambda: SqlAlchemyUnitOfWork(
        session_factory=sessionmaker(bind=db_engine, expire_on_commit=False))


def _seed(db_engine) -> int:
    """最小种子（满足 interactions.channel_id FK），返回 channel_id。"""
    with _uow_factory(db_engine)() as uow:
        uow.users.create(User(name="alice"))
        channel = uow.channels.create(Channel(name="wecom-1", type="wecom"))
        uow.commit()
        return channel.id


def _coordinator(db_engine) -> PersistingCoordinator:
    return PersistingCoordinator(_StubRuntime(), _uow_factory(db_engine))


def _robot_inbound(channel_id: int = 1) -> InboundInteraction:
    """智能机器人入站：response_url 走 metadata（12.8.2 冻结）。"""
    return InboundInteraction(
        channel_id=channel_id, external_interaction_id="msg-1",
        external_conversation_id="conv-1", external_identity_id="wx_a",
        content="你好",
        metadata={RESPONSE_URL_METADATA_KEY: ROBOT_URL})


def _plain_inbound(channel_id: int = 1) -> InboundInteraction:
    """自建应用 XML 入站：不携带 metadata。"""
    return InboundInteraction(
        channel_id=channel_id, external_interaction_id="msg-2",
        external_conversation_id="conv-1", external_identity_id="wx_a",
        content="你好")


def _legacy_payload() -> dict:
    """12.9.4.3 之前落库的行：没有 metadata 键。"""
    return {
        "channel_id": 1,
        "external_interaction_id": "msg-1",
        "external_conversation_id": "conv-1",
        "external_identity_id": "wx_a",
        "content": "你好",
    }


def _row(db_engine, interaction_id: int) -> InteractionRecord:
    with _uow_factory(db_engine)() as uow:
        rec = uow.interactions.get_by_id(interaction_id)
        assert rec is not None
        return rec


# ================================================================== A. 快照形状


def test_a1_snapshot_carries_every_runtime_required_fact() -> None:
    """快照必须含全部运行时必需字段 + metadata（含 response_url）。"""
    snap = inbound_to_snapshot(_robot_inbound())
    for key in RUNTIME_REQUIRED_FIELDS:
        assert key in snap
    assert snap["metadata"] == {RESPONSE_URL_METADATA_KEY: ROBOT_URL}


def test_a2_metadata_key_exists_even_when_none() -> None:
    """metadata 恒存（可为 None）：让「入站未携带」与「快照缺键」可区分。"""
    snap = inbound_to_snapshot(_plain_inbound())
    assert "metadata" in snap
    assert snap["metadata"] is None


def test_a3_create_pending_persists_metadata(db_engine) -> None:
    """create_pending 落库后，request_payload 真实携带 metadata（12.9.3.5 缺口）。"""
    channel_id = _seed(db_engine)
    interaction_id = _coordinator(db_engine).create_pending(
        _robot_inbound(channel_id))

    payload = _row(db_engine, interaction_id).request_payload
    assert payload["metadata"] == {RESPONSE_URL_METADATA_KEY: ROBOT_URL}


# ================================================================== B. 往返等价


@pytest.mark.parametrize("original", [_plain_inbound(), _robot_inbound()])
def test_b1_snapshot_round_trip_is_lossless(original) -> None:
    """persist → reconstruct 无事实丢失（frozen dataclass 全字段相等）。"""
    assert inbound_from_snapshot(inbound_to_snapshot(original)) == original


def test_b2_db_round_trip_is_lossless(db_engine) -> None:
    """经真实 SQLite 落库 + 读回 + 重建，结果仍与原始 inbound 等价。"""
    channel_id = _seed(db_engine)
    original = _robot_inbound(channel_id)
    interaction_id = _coordinator(db_engine).create_pending(original)

    rebuilt = inbound_from_snapshot(_row(db_engine, interaction_id).request_payload)
    assert rebuilt == original
    assert rebuilt.metadata[RESPONSE_URL_METADATA_KEY] == ROBOT_URL


# ================================================================== C. 旧数据兼容


def test_c1_legacy_payload_without_metadata_reconstructs_to_none() -> None:
    """缺 metadata 键的旧行 → None，与自建应用入站路径一致，不抛错。"""
    rebuilt = inbound_from_snapshot(_legacy_payload())
    assert rebuilt.metadata is None
    assert rebuilt.content == "你好"
    assert rebuilt.external_interaction_id == "msg-1"


def test_c2_legacy_row_in_db_reconstructs_without_error(db_engine) -> None:
    """库里真实存在的旧行（无 metadata）也能被重建。"""
    channel_id = _seed(db_engine)
    with _uow_factory(db_engine)() as uow:
        rec = uow.interactions.create(InteractionRecord(
            direction="inbound", channel_id=channel_id,
            external_interaction_id="legacy-1",
            request_payload=_legacy_payload()))
        uow.commit()

    rebuilt = inbound_from_snapshot(_row(db_engine, rec.id).request_payload)
    assert rebuilt.metadata is None
    assert rebuilt.content == "你好"


# ================================================================== D. Fail Closed


def test_d1_missing_required_field_fails_closed() -> None:
    """必需字段缺失 → ValueError（静默补全 = 伪造入站输入）。"""
    with pytest.raises(ValueError):
        inbound_from_snapshot({"channel_id": 1, "content": "你好"})


def test_d2_non_dict_metadata_fails_closed() -> None:
    """metadata 既非 dict 也非 null → ValueError（类型非法不可静默通过）。"""
    payload = dict(_legacy_payload(), metadata="not-a-dict")
    with pytest.raises(ValueError):
        inbound_from_snapshot(payload)


@pytest.mark.parametrize("payload", [None, ["not", "an", "object"], "raw"])
def test_d3_non_object_payload_fails_closed(payload) -> None:
    with pytest.raises(ValueError):
        inbound_from_snapshot(payload)


# ================================================================== E. response_url 往返


def test_e1_reconstructed_response_url_still_drives_robot_delivery(db_engine) -> None:
    """重建后的 metadata 仍驱动机器人回执通道——不静默回退 message/send。

    这是 12.9.3.5 缺口的正向守卫：若快照漏存 metadata，这里会得到 None，
    deliver 会静默走 message/send（绕过入站 Fail Closed 且不报错）。
    """
    channel_id = _seed(db_engine)
    interaction_id = _coordinator(db_engine).create_pending(
        _robot_inbound(channel_id))
    rebuilt = inbound_from_snapshot(_row(db_engine, interaction_id).request_payload)

    # Runtime 把 channel-originated metadata 传播到 OutboundResponse（coordinator.py:142）
    outbound = OutboundResponse(
        channel_id=rebuilt.channel_id,
        external_conversation_id=rebuilt.external_conversation_id,
        content="hi", metadata=rebuilt.metadata)
    assert _response_url_from(outbound) == ROBOT_URL


def test_e2_without_metadata_delivery_falls_back_to_message_send() -> None:
    """无 metadata（旧数据 / 自建应用）→ 回执地址为 None → 走既有 message/send。"""
    rebuilt = inbound_from_snapshot(_legacy_payload())
    outbound = OutboundResponse(
        channel_id=rebuilt.channel_id,
        external_conversation_id=rebuilt.external_conversation_id,
        content="hi", metadata=rebuilt.metadata)
    assert _response_url_from(outbound) is None


# ================================================================== F. 重建边界


def test_f1_snapshot_module_is_pure_domain() -> None:
    """重建是纯 domain 能力：不依赖 SQLAlchemy / persistence / application。"""
    src = (BACKEND / "app" / "domain" / "interaction_snapshot.py").read_text(
        encoding="utf-8")
    for forbidden in ("sqlalchemy", "app.persistence", "app.application",
                      "app.repositories"):
        assert forbidden not in src, f"重建边界被污染：{forbidden}"
