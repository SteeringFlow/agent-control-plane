"""渠道领域实体（Step 5.4 First Vertical Slice）。

纯 Python（仅标准库 dataclasses）。config 是渠道级配置（与存储无关的业务事实），
与 ORM 表如何存无关。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Channel:
    """入站渠道（WeCom / 飞书 / 钉钉 / Webhook ...）。"""

    name: str
    type: str
    id: int | None = None
    status: str = "active"
    config: dict | None = None
