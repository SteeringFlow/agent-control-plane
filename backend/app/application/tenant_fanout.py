"""Multi-Tenant Driver Fan-out —— per-tenant Runtime 组合（B §19.3 Increment 3 / Cloud-0.1.3.3）。

fan-out 是 Worker 的 Scope / 运行方式变化，**不是** Runtime execution logic 的变化
（B 实现边界 §8 末 / §13.3；0.1.3.2 Gate 中心命题的落地形态）：

    每个租户各一套 Driver/Worker（同一份单租户组合的忠实实例），绑定该租户的
    tenant-scoped UoW。Runtime execution order / CAS / Attempt Fence /
    failure semantics / Driver capabilities 全部不变。

组件（B 冻结契约，规格测试 test_cloud_0_1_1_b_tenant_fanout.py 先行）：

- `TenantComposition`：一个租户的忠实单租户组合清单（worker/driver/phases/
  channel_adapter，全部 UoW 工厂已由组合根绑到该租户）；
- `TenantFanoutDriver(tenant_source=..., driver_factory=...)`：
      start() → 为 tenant_source() 的每个 TenantScope 构建并启动一套 Driver
                （每租户各一套、均已启动）；
      stop()  → 全部停止（不中断在途执行——所有权归各 Driver/Worker）。
  单租户（N=1，TENANT_LOCAL）退化为恰好一套——与既有单一 Driver 行为逐字节同构。

边界纪律（0.1.3.2 Gate Q10 红线在编排器侧的兑现）：

- **tenant 不进 Runtime 域**：本模块只持有「已组合好」的 TenantComposition；
  Worker/Driver 保持 tenant-blind，本模块不解释任何 Runtime 语义；
- **tenant 枚举是注入的**：`tenant_source` 由组合根提供（bootstrap 只读投影）；
- **per-tenant 错误隔离**（Gate Q5）：每租户独立的 Driver 线程 + Driver 自身的
  异常纪律（12.9.4.16.5）——一个租户的异常绝不阻塞其他租户；
- **双触发汇合同一租户**（12.9.4.18A）：Trigger 1（webhook）经组合根的
  per-tenant 注册表把执行落到已认证租户的 Worker，与 Trigger 2 重叠安全
  （claim CAS + Attempt Fence 由 Core 保证）。

刻意不做（全部后置）：租户集合的运行期周期刷新（新租户先由 Trigger 1 惰性
覆盖，durable Driver 于重启后补全——F3 修订）/ 租户级限流 / 多进程拓扑。
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass

from app.application.driver import ProductionDriver
from app.application.worker import Worker

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class TenantComposition:
    """一个租户的忠实单租户 Runtime 组合（同一 builder 产物，仅 UoW 绑定不同）。

    组合根按同一构造序列为每个租户实例化一份。本记录只是组合装配清单
    （composition wiring），不是域实体——不含任何业务语义。
    """

    tenant_id: int
    worker: Worker
    driver: ProductionDriver
    phases: object           # PersistingCoordinator（legacy intake 依赖 / 诊断用）
    channel_adapter: object  # MultiChannelAdapter（入站 Adapter 消费点）
    # P2：投递恢复驱动（per-tenant，**默认不启动**——由组合根按配置决定）。
    # 缺省 None ⇒ 行为与 P2 之前逐字节一致（不启动任何恢复 ticker）。
    recovery_driver: object | None = None


class TenantFanoutDriver:
    """per-tenant Driver 的生命周期编排：每租户各一套，start/stop 对称。

    依赖全部注入：本类不枚举租户（`tenant_source`）、不构建 Runtime
    （`driver_factory`）、不碰 DB。`driver_factory` 收到**显式 TenantScope**
    （tenant 显式进入，无隐式全局——B 冻结契约），返回带 start/stop 的 Driver。
    """

    def __init__(
        self,
        *,
        tenant_source: Callable[[], list],
        driver_factory: Callable[[object], object],
    ) -> None:
        self._tenant_source = tenant_source
        self._driver_factory = driver_factory
        self._drivers: list = []

    def start(self) -> None:
        """为每个租户构建并启动一套 Driver（幂等保护：重复 start 忽略）。"""
        if self._drivers:
            return
        for scope in self._tenant_source():
            driver = self._driver_factory(scope)
            driver.start()
            self._drivers.append(driver)
            LOGGER.info("tenant driver started, tenant_id=%s", scope.tenant_id)

    def stop(self) -> None:
        """停止全部 per-tenant Driver（不中断在途执行；幂等）。"""
        for driver in self._drivers:
            driver.stop()
        self._drivers.clear()
