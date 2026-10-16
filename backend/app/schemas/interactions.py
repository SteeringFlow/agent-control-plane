"""Interaction Observability API 的 HTTP 契约（Step 13.1，**只读**）。

五个模型：

    InteractionSummaryResponse   GET /api/interactions          列表元素（不含 payload）
    InteractionDetailResponse    GET /api/interactions/{id}     详情 = Summary
                                 + request/response payload 快照 + activities 时间线
    InteractionListResponse      GET /api/interactions          响应体
    ActivityResponse             时间线元素（Evidence 原样）
    ActivityListResponse         GET /api/interactions/{id}/activities

边界（13.1 冻结）：

- **Schema 只做形状，不做业务判断**；字段面 == 领域记录的事实面；
- **不重新解释状态**：`processing_status × delivery_status` 原生双状态 +
  失败三元组（failure_stage / failure_code / retryable）+ `processing_started_at`
  （attempt fence 事实）原样透出——**绝不合成**单一 overall status / 派生
  结果字段；「Agent 成功 × 投递失败」这类合法组合在协议面就是两个独立字段；
- **Evidence 不改写**：ActivityResponse 是 activities 行的 1:1 投影
  （type / stage / status / message / metadata / created_at），不归并、
  不过滤、不「故事化」；
- **payload 只出现在详情**：列表是陈列面（轻量）；载荷快照在详情端点按需
  读取——`response_payload` 是 AgentResponse 原样快照（10.7.2 B 裁决），
  `request_payload` 是入站快照（12.9.4.3），两者都是只读事实；
- **无写形状**：没有 Create/Update 模型——Observability 面不存在写用例，
  状态变更只属于 Worker / phase API（12.9.4 冻结）。
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ActivityResponse(BaseModel):
    """一条 Evidence 的 1:1 投影（append-only，无更新语义）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int | None = None
    type: str
    status: str
    stage: str | None = None
    message: str | None = None
    metadata: dict | None = None
    interaction_id: int | None = None
    session_id: int | None = None
    created_at: datetime | None = None


class InteractionSummaryResponse(BaseModel):
    """Interaction 陈列面（列表元素；不含 payload 快照）。

    双状态 / 失败三元组 / attempt fence 时间戳 / DB 审计时间戳原样透出。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int | None = None
    direction: str
    channel_id: int | None = None
    session_id: int | None = None
    external_interaction_id: str | None = None
    processing_status: str
    delivery_status: str
    failure_stage: str | None = None
    failure_code: str | None = None
    retryable: bool
    processing_started_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class InteractionDetailResponse(InteractionSummaryResponse):
    """详情面：Summary + 载荷快照 + Evidence 时间线（一次调用还原执行故事）。"""

    request_payload: dict | None = None
    response_payload: dict | None = None
    activities: list[ActivityResponse] = []


class InteractionListResponse(BaseModel):
    """GET /api/interactions 响应体（bounded limit，非翻页——13.1 冻结）。"""

    items: list[InteractionSummaryResponse]
    total: int
    limit: int


class ActivityListResponse(BaseModel):
    """GET /api/interactions/{id}/activities 响应体（发生序时间线）。"""

    items: list[ActivityResponse]
    total: int
