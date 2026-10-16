"""Agent 领域实体（Step 5.4 First Vertical Slice）。

纯 Python（仅标准库 dataclasses）。
credential_ref 是凭据引用（指向外部 Secret 存储的标识），
领域对象同样不承载凭据明文——这与 ORM 层承诺一致。
"""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Agent:
    """下游 ACP Agent（Control Plane 要路由过去对话的对象）。

    Step 11.3：补 created_at / updated_at 两个只读审计时间戳回显
    （与 Session 实体同一先例：DB 落库时间戳回显，领域不负责写它们）。
    """

    name: str
    type: str
    id: int | None = None
    endpoint: str | None = None
    status: str = "active"
    config: dict | None = None
    credential_ref: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
