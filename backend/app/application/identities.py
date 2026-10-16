"""Identity Management Application Service（Step 6.6）。

Identity 是 Control Plane 的**外部身份桥梁**：

    User ──1:N── Identity ──N:1── Channel（external_id）

与 Agent/Channel/User 的本质区别：IdentityApplicationService 第一次需要
**同时协调三个 Repository**——创建时必须回答三个问题：

1. user_id 是否存在？       → uow.users.get_by_id()      → 不存在抛 UserNotFoundError
2. channel_id 是否存在？    → uow.channels.get_by_id()   → 不存在抛 ChannelNotFoundError
3. (channel_id, external_id)
   是否已存在？            → uow.identities.find_by_channel_and_external_id()
                              → 已存在抛 DuplicateIdentityError（DB 另有 UNIQUE 兜底）

**Orchestration，不是职责污染**：本 Service 只做存在性校验 + 写入编排，
绝不复制 User / Channel 的创建逻辑（不出现 User(...) / Channel(...) 构造，
不调用 uow.users.create / uow.channels.create）。

**Identity Management ≠ Identity Resolution**：
本 Slice 做的是「external identity → User」的管理绑定；按
channel_id + external_id 反查 Identity → User 的解析属于运行时，分开。

**不做自动 Identity Linking**：WeCom/zhangsan 与 Feishu/zhangsan 默认是两个
Identity（唯一键是 (channel_id, external_id)，不是 external_id）；把它们判定
为同一人需要未来的 Linking Slice 显式执行。

边界（AST 守护）：不 import SQLAlchemy / FastAPI / api / schemas / adapters /
app.persistence；只依赖 app.domain、app.repositories（端口）、app.application。
"""

from collections.abc import Callable
from dataclasses import dataclass, replace

from app.application.errors import (
    ChannelNotFoundError,
    DuplicateIdentityError,
    IdentityNotFoundError,
    InvalidIdentityInputError,
    UserNotFoundError,
)
from app.domain.identity import Identity
from app.repositories.unit_of_work import UnitOfWork


@dataclass(frozen=True)
class CreateIdentity:
    """**Create managed binding** 的输入契约（应用层校验点，非 Pydantic/API schema）。

    语义（Gate 3 §D 冻结）：本用例原子地完成两件事——

        A. 创建 External Identity（这条外部身份存在）
        B. 绑定到 ACP User（这条外部身份属于哪个 ACP Account）

    Gate 2.6 D7 要求 A / B 是**两个不同语义**；本 Package 的拆分落在
    模型（Identity.user_id 可空）/ 运行时（Resolution 不依赖 User）/ 文档层，
    **不**新增「创建未绑定 Identity」与「Bind / Unbind」的独立 API——
    **语义拆分 ≠ API 数量必须增加**。

    (user_id, channel_id, external_id) 构成绑定骨架；
    display_name / metadata 是档案字段，可在 update 时修改。
    """

    user_id: int
    channel_id: int
    external_id: str
    display_name: str | None = None
    metadata: dict | None = None


@dataclass(frozen=True)
class UpdateIdentity:
    """更新 Identity 的输入契约（只更新档案字段；12.3 冻结）。

    None / omitted = 不修改：PATCH {} → noop，与 11.2 Agent / 11.8 Channel /
    11.11 Connection 的管理面 PATCH 语义一致；metadata 提供 = 整段替换。
    绑定骨架（user_id / channel_id / external_id）不可通过本契约修改——
    改绑定 = Identity Linking，属于后续 Slice（12.3 绝不引入）。
    """

    display_name: str | None = None
    status: str | None = None
    metadata: dict | None = None


def _require_positive_id(value: int, field: str) -> None:
    if not isinstance(value, int) or value <= 0:
        raise InvalidIdentityInputError(f"{field} 必须是正整数")


def _require_non_blank(value: str | None, field: str) -> None:
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise InvalidIdentityInputError(f"{field} 不能为空")


