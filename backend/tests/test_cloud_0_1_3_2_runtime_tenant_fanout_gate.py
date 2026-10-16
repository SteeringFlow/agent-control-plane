"""Cloud-0.1.3.2 —— Multi-Tenant Async Runtime / Worker Tenant Fan-out（架构护栏）.

本 Gate **不写实现代码**，只锁结构（对现有代码即绿；实现期若把 tenant 塞进 Runtime
Domain，则转红）。核心裁决（见 docs/step-cloud-0.1.3.2-...-architecture-gate.md）：

    fan-out = 同一份 Runtime 组合按 tenant 实例化 N 次，每份绑 tenant-scoped UoW，
              由组合根薄编排器 tick；Worker/Driver/Coordinator/Executor 代码零改动。

锁清单（L1–L8 对应 Gate §3）：
    L1  Worker.process 无 tenant 参数、worker.py 不 import tenant
    L2  Driver 无 tenant 枚举 / 参数（PendingSource / DriverConfig / run_once 调用）
    L3  Coordinator.execute 无 tenant 参数、runtime.py 不 import tenant
    L4  四个异步原语已 tenant-scoped（find_pending/claim/record_outcome/reclaim）
    L5  Attempt Fence 以 PK id 为锚（唯一性不经 tenant 参数）
    L6  tenant 只经 UoW 工厂桥注入；Worker/Driver/Coordinator/Executor 构造无 tenant= 关键字
    L7  行为：per-tenant find_pending 隔离（本租户只看见本租户 PENDING）
    L8  无隐式全局租户上下文（worker/driver/runtime/interaction_recording 不 import contextvars）
"""

from __future__ import annotations

import ast
import pathlib

from sqlalchemy.orm import sessionmaker

from app.domain.channel import Channel
from app.domain.interaction_record import InteractionRecord
from app.persistence.models.tenant import TenantModel
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

APP = pathlib.Path(__file__).resolve().parents[1] / "app"


def _src(rel: str) -> str:
    return (APP / rel).read_text(encoding="utf-8")


def _tree(rel: str) -> ast.Module:
    return ast.parse(_src(rel))


def _class_method(tree: ast.Module, cls: str, method: str):
    for n in ast.walk(tree):
        if isinstance(n, ast.ClassDef) and n.name == cls:
            for m in n.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name == method:
                    return m
    return None


def _params_have_no_tenant(funcdef: ast.FunctionDef) -> bool:
    for a in (*funcdef.args.args, *funcdef.args.kwonlyargs):
        if "tenant" in a.arg.lower():
            return False
    return True


