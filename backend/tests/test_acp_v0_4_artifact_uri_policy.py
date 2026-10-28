"""ACP V0.4 Phase 2 —— Artifact URI 策略与安全呈现（实施验收）。

对应契约：`docs/step-acp-v0.4-artifact-uri-policy.md`。

覆盖：
- URI 分类规则（协议 / 绝对性 / userinfo / 主机 / 端口 / 结构 / 长度 / 字符类别）；
- 呈现层的「完整呈现 or 整体省略」纪律（被拒绝 URI **不输出任何片段**）；
- `mime_type` / `size_bytes` 的「Agent 声明值，未验证」标注；
- 零网络调用、纯 stdlib 导入边界、异常输入 fail closed；
- 快照与 Local outbox 的**原值保真**（呈现层收紧**不得**回溯影响持久化层）。

不修改 Phase 1 的 47 条验收标准；Phase 1 的既有断言仅在**本阶段明确行为变化**处
做最小调整（见实施报告的逐项列明）。
"""

import ast
import json
import socket
import sys
import urllib.request
from pathlib import Path

import pytest

from app.adapters.channels.local import LocalChannelAdapter
from app.application.agent_dispatch import AgentDispatchExecutor
from app.application.coordinator import RuntimeCoordinator
from app.application.delivery import DeliveryExecutor
from app.application.errors import AgentResponseInvalidError
from app.application.interaction_recording import PersistingCoordinator
from app.domain.agent import Agent
from app.domain.agent_response import AgentResponse
from app.domain.artifact import (
    ARTIFACT_KIND_DATA,
    ARTIFACT_KIND_FILE,
    ARTIFACT_KIND_IMAGE,
    ARTIFACT_KIND_LINK,
    Artifact,
)
from app.domain.artifact_reference import (
    render_artifact_references,
    render_outbound_content,
)
from app.domain.artifact_uri_policy import (
    ALLOWED_URI_SCHEMES,
    DEFAULT_MAX_URI_LENGTH,
    REASON_HOST,
    REASON_NOT_ABSOLUTE,
    REASON_NO_REFERENCE,
    REASON_PORT,
    REASON_SCHEME,
    REASON_STRUCTURE,
    REASON_TOO_LONG,
    REASON_UNSAFE_CHARS,
    REASON_USERINFO,
    URI_OMISSION_MESSAGES,
    classify_artifact_uri,
    unsafe_character_kind,
)
from app.domain.channel import Channel
from app.domain.connection import Connection
from app.domain.connection_conversation import ConnectionConversation
from app.domain.identity import Identity
from app.domain.inbound_interaction import InboundInteraction
from app.domain.outbound_response import OutboundResponse
from app.domain.user import User
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

APP_DIR = Path(__file__).resolve().parents[1] / "app"
POLICY_PY = APP_DIR / "domain" / "artifact_uri_policy.py"

#: 被拒绝的 URI 及其**稳定**原因码。
REJECTED_CASES = [
    ("javascript:alert(1)", REASON_SCHEME),
    ("JavaScript:alert(1)", REASON_SCHEME),
    ("data:text/html;base64,PHNjcmlwdD4=", REASON_SCHEME),
    ("file:///etc/passwd", REASON_SCHEME),
    ("ftp://example.com/x", REASON_SCHEME),
    ("gopher://example.com/x", REASON_SCHEME),
    ("ws://example.com/x", REASON_SCHEME),
    ("mailto:someone@example.com", REASON_SCHEME),
    ("https://user:pass@example.com/x", REASON_USERINFO),
    ("https://user@example.com/x", REASON_USERINFO),
    ("/relative/path", REASON_NOT_ABSOLUTE),
    ("relative/path", REASON_NOT_ABSOLUTE),
    ("//example.com/x", REASON_NOT_ABSOLUTE),
    ("https:///no-host", REASON_HOST),
    ("https://:8080/x", REASON_HOST),
    ("https://example.com:0/x", REASON_PORT),
    ("https://example.com:99999/x", REASON_PORT),
    ("https://example.com:abc/x", REASON_PORT),
    ("http://[::1/x", REASON_STRUCTURE),
    ("https://example.com\\path", REASON_STRUCTURE),
    ("https://example.com/a b", REASON_STRUCTURE),
]


def _artifact(**overrides) -> Artifact:
    payload = {
        "artifact_id": "a-1",
        "kind": ARTIFACT_KIND_FILE,
        "name": "spec.pdf",
        "uri": "https://example.com/spec.pdf",
        "mime_type": "application/pdf",
        "size_bytes": 1234,
        "metadata": {"rev": 2},
    }
    payload.update(overrides)
    return Artifact(**payload)