def _require_dict_metadata(value: dict | None) -> None:
    """metadata 必须是 JSON 对象（dict）——对齐 Agent/Channel 的 config 校验（12.3）。"""
    if value is not None and not isinstance(value, dict):
        raise InvalidIdentityInputError("metadata 必须是 JSON 对象（dict）")


class IdentityApplicationService:
    """Identity 管理用例的编排者（依赖 UoW Port，而非具体 SQLAlchemy 实现）。"""

    def __init__(self, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    # ------------------------------------------------------------ Create

    def create_identity(self, input: CreateIdentity) -> Identity:
        """创建 Identity：User/Channel 存在性校验 + 唯一键预检 → 写入 → commit。

        只读校验端点（get_by_id），不复制任何 User/Channel 创建逻辑。
        """
        _require_positive_id(input.user_id, "user_id")
        _require_positive_id(input.channel_id, "channel_id")
        _require_non_blank(input.external_id, "external_id")
        _require_non_blank(input.display_name, "display_name")
        _require_dict_metadata(input.metadata)

        identity = Identity(
            user_id=input.user_id,
            channel_id=input.channel_id,
            external_id=input.external_id,
            display_name=input.display_name,
            metadata=input.metadata,
        )
        with self._uow_factory() as uow:
            if uow.users.get_by_id(input.user_id) is None:
                raise UserNotFoundError(input.user_id)
            if uow.channels.get_by_id(input.channel_id) is None:
                raise ChannelNotFoundError(input.channel_id)
            if uow.identities.find_by_channel_and_external_id(
                input.channel_id, input.external_id
            ) is not None:
                raise DuplicateIdentityError(input.channel_id, input.external_id)
            created = uow.identities.create(identity)
            uow.commit()
            return created

    # ------------------------------------------------------------ Get

    def get_identity(self, identity_id: int) -> Identity | None:
        """按 id 读取 Identity；不存在返回 None。"""
        with self._uow_factory() as uow:
            return uow.identities.get_by_id(identity_id)

    # ------------------------------------------------------------ List

    def list_identities(self) -> list[Identity]:
        """12.4：List 用例——返回全部 Identity（管理面陈列 / 绑定核对用）。

        MVP 无分页/过滤（与 11.3 list_agents / 11.8 list_channels 同一裁决）；
        同一 external_id 跨渠道的多条 Identity 全部如实陈列（不合并）。
        """
        with self._uow_factory() as uow:
            return uow.identities.list_identities()

    # ------------------------------------------------------------ Update

    def update_identity(self, identity_id: int, change: UpdateIdentity) -> Identity:
        """部分更新 Identity 档案字段（12.3 冻结：None / omitted = 不修改）。

        PATCH {}（全 None）→ noop：字段全部保留，仅完成存在性确认后的原值
        写回（与 Agent/Channel/Connection 管理面 PATCH 同型）；
        metadata 提供 = 整段替换；非法输入抛 InvalidIdentityInputError。
        绑定骨架不可改（改绑定 = Linking，后续 Slice，本步绝不引入）。
        """
        _require_non_blank(change.display_name, "display_name")
        _require_non_blank(change.status, "status")
        _require_dict_metadata(change.metadata)

        with self._uow_factory() as uow:
            current = uow.identities.get_by_id(identity_id)
            if current is None:
                raise IdentityNotFoundError(identity_id)
            updated = self._apply_change(current, change)
            result = uow.identities.update(updated)
            uow.commit()
            return result

    @staticmethod
    def _apply_change(current: Identity, change: UpdateIdentity) -> Identity:
        return replace(
            current,
            display_name=(
                change.display_name
                if change.display_name is not None
                else current.display_name
            ),
            status=change.status if change.status is not None else current.status,
            metadata=(
                change.metadata if change.metadata is not None else current.metadata
            ),
        )
