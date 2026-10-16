"""Bootstrap Coordinator（M6.6-C3 / ADR-12）—— 执行边界上的独立 Bootstrap 组件。

定位：**管线的前置事实供给**，不是管线的一段。

    Trust Gate（CP-2）
        ↓
    Worker：claim（CAS）
        ↓
    **Bootstrap（本组件）**          ← 唯一新增的运行时授权建立点
        ↓
    normal 7-stage pipeline（**逐字节不变**）

为什么在这里：三条 Trigger（HTTP / WS / Driver）**全部汇聚到 `Worker.process`**
（`app/main.py:384-385 / :395 / :354-357`），因此本组件只需在 Worker 内注入一次
即覆盖全部入口 —— 不出现 transport-specific divergence（ADR-12 §9 / CP3-F §1.2）。

**它只做一件事**：当且仅当存在一条**有效的** Bootstrap Arm 时，在**同一个短事务**里
原子地建立 Identity + Grant 并消费该 Arm；否则 **no-op**。

明确不做（红线）：

- ❌ 创建 ACP User（Identity 恒 `user_id = NULL`，绝不等于 initiator）；
- ❌ bypass Authorization（授权阶段仍执行、仍可 DENY）；
- ❌ 参与 Routing（connection 由 Arm 指定，本组件不选择）；
- ❌ 新增 Runtime stage / failure_code（失败一律回落既有 Fail Closed）；
- ❌ 支持群聊（V0：`chat_type != single` ⇒ 不消费、不创建、不报错）。
"""

import logging
from collections.abc import Callable

from app.domain.connection_conversation import ConnectionConversation
from app.domain.identity import Identity
from app.domain.inbound_interaction import (
    CHAT_TYPE_METADATA_KEY,
    CHAT_TYPE_SINGLE,
    InboundInteraction,
)
from app.repositories.unit_of_work import UnitOfWork

LOGGER = logging.getLogger(__name__)

#: 无有效 Arm / 已过期 / 已消费 / 群聊 / scope 不符 ⇒ **不消费、不创建**。
NOT_APPLICABLE = "not_applicable"
#: Identity 与 Grant 都已存在 ⇒ 无需 Bootstrap（**不消费** Arm）。
ALREADY = "already"
#: 本组件完成了「消费 Arm + 建立授权事实」的原子事务。
APPLIED = "applied"


class BootstrapCoordinator:
    """Bootstrap 执行组件：只对**有效 Arm** 兑现一次授权建立（Fail Closed）。"""

    def __init__(self, *, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    def ensure(self, inbound: InboundInteraction, interaction_id: int) -> str:
        """必要时建立 Bootstrap 事实；不满足即 no-op。

        **绝不抛出**：本组件的失败不得成为新的失败码来源（CP3-F §20）。
        事务内异常 ⇒ 回滚 + 记日志 ⇒ 管线照常 ⇒ 既有 `IDENTITY_RESOLUTION_FAILED`。
        """
        try:
            return self._ensure(inbound, interaction_id)
        except Exception:  # noqa: BLE001  回落既有 Fail Closed，不新造失败语义
            LOGGER.exception(
                "bootstrap failed, falling back to existing fail-closed: "
                "channel_id=%s interaction_id=%s",
                inbound.channel_id, interaction_id,
            )
            return NOT_APPLICABLE

    # ------------------------------------------------------------------ 内部

    def _ensure(self, inbound: InboundInteraction, interaction_id: int) -> str:
        # ① 群聊 / 未知会话形态 ⇒ 不消费（ADR-12 §14；Core 不做启发式推断）
        if not self._is_single_chat(inbound):
            return NOT_APPLICABLE

        # ② 有效 Arm 存在？（过期判定在 SQL 内由 DB 时钟给出）
        arm = self._active_arm(inbound.channel_id)
        if arm is None or arm.id is None:
            return NOT_APPLICABLE

        # ③ 授权事实已存在 ⇒ 不消费（避免"消费了却什么都没建立"）
        if self._established(inbound, arm.connection_id):
            return ALREADY

        # ④ CAS 消费 + 建立授权事实（同一事务）
        if self._attempt(inbound, arm.id, arm.connection_id, interaction_id):
            return APPLIED

        # ⑤ CAS 输家：**回滚并关闭事务后**开新事务回读赢家成果
        #    （READ COMMITTED 下同一事务读不到赢家未提交的写入）
        if self._established(inbound, arm.connection_id):
            return ALREADY

        # ⑥ 赢家可能已回滚 ⇒ 重试 CAS 一次；仍不适用则回落既有 Fail Closed
        if self._attempt(inbound, arm.id, arm.connection_id, interaction_id):
            return APPLIED
        return NOT_APPLICABLE

    def _attempt(
        self,
        inbound: InboundInteraction,
        arm_id: int,
        connection_id: int,
        interaction_id: int,
    ) -> bool:
        """一次原子尝试：CAS 消费 + find-or-create Identity + find-or-create Grant。

        三者**同一短事务**：任一步失败 ⇒ 回滚 ⇒ Arm 回到 `armed`（可 retry），
        ⇒「已消费但无授权」在状态空间上不可达（ADR-12 §10）。
        """
        with self._uow_factory() as uow:
            consumed = uow.bootstrap_arms.consume(
                arm_id,
                interaction_id=interaction_id,
                external_identity_id=inbound.external_identity_id,
                external_conversation_id=inbound.external_conversation_id,
            )
            if not consumed:
                # 显式回滚（未取得消费权 ⇒ 绝不产生任何授权写入）。
                uow.rollback()
                return False
            # Identity：**External Identity**，user_id 恒 NULL（ADR-12 §6）。
            uow.identities.find_or_create(Identity(
                user_id=None,
                channel_id=inbound.channel_id,
                external_id=inbound.external_identity_id,
            ))
            # Grant：**普通** Grant（同一张表、同一唯一键、无特殊类型）。
            uow.connection_conversations.find_or_create(ConnectionConversation(
                channel_id=inbound.channel_id,
                external_conversation_id=inbound.external_conversation_id,
                connection_id=connection_id,
            ))
            uow.commit()
        return True

    def _active_arm(self, channel_id: int):
        with self._uow_factory() as uow:
            return uow.bootstrap_arms.find_active_by_channel(channel_id)

    def _established(
        self, inbound: InboundInteraction, connection_id: int
    ) -> bool:
        """Identity 与 Grant 是否都已存在（命中即无需 Bootstrap）。"""
        with self._uow_factory() as uow:
            identity = uow.identities.find_by_channel_and_external_id(
                inbound.channel_id, inbound.external_identity_id)
            grant = uow.connection_conversations.find_by_binding(
                inbound.channel_id,
                inbound.external_conversation_id,
                connection_id,
            )
        return identity is not None and grant is not None

    @staticmethod
    def _is_single_chat(inbound: InboundInteraction) -> bool:
        """会话形态 = 单聊？（由 Adapter 显式声明；缺失 / 其它 ⇒ 不允许）。"""
        metadata = inbound.metadata
        if not isinstance(metadata, dict):
            return False
        return metadata.get(CHAT_TYPE_METADATA_KEY) == CHAT_TYPE_SINGLE
