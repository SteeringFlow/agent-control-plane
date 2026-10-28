"""P2 Delivery Recovery —— **PostgreSQL** 集成测试（并发 claim / attempt 围栏 / 租户隔离）。

SQLite **不能**替代本文件：本地测试用 `StaticPool`（单连接 ⇒ 写锁串行），
并发唯一性在 SQLite 上**测不出来**。本文件用**真实 PostgreSQL**（独立连接）验证：

  A. 并发首次 claim：两个连接同时 claim 同一行 ⇒ **恰好一个**成功；
  B. 并发失败重投 claim：同上；
  C. attempt 围栏：旧 attempt 的 outcome 写不进新 attempt；
  D. 租户隔离：另一租户的仓储不能 claim/改本租户的行。

运行（需要一个**专用** PostgreSQL 数据库；不会连接生产库）：

    docker run -d --name acp-p2-pg -e POSTGRES_PASSWORD=postgres \
      -e POSTGRES_DB=acp_p2 -p 55432:5432 postgres:17-alpine
    $env:TEST_POSTGRES_URL="postgresql+psycopg://postgres:postgres@127.0.0.1:55432/acp_p2"
    python -m pytest tests/test_p2_delivery_recovery_postgres.py -q

未设置 `TEST_POSTGRES_URL` ⇒ 整个文件 skip（**不**降级为 SQLite 断言）。
"""

import os
import threading

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.domain.interaction_record import (
    DELIVERY_DELIVERING,
    DELIVERY_FAILED,
    DELIVERY_PENDING,
    DELIVERY_PROTOCOL_V1,
    PROCESSING_SUCCEEDED,
)
from app.persistence.database import Base
from app.persistence.models.channel import ChannelModel
from app.persistence.models.interaction import InteractionModel
from app.persistence.models.tenant import TenantModel
from app.persistence.repositories.interaction_record import (
    SqlAlchemyInteractionRecordRepository,
)

PG_URL = os.environ.get("TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not PG_URL, reason="未配置 TEST_POSTGRES_URL（PostgreSQL 集成测试跳过）")

MAX_ATTEMPTS = 3
LEASE_SECONDS = 300


@pytest.fixture()
def pg_engine():
    engine = create_engine(PG_URL, future=True)
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


def _repo(engine, tenant_id: int | None = None):
    session = Session(engine)
    return session, SqlAlchemyInteractionRecordRepository(session, tenant_id)


def _seed(engine, tenant_id: int = 1) -> int:
    """造一条「Agent 已成功、待投递」的行（ORM 层，避免依赖领域对象映射）。"""
    with Session(engine) as s:
        if s.get(TenantModel, tenant_id) is None:
            s.add(TenantModel(id=tenant_id, name=f"tenant-{tenant_id}"))
            s.flush()
        channel = ChannelModel(name="c", type="wecom", tenant_id=tenant_id)
        s.add(channel)
        s.flush()
        row = InteractionModel(
            direction="inbound", channel_id=channel.id,
            external_interaction_id="msg-pg",
            request_payload={"channel_id": channel.id,
                             "external_interaction_id": "msg-pg",
                             "external_conversation_id": "conv-1",
                             "external_identity_id": "wx",
                             "content": "hello", "metadata": None},
            processing_status=PROCESSING_SUCCEEDED,
            delivery_status=DELIVERY_PENDING,
            response_payload={"content": "answer"},
            delivery_protocol=DELIVERY_PROTOCOL_V1,
            delivery_attempts=0,
        )
        row.tenant_id = tenant_id
        s.add(row)
        s.commit()
        return row.id


def test_pg_a1_concurrent_first_claim_has_exactly_one_winner(pg_engine) -> None:
    """并发首次 claim：两个**独立连接**同时 claim ⇒ 恰好一个成功。"""
    iid = _seed(pg_engine)
    barrier = threading.Barrier(2)
    results: list = []

    def worker() -> None:
        session, repo = _repo(pg_engine)
        barrier.wait()
        attempt = repo.claim_delivery(
            iid, max_attempts=MAX_ATTEMPTS, lease_seconds=LEASE_SECONDS)
        session.commit()
        results.append(attempt)
        session.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r is None for r in results) == [False, True]
    with Session(pg_engine) as s:
        row = s.get(InteractionModel, iid)
    assert row.delivery_status == DELIVERY_DELIVERING
    assert row.delivery_attempts == 1


def test_pg_a2_concurrent_retry_claim_has_exactly_one_winner(pg_engine) -> None:
    """并发失败重投 claim：同样恰好一个成功。"""
    iid = _seed(pg_engine)
    with Session(pg_engine) as s:
        row = s.get(InteractionModel, iid)
        row.delivery_status = DELIVERY_FAILED
        row.retryable = True
        row.delivery_attempts = 1
        s.commit()
    barrier = threading.Barrier(2)
    results: list = []

    def worker() -> None:
        session, repo = _repo(pg_engine)
        barrier.wait()
        attempt = repo.claim_delivery(
            iid, max_attempts=MAX_ATTEMPTS, lease_seconds=LEASE_SECONDS,
            include_failed=True)
        session.commit()
        results.append(attempt)
        session.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r is None for r in results) == [False, True]


def test_pg_c1_old_attempt_outcome_cannot_write_through(pg_engine) -> None:
    """attempt 围栏：旧 attempt（attempts=1）的 outcome 写不进新 attempt（=2）。"""
    iid = _seed(pg_engine)
    with Session(pg_engine) as s:
        row = s.get(InteractionModel, iid)
        row.delivery_status = DELIVERY_DELIVERING
        row.delivery_attempts = 2
        s.commit()
    session, repo = _repo(pg_engine)
    commit = repo.record_outcome(
        iid, processing_status="succeeded", delivery_status="delivered",
        expected_processing_status="succeeded",
        expected_delivery_status=DELIVERY_DELIVERING,
        expected_delivery_attempts=1)
    session.commit()
    assert commit.value == "rejected"
    with Session(pg_engine) as s:
        row = s.get(InteractionModel, iid)
    assert row.delivery_status == DELIVERY_DELIVERING
    assert row.delivery_attempts == 2


def test_pg_d1_tenant_isolation(pg_engine) -> None:
    """租户隔离：tenant=2 的仓储不能 claim tenant=1 的行。"""
    iid = _seed(pg_engine, tenant_id=1)
    session, repo = _repo(pg_engine, tenant_id=2)
    attempt = repo.claim_delivery(
        iid, max_attempts=MAX_ATTEMPTS, lease_seconds=LEASE_SECONDS)
    session.commit()
    assert attempt is None
    with Session(pg_engine) as s:
        row = s.get(InteractionModel, iid)
    assert row.delivery_attempts == 0
