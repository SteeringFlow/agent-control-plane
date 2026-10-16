"""Step 15.5.5.2 —— Minimal Binding（Wire <-> SDK Model 纯函数 binding）契约测试。

覆盖面（D1~D10，15.5.5.2 冻结）：
- D1 wire.message    -> AgentRequest.content
- D2 wire.session_id -> AgentRequest.external_session_id（缺失视同 null）
- D3 decode 后 agent_id / control_plane_session_id / metadata 恒为 None
- D4 AgentResponse.content             -> wire.content
- D5 AgentResponse.external_session_id -> wire.session_id
- D6 external_session_id round-trip 保持不变（含 None 与非 None 两态）
- D7 stateful：Agent 自管 session state（SDK 不提供 SessionStore、不保存 Session）
- D8 invalid request：missing message / message 非 str / session_id 非 str|null
- D9 只有 AgentResponse 才能被 encode（含类型与值面校验）
- D10 dependency closure 继续 stdlib-only，不 import backend.app
"""

import ast
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import agent_runtime_sdk
from agent_runtime_sdk import (
    AgentRequest,
    AgentResponse,
    MinimalBindingError,
    decode_request,
    encode_response,
    handle,
)

# ================================================================== D1/D2/D3 decode mapping


def test_d1_message_maps_to_content():
    req = decode_request({"message": "hello", "session_id": None})
    assert isinstance(req, AgentRequest)
    assert req.content == "hello"


def test_d2_session_id_maps_to_external_session_id():
    req = decode_request({"message": "m", "session_id": "agent-session-abc"})
    assert req.external_session_id == "agent-session-abc"
    # key 缺失视同 null（wire 契约 string | null）
    req_null = decode_request({"message": "m"})
    assert req_null.external_session_id is None


def test_d3_decode_neutralizes_control_plane_fields():
    req = decode_request({"message": "m", "session_id": "s1"})
    assert req.agent_id is None
    assert req.control_plane_session_id is None
    assert req.metadata is None


def test_decode_is_pure_and_models_are_frozen():
    """纯函数：每次返回新实例；模型 frozen。"""
    payload = {"message": "m", "session_id": "s1"}
    assert decode_request(payload) is not decode_request(payload)
    req = decode_request(payload)
    with pytest.raises(FrozenInstanceError):
        req.content = "mutated"
    # 额外未知 key 被忽略，不污染模型
    req_extra = decode_request({"message": "m", "session_id": None, "future": 1})
    assert req_extra.content == "m"


# ================================================================== D4/D5/D6 encode mapping


def test_d4_d5_encode_mapping():
    payload = encode_response(
        AgentResponse(content="ok", external_session_id="s1")
    )
    assert payload == {"content": "ok", "session_id": "s1"}


def test_encode_never_serializes_metadata():
    payload = encode_response(
        AgentResponse(
            content="ok",
            external_session_id=None,
            metadata={"protocol": "secret"},
        )
    )
    assert payload == {"content": "ok", "session_id": None}
    assert "metadata" not in payload


def test_d6_external_session_id_round_trip_preserved():
    """D6：wire -> decode -> echo agent -> encode -> wire，session_id 两态均不变。"""

    class _Echo:
        def handle_turn(self, request: AgentRequest) -> AgentResponse:
            return AgentResponse(
                content=f"echo:{request.content}",
                external_session_id=request.external_session_id,
            )

    # 非 None：round-trip 保持
    out = handle({"message": "hi", "session_id": "s-1"}, _Echo())
    assert out == {"content": "echo:hi", "session_id": "s-1"}
    # None：round-trip 保持
    out_null = handle({"message": "hi", "session_id": None}, _Echo())
    assert out_null == {"content": "echo:hi", "session_id": None}


# ================================================================== D7 stateful（Agent 自管 session）


