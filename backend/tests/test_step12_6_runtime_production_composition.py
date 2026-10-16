"""Step 12.6 —— Runtime Production Composition 验收。

目标极其狭窄：把已通过 E2E 验证的 Runtime **原样**装配进 main.py 组合根，
零新增业务逻辑、零重构、零新能力。验收核心问题：

    Production Composition 与测试 Composition 是否完全一致？

对照基准（测试装配范式）：

    test_step10_1_4_real_agent_runtime._coordinator   （六段 + 真实 ACPAdapter）
    test_step10_2_1_wecom_webhook.client              （webhook + 真实 WeComAdapter）
    test_step10_8_3_runtime_coordinator_result_integration（PersistingCoordinator 包装）

本文件锁定四件事：

1. 生产 app 已注册 /api/channels/{id}/wecom/webhook（GET 验证 + POST 消息）；
2. **组合一致性（AST 级）**：main.py 的 Runtime 组件清单与测试范式逐一相等——
   同一个 RuntimeCoordinator（六个冻结 kwarg）、同六 Executor、PersistingCoordinator
   包装、真实 ACPAdapter、单一 WeComAdapter 双端口、同一 UoW factory 贯穿全部组件；
3. **组合根零业务逻辑**：无新类、无 domain 对象构造、无 wire 函数调用——
   不产生第二套 Runtime 逻辑；
4. 生产行为探针（内存 UoW 注入，不触生产库/网络）：握手 200 明文回显、
   未知渠道 500、坏签名 403、非 text 消息 200 acknowledge-ignore——
   HTTP → Service → 真实 WeComAdapter 的生产边界真实工作。

完整消息链路（Dispatch → Delivery）的 E2E 已由测试组合（10.1.4 真实腾讯
Runtime / 10.2.2 出站 E2E）覆盖；本步只证明「生产装的就是那套组件」。
"""

import ast
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.adapters.channels.wecom import (
    compute_signature,
    encrypt_message,
)
from app.domain.channel import Channel
from app.persistence import models  # noqa: F401  # 注册 8 张表
from app.persistence.database import Base
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

BACKEND = Path(__file__).resolve().parents[1]
MAIN_PY = BACKEND / "app" / "main.py"

WEBHOOK_PATH = "/api/channels/{channel_id}/wecom/webhook"

# 测试装配范式冻结的组件清单（12.6 生产组合必须逐一相等）
EXPECTED_COORDINATOR_KWARGS = {
    "identity_resolution", "access_authorization", "routing", "session_resolution",
    "session_creation", "agent_dispatch", "delivery",
}
EXPECTED_EXECUTORS_ONCE = (
    "IdentityResolutionExecutor", "AccessAuthorizationExecutor", "RoutingExecutor",
    "SessionResolutionExecutor",
    "SessionCreationExecutor", "AgentDispatchExecutor", "DeliveryExecutor",
)


# ================================================================== 装配面（生产）


def test_production_app_registers_wecom_webhook() -> None:
    """main.py 组合根已装配入站 webhook（openapi 契约可见；GET + POST 双端点）。"""
    from app.main import app

    client = TestClient(app)
    paths = client.get("/openapi.json").json()["paths"]
    assert WEBHOOK_PATH in paths, "12.6：生产 app 应注册 WeCom 入站 webhook"
    assert set(paths[WEBHOOK_PATH]) == {"get", "post"}
    # 管理面路由不受影响
    assert "/api/channels" in paths and "/api/users" in paths


# ================================================================== 组合一致性（核心验收）


def _create_app_tree() -> ast.Module:
    return ast.parse(MAIN_PY.read_text(encoding="utf-8"), filename="main.py")


def _calls_in_create_app(tree: ast.Module) -> list[ast.Call]:
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "create_app")
    return [n for n in ast.walk(fn) if isinstance(n, ast.Call)]


def _calls_in_module(tree: ast.Module) -> list[ast.Call]:
    """Cloud-0.1.3.3：Runtime 组合移入 `_build_tenant_runtime`（per-tenant 忠实
    实例的**唯一构造点**，0.1.3.2 Gate F4）——组件计数按整模块扫描（构造点恰好
    一次 = 运行时按租户实例化 N 次仍无第二套组合）。"""
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)]


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    return ""


def _kwargs(call: ast.Call) -> dict[str, str]:
    """kwarg 名 → 值的源码表达式（比较一致性用）。"""
    return {kw.arg: ast.unparse(kw.value) for kw in call.keywords if kw.arg}


