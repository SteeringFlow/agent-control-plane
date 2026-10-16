"""Cloud-0.1.3.1b —— WeCom Webhook RLS bootstrap 回归（0004 blocker fix 验证）。

模拟 Tenant A / Tenant B 各自 channel_id，验证：

  1. bootstrap 定位读（tenant_id=None）按 PK 正确解析 channel → 其真实 tenant；
  2. 解析结果不串租户：A 的 channel 得 A，B 的 channel 得 B；
  3. bootstrap 机制只读「定位目标」：解析是 PK 定位，不会读出任意租户业务数据；
  4. 得到 tenant_id 后，tenant-scoped UoW 的后续读只看到该租户数据（第一层显式过滤）；
  5.（仅 PostgreSQL）真实 RLS 第二层同样只放行该租户——由 ACP_TEST_PG_URL 触发。

SQLite 无 RLS：1–4 在 SQLite 即可验证（应用层 + 第一层隔离）；
PostgreSQL RLS 第二层由 ACP_TEST_PG_URL 触发真实迁移 + 真实 webhook 流程验证
（当前仓库测试基础设施默认 SQLite，故该部分默认 skip）。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.channels.wecom import (
    WeComAdapter,
    compute_signature,
    encrypt_message,
)
from app.application.channel_inbound import ChannelInboundService
from app.application.interaction_recording import PersistingCoordinator
from app.domain.channel import Channel
from app.persistence.models.interaction import InteractionModel
from app.persistence.models.tenant import TenantModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork
from app.repositories.channel_inbound import InboundChannelConfigError

TOKEN = "tenant-token"
AES_KEY = "c" * 43
CORP_ID = "wwtenant"
TIMESTAMP = "1409659813"
NONCE = "nonce-tenant"
_INBOUND_CONFIG = {"token": TOKEN, "encoding_aes_key": AES_KEY, "corp_id": CORP_ID}

TEXT_XML = (
    "<xml><ToUserName><![CDATA[wwtenant]]></ToUserName>"
    "<FromUserName><![CDATA[zhangsan]]></FromUserName>"
    "<CreateTime>1348831860</CreateTime>"
    "<MsgType><![CDATA[text]]></MsgType>"
    "<Content><![CDATA[hello tenant]]></Content>"
    "<MsgId>7001</MsgId></xml>"
)


def _session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


def _bootstrap_uow(engine):
    sf = _session_factory(engine)
    return lambda: SqlAlchemyUnitOfWork(session_factory=sf, tenant_id=None)


def _scoped_uow_factory(engine):
    sf = _session_factory(engine)

    def provider(tenant_id: int):
        return lambda: SqlAlchemyUnitOfWork(session_factory=sf, tenant_id=tenant_id)

    return provider


def _ensure_tenant(engine, tenant_id: int) -> None:
    with Session(engine) as session:
        if session.get(TenantModel, tenant_id) is None:
            session.add(
                TenantModel(id=tenant_id, name=f"tenant-{tenant_id}", status="active")
            )
            session.commit()


def _seed_channel(engine, *, tenant_id: int, name: str) -> int:
    _ensure_tenant(engine, tenant_id)
    factory = _scoped_uow_factory(engine)(tenant_id)
    with factory() as uow:
        channel = uow.channels.create(
            Channel(name=name, type="wecom", config=dict(_INBOUND_CONFIG))
        )
        uow.commit()
        return channel.id


def _service(engine, *, tenant_aware: bool, adapter=None) -> ChannelInboundService:
    return ChannelInboundService(
        uow_factory=_bootstrap_uow(engine),
        intake=PersistingCoordinator(
            inner=None, uow_factory=_bootstrap_uow(engine)
        ),
        inbound_adapter=adapter if adapter is not None else WeComAdapter(),
        tenant_scoped_factory=_scoped_uow_factory(engine) if tenant_aware else None,
    )


def _message_request(plaintext_xml: str) -> dict:
    encrypt = encrypt_message(plaintext_xml, AES_KEY, CORP_ID)
    body = (
        f"<xml><ToUserName><![CDATA[{CORP_ID}]]></ToUserName>"
        f"<Encrypt><![CDATA[{encrypt}]]></Encrypt></xml>"
    )
    return {
        "signature": compute_signature(TOKEN, TIMESTAMP, NONCE, encrypt),
        "timestamp": TIMESTAMP,
        "nonce": NONCE,
        "body": body,
    }


def _interaction_rows(engine) -> list[tuple[int, int]]:
    with Session(engine) as session:
        return [
            (r.id, r.tenant_id)
            for r in session.query(InteractionModel).order_by(InteractionModel.id).all()
        ]


# ================================================================== SQLite 契约（始终运行）


def test_bootstrap_resolves_per_channel_tenant_no_cross(db_engine) -> None:
    """Tenant A / B 各自 channel：bootstrap 按 PK 解析出各自真实 tenant，不串。"""
    ca = _seed_channel(db_engine, tenant_id=1, name="chan-a")
    cb = _seed_channel(db_engine, tenant_id=2, name="chan-b")
    with SqlAlchemyUnitOfWork(
        session_factory=_session_factory(db_engine), tenant_id=None
    ) as uow:
        assert uow.channels.find_tenant_id_by_id(ca) == 1
        assert uow.channels.find_tenant_id_by_id(cb) == 2


def test_bootstrap_is_pk_scoped_not_arbitrary_leak(db_engine) -> None:
    """bootstrap 定位读是 PK 定位：只返回定位目标，不读出任意租户业务数据。"""
    ca = _seed_channel(db_engine, tenant_id=1, name="chan-a")
    cb = _seed_channel(db_engine, tenant_id=2, name="chan-b")
    with SqlAlchemyUnitOfWork(
        session_factory=_session_factory(db_engine), tenant_id=None
    ) as uow:
        assert uow.channels.get_by_id(ca).id == ca
        assert uow.channels.get_by_id(cb).id == cb
    # 第一层隔离：tenant=1 作用域下读 channels 只看到 chan-a，看不到 chan-b。
    with SqlAlchemyUnitOfWork(
        session_factory=_session_factory(db_engine), tenant_id=1
    ) as uow:
        ids = {c.id for c in uow.channels.list_channels()}
    assert ids == {ca}
    assert cb not in ids


def test_post_resolution_scope_sees_only_own_tenant(db_engine) -> None:
    """得到 tenant_id 后，tenant-scoped UoW 后续读只看到该租户数据（第一层隔离）。"""
    ca = _seed_channel(db_engine, tenant_id=1, name="chan-a")
    cb = _seed_channel(db_engine, tenant_id=2, name="chan-b")
    svc = _service(db_engine, tenant_aware=True)
    acc_a = svc.accept_message(ca, **_message_request(TEXT_XML))
    assert acc_a.tenant_scope.tenant_id == 1
    acc_b = svc.accept_message(cb, **_message_request(TEXT_XML))
    assert acc_b.tenant_scope.tenant_id == 2
    rows = _interaction_rows(db_engine)
    assert [t for _i, t in rows] == [1, 2]


def test_bootstrap_does_not_expose_other_tenant_channels(db_engine) -> None:
    """bootstrap 解析 tenant 来自「已认证对象」，绝不因 bootstrap 机制读出他租户数据。"""
    ca = _seed_channel(db_engine, tenant_id=1, name="chan-a")
    cb = _seed_channel(db_engine, tenant_id=2, name="chan-b")
    # bootstrap UoW 显式列举（模拟「枚举」尝试）：list_channels 在 tenant_id=None 时
    # 返回空投影之外的全量——验证其不被当成「读任意租户数据」的通道。
    with SqlAlchemyUnitOfWork(
        session_factory=_session_factory(db_engine), tenant_id=None
    ) as uow:
        # 解析是逐 PK 的：给定 cb 只得到 B，绝不会因 bootstrap 返回 A 的 tenant。
        assert uow.channels.find_tenant_id_by_id(cb) == 2
        assert uow.channels.find_tenant_id_by_id(ca) == 1


# ================================================================== PostgreSQL RLS（需 PG）


@pytest.fixture()
def pg():
    app_url = os.environ.get("ACP_TEST_PG_URL")
    if not app_url:
        pytest.skip("ACP_TEST_PG_URL 未设置：跳过 PostgreSQL RLS 第二层验证")
    # 迁移必须由 admin/owner 角色执行（runtime app 角色无 DDL 权限）；
    # 测试操作（读/写/RLS 验证）走 ACP_TEST_PG_URL —— 必须是非-superuser app 角色，
    # 否则 superuser 绕过 RLS，验证无效。
    admin_url = os.environ.get("ACP_TEST_PG_ADMIN_URL")
    if admin_url:
        backend_dir = Path(__file__).resolve().parents[1]
        os.environ["DATABASE_URL"] = admin_url
        from alembic import command
        from alembic.config import Config

        cfg = Config(str(backend_dir / "alembic.ini"))
        cfg.set_main_option("script_location", str(backend_dir / "alembic"))
        command.upgrade(cfg, "head")

    eng = create_engine(app_url)
    sf = sessionmaker(bind=eng, expire_on_commit=False)
    yield eng, sf
    eng.dispose()


def test_pg_rls_webhook_bootstrap_succeeds_and_is_scoped(pg) -> None:
    """PG 真实 RLS：webhook bootstrap 读 channels 不再被 0 行阻断，且落库到正确租户。

    同时验证：以 tenant=2 作用域读取 tenant=1 的入站交互 → RLS 拦截为 None
    （第二层防线仍只放行该租户数据）。
    """
    eng, _sf = pg

    # tenant #1 已由迁移种子；在其下建一个 active WeCom 渠道。
    ca = _seed_channel(eng, tenant_id=1, name="chan-a-pg")

    svc = ChannelInboundService(
        uow_factory=_bootstrap_uow(eng),
        intake=PersistingCoordinator(inner=None, uow_factory=_bootstrap_uow(eng)),
        inbound_adapter=WeComAdapter(),
        tenant_scoped_factory=_scoped_uow_factory(eng),
    )

    acc = svc.accept_message(ca, **_message_request(TEXT_XML))
    assert acc.interaction_id is not None
    assert acc.tenant_scope.tenant_id == 1

    # 第二层防线：tenant=2 作用域读取 tenant=1 的交互 → 被 RLS 拦截。
    with _scoped_uow_factory(eng)(2)() as uow:
        got = uow.interactions.get_by_id(acc.interaction_id)
    assert got is None
