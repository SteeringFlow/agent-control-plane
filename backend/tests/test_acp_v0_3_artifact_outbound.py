"""ACP V0.3 Phase 1 —— Artifact 引用式出站交付（实施验收）。

对应契约与验收标准：`docs/step-acp-v0.3-artifact-outbound-implementation-gate.md`
（§6 安全 / §8 验收标准 / §9.3 实施约束 / §9.4 测试命令）。

本文件覆盖 Gate §8 的 **B / C / D / E** 组中可在本层直接验证的条目，以及
**C20 / C21**（持久化保真）。涉及**跨文件字段面**的条目（A1–A5）由既有的契约
护栏文件承担（`test_step8_6_delivery_contract.py` / `test_step9_A_runtime_slice_closure.py` /
`test_step10_1_1_acp_adapter_contract.py` / `test_step10_5_1_…` / `test_step10_5_3_…`），
本文件**不重复**它们。

核心契约要点：
- **引用式**交付：只呈现 Agent 提供的**引用**，**不取回字节**、不存储、不代理；
- **URI 原子性**：URI 要么**原样逐字节**呈现，要么**整体省略**并说明原因——
  **绝不**输出「被截断的 URI 片段」并把它暗示成可用地址；
- **展示预算不污染持久化**：快照与 Local outbox 的 `artifacts` 保留**完整原值**；
- **旧出站零变化**：无 artifact 时消息 / outbox 行**逐字节**不变。
"""

import ast
import json
import socket
import sys
import threading
import urllib.request
from pathlib import Path

import pytest

from app.adapters.channels.local import LocalChannelAdapter
from app.adapters.channels.wecom import WeComAdapter
from app.adapters.channels.wecom_bot import WeComBotAdapter
from app.application.agent_dispatch import AgentDispatchExecutor
from app.application.coordinator import RuntimeCoordinator
from app.application.delivery import DeliveryExecutor
from app.application.errors import AgentResponseInvalidError
from app.application.interaction_recording import PersistingCoordinator
from app.domain.agent import Agent
from app.domain.agent_response import AgentResponse
from app.domain.artifact import ARTIFACT_KIND_FILE, Artifact
from app.domain.artifact_reference import (
    ARTIFACT_REFERENCE_HEADER,
    MAX_DISPLAY_TEXT_CHARS,
    MAX_PRESENTED_ARTIFACTS,
    MAX_REFERENCE_BLOCK_CHARS,
    artifact_reference_records,
    render_artifact_references,
    render_outbound_content,
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

#: 完成态措辞黑名单——呈现文案**不得**声称任何已交付事实（C3）。
FORBIDDEN_CLAIMS = ("已上传", "已存储", "已下载", "已发送文件", "下载成功", "已验证")


# ================================================================== 夹具与工具


def _artifact(**overrides) -> Artifact:
    payload = {
        "artifact_id": "a-1",
        "kind": ARTIFACT_KIND_FILE,
        "name": "spec.pdf",
        "uri": "https://example/spec.pdf",
        "mime_type": "application/pdf",
        "size_bytes": 1234,
        "metadata": {"rev": 2},
    }
    payload.update(overrides)
    return Artifact(**payload)


def _outbound(content: str = "hello", artifacts=None, **overrides) -> OutboundResponse:
    payload = {
        "channel_id": 1,
        "external_conversation_id": "conv-1",
        "content": content,
        "artifacts": artifacts,
    }
    payload.update(overrides)
    return OutboundResponse(**payload)


#: WeCom 出站测试用的 Channel.id（与 `_wecom_channel()` 一致）。
WECOM_CHANNEL_ID = 11


class _FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args) -> bool:
        return False


