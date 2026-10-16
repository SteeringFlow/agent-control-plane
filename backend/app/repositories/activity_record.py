"""ActivityRecordRepository 端口（Port，Step 10.4.2）。

Append-only Evidence 的写入能力：只有 `append`，没有 update / delete——
历史事实不允许被改写（与 activities 表 append-only schema 互为表里）。

13.1 起：`list_by_interaction` 是本 Port 的第一个读方法（10.7.1 §Q3 记录的
「数据层完整、读取层空白」缺口由 Observability Read Vertical Slice 关闭）；
读路径同样 append-only——绝不提供 update / delete。
"""

from typing import Protocol, runtime_checkable

from app.domain.activity_record import ActivityRecord


@runtime_checkable
class ActivityRecordRepository(Protocol):
    """Activity 记录仓储：append-only 写 + 只读时间线。"""

    def append(self, record: ActivityRecord) -> ActivityRecord:
        """追加一条 Evidence 并返回带 id 的新实例（事务边界归 UoW）。"""
        ...

    def list_by_interaction(self, interaction_id: int) -> list[ActivityRecord]:
        """某次 Interaction 的全部 Evidence（13.1 Observability 读面，**纯读**）。

        - **纯读**：不修改任何行（append-only 的读路径同样不改写历史）；
        - **顺序**：`created_at` 升序、`created_at` 相同时以 `id` 升序——
          Evidence 的发生序（10.7.1 §B5：append-only + created_at 单调可排；
          id 是写入序，作为秒级时间戳的稳定 tiebreak）；
        - **不重新解释 Evidence**：原样返回 type / stage / status / message /
          metadata，不做任何归并、过滤或「故事化」改写——时间线的解读属于
          消费方（管理员 / UI），不是仓储的职责。
        """
        ...
