"""存活探针的 HTTP 契约。"""

from typing import Literal

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """GET /health 的响应体。"""

    status: Literal["ok"] = Field(
        ...,
        description="服务存活状态；Step 5.1 恒为 'ok'。",
        examples=["ok"],
    )
