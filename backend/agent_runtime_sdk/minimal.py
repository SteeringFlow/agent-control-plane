"""Agent Runtime SDK —— Minimal Binding（Step 15.5.5.2）。

Wire <-> SDK Model 的**纯函数 binding**（不是 HTTP transport/server；
无 hosting / client / retry / SessionStore）。

冻结的 minimal wire contract（与 Control Plane 侧
`app/adapters/agents/acp.py` 的 `_compose_acp_request` / `_normalize_acp_response`
对偶，但独立实现、绝不 import `app.*`）：

    Request:  {"message": string, "session_id": string | null}
    Response: {"content": string, "session_id": string | null}

Mapping（15.5.5.2 冻结）：
    decode_request:
        wire.message    -> AgentRequest.content
        wire.session_id -> AgentRequest.external_session_id
        agent_id / control_plane_session_id / metadata 恒为 None
    encode_response:
        AgentResponse.content             -> wire.content
        AgentResponse.external_session_id -> wire.session_id
        AgentResponse.metadata **不**序列化到 minimal wire（15.5.5.2 冻结）

Validation（Fail Closed，类型面严格）：
- payload 必须是 JSON object（dict）；
- `message` 必填且必须是 str（wire 上允许空串：CP Dispatch 契约保证发出前
  content 非空，binding 不重复启发式校验）；
- `session_id` 必须是 str 或 None（key 缺失视同 null）；空串非法
  ——与 CP 侧响应归一化对 `session_id` 的非空校验对称（`acp.py:648-652`）；
- 未知的额外 key 被忽略（发送方可携带未来可选字段）；
- `encode_response` 只接受 `AgentResponse` 实例，且 `content` 必须 str、
  `external_session_id` 必须 str|None（非空）。
"""

from typing import Any

from .models import AgentRequest, AgentResponse

__all__ = ["MinimalBindingError", "decode_request", "encode_response", "handle"]


class MinimalBindingError(ValueError):
    """minimal wire payload 违反冻结契约（decode / encode Fail Closed）。"""


def decode_request(payload: Any) -> AgentRequest:
    """minimal request payload -> AgentRequest（纯函数，每次返回新实例）。"""
    if not isinstance(payload, dict):
        raise MinimalBindingError("minimal request payload must be a JSON object")
    if "message" not in payload:
        raise MinimalBindingError("minimal request payload missing 'message'")
    message = payload["message"]
    if not isinstance(message, str):
        raise MinimalBindingError("'message' must be a string")
    session_id = payload.get("session_id")
    if session_id is not None and (
        not isinstance(session_id, str) or not session_id
    ):
        raise MinimalBindingError(
            "'session_id' must be a non-empty string or null")
    # agent_id / control_plane_session_id / metadata 不在 minimal wire 上，
    # 恒为模型默认 None（15.5.5.2 冻结 mapping）。
    return AgentRequest(content=message, external_session_id=session_id)


def encode_response(response: AgentResponse) -> dict[str, Any]:
    """AgentResponse -> minimal response payload（只接受 AgentResponse）。"""
    if not isinstance(response, AgentResponse):
        raise MinimalBindingError("minimal binding can only encode AgentResponse")
    if not isinstance(response.content, str):
        raise MinimalBindingError("'content' must be a string")
    session_id = response.external_session_id
    if session_id is not None and (
        not isinstance(session_id, str) or not session_id
    ):
        raise MinimalBindingError(
            "'external_session_id' must be a non-empty string or None")
    # metadata 是 protocol-neutral auxiliary，不进 minimal wire（冻结）。
    return {"content": response.content, "session_id": session_id}


def handle(payload: Any, agent: Any) -> dict[str, Any]:
    """一次 minimal wire 调用：decode_request -> agent.handle_turn -> encode_response。"""
    return encode_response(agent.handle_turn(decode_request(payload)))
