"""Step 12.8.6 —— Real ACP Wire Classification Smoke。

只做一件事：真实 Tencent ACP → 真实 SSE → 观察 reasoning block 原始结构 →
判定 Case A（独立 `type`）/ Case B（text node + 语义字段）/ Case C（fused）。

- 判定逻辑为模块级纯函数 `classify_wire_verdict`，可离线单测；
- live 测试 env 门控（`TENCENT_ACP_URL/TOKEN/RUNTIME_ID`，缺省 skip），
  把每条原始 SSE 事件写入 `backend/artifacts/acp-wire-capture-*.jsonl` 作为证据；
- 判定 A/B → 在 `acp_content_classification.py` 补充真实分型常量 + 回归；
- 判定 C → **不改代码**，正式关闭「reasoning 在 CP 侧可分离性」问题。
"""

import json
import time
from pathlib import Path

import pytest

from app.adapters.agents.acp import PROTOCOL_TENCENT_ACP_SSE, ACPAdapter
from app.domain.agent_request import AgentRequest
from app.domain.connection import Connection

BACKEND_DIR = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = BACKEND_DIR / "artifacts"

_LIVE_ENV = ("TENCENT_ACP_URL", "TENCENT_ACP_TOKEN", "TENCENT_RUNTIME_ID")


# ================================================================== 判定纯函数（离线可测）


_REASONING_TYPE_MARKS = ("reason", "think", "thought")


def _type_marks_reasoning(btype: str) -> bool:
    lowered = btype.lower()
    return any(mark in lowered for mark in _REASONING_TYPE_MARKS)


def _text_block_reasoning_fields(block: dict) -> list[str]:
    """Case B 信号：text 块上携带的 reasoning 语义字段名。"""
    marks = []
    role = block.get("role")
    if isinstance(role, str) and role.lower() in ("reasoning", "thinking", "thought"):
        marks.append("role")
    if block.get("reasoning") is True:
        marks.append("reasoning")
    if block.get("thinking") is True:
        marks.append("thinking")
    meta = block.get("metadata")
    if isinstance(meta, dict) and (
            meta.get("reasoning") is True or meta.get("thinking") is True):
        marks.append("metadata")
    return marks


def classify_wire_verdict(events: list[str]) -> dict:
    """扫描原始 SSE 事件 → {verdict, case_a_blocks, case_b_blocks, type_values,
    text_block_keys, text_samples}。

    - verdict = "A"：存在独立 reasoning type 的 content 块；
    - verdict = "B"：text 块带 reasoning 语义字段（块级字段，或 12.8.6 真实校准的
      父级 update 信封 `sessionUpdate` 含 thought），且无 Case A；
    - verdict = "C"：只有无信号 text 块（reasoning 与正文 fused 或根本无 reasoning）；
    - verdict = "UNKNOWN"：事件里没有任何 text/reasoning content（wire 形状出乎预期，
      需要人工看 artifact 校准）。
    """
    case_a: list[dict] = []
    case_b: list[dict] = []
    type_values: set[str] = set()
    text_keys: set[str] = set()
    text_samples: list[str] = []

    def _scan(node, envelope_reasoning: bool, envelope_su) -> None:
        if isinstance(node, dict):
            su = node.get("sessionUpdate")
            here_reasoning = envelope_reasoning or (
                isinstance(su, str) and "thought" in su.lower())
            here_su = su if isinstance(su, str) else envelope_su
            btype = node.get("type")
            if isinstance(btype, str):
                type_values.add(btype)
                text = node.get("text")
                if btype == "text":
                    text_keys.update(
                        k for k in node.keys() if k not in ("type", "text"))
                    if isinstance(text, str) and text and len(text_samples) < 10:
                        text_samples.append(text)
                    if here_reasoning or _text_block_reasoning_fields(node):
                        case_b.append({"envelope_sessionUpdate": here_su}
                                      if here_reasoning and not
                                      _text_block_reasoning_fields(node) else node)
                elif _type_marks_reasoning(btype):
                    case_a.append(node)
            for value in node.values():
                _scan(value, here_reasoning, here_su)
        elif isinstance(node, list):
            for item in node:
                _scan(item, envelope_reasoning, envelope_su)

    for raw in events:
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            _scan(obj, False, None)

    if case_a:
        verdict = "A"
    elif case_b:
        verdict = "B"
    elif text_samples:
        verdict = "C"
    else:
        verdict = "UNKNOWN"
    return {
        "verdict": verdict,
        "case_a_blocks": case_a,
        "case_b_blocks": case_b,
        "type_values": sorted(type_values),
        "text_block_keys": sorted(text_keys),
        "text_samples": text_samples,
    }


# ================================================================== 离线单测（判定函数）


def test_verdict_case_a_independent_type() -> None:
    events = [json.dumps({
        "method": "session/update",
        "params": {"sessionUpdate": {"updates": [{
            "type": "agent_message_chunk",
            "message": {"content": [
                {"type": "reasoning", "text": "思考中"},
                {"type": "text", "text": "答案"},
            ]},
        }]}},
    }, ensure_ascii=False)]
    report = classify_wire_verdict(events)
    assert report["verdict"] == "A"
    assert report["case_a_blocks"]


def test_verdict_case_b_text_semantic_field() -> None:
    events = [json.dumps({
        "params": {"sessionUpdate": {"updates": [{
            "message": {"content": [
                {"type": "text", "role": "reasoning", "text": "思考"},
                {"type": "text", "text": "答案"},
            ]},
        }]}},
    }, ensure_ascii=False)]
    report = classify_wire_verdict(events)
    assert report["verdict"] == "B"
    assert report["case_b_blocks"]