def test_production_runtime_coordinator_matches_test_composition() -> None:
    """RuntimeCoordinator 构造点恰好一次（per-tenant builder 内）、六个冻结 kwarg
    与测试装配逐一相等（0.1.3.3：按租户实例化 N 次，构造点仍唯一）。"""
    coordinator_calls = [c for c in _calls_in_module(_create_app_tree())
                         if _dotted(c.func) == "RuntimeCoordinator"]
    assert len(coordinator_calls) == 1, "RuntimeCoordinator 构造点恰好一次"
    assert set(_kwargs(coordinator_calls[0])) == EXPECTED_COORDINATOR_KWARGS


def test_production_executors_each_wired_exactly_once() -> None:
    """六段 Executor 构造点各恰好一次（不少装、不重复装 = 无第二套 Runtime）。"""
    calls = _calls_in_module(_create_app_tree())
    for executor in EXPECTED_EXECUTORS_ONCE:
        hits = [c for c in calls if _dotted(c.func) == executor]
        assert len(hits) == 1, f"{executor} 应恰好装配一次，得到 {len(hits)}"

    # Executor kwarg 形状与测试范式一致
    by_name = {n: [c for c in calls if _dotted(c.func) == n]
               for n in EXPECTED_EXECUTORS_ONCE}
    for name in ("IdentityResolutionExecutor", "RoutingExecutor",
                 "SessionResolutionExecutor", "SessionCreationExecutor"):
        assert not _kwargs(by_name[name][0]), f"{name} 只吃一个位置参数（uow factory）"
    assert set(_kwargs(by_name["AgentDispatchExecutor"][0])) == {
        "agent_adapter", "uow_factory"}
    assert set(_kwargs(by_name["DeliveryExecutor"][0])) == {"channel_adapter"}


def test_production_persisting_coordinator_wraps_inner() -> None:
    """PersistingCoordinator 构造点恰好一次、包装 inner（10.4.2 观察层同位装配）。"""
    calls = _calls_in_module(_create_app_tree())
    persisting = [c for c in calls if _dotted(c.func) == "PersistingCoordinator"]
    assert len(persisting) == 1
    assert set(_kwargs(persisting[0])) == {"inner", "uow_factory"}
    # inner 就是那个唯一的 RuntimeCoordinator（同位包装，不是第二套编排）。
    # 12.9.4.6：inner 改为具名变量（同一个实例同时由 Worker 持有），不再内联。
    inner = persisting[0].keywords[0].value
    assert isinstance(inner, ast.Name) and inner.id == "runtime_coordinator"
    # Worker 持有的是同一个 RuntimeCoordinator（执行权唯一，不是第二套编排）
    worker_calls = [c for c in calls if _dotted(c.func) == "Worker"]
    assert len(worker_calls) == 1, "Worker 构造点恰好一次"
    # M6.6-C3 / ADR-12：+ bootstrap（唯一注入点；三条 Trigger 共用同一边界）
    assert _kwargs(worker_calls[0]) == {
        "runtime": "runtime_coordinator", "phases": "phases",
        "uow_factory": "runtime_uow_factory", "bootstrap": "bootstrap"}


def test_production_single_wecom_adapter_dual_port() -> None:
    """WeComAdapter 构造点仍是唯一 wecom 实现（双端口共用）。

    15.2.3：渠道出口 MultiChannelAdapter 路由（wecom → WeComAdapter /
    local → LocalChannelAdapter），Runtime / Executor 仍只看见一个
    InboundChannelAdapter / ChannelAdapter。
    Cloud-0.1.3.3：构造点唯一、按租户实例化（0.1.3.2 Gate：per-tenant 忠实
    实例——出站凭据读取必须在该租户的 UoW 内）。"""
    calls = _calls_in_module(_create_app_tree())
    wecom_calls = [c for c in calls if _dotted(c.func) == "WeComAdapter"]
    assert len(wecom_calls) == 1, "WeComAdapter 构造点恰好一次（双端口共用）"
    acp_calls = [c for c in calls if _dotted(c.func) == "ACPAdapter"]
    assert len(acp_calls) == 1, "Agent Adapter 只装配唯一真实实现 ACPAdapter"
    assert not _kwargs(acp_calls[0]), "ACPAdapter 无状态构造（与测试装配一致）"

    # 单一 WeComAdapter 构造点经 type→Adapter 路由贯穿入站 / 出站两个消费点
    src = MAIN_PY.read_text(encoding="utf-8")
    assert "wecom_adapter = WeComAdapter(" in src
    assert '"wecom": wecom_adapter,' in src
    assert "delivery=DeliveryExecutor(channel_adapter=channel_adapter)" in src
    assert "inbound_adapter=oss_runtime.channel_adapter" in src


