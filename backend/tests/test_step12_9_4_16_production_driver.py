"""Step 12.9.4.16 —— Production Driver 最小实现。

锁定「Driver 只是 trigger」这条边界：

    A  Pickup          PENDING → Worker.process(id) 各一次
    B  Empty           没有候选 = 正常一轮
    C  Multiple        [1,2,3] → process 各一次
    D  Worker 异常隔离  1 抛异常，2 仍执行，Driver 存活；False 不触发重试
    E  基础设施故障     find_pending / reclaim 抛错 → Driver 不死，下一轮继续
    F  Reclaim cadence 每 N 轮一次；cadence 与 Core 阈值严格分离
    G  Shutdown        stop 后不再启动新的 iteration
    H  结构守卫        Driver 不含任何状态语义 / 不按状态分支 / 只调三个能力

刻意不测（全部后置）：Worker Pool / 并发 / 队列 / 重试 / 优先级 / 调度器 /
心跳 / lease / 指标 / 分布式 —— 本步 Driver 不拥有这些能力。
"""

import ast
import logging
import time
from pathlib import Path

from sqlalchemy.orm import sessionmaker

from app.application.driver import DriverConfig, ProductionDriver
from app.application.interaction_recording import PersistingCoordinator
from app.application.worker import Worker
from app.domain.agent_response import AgentResponse
from app.domain.channel import Channel
from app.domain.identity import Identity
from app.domain.inbound_interaction import InboundInteraction
from app.domain.runtime_execution_result import RuntimeExecutionResult
from app.domain.session import Session as SessionEntity
from app.domain.session_resolution import REUSE_ACTIVE
from app.domain.user import User
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

BACKEND = Path(__file__).resolve().parents[1]
DRIVER_PY = BACKEND / "app" / "application" / "driver.py"
MAIN_PY = BACKEND / "app" / "main.py"


# ================================================================== 测试底座


class _Record:
    """InteractionRecord 的最小替身：Driver 只消费 `id`。"""

    def __init__(self, interaction_id: int) -> None:
        self.id = interaction_id


class _FakeSource:
    """DISCOVER + RECOVER 的假实现：记录调用，可注入一次性失败。"""

    def __init__(self, ids, *, find_error=None, reclaim_error=None) -> None:
        self._ids = list(ids)
        self._find_error = find_error
        self._reclaim_error = reclaim_error
        self.find_calls = 0
        self.find_limits: list[int] = []
        self.reclaim_calls = 0
        self.reclaim_thresholds: list[int] = []

    def find_pending(self, limit: int = 10) -> list[_Record]:
        self.find_calls += 1
        self.find_limits.append(limit)
        if self._find_error is not None:
            error, self._find_error = self._find_error, None   # 只失败一次
            raise error
        return [_Record(i) for i in self._ids]

    def reclaim_stale_processing(self, threshold_seconds: int) -> int:
        self.reclaim_calls += 1
        self.reclaim_thresholds.append(threshold_seconds)
        if self._reclaim_error is not None:
            raise self._reclaim_error
        return 0


class _FakeWorker:
    """Worker 替身：可让指定 id 抛异常，或返回 False（未取得执行权）。"""

    def __init__(self, *, failing=(), losing_claim=()) -> None:
        self._failing = set(failing)
        self._losing = set(losing_claim)
        self.calls: list[int] = []

    def process(self, interaction_id: int) -> bool:
        self.calls.append(interaction_id)
        if interaction_id in self._failing:
            raise RuntimeError(f"worker failed for {interaction_id}")
        return interaction_id not in self._losing


class _OkRuntime:
    """成功 Runtime（真实 Core 集成用）：记录被执行的次数。

    P1 / C1：投递是显式的第 7 段（由应用层在结果落库后发起）。
    """

    def __init__(self, result: RuntimeExecutionResult) -> None:
        self.result = result
        self.calls = 0
        self.deliveries: list = []

    def execute(self, inbound: InboundInteraction) -> RuntimeExecutionResult:
        self.calls += 1
        return self.result

    def deliver(self, outbound) -> None:
        self.deliveries.append(outbound)