# ================================================================== A. 分类规则


def test_a1_normal_https_is_presentable() -> None:
    """正常 HTTPS → 可呈现。"""
    verdict = classify_artifact_uri("https://example.com/a%20b/c?x=1&y=2#frag")
    assert verdict.presentable is True
    assert verdict.reason is None


def test_a2_normal_http_is_presentable() -> None:
    """正常 HTTP → 可呈现（默认允许 http 以兼容 Phase 1 的普通 HTTP(S) URI）。"""
    assert classify_artifact_uri("http://example.com/x").presentable is True


def test_a3_scheme_case_is_normalised() -> None:
    """scheme 大小写 → 归一后判定（`HTTPS` / `HTTP` 与小写等价）。"""
    for uri in ("HTTPS://EXAMPLE.COM/x", "Http://Example.com/x",
                "https://example.com/x"):
        assert classify_artifact_uri(uri).presentable is True


def test_a4_dangerous_and_other_schemes_are_rejected() -> None:
    """`javascript:` / `data:` / `file:` 及其他非 HTTP(S) scheme → 拒绝。"""
    for uri, reason in REJECTED_CASES:
        if reason != REASON_SCHEME:
            continue
        verdict = classify_artifact_uri(uri)
        assert verdict.presentable is False, uri
        assert verdict.reason == reason, uri


def test_a5_userinfo_is_rejected() -> None:
    """含用户名 / 密码的 URL → 拒绝（凭据不得随引用外泄）。"""
    for uri in ("https://user:pass@example.com/x", "https://user@example.com/x",
                "http://u:p@example.com:8080/x"):
        verdict = classify_artifact_uri(uri)
        assert verdict.presentable is False, uri
        assert verdict.reason == REASON_USERINFO, uri


def test_a6_relative_url_is_rejected() -> None:
    """相对 URL / 协议相对 URL → 拒绝。"""
    for uri in ("/relative/path", "relative/path", "//example.com/x"):
        verdict = classify_artifact_uri(uri)
        assert verdict.presentable is False, uri
        assert verdict.reason == REASON_NOT_ABSOLUTE, uri


def test_a7_missing_host_is_rejected() -> None:
    """缺主机 → 拒绝。"""
    for uri in ("https:///no-host", "https://:8080/x"):
        verdict = classify_artifact_uri(uri)
        assert verdict.presentable is False, uri
        assert verdict.reason == REASON_HOST, uri


def test_a8_invalid_and_out_of_range_ports_are_rejected() -> None:
    """非法端口 / 端口越界 → 拒绝。"""
    for uri in ("https://example.com:0/x", "https://example.com:99999/x",
                "https://example.com:abc/x"):
        verdict = classify_artifact_uri(uri)
        assert verdict.presentable is False, uri
        assert verdict.reason == REASON_PORT, uri


def test_a9_valid_port_range_is_accepted() -> None:
    """合法端口（含 1 与 65535）→ 可呈现；0 与 65536 → 拒绝。"""
    assert classify_artifact_uri("https://example.com:8443/x").presentable is True
    assert classify_artifact_uri("https://example.com:1/x").presentable is True
    assert classify_artifact_uri("https://example.com:65535/x").presentable is True
    assert classify_artifact_uri("https://example.com:65536/x").presentable is False


def test_a10_ipv6_normal_and_abnormal_forms() -> None:
    """IPv6 正常形式 → 可呈现；括号不配对 / 非法字面量 → 拒绝。"""
    assert classify_artifact_uri("http://[::1]/x").presentable is True
    assert classify_artifact_uri("http://[2001:db8::1]:8080/x").presentable is True
    for uri in ("http://[::1/x", "http://[not-an-ip]/x"):
        verdict = classify_artifact_uri(uri)
        assert verdict.presentable is False, uri


def test_a11_unsafe_character_categories_are_rejected() -> None:
    """`Cc` / `Cf` / `Zl` / `Zp` 字符 → 拒绝（不可展示字符）。"""
    for ch in ("\r", "\n", "\t", "\x00", "\x1b", "\u200b", "\u202e", "\u00ad",
               "\u2028", "\u2029"):
        uri = "https://example.com/a" + ch + "b"
        verdict = classify_artifact_uri(uri)
        assert verdict.presentable is False, repr(ch)
        assert verdict.reason == REASON_UNSAFE_CHARS, repr(ch)
    assert unsafe_character_kind("a\u2028b") == "separator"
    assert unsafe_character_kind("a\nb") == "control"
    assert unsafe_character_kind("a\u200bb") == "control"
    assert unsafe_character_kind("plain") is None


