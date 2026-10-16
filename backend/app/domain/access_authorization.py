"""Access Authorization 结果契约（Implementation Package 2 / Step 2）。

一句话定位：**一次 Access Authorization 成功的唯一事实 =「这条 Conversation
允许访问这些 Connection」。**

    InboundInteraction.channel_id + external_conversation_id     ← 判定输入（ADR-3）
            ▼
    [AccessAuthorizationExecutor]
            ▼
    AccessAuthorization(granted_connection_ids)                  ← 许可集合

来源裁决（全部 FROZEN）：

- **ADR-3**：Authorization 的 MVP 作用域 = Conversation，判定是 `G1 → C1 = ALLOW`，
  不是 `User A/B/C → C1`；
- **ADR-6**：Discovery ≠ Authorization——授权默认 deny（缺授权记录 = 不允许），
  允许必须显式成立；因此判定输入不含任何「我们见过这条会话」的痕迹；
- **ADR-8**：Authorization 先于最终 Routing——本契约是 Router 的**输入**，
  不是 Router 的选择结果；
- **ADR-9**：V0 不做复杂权限系统——结果只有「允许的集合」，**没有**
  effect / 优先级 / 条件组合 / 继承。

输入边界（红线）：判定**不消费** Identity / User / content / Connection 状态——
`identity_id` / `user_id` / `external_identity_id` / `content` 一律不是输入
（ADR-1 / ADR-3）；Connection 的存在性、status、数量**完全归 Routing**
（本契约不裁决唯一性）。因此「未绑定 ACP User 的 External Identity」
（`Identity.user_id is None`，Gate 2.6 D1/D2）与已绑定身份**走完全相同的授权路径**
——这正是 Package 1 与 Package 2 的接缝。

失败没有结果对象：DENY 抛 `AccessAuthorizationDeniedError`
（`failure_stage=authorization` / `failure_code=UNAUTHORIZED` / `retryable=false`），
与其余阶段「成功有结果、失败只有 typed error」同一纪律。

**模块命名说明（为什么不是 `authorization.py`）**：与兄弟契约同构——
`identity_resolution` / `session_resolution` / `routing_result` 都带限定词，
本模块是**结果契约**而非权限体系。ADR-9 的 NON-GOAL 依旧成立：本层没有
Policy / Role / Permission / Principal / ACL / effect / 条件组合。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class AccessAuthorization:
    """一次 Access Authorization 的成功结果：该 Conversation 的**许可集合**。

    `granted_connection_ids` = 「这条会话被允许访问的 Connection」集合。
    它**不是** eligible 集合——`active(channel) ∩ granted` 由 Routing 计算
    （ADR-8：Router 只消费已授权候选，但**存在性与唯一性判定仍归 Router**）。
    """

    granted_connection_ids: frozenset[int]
