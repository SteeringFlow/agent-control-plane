"""Production Driver（Step 12.9.4.16）—— 最小的 Core Driver / Trigger。

Driver 只回答「**什么时候**推动 Core」，**不拥有任何业务状态机**（12.9.4.14.3
冻结的最小形状）：

    loop
      ├─ DISCOVER   source.find_pending(limit)                  → 候选（纯读，无执行权）
      ├─ TRIGGER    worker.process(id)                          → 执行权由 Core 的 CAS 裁决
      └─ RECOVER    source.reclaim_stale_processing(threshold)  → 到点才做一次

**pickup 不等于执行权**：`find_pending` 是纯读，多个 Driver / 多个 Worker 读到同一
批候选是设计允许的——唯一性由 `Worker.process` 内部的 CAS claim 保证。Driver 因此
**不假设**自己「领到了」任何一条。

**无状态（12.9.4.16.8）**：Driver 对 Interaction 生命周期**零记忆**——不保存 id、
不计数成功/失败、不重试。它只记住两个**基础设施**事实：本轮是否健康、距上次回收
过了几轮。二者都是进程存活机制，**不是** Interaction retry policy（12.9.4.16.4）。

**异常纪律（12.9.4.16.5）**：
- `worker.process(id)` 的 True / False 一视同仁（只表示有没有拿到执行权），
  Driver **绝不**据此做任何业务动作——尤其不能写成
  「没跑到就重试」这类分支（那等于偷偷造第二套状态机）；
- `worker.process(id)` 抛异常时，Core 已把该次执行的事实落定，Driver 只记日志并
  继续下一条：**不改状态、不重新入队、不解释异常语义**。

**生命周期**：`start()` / `stop()` 是 ticker 的开关，挂到应用既有的 lifespan，
不另造一套生命周期机制。`stop` **不中断**在途执行——那是 Worker 的所有权；被打断
的执行由 Core 的 RECOVER 原语在后续轮次兜底。

刻意不做（全部后置）：Worker Pool / 并发 / 队列 / 重试策略 / 优先级 / 调度器 /
心跳 / lease / 指标框架 / 链路追踪 / supervisor / 分布式协调。
"""

import logging
import threading
from dataclasses import dataclass
from typing import Protocol

from app.domain.interaction_record import InteractionRecord

LOGGER = logging.getLogger(__name__)


class PendingSource(Protocol):
    """Driver 唯一的 Core 入口（DISCOVER + RECOVER）。

    刻意窄到只有两个方法：Driver 因此**拿不到**任何 claim / 落 outcome 的能力——
    不是靠自觉，是靠类型面（12.9.4.16.7）。
    """

    def find_pending(self, limit: int = 10) -> list[InteractionRecord]:
        """读取当前待执行的 Interaction（纯读：不 claim、不改行、不加锁）。"""
        ...

    def reclaim_stale_processing(self, threshold_seconds: int) -> int:
        """回收超时未落终态的执行中孤儿，退回待执行池（只回收，不执行）。"""
        ...


@dataclass(frozen=True)
class DriverConfig:
    """Driver 的全部可调项——**只有节奏与批量**，没有任何业务语义。

    - `poll_interval_seconds`：正常轮次之间的间隔；
    - `pickup_limit`：每轮 DISCOVER 的上限（透传给 Core，不是翻页参数）；
    - `reclaim_every`：每多少轮做一次 RECOVER（cadence，**不是** Core 的
      `threshold_seconds`——后者是「多老算孤儿」，两者严格分离，12.9.4.16.3）；
    - `stale_threshold_seconds`：原样透传给 Core 的孤儿判定阈值；
    - `backoff_interval_seconds`：基础设施出错后的退避间隔（进程存活机制）。
    """

    poll_interval_seconds: float = 2.0
    pickup_limit: int = 10
    reclaim_every: int = 30
    stale_threshold_seconds: int = 300
    backoff_interval_seconds: float = 5.0


class ProductionDriver:
    """最小 Driver：单线程、单循环、无并发的 ticker。"""

    def __init__(
        self,
        *,
        source: PendingSource,
        worker,
        config: DriverConfig,
    ) -> None:
        """依赖全部注入：Driver 不构造 Runtime、不构造 Worker、不开 UoW。

        - `source`：Core 的 DISCOVER + RECOVER 入口（窄 Protocol）；
        - `worker`：执行者（Driver 只调用它的 `process`，不碰 Runtime）；
        - `config`：节奏与批量。
        """
        self._source = source
        self._worker = worker
        self._config = config
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._poll_count = 0

    # ------------------------------------------------------------------ 生命周期

    def start(self) -> None:
        """启动后台 ticker（幂等：已在运行则忽略）。

        线程为 daemon：进程退出不会因 ticker 挂住（ticker 无状态、无需 flush）。
        """
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self.run_forever, name="production-driver", daemon=True)
        self._thread.start()

    def stop(self, timeout: float | None = None) -> None:
        """请求停止并等待 ticker 退出。

        **不中断**在途的 `worker.process`——执行所有权归 Worker（12.9.4.15.9）；
        被打断的执行会由 Core 的 RECOVER 原语在后续轮次兜底。因此本方法只保证
        「不再启动新的 iteration」，不保证在途执行已完成。
        """
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout)

    def run_forever(self) -> None:
        """ticker 主循环：一轮一轮跑到被 `stop`（异常**绝不**终止本循环）。

        间隔用 `Event.wait` 而不是 `time.sleep`——stop 可以在等待中立刻唤醒它，
        使 shutdown 不必等满一个间隔。
        """
        while not self._stop.is_set():
            try:
                healthy = self.run_once()
            except Exception:                      # noqa: BLE001  兜底：循环永不死
                LOGGER.exception("driver iteration failed unexpectedly")
                healthy = False
            self._stop.wait(
                self._config.poll_interval_seconds if healthy
                else self._config.backoff_interval_seconds)

    # ------------------------------------------------------------------ 单轮

    def run_once(self) -> bool:
        """执行一轮：DISCOVER → TRIGGER，（到点）RECOVER。

        返回 True = 本轮基础设施健康（可按正常节奏继续）；False = 发生过基础设施
        错误（调用方退避）。**该返回值与任何 Interaction 的结果无关**——Worker
        抛异常不算基础设施错误（那是 Core 已落定的执行事实）。
        """
        healthy = True
        try:
            pending = self._source.find_pending(self._config.pickup_limit)
        except Exception:                          # noqa: BLE001  基础设施错误
            LOGGER.exception("driver pickup failed")
            pending = []
            healthy = False

        for record in pending:
            try:
                self._worker.process(record.id)
            except Exception:                      # noqa: BLE001  单条隔离：保活循环
                # Core 已把该次执行的事实落定；Driver 只保证循环存活，
                # **绝不**解释异常、**绝不**据此改状态或重新入队。
                LOGGER.exception(
                    "worker raised for interaction_id=%s", record.id)

        self._poll_count += 1
        if self._poll_count >= self._config.reclaim_every:
            try:
                self._source.reclaim_stale_processing(
                    self._config.stale_threshold_seconds)
            except Exception:                      # noqa: BLE001  基础设施错误
                LOGGER.exception("driver reclaim failed")
                healthy = False
            self._poll_count = 0
        return healthy
