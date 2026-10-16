"""Interaction Observability Read Service（Step 13.1，**纯读**）。

10.7.1 §Q4/Q10 记录的触发条件在本步达成：Observability Read Vertical Slice
出现了真实消费者（管理员 / UI 需要「不直接查数据库，还原一次 Interaction 的
执行故事」）。本服务把已冻结的 Interaction / Activity 数据接到读用例上。

职责边界（刻意写死）：

- **只读**：三个用例全部是纯读——不 create / 不 claim / 不 record_outcome /
  不 reclaim、不 append 任何 Evidence、不 commit（读用例没有要提交的变更，
  UoW 退出即关 Session）；对 Runtime / Async Core / Driver 零感知、零改动；
- **不重新解释状态**：原生双状态（processing × delivery）、失败三元组、
  `processing_started_at`（attempt fence 事实）、DB 时间戳全部原样透出——
  「Agent 成功 × 投递失败」这类 4.2 冻结的合法组合绝不在读路径合成单一
  status；Evidence 时间线不做归并 / 过滤 / 故事化改写（解读属于消费方）；
- **不做** Read Model / Projection / CQRS / 聚合统计 / 分页游标 / 任意条件
  过滤（10.7.1 §Q5 同一裁决：bounded limit 足够，多维查询等真实用例出现
  再扩）；
- **不做** 写端点、重试、replay、状态修复——Observability 与执行语义
  （Worker / phase API）永远分离。

数据能力完全来自既有 Port：`uow.interactions`（get_by_id / list_recent）与
`uow.activities`（list_by_interaction）。本模块不 import SQLAlchemy / FastAPI /
app.persistence（AST 守护与既有 Application 层同一规则）。
"""

from collections.abc import Callable

from app.domain.activity_record import ActivityRecord
from app.domain.interaction_record import InteractionRecord
from app.repositories.unit_of_work import UnitOfWork

DEFAULT_LIST_LIMIT = 50


class InteractionObservabilityService:
    """Interaction / Activity 证据的只读用例（依赖 UoW Port）。"""

    def __init__(self, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    # ------------------------------------------------------------ List

    def list_interactions(self, limit: int = DEFAULT_LIST_LIMIT) -> list[InteractionRecord]:
        """最近 N 条 Interaction（最新在前；纯读陈列面，不含 payload）。"""
        with self._uow_factory() as uow:
            return uow.interactions.list_recent(limit)

    # ------------------------------------------------------------ Get

    def get_interaction(self, interaction_id: int) -> InteractionRecord | None:
        """按 id 读取单条 Interaction；不存在返回 None（API 层映射 404）。"""
        with self._uow_factory() as uow:
            return uow.interactions.get_by_id(interaction_id)

    # ------------------------------------------------------------ Timeline

    def list_activities(self, interaction_id: int) -> list[ActivityRecord] | None:
        """某次 Interaction 的 Evidence 时间线（发生序）。

        返回 `None` = 该 Interaction 不存在（与「存在但尚无 Evidence」区分，
        API 层据此映射 404 / 空列表——不隐藏存在性也不虚构时间线）。
        """
        with self._uow_factory() as uow:
            if uow.interactions.get_by_id(interaction_id) is None:
                return None
            return uow.activities.list_by_interaction(interaction_id)