def _driver(source, worker, **overrides) -> ProductionDriver:
    return ProductionDriver(source=source, worker=worker,
                            config=DriverConfig(**overrides))


# ================================================================== A. Pickup


def test_a1_pickup_triggers_worker_for_the_discovered_id() -> None:
    """PENDING → Driver pickup → Worker.process(id)（各一次）。"""
    source = _FakeSource([1])
    worker = _FakeWorker()

    assert _driver(source, worker).run_once() is True
    assert worker.calls == [1]
    assert source.find_calls == 1


def test_a2_pickup_limit_is_forwarded_to_core() -> None:
    """每轮上限是配置，原样透传给 Core（不是 Driver 自己筛）。"""
    source = _FakeSource([])

    _driver(source, _FakeWorker(), pickup_limit=7).run_once()

    assert source.find_limits == [7]


def test_a3_driver_drives_the_real_core_end_to_end(db_engine) -> None:
    """真实 Core 集成（一次）：Driver → 真 source（phase API）→ 真 Worker → 真库。

    证明 Driver 推动的是**真的**那条链：DISCOVER 走真实持久化读，TRIGGER 走真实
    Worker（CAS claim + 执行 + 落 outcome）——不是只有假件能跑通。
    """
    uow_factory = lambda: SqlAlchemyUnitOfWork(  # noqa: E731
        session_factory=sessionmaker(bind=db_engine, expire_on_commit=False))
    with uow_factory() as uow:
        user = uow.users.create(User(name="alice"))
        channel = uow.channels.create(Channel(name="wecom-1", type="wecom"))
        identity = uow.identities.create(Identity(
            user_id=user.id, channel_id=channel.id, external_id="wx_a"))
        session = uow.sessions.create(SessionEntity(
            identity_id=identity.id, channel_id=channel.id,
            external_conversation_id="conv-1", status="active"))
        uow.commit()
        channel_id, session_id = channel.id, session.id

    runtime = _OkRuntime(RuntimeExecutionResult(
        session_mode=REUSE_ACTIVE, session_id=session_id,
        agent_response=AgentResponse(content="ok")))
    phases = PersistingCoordinator(inner=runtime, uow_factory=uow_factory)
    interaction_id = phases.create_pending(InboundInteraction(
        channel_id=channel_id, external_interaction_id="msg-1",
        external_conversation_id="conv-1", external_identity_id="wx_a",
        content="你好"))

    driver = ProductionDriver(
        source=phases,
        worker=Worker(runtime=runtime, phases=phases, uow_factory=uow_factory),
        config=DriverConfig())

    assert driver.run_once() is True
    assert runtime.calls == 1                       # 恰好执行一次（无双执行）
    with uow_factory() as uow:
        row = uow.interactions.get_by_id(interaction_id)
    assert row.processing_status == "succeeded"
    assert row.delivery_status == "delivered"


# ================================================================== B. Empty


def test_b1_empty_pending_is_a_normal_iteration() -> None:
    """没有候选 = 正常一轮：不报错、不调 Worker。"""
    source = _FakeSource([])
    worker = _FakeWorker()

    assert _driver(source, worker).run_once() is True
    assert worker.calls == []
    assert source.find_calls == 1


def test_b2_empty_pending_does_not_stop_the_loop() -> None:
    """连续空轮：Driver 正常进入下一轮。"""
    driver = _driver(_FakeSource([]), _FakeWorker())

    for _ in range(3):
        assert driver.run_once() is True


# ================================================================== C. Multiple


def test_c1_each_discovered_id_is_triggered_once_in_order() -> None:
    """find_pending = [1,2,3] → process(1) / process(2) / process(3)。"""
    source = _FakeSource([1, 2, 3])
    worker = _FakeWorker()

    _driver(source, worker).run_once()

    assert worker.calls == [1, 2, 3]


