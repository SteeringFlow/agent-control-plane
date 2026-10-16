"""Step 6.3 —— Channel Management Application Slice 验收。

与 6.2（Agent）对称但不机械复制：
- Channel 无 endpoint / credential_ref（入站渠道只有 name/type/status/config）
- 重复性按 (type, name) 判定：同一平台下的同名渠道才算重复，
  不同平台（wecom / feishu / ...）的同名渠道合法共存
- Channel type=wecom 只是分类字符串，不代表实现 WeCom Adapter/Runtime

覆盖：Create / Get / Update + 输入契约校验 + 错误映射 + UoW 事务边界。
仍然不做：WeCom/Feishu Adapter、webhook、channel authentication、
Connection、Routing、Identity、Runtime、API。
"""

import ast
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.application import (
    ChannelApplicationService,
    ChannelNotFoundError,
    CreateChannel,
    DuplicateChannelError,
    InvalidChannelInputError,
    UpdateChannel,
)
from app.domain.channel import Channel
from app.persistence.models.channel import ChannelModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

BACKEND_DIR = Path(__file__).resolve().parents[1]


def _factory(db_engine) -> sessionmaker:
    return sessionmaker(bind=db_engine, class_=Session, expire_on_commit=False)


def _service(db_engine) -> ChannelApplicationService:
    return ChannelApplicationService(
        uow_factory=lambda: SqlAlchemyUnitOfWork(session_factory=_factory(db_engine))
    )


def _count(db_engine) -> int:
    with Session(db_engine) as session:
        return session.execute(
            select(func.count()).select_from(ChannelModel)
        ).scalar_one()


# ------------------------------------------------ AST：channels.py 依赖边界


