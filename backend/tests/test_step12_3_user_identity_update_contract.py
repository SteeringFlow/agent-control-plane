"""Step 12.3 —— User / Identity Update Contract Reconciliation 验收。

冻结目标（12.3）：User / Identity 的 Update 语义与已验证的管理面 PATCH 模式
（11.2 Agent / 11.8 Channel / 11.11 Connection）完全一致：

    None / omitted = 不修改；PATCH {} → noop。

    UpdateUser      = {name?, status?}
    UpdateIdentity  = {display_name?, status?, metadata?}

同时保持（12.3 不放宽、不引入）：

- User.id 不可改（UpdateUser 无 id 字段）；
- Identity 绑定骨架（user_id / channel_id / external_id）不可改
  （UpdateIdentity 无绑定字段；改绑定 = Identity Linking，后续 Slice，
  本步绝不引入）；
- 非 None 空串仍然拒绝；
- metadata 非 dict 拒绝（对齐 Agent/Channel 的 config dict 校验——
  12.3 补齐的唯一真实生产代码差距）。
"""

import dataclasses

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.application import (
    CreateIdentity,
    CreateUser,
    IdentityApplicationService,
    InvalidIdentityInputError,
    InvalidUserInputError,
    UpdateIdentity,
    UpdateUser,
    UserApplicationService,
)
from app.domain.user import User
from app.persistence.models.identity import IdentityModel
from app.persistence.models.user import UserModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork


def _factory(db_engine) -> sessionmaker:
    return sessionmaker(bind=db_engine, class_=Session, expire_on_commit=False)


def _user_service(db_engine) -> UserApplicationService:
    return UserApplicationService(
        uow_factory=lambda: SqlAlchemyUnitOfWork(session_factory=_factory(db_engine))
    )


def _identity_service(db_engine) -> IdentityApplicationService:
    return IdentityApplicationService(
        uow_factory=lambda: SqlAlchemyUnitOfWork(session_factory=_factory(db_engine))
    )


def _count_users(db_engine) -> int:
    with Session(db_engine) as session:
        return session.execute(
            select(func.count()).select_from(UserModel)
        ).scalar_one()


def _count_identities(db_engine) -> int:
    with Session(db_engine) as session:
        return session.execute(
            select(func.count()).select_from(IdentityModel)
        ).scalar_one()


def _make_user(db_engine, name: str = "zhang-san") -> User:
    with SqlAlchemyUnitOfWork(session_factory=_factory(db_engine)) as uow:
        user = uow.users.create(User(name=name))
        uow.commit()
        return user


def _make_channel(db_engine, name: str = "wecom-main", channel_type: str = "wecom"):
    from app.domain.channel import Channel

    with SqlAlchemyUnitOfWork(session_factory=_factory(db_engine)) as uow:
        channel = uow.channels.create(Channel(name=name, type=channel_type))
        uow.commit()
        return channel


# ------------------------------------------------ 冻结契约的结构面（字段集合）


def test_update_contract_field_sets_frozen() -> None:
    """12.3 冻结：两个 Update 契约的字段面 == 冻结清单，无绑定字段、无新增。"""
    user_fields = {f.name for f in dataclasses.fields(UpdateUser)}
    identity_fields = {f.name for f in dataclasses.fields(UpdateIdentity)}

    assert user_fields == {"name", "status"}, f"UpdateUser 字段面漂移：{user_fields}"
    assert identity_fields == {"display_name", "status", "metadata"}, \
        f"UpdateIdentity 字段面漂移：{identity_fields}"

    # 不可变绑定绝不出现在 Update 契约中
    assert "id" not in user_fields, "User.id 不可通过 update 修改"
    binding = {"user_id", "channel_id", "external_id"}
    assert not binding & identity_fields, \
        "Identity 绑定骨架不可通过 update 修改（改绑定 = Linking，后续 Slice）"


# ------------------------------------------------ User：PATCH {} → noop + 部分更新


def test_user_empty_patch_is_noop(db_engine) -> None:
    """PATCH {}（UpdateUser() 全 None）→ noop：字段全部保留。"""
    service = _user_service(db_engine)
    created = service.create_user(CreateUser(name="noop-user"))

    updated = service.update_user(created.id, UpdateUser())

    assert updated == created
    assert service.get_user(created.id) == created
    assert _count_users(db_engine) == 1


def test_user_partial_update_keeps_omitted_fields(db_engine) -> None:
    """只提供部分字段：未提供（None）的字段保持原值。"""
    service = _user_service(db_engine)
    created = service.create_user(CreateUser(name="partial-user"))

    renamed = service.update_user(created.id, UpdateUser(name="partial-user-v2"))
    assert renamed.name == "partial-user-v2"
    assert renamed.status == created.status

    restatused = service.update_user(created.id, UpdateUser(status="disabled"))
    assert restatused.status == "disabled"
    assert restatused.name == "partial-user-v2"  # 前一次改名保留


def test_user_id_immutable_through_update(db_engine) -> None:
    """id 不可改：update 后主键不变（UpdateUser 亦无 id 字段）。"""
    service = _user_service(db_engine)
    created = service.create_user(CreateUser(name="id-user"))

    updated = service.update_user(created.id, UpdateUser(name="id-user-v2"))

    assert updated.id == created.id
    assert service.get_user(created.id).id == created.id


@pytest.mark.parametrize(
    "bad",
    [{"name": ""}, {"name": "   "}, {"status": ""}, {"status": "  "}],
    ids=["empty-name", "blank-name", "empty-status", "blank-status"],
)
def test_user_blank_string_still_rejected(db_engine, bad) -> None:
    """None 放行 ≠ 空串放行：非 None 空白串仍拒绝，且状态不变。"""
    service = _user_service(db_engine)
    created = service.create_user(CreateUser(name="guard-user"))

    with pytest.raises(InvalidUserInputError):
        service.update_user(created.id, UpdateUser(**bad))

    assert service.get_user(created.id) == created