def _call_passes_tenant_kwarg(tree: ast.Module) -> bool:
    """任意 Call 不得传 `tenant=` 关键字（tenant 只经 UoW 工厂桥，不经 Runtime 构造）。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "tenant":
                    return True
    return False


def _methods_of(tree: ast.Module, names: set[str]) -> list[ast.FunctionDef]:
    found = []
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name in names:
            found.append(n)
    return found


# ============================================================== L1–L3 Worker/Driver/Coordinator tenant-blind


def test_l1_worker_process_is_tenant_free() -> None:
    """Worker.process 无 tenant 参数；worker.py 不 import tenant 原语。"""
    tree = _tree("application/worker.py")
    proc = _class_method(tree, "Worker", "process")
    assert proc is not None
    assert _params_have_no_tenant(proc), "Worker.process 不得带 tenant 参数"
    assert "from app.core.tenant" not in _src("application/worker.py")
    assert "tenant_id" not in _src("application/worker.py")


def test_l2_driver_has_no_tenant_enumeration() -> None:
    """Driver 不枚举/不携带 tenant：PendingSource / DriverConfig / run_once 调用均无 tenant。"""
    tree = _tree("application/driver.py")
    # PendingSource 两方法无 tenant 参数
    for m in _methods_of(tree, {"find_pending", "reclaim_stale_processing"}):
        assert _params_have_no_tenant(m), f"{m.name} 不得带 tenant 参数"
    # DriverConfig 字段无 tenant（只含节奏/批量）
    for n in ast.walk(tree):
        if isinstance(n, ast.ClassDef) and n.name == "DriverConfig":
            for stmt in n.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    assert "tenant" not in stmt.target.id.lower(), \
                        f"DriverConfig 不得含 tenant 字段（{stmt.target.id}）"
    # 不得有任何 Call 传 tenant= 关键字
    assert not _call_passes_tenant_kwarg(tree)
    assert "from app.core.tenant" not in _src("application/driver.py")


def test_l3_coordinator_execute_is_tenant_free() -> None:
    """RuntimeCoordinator.execute 无 tenant 参数；runtime.py 不 import tenant 原语。"""
    tree = _tree("application/runtime.py")
    # 协议方法 execute（RuntimeExecutionCoordinator 是 Protocol）
    proc = _class_method(tree, "RuntimeExecutionCoordinator", "execute")
    assert proc is not None
    assert _params_have_no_tenant(proc), "Coordinator.execute 不得带 tenant 参数"
    assert "from app.core.tenant" not in _src("application/runtime.py")


# ============================================================== L4–L5 异步原语已 tenant-scoped + Fence 锚定 PK


def test_l4_async_primitives_carry_tenant_filter() -> None:
    """四个异步原语已在 self._tenant_id 非空时追加 tenant 过滤（代码实证，fan-out 零改动）。"""
    tree = _tree("persistence/repositories/interaction_record.py")
    for m in _methods_of(
        tree,
        {"find_pending", "claim_processing", "record_outcome", "reclaim_stale_processing"},
    ):
        unparsed = ast.unparse(m)
        assert "InteractionModel.tenant_id == self._tenant_id" in unparsed, \
            f"{m.name} 必须 tenant-scoped（InteractionModel.tenant_id == self._tenant_id）"


def test_l5_attempt_fence_anchored_on_pk_id() -> None:
    """Attempt Fence 唯一性由全局 PK id 决定（不经 tenant 参数）——多 tenant/多 Driver 仍成立。"""
    tree = _tree("persistence/repositories/interaction_record.py")
    claim = next(m for m in _methods_of(tree, {"claim_processing"}))
    unparsed = ast.unparse(claim)
    assert "InteractionModel.id == interaction_id" in unparsed, \
        "claim_processing 必须以 InteractionModel.id 为 PK 锚（围栏唯一性由 id 决定）"


# ============================================================== L6 tenant 只经 UoW 工厂桥注入


def test_l6_tenant_injected_only_via_uow_factory_bridge() -> None:
    """main.py：Runtime 组件构造无 tenant= 关键字；tenant 经 partial(UoW, tenant_id=) 桥流入。"""
    tree = _tree("main.py")
    runtime_cls = {
        "Worker", "PersistingCoordinator", "RuntimeCoordinator",
        "IdentityResolutionExecutor", "RoutingExecutor", "SessionResolutionExecutor",
        "SessionCreationExecutor", "AgentDispatchExecutor", "DeliveryExecutor",
        "ProductionDriver",
    }
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
                and n.func.id in runtime_cls:
            for kw in n.keywords:
                assert kw.arg != "tenant", \
                    f"{n.func.id} 构造不得传 tenant=（tenant 只经 UoW 工厂桥注入）"
    # 工厂桥存在：tenant 经 partial(SqlAlchemyUnitOfWork, tenant_id=...) 流入
    assert "partial(\n        SqlAlchemyUnitOfWork, tenant_id=" in _src("main.py") or \
        "partial(SqlAlchemyUnitOfWork, tenant_id=" in _src("main.py")
    # 这些 Runtime 模块不 import tenant 原语（tenant 不进 Runtime 域）
    for mod in ("application/worker.py", "application/driver.py",
                "application/runtime.py", "application/interaction_recording.py"):
        assert "from app.core.tenant" not in _src(mod)


# ============================================================== L7 行为：per-tenant 隔离


def _seed(engine, tenant_id: int) -> int:
    """在指定 tenant 下建一个 channel + 一条 PENDING interaction，返回 interaction id。"""
    sf = sessionmaker(bind=engine, expire_on_commit=False)
    bootstrap = SqlAlchemyUnitOfWork(session_factory=sf, tenant_id=None)
    with bootstrap as uow:
        if uow.session.get(TenantModel, tenant_id) is None:
            uow.session.add(TenantModel(id=tenant_id, name=f"t{tenant_id}", status="active"))
            uow.session.commit()
    scoped = SqlAlchemyUnitOfWork(session_factory=sf, tenant_id=tenant_id)
    with scoped as uow:
        channel = uow.channels.create(Channel(name=f"c{tenant_id}", type="local", config={}))
        uow.commit()
        rec = uow.interactions.create(
            InteractionRecord(channel_id=channel.id, direction="inbound"))
        uow.commit()
        return rec.id


def test_l7_per_tenant_find_pending_isolation(db_engine) -> None:
    """tenant-scoped UoW 的 find_pending 只看见本租户 PENDING——fan-out 边界由 UoW binding 保证。"""
    iid1 = _seed(db_engine, 1)
    iid2 = _seed(db_engine, 2)
    sf = sessionmaker(bind=db_engine, expire_on_commit=False)
    with SqlAlchemyUnitOfWork(session_factory=sf, tenant_id=1) as uow:
        t1 = [r.id for r in uow.interactions.find_pending(10)]
    with SqlAlchemyUnitOfWork(session_factory=sf, tenant_id=2) as uow:
        t2 = [r.id for r in uow.interactions.find_pending(10)]
    assert iid1 in t1 and iid1 not in t2
    assert iid2 in t2 and iid2 not in t1


# ============================================================== L8 无隐式全局租户上下文


def test_l8_no_implicit_global_tenant_context() -> None:
    """Runtime 模块不得用 contextvars / 模块级全局承载 tenant（B §0 红线：禁止隐式全局租户上下文）。"""
    for mod in ("application/worker.py", "application/driver.py",
                "application/runtime.py", "application/interaction_recording.py"):
        assert "contextvars" not in _src(mod), f"{mod} 不得引入 contextvars 隐式租户上下文"
