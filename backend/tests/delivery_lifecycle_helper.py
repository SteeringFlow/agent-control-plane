"""P1 共享测试辅助：把「执行」与「显式投递」合起来驱动。

P1 / C1 之后，`RuntimeCoordinator.execute()` 只覆盖前六段（identity →
authorization → routing → session_resolution → [session_creation] →
agent_dispatch），外部投递是**显式**的第 7 段（由应用层在「Agent 成功结果
已落库之后」发起）。既有测试大多只关心「一次完整交互」，故由本替身把两段
接起来——**不**改变任何被测语义（段序、失败传播、投递内容都不变）。
"""

from app.application.delivery import build_outbound_response


class DeliveringCoordinator:
    """驱动 Coordinator 的测试替身：execute() 之后**显式**发起投递。

    - 成功路径：execute() → 组装出站消息 → deliver()；
    - 失败路径：execute() 抛出即**不**投递（Agent 失败 → NO Delivery 的纪律
      保持不变）。
    """

    def __init__(self, coordinator) -> None:
        self._coordinator = coordinator

    def execute(self, inbound):
        result = self._coordinator.execute(inbound)
        self._coordinator.deliver(
            build_outbound_response(inbound, result.agent_response))
        return result

    def __getattr__(self, name):
        # 透传其余成员（如各测试自己挂在 coordinator 上的探针）。
        return getattr(self._coordinator, name)