# ------------------------------------------------ Identity：PATCH {} → noop + 部分更新


def test_identity_empty_patch_is_noop(db_engine) -> None:
    """PATCH {}（UpdateIdentity() 全 None）→ noop：档案与绑定全部保留。"""
    service = _identity_service(db_engine)
    user = _make_user(db_engine)
    channel = _make_channel(db_engine)
    created = service.create_identity(CreateIdentity(
        user_id=user.id, channel_id=channel.id, external_id="zhangsan",
        display_name="张三", metadata={"dept": "sales"},
    ))

    updated = service.update_identity(created.id, UpdateIdentity())

    assert updated == created
    assert service.get_identity(created.id) == created
    assert _count_identities(db_engine) == 1


def test_identity_partial_update_keeps_omitted_fields(db_engine) -> None:
    """只提供部分档案字段：未提供（None）的字段保持原值。"""
    service = _identity_service(db_engine)
    user = _make_user(db_engine)
    channel = _make_channel(db_engine)
    created = service.create_identity(CreateIdentity(
        user_id=user.id, channel_id=channel.id, external_id="zhangsan",
        display_name="张三", metadata={"dept": "sales"},
    ))

    renamed = service.update_identity(created.id, UpdateIdentity(display_name="张伟"))
    assert renamed.display_name == "张伟"
    assert renamed.status == created.status
    assert renamed.metadata == {"dept": "sales"}

    remeta = service.update_identity(created.id, UpdateIdentity(metadata={"dept": "ops"}))
    assert remeta.metadata == {"dept": "ops"}  # 提供 = 整段替换
    assert remeta.display_name == "张伟"        # 未提供 → 保留
    assert remeta.status == "active"

    restatused = service.update_identity(created.id, UpdateIdentity(status="disabled"))
    assert restatused.status == "disabled"
    assert restatused.display_name == "张伟"
    assert restatused.metadata == {"dept": "ops"}


def test_identity_binding_skeleton_immutable_through_update(db_engine) -> None:
    """绑定骨架（user_id / channel_id / external_id）在任何档案更新后不变。"""
    service = _identity_service(db_engine)
    user = _make_user(db_engine)
    channel = _make_channel(db_engine)
    created = service.create_identity(CreateIdentity(
        user_id=user.id, channel_id=channel.id, external_id="zhangsan",
    ))

    updated = service.update_identity(created.id, UpdateIdentity(
        display_name="张伟", status="disabled", metadata={"dept": "ops"},
    ))

    assert (updated.user_id, updated.channel_id, updated.external_id) == (
        user.id, channel.id, "zhangsan")


@pytest.mark.parametrize(
    "bad",
    [{"display_name": ""}, {"display_name": "  "}, {"status": ""}, {"status": "  "}],
    ids=["empty-display", "blank-display", "empty-status", "blank-status"],
)
def test_identity_blank_string_still_rejected(db_engine, bad) -> None:
    """None 放行 ≠ 空串放行：非 None 空白串仍拒绝，且状态不变。"""
    service = _identity_service(db_engine)
    user = _make_user(db_engine)
    channel = _make_channel(db_engine)
    created = service.create_identity(CreateIdentity(
        user_id=user.id, channel_id=channel.id, external_id="zhangsan",
        display_name="张三", metadata={"dept": "sales"},
    ))

    with pytest.raises(InvalidIdentityInputError):
        service.update_identity(created.id, UpdateIdentity(**bad))

    assert service.get_identity(created.id) == created


# ------------------------------------------------ metadata dict 校验（12.3 补齐的唯一生产差距）


@pytest.mark.parametrize("bad_metadata", ["not-a-dict", ["list"], 42])
def test_identity_metadata_non_dict_rejected_on_create(db_engine, bad_metadata) -> None:
    """create：metadata 非 dict → InvalidIdentityInputError（对齐 Agent/Channel config）。"""
    service = _identity_service(db_engine)
    user = _make_user(db_engine)
    channel = _make_channel(db_engine)

    with pytest.raises(InvalidIdentityInputError):
        service.create_identity(CreateIdentity(
            user_id=user.id, channel_id=channel.id, external_id="zhangsan",
            metadata=bad_metadata,
        ))

    assert _count_identities(db_engine) == 0


@pytest.mark.parametrize("bad_metadata", ["not-a-dict", ["list"], 42])
def test_identity_metadata_non_dict_rejected_on_update(db_engine, bad_metadata) -> None:
    """update：metadata 非 dict → InvalidIdentityInputError，且原 metadata 保留。"""
    service = _identity_service(db_engine)
    user = _make_user(db_engine)
    channel = _make_channel(db_engine)
    created = service.create_identity(CreateIdentity(
        user_id=user.id, channel_id=channel.id, external_id="zhangsan",
        metadata={"dept": "sales"},
    ))

    with pytest.raises(InvalidIdentityInputError):
        service.update_identity(created.id, UpdateIdentity(metadata=bad_metadata))

    assert service.get_identity(created.id).metadata == {"dept": "sales"}


def test_identity_metadata_none_on_update_keeps_existing(db_engine) -> None:
    """metadata=None 是「不修改」，不是「清空」——既有 metadata 保留。"""
    service = _identity_service(db_engine)
    user = _make_user(db_engine)
    channel = _make_channel(db_engine)
    created = service.create_identity(CreateIdentity(
        user_id=user.id, channel_id=channel.id, external_id="zhangsan",
        metadata={"dept": "sales"},
    ))

    updated = service.update_identity(created.id, UpdateIdentity(status="disabled"))

    assert updated.metadata == {"dept": "sales"}
