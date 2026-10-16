"""Bootstrap Arm API 的响应契约（M6.6-C3 / ADR-12）。

纪律（沿用 Package 2 Step 3 的 Grant schema）：

- **无请求体 schema**：Arm 端点只接受路径参数 `connection_id`——tenant / channel /
  initiator 全部由服务端解析，请求体不接受任何 scope 或身份字段；
- 响应只回显**必要的非敏感状态**，**不暴露** ACP 内部授权机制
  （无 grant / identity / authorization 词汇，ADR-10 + Cloud-0.1.5 G3）；
- 无 Update / Delete schema：Arm 不是 CRUD 资源（V0 只有「开窗口」一个动作）。
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class BootstrapArmResponse(BaseModel):
    """POST /api/connections/{id}/bootstrap-arm 的响应体（201）。

    `expires_at` 是 **DB 生成值**的原样回显（app 不计算、不换算），供 UI 显示窗口。
    """

    model_config = ConfigDict(from_attributes=True)

    arm_id: int
    connection_id: int
    status: str
    expires_at: datetime
