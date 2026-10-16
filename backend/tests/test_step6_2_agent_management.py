"""Step 6.2 —— Agent Management Application Slice 验收。

不是 CRUD 全家桶，而是确认 Agent 的 Create / Get / Update + 基础业务约束
在 UoW transaction boundary 上稳定运行：

- 输入契约校验（InvalidAgentInputError）
- 创建（应用级去重守卫 DuplicateAgentError；全链路 round-trip）
- 查询（existing → Agent；missing → None）
- 更新（AgentNotFoundError / 部分更新落库）
- 事务边界（每个用例自持一个 UoW，commit 生效 / 异常回滚）

仍然不做：Channel/User/Identity/Connection Management、API、Runtime、
Router、AgentAdapter（Connection 是运行时授权边界，等 Agent/Channel
管理模型稳定后再单独切 Vertical Slice）。
"""

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.application import (
    AgentApplicationService,
    AgentNotFoundError,
    CreateAgent,
    DuplicateAgentError,
    InvalidAgentInputError,
    UpdateAgent,
)
from app.domain.agent import Agent
from app.persistence.models.agent import AgentModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork


def _factory(db_engine) -> sessionmaker:
    return sessionmaker(bind=db_engine, class_=Session, expire_on_commit=False)


def _service(db_engine) -> AgentApplicationService:
    return AgentApplicationService(
        uow_factory=lambda: SqlAlchemyUnitOfWork(session_factory=_factory(db_engine))
    )


def _count(db_engine) -> int:
    with Session(db_engine) as session:
        return session.execute(select(func.count()).select_from(AgentModel)).scalar_one()


# ------------------------------------------------ 输入契约 / 基础业务约束


@pytest.mark.parametrize(
    "bad_input",
    [
        CreateAgent(name="", type="acp"),
        CreateAgent(name="   ", type="acp"),
        CreateAgent(name="ok", type=""),
        CreateAgent(name="ok", type="  "),
    ],
    ids=["empty-name", "blank-name", "empty-type", "blank-type"],
)
def test_create_rejects_invalid_input(db_engine, bad_input) -> None:
    service = _service(db_engine)
    with pytest.raises(InvalidAgentInputError):
        service.create_agent(bad_input)
    assert _count(db_engine) == 0


def test_update_rejects_invalid_input(db_engine) -> None:
    service = _service(db_engine)
    created = service.create_agent(CreateAgent(name="helper", type="acp"))
    with pytest.raises(InvalidAgentInputError):
        service.update_agent(created.id, UpdateAgent(name=""))
    # 原值保持不变
    assert service.get_agent(created.id).name == "helper"


# ------------------------------------------------ Create（含去重守卫）


def test_create_agent_round_trip(db_engine) -> None:
    service = _service(db_engine)
    created = service.create_agent(
        CreateAgent(
            name="helper",
            type="acp",
            endpoint="https://agent.example/acp",
            config={"timeout_ms": 5000},
            credential_ref="secret://acp/helper",
        )
    )
    assert created.id is not None
    assert created.status == "active"

    fetched = service.get_agent(created.id)
    assert fetched == created
    assert fetched == Agent(
        id=created.id, name="helper", type="acp",
        endpoint="https://agent.example/acp", status="active",
        config={"timeout_ms": 5000}, credential_ref="secret://acp/helper",
        created_at=fetched.created_at, updated_at=fetched.updated_at,
    )
    assert _count(db_engine) == 1


def test_create_duplicate_name_rejected(db_engine) -> None:
    service = _service(db_engine)
    service.create_agent(CreateAgent(name="helper", type="acp"))

    with pytest.raises(DuplicateAgentError):
        service.create_agent(CreateAgent(name="helper", type="acp"))

    assert _count(db_engine) == 1


def test_create_duplicate_rolls_back_nothing_persisted(db_engine) -> None:
    """重复创建抛错后事务回滚，不留半条数据。"""
    service = _service(db_engine)
    service.create_agent(CreateAgent(name="helper", type="acp"))
    with pytest.raises(DuplicateAgentError):
        service.create_agent(
            CreateAgent(name="helper", type="acp", endpoint="https://other.example"))
    assert _count(db_engine) == 1
    rows = _all_rows(db_engine)
    assert rows[0][2] == "acp"  # 没有残留第二次的 type=wecom 半成品


