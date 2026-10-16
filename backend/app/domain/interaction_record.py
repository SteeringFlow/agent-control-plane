"""InteractionRecord —— Interaction ORM（4.2 冻结 schema）的记录域镜像（Step 10.4.2）。

命名裁决：**InteractionRecord ≠ 8.1 InboundInteraction**（Adapter 归一化输出，
不落库）——它是 `interactions` 行的领域表示（Evidence 记录模型）。`record`
后缀同时避开 6.7 `forbidden_next` 对 stem "interaction" 的防偷渡扫描被误触。

生命周期（10.4.2 冻结的最小状态机）：

    processing_status: PENDING → PROCESSING → SUCCEEDED | FAILED
    delivery_status:   PENDING → DELIVERED | FAILED

「Agent 已成功（processing=SUCCEEDED）→ 渠道投递失败（delivery=FAILED）」是
完全合法的状态组合（4.2 冻结，双状态绝不合成）。

`session_id` 可空（10.4.2 Schema 修正案）：Identity/Routing 等 Session 建立
之前的失败也要被记录，此时本记录尚无 Session 可挂；成功路径与到达 Dispatch
的失败路径由 Persistence Wrapper 回填（Interaction ↔ Session FK 正确）。

**10.5.2 Interaction Identity 物化**：`channel_id` 成为一等持久化字段
（原只在 `request_payload` JSON 里），与 `external_interaction_id` 共同构成
Interaction Identity：

    Identity   : channel_id + external_id
    Interaction: channel_id + external_interaction_id
    Connection : channel_id + agent_id

统一纪律：**所有外部世界产生的 ID，都必须在自己的 Channel Scope 内解释。**
创建时必须提供 channel_id（ORM NOT NULL 兜底）。
"""

import enum
from dataclasses import dataclass
from datetime import datetime

PROCESSING_PENDING = "pending"
PROCESSING_PROCESSING = "processing"
PROCESSING_SUCCEEDED = "succeeded"
PROCESSING_FAILED = "failed"

DELIVERY_PENDING = "pending"
DELIVERY_DELIVERED = "delivered"
DELIVERY_FAILED = "failed"


class OutcomeCommit(enum.Enum):
    """record_outcome 的提交结果（12.9.4.12 Attempt Fence）。

    - COMMITTED：条件 UPDATE 命中（rowcount == 1）——本次 attempt 仍持有
      执行权，终态落定；
    - REJECTED：条件未命中（rowcount == 0）——**执行权已失**：该行的
      `processing_started_at` 已不是本 attempt 持有的值（行被回收后由新
      attempt 重新取得执行权，或行已落终态）。这是正常竞争结果，**不是
      DB 错误，调用方静默忽略**（12.9.4.11 §3：绝不抛异常）。
    """

    COMMITTED = "committed"
    REJECTED = "rejected"


@dataclass(frozen=True)
class InteractionRecord:
    """一次 Runtime 交互的持久化记录（interactions 行）。"""

    direction: str = "inbound"
    channel_id: int | None = None  # Interaction Identity 的 scope（创建时必填）
    external_interaction_id: str | None = None
    request_payload: dict | None = None
    response_payload: dict | None = None
    processing_status: str = PROCESSING_PENDING
    delivery_status: str = DELIVERY_PENDING
    failure_stage: str | None = None
    failure_code: str | None = None
    retryable: bool = False
    session_id: int | None = None
    # 12.9.4.12：CAS 抢占时刻的 DB 时间戳（只有抢占写入、回收写 NULL、
    # outcome 只读不写）——Worker 读回持有后作为 attempt fence 的等式值。
    processing_started_at: datetime | None = None
    id: int | None = None
    # 13.1 Observability 读面：DB 生成的审计时间戳，只由 mapper to_domain
    # 读回（读路径回显）；to_orm 写路径不携带——created_at/updated_at 仍由
    # 数据库生成，写行为零变化。
    created_at: datetime | None = None
    updated_at: datetime | None = None