def test_a12_too_long_uri_is_rejected_without_truncation() -> None:
    """超长 URI → 拒绝（**不截断**）。"""
    uri = "https://example.com/" + "z" * 3000
    verdict = classify_artifact_uri(uri)
    assert verdict.presentable is False
    assert verdict.reason == REASON_TOO_LONG
    limit = DEFAULT_MAX_URI_LENGTH
    exact = "https://example.com/" + "z" * (limit - len("https://example.com/"))
    assert len(exact) == limit
    assert classify_artifact_uri(exact).presentable is True
    assert classify_artifact_uri(exact + "z").presentable is False


def test_a13_none_and_blank_are_no_reference() -> None:
    """`None` / 纯空白 / 非 str → `no_reference`（沿用 Phase 1「未提供可用引用」语义）。"""
    for value in (None, "", "   ", "\t", 123, object(), ["x"]):
        verdict = classify_artifact_uri(value)
        assert verdict.presentable is False, repr(value)
        assert verdict.reason == REASON_NO_REFERENCE, repr(value)


def test_a14_abnormal_inputs_fail_closed() -> None:
    """异常输入 → **fail closed**（拒绝呈现），**不抛异常**。"""
    for value in ("http://", "https://", "://x", "https://[", "https://%zz",
                  "https://example.com:%", "https://exa mple.com/x", "\\",
                  "https://example.com/" + "z" * 100000):
        verdict = classify_artifact_uri(value)     # 不抛
        assert verdict.presentable is False, repr(value)


def test_a15_allowed_scheme_set_is_fixed_and_safe() -> None:
    """允许的 scheme 集合是**固定安全策略**（仅 http/https），不含危险协议。"""
    assert ALLOWED_URI_SCHEMES == frozenset({"https", "http"})
    for scheme in ("javascript", "data", "file", "ftp", "gopher", "ws", "mailto"):
        assert scheme not in ALLOWED_URI_SCHEMES


def test_a16_kind_does_not_invent_uri_requirements() -> None:
    """既有契约**未**定义 per-kind 的 URI 格式 ⇒ 策略对所有 kind **一致**。

    依据：`app/domain/artifact.py` 的字段语义只规定 `kind` 是类型判据、`uri` 是
    「可解析位置」；**未**为 `data` / `file` 规定不同的 URI 形态。
    """
    for kind in (ARTIFACT_KIND_FILE, ARTIFACT_KIND_DATA,
                 ARTIFACT_KIND_LINK, ARTIFACT_KIND_IMAGE):
        assert classify_artifact_uri("https://example.com/x").presentable is True
        assert classify_artifact_uri("javascript:alert(1)").reason == REASON_SCHEME


def test_a17_classification_is_deterministic() -> None:
    """同输入两次判定**完全一致**。"""
    for uri in ("https://example.com/x", "javascript:alert(1)", "https://u:p@e/x"):
        assert classify_artifact_uri(uri) == classify_artifact_uri(uri)


# ================================================================== B. 呈现层


def test_b1_rejected_uri_outputs_no_fragment() -> None:
    """被拒绝的 URI → **不输出 URI 本身或任何片段**。"""
    for uri, _reason in REJECTED_CASES:
        out = render_artifact_references([_artifact(uri=uri)])
        assert uri not in out, uri
        for fragment in (uri[:12], uri[-12:]):
            if len(fragment) >= 6:
                assert fragment not in out, (uri, fragment)
        assert "引用未在消息中呈现" in out, uri


#: Phase 1 的标记字符闸门文案（**更贴合**「可能拼出链接 / HTML 结构」）。
PHASE1_MARKUP_MESSAGE = "引用包含不适合展示的字符"


def test_b2_rejected_uri_gives_explicit_reason() -> None:
    """被拒绝 → 明确原因提示。

    闸门优先级：Phase 1 的**字符 / 标记**闸门在前（其文案更贴近「可能拼出链接
    结构」），否则用**策略原因码**对应文案。二者都**不含** URI 片段。
    """
    for uri, reason in REJECTED_CASES:
        out = render_artifact_references([_artifact(uri=uri)])
        if any(ch in uri for ch in "[]<>`"):
            assert PHASE1_MARKUP_MESSAGE in out, (uri, reason)
        else:
            assert URI_OMISSION_MESSAGES[reason] in out, (uri, reason)


