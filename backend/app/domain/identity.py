"""身份领域实体（Step 6.6 Identity Management Slice）。

Identity 不是普通管理对象，它是 Control Plane 的**Channel-scoped 外部身份**
（Implementation Package 1 / Gate 2.6 D1）：

    User ──0..1:N── Identity ──N:1── Channel
                       │
                   external_id

**ACP User 关联是可选的**（Gate 2.6 D1/D2）：Identity 自身即合法的运行时身份；
`user_id = None` 表示「**未与 ACP Account 建立管理绑定**」——**不是**
Anonymous / Guest / System User，也**不**表示「公共 / 任何人可用」。
禁止 sentinel user_id（0 / -1 / 保留 id 兜底）与任何自动 Linking。

核心唯一键 = (tenant_id, channel_id, external_id)，源起 4.2 / 5.3 冻结的
UNIQUE(channel_id, external_id)（Cloud-0.1.1-B 前导 tenant_id）。
推论（本 Slice 明确不跨越）：

- external_id **本身不是全局身份键**：WeCom/zhangsan 与 Feishu/zhangsan
  是两个不同的 Identity，默认可以属于不同的 User；
- 把两个外部身份判定为同一个人 = Identity Linking（未来 Slice，需管理员显式执行），
  本 Slice 不做任何自动跨渠道匹配（email / phone / fuzzy / merge 一律不做）；
- Identity Management（external identity → User 的管理绑定）
  ≠ Identity Resolution（运行时按 channel_id + external_id 反查 Identity/User）。

metadata 是 Identity 级附加信息（对应冻结列名 metadata；SQLAlchemy 侧因保留名
而叫 extra_metadata，转换由 Mapper 负责）。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Identity:
    """某 Channel 上的某个外部身份；**可选**关联到一个 ACP User。

    user_id = None ⇒ 未与 ACP Account 建立管理绑定（Gate 2.6 D2），
    不是匿名 / 访客 / 系统用户。
    """

    # None = 未绑定（Gate 2.6 D2）；不是 sentinel，不是匿名主体
    user_id: int | None
    channel_id: int
    external_id: str
    id: int | None = None
    display_name: str | None = None
    status: str = "active"
    metadata: dict | None = None
