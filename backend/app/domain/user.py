"""用户领域实体（Step 5.4 First Vertical Slice）。

纯 Python：只依赖标准库 dataclasses，不依赖 SQLAlchemy / Pydantic / FastAPI。
与 ORM Model（app/persistence/models/user.py）是两件不同的事——
转换由 Mapper（app/persistence/mappers/）完成，领域不感知存储。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class User:
    """Control Plane 用户（Identity 背后的真人 / 账号）。

    id 在 create 之前为 None，落库后由 Repository 返回带 id 的新实例。
    """

    name: str
    id: int | None = None
    status: str = "active"