def test_production_single_uow_factory_across_all_components() -> None:
    """同一 UoW factory 变量贯穿 Runtime **执行**组件（per-tenant builder 内）。

    0.1.3.2 Gate F4：`runtime_uow_factory` 绑定到 builder 的 `tenant_id` 形参
    （B §7 桥）——同一构造序列按租户实例化 N 份；TENANT_LOCAL 不再是 Runtime
    的 UoW 绑定（Gate Q7：消失于 Runtime 接线，只保留组合根 OSS 缺省语义）。"""
    runtime_components = {
        "RuntimeCoordinator", "PersistingCoordinator",
        "Worker",      # 12.9.4.6：执行者同属 Runtime 管线，必须同一 UoW factory
        *EXPECTED_EXECUTORS_ONCE,
    }
    calls = [c for c in _calls_in_module(_create_app_tree())
             if _dotted(c.func) in runtime_components]
    uow_values = set()
    for c in calls:
        for kw in c.keywords:
            if kw.arg == "uow_factory":
                uow_values.add(ast.unparse(kw.value))
    assert uow_values == {"runtime_uow_factory"}, \
        f"Runtime 组件的 UoW factory 必须单一贯穿：{uow_values}"
    src = MAIN_PY.read_text(encoding="utf-8")
    # Cloud-0.1.3.3：per-tenant 绑定 = `partial(UoW, tenant_id=tenant_id)`（B §7 桥）；
    # bootstrap 定位读 = `tenant_id=None`；TENANT_LOCAL 不进 Runtime 的 UoW 绑定。
    assert ("runtime_uow_factory = functools.partial("
            "SqlAlchemyUnitOfWork, tenant_id=tenant_id)") in src
    assert "partial(\n        SqlAlchemyUnitOfWork, tenant_id=None)" in src
    assert "tenant_id=TENANT_LOCAL" not in src


def test_production_inbound_service_wiring_matches_test_composition() -> None:
    """ChannelInboundService 恰好一次，三依赖（uow_factory/intake/inbound_adapter）
    与 test_step10_2_1 装配同形；经 build_channel_inbound_api(service, worker) 挂载。

    12.9.4.6：`runtime` → `intake`——intake 只入队（PENDING + COMMIT），
    执行权归 Worker；router 同时收到 worker，二者原子装配。
    """
    calls = _calls_in_create_app(_create_app_tree())
    service_calls = [c for c in calls if _dotted(c.func) == "ChannelInboundService"]
    assert len(service_calls) == 1
    # Cloud-0.1.3.1：入站边界新增 tenant_scoped_factory（租户解析点）——
    # uow_factory = bootstrap 定位读（无 tenant 过滤）；tenant_scoped_factory
    # = 已认证 tenant 的可信写入工厂。三依赖（intake/inbound_adapter 等）不变。
    assert set(_kwargs(service_calls[0])) == {
        "uow_factory", "intake", "inbound_adapter", "tenant_scoped_factory"}
    assert _kwargs(service_calls[0])["uow_factory"] == "bootstrap_uow_factory"
    assert _kwargs(service_calls[0])["tenant_scoped_factory"] == \
        "tenant_scoped_uow_factory"
    src = MAIN_PY.read_text(encoding="utf-8")
    # Cloud-0.1.3.3：Trigger 1 经 worker_for 落到已认证租户的 Worker
    assert "worker_for=_worker_for))" in src
    assert "app.include_router(build_channel_inbound_api(" in src
    # 静态 mount 仍最后注册（API 优先，11.5 冻结）
    include_positions = [src.index(tok) for tok in (
        "worker_for=_worker_for))",
        "StaticFiles(directory")]
    assert include_positions == sorted(include_positions), "webhook 路由应先于 mount"


# ================================================================== 组合根零业务逻辑


def test_composition_root_has_no_second_runtime_logic() -> None:
    """main.py 只做装配：无新类、除 create_app 外无新函数；不构造 domain 对象、
    不调用 wire 函数、不出现 stage 编排分支——不产生第二套 Runtime 逻辑。"""
    tree = _create_app_tree()
    classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    assert not classes, f"组合根不应定义类：{[c.name for c in classes]}"
    # Cloud-0.1.3.3：`_build_tenant_runtime` 是 per-tenant 组合的唯一构造点
    # （0.1.3.2 Gate F4）——仍是纯装配函数，不是第二套 Runtime 逻辑。
    top_funcs = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
    assert top_funcs == ["_build_tenant_runtime", "create_app"], \
        f"组合根函数面漂移：{top_funcs}"

    src = MAIN_PY.read_text(encoding="utf-8")
    # 不构造 Runtime 域对象（InboundInteraction/AgentRequest/OutboundResponse/
    # AgentResponse 由 Adapter/Executor 产生，组合根绝不经手）
    for domain_obj in ("InboundInteraction(", "AgentRequest(", "OutboundResponse(",
                       "AgentResponse("):
        assert domain_obj not in src, f"组合根出现域对象构造：{domain_obj}"
    # 不调用 WeCom wire 函数（wire 全部归 Adapter，10.2.1 冻结）
    for wire in ("compute_signature", "encrypt_message", "decrypt_message",
                 "parse_xml_fields"):
        assert wire not in src, f"组合根出现 wire 逻辑：{wire}"
    # 不 import domain / runtime 边界模块（装配只消费组件与工厂）
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith("app.domain"), \
                f"组合根不应 import domain：{node.module}"


