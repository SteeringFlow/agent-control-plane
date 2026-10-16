"""Connection Conversation Management（Implementation Package 2 / Step 3）。

把 Step 1 的授权状态存储正式暴露为 **Grant Management API** 的业务裁决点
（Package 2-B §B4 冻结的三个必需动作 + 一个只读投影）：

    Create   把会话加进 Connection 的工作范围（default-deny 下唯一的授权途径）
    List     回显「这条 Connection 在哪些会话上工作」
    Delete   撤销（ADR-9「允许的集合」必须可增可减）
    ─────────
    known-conversations   只读投影：该 Channel 过去出现过哪些会话键（B4.5）

明确不做（B4.2 冻结）：

- ❌ **Update**：行不可变——改授权 = 删 + 建（与 Connection 绑定不可变同一思想）；
- ❌ bulk / import / export / priority / effect（ADR-9 NON-GOAL）；
- ❌ **从 known-conversations 自动创建 grant**：Discovery ≠ Authorization
  （ADR-6）——**known conversation ≠ authorized conversation**（本 Slice 红线）；
  投影只是让「会话可以被观察」，观察不产生授权状态。

依赖边界：只依赖 Repository / UoW Port——不 import SQLAlchemy / ORM /
persistence / adapters（与 identities / connections 同一纪律）。
"""

from collections.abc import Callable
from dataclasses import dataclass

from app.application.errors import (
    ChannelNotFoundError,
    ConnectionConversationNotFoundError,
    ConnectionNotFoundError,
    DuplicateConnectionConversationError,
    InvalidConnectionConversationInputError,
)
from app.domain.connection_conversation import ConnectionConversation
from app.repositories.unit_of_work import UnitOfWork


@dataclass(frozen=True)
class CreateConnectionConversation:
    """创建授权的输入契约（应用层校验点，非 Pydantic/API schema）。"""

    channel_id: int
    external_conversation_id: str
    connection_id: int


class ConnectionConversationApplicationService:
    """授权管理用例：Create / List / Delete + known-conversations 只读投影。"""

    def __init__(self, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    # ------------------------------------------------------------ Create

    def create_connection_conversation(
        self, command: CreateConnectionConversation
    ) -> ConnectionConversation:
        """为一条**已存在**的 Connection 指定「它在哪个会话上工作」（B4.2 Create）。

        校验顺序（状态码与 B4.4 一致，沿用既有先例不发明）：

            1. 输入形状（正整数键 / 非空白会话键）        → Invalid…（422）
            2. channel 存在性（tenant 作用域内）          → ChannelNotFoundError（404）
            3. connection 存在性（tenant 作用域内）       → ConnectionNotFoundError（404）
            4. connection 必须属于该 channel              → Invalid…（422）
            5. (channel, conversation, connection) 唯一   → Duplicate…（409）

        绝不「顺带」创建 Channel / Connection / 会话——授权只**引用**已存在的事实
        （跨租户的 channel/connection 在本租户作用域内等价于不存在 → 404）。
        """
        self._validate_keys(
            command.channel_id, command.connection_id,
            command.external_conversation_id)
        with self._uow_factory() as uow:
            if uow.channels.get_by_id(command.channel_id) is None:
                raise ChannelNotFoundError(command.channel_id)
            connection = uow.connections.get_by_id(command.connection_id)
            if connection is None:
                raise ConnectionNotFoundError(command.connection_id)
            if connection.channel_id != command.channel_id:
                raise InvalidConnectionConversationInputError(
                    f"connection {command.connection_id} 不属于 channel "
                    f"{command.channel_id}")
            if uow.connection_conversations.find_by_binding(
                command.channel_id,
                command.external_conversation_id,
                command.connection_id,
            ) is not None:
                raise DuplicateConnectionConversationError(
                    command.channel_id,
                    command.external_conversation_id,
                    command.connection_id,
                )
            grant = uow.connection_conversations.create(ConnectionConversation(
                channel_id=command.channel_id,
                external_conversation_id=command.external_conversation_id,
                connection_id=command.connection_id))
            uow.commit()
        return grant

    # ------------------------------------------------------------ List

    def list_connection_conversations(
        self, connection_id: int
    ) -> list[ConnectionConversation]:
        """回显一条 Connection 的全部授权（B4.2 List；陈列面，MVP 无分页）。

        connection 不存在（或跨租户不可见）→ `ConnectionNotFoundError`（404）。
        返回**显式授权行**——绝不把 known-conversations 投影混进来。
        """
        if not isinstance(connection_id, int) or connection_id <= 0:
            raise InvalidConnectionConversationInputError(
                f"connection_id 必须是正整数：{connection_id!r}")
        with self._uow_factory() as uow:
            if uow.connections.get_by_id(connection_id) is None:
                raise ConnectionNotFoundError(connection_id)
            return uow.connection_conversations.list_by_connection(connection_id)

    # ------------------------------------------------------------ Delete

    def delete_connection_conversation(
        self, connection_conversation_id: int
    ) -> None:
        """撤销一条授权（B4.2 Delete）。不存在 → `ConnectionConversationNotFoundError`（404）。"""
        if (not isinstance(connection_conversation_id, int)
                or connection_conversation_id <= 0):
            raise InvalidConnectionConversationInputError(
                f"connection_conversation_id 必须是正整数："
                f"{connection_conversation_id!r}")
        with self._uow_factory() as uow:
            if not uow.connection_conversations.delete(connection_conversation_id):
                raise ConnectionConversationNotFoundError(
                    connection_conversation_id)
            uow.commit()

    # -------------------------------------------- known-conversations（projection）

    def list_known_conversations(self, channel_id: int) -> list[str]:
        """B4.5 只读投影：该 Channel 过去出现过哪些会话键。

        channel 不存在（或跨租户不可见）→ `ChannelNotFoundError`（404）。

        **红线（本 Slice 核心）**：本方法只**读** sessions，绝不写授权；
        返回值**不是**授权输入——known conversation ≠ authorized conversation。
        会话要获得访问权只有一条路：`create_connection_conversation` 显式成立
        （ADR-6：Discovery 允许被观察，但观察不产生授权状态）。
        """
        if not isinstance(channel_id, int) or channel_id <= 0:
            raise InvalidConnectionConversationInputError(
                f"channel_id 必须是正整数：{channel_id!r}")
        with self._uow_factory() as uow:
            if uow.channels.get_by_id(channel_id) is None:
                raise ChannelNotFoundError(channel_id)
            return uow.sessions.list_known_conversation_ids(channel_id)

    # ------------------------------------------------------------ 内部

    @staticmethod
    def _validate_keys(channel_id: int, connection_id: int,
                       external_conversation_id: str) -> None:
        """输入形状校验（422 的唯一来源；存在性校验归各用例的 404）。"""
        if not isinstance(channel_id, int) or channel_id <= 0:
            raise InvalidConnectionConversationInputError(
                f"channel_id 必须是正整数：{channel_id!r}")
        if not isinstance(connection_id, int) or connection_id <= 0:
            raise InvalidConnectionConversationInputError(
                f"connection_id 必须是正整数：{connection_id!r}")
        if not isinstance(external_conversation_id, str) \
                or not external_conversation_id.strip():
            raise InvalidConnectionConversationInputError(
                f"external_conversation_id 必须是非空白字符串："
                f"{external_conversation_id!r}")
