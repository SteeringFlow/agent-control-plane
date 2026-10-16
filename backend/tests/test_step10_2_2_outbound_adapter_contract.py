"""Step 10.2.2 Layer B —— Outbound Adapter 契约测试。

验证 ChannelAdapter.deliver(outbound) 的**边界**（与 wire 行为正交）：

    ✓ deliver 返回 None（Port 语义：交付，非应答生成）
    ✓ WeComAdapter 不修改 OutboundResponse（frozen 契约）
    ✓ 不访问 Runtime / Session / persistence（AST import 白名单）
    ✓ 底层失败统一映射 ChannelDeliveryFailedError（不新造错误码）
    ✓ 与入站 Port 同实例共存（双 Port，无状态串扰）
"""

import ast
import dataclasses
from pathlib import Path

import pytest

from app.adapters.channels.wecom import WeComAdapter
from app.application.errors import ChannelDeliveryFailedError
from app.repositories.channel_adapter import ChannelAdapter
from app.repositories.channel_inbound import InboundChannelAdapter

WECOM_PY = Path(__file__).resolve().parents[1] / "app" / "adapters" / "channels" / "wecom.py"


def test_a1_we_adapter_satisfies_both_ports() -> None:
    """双 Port：出站 ChannelAdapter + 入站 InboundChannelAdapter。"""
    adapter = WeComAdapter()
    assert isinstance(adapter, ChannelAdapter)
    assert isinstance(adapter, InboundChannelAdapter)


def test_a2_outbound_response_is_frozen() -> None:
    """OutboundResponse frozen —— deliver 结构上不可能修改 outbound。"""
    from app.domain.outbound_response import OutboundResponse
    outbound = OutboundResponse(channel_id=1, external_conversation_id="x",
                                content="y")
    with pytest.raises(dataclasses.FrozenInstanceError):
        outbound.content = "mutated"


def test_b1_wecom_module_import_allowlist() -> None:
    """wecom.py import 面锁定：wire 依赖 + 端口 + 出站失败码，无 Runtime/persistence。"""
    tree = ast.parse(WECOM_PY.read_text(encoding="utf-8"), filename=str(WECOM_PY))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module)

    allowed_prefixes = (
        "base64", "binascii", "hashlib", "json", "os", "urllib",
        "xml", "collections.abc", "cryptography",
        "app.application.errors",
        "app.domain",
        "app.repositories",
    )
    bad = [r for r in sorted(roots)
           if not any(r == p or r.startswith(p + ".") for p in allowed_prefixes)]
    assert not bad, f"wecom.py 出现白名单外 import：{bad}"

    # Runtime 组件零接触（显式点名，防未来漂移）
    forbidden = {"app.persistence", "app.api", "app.schemas",
                 "app.application.coordinator", "app.application.runtime",
                 "app.application.identity_resolution",
                 "app.application.session_resolution",
                 "app.application.session_creation",
                 "app.application.routing",
                 "app.application.agent_dispatch",
                 "app.application.delivery"}
    hit = [r for r in roots if any(r == f or r.startswith(f + ".") for f in forbidden)]
    assert not hit, f"wecom.py 触碰 Runtime/persistence：{hit}"


def test_b2_no_new_error_codes_introduced() -> None:
    """出站失败只复用 ChannelDeliveryFailedError —— Port 契约（9.6）。

    AST 扫描（代码符号，不含 docstring —— 文档中解释 Port 契约的合法引用不算）。
    """
    tree = ast.parse(WECOM_PY.read_text(encoding="utf-8"), filename=str(WECOM_PY))
    symbols: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.add(node.name)
        elif isinstance(node, ast.Name):
            symbols.add(node.id)
        elif isinstance(node, ast.Attribute):
            symbols.add(node.attr)
    assert "ChannelDeliveryFailedError" in symbols
    for forbidden in ("ChannelNotFound", "ChannelAuthFailed", "ChannelTimeout",
                      "ChannelRateLimited", "ChannelTransportFailed"):
        assert forbidden not in symbols, f"新造底层错误码：{forbidden}"


def test_b3_deliver_semantics_is_delivery_not_response_generation() -> None:
    """deliver 是「交付」语义 —— 不构建/返回被动回复对象（10.2.2 裁决）。

    静态结构断言：deliver 方法体不引用 build_passive_reply。
    """
    tree = ast.parse(WECOM_PY.read_text(encoding="utf-8"), filename=str(WECOM_PY))
    deliver = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "deliver")
    names = {n.id for n in ast.walk(deliver) if isinstance(n, ast.Name)}
    assert "build_passive_reply" not in names, \
        "deliver 不得走被动回复路径（HTTP request 生命周期不属于 Port 语义）"


def test_b4_uow_factory_failure_maps_to_delivery_failed() -> None:
    """凭据解析阶段的任何异常（DB 等）→ ChannelDeliveryFailedError（不泄漏底层异常）。"""

    class _ExplodingUoW:
        def __enter__(self):
            raise RuntimeError("db down")

        def __exit__(self, *args):
            return False

    adapter = WeComAdapter(uow_factory=lambda: _ExplodingUoW())
    from app.domain.outbound_response import OutboundResponse
    with pytest.raises(ChannelDeliveryFailedError) as exc_info:
        adapter.deliver(OutboundResponse(
            channel_id=1, external_conversation_id="zhangsan", content="hi"))
    # 底层异常被收口（保留 __cause__ 用于诊断，但不向上泄漏类型）
    assert not isinstance(exc_info.value, RuntimeError)