# ================================================================== 生产行为探针（内存 UoW 注入）


AES_KEY = "0" * 43  # 43 字符 EncodingAESKey（合法 base64 → 32B AESKey）
CORP_ID = "ww_test_corp"
TOKEN = "test-token"


@pytest.fixture()
def production_client(monkeypatch):
    """生产组合 + 内存 UoW：把 main.SqlAlchemyUnitOfWork 指向内存库后重建 app。

    这是「生产装的就是测试那套组件」的行为级证明——除 UoW 绑定外，
    HTTP → Service → 真实 WeComAdapter 全部是生产路径，不触生产库与网络。
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)

    def uow_factory(**kwargs):
        # 生产组合根以 functools.partial(UoW, tenant_id=...) 绑定租户
        # （Cloud-0.1.1-B）；替身须接受同一 kwarg。
        return SqlAlchemyUnitOfWork(
            session_factory=factory, tenant_id=kwargs.get("tenant_id"))

    import app.main as app_main
    monkeypatch.setattr(app_main, "SqlAlchemyUnitOfWork", uow_factory)
    client = TestClient(app_main.create_app())

    # 预置一个 active WeCom 渠道（经生产 UoW 写入，与管理面同表）
    with uow_factory() as uow:
        channel = uow.channels.create(Channel(
            name="wecom-prod", type="wecom",
            config={"token": TOKEN, "encoding_aes_key": AES_KEY,
                    "corp_id": CORP_ID}))
        uow.commit()

    yield client, channel.id
    engine.dispose()


def _signed_echo(plaintext: str, *, timestamp="1710000000", nonce="nonce-1") -> dict:
    encrypt = encrypt_message(plaintext, AES_KEY, CORP_ID)
    return {
        "msg_signature": compute_signature(TOKEN, timestamp, nonce, encrypt),
        "timestamp": timestamp,
        "nonce": nonce,
        "echostr": encrypt,
    }


def test_production_handshake_verifies_and_echoes_plaintext(production_client) -> None:
    """GET webhook：生产组合完成真实 WeCom 握手（验签 + 解密 → 明文回显）。"""
    client, channel_id = production_client
    plaintext = "echo-plaintext-123"
    resp = client.get(
        f"/api/channels/{channel_id}/wecom/webhook", params=_signed_echo(plaintext))
    assert resp.status_code == 200
    assert resp.text == plaintext


def test_production_unknown_channel_maps_to_500(production_client) -> None:
    """未知渠道 → InboundChannelConfigError → 生产边界映射 500（10.2.1 裁决表）。"""
    client, _ = production_client
    resp = client.get(
        "/api/channels/99999/wecom/webhook", params=_signed_echo("x"))
    assert resp.status_code == 500
    assert resp.json()["detail"] == "channel inbound misconfigured"


def test_production_bad_signature_maps_to_403(production_client) -> None:
    """坏签名 → InboundSignatureError → 生产边界映射 403（拒绝不可信请求）。"""
    client, channel_id = production_client
    params = _signed_echo("x")
    params["msg_signature"] = "0" * 40
    resp = client.get(f"/api/channels/{channel_id}/wecom/webhook", params=params)
    assert resp.status_code == 403
    assert resp.json()["detail"] == "inbound authentication failed"


def test_production_non_text_message_acknowledged_ignored(production_client) -> None:
    """POST 非 text 消息（真实 wire 签名/加密）：Adapter acknowledge-ignore
    → 生产边界 200 空体，不进 Runtime（MVP 冻结：仅 text 产生 CP 交互）。"""
    client, channel_id = production_client
    inner = ("<xml><ToUserName><![CDATA[ww_corp]]></ToUserName>"
             "<FromUserName><![CDATA[wx_a]]></FromUserName>"
             "<CreateTime>1710000000</CreateTime>"
             "<MsgType><![CDATA[event]]></MsgType>"
             "<Event><![CDATA[subscribe]]></Event></xml>")
    encrypt = encrypt_message(inner, AES_KEY, CORP_ID)
    timestamp, nonce = "1710000000", "nonce-2"
    resp = client.post(
        f"/api/channels/{channel_id}/wecom/webhook",
        params={
            "msg_signature": compute_signature(TOKEN, timestamp, nonce, encrypt),
            "timestamp": timestamp,
            "nonce": nonce,
        },
        content=f"<xml><Encrypt><![CDATA[{encrypt}]]></Encrypt></xml>".encode(),
    )
    assert resp.status_code == 200
    assert resp.content == b""
