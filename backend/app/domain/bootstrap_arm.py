"""Bootstrap Arm 领域实体（M6.6-C3 / ADR-12）。

一句话定位：**Bootstrap Arm = 由已认证 Tenant Admin 显式发起的、短时、一次性、
精准作用域、可归属、可审计的「首次授权建立许可」**（Bootstrap Context）。

    Tenant Admin（已认证会话）
        ↓ 显式 Verify Connection
    Bootstrap Arm（bounded / one-shot / scoped / attributable / auditable）
        ↓ = Authorization Source（消息到达之前已成立）
    first trusted single-chat message   ← Trigger，不是 Source
        ↓
    Identity（user_id = NULL）+ 普通 Grant

它不是：

- Authorization Grant（授权仍由 `connection_conversations` 一行表达；本表**不参与**
  `find_granted_connection_ids`）；
- Identity / User / Connection / Session / Interaction；
- Discovery（`known-conversations` 与本表无任何关系）；
- Runtime Stage（不在 `RUNTIME_EXECUTION_ORDER` 内）。

**本实体不携带 `tenant_id`** —— 与 `ConnectionConversation` / `Identity` 同纪律：
租户归属由 Persistence 层以自身列承载，绝不通过 `Connection → Tenant` 推导。

**过期不建状态值**：`EXPIRED` 不是 `status` 的一员。过期 = `status == 'armed'
AND expires_at <= now()` 的逻辑判定（由 DB 时钟在 SQL 内裁决），不写行、不删行、
不做后台清扫（审计留存）。
"""

from dataclasses import dataclass
from datetime import datetime

STATUS_ARMED = "armed"
STATUS_CONSUMED = "consumed"
STATUS_REVOKED = "revoked"


@dataclass(frozen=True)
class BootstrapArm:
    """一条 Bootstrap 窗口（Connection 上的一次有界授权建立许可）。

    写入时**不携带** `expires_at` / `created_at` / `updated_at`（由 **DB** 生成，
    读回时回显）——与 `ConnectionConversation` 的时间戳纪律同形：app 绝不生成
    用于并发/过期判定的时间（12.9.4.11 §4 铁律）。
    """

    channel_id: int
    connection_id: int
    # ADR-12 §15 / O-4：Arm 只在存在真实已认证操作者时被创建 ⇒ 不可为空、
    # 无默认值、无 sentinel（anonymous / guest / system 一律禁止）。
    initiator_user_id: int
    id: int | None = None
    status: str = STATUS_ARMED
    # DB 生成（now() + interval）；写入不携带，读回展示。
    expires_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    consumed_at: datetime | None = None
    consumed_by_interaction_id: int | None = None
    # 两个外部裸键：+ (channel_id) ⇒ Identity 唯一键；
    #              + (channel_id, connection_id) ⇒ Grant 四元组唯一键。
    # 刻意不存 identity_id / grant_id：FK 会阻断授权撤销，裸 id 会因授权重建而漂移。
    consumed_external_identity_id: str | None = None
    consumed_external_conversation_id: str | None = None
