"""Step 5.4 First Vertical Slice 验收：Repository Port 链路真实 round-trip。

Port(协议)
  ↓
SQLAlchemy Adapter
  ↓
ORM Model
  ↓
Mapper
  ↓
Domain Entity

本轮只做 User / Channel / Agent 三个最简单的 Repository，
用它们证明链路成立；其余五个（Identity/Connection/Session/
Interaction/Activity）不提前实现（防漂移测试见文末）。

不做 Service / Routing / Resolver / Dispatcher / API / UoW（5.5）。
"""

from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.domain.agent import Agent
from app.domain.channel import Channel
from app.domain.user import User
from app.persistence.repositories.agent import SqlAlchemyAgentRepository
from app.persistence.repositories.channel import SqlAlchemyChannelRepository
from app.persistence.repositories.user import SqlAlchemyUserRepository
from app.repositories.agent import AgentRepository
from app.repositories.channel import ChannelRepository
from app.repositories.user import UserRepository


# ---------------------------------------------------------------- 适配器一致性


def test_adapters_conform_to_ports(db_engine) -> None:
    """Adapter 必须满足 Port 协议（结构兼容 + isinstance 检查）。"""
    with Session(db_engine) as session:
        assert isinstance(SqlAlchemyUserRepository(session), UserRepository)
        assert isinstance(SqlAlchemyChannelRepository(session), ChannelRepository)
        assert isinstance(SqlAlchemyAgentRepository(session), AgentRepository)


# ---------------------------------------------------------------- Round Trip


def test_user_round_trip(db_engine) -> None:
    """Domain -> Repository -> DB(commit) -> Repository -> Domain。"""
    with Session(db_engine) as session:
        repo = SqlAlchemyUserRepository(session)
        created = repo.create(User(name="zhang-san"))
        assert created.id is not None
        assert created.name == "zhang-san"
        created_id = created.id
        session.commit()

    with Session(db_engine) as session:
        fetched = SqlAlchemyUserRepository(session).get_by_id(created_id)

    assert fetched is not None
    assert fetched == User(id=created_id, name="zhang-san", status="active")


def test_channel_round_trip(db_engine) -> None:
    with Session(db_engine) as session:
        repo = SqlAlchemyChannelRepository(session)
        created = repo.create(
            Channel(name="wecom", type="wecom", config={"agent_id": "corp-1"})
        )
        created_id = created.id
        session.commit()

    with Session(db_engine) as session:
        fetched = SqlAlchemyChannelRepository(session).get_by_id(created_id)

    assert fetched is not None
    assert fetched == Channel(
        id=created_id, name="wecom", type="wecom",
        status="active", config={"agent_id": "corp-1"},
    )


def test_agent_round_trip_with_full_fields(db_engine) -> None:
    with Session(db_engine) as session:
        repo = SqlAlchemyAgentRepository(session)
        created = repo.create(
            Agent(
                name="helper",
                type="acp",
                endpoint="https://agent.example/acp",
                config={"timeout_ms": 5000},
                credential_ref="secret://acp/helper",
            )
        )
        created_id = created.id
        session.commit()

    with Session(db_engine) as session:
        fetched = SqlAlchemyAgentRepository(session).get_by_id(created_id)

    assert fetched is not None
    assert fetched == Agent(
        id=created_id,
        name="helper",
        type="acp",
        endpoint="https://agent.example/acp",
        status="active",
        config={"timeout_ms": 5000},
        credential_ref="secret://acp/helper",
        created_at=fetched.created_at, updated_at=fetched.updated_at,
    )


def test_get_by_id_missing_returns_none(db_engine) -> None:
    with Session(db_engine) as session:
        assert SqlAlchemyUserRepository(session).get_by_id(99999) is None
        assert SqlAlchemyChannelRepository(session).get_by_id(99999) is None
        assert SqlAlchemyAgentRepository(session).get_by_id(99999) is None


def test_commit_failure_rolls_back_on_rollback(db_engine) -> None:
    """Repository 不 commit：显式 rollback 后数据不落库（事务归调用方）。"""
    with Session(db_engine) as session:
        created = SqlAlchemyUserRepository(session).create(User(name="temp-user"))
        created_id = created.id
        session.rollback()

    with Session(db_engine) as session:
        assert SqlAlchemyUserRepository(session).get_by_id(created_id) is None


# ---------------------------------------------------------------- Domain 纯度


def test_domain_entities_are_pure_python() -> None:
    """Domain Entity 不得继承 SQLAlchemy Base（Persistence 不泄漏进 Domain）。"""
    from app.persistence.database import Base

    for entity in (User, Channel, Agent):
        assert not issubclass(entity, Base)
        assert not hasattr(entity, "__table__")


# ---------------------------------------------------------------- 防漂移：只做三个


def test_remaining_slices_are_not_implemented_yet() -> None:
    """5.4 本轮刻意只做 User/Channel/Agent（随后 6.6 加入 Identity）。

    Interaction/Activity 的 port / adapter / mapper / domain 文件都不应提前出现
    （Session 自 9.3 起随 Session Resolution Execution 落地读路径）。
    """
    remaining = ("interaction", "activity")
    backend = Path(__file__).resolve().parents[1]

    unexpected: list[str] = []
    for name in remaining:
        candidates = [
            backend / "app" / "repositories" / f"{name}.py",
            backend / "app" / "persistence" / "repositories" / f"{name}.py",
            backend / "app" / "persistence" / "mappers" / f"{name}.py",
            backend / "app" / "domain" / f"{name}.py",
        ]
        unexpected.extend(str(p) for p in candidates if p.exists())

    assert not unexpected, (
        f"First Vertical Slice 之外的文件被提前实现（属于后续 Slice）：\n"
        + "\n".join(unexpected)
    )