def _all_rows(db_engine):
    with Session(db_engine) as session:
        return session.execute(
            select(AgentModel.id, AgentModel.name, AgentModel.type)
        ).all()


# ------------------------------------------------ Get


def test_get_agent_existing_and_missing(db_engine) -> None:
    service = _service(db_engine)
    created = service.create_agent(CreateAgent(name="exists", type="acp"))
    assert service.get_agent(created.id) == created
    assert service.get_agent(99999) is None


# ------------------------------------------------ Update


def test_update_agent_partial_fields_persist(db_engine) -> None:
    service = _service(db_engine)
    created = service.create_agent(
        CreateAgent(name="helper", type="acp", endpoint="https://old.example")
    )

    updated = service.update_agent(
        created.id,
        UpdateAgent(name="helper-v2", endpoint="https://new.example", credential_ref="secret://v2"),
    )
    assert updated.id == created.id
    assert updated.name == "helper-v2"
    assert updated.endpoint == "https://new.example"
    assert updated.credential_ref == "secret://v2"

    # 跨 UoW 重新读取：改动真实落库；未提供的字段保持不变
    refetched = service.get_agent(created.id)
    assert refetched == updated
    assert refetched.type == "acp"
    assert refetched.status == "active"


def test_update_missing_agent_raises_not_found(db_engine) -> None:
    service = _service(db_engine)
    with pytest.raises(AgentNotFoundError):
        service.update_agent(99999, UpdateAgent(name="ghost"))


def test_update_missing_does_not_create_row(db_engine) -> None:
    service = _service(db_engine)
    with pytest.raises(AgentNotFoundError):
        service.update_agent(99999, UpdateAgent(name="ghost"))
    assert _count(db_engine) == 0


def test_update_agent_status_management_state_only(db_engine) -> None:
    """11.2 §1.2：status 输入收口为管理态 {active, inactive}；运行态 error 与
    任意其它值一律拒绝（error 仅由 Runtime dispatch 失败路径写入）。"""
    service = _service(db_engine)
    created = service.create_agent(CreateAgent(name="helper", type="acp"))
    assert created.status == "active"

    inactive = service.update_agent(created.id, UpdateAgent(status="inactive"))
    assert inactive.status == "inactive"
    assert service.get_agent(created.id).status == "inactive"

    for bad in ("error", "disabled", "running"):
        with pytest.raises(InvalidAgentInputError):
            service.update_agent(created.id, UpdateAgent(status=bad))
    assert service.get_agent(created.id).status == "inactive"


# ------------------------------------------------ 事务边界


def test_transaction_boundary_commit_is_effective(db_engine) -> None:
    """每个用例自持一个 UoW：create/update 提交后立即可见。"""
    service = _service(db_engine)
    created = service.create_agent(CreateAgent(name="tx-ok", type="acp"))
    assert _count(db_engine) == 1
    service.update_agent(created.id, UpdateAgent(status="inactive"))
    assert _count(db_engine) == 1
    assert service.get_agent(created.id).status == "inactive"


def test_transaction_boundary_rollback_on_exception(db_engine) -> None:
    """用例中途异常 → UoW 自动回滚 → 无残留。"""
    # 注入 create 真实 flush 后抛异常的 adapter，验证 service 层面异常回滚
    from app.persistence.repositories.agent import SqlAlchemyAgentRepository

    class _FlushThenRaise(SqlAlchemyAgentRepository):
        def create(self, agent: Agent) -> Agent:
            super().create(agent)  # 真实 flush
            raise RuntimeError("boom")

    class _FailingUoW(SqlAlchemyUnitOfWork):
        def __enter__(self):
            super().__enter__()
            self.agents = _FlushThenRaise(self.session)
            return self

    service = AgentApplicationService(
        uow_factory=lambda: _FailingUoW(session_factory=_factory(db_engine))
    )
    with pytest.raises(RuntimeError):
        service.create_agent(CreateAgent(name="boom", type="acp"))
    assert _count(db_engine) == 0