def test_b3_presentable_uri_keeps_verbatim_and_unverified_notice() -> None:
    """可呈现 URI → **逐字节原样** + 「未验证」语义 + 无链接语法。"""
    uri = "https://example.com/a%20b/c?x=1&y=2#frag"
    out = render_artifact_references([_artifact(uri=uri)])
    assert uri in out
    assert "未验证" in out
    assert "](" not in out and "<a" not in out


def test_b4_declared_values_are_marked_unverified() -> None:
    """`mime_type` / `size_bytes` 呈现时 → 标注「Agent 声明值，未验证」。"""
    out = render_artifact_references([
        _artifact(mime_type="application/pdf", size_bytes=1234)])
    assert "Agent 声明值，未验证" in out
    plain = render_artifact_references([
        _artifact(mime_type=None, size_bytes=None)])
    assert "Agent 声明值，未验证" not in plain


def test_b5_none_uri_keeps_phase1_semantics() -> None:
    """`uri=None` → 沿用 Phase 1「未提供可用引用」语义（不抛异常、无空链接）。"""
    out = render_artifact_references([_artifact(uri=None)])
    assert "未提供可用引用" in out
    assert "``" not in out


def test_b6_multiple_artifacts_keep_order_and_count() -> None:
    """多 Artifact → 顺序稳定 + 省略计数可见。"""
    artifacts = [
        _artifact(artifact_id="a-1", name="one.txt", uri="https://example.com/1"),
        _artifact(artifact_id="a-2", name="two.js", uri="javascript:alert(1)"),
        _artifact(artifact_id="a-3", name="three.txt", uri="https://example.com/3"),
    ]
    out = render_artifact_references(artifacts)
    assert out.index("one.txt") < out.index("two.js") < out.index("three.txt")
    assert "1 条引用的地址未在消息中呈现" in out


def test_b7_no_artifact_leaves_content_byte_identical() -> None:
    """无 Artifact → 出站 content **逐字节**不变。"""
    assert render_outbound_content("hi", None) == "hi"
    assert render_outbound_content("hi", []) == "hi"


def test_b8_renderer_makes_zero_network_calls(monkeypatch) -> None:
    """呈现与判定 **零网络调用**。"""

    def _boom(*args, **kwargs):
        raise AssertionError("呈现/判定触发了网络调用")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    out = render_artifact_references([
        _artifact(uri="https://example.com/x"),
        _artifact(artifact_id="a-2", uri="javascript:alert(1)"),
    ])
    assert "https://example.com/x" in out


# ================================================================== C. 导入边界


