"""Cloud-0.1.3.1 —— WeCom Webhook Tenant Resolution（实现验证门）.

窄安全边界：WeCom 入站接纳必须 tenant-scoped，且租户来自**已认证 Channel**：

    URL channel_id（仅定位）
      → _load_channel            bootstrap 定位读（无 tenant 过滤，不构成信任）
      → adapter.parse_message    WeCom 验签（信任闸门）
      → 已认证 Channel.tenant_id ← 绝不取自 URL
      → TenantScope → tenant-scoped UoW（幂等回查 + PENDING 落库）

锁死的安全性质：
  A. URL channel_id 仅是定位键——未知 / 非法 → 拒绝，绝不据此建立可信作用域；
  B. 验签失败 → 不落任何行、不建立可信 TenantScope；
  C. PENDING 落在**已认证 Channel 的 tenant**，不是入口缺省租户；
  D. 验签**之后**才解析 tenant（顺序不可倒置）；
  E. 复用 B 的 TenantScope / UoW；未注入 tenant_scoped_factory 时退回 legacy
     行为（单租户，行为 == 隔离前）——向后兼容。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.channels.wecom import (
    WeComAdapter,
    compute_signature,
    encrypt_message,
)
from app.application.channel_inbound import ChannelInboundService, InboundAcceptance
from app.application.interaction_recording import PersistingCoordinator
from app.core.tenant import TENANT_LOCAL, default_tenant_scope
from app.domain.channel import Channel
from app.persistence.models.interaction import InteractionModel
from app.persistence.models.tenant import TenantModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork
from app.repositories.channel_inbound import InboundChannelConfigError, InboundSignatureError

TOKEN = "tenant-token"
AES_KEY = "c" * 43
CORP_ID = "wwtenant"
TIMESTAMP = "1409659813"
NONCE = "nonce-tenant"

TEXT_XML = (
    "<xml><ToUserName><![CDATA[wwtenant]]></ToUserName>"
    "<FromUserName><![CDATA[zhangsan]]></FromUserName>"
    "<CreateTime>1348831860</CreateTime>"
    "<MsgType><![CDATA[text]]></MsgType>"
    "<Content><![CDATA[hello tenant]]></Content>"
    "<MsgId>7001</MsgId></xml>"
)
IMAGE_XML = (
    "<xml><ToUserName><![CDATA[wwtenant]]></ToUserName>"
    "<FromUserName><![CDATA[zhangsan]]></FromUserName>"
    "<CreateTime>1348831860</CreateTime>"
    "<MsgType><![CDATA[image]]></MsgType>"
    "<PicUrl><![CDATA[http://example/p.jpg]]></PicUrl>"
    "<MsgId>7002</MsgId></xml>"
)

_INBOUND_CONFIG = {"token": TOKEN, "encoding_aes_key": AES_KEY, "corp_id": CORP_ID}


# ------------------------------------------------------------------ helpers


def _session_factory(db_engine):
    return sessionmaker(bind=db_engine, expire_on_commit=False)


def _bootstrap_uow(db_engine):
    """bootstrap 定位读工厂（tenant_id=None → 无 tenant 过滤）。"""
    sf = _session_factory(db_engine)
    return lambda: SqlAlchemyUnitOfWork(session_factory=sf, tenant_id=None)


def _scoped_uow_factory(db_engine):
    """可信写入工厂提供者：tenant_id → tenant-scoped UoW 工厂（B §7 工厂桥）。"""
    sf = _session_factory(db_engine)

    def provider(tenant_id: int):
        return lambda: SqlAlchemyUnitOfWork(session_factory=sf, tenant_id=tenant_id)

    return provider


def _ensure_tenant(db_engine, tenant_id: int) -> None:
    """确保 tenants 行存在（channels.tenant_id 有 FK；#1 由 create_all 预种）。"""
    with Session(db_engine) as session:
        if session.get(TenantModel, tenant_id) is None:
            session.add(TenantModel(
                id=tenant_id, name=f"tenant-{tenant_id}", status="active"))
            session.commit()


def _seed_channel(db_engine, *, tenant_id: int, name: str = "wecom-t") -> int:
    """在指定 tenant 下建一个 active WeCom 渠道（经 tenant-scoped UoW stamp tenant）。"""
    _ensure_tenant(db_engine, tenant_id)
    factory = _scoped_uow_factory(db_engine)(tenant_id)
    with factory() as uow:
        channel = uow.channels.create(Channel(
            name=name, type="wecom", config=dict(_INBOUND_CONFIG)))
        uow.commit()
        return channel.id


