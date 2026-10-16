"""BootstrapArmRepository 端口（Port，M6.6-C3 / ADR-12）。

只描述业务需要的查询/写入能力，不含实现，不 import SQLAlchemy / persistence。
实现见 `app/persistence/repositories/bootstrap_arm.py`。

方法面**刻意极小**（4 个），每个都有明确消费者：

    arm                     → Verify Connection 端点（supersede 语义的唯一写入路径）
    find_active_by_channel  → Bootstrap 消费判定（入站只有 channel_id，Connection
                              由 Arm 自身给出——这是「Arm 精准作用域」的兑现方式）
    consume                 → Bootstrap 事务内的 CAS 消费（唯一赢家判定）
    get_by_id               → 读回 / 诊断断言

刻意**不做**：

- ❌ `update` / `delete`：Arm 只经 `arm`（supersede）与 `consume`（CAS）跃迁，
  不提供通用改删面（防止"延长窗口 / 复活已消费 Arm"）；
- ❌ `list_*`：V0 无管理陈列面（ADR-12 §16）；
- ❌ 任何「按过期状态过滤后删除」的清扫操作：过期是**逻辑判定**，不写、不删。
"""

from typing import Protocol, runtime_checkable

from app.domain.bootstrap_arm import BootstrapArm


@runtime_checkable
class BootstrapArmRepository(Protocol):
    """Bootstrap 窗口仓储：按业务语义命名的方法，不是 Generic CRUD。"""

    def arm(
        self, record: BootstrapArm, *, ttl_seconds: int
    ) -> BootstrapArm:
        """开启一条新 Arm：**同一事务内**把该 scope 上既有 `armed` 行置为 `revoked`
        并插入新 `armed` 行（supersede）。

        `ttl_seconds` 是**间隔**（不是绝对时刻）：`expires_at` 由 **DB 时钟**计算
        （12.9.4.11 §4 铁律 —— app 绝不生成用于过期判定的时间）。

        并发双击撞 O-1 的部分唯一索引时，实现内部**重试一次**（revoke → insert），
        使「至多一条 ARMED」在 DB 约束下仍能得到 supersede 语义而非 409。
        返回带 id 的新 Arm（事务边界由 UoW 管理）。
        """
        ...

    def find_active_by_channel(self, channel_id: int) -> BootstrapArm | None:
        """该 Channel 上当前**有效**的 Arm（`status='armed' AND expires_at > now()`）。

        过期判定在 **SQL 内**由 DB 时钟完成（与消费同判据 ⇒ 无 TOCTOU）。
        同一 scope 至多一条；跨 Connection 的多条理论上可能，按 id 升序取第一条
        （确定性，不做任何"选哪个"的业务推断）。不存在 / 已过期 → None。
        """
        ...

    def consume(
        self,
        arm_id: int,
        *,
        interaction_id: int,
        external_identity_id: str,
        external_conversation_id: str,
    ) -> bool:
        """CAS 原子消费：仅当该行仍是 `armed` 且**未过期**时才跃迁到 `consumed`。

        单条条件化 UPDATE 同时完成「判断 + 写入 + 记时」，并发下由**数据库**保证
        只有一个调用方拿到 `rowcount == 1`（与 `claim_processing` 同范式）。
        刻意不用「先读回再赋值」的非原子读-改-写，也不用 `SELECT ... FOR UPDATE`
        （SQLite 不支持，且会把锁持有到执行结束）。

        返回 True = 取得消费权；False = 未取得（已被消费 / 已过期 / 不存在 /
        跨租户）——**正常竞争结果，不是 DB 错误**。
        """
        ...

    def get_by_id(self, arm_id: int) -> BootstrapArm | None:
        """按主键读回；不存在（或跨租户不可见）返回 None。"""
        ...
