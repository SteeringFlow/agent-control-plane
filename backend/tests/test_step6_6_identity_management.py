"""Step 6.6 —— Identity Management Application Slice 验收。

Identity 是 Control Plane 的**外部身份桥梁**（User ──1:N── Identity ──N:1──
Channel ──external_id），核心唯一键 = (channel_id, external_id)（4.2 冻结的
UNIQUE）。因此本步与 User.name 的本质区别：Identity.external_id 就是外部身份键，
但**不是全局身份键**。

本步专门验证：

- Application 层第一次**同时协调** User / Channel / Identity 三个 Repository
  （存在性校验 + 唯一键预检 + 写入），但**不复制** User/Channel 的业务逻辑
  （Orchestration ≠ 职责污染）；
- WeCom/zhangsan 与 Feishu/zhangsan 默认是两个 Identity，可属于不同 User
  （**不做自动 Identity Linking**）；
- 绑定骨架（user_id/channel_id/external_id）不可通过 update 修改；
- Identity Management ≠ Identity Resolution（无 resolve 能力）。

仍然不做：Connection / Session / Interaction / Routing / Runtime /
Authentication / RBAC / 各种 identity matching（email/phone/fuzzy/merge）。
"""

import ast
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.application import (
    ChannelNotFoundError,
    CreateIdentity,
    DuplicateIdentityError,
    IdentityApplicationService,
    IdentityNotFoundError,
    InvalidIdentityInputError,
    UpdateIdentity,
    UserNotFoundError,
)
from app.domain.identity import Identity
from app.domain.user import User
from app.domain.channel import Channel
from app.persistence.models.identity import IdentityModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

BACKEND_DIR = Path(__file__).resolve().parents[1]
IDENTITIES_PY = BACKEND_DIR / "app" / "application" / "identities.py"


def _factory(db_engine) -> sessionmaker:
    return sessionmaker(bind=db_engine, class_=Session, expire_on_commit=False)


def _service(db_engine) -> IdentityApplicationService:
    return IdentityApplicationService(
        uow_factory=lambda: SqlAlchemyUnitOfWork(session_factory=_factory(db_engine))
    )


def _count(db_engine) -> int:
    with Session(db_engine) as session:
        return session.execute(
            select(func.count()).select_from(IdentityModel)
        ).scalar_one()


def _make_user(db_engine, name: str = "zhang-san") -> User:
    with SqlAlchemyUnitOfWork(session_factory=_factory(db_engine)) as uow:
        user = uow.users.create(User(name=name))
        uow.commit()
        return user


def _make_channel(db_engine, name: str, channel_type: str) -> Channel:
    with SqlAlchemyUnitOfWork(session_factory=_factory(db_engine)) as uow:
        channel = uow.channels.create(Channel(name=name, type=channel_type))
        uow.commit()
        return channel


# ------------------------------------------------ 存在性校验（跨仓储编排）


def test_create_identity_rejects_missing_user(db_engine) -> None:
    """user_id 不存在 → UserNotFoundError，且零写入。"""
    service = _service(db_engine)
    channel = _make_channel(db_engine, "wecom-main", "wecom")
    with pytest.raises(UserNotFoundError):
        service.create_identity(
            CreateIdentity(user_id=99999, channel_id=channel.id, external_id="zhangsan")
        )
    assert _count(db_engine) == 0


def test_create_identity_rejects_missing_channel(db_engine) -> None:
    """channel_id 不存在 → ChannelNotFoundError，且零写入。"""
    service = _service(db_engine)
    user = _make_user(db_engine)
    with pytest.raises(ChannelNotFoundError):
        service.create_identity(
            CreateIdentity(user_id=user.id, channel_id=99999, external_id="zhangsan")
        )
    assert _count(db_engine) == 0