def _service(db_engine, *, tenant_aware: bool, adapter=None) -> ChannelInboundService:
    return ChannelInboundService(
        uow_factory=_bootstrap_uow(db_engine),
        intake=PersistingCoordinator(inner=None, uow_factory=_bootstrap_uow(db_engine)),
        inbound_adapter=adapter if adapter is not None else WeComAdapter(),
        tenant_scoped_factory=(
            _scoped_uow_factory(db_engine) if tenant_aware else None),
    )


def _message_request(plaintext_xml: str) -> dict:
    encrypt = encrypt_message(plaintext_xml, AES_KEY, CORP_ID)
    body = (f"<xml><ToUserName><![CDATA[{CORP_ID}]]></ToUserName>"
            f"<Encrypt><![CDATA[{encrypt}]]></Encrypt></xml>")
    return {
        "signature": compute_signature(TOKEN, TIMESTAMP, NONCE, encrypt),
        "timestamp": TIMESTAMP, "nonce": NONCE, "body": body,
    }


def _interaction_rows(db_engine) -> list[tuple[int, int]]:
    """(id, tenant_id) —— 直接读 ORM 列，验证写入落到的租户。"""
    with Session(db_engine) as session:
        return [
            (row.id, row.tenant_id)
            for row in session.query(InteractionModel).order_by(InteractionModel.id).all()
        ]


# ================================================================== A. 定位 → 拒绝


def test_a1_url_channel_id_is_routing_only_unknown_channel_rejected(db_engine) -> None:
    """未知 channel_id → InboundChannelConfigError，绝不建立可信作用域（仅定位键）。"""
    svc = _service(db_engine, tenant_aware=True)
    with pytest.raises(InboundChannelConfigError):
        svc.accept_message(99999, **_message_request(TEXT_XML))
    assert _interaction_rows(db_engine) == []


def test_a2_illegal_channel_id_rejected(db_engine) -> None:
    """非法 channel_id（0/负数）→ 拒绝（URL 参数不是信任来源）。"""
    svc = _service(db_engine, tenant_aware=True)
    for bad in (0, -1):
        with pytest.raises(InboundChannelConfigError):
            svc.accept_message(bad, **_message_request(TEXT_XML))


# ================================================================== B. 验签 → 拒绝


def test_b1_bad_signature_rejected_no_row_no_trusted_scope(db_engine) -> None:
    """验签失败 → InboundSignatureError；不落任何行（信任闸门在租户解析之前）。"""
    channel_id = _seed_channel(db_engine, tenant_id=2)
    svc = _service(db_engine, tenant_aware=True)
    req = _message_request(TEXT_XML)
    req["signature"] = "0" * 40
    with pytest.raises(InboundSignatureError):
        svc.accept_message(channel_id, **req)
    assert _interaction_rows(db_engine) == []


# ================================================================== C. 落库到已认证 tenant


def test_c1_message_lands_in_authenticated_channel_tenant(db_engine) -> None:
    """PENDING 落在已认证 Channel 的 tenant（2），不是入口缺省租户（1）。"""
    channel_id = _seed_channel(db_engine, tenant_id=2)
    svc = _service(db_engine, tenant_aware=True)
    acc = svc.accept_message(channel_id, **_message_request(TEXT_XML))
    assert isinstance(acc, InboundAcceptance)
    assert acc.interaction_id is not None
    assert acc.tenant_scope.tenant_id == 2                    # 可信作用域 = Channel 租户
    rows = _interaction_rows(db_engine)
    assert len(rows) == 1
    assert rows[0][1] == 2                                    # 行 tenant_id == 2


def test_c2_tenant_scope_not_from_url(db_engine) -> None:
    """tenant 来自 Channel 行，不是 URL：不同 Channel → 各自租户的作用域与落库。"""
    t1 = _seed_channel(db_engine, tenant_id=1, name="c-t1")
    t3 = _seed_channel(db_engine, tenant_id=3, name="c-t3")
    svc = _service(db_engine, tenant_aware=True)
    assert svc.accept_message(t1, **_message_request(TEXT_XML)).tenant_scope.tenant_id == 1
    assert svc.accept_message(t3, **_message_request(TEXT_XML)).tenant_scope.tenant_id == 3
    assert [tenant for _id, tenant in _interaction_rows(db_engine)] == [1, 3]


