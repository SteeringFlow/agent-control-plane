"""持久化实现层。

- ``database.py``    Engine / SessionLocal / Base / get_db（Step 5.2，基础设施）
- ``models/``        SQLAlchemy ORM 模型（8 张核心表，Step 5.3 落地）
- ``mappers/``       ORM 行 <-> 领域对象 的映射（双向，唯一允许做这种翻译的地方）
- ``repositories/``  仓储端口的 SQLAlchemy 实现（Step 5.4 First Slice）
- ``unit_of_work.py`` 事务边界：共享 Session + Repository + commit/rollback（Step 5.5）

本层是唯一允许 import SQLAlchemy 的业务相关层。
api 层只允许经由 ``database.get_db`` 获取 Session；
application / domain / repositories（端口层）禁止接触 SQLAlchemy。
"""

from app.persistence.database import Base, SessionLocal, engine, get_db
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

__all__ = ["Base", "SessionLocal", "SqlAlchemyUnitOfWork", "engine", "get_db"]