def test_channels_service_imports_only_ports_and_domain() -> None:
    """channels.py 只 import domain + repositories 端口 + 自身，绝不 import persistence/sqlalchemy。"""
    py = BACKEND_DIR / "app" / "application" / "channels.py"
    tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))

    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module)

    allowed_prefixes = (
        "app.domain", "app.repositories", "app.application",
        "collections", "dataclasses", "typing",
    )
    leaked = [
        r for r in roots
        if not any(r == p or r.startswith(p + ".") for p in allowed_prefixes)
    ]
    assert not leaked, f"channels.py 越层导入：{leaked}"
    assert any(r == "app.repositories" or r.startswith("app.repositories.")
               for r in roots), "Service 应依赖 repositories（UoW/仓储）端口"

    forbidden_names = {"SqlAlchemyUnitOfWork", "Session", "sessionmaker",
                       "create_engine", "Engine", "SessionLocal"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in forbidden_names:
            pytest.fail(f"channels.py 出现 SQLAlchemy 概念：{node.id}")


# ------------------------------------------------ 输入契约 / 基础业务约束


@pytest.mark.parametrize(
    "bad_input",
    [
        CreateChannel(name="", type="wecom"),
        CreateChannel(name="   ", type="wecom"),
        CreateChannel(name="ok", type=""),
        CreateChannel(name="ok", type="  "),
    ],
    ids=["empty-name", "blank-name", "empty-type", "blank-type"],
)
def test_create_rejects_invalid_input(db_engine, bad_input) -> None:
    service = _service(db_engine)
    with pytest.raises(InvalidChannelInputError):
        service.create_channel(bad_input)
    assert _count(db_engine) == 0


def test_update_rejects_invalid_input(db_engine) -> None:
    service = _service(db_engine)
    created = service.create_channel(CreateChannel(name="wecom-main", type="wecom"))
    with pytest.raises(InvalidChannelInputError):
        service.update_channel(created.id, UpdateChannel(name=""))
    # 原值保持不变
    assert service.get_channel(created.id).name == "wecom-main"


# ------------------------------------------------ Create（含同平台去重）


def test_create_channel_round_trip(db_engine) -> None:
    service = _service(db_engine)
    created = service.create_channel(
        CreateChannel(name="wecom-main", type="wecom", config={"agent_id": "corp-1"})
    )
    assert created.id is not None
    assert created.status == "active"

    fetched = service.get_channel(created.id)
    assert fetched == created
    assert fetched == Channel(
        id=created.id, name="wecom-main", type="wecom",
        status="active", config={"agent_id": "corp-1"},
    )
    assert _count(db_engine) == 1


def test_create_duplicate_same_type_same_name_rejected(db_engine) -> None:
    service = _service(db_engine)
    service.create_channel(CreateChannel(name="wecom-main", type="wecom"))

    with pytest.raises(DuplicateChannelError):
        service.create_channel(
            CreateChannel(name="wecom-main", type="wecom", config={"agent_id": "corp-2"})
        )

    assert _count(db_engine) == 1


def test_create_same_name_different_type_allowed(db_engine) -> None:
    """与 Agent 的关键差异：不同平台同名渠道合法共存（15.2.3 起受控集合内）。"""
    service = _service(db_engine)
    wecom = service.create_channel(CreateChannel(name="main", type="wecom"))
    local = service.create_channel(CreateChannel(name="main", type="local"))

    assert _count(db_engine) == 2
    assert wecom.type == "wecom"
    assert local.type == "local"


def test_create_duplicate_rolls_back_nothing_persisted(db_engine) -> None:
    service = _service(db_engine)
    service.create_channel(CreateChannel(name="main", type="wecom"))
    with pytest.raises(DuplicateChannelError):
        service.create_channel(
            CreateChannel(name="main", type="wecom", config={"agent_id": "corp-2"})
        )
    assert _count(db_engine) == 1
    rows = _all_rows(db_engine)
    assert rows[0][2] == "wecom"  # 没有残留第二次的 type=wecom 半成品


def _all_rows(db_engine):
    with Session(db_engine) as session:
        return session.execute(
            select(ChannelModel.id, ChannelModel.name, ChannelModel.type)
        ).all()


# ------------------------------------------------ Get


def test_get_channel_existing_and_missing(db_engine) -> None:
    service = _service(db_engine)
    created = service.create_channel(CreateChannel(name="exists", type="wecom"))
    assert service.get_channel(created.id) == created
    assert service.get_channel(99999) is None


# ------------------------------------------------ Update


def test_update_channel_partial_fields_persist(db_engine) -> None:
    service = _service(db_engine)
    created = service.create_channel(
        CreateChannel(name="wecom-main", type="wecom", config={"agent_id": "corp-1"})
    )

    updated = service.update_channel(
        created.id,
        UpdateChannel(name="wecom-support", config={"agent_id": "corp-1", "secret_ref": "s://1"}),
    )
    assert updated.id == created.id
    assert updated.name == "wecom-support"
    assert updated.config == {"agent_id": "corp-1", "secret_ref": "s://1"}

    # 跨 UoW 重新读取：改动真实落库；未提供的字段保持不变
    refetched = service.get_channel(created.id)
    assert refetched == updated
    assert refetched.type == "wecom"
    assert refetched.status == "active"


def test_update_channel_type_allowed(db_engine) -> None:
    """管理期允许在受控集合内改 type（渠道类型重分类；不触发 Adapter）。"""
    service = _service(db_engine)
    created = service.create_channel(CreateChannel(name="bot", type="wecom"))
    moved = service.update_channel(created.id, UpdateChannel(type="local"))
    assert moved.type == "local"
    assert service.get_channel(created.id).type == "local"


def test_update_missing_channel_raises_not_found(db_engine) -> None:
    service = _service(db_engine)
    with pytest.raises(ChannelNotFoundError):
        service.update_channel(99999, UpdateChannel(name="ghost"))


def test_update_missing_does_not_create_row(db_engine) -> None:
    service = _service(db_engine)
    with pytest.raises(ChannelNotFoundError):
        service.update_channel(99999, UpdateChannel(name="ghost"))
    assert _count(db_engine) == 0


def test_update_channel_status(db_engine) -> None:
    """基础状态字段可管理（status 为自由字符串，允许 disabled 等值）。"""
    service = _service(db_engine)
    created = service.create_channel(CreateChannel(name="wecom-main", type="wecom"))
    assert created.status == "active"

    disabled = service.update_channel(created.id, UpdateChannel(status="disabled"))
    assert disabled.status == "disabled"
    assert service.get_channel(created.id).status == "disabled"


# ------------------------------------------------ 事务边界


def test_transaction_boundary_commit_is_effective(db_engine) -> None:
    """每个用例自持一个 UoW：create/update 提交后立即可见。"""
    service = _service(db_engine)
    created = service.create_channel(CreateChannel(name="tx-ok", type="wecom"))
    assert _count(db_engine) == 1
    service.update_channel(created.id, UpdateChannel(status="disabled"))
    assert _count(db_engine) == 1
    assert service.get_channel(created.id).status == "disabled"


def test_transaction_boundary_rollback_on_exception(db_engine) -> None:
    """用例中途异常 → UoW 自动回滚 → 无残留。"""
    from app.persistence.repositories.channel import SqlAlchemyChannelRepository

    class _FlushThenRaise(SqlAlchemyChannelRepository):
        def create(self, channel: Channel) -> Channel:
            super().create(channel)  # 真实 flush
            raise RuntimeError("boom")

    class _FailingUoW(SqlAlchemyUnitOfWork):
        def __enter__(self):
            super().__enter__()
            self.channels = _FlushThenRaise(self.session)
            return self

    service = ChannelApplicationService(
        uow_factory=lambda: _FailingUoW(session_factory=_factory(db_engine))
    )
    with pytest.raises(RuntimeError):
        service.create_channel(CreateChannel(name="boom", type="wecom"))
    assert _count(db_engine) == 0