class _FakeUrlOpen:
    """记录所有出站 HTTP 调用（无真实网络）。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        self.calls.append({
            "url": url,
            "method": req.get_method() if hasattr(req, "get_method") else "GET",
            "data": getattr(req, "data", None),
        })
        body = (b'{"errcode": 0, "access_token": "tok-1"}' if "gettoken" in url
                else b'{"errcode": 0}')
        return _FakeResponse(200, body)

    def sent_payloads(self) -> list[dict]:
        return [json.loads(c["data"].decode("utf-8"))
                for c in self.calls if c["data"]]


@pytest.fixture()
def fake_urlopen(monkeypatch):
    fake = _FakeUrlOpen()
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return fake


class _FakeChannelRepo:
    def __init__(self, channel: Channel | None) -> None:
        self._channel = channel

    def get_by_id(self, channel_id: int) -> Channel | None:
        if self._channel is not None and self._channel.id == channel_id:
            return self._channel
        return None


class _FakeUoW:
    def __init__(self, channel: Channel | None) -> None:
        self.channels = _FakeChannelRepo(channel)

    def __enter__(self) -> "_FakeUoW":
        return self

    def __exit__(self, *args) -> bool:
        return False

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass


def _wecom_channel(**overrides) -> Channel:
    config = {
        "token": "wire-token",
        "encoding_aes_key": "c" * 43,
        "corp_id": "wwcorp-out",
        "corpsecret": "corp-secret-out",
        "agentid": 1000002,
    }
    config.update(overrides)
    return Channel(name="wecom-out", type="wecom", id=11, config=config)


def _wecom_adapter(channel: Channel | None = None) -> WeComAdapter:
    return WeComAdapter(uow_factory=lambda: _FakeUoW(channel))


class _FakeAckSlot:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.event.set()
        self.payload = {"errcode": 0}


class _FakeBotRegistry:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def sender_for(self, channel_id: int):
        def _send(raw: str) -> None:
            self.sent.append(json.loads(raw))
        return _send

    def expect_ack(self, channel_id: int, req_id: str) -> _FakeAckSlot:
        return _FakeAckSlot()

    def forget_ack(self, channel_id: int, req_id: str) -> None:
        pass


def _bot_adapter() -> tuple[WeComBotAdapter, _FakeBotRegistry]:
    registry = _FakeBotRegistry()
    return WeComBotAdapter(registry=registry), registry


# ================================================================== C. 呈现与安全


def test_c01_no_markdown_or_html_link_syntax() -> None:
    """C1：输出**不含** `[text](url)` 形态、**不含** `<a href` / 任何 HTML 标签。"""
    hostile = _artifact(uri="https://e/x](https://evil/y")
    out = render_artifact_references([hostile])
    assert "](" not in out
    assert "<a href" not in out
    assert "<" not in out and ">" not in out
    normal = render_artifact_references([_artifact()])
    assert "](https" not in normal
    assert "<" not in normal


def test_c02_metadata_never_rendered() -> None:
    """C2：输出**不含** `Artifact.metadata` 的任何键或值。"""
    artifact = _artifact(metadata={"rev": 2, "secret_key": "zzz-secret-value"})
    out = render_artifact_references([artifact])
    assert "rev" not in out
    assert "zzz-secret-value" not in out
    assert "secret_key" not in out


def test_c03_no_completion_wording() -> None:
    """C3：输出**不含**任何完成态措辞（已上传 / 已存储 / 已下载 …）。"""
    out = render_artifact_references([_artifact(), _artifact(artifact_id="a-2")])
    for word in FORBIDDEN_CLAIMS:
        assert word not in out, f"出现完成态措辞：{word}"


def test_c04_unverified_notice_present() -> None:
    """C4：输出**必含**「未验证」等价语义提示。"""
    out = render_artifact_references([_artifact()])
    assert "未验证" in out
    assert ARTIFACT_REFERENCE_HEADER in out


def test_c05_uri_none_shows_clear_notice_and_no_link() -> None:
    """C5：`uri=None` → 含「未提供可用引用」提示，**无**空链接，**不抛异常**。"""
    out = render_artifact_references([_artifact(uri=None)])
    assert "未提供可用引用" in out
    assert "``" not in out
    assert "https://example" not in out


def test_c06_control_chars_sanitised_in_free_text() -> None:
    """C6：自由文本字段的控制字符 → 空格（不产生换行 / 制表伪造结构）。"""
    artifact = _artifact(name="a\r\nb\tc\x00d")
    out = render_artifact_references([artifact])
    body = out.split("\n", 1)[1]
    assert "\r" not in out and "\t" not in out and "\x00" not in out
    # 抬头 1 行 + 每个 artifact 恰好 2 行 ⇒ 换行数固定，无注入
    assert body.count("\n") == 1
    # 每个不安全字符各自替换为一个空格（确定性）
    assert "1. a  b c d（" in out


def test_c07_at_most_ten_artifacts_plus_count_notice() -> None:
    """C7：数量 > 10 → 呈现前 10 条 + 明确「另有 N 条」提示（不静默丢弃）。"""
    artifacts = [_artifact(artifact_id=f"a{i}", name=f"n{i}.txt",
                           uri=f"https://example/{i}") for i in range(13)]
    out = render_artifact_references(artifacts)
    assert out.count("引用：") == MAX_PRESENTED_ARTIFACTS
    assert "3 条引用未在消息中呈现" in out


def test_c08_name_truncated_with_explicit_marker() -> None:
    """C8：**名称**超 128 → 截断 + 显式标注（不抛错）。"""
    out = render_artifact_references([_artifact(name="N" * 400)])
    assert "…" in out
    assert "N" * (MAX_DISPLAY_TEXT_CHARS - 1) in out
    assert "N" * (MAX_DISPLAY_TEXT_CHARS + 1) not in out


def test_c06_unicode_separators_sanitised_in_free_text() -> None:
    """C6（R-1 加固）：`Zl` / `Zp` 行/段分隔符在自由文本中 → 空格。

    U+2028（LINE SEPARATOR）/ U+2029（PARAGRAPH SEPARATOR）是**不可见字形**，
    在部分渲染器中可造成视觉换行 ⇒ 必须与 `Cc` / `Cf` 同等净化，
    不得让它们逃逸出预期的文本呈现结构。
    """
    artifact = _artifact(name="a\u2028b\u2029c")
    out = render_artifact_references([artifact])
    assert "\u2028" not in out
    assert "\u2029" not in out
    # 每个不安全字符各自替换为一个空格（确定性）
    assert "1. a b c（" in out
    # 换行数固定：抬头 1 行 + 每个 artifact 恰好 2 行 ⇒ 无行结构伪造
    body = out.split("\n", 1)[1]
    assert body.count("\n") == 1


def test_c09_rendering_is_deterministic() -> None:
    """C9：同输入两次调用**逐字节**一致。"""
    artifacts = [_artifact(), _artifact(artifact_id="a-2", uri=None)]
    assert render_artifact_references(artifacts) == render_artifact_references(artifacts)


def test_c10_renderer_is_total_function() -> None:
    """C10：对 None / 空列表 / 畸形元素 / 超长串 / 非 str 字段**均不抛异常**。"""
    assert render_artifact_references(None) == ""
    assert render_artifact_references([]) == ""
    assert render_artifact_references("not-a-list") == ""
    assert render_artifact_references([object(), 42, "x", None])
    assert render_artifact_references([_artifact(name="x" * 5000, uri="u" * 5000)])


def test_c11_no_network_call_during_rendering(monkeypatch) -> None:
    """C11：**零网络请求**——渲染过程不触碰 socket / HTTP。"""

    def _boom(*args, **kwargs):
        raise AssertionError("渲染过程触发了网络调用")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    out = render_artifact_references([_artifact(uri="https://example/x")])
    assert "https://example/x" in out


def test_c12_renderer_imports_stdlib_only() -> None:
    """C12：渲染模块的 import 根**仅 stdlib**（无 app.* / 框架 / persistence）。

    ACP V0.4 Phase 2 调整（**措辞精化，实质保护未降低**）：渲染器新增**唯一**一个
    仓内依赖 —— 纯 stdlib 的同级策略模块 `app.domain.artifact_uri_policy`
    （§3 强制要求「新增纯标准库 URI 策略模块」+ §5 要求接入呈现流程，二者共同
    决定了这条仓内依赖的**存在性**）。断言随之精化为：
    ① 渲染器的仓内依赖**仅限**该策略模块，其余仍必须来自 stdlib；
    ② **该策略模块自身**的 import 根**仅 stdlib**（新增守卫 ⇒ 实质保护**增强**）。
    """
    def _imports(path: Path) -> set[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        mods: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods.add(node.module)
        return mods

    renderer_imports = _imports(APP_DIR / "domain" / "artifact_reference.py")
    # 仓内依赖**仅限**策略模块；其余必须来自 stdlib
    assert "app.domain.artifact_uri_policy" in renderer_imports, renderer_imports
    non_stdlib = {m for m in renderer_imports
                  if m.split(".")[0] not in set(sys.stdlib_module_names)}
    assert non_stdlib == {"app.domain.artifact_uri_policy"}, non_stdlib
    # 策略模块自身：import 根**仅 stdlib**（无 I/O / 框架 / 仓内依赖）
    policy_imports = _imports(APP_DIR / "domain" / "artifact_uri_policy.py")
    assert {m.split(".")[0] for m in policy_imports} \
        <= set(sys.stdlib_module_names), policy_imports


def test_c13_new_symbol_names_avoid_banned_tokens() -> None:
    """C13：新增模块的符号名不含 retry / replay / redeliver / outbox。"""
    tree = ast.parse((APP_DIR / "domain" / "artifact_reference.py")
                     .read_text(encoding="utf-8"))
    names = [n.name.lower() for n in ast.walk(tree)
             if isinstance(n, (ast.ClassDef, ast.FunctionDef,
                               ast.AsyncFunctionDef))]
    assert names
    for token in ("retry", "replay", "redeliver", "outbox"):
        for name in names:
            assert token not in name, f"新符号名触发扫描敏感词：{name}（{token}）"


def test_c14_metadata_absent_from_message_and_error_text() -> None:
    """C14：出站消息 / 错误信息中**不含** `Artifact.metadata` 内容。"""
    artifact = _artifact(metadata={"token": "super-secret"})
    out = render_artifact_references([artifact])
    assert "super-secret" not in out
    assert "token" not in out


def test_c15_uri_rendered_verbatim_byte_identical() -> None:
    """C15：消息中出现的 URI 与 `Artifact.uri` **逐字节相同**。"""
    uri = "https://example/a%20b/c?x=1&y=2#frag"
    out = render_artifact_references([_artifact(uri=uri)])
    assert uri in out
    assert uri + "…" not in out
    assert uri.replace("&", "&amp;") not in out


def test_c16_over_budget_uri_omitted_entirely() -> None:
    """C16：URI 超出剩余展示预算 → **整体省略**，输出**不含**其任何片段。

    ACP V0.4 Phase 2 调整（**仅用例长度取值**，三条断言逐字未改）：策略层新增 URI
    长度上限 2048（超过即拒绝、**不截断**），故本用例改用 **1900** 字符 ——
    它**仍然超出渲染可用预算**（块预算 2000 − 提示预留 240 ⇒ 可用 ≈1760），
    因而仍然精确检验「渲染预算路径」；「策略长度上限」路径由
    `tests/test_acp_v0_4_artifact_uri_policy.py::test_a12_too_long_uri_is_rejected_without_truncation`
    单独覆盖。
    """
    huge = "https://example/" + "z" * 1900
    out = render_artifact_references([_artifact(uri=huge)])
    assert "z" * 20 not in out
    assert "https://example/zzz" not in out
    assert "超出可展示预算" in out


def test_c17_control_char_uri_omitted_entirely() -> None:
    """C17：含控制字符的 URI → **整体省略**（不先消毒再呈现）。"""
    out = render_artifact_references([_artifact(uri="https://example/a\nb")])
    assert "https://example/a" not in out
    assert "引用格式非法" in out


def test_c17_unicode_separator_uri_omitted_entirely() -> None:
    """C17（R-1 加固）：含 `Zl` / `Zp` 的 URI → **整体省略** + 明确原因，零片段。

    绝不先消毒 URI 再把改写后的值当作地址呈现（那会破坏 L2 原值完整性，
    且可能产出**错误**的地址）。
    """
    for raw in ("https://example/a\u2028b", "https://example/a\u2029b"):
        out = render_artifact_references([_artifact(uri=raw)])
        assert "引用未在消息中呈现" in out
        assert "引用包含行/段分隔符" in out
        # 零片段：不含 URI 的任何前缀 / 后缀，也不含分隔符本身
        assert "https://example/a" not in out
        assert "\u2028" not in out and "\u2029" not in out


def test_c18_omission_is_visible_with_count() -> None:
    """C18：省略**可见**——逐条原因提示 + 计数（不静默丢弃）。"""
    out = render_artifact_references([
        _artifact(artifact_id="a-1", uri="https://example/ok"),
        _artifact(artifact_id="a-2", uri="https://example/bad\n"),
    ])
    assert "引用未在消息中呈现" in out
    assert "1 条引用的地址未在消息中呈现" in out


def test_c19_block_budget_respected_and_announced() -> None:
    """C19：引用块超 2000 → 停止追加 + 「引用块已截断」提示，**不丢文本主体**。"""
    artifacts = [_artifact(artifact_id=f"a{i}", name="N" * 200,
                           uri=f"https://example/{i}") for i in range(30)]
    out = render_artifact_references(artifacts)
    assert len(out) <= MAX_REFERENCE_BLOCK_CHARS
    assert "引用块已截断" in out


def test_c22_omitted_reference_not_described_as_retrievable() -> None:
    """C22：被省略的引用**不得**被描述为终端用户可取得 / 可点击。"""
    out = render_artifact_references(
        [_artifact(uri="https://example/" + "q" * 3000)])
    for word in ("点击", "可访问", "可下载", "已提供地址", "用户可取得"):
        assert word not in out


# ================================================================== B. 兼容（逐字节）


def test_b1_wecom_text_payload_unchanged_without_artifact(fake_urlopen) -> None:
    """B1：无 artifact 时，WeCom 自建应用 `message/send` payload **逐字节**不变。"""
    _wecom_adapter(_wecom_channel()).deliver(
        _outbound("hi", channel_id=WECOM_CHANNEL_ID))
    assert fake_urlopen.sent_payloads()[-1] == {
        "touser": "conv-1", "msgtype": "text", "agentid": 1000002,
        "text": {"content": "hi"},
    }


def test_b2_wecom_robot_payload_unchanged_without_artifact(fake_urlopen) -> None:
    """B2：无 artifact 时，智能机器人 `response_url` payload **逐字节**不变。"""
    WeComAdapter().deliver(
        _outbound("hi", metadata={"response_url": "https://cb/x"}))
    assert fake_urlopen.sent_payloads()[-1] == {
        "msgtype": "markdown", "markdown": {"content": "hi"}}


def test_b3_bot_frame_unchanged_without_artifact() -> None:
    """B3：无 artifact 时，WeCom Bot 回信帧**逐字节**不变。"""
    adapter, registry = _bot_adapter()
    adapter.deliver(_outbound("hi", metadata={"bot_reply": {"req_id": "R-1"}}))
    assert registry.sent[0]["body"] == {
        "msgtype": "markdown", "markdown": {"content": "hi"}}


def test_b4_local_outbox_unchanged_without_artifact(tmp_path) -> None:
    """B4：无 artifact 时，Local outbox 行**逐字节**一致且**无** `artifacts` 键。"""
    outbox = tmp_path / "o.jsonl"
    LocalChannelAdapter(outbox_path=outbox).deliver(_outbound("hi"))
    record = json.loads(outbox.read_text(encoding="utf-8").split("\n")[0])  # 按 \n 切分（非 splitlines()：见 Gate §8.2 R-6）
    assert record == {
        "channel_id": 1, "external_conversation_id": "conv-1",
        "content": "hi", "metadata": None,
    }
    assert "artifacts" not in record


def test_b5_wecom_text_payload_contains_reference_with_artifact(fake_urlopen) -> None:
    """B5：有 artifact 时，WeCom 自建应用消息**含引用**且文本主体不变。"""
    _wecom_adapter(_wecom_channel()).deliver(
        _outbound("hi", channel_id=WECOM_CHANNEL_ID, artifacts=[_artifact()]))
    content = fake_urlopen.sent_payloads()[-1]["text"]["content"]
    assert content.startswith("hi")
    assert "spec.pdf" in content
    assert "https://example/spec.pdf" in content
    assert "未验证" in content


def test_b6_wecom_robot_payload_contains_reference_with_artifact(fake_urlopen) -> None:
    """B6：有 artifact 时，智能机器人 markdown **含引用**。"""
    WeComAdapter().deliver(
        _outbound("hi", metadata={"response_url": "https://cb/x"},
                  artifacts=[_artifact()]))
    content = fake_urlopen.sent_payloads()[-1]["markdown"]["content"]
    assert "spec.pdf" in content
    assert "https://example/spec.pdf" in content


def test_b7_bot_frame_contains_reference_with_artifact() -> None:
    """B7：有 artifact 时，WeCom Bot 回信帧**含引用**。"""
    adapter, registry = _bot_adapter()
    adapter.deliver(_outbound("hi", metadata={"bot_reply": {"req_id": "R-1"}},
                              artifacts=[_artifact()]))
    content = registry.sent[0]["body"]["markdown"]["content"]
    assert "spec.pdf" in content
    assert "https://example/spec.pdf" in content


# ================================================================== D. Local outbox


def test_d1_local_outbox_without_artifact_keeps_legacy_shape(tmp_path) -> None:
    """D1：无 artifact → 行结构与旧版**逐字节**一致（4 键，无 `artifacts`）。"""
    outbox = tmp_path / "o.jsonl"
    LocalChannelAdapter(outbox_path=outbox).deliver(_outbound("hi"))
    record = json.loads(outbox.read_text(encoding="utf-8").split("\n")[0])  # 按 \n 切分（非 splitlines()：见 Gate §8.2 R-6）
    assert set(record) == {"channel_id", "external_conversation_id", "content",
                           "metadata"}


def test_d2_local_outbox_with_artifact_full_original_fields(tmp_path) -> None:
    """D2：有 artifact → `artifacts` 键含 7 字段**完整原值**（L4）。"""
    outbox = tmp_path / "o.jsonl"
    LocalChannelAdapter(outbox_path=outbox).deliver(
        _outbound("hi", artifacts=[_artifact()]))
    record = json.loads(outbox.read_text(encoding="utf-8").split("\n")[0])  # 按 \n 切分（非 splitlines()：见 Gate §8.2 R-6）
    assert set(record) == {"channel_id", "external_conversation_id", "content",
                           "metadata", "artifacts"}
    entry = record["artifacts"][0]
    assert set(entry) == {"artifact_id", "kind", "name", "uri", "mime_type",
                          "size_bytes", "metadata"}
    assert entry["uri"] == "https://example/spec.pdf"
    assert entry["metadata"] == {"rev": 2}
    assert entry["size_bytes"] == 1234


# ================================================================== 结构化记录工具


def test_artifact_reference_records_shape_and_fidelity() -> None:
    """结构化记录：7 字段、**完整原值**（与快照形状对齐）。"""
    records = artifact_reference_records([_artifact(uri="https://example/x",
                                                    metadata={"a": 1})])
    assert len(records) == 1
    assert set(records[0]) == {"artifact_id", "kind", "name", "uri", "mime_type",
                               "size_bytes", "metadata"}
    assert records[0]["uri"] == "https://example/x"
    assert records[0]["metadata"] == {"a": 1}
    assert artifact_reference_records(None) == []


def test_render_outbound_content_is_byte_identical_without_artifacts() -> None:
    """无 artifact → 出站文本**原样**返回（逐字节一致）。"""
    assert render_outbound_content("hi", None) == "hi"
    assert render_outbound_content("hi", []) == "hi"


# ================================================================== E. 运行时与事务


def test_e1_no_new_activity_type_introduced() -> None:
    """E1：Activity 类型集合仍为既有 4 个（本阶段**不新增**事件）。"""
    from app.domain import activity_record
    types = {value for name, value in vars(activity_record).items()
             if name.startswith("ACTIVITY_") and isinstance(value, str)}
    assert types == {"interaction_received", "stage_failure", "session_created",
                     "interaction_completed"}


def test_e2_no_new_persistence_write_site_for_artifacts() -> None:
    """E2：artifacts 仍**只**写既有 `response_payload` 键（无新表 / 新列 / 新写入点）。"""
    recording = (APP_DIR / "application" / "interaction_recording.py") \
        .read_text(encoding="utf-8")
    assert '"artifacts": _snapshot_artifacts(' in recording
    # 渲染模块**不**触碰持久化：import 面仅 stdlib + 纯 stdlib 的同级策略模块
    # （docstring 提及不算依赖）—— 详见 test_c12_… 的同名守卫
    tree = ast.parse((APP_DIR / "domain" / "artifact_reference.py")
                     .read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    assert roots <= {"unicodedata", "app"}, roots
    assert "app.persistence" not in roots


def test_e3_delivery_status_values_unchanged() -> None:
    """E3：`delivery_status` 取值集合**不变**（未新增 delivered_partial 等）。"""
    from app.domain import interaction_record
    values = {value for name, value in vars(interaction_record).items()
              if name.startswith("DELIVERY_") and isinstance(value, str)}
    # P2 Delivery Lifecycle 扩展了两个取值（在途 / 结果未知）
    assert values == {"delivered", "failed", "pending", "delivering", "unknown"}


def test_e7_artifact_processing_failure_does_not_flip_text_delivery(
        fake_urlopen, monkeypatch) -> None:
    """E7：引用处理**失败**（注入）**不**把已成功的文本交付改判为失败。

    引用呈现是**附加**能力：即使引用处理整体失效（此处注入一个不产出任何引用的
    退化渲染器），**文本消息仍原样送达**——文本交付结果不受引用处理影响。
    """
    import app.adapters.channels.wecom as wecom_module

    monkeypatch.setattr(wecom_module, "render_outbound_content",
                        lambda content, artifacts: content)
    _wecom_adapter(_wecom_channel()).deliver(
        _outbound("hi", channel_id=WECOM_CHANNEL_ID, artifacts=[_artifact()]))
    # 投递**成功**（无异常），且消息内容 == 原文本（逐字节）
    assert fake_urlopen.sent_payloads()[-1] == {
        "touser": "conv-1", "msgtype": "text", "agentid": 1000002,
        "text": {"content": "hi"},
    }


# ================================================================== Runtime 装配（DB）


class _ArtifactAgent:
    """返回 content + artifacts 的 Agent 适配器。"""

    def __init__(self, artifacts) -> None:
        self._artifacts = artifacts

    def send(self, request, connection) -> AgentResponse:  # noqa: ARG002
        return AgentResponse(content=f"echo:{request.content}",
                             artifacts=self._artifacts)


class _BadArtifactAgent:
    """返回非法 artifact 的 Agent 适配器（触发 dispatch Fail Closed）。"""

    def send(self, request, connection) -> AgentResponse:  # noqa: ARG002
        return AgentResponse(
            content=f"echo:{request.content}",
            artifacts=[Artifact(artifact_id="", kind="file", name="n")])


class _RecordingSink:
    def __init__(self) -> None:
        self.deliveries: list[OutboundResponse] = []

    def deliver(self, outbound: OutboundResponse) -> None:
        self.deliveries.append(outbound)


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


def test_d3_invalid_artifact_never_reaches_outbox(db_engine, tmp_path) -> None:
    """D3：无效 artifact → dispatch Fail Closed，**outbox 无新行**。"""
    outbox = tmp_path / "o.jsonl"
    g = _seed(db_engine)
    runtime = _runtime(db_engine, agent=_BadArtifactAgent(),
                       sink=LocalChannelAdapter(outbox_path=outbox))
    with pytest.raises(AgentResponseInvalidError):
        runtime.execute(_inbound(g["channel"]))
    assert not outbox.exists() or outbox.read_text(encoding="utf-8").strip() == ""


def test_e4_artifacts_reach_outbound_response_through_coordinator(db_engine) -> None:
    """E4：Coordinator **只透传** —— artifact 引用到达 OutboundResponse（不渲染）。"""
    g = _seed(db_engine)
    sink = _RecordingSink()
    artifacts = [_artifact()]
    _runtime(db_engine, agent=_ArtifactAgent(artifacts), sink=sink).execute(
        _inbound(g["channel"]))
    assert len(sink.deliveries) == 1
    delivered = sink.deliveries[0]
    assert delivered.artifacts == artifacts
    assert delivered.content == "echo:hello"      # 文本主体不变
    assert "spec.pdf" not in delivered.content     # 呈现由 Adapter 决定


def test_c20_snapshot_keeps_full_original_uri(db_engine) -> None:
    """C20：快照中的 uri / metadata 是**完整原值**（展示预算**不**回溯影响持久化）。"""
    g = _seed(db_engine)
    long_uri = "https://example/" + "z" * 3000
    artifacts = [_artifact(uri=long_uri, metadata={"k": "v"})]
    _runtime(db_engine, agent=_ArtifactAgent(artifacts)).execute(
        _inbound(g["channel"]))
    snapshot = _response_payload(db_engine)
    assert snapshot["artifacts"][0]["uri"] == long_uri
    assert snapshot["artifacts"][0]["metadata"] == {"k": "v"}


def test_c20_snapshot_keeps_unicode_separator_uri_verbatim(db_engine) -> None:
    """C20（R-1 加固）：含分隔符的 URI 在**快照**中仍是**完整原值**。

    呈现层的净化 / 省略**不得**回溯影响持久化层（L4）——快照保存的是
    Agent 提供的原始引用，与它能否在消息中呈现无关。
    """
    g = _seed(db_engine)
    raw = "https://example/a\u2028b"
    artifacts = [_artifact(uri=raw, metadata={"k": "v"})]
    _runtime(db_engine, agent=_ArtifactAgent(artifacts)).execute(
        _inbound(g["channel"]))
    snapshot = _response_payload(db_engine)
    assert snapshot["artifacts"][0]["uri"] == raw
    assert "\u2028" in snapshot["artifacts"][0]["uri"]      # 原值，未被净化
    assert snapshot["artifacts"][0]["metadata"] == {"k": "v"}


def test_c21_outbox_artifact_uri_is_verbatim_even_if_over_budget(tmp_path) -> None:
    """C21：outbox 的 `artifacts[i]["uri"]` 与原值**逐字节**相同（不因展示预算截断）。"""
    long_uri = "https://example/" + "z" * 3000
    outbox = tmp_path / "o.jsonl"
    LocalChannelAdapter(outbox_path=outbox).deliver(
        _outbound("hi", artifacts=[_artifact(uri=long_uri)]))
    record = json.loads(outbox.read_text(encoding="utf-8").split("\n")[0])  # 按 \n 切分（非 splitlines()：见 Gate §8.2 R-6）
    assert record["artifacts"][0]["uri"] == long_uri


def test_c21_outbox_keeps_unicode_separator_uri_verbatim(tmp_path) -> None:
    """C21（R-1 加固）：含分隔符的 URI 在 **Local outbox 结构化记录**中仍为原值。

    结构化记录**不是消息呈现**——展示预算与呈现层净化**都不作用于它**。
    """
    raw = "https://example/a\u2029b"
    outbox = tmp_path / "o.jsonl"
    LocalChannelAdapter(outbox_path=outbox).deliver(
        _outbound("hi", artifacts=[_artifact(uri=raw)]))
    # outbox 是 **以 `\n` 分隔**的 JSONL：此处按 `\n` 切分（**不用** `splitlines()`——
    # 后者连 U+2028 / U+2029 一并切开，会把一条记录切成两半）。
    records = [ln for ln in outbox.read_text(encoding="utf-8").split("\n") if ln.strip()]
    assert len(records) == 1
    record = json.loads(records[0])
    assert record["artifacts"][0]["uri"] == raw
    assert "\u2029" in record["artifacts"][0]["uri"]        # 原值，未被净化
    # 呈现层未渲染引用块 ⇒ outbox 的 content 仍是 Agent 原文
    assert record["content"] == "hi"
