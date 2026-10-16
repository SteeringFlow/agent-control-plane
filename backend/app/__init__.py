"""Agent Control Plane —— 后端应用根包（Step 5.1 Bootstrap）。

本包是分层架构的物理边界，依赖方向只允许「外层 -> 内层」：

    api            HTTP 入口（FastAPI Router，只做参数校验与响应装配）
      |
      +-> schemas  HTTP 契约（Pydantic）
      |
      +-> application   用例编排（不认识 FastAPI / SQLAlchemy）
            |
            +-> domain           领域（纯 Python，不认识任何框架）
            +-> repositories      仓储抽象端口（Protocol / ABC，无实现）

    adapters      外部世界适配器（渠道入站 / Agent 出站），实现外层依赖
    persistence   ORM 模型 + Mapper + 仓储实现（唯一允许碰 SQLAlchemy 的地方）
    core          跨层基础设施（配置 / 错误语义），不反向依赖任何业务层

Step 5.1 的唯一目标：证明这套骨架能启动（GET /health -> 200）。
目录存在不等于实现存在——除 core / api.health / schemas.health 外，
其余层在本步均保持空壳，等到对应 Vertical Slice 时再实现。
"""
