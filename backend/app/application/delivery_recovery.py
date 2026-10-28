"""Delivery Recovery Driver（P2）—— 投递恢复职责（与 ProductionDriver **并列**）。

**为什么独立于 ProductionDriver**：执行驱动（12.9.4.16）的契约明写「Driver 不
拥有任何业务状态机」「绝不解释异常」；投递恢复是**业务策略**（白名单、租约、
重投上限），塞进既有 Driver 会破坏其冻结契约 ⇒ 复用其**骨架**（单线程 ticker /
DISCOVER→TRIGGER→RECOVER / 有界批次 / 单条异常隔离），不共用其**语义**。

    loop
      ├─ DISCOVER  source.find_undelivered(limit)        → 候选（纯读，白名单两类）
      ├─ TRIGGER   executor.recover_delivery(record)     → claim → 投递 → 落终态
      └─ RECOVER   source.reclaim_stale_delivery(thr)    → 租约过期 → unknown（不重发）

**铁律**（每条都有测试锁定）：

1. **绝不重跑 Agent**：恢复路径只用**已落库**的 `response_payload` + 入站快照
   重建出站消息，不经过 Runtime / Worker / Bootstrap。
2. **白名单只有两类**（由 Repository 的 CAS 结构保证，不靠约定）：
   - 首次投递：`protocol=1 ∧ delivery='pending' ∧ attempts=0`；
   - 失败重投：`protocol=1 ∧ delivery='failed' ∧ retryable ∧ attempts<max`。
   `unknown` / `delivering` / `delivered` 与历史行（`protocol=0`）**不在**候选内。
3. **`unknown` 永不自动重投**：租约过期一律转 `unknown`（转人工裁决），
   **绝不**退回 `pending`（旧 attempt 可能仍在外部调用中甚至已送达）。
4. **每条记录都必须先 claim**：claim 失败（rowcount≠1）⇒ 不投递。
5. **不承诺 exactly-once**：租约只保证「同一时刻一个在途 attempt」；旧 attempt
   可能仍在外部发送 ⇒ 重复消息风险**存在**（收敛依赖租约长度与渠道幂等行为，
   后者**待 P4 实测**）。
"""

import logging
import threading
from dataclasses import dataclass
from typing import Protocol

from app.domain.interaction_record import InteractionRecord

LOGGER = logging.getLogger(__name__)


class DeliveryRecoverySource(Protocol):
    """恢复驱动的 Core 入口（**只有** DISCOVER + RECOVER 两个方法）。

    刻意窄：驱动因此拿不到「直接改行」的能力——一切变更都必须经由执行器的
    原子 claim。
    """

    def find_undelivered(
        self, limit: int = 10, *, max_attempts: int,
    ) -> list[InteractionRecord]:
        """恢复候选（**纯读**：不 claim、不改行、不加锁）。"""
        ...

    def reclaim_stale_delivery(self, threshold_seconds: int) -> int:
        """回收租约过期的在途投递 → `unknown`（**绝不**退回 pending）。"""
        ...


class DeliveryRecoveryExecutor(Protocol):
    """单条恢复执行器（claim → 投递 → 落终态）。"""

    def recover_delivery(self, record: InteractionRecord) -> bool:
        """恢复一条待投递记录；返回 True = 本次真的发起了投递。"""
        ...


@dataclass(frozen=True)
class DeliveryRecoveryConfig:
    """恢复驱动的全部可调项——**只有节奏与批量**，没有任何业务语义。

    - `poll_interval_seconds`：轮次间隔；
    - `pickup_limit`：每轮 DISCOVER 上限（透传给 Core，不是翻页参数）；
    - `reclaim_every`：每多少轮做一次 RECOVER（cadence）；
    - `lease_seconds`：投递 attempt 的租约长度；
    - `max_attempts`：**总** attempt 次数上限（含首次投递）。
    """

    poll_interval_seconds: float = 10.0
    pickup_limit: int = 10
    reclaim_every: int = 30
    lease_seconds: int = 300
    max_attempts: int = 3


class DeliveryRecoveryDriver:
    """恢复驱动：单线程、单循环、无并发 ticker（与 ProductionDriver 同形）。"""

    def __init__(
        self,
        *,
        source: DeliveryRecoverySource,
        executor: DeliveryRecoveryExecutor,
        config: DeliveryRecoveryConfig,
    ) -> None:
        self._source = source
        self._executor = executor
        self._config = config
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._poll_count = 0

    def start(self) -> None:
        """启动后台 ticker（幂等：已在运行则忽略）。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self.run_forever, name="delivery-recovery-driver", daemon=True)
        self._thread.start()

    def stop(self, timeout: float | None = None) -> None:
        """请求停止并等待 ticker 退出（**不中断**在途投递）。"""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout)

    def run_forever(self) -> None:
        """ticker 主循环（异常**绝不**终止本循环）。"""
        while not self._stop.is_set():
            try:
                healthy = self.run_once()
            except Exception:                      # noqa: BLE001 兜底：循环永不死
                LOGGER.exception("delivery recovery iteration failed unexpectedly")
                healthy = False
            self._stop.wait(
                self._config.poll_interval_seconds if healthy
                else self._config.poll_interval_seconds * 2)

    def run_once(self) -> bool:
        """一轮：DISCOVER → TRIGGER，（到点）RECOVER。

        返回 True = 本轮基础设施健康。**绝不**据此解释任何投递结果。
        """
        healthy = True
        try:
            candidates = self._source.find_undelivered(
                self._config.pickup_limit, max_attempts=self._config.max_attempts)
        except Exception:                          # noqa: BLE001 基础设施错误
            LOGGER.exception("delivery recovery pickup failed")
            candidates = []
            healthy = False

        for record in candidates:
            try:
                self._executor.recover_delivery(record)
            except Exception:                      # noqa: BLE001 单条隔离
                LOGGER.exception(
                    "delivery recovery raised for interaction_id=%s", record.id)

        self._poll_count += 1
        if self._poll_count >= self._config.reclaim_every:
            try:
                self._source.reclaim_stale_delivery(self._config.lease_seconds)
            except Exception:                      # noqa: BLE001 基础设施错误
                LOGGER.exception("delivery recovery reclaim failed")
                healthy = False
            self._poll_count = 0
        return healthy
