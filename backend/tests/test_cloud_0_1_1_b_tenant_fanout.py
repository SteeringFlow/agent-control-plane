"""Cloud-0.1.1-B —— Tenant fan-out 组合验证（实现边界 §19.3 Increment 3 / 用户裁决 §4）。

fan-out 是 Worker 的 Scope / 运行方式变化，不是 Runtime execution logic 的变化：
每个租户各一套 Driver/Worker，绑定该租户的 tenant-scoped UoW。Runtime execution
order / CAS / Attempt Fence / failure semantics / Driver capabilities 全部不变。
"""

import ast
import inspect
from pathlib import Path

from app.application.tenant_fanout import TenantFanoutDriver
from app.core.tenant import TENANT_LOCAL, TenantScope

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"


class _FakeDriver:
    def __init__(self, scope: TenantScope) -> None:
        self.scope = scope
        self.running = False

    def start(self) -> None:
        self.running = True

    def stop(self) -> None:
        self.running = False


def test_fanout_starts_one_driver_per_tenant() -> None:
    scopes = [TenantScope(1), TenantScope(2), TenantScope(3)]
    built: list[_FakeDriver] = []

    def factory(scope: TenantScope) -> _FakeDriver:
        d = _FakeDriver(scope)
        built.append(d)
        return d

    fan = TenantFanoutDriver(
        tenant_source=lambda: scopes, driver_factory=factory)
    fan.start()

    assert [d.scope.tenant_id for d in built] == [1, 2, 3]
    assert all(d.running for d in built)     # 每租户各一套、均已启动

    fan.stop()
    assert all(not d.running for d in built)


def test_fanout_single_tenant_degenerates_to_one_driver() -> None:
    """OSS / 本地单租户：fan-out 退化为恰好 1 套（TENANT_LOCAL）。"""
    built: list[_FakeDriver] = []
    fan = TenantFanoutDriver(
        tenant_source=lambda: [TenantScope(TENANT_LOCAL)],
        driver_factory=lambda s: built.append(_FakeDriver(s)) or built[-1],
    )
    fan.start()
    assert [d.scope.tenant_id for d in built] == [TENANT_LOCAL]
    fan.stop()


def test_fanout_passes_tenant_scope_to_driver_factory() -> None:
    """driver_factory 收到的是显式 TenantScope（tenant 显式进入，无隐式全局）。"""
    seen: list[int] = []
    fan = TenantFanoutDriver(
        tenant_source=lambda: [TenantScope(7)],
        driver_factory=lambda scope: (seen.append(scope.tenant_id) or _FakeDriver(scope)),
    )
    fan.start()
    assert seen == [7]


def test_fanout_does_not_change_runtime_execution_logic() -> None:
    """红线：fan-out 只做 Scope 组合，Runtime/Worker/Driver 执行逻辑零改动。

    与 fan-out 无关的既有组件不得 import 它（执行不因多租户而分叉）。
    """
    for rel in ("application/runtime.py", "application/worker.py",
                "application/coordinator.py"):
        src = (APP / rel).read_text(encoding="utf-8")
        assert "tenant" not in src.lower(), f"{rel} 不应出现 tenant（执行逻辑租户无关）"

    # Runtime execution order 冻结不变
    from app.application.runtime import RUNTIME_EXECUTION_ORDER

    assert RUNTIME_EXECUTION_ORDER == (
        "identity_resolution", "authorization", "routing", "session_resolution",
        "session_creation", "agent_dispatch", "delivery",
    )
