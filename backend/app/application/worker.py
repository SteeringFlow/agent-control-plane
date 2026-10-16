"""Worker —— 单次执行 primitive（Step 12.9.4.5）。

异步执行路径的**唯一** Runtime 执行者。一次 `process` = 一条 interaction 的一次执行：

    获得 Interaction → claim → reconstruct → Runtime.execute → record outcome

    Worker.process(interaction_id)
        ├─ 读回 interaction（拿 request_payload 快照）
        ├─ claim_processing(id)        ← CAS；False → STOP，绝不执行（G1）
        ├─ get_by_id → 读回 processing_started_at（attempt fence 持有值，12.9.4.12）
        ├─ inbound_from_snapshot(...)  ← 重建入站（12.9.4.3）
        ├─ RuntimeCoordinator.execute(inbound)
        └─ record_success / record_failure(expected_started_at=...)
                                              ← REJECTED（ownership lost）
                                                静默忽略，不抛（12.9.4.11 §3）

**G1 安全阀**：claim 失败立即 return，Runtime.execute **一次都不被调用**。
这是「一次且仅一次执行」的唯一依据——无条件执行就是双执行（12.9.3.2 §3）。

**G2 所有权**：Worker 直接持有 RuntimeCoordinator 并是它的执行调用点；
`ChannelInboundService` 只依赖 `RuntimeExecutionCoordinator` **Protocol**，
不 import 具体类——因此把它切换成「intake 只入队」是一次纯装配变更
（12.9.3.6 §11：原子切换，不存在双驱动窗口）。

**G3 状态语义**：Worker **不重新解释**任何状态——成功 / 失败一律交给 phase API
（`record_success` / `record_failure`），状态映射表（STAGE_FAILURE_MAP）仍只有
一处。本模块刻意不含任何 processing / delivery 状态字面量（机器化锁定见测试）。

刻意不做（全部后置）：daemon loop / poll / sleep / process supervisor /
graceful shutdown / queue / multiple workers / Kubernetes / lease / heartbeat /
stale recovery。
"""

from collections.abc import Callable

from app.application.bootstrap import BootstrapCoordinator
from app.application.interaction_recording import PersistingCoordinator
from app.application.runtime import RuntimeExecutionCoordinator
from app.domain.interaction_snapshot import inbound_from_snapshot
from app.repositories.unit_of_work import UnitOfWork


class Worker:
    """异步执行路径的执行者：一次 process = 一条 interaction 的一次执行。"""

    def __init__(
        self,
        *,
        runtime: RuntimeExecutionCoordinator,
        phases: PersistingCoordinator,
        uow_factory: Callable[[], UnitOfWork],
        bootstrap: BootstrapCoordinator | None = None,
    ) -> None:
        """依赖全部注入：Worker 自己不构造 Runtime、不构造 Repository。

        - `runtime`：真实 RuntimeCoordinator（六段编排，零改动）；
        - `phases`：persistence phase API（claim / record outcome）；
        - `uow_factory`：读回 interaction 快照；
        - `bootstrap`：M6.6-C3 / ADR-12 —— 可选 Bootstrap 组件。**缺省 None ⇒
          行为与注入前逐字节一致**（未启用 Bootstrap 的部署/测试零变化）。

          它是**唯一**的注入点：HTTP / WS / Driver 三条 Trigger 全部汇聚到
          `Worker.process`，因此不存在「某条 transport 没有 Bootstrap」的分叉。
        """
        self._runtime = runtime
        self._phases = phases
        self._uow_factory = uow_factory
        self._bootstrap = bootstrap

    def process(self, interaction_id: int) -> bool:
        """处理一条 PENDING interaction。

        返回 True = 取得执行权并已执行；False = 未取得执行权（不存在 / 非
        PENDING / 他人已 claim）→ **绝不执行** Runtime（G1）。

        Runtime 的 typed error **原样向上抛**（异常通道本身就是执行事实通道，
        与既有同步路径一致）；失败终态已在抛出前由 phase API 落定。
        """
        record = self._load(interaction_id)
        if record is None:
            return False
        if not self._phases.claim_processing(interaction_id):
            return False      # ← G1：未取得执行权，STOP，Runtime 一次都不跑
        expected_started_at = self._read_started_at(interaction_id)
        inbound = inbound_from_snapshot(record.request_payload)
        # M6.6-C3 / ADR-12：Bootstrap 落在 **claim 之后、execute 之前**——
        # ① claim 之前 ⇒ 未取得执行权的进程也会改状态；
        # ② execute 之后 ⇒ 管线已经失败，来不及供给授权事实。
        # 不满足 Arm 条件时它是 no-op（与未启用 Bootstrap 等价）。
        if self._bootstrap is not None:
            self._bootstrap.ensure(inbound, interaction_id)
        try:
            result = self._runtime.execute(inbound)
        except Exception as exc:
            self._phases.record_failure(
                interaction_id, inbound, exc,
                expected_started_at=expected_started_at)
            raise
        self._phases.record_success(
            interaction_id, result, expected_started_at=expected_started_at)
        return True

    def _load(self, interaction_id: int):
        """读回 interaction 记录——只为拿到 request_payload 快照（纯读）。"""
        with self._uow_factory() as uow:
            return uow.interactions.get_by_id(interaction_id)

    def _read_started_at(self, interaction_id: int):
        """读回 claim 时刻 DB 写入的 fence 值并持有（12.9.4.12）。

        等式两侧必须同为 DB 值（列 vs 读回值）——Worker **绝不**自己生成
        时间戳（12.9.4.11 §1.2）。若本 attempt 执行期间被 reclaim，旧 attempt
        的 outcome 会因等式失配落为 REJECTED，绝不写穿新 attempt。
        """
        with self._uow_factory() as uow:
            record = uow.interactions.get_by_id(interaction_id)
        return record.processing_started_at if record is not None else None
