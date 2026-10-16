"""ChannelRepository 端口（Port）。

只描述业务需要的查询/写入能力，不含实现，不 import SQLAlchemy / persistence。
实现见 app/persistence/repositories/channel.py（SqlAlchemyChannelRepository）。

方法按 Channel 管理语义命名（6.3 起加入 update / find_by_type_and_name），
不做 GenericRepository[T] 万能 CRUD。
"""

from typing import Protocol, runtime_checkable

from app.domain.channel import Channel


@runtime_checkable
class ChannelRepository(Protocol):
    """渠道仓储：按业务语义命名的方法，不是 Generic CRUD。"""

    def get_by_id(self, channel_id: int) -> Channel | None:
        """按主键取渠道；不存在返回 None。"""
        ...

    def find_tenant_id_by_id(self, channel_id: int) -> int | None:
        """bootstrap 路由投影（Cloud-0.1.3.1）：按主键读渠道所属 `tenant_id`。

        **刻意不做 tenant 过滤**——它是「URL routing hint（channel_id）→ 候选对象」
        的定位读，发生在 tenant 解析**之前**（B §10/§12：TenantResolver 在 UoW 构造
        之前、在已 SET LOCAL 的会话之外解析）。

        它**只用于**入站路由定位；真正的信任来自「已校验（WeCom 验签）Channel」——
        tenant 取自该已认证对象，绝不取自 URL。返回 None = 渠道不存在。

        纪律（B §13.2）：tenant_id **不进入 Domain**，故经本投影单独返回，
        不污染 `Channel` 域实体。
        """
        ...

    def create(self, channel: Channel) -> Channel:
        """写入渠道并返回带 id 的新实例。"""
        ...

    def update(self, channel: Channel) -> Channel:
        """把 Channel 上的字段覆写到已存在的行（id 必须存在）；返回更新后的实例。"""
        ...

    def find_by_type_and_name(self, channel_type: str, name: str) -> Channel | None:
        """按 (type, name) 精确查找（同平台去重守卫用）；不存在返回 None。

        渠道重复性是平台限定的：type 相同才比较 name；
        不同平台（wecom / feishu / ...）的同名渠道可以共存。
        """
        ...

    def list_channels(self) -> list[Channel]:
        """11.8：List 用例——返回全部 Channel（管理面陈列 / Connection 下拉数据源用）。"""
        ...