def test_c1_policy_module_is_stdlib_only() -> None:
    """策略模块 import 根**仅 stdlib**（无 I/O / 框架 / 仓内依赖）。"""
    tree = ast.parse(POLICY_PY.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    assert roots <= set(sys.stdlib_module_names), roots
    assert not any(r.startswith("app") for r in roots), roots


def test_c2_policy_module_does_no_io() -> None:
    """策略模块**不**出现任何网络 / 文件 / 进程 / 配置调用。"""
    source = POLICY_PY.read_text(encoding="utf-8")
    for forbidden in ("urlopen", "requests", "httpx", "socket", "subprocess",
                      "open(", "os.environ", "get_settings"):
        assert forbidden not in source, forbidden


# ================================================================== D. 原值保真


def test_d1_snapshot_keeps_rejected_uri_verbatim(db_engine) -> None:
    """被策略拒绝的 URI 在**快照**中仍是**完整原值**（呈现层收紧不回溯）。"""
    g = _seed(db_engine)
    raw = "javascript:alert(1)"
    artifacts = [_artifact(uri=raw, metadata={"k": "v"})]
    _runtime(db_engine, agent=_ArtifactAgent(artifacts)).execute(
        _inbound(g["channel"]))
    snapshot = _response_payload(db_engine)
    assert snapshot["artifacts"][0]["uri"] == raw
    assert snapshot["artifacts"][0]["metadata"] == {"k": "v"}


def test_d2_outbox_keeps_rejected_uri_verbatim(tmp_path) -> None:
    """被策略拒绝的 URI 在 **Local outbox** 中仍是**完整原值**。"""
    raw = "javascript:alert(1)"
    outbox = tmp_path / "o.jsonl"
    LocalChannelAdapter(outbox_path=outbox).deliver(
        _outbound("hi", artifacts=[_artifact(uri=raw)]))
    records = [ln for ln in outbox.read_text(encoding="utf-8").split("\n")
               if ln.strip()]
    record = json.loads(records[0])
    assert record["artifacts"][0]["uri"] == raw
    assert record["content"] == "hi"


def test_d3_invalid_artifact_never_reaches_outbox(db_engine, tmp_path) -> None:
    """非法 artifact → dispatch Fail Closed → outbox **无新行**（既有纪律不变）。"""
    outbox = tmp_path / "o.jsonl"
    g = _seed(db_engine)
    runtime = _runtime(db_engine, agent=_BadArtifactAgent(),
                       sink=LocalChannelAdapter(outbox_path=outbox))
    with pytest.raises(AgentResponseInvalidError):
        runtime.execute(_inbound(g["channel"]))
    assert not outbox.exists() or outbox.read_text(encoding="utf-8").strip() == ""


# ================================================================== Runtime 装配


class _ArtifactAgent:
    def __init__(self, artifacts) -> None:
        self._artifacts = artifacts

    def send(self, request, connection) -> AgentResponse:  # noqa: ARG002
        return AgentResponse(content=f"echo:{request.content}",
                             artifacts=self._artifacts)


class _BadArtifactAgent:
    def send(self, request, connection) -> AgentResponse:  # noqa: ARG002
        return AgentResponse(
            content=f"echo:{request.content}",
            artifacts=[Artifact(artifact_id="", kind="file", name="n")])


class _RecordingSink:
    def __init__(self) -> None:
        self.deliveries: list[OutboundResponse] = []

    def deliver(self, outbound: OutboundResponse) -> None:
        self.deliveries.append(outbound)


def _outbound(content: str = "hello", artifacts=None) -> OutboundResponse:
    return OutboundResponse(channel_id=1, external_conversation_id="conv-1",
                            content=content, artifacts=artifacts)


def _uow_factory(db_engine):
    from sqlalchemy.orm import sessionmaker
    return lambda: SqlAlchemyUnitOfWork(
        session_factory=sessionmaker(bind=db_engine, expire_on_commit=False))


def _seed(db_engine) -> dict:
    with _uow_factory(db_engine)() as uow:
        alice = uow.users.create(User(name="alice"))
        channel = uow.channels.create(Channel(name="wecom-1", type="wecom"))
        agent = uow.agents.create(Agent(name="agent-a", type="acp"))
        ident = uow.identities.create(Identity(
            user_id=alice.id, channel_id=channel.id, external_id="wx_a"))
        path = uow.connections.create(Connection(
            channel_id=channel.id, agent_id=agent.id, name="path-a",
            status="active",
            config={"endpoint": "http://agent.internal", "timeout_s": 5.0},
            credential_ref="cred-a"))
        uow.connection_conversations.create(ConnectionConversation(
            channel_id=channel.id, external_conversation_id="conv-1",
            connection_id=path.id))
        uow.commit()
        return {"channel": channel.id, "identity": ident.id}


def _runtime(db_engine, *, agent=None, sink=None):
    from app.application.access_authorization import AccessAuthorizationExecutor
    from app.application.identity_resolution import IdentityResolutionExecutor
    from app.application.routing import RoutingExecutor
    from app.application.session_creation import SessionCreationExecutor
    from app.application.session_resolution import SessionResolutionExecutor
    uow = _uow_factory(db_engine)
    inner = RuntimeCoordinator(
        identity_resolution=IdentityResolutionExecutor(uow),
        access_authorization=AccessAuthorizationExecutor(uow),
        routing=RoutingExecutor(uow),
        session_resolution=SessionResolutionExecutor(uow),
        session_creation=SessionCreationExecutor(uow),
        agent_dispatch=AgentDispatchExecutor(
            agent_adapter=agent or _ArtifactAgent(None), uow_factory=uow),
        delivery=DeliveryExecutor(channel_adapter=sink or _RecordingSink()),
    )
    return PersistingCoordinator(inner=inner, uow_factory=_uow_factory(db_engine))


def _inbound(channel_id: int) -> InboundInteraction:
    return InboundInteraction(
        channel_id=channel_id, external_interaction_id="msg-1",
        external_conversation_id="conv-1", external_identity_id="wx_a",
        content="hello")


def _response_payload(db_engine) -> dict:
    from sqlalchemy.orm import Session as SASession
    from app.persistence.models.interaction import InteractionModel
    with SASession(db_engine) as session:
        row = session.query(InteractionModel).order_by(
            InteractionModel.id).first()
        return row.response_payload

