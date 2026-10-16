"""ConnectionConversationRepository 端口（Port，Implementation Package 2 / Step 1）。

只描述业务需要的查询/写入能力，不含实现，不 import SQLAlchemy / persistence。
实现见 app/persistence/repositories/connection_conversation.py。

方法面**刻意极小**（3 个），每个都有明确消费者：

    create              → 写入一条授权（唯一写入路径；Step 3 的 Create Grant API 与
                          Step 1 的持久化测试消费）
    get_by_id            → 按主键读回（DELETE 的定位前置 / 诊断断言）
    find_by_binding      → 按**授权作用域三元组**精确查找（四元组 UNIQUE 的应用侧
                          对应物；Step 3 的重复授权 409 预检消费）

刻意**不做**：

- ❌ `update`：授权行不可变（改授权 = 删除 + 新建）；
- ❌ `delete`：删除的消费者是 Step 3 的 Delete Grant API，届时再加
  （不为无消费者的操作预先扩张 Port）；
- ❌ `list_*` / 授权判定查询（`find_granted_connection_ids(...)`）：消费者是
  Step 2 的 Authorization 执行器与 Step 3 的 List Grants，**到那时再加**——
  本 Step 只落持久化边界，不提前实现运行时语义；
- ❌ 任何 effect / priority / 条件过滤：ADR-9 NON-GOAL，本表是**纯 allow 关系**，
  deny 由**缺行**表达（ADR-6）。
"""

from typing import Protocol, runtime_checkable

from app.domain.connection_conversation import ConnectionConversation


@runtime_checkable
class ConnectionConversationRepository(Protocol):
    """授权状态仓储：按业务语义命名的方法，不是 Generic CRUD。"""

    def create(self, record: ConnectionConversation) -> ConnectionConversation:
        """写入一条授权并返回带 id 的新实例（事务边界由 UoW 管理）。"""
        ...

    def get_by_id(
        self, connection_conversation_id: int
    ) -> ConnectionConversation | None:
        """按主键取授权行；不存在返回 None。"""
        ...

    def find_by_binding(
        self,
        channel_id: int,
        external_conversation_id: str,
        connection_id: int,
    ) -> ConnectionConversation | None:
        """按 (channel_id, external_conversation_id, connection_id) 精确查找。

        这是四元组 UNIQUE（前导 tenant_id 由租户作用域隐式限定）的应用侧对应物：
        同一 Conversation 对同一 Connection 至多一条授权，因此查找结果唯一。
        不存在返回 None。
        """
        ...

    def find_granted_connection_ids(
        self,
        channel_id: int,
        external_conversation_id: str,
    ) -> frozenset[int]:
        """该 Conversation（Channel 作用域内）被允许访问的 Connection id **集合**。

        这是 Step 2 的 Authorization 判定查询（消费者 = `AccessAuthorizationExecutor`）：

        - 返回 `frozenset`：许可集合**顺序无意义**，与「只有允许的集合、没有
          effect / 优先级」的 ADR-9 模型一致；
        - 无授权记录 → **空集**（不是 None）——调用方据此 DENY
          （ADR-6：缺授权记录 = 不允许）；
        - **只读 `connection_conversations`**：不查 Connection 的存在性 /
          status / 数量（那归 Routing；见 Package 2-B §B6）；
        - 不返回 Identity / User 相关信息（ADR-1 / ADR-3 红线）。
        """
        ...

    def list_by_connection(
        self,
        connection_id: int,
    ) -> list[ConnectionConversation]:
        """列出一条 Connection 的全部授权（B4.2 List：回显「它在哪工作」）。

        Grant Management API 的 List 读路径。返回**显式授权行**（按 id 稳定
        排序的陈列面）；不与 sessions / known-conversations 投影做任何连接
        ——「known」与「authorized」是两个概念，绝不在查询层混淆。
        """
        ...

    def find_or_create(
        self, record: ConnectionConversation
    ) -> ConnectionConversation:
        """**幂等**地确保该四元组授权存在（命中复用 / 未命中创建）。

        M6.6-C3 / ADR-12 §11（Bootstrap 原子建立）：并发冲突由实现内部以 SAVEPOINT
        消化并**回读复用**（C3-R §6.2 的 M2），绝不冒泡 `IntegrityError`
        ——那会把一条正常竞争打成 `UNEXPECTED_FAILURE`。

        创建的是**普通 Grant**：同一张表、同一四元组唯一键、同一语义
        （ADR-12 §7：无 temporary grant、无有效期、无特殊类型）。
        """
        ...

    def delete(self, connection_conversation_id: int) -> bool:
        """按行 id 删除授权（B4.2 Delete：撤销）。删除成功 → True；不存在 → False。

        行不可变（B4.2）——本 Port 没有 update；「改授权 = 删 + 建」由调用方
        组合。404 语义由 Application 层把 False 翻译成
        `ConnectionConversationNotFoundError`。
        """
        ...