def test_service_coordinates_three_repositories_without_pollution() -> None:
    """关键边界：IdentityApplicationService 只读校验 User/Channel + 写 Identity，
    绝不复制 User/Channel 的创建逻辑（Orchestration ≠ 职责污染）。"""
    tree = ast.parse(IDENTITIES_PY.read_text(encoding="utf-8"), filename=str(IDENTITIES_PY))

    calls = {_dotted(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}

    # 必须编排三个仓储（读存在性 + 写 Identity）
    assert any(c.startswith("uow.users.get_by_id") for c in calls), "应校验 user 存在"
    assert any(c.startswith("uow.channels.get_by_id") for c in calls), "应校验 channel 存在"
    assert any(c.startswith("uow.identities.") for c in calls), "应写入 identities"

    # 不得复制 User/Channel 创建逻辑
    assert not any(c.startswith("uow.users.create") or c.startswith("uow.users.update")
                   for c in calls), "不应创建/更新 User"
    assert not any(c.startswith("uow.channels.create") or c.startswith("uow.channels.update")
                   for c in calls), "不应创建/更新 Channel"
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            dotted = _dotted(node.func)
            assert not (dotted == "User" or dotted == "Channel"), \
                f"不应构造 User/Channel 领域对象：{dotted}"


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    if isinstance(node, ast.Call):
        return _dotted(node.func)
    return ""


# ------------------------------------------------ 唯一键 (channel_id, external_id)


def test_create_identity_round_trip(db_engine) -> None:
    service = _service(db_engine)
    user = _make_user(db_engine)
    channel = _make_channel(db_engine, "wecom-main", "wecom")

    created = service.create_identity(
        CreateIdentity(
            user_id=user.id,
            channel_id=channel.id,
            external_id="zhangsan",
            display_name="张三",
            metadata={"dept": "sales"},
        )
    )
    assert created.id is not None
    assert created.status == "active"

    fetched = service.get_identity(created.id)
    assert fetched == created
    assert fetched == Identity(
        id=created.id, user_id=user.id, channel_id=channel.id, external_id="zhangsan",
        display_name="张三", status="active", metadata={"dept": "sales"},
    )
    assert _count(db_engine) == 1


def test_duplicate_on_same_channel_rejected(db_engine) -> None:
    """UNIQUE(channel_id, external_id)：同渠道同外部身份重复 → DuplicateIdentityError。"""
    service = _service(db_engine)
    user = _make_user(db_engine)
    channel = _make_channel(db_engine, "wecom-main", "wecom")
    service.create_identity(
        CreateIdentity(user_id=user.id, channel_id=channel.id, external_id="zhangsan")
    )
    with pytest.raises(DuplicateIdentityError):
        service.create_identity(
            CreateIdentity(user_id=user.id, channel_id=channel.id, external_id="zhangsan")
        )
    assert _count(db_engine) == 1


def test_same_external_id_across_channels_allowed(db_engine) -> None:
    """核心语义：WeCom/zhangsan 与 Feishu/zhangsan 是两个 Identity，**不自动合并**。"""
    service = _service(db_engine)
    user1 = _make_user(db_engine, "zhang-san")
    user2 = _make_user(db_engine, "li-si")  # 另一个 User
    wecom = _make_channel(db_engine, "wecom-main", "wecom")
    feishu = _make_channel(db_engine, "feishu-main", "feishu")

    a = service.create_identity(
        CreateIdentity(user_id=user1.id, channel_id=wecom.id, external_id="zhangsan")
    )
    b = service.create_identity(
        CreateIdentity(user_id=user2.id, channel_id=feishu.id, external_id="zhangsan")
    )
    assert _count(db_engine) == 2
    # 两个外部身份、两个不同的 User —— 完全合法（不做自动 linking）
    assert a.user_id == user1.id
    assert b.user_id == user2.id
    assert a.id != b.id


def test_same_channel_multiple_external_ids_allowed(db_engine) -> None:
    """同 User + 同 Channel 下的不同外部身份合法（external_id 才是该渠道内的键）。"""
    service = _service(db_engine)
    user = _make_user(db_engine)
    channel = _make_channel(db_engine, "wecom-main", "wecom")
    a = service.create_identity(
        CreateIdentity(user_id=user.id, channel_id=channel.id, external_id="ext-A")
    )
    b = service.create_identity(
        CreateIdentity(user_id=user.id, channel_id=channel.id, external_id="ext-B")
    )
    assert _count(db_engine) == 2
    assert a.user_id == b.user_id


def test_db_unique_constraint_is_backstop(db_engine) -> None:
    """应用级预检之外，DB 的 UNIQUE(channel_id, external_id) 仍是硬兜底。"""
    service = _service(db_engine)
    user = _make_user(db_engine)
    channel = _make_channel(db_engine, "wecom-main", "wecom")
    service.create_identity(
        CreateIdentity(user_id=user.id, channel_id=channel.id, external_id="zhangsan")
    )

    # 绕过 Application 守卫直接写 → 撞 DB 唯一约束
    with pytest.raises(IntegrityError):
        with SqlAlchemyUnitOfWork(session_factory=_factory(db_engine)) as uow:
            uow.identities.create(
                Identity(user_id=user.id, channel_id=channel.id, external_id="zhangsan")
            )
            uow.commit()


# ------------------------------------------------ Get / Update


def test_get_identity_existing_and_missing(db_engine) -> None:
    service = _service(db_engine)
    user = _make_user(db_engine)
    channel = _make_channel(db_engine, "wecom-main", "wecom")
    created = service.create_identity(
        CreateIdentity(user_id=user.id, channel_id=channel.id, external_id="zhangsan")
    )
    assert service.get_identity(created.id) == created
    assert service.get_identity(99999) is None


def test_update_identity_profile_fields_persist(db_engine) -> None:
    service = _service(db_engine)
    user = _make_user(db_engine)
    channel = _make_channel(db_engine, "wecom-main", "wecom")
    created = service.create_identity(
        CreateIdentity(
            user_id=user.id, channel_id=channel.id, external_id="zhangsan",
            display_name="张三", metadata={"dept": "sales"},
        )
    )

    updated = service.update_identity(
        created.id,
        UpdateIdentity(display_name="张伟", status="disabled", metadata={"dept": "ops"}),
    )
    assert updated.id == created.id
    assert updated.display_name == "张伟"
    assert updated.status == "disabled"
    assert updated.metadata == {"dept": "ops"}
    # 绑定骨架不变
    assert updated.user_id == user.id
    assert updated.channel_id == channel.id
    assert updated.external_id == "zhangsan"

    refetched = service.get_identity(created.id)
    assert refetched == updated


def test_update_missing_identity_raises_not_found(db_engine) -> None:
    service = _service(db_engine)
    with pytest.raises(IdentityNotFoundError):
        service.update_identity(99999, UpdateIdentity(display_name="ghost"))
    assert _count(db_engine) == 0


def test_update_contract_has_no_binding_fields() -> None:
    """绑定骨架不可通过 update 修改：UpdateIdentity 无 user_id/channel_id/external_id。

    改绑定 = Identity Linking（未来 Slice），不是档案字段更新。
    """
    tree = ast.parse(IDENTITIES_PY.read_text(encoding="utf-8"), filename=str(IDENTITIES_PY))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "UpdateIdentity":
            fields = {
                stmt.target.id for stmt in node.body
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
            }
            assert fields == {"display_name", "status", "metadata"}, \
                f"UpdateIdentity 只应改档案字段：{fields}"


# ------------------------------------------------ 输入契约


@pytest.mark.parametrize(
    "bad",
    [
        CreateIdentity(user_id=0, channel_id=1, external_id="zhangsan"),
        CreateIdentity(user_id=-1, channel_id=1, external_id="zhangsan"),
        CreateIdentity(user_id=1, channel_id=0, external_id="zhangsan"),
        CreateIdentity(user_id=1, channel_id=1, external_id=""),
        CreateIdentity(user_id=1, channel_id=1, external_id="   "),
    ],
    ids=["zero-user", "negative-user", "zero-channel", "empty-external", "blank-external"],
)
def test_create_rejects_invalid_input(db_engine, bad) -> None:
    service = _service(db_engine)
    with pytest.raises(InvalidIdentityInputError):
        service.create_identity(bad)
    assert _count(db_engine) == 0


# ------------------------------------------------ Identity Management ≠ Resolution


def test_no_identity_resolution_smuggled() -> None:
    """Identity Management ≠ Identity Resolution：本 Slice 没有任何 resolve/link 能力。"""
    for py in sorted((BACKEND_DIR / "app" / "application").glob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                name = node.name.lower()
                assert "resolve" not in name, f"{py.name} 出现 resolve：{node.name}"
                assert "link" not in name, f"{py.name} 出现 link：{node.name}"
                assert "merger" not in name and "merge" not in name, \
                    f"{py.name} 出现 merge：{node.name}"
                assert "matcher" not in name and "match" not in name, \
                    f"{py.name} 出现 match：{node.name}"


# ------------------------------------------------ 事务边界


def test_transaction_boundary_commit_is_effective(db_engine) -> None:
    """每个用例自持一个 UoW：create 提交后立即可见。"""
    service = _service(db_engine)
    user = _make_user(db_engine)
    channel = _make_channel(db_engine, "wecom-main", "wecom")
    created = service.create_identity(
        CreateIdentity(user_id=user.id, channel_id=channel.id, external_id="zhangsan")
    )
    assert _count(db_engine) == 1
    service.update_identity(created.id, UpdateIdentity(status="disabled"))
    assert _count(db_engine) == 1
    assert service.get_identity(created.id).status == "disabled"


def test_transaction_boundary_rollback_on_exception(db_engine) -> None:
    """用例中途异常 → UoW 自动回滚 → 无残留（存在性校验成功后写入失败）。"""
    from app.persistence.repositories.identity import SqlAlchemyIdentityRepository

    class _FlushThenRaise(SqlAlchemyIdentityRepository):
        def create(self, identity: Identity) -> Identity:
            super().create(identity)  # 真实 flush
            raise RuntimeError("boom")

    class _FailingUoW(SqlAlchemyUnitOfWork):
        def __enter__(self):
            super().__enter__()
            self.identities = _FlushThenRaise(self.session)
            return self

    user = _make_user(db_engine)
    channel = _make_channel(db_engine, "wecom-main", "wecom")
    service = IdentityApplicationService(
        uow_factory=lambda: _FailingUoW(session_factory=_factory(db_engine))
    )
    with pytest.raises(RuntimeError):
        service.create_identity(
            CreateIdentity(user_id=user.id, channel_id=channel.id, external_id="zhangsan")
        )
    assert _count(db_engine) == 0
