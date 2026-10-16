"""Step 10.2.1 Layer 2 —— Inbound Adapter 契约测试（Application ↔ Port ↔ Runtime）。

不用 WeCom / 不用 HTTP：用 Fake Adapter 验证 **Application 层的编排契约**：

    Channel（Repository Port）
        ↓
    ChannelInboundService（application/channel_inbound.py）
        ↓ InboundChannelAdapter（Port）
        ↓ InboundInteraction
        ↓ RuntimeCoordinator（Runtime Core）

覆盖（10.2.1 §测试分层 · Layer 2）：
    ✓ CP contract（channel 定位 / None 语义 / Runtime 交付 / 字段透传）
    ✓ no HTTP dependency（AST：application 模块 import 白名单）
    ✓ no Runtime dependency 反向（Runtime 错误原样传播，不被 application 翻译）
    ✓ no DB dependency beyond Repository Port（UoW Port 注入）
"""

import ast
from pathlib import Path

import pytest

from app.application.channel_inbound import ChannelInboundService
from app.application.errors import RoutingFailedError
from app.application.interaction_recording import PersistingCoordinator
from app.application.worker import Worker
from app.domain.channel import Channel
from app.domain.inbound_interaction import InboundInteraction
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork
from app.repositories.channel_inbound import (
    InboundSignatureError,
    InboundChannelConfigError,
    InboundFormatError,
)

# ------------------------------------------------------------------ Fake 组件


class _RecordingRuntime:
    """记录型 Runtime：只记录收到的 InboundInteraction（或按脚本抛错）。"""

    def __init__(self, error: Exception | None = None) -> None:
        self.inbounds: list[InboundInteraction] = []
        self._error = error

    def execute(self, inbound: InboundInteraction) -> None:
        if self._error is not None:
            raise self._error
        self.inbounds.append(inbound)


class _ScriptedInboundAdapter:
    """协议无关 Fake Adapter：可编程结果（InboundInteraction / None / 异常 / 工厂）。"""

    def __init__(self, result) -> None:
        self.result = result
        self.calls: list[tuple[str, int | None]] = []

    def verify_handshake(self, channel, **kwargs) -> str:
        self.calls.append(("handshake", channel.id))
        return "plain-echo-from-adapter"

    def parse_message(self, channel, **kwargs):
        self.calls.append(("message", channel.id))
        if isinstance(self.result, Exception):
            raise self.result
        if callable(self.result):
            return self.result(channel)
        return self.result


class _StubInner:
    """PersistingCoordinator 的 inner 占位（同步包装遗留）；入站链路不触发它。"""

    def execute(self, inbound: InboundInteraction) -> None:
        return None


def _inbound() -> InboundInteraction:
    return InboundInteraction(
        channel_id=7, external_interaction_id="m-1",
        external_conversation_id="conv-1", external_identity_id="zhangsan",
        content="hello")


def _uow_factory(db_engine):
    return lambda: SqlAlchemyUnitOfWork(
        session_factory=__import__("sqlalchemy.orm", fromlist=["sessionmaker"])
        .sessionmaker(bind=db_engine, expire_on_commit=False))


def _seed_channel(db_engine, *, status: str = "active") -> int:
    with _uow_factory(db_engine)() as uow:
        channel = uow.channels.create(Channel(name="wecom-1", type="wecom", status=status))
        uow.commit()
        return channel.id


def _service(db_engine, adapter, intake=None) -> ChannelInboundService:
    """12.9.4.6：入站服务注入的是 **intake**（PENDING 入队），不再是 runtime。"""
    uow = _uow_factory(db_engine)
    if intake is None:
        intake = PersistingCoordinator(inner=_StubInner(), uow_factory=uow)
    return ChannelInboundService(
        uow_factory=uow, intake=intake, inbound_adapter=adapter,
    )


# ================================================================== A. 用例契约


def test_a1_handle_message_enqueues_inbound_as_pending(db_engine) -> None:
    """12.9.4.6：text 消息 → Adapter 归一化 → intake 落一条 **PENDING**（不直连 Runtime）。"""
    channel_id = _seed_channel(db_engine)
    # Fake Adapter 基于真实传入的 channel 构造契约（同时验证 channel 被正确传递）
    adapter = _ScriptedInboundAdapter(lambda ch: InboundInteraction(
        channel_id=ch.id, external_interaction_id="m-1",
        external_conversation_id="conv-1", external_identity_id="zhangsan",
        content="hello"))
    service = _service(db_engine, adapter)

    interaction_id = service.handle_message(
        channel_id, signature="sig", timestamp="t", nonce="n", body="body")

    assert adapter.calls == [("message", channel_id)]      # 真实 channel 被传入 Adapter
    assert interaction_id is not None
    with _uow_factory(db_engine)() as uow:
        row = uow.interactions.get_by_id(interaction_id)
    assert row is not None
    assert row.processing_status == "pending"              # 只入队，尚未执行
    assert row.request_payload["external_interaction_id"] == "m-1"
    assert row.request_payload["content"] == "hello"


def test_a2_none_result_never_enqueues(db_engine) -> None:
    """Adapter 返回 None（unsupported MsgType）→ acknowledge-ignore，不落任何行。"""
    channel_id = _seed_channel(db_engine)
    adapter = _ScriptedInboundAdapter(None)
    assert _service(db_engine, adapter).handle_message(
        channel_id, signature="sig", timestamp="t", nonce="n", body="body") is None
    with _uow_factory(db_engine)() as uow:
        assert uow.interactions.find_pending() == []       # 队列未被污染