# ================================================================== D. 幂等 / ack-ignore


def test_d1_duplicate_message_deduped_within_tenant(db_engine) -> None:
    """同一 (channel, external_id) 重推 → 返回既有 id，只有一行（幂等 scope = tenant）。"""
    channel_id = _seed_channel(db_engine, tenant_id=2)
    svc = _service(db_engine, tenant_aware=True)
    first = svc.accept_message(channel_id, **_message_request(TEXT_XML))
    second = svc.accept_message(channel_id, **_message_request(TEXT_XML))
    assert first.interaction_id == second.interaction_id
    assert len(_interaction_rows(db_engine)) == 1


def test_d2_ack_ignore_returns_scope_without_row(db_engine) -> None:
    """非 text 消息 → acknowledge-ignore（interaction_id=None），仍带可信作用域。"""
    channel_id = _seed_channel(db_engine, tenant_id=2)
    svc = _service(db_engine, tenant_aware=True)
    acc = svc.accept_message(channel_id, **_message_request(IMAGE_XML))
    assert acc.interaction_id is None
    assert acc.tenant_scope.tenant_id == 2
    assert _interaction_rows(db_engine) == []


# ================================================================== E. 向后兼容 / 顺序


def test_e1_legacy_path_without_tenant_resolution_unchanged(db_engine) -> None:
    """未注入 tenant_scoped_factory → legacy 行为（缺省作用域，行为 == 隔离前）。"""
    channel_id = _seed_channel(db_engine, tenant_id=TENANT_LOCAL)
    svc = _service(db_engine, tenant_aware=False)
    acc = svc.accept_message(channel_id, **_message_request(TEXT_XML))
    assert acc.interaction_id is not None
    assert acc.tenant_scope == default_tenant_scope()
    assert acc.tenant_scope.tenant_id == TENANT_LOCAL


def test_e2_handle_message_compat_returns_int(db_engine) -> None:
    """handle_message 兼容入口仍返回 int | None（既有调用方零改动）。"""
    channel_id = _seed_channel(db_engine, tenant_id=2)
    svc = _service(db_engine, tenant_aware=True)
    interaction_id = svc.handle_message(channel_id, **_message_request(TEXT_XML))
    assert isinstance(interaction_id, int)
    assert svc.handle_message(channel_id, **_message_request(IMAGE_XML)) is None


def test_e3_verify_before_tenant_resolution_order_is_static() -> None:
    """静态顺序锁：accept_message 中 parse_message（验签）先于 _tenant_scope（信任）。"""
    src = (Path(__file__).resolve().parents[1] / "app" / "application"
           / "channel_inbound.py").read_text(encoding="utf-8")
    body = src.split("def accept_message", 1)[1].split("def handle_message", 1)[0]
    assert body.index("parse_message") < body.index("_tenant_scope"), \
        "验签必须早于租户解析（验签前不得建立可信 TenantScope）"


# ================================================================== F. bootstrap 投影


def test_f1_bootstrap_projection_reads_tenant_unscoped(db_engine) -> None:
    """find_tenant_id_by_id 是 bootstrap 定位投影：无 tenant 过滤，按 PK 读 tenant_id。"""
    channel_id = _seed_channel(db_engine, tenant_id=2)
    # 用 tenant=1 的作用域 UoW，仍能读到 tenant=2 渠道的 tenant_id
    # —— 这正是「定位读」：tenant 解析发生在 SET LOCAL 之外（B §12）。
    with SqlAlchemyUnitOfWork(
        session_factory=_session_factory(db_engine), tenant_id=1
    ) as uow:
        assert uow.channels.find_tenant_id_by_id(channel_id) == 2
        assert uow.channels.find_tenant_id_by_id(99999) is None


def test_f2_bootstrap_projection_does_not_leak_domain_tenant(db_engine) -> None:
    """tenant_id 不进 Domain：get_by_id 返回的 Channel 域无 tenant 字段。"""
    channel_id = _seed_channel(db_engine, tenant_id=2)
    with SqlAlchemyUnitOfWork(
        session_factory=_session_factory(db_engine), tenant_id=None
    ) as uow:
        channel = uow.channels.get_by_id(channel_id)
    assert channel is not None
    assert "tenant_id" not in set(channel.__dataclass_fields__)