class _SelfManagedSessionAgent:
    """session state 完全由 Agent 自己保存（dict）；SDK 不提供任何存储。"""

    def __init__(self) -> None:
        self._turns: dict[str, int] = {}

    def handle_turn(self, request: AgentRequest) -> AgentResponse:
        sid = request.external_session_id
        if sid is None:
            sid = f"s-{len(self._turns) + 1}"  # Agent 自建 external_session_id
            self._turns[sid] = 1
        else:
            self._turns[sid] = self._turns.get(sid, 0) + 1
        return AgentResponse(
            content=f"turn {self._turns[sid]}", external_session_id=sid
        )


def test_d7_agent_owns_session_state_sdk_stores_nothing():
    agent = _SelfManagedSessionAgent()

    # Turn 1：session_id = None → Agent 自建 s-1
    out1 = handle({"message": "a", "session_id": None}, agent)
    assert out1 == {"content": "turn 1", "session_id": "s-1"}

    # Turn 2：同一 external_session_id 回流 → Agent 读回自己的状态
    out2 = handle({"message": "b", "session_id": "s-1"}, agent)
    assert out2 == {"content": "turn 2", "session_id": "s-1"}

    # Turn 3：继续递增
    out3 = handle({"message": "c", "session_id": "s-1"}, agent)
    assert out3 == {"content": "turn 3", "session_id": "s-1"}

    # SDK 侧无可变 session 容器：binding 只产出不可变 frozen 模型与纯 dict
    assert decode_request({"message": "x"}).external_session_id is None


# ================================================================== D8 invalid request


@pytest.mark.parametrize(
    "bad_payload",
    [
        {},                                   # missing message
        {"session_id": "s1"},                 # missing message
        {"message": 123},                     # message 非 string
        {"message": None},                    # message 非 string
        {"message": ["m"]},                   # message 非 string
        {"message": "m", "session_id": 5},    # session_id 非 string/null
        {"message": "m", "session_id": ["x"]},
        {"message": "m", "session_id": ""},   # 空串非法（与 CP 归一化对称）
        {"message": "m", "session_id": False},
    ],
)
def test_d8_invalid_request_fails_closed(bad_payload):
    with pytest.raises(MinimalBindingError):
        decode_request(bad_payload)


@pytest.mark.parametrize(
    "bad_payload", [[], "x", None, 42],
)
def test_d8_payload_must_be_json_object(bad_payload):
    with pytest.raises(MinimalBindingError):
        decode_request(bad_payload)


# ================================================================== D9 encode 只接受 AgentResponse


@pytest.mark.parametrize(
    "bad_response",
    [
        None,
        {"content": "ok", "session_id": None},          # 裸 dict 不行
        AgentRequest(content="m"),                       # 反向模型不行
        "ok",
    ],
)
def test_d9_encode_only_accepts_agent_response(bad_response):
    with pytest.raises(MinimalBindingError):
        encode_response(bad_response)


def test_d9_encode_validates_value_face():
    """AgentResponse 类型面由构造保证，值面由 encode Fail Closed。"""
    with pytest.raises(MinimalBindingError):
        encode_response(AgentResponse(content=123))  # type: ignore[arg-type]
    with pytest.raises(MinimalBindingError):
        encode_response(AgentResponse(content="ok", external_session_id=""))


# ================================================================== D10 dependency closure

_FORBIDDEN_ROOTS = {
    "app",
    "sqlalchemy",
    "fastapi",
    "redis",
    "httpx",
    "anyio",
    "uvicorn",
    "flask",
    "pytest",
}


def _iter_top_level_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.extend(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                continue  # 包内相对导入，允许
            roots.append((node.module or "").split(".")[0])
    return roots


def test_d10_sdk_dependency_closure_stays_stdlib_only():
    sdk_dir = Path(agent_runtime_sdk.__file__).resolve().parent
    py_files = sorted(sdk_dir.rglob("*.py"))
    assert py_files
    for py in py_files:
        for root in _iter_top_level_imports(py):
            assert root not in _FORBIDDEN_ROOTS, f"{py.name}: forbidden import '{root}'"
            assert root in sys.stdlib_module_names or root == "agent_runtime_sdk", (
                f"{py.name}: non-stdlib import '{root}'"
            )
