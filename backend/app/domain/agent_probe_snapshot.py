"""AgentProbeSnapshot —— ACP 可达性即时诊断快照（Cloud-0.1.2.2.2.1）。

一句话定位：**一次诊断探针的归一化结论**，与 `AgentResponse` 同构地作为
AgentAdapter 的**出站输出值对象**，只描述「此刻能不能连上」，不承载任何持久状态。

命名说明：刻意**不**以 `*Result` 结尾——`test_step10_8_3...test_c3` 冻结
「全 app 的 `*Result` 只有 RoutingResult + RuntimeExecutionResult」。
探针结论不是 Runtime 执行结果，而是「即时快照」（Gate 0.1.2.2.2 §2 用语），
故命名为 Snapshot，既不稀释该守卫，又语义精确。

冻结契约（Gate 0.1.2.2.2 §2/§7）：

- `reachable`：此刻探针是否成功连上 Agent runtime（Fail-Closed：任何失败 = False）；
- `detail`：不可达时的简短诊断原因（可达时为 None）；**不解释异常语义**，只给人看；
- `probe`：探针所走的 binding 标签（"tencent-acp-sse" / "minimal"），供 Cloud 用户
  知道「用哪条路径探的」。

它不是领域状态：**不持久化、不缓存、不带时间戳**（`checked_at` 由调用方在请求
时刻生成，见 `ConnectionHealthResponse`）。Connection 上没有 health 字段，本对象
只活在一次请求的栈上。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentProbeSnapshot:
    """Agent 可达性诊断快照（纯值对象，无副作用）。"""

    reachable: bool
    detail: str | None = None
    probe: str | None = None