# ================================================================== D. 异常隔离


def test_d1_worker_exception_is_isolated_and_loop_survives(caplog) -> None:
    """1 抛异常 → 记录；2 仍执行；Driver 存活（单条失败不算基础设施错误）。"""
    source = _FakeSource([1, 2])
    worker = _FakeWorker(failing={1})
    driver = _driver(source, worker)

    with caplog.at_level(logging.ERROR):
        assert driver.run_once() is True

    assert worker.calls == [1, 2]              # 1 抛了，2 仍然执行
    assert "worker raised" in caplog.text      # 异常被记录

    assert driver.run_once() is True           # Driver 仍存活
    assert worker.calls == [1, 2, 1, 2]


def test_d2_false_from_worker_is_never_retried() -> None:
    """未取得执行权（False）→ 恰好调用一次，**绝不**「没跑到就再来一次」。"""
    source = _FakeSource([1])
    worker = _FakeWorker(losing_claim={1})

    _driver(source, worker).run_once()

    assert worker.calls == [1]


# ================================================================== E. 基础设施故障


def test_e1_pickup_infrastructure_failure_does_not_terminate_driver() -> None:
    """find_pending 抛 DB 错误 → 本轮不健康；下一轮恢复后继续 pickup。"""
    source = _FakeSource([9], find_error=RuntimeError("db down"))
    worker = _FakeWorker()
    driver = _driver(source, worker)

    assert driver.run_once() is False          # 基础设施错误 → 退避信号
    assert worker.calls == []

    assert driver.run_once() is True           # Driver 没有死
    assert worker.calls == [9]


def test_e2_reclaim_infrastructure_failure_is_contained() -> None:
    """reclaim 抛错被就地隔离：不扩散、不终止循环。"""
    source = _FakeSource([], reclaim_error=RuntimeError("db down"))

    assert _driver(source, _FakeWorker(), reclaim_every=1).run_once() is False
    assert source.reclaim_calls == 1


# ================================================================== F. Reclaim cadence


def test_f1_reclaim_runs_every_n_iterations() -> None:
    """reclaim_every=3 → 第 3、6 轮回收，其余轮次不回收。"""
    source = _FakeSource([])
    driver = _driver(source, _FakeWorker(), reclaim_every=3)

    reclaimed_per_round = []
    for _ in range(7):
        before = source.reclaim_calls
        driver.run_once()
        reclaimed_per_round.append(source.reclaim_calls > before)

    assert reclaimed_per_round == [False, False, True, False, False, True,
                                   False]


def test_f2_cadence_and_core_threshold_are_separate() -> None:
    """cadence（每几轮）与 Core 的孤儿阈值互不相干：阈值原样透传。"""
    source = _FakeSource([])

    _driver(source, _FakeWorker(), reclaim_every=1,
            stale_threshold_seconds=42).run_once()

    assert source.reclaim_thresholds == [42]
    assert DriverConfig().reclaim_every != 42     # cadence 不是阈值


# ================================================================== G. Shutdown


def test_g1_stop_stops_the_loop_and_starts_no_new_iteration() -> None:
    """stop()：ticker 退出，且不再启动新的 iteration。"""
    source = _FakeSource([])
    driver = _driver(source, _FakeWorker(), poll_interval_seconds=0.01,
                     reclaim_every=1)
    driver.start()
    deadline = time.time() + 5.0
    while source.find_calls == 0 and time.time() < deadline:
        time.sleep(0.01)
    assert source.find_calls >= 1, "start 后 ticker 应跑起来"

    driver.stop(timeout=5.0)
    snapshot = source.find_calls
    time.sleep(0.05)

    assert source.find_calls == snapshot, "stop 之后不应再启动新的 iteration"


