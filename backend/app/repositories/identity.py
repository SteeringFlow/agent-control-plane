"""IdentityRepository 端口（Port）。

只描述业务需要的查询/写入能力，不含实现，不 import SQLAlchemy / persistence。
实现见 app/persistence/repositories/identity.py（SqlAlchemyIdentityRepository）。

方法按 Identity 的**外部身份语义**命名：唯一键是 (channel_id, external_id)
（4.2 冻结的 UNIQUE），因此查找方法就是 find_by_channel_and_external_id，
而不是按 name / 按 external_id 全局查找——后者会隐含“跨渠道同一人”的错误假设。
"""

from typing import Protocol, runtime_checkable

from app.domain.identity import Identity


@runtime_checkable
class IdentityRepository(Protocol):
    """身份仓储：按业务语义命名的方法，不是 Generic CRUD。"""

    def get_by_id(self, identity_id: int) -> Identity | None:
        """按主键取 Identity；不存在返回 None。"""
        ...

    def create(self, identity: Identity) -> Identity:
        """写入 Identity 并返回带 id 的新实例（事务边界由 UoW 管理）。"""
        ...

    def update(self, identity: Identity) -> Identity:
        """把 Identity 上的字段覆写到已存在的行（id 必须存在）；返回更新后的实例。"""
        ...

    def find_by_channel_and_external_id(
        self, channel_id: int, external_id: str
    ) -> Identity | None:
        """按唯一键 (channel_id, external_id) 精确查找；不存在返回 None。

        这是外部身份的**平台限定**查找：不同 channel 上的相同 external_id
        是两个不同的 Identity（本 Slice 不做跨渠道身份合并）。
        """
        ...

    def find_or_create(self, identity: Identity) -> Identity:
        """**幂等**地确保该 (channel_id, external_id) 的 Identity 存在。

        M6.6-C3 / ADR-12 §11（Bootstrap 原子建立）：Bootstrap 事务内必须在**不破坏
        外层事务**的前提下处理并发插入冲突——因此冲突（UNIQUE 命中）由实现内部
        用 SAVEPOINT 消化并**回读复用**，绝不把 `IntegrityError` 冒泡成
        `UNEXPECTED_FAILURE`（C3-R §6.2 的 M1）。

        语义纪律：命中既有行 ⇒ **复用**（绝不产生第二行）；未命中 ⇒ 创建。
        本方法**不用于**管理面 Create（那里需要 409 重复语义，走 `create`）。
        """
        ...

    def list_identities(self) -> list[Identity]:
        """12.4：List 用例——返回全部 Identity（管理面陈列 / 绑定核对用）。"""
        ...
