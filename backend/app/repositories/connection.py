"""ConnectionRepository 端口（Port）。

只描述业务需要的查询/写入能力，不含实现，不 import SQLAlchemy / persistence。
实现见 app/persistence/repositories/connection.py（SqlAlchemyConnectionRepository）。

方法按 Connection 的 Access Path 语义命名：唯一键是 (channel_id, agent_id)
（4.2 冻结的 UNIQUE），因此：
- 查找方法 = find_by_channel_and_agent（重复/存在性预检用）；
- **没有** find_by_name——name 只是显示名，不承担唯一性。

读路径分两轴（互不替代）：
- `find_active_by_channel`：**runtime 最小读**（9.4 Routing：单 Channel + active）；
- `list_connections`：**management 陈列读**（11.11 新增：无过滤、含 inactive，
  与 11.3 list_agents / 11.8 list_channels 同形；管理面列举不可复用 runtime 读）。
"""

from typing import Protocol, runtime_checkable

from app.domain.connection import Connection


@runtime_checkable
class ConnectionRepository(Protocol):
    """Connection 仓储：按业务语义命名的方法，不是 Generic CRUD。"""

    def get_by_id(self, connection_id: int) -> Connection | None:
        """按主键取 Connection；不存在返回 None。"""
        ...

    def create(self, connection: Connection) -> Connection:
        """写入 Connection 并返回带 id 的新实例（事务边界由 UoW 管理）。"""
        ...

    def update(self, connection: Connection) -> Connection:
        """把可变字段（name/status/config/credential_ref）覆写到已存在行。

        绑定（channel_id / agent_id）不可变——本方法不修改绑定。
        """
        ...

    def find_by_channel_and_agent(
        self, channel_id: int, agent_id: int
    ) -> Connection | None:
        """按唯一键 (channel_id, agent_id) 精确查找；不存在返回 None。

        这是 Access Path 的绑定预检：一个 Channel → 一个 Agent 只允许一条
        Connection（MVP 假设，见 7.1.4）。
        """
        ...

    def find_active_by_channel(self, channel_id: int) -> list[Connection]:
        """取该 Channel 上全部 **active** Connection；没有则返回空列表。

        9.4 Routing Execution 的**最小读路径**（8.4 冻结的选择依据 = Channel →
        唯一 active Connection → Agent）。刻意只按 channel_id + status=="active"
        过滤——不做 find_by_status / find_all / find_candidates /
        RoutingRule / RoutingPolicy / RoutingTable 查询；管理面列举由
        list_connections（11.11）承担，两者互不替代。
        """
        ...

    def list_connections(self) -> list[Connection]:
        """11.11：management 陈列读——返回全部 Connection（含 inactive），按 id 排序。

        与 find_active_by_channel 互不替代：那是 9.4 Routing 的 runtime 最小读
        （单 Channel + active 过滤）；本方法无过滤（管理面列举，与 11.3
        list_agents / 11.8 list_channels 同形，MVP 无分页）。
        """
        ...