def test_verdict_case_b_envelope_signal() -> None:
    """12.8.6 真实校准：信号在父级 update 信封 sessionUpdate 上（agent_thought_chunk）。"""
    events = [json.dumps({
        "method": "session/update",
        "params": {"sessionId": "1", "update": {
            "sessionUpdate": "agent_thought_chunk",
            "content": {"type": "text", "text": "The user asks: ..."},
        }},
    }, ensure_ascii=False), json.dumps({
        "method": "session/update",
        "params": {"sessionId": "1", "update": {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "9.8 更大"},
        }},
    }, ensure_ascii=False)]
    report = classify_wire_verdict(events)
    assert report["verdict"] == "B"
    assert report["case_b_blocks"]


def test_verdict_case_c_fused_or_plain_text_only() -> None:
    events = [json.dumps({
        "params": {"sessionUpdate": {"updates": [{
            "message": {"content": [{"type": "text", "text": "Let me think... 答案"}]},
        }]}},
    }, ensure_ascii=False)]
    report = classify_wire_verdict(events)
    assert report["verdict"] == "C"
    assert not report["case_a_blocks"] and not report["case_b_blocks"]


def test_verdict_unknown_when_no_content() -> None:
    report = classify_wire_verdict([json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}})])
    assert report["verdict"] == "UNKNOWN"


# ================================================================== live smoke（env 门控）


def _load_live_env() -> dict[str, str]:
    from dotenv import load_dotenv

    load_dotenv(BACKEND_DIR / ".env", override=False)
    import os

    return {name: os.environ.get(name, "").strip() for name in _LIVE_ENV}


def _live_connection(values: dict[str, str]) -> Connection:
    return Connection(
        channel_id=1, agent_id=1, name="tencent-acp-live", id=99,
        config={
            "endpoint": values["TENCENT_ACP_URL"],
            "protocol": PROTOCOL_TENCENT_ACP_SSE,
            "bearer_token": values["TENCENT_ACP_TOKEN"],
            "runtime_id": values["TENCENT_RUNTIME_ID"],
            "timeout_s": 60.0,
        })


@pytest.fixture()
def live_env() -> dict[str, str]:
    values = _load_live_env()
    if not all(values.values()):
        pytest.skip("未配置 TENCENT_ACP_URL/TOKEN/RUNTIME_ID（12.8.6 真实 wire smoke）")
    return values


def test_live_wire_reasoning_classification(live_env) -> None:
    """真实 Tencent ACP wire smoke：捕获原始 SSE → 判定 A/B/C。

    prompt 刻意选需要逐步推理 + 产出 markdown 的形态，最大化观察 reasoning 块的机会。
    """
    captured: list[str] = []
    adapter = ACPAdapter(event_recorder=captured.append)
    request = AgentRequest(
        agent_id=1, control_plane_session_id=7,
        content=("请先一步步推理再回答：9.11 和 9.8 哪个大？"
                 "然后用一个 Markdown 小节给出结论（含 `## 结论`）。"),
        external_session_id=None)
    response = adapter.send(request, _live_connection(live_env))

    # 基线 sanity：真实 Agent 正常回复（同 10.1.3 live smoke 语义）
    assert isinstance(response.content, str) and response.content.strip()
    assert response.metadata.get("protocol") == PROTOCOL_TENCENT_ACP_SSE
    assert captured, "真实 SSE 必须至少产生一条原始事件（否则 wire 观测失败）"

    # 证据落盘（原始 wire 结构，供人工校准 / 复盘）
    ARTIFACT_DIR.mkdir(exist_ok=True)
    artifact = ARTIFACT_DIR / (
        "acp-wire-capture-" + time.strftime("%Y%m%d-%H%M%S") + ".jsonl")
    artifact.write_text(
        "\n".join(captured) + "\n", encoding="utf-8")

    report = classify_wire_verdict(captured)
    detail = (
        f"\nverdict={report['verdict']}\n"
        f"type_values={report['type_values']}\n"
        f"text_block_keys={report['text_block_keys']}\n"
        f"case_a_blocks={report['case_a_blocks'][:3]}\n"
        f"case_b_blocks={report['case_b_blocks'][:3]}\n"
        f"text_samples={report['text_samples'][:5]}\n"
        f"artifact={artifact}\n"
        f"response.content[:200]={response.content[:200]!r}\n")
    assert report["verdict"] in ("A", "B", "C"), (
        "wire 形状出乎预期（无任何 text/reasoning content）→ 需人工看 artifact 校准"
        + detail)
    # verdict 的具体值即 12.8.6 的结论：A/B → 补真实分型常量；C → 关闭可分离性问题。

    # 端到端校准断言：adapter 返回的 content 必须等于 message 通道重建文本
    # （thought 通道被分类器正确丢弃）——12.8.6 真实 wire（Case B 信封信号）。
    message_parts = []
    for raw in captured:
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        upd = (obj.get("params") or {}).get("update")
        if (isinstance(upd, dict) and upd.get("sessionUpdate") == "agent_message_chunk"):
            c = upd.get("content")
            if isinstance(c, dict) and c.get("type") == "text" \
                    and isinstance(c.get("text"), str):
                message_parts.append(c["text"])
    if message_parts:
        assert response.content == "".join(message_parts), (
            "adapter content != message 通道重建文本（reasoning 分离失败）" + detail)
