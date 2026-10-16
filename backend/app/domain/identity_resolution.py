"""Identity Resolution Contract（Step 8.2）—— 冻结「问题、成功结果与 Fail-Closed 语义」。

本模块**不实现 Resolver**：IdentityResolver 属于 Runtime 组件，留到后续 Slice。
8.2 只冻结运行时解析侧的**契约**，让未来实现与消费方都朝同一个形状对齐。

唯一核心问题（8.2 的边界定义）：

    这个 Channel 上的 external_identity_id，到底对应 Control Plane 中哪个
    Identity / User？

    InboundInteraction
          │  channel_id / external_identity_id   ← 最小输入（恰好这两个字段）
          ▼
    [IdentityResolver]                            ← 未来 Runtime 组件，本模块不实现
          │
          ▼
    Identity
          │
          └── User（**可选**的管理关联；缺失不影响解析成立）

契约三原则：

1. **最小输入**：`channel_id` + `external_identity_id` 恰好来自 InboundInteraction，
   不新增任何"查询意图 / 过滤器"字段；
2. **复用 6.6 冻结的唯一键**：查找只调用既有 Port 方法
   `IdentityRepository.find_by_channel_and_external_id(channel_id, external_identity_id)`
   ——Runtime **不重新定义身份唯一性**（不发明新的 identity key）；
3. **Fail Closed**：未找到 Identity 时**绝不猜测一个 User**（无 auto linking /
   email / phone fuzzy / user matching），按 Runtime failure boundary 语义失败，
   失败码为 `IDENTITY_RESOLUTION_FAILED`。

**成功条件（Gate 2.6 D3 修订，Implementation Package 1 落地）**：

    Identity exists + Channel matches + External ID matches   →   RESOLVED
    User exists / does not exist                              →   不再决定成败

「未绑定 ACP User 的 External Identity」是**合法的一等运行时身份**
（Gate 2.6 D1/D2）：此时 `IdentityResolution.user is None`，
**不是** `IDENTITY_RESOLUTION_FAILED`。User 缺失**不触发任何创建**（ADR-5 继续有效）。

与 6.6 Identity Management 的边界（两者**不是同一个 Service**）：

    6.6 Identity Management：Admin → CreateIdentity → User + Channel + Identity
    8.2 Identity Resolution ：InboundInteraction → … → Identity → User

前者是**管理身份绑定**（application/identities.py，UoW 写 + commit）；
后者是**运行时解析身份**（未来 Runtime 组件，只读 + Fail Closed）。
把两者合成一个 IdentityService 会在管理侧引入运行时解析、在解析侧引入管理写入，
容易产生职责污染——因此它们分属不同边界，8.2 不加任何管理侧代码。
"""

from dataclasses import dataclass

from app.domain.identity import Identity
from app.domain.user import User

# Runtime failure boundary：external identity 无法解析时的失败码（Fail Closed）。
IDENTITY_RESOLUTION_FAILED = "IDENTITY_RESOLUTION_FAILED"


@dataclass(frozen=True)
class IdentityResolution:
    """一次成功的 Identity Resolution 结果：渠道外部身份 → 内部 Identity（+ 可选 User）。

    只有当 `find_by_channel_and_external_id` 命中时才可能产生本结果；
    未命中没有结果对象，只有 `IDENTITY_RESOLUTION_FAILED`（Fail Closed）。

    `user` 是**可选**的管理关联（Gate 2.6 D3）：为 None 表示该外部身份尚未绑定
    ACP Account——**不影响**解析成功，也**不代表**匿名 / 访客 / 系统用户。
    """

    identity: Identity
    user: User | None
