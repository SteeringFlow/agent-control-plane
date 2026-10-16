"""Runtime Execution Result Contract（Step 10.8.2）—— 冻结「一次成功 Runtime 执行的
最小事实面」。本步只定契约，不接线、不实现。

来源与裁决：docs/step-10.8.1-runtime-result-boundary-audit.md（10.4~10.7 被探针
证明的观察缺口 → 恰好两个新事实 + 一个搭车字段）。命名刻意避开 9.1 拒绝的
"RuntimeResult"（巨型 DTO 语境）——本契约是其反面：**恰好三个字段的极薄
execution result**（10.8.1 的 A 裁决）。

    RuntimeExecutionResult
    ├── session_mode     REUSE_ACTIVE / CREATE_NEW（复用 8.3 常量，单一事实源）
    ├── session_id       本次执行实际使用的 CP Session（REUSE 复用的 / CREATE 新建的）
    └── agent_response   Dispatch 产出的归一化响应（直接复用 8.5 frozen AgentResponse）

八项契约裁决（本步定死；机器化锁定见
tests/test_step10_8_2_runtime_execution_result_contract.py）：

1. `session_mode` **复用既有常量，不新建 Enum**——项目状态语义全部是 str 常量
   （processing_status / delivery_status / SessionResolution.mode），Enum 会引入
   第二种表达风格与第二份常量源；
2. `session_id` 用 **int**——项目无 identifier wrapper 类型（identity_id /
   connection_id / control_plane_session_id 同为 int）；
3. `agent_response` **直接复用现有 frozen AgentResponse**——不复制、不包装、
   不重定义（10.7.2 裁决的响应快照列形状就是它的字段面）；
4. 三字段全部**只读**（frozen=True）——契约事实不可变；
5. 成功路径不变量：`session_id` 必填（运行时语义 > 0）；`session_mode` 恰好
   两值；`agent_response` **必非 None**——本契约只表达「成功执行」，失败走
   typed 异常通道（10.8.1 裁决 ④：异常通道本身就是执行事实通道）；
6. **agent_response is None 不允许**：无默认值、必填。注意与
   SessionResolution.existing_session_id 的「CREATE_NEW 时 None」不同——
   result 的 session 恒存在（本次执行必然实际使用了一个 CP Session）；
7. `session_id × session_mode` 组合约束：mode 只决定 session_id 的**解释**
   （REUSE = 复用既有 active session / CREATE = 本次新建），不产生额外组合
   状态；不存在「CREATE_NEW 且 session_id 为空」的合法组合（那是不可能状态
   ——决议失败走 SESSION_RESOLUTION_FAILED 异常，不产生结果对象）；
8. 字段面**恰好三个，禁止未经审计扩展**（机器化锁定：字段面相等断言）——
   任何新增字段（如逐段执行事实、时间戳）必须重开本契约审计；这是 9.1
   「不引入 RuntimeResult 巨型 DTO」在结果对象上的延续。

不变量的执行点：domain 值对象保持 8.x 纯净风格（plain frozen，不内嵌校验，
RoutingResult / SessionResolution / AgentResponse 同风格）；生产者（Coordinator）
在未来接线步 Fail Closed 校验（镜像 DispatchExecutor._valid_response 模式）。

边界（本步刻意不做）：

- **不接线**：不改 RuntimeExecutionCoordinator Protocol（execute() 仍 -> None）、
  不改 RuntimeCoordinator / PersistingCoordinator——接线 = Runtime Core 修改，
  须过 10.1.4 §10 Freeze Rule 六问（预备答案素材见 docs/step-10.8.1 §4）；
- 不携带 outcome / 逐段事实 / 时间戳（10.8.1 裁决 ④ 与 A/B 裁决）；
- 失败路径无结果对象（异常传播时 result 不存在）——失败路径的 session 归属
  仍走既有弱键读路径（MVP 单 Connection 下无歧义，开放点记录在 10.8.1 §4）。
"""

from dataclasses import dataclass

from app.domain.agent_response import AgentResponse
from app.domain.session_resolution import CREATE_NEW, REUSE_ACTIVE


@dataclass(frozen=True)
class RuntimeExecutionResult:
    """一次成功 Runtime 执行的最小事实面（10.8.1 A 裁决的契约化）。

    字段语义：

    - `session_mode`：REUSE_ACTIVE（复用既有 active session）/ CREATE_NEW
      （本次新建）——直接复用 8.3 常量（单一事实源），只决定 `session_id`
      的解释，不产生额外组合状态；
    - `session_id`：本次执行实际使用的 CP Session id（REUSE 复用的那条 /
      CREATE_NEW 物化的那条），运行时语义 > 0；
    - `agent_response`：Dispatch 产出的归一化响应（frozen AgentResponse），
      成功路径必非 None——它是响应快照列与未来
      sessions.external_session_id 绑定的唯一数据源（10.7.2 B 裁决）。
    """

    session_mode: str
    session_id: int
    agent_response: AgentResponse