def test_g2_start_is_idempotent_and_stop_without_start_is_safe() -> None:
    """重复 start 只起一个 ticker；未 start / 已 stop 的 stop 都是安全的。"""
    driver = _driver(_FakeSource([]), _FakeWorker(), poll_interval_seconds=0.01)

    driver.start()
    driver.start()
    driver.stop(timeout=5.0)
    driver.stop()


# ================================================================== H. 结构守卫

FORBIDDEN_TOKENS = (
    "PROCESSING_PENDING", "PROCESSING_PROCESSING", "PROCESSING_SUCCEEDED",
    "PROCESSING_FAILED", "DELIVERY_PENDING", "DELIVERY_DELIVERED",
    "DELIVERY_FAILED", "processing_status", "delivery_status",
    "uow.interactions", "RuntimeCoordinator",
)

FORBIDDEN_CALLS = frozenset({
    "record_outcome", "claim_processing", "create_pending",
    # P1 / C6：成功路径拆为「Agent 成功」与「投递终态」两个 phase——
    # Driver 依旧**不得**直接调用任何 phase（它只触发 Worker）。
    "record_agent_success", "record_delivery_success",
    "record_delivery_failure", "record_failure", "execute",
})


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    return ""


def test_h1_driver_contains_no_state_semantics() -> None:
    """Driver 源码不含任何状态字面量 / 状态字段 / 直接持久化访问。"""
    src = DRIVER_PY.read_text(encoding="utf-8")
    for token in FORBIDDEN_TOKENS:
        assert token not in src, f"Driver 触碰了状态语义：{token}"


def test_h2_driver_never_branches_on_status() -> None:
    """Driver 绝不按状态分支——否则就是第二套状态机。"""
    src = DRIVER_PY.read_text(encoding="utf-8")
    for pattern in ("status ==", "status !=", "status is"):
        assert pattern not in src, f"Driver 出现状态分支：{pattern}"


def test_h3_driver_vocabulary_is_discover_trigger_recover_only() -> None:
    """Driver 唯一的业务调用能力：DISCOVER / TRIGGER / RECOVER（AST 级）。

    它拿不到 claim / 落 outcome / 直接执行的能力——不是靠自觉，是靠调用面。
    """
    tree = ast.parse(DRIVER_PY.read_text(encoding="utf-8"))
    offenders = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr in FORBIDDEN_CALLS
    ]
    assert not offenders, f"Driver 越权调用了 Core 写能力：{offenders}"


def test_h4_driver_exposes_no_interaction_state() -> None:
    """12.9.4.16.8：Driver 不得把 Interaction 生命周期存成自己的公开状态
    （processed_count / failed_count / pending_ids / retry_count 一律不许）。"""
    tree = ast.parse(DRIVER_PY.read_text(encoding="utf-8"))
    public_attrs = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store)
        and isinstance(node.value, ast.Name) and node.value.id == "self"
        and not node.attr.startswith("_")
    }
    assert not public_attrs, f"Driver 暴露了 Interaction 状态：{public_attrs}"


def test_h5_composition_root_wires_exactly_one_driver() -> None:
    """Driver 构造点在组合根恰好一次（per-tenant builder 内，0.1.3.3 按租户
    实例化 N 套），生命周期挂到 FastAPI lifespan（唯一生命周期机制）。"""
    tree = ast.parse(MAIN_PY.read_text(encoding="utf-8"), filename="main.py")
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    driver_calls = [c for c in calls if _dotted(c.func) == "ProductionDriver"]

    assert len(driver_calls) == 1, "ProductionDriver 构造点恰好一次"
    assert {kw.arg for kw in driver_calls[0].keywords if kw.arg} == {
        "source", "worker", "config"}

    src = MAIN_PY.read_text(encoding="utf-8")
    assert "lifespan=lifespan" in src        # 生命周期挂在应用上，不另造一套
    assert "tenant_fanout.start()" in src and "tenant_fanout.stop()" in src