def test_a3_handle_handshake_is_passthrough(db_engine) -> None:
    """握手：Application 原样透传 Adapter 结果（明文 echostr），不做任何加工。"""
    channel_id = _seed_channel(db_engine)
    adapter = _ScriptedInboundAdapter(_inbound())
    result = _service(db_engine, adapter).handle_handshake(
        channel_id, signature="sig", timestamp="t", nonce="n", echostr="echo")
    assert result == "plain-echo-from-adapter"


def test_a4_adapter_errors_propagate_unwrapped(db_engine) -> None:
    """入站协议错误从 Adapter 原样传播（不翻译、不吞包、不变成 CP 错误码）。"""
    channel_id = _seed_channel(db_engine)
    service = _service(db_engine, _ScriptedInboundAdapter(InboundSignatureError("bad sig")))
    with pytest.raises(InboundSignatureError):
        service.handle_message(channel_id, signature="s", timestamp="t",
                               nonce="n", body="b")
    service2 = _service(db_engine, _ScriptedInboundAdapter(InboundFormatError("bad xml")))
    with pytest.raises(InboundFormatError):
        service2.handle_message(channel_id, signature="s", timestamp="t",
                                nonce="n", body="b")


def test_a5_runtime_errors_propagate_from_worker(db_engine) -> None:
    """12.9.4.6：Runtime typed error 不再经入站链路冒泡，改由 Worker.process 原样抛出
    （Application 仍不做二次判断，执行权已移交）。"""
    channel_id = _seed_channel(db_engine)
    runtime = _RecordingRuntime(error=RoutingFailedError(channel_id))
    uow = _uow_factory(db_engine)
    phases = PersistingCoordinator(inner=runtime, uow_factory=uow)
    worker = Worker(runtime=runtime, phases=phases, uow_factory=uow)
    # intake 现在真实落库 → inbound 必须携带真实存在的 channel_id（FK）
    service = _service(db_engine, _ScriptedInboundAdapter(lambda ch: InboundInteraction(
        channel_id=ch.id, external_interaction_id="m-1",
        external_conversation_id="conv-1", external_identity_id="zhangsan",
        content="hello")), phases)

    interaction_id = service.handle_message(
        channel_id, signature="s", timestamp="t", nonce="n", body="b")
    assert interaction_id is not None
    with pytest.raises(RoutingFailedError):
        worker.process(interaction_id)


# ================================================================== B. Channel 定位 Fail Closed


def test_b1_unknown_channel_rejected(db_engine) -> None:
    """channel_id 不存在 → InboundChannelConfigError（HTTP 500 语义）。"""
    with pytest.raises(InboundChannelConfigError):
        _service(db_engine, _ScriptedInboundAdapter(_inbound())).handle_message(
            99999, signature="s", timestamp="t", nonce="n", body="b")


def test_b2_inactive_channel_rejected(db_engine) -> None:
    """channel 未启用 → InboundChannelConfigError（禁用渠道不再承接回调）。"""
    channel_id = _seed_channel(db_engine, status="inactive")
    with pytest.raises(InboundChannelConfigError):
        _service(db_engine, _ScriptedInboundAdapter(_inbound())).handle_message(
            channel_id, signature="s", timestamp="t", nonce="n", body="b")


@pytest.mark.parametrize("bad_id", [0, -1])
def test_b3_illegal_channel_id_rejected(db_engine, bad_id: int) -> None:
    """非法 channel_id（0 / 负数）→ InboundChannelConfigError。"""
    with pytest.raises(InboundChannelConfigError):
        _service(db_engine, _ScriptedInboundAdapter(_inbound())).handle_message(
            bad_id, signature="s", timestamp="t", nonce="n", body="b")


# ================================================================== C. 依赖纯净（AST）


def test_c1_application_module_import_allowlist() -> None:
    """channel_inbound.py 的 import 面锁定为白名单——不知道 WeCom / HTTP / wire。"""
    module = Path(__file__).resolve().parents[1] / "app" / "application" / "channel_inbound.py"
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    allowed = {
        "collections.abc",
        "dataclasses",                             # Cloud-0.1.3.1：InboundAcceptance 值对象
        "app.application.interaction_recording",   # 12.9.4.6：intake（phase API）
        "app.core.tenant",                         # Cloud-0.1.3.1：TenantScope（隔离原语）
        "app.domain.channel",
        "app.repositories.channel_inbound",
        "app.repositories.unit_of_work",
    }
    assert imports <= allowed, f"application 入站模块出现白名单外 import：{imports - allowed}"


def test_c2_service_never_implements_wire_logic() -> None:
    """Application 源码不出现任何 wire 概念符号（SHA1/AES/XML/MsgType/wecom 函数）。"""
    module = Path(__file__).resolve().parents[1] / "app" / "application" / "channel_inbound.py"
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    forbidden = {"sha1", "SHA1", "aes", "AES", "decrypt", "encrypt",
                 "xml", "XML", "MsgType", "FromUserName", "wecom", "WeCom"}
    hits = names & forbidden
    assert not hits, f"Application 层出现 wire 概念符号：{hits}"
