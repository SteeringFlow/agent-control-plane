"""控制面 API（Step 5.1：空壳）。

未来承载：Agent / Channel / Connection / Routing / Policy / Tenant
等管理类接口（CRUD 与配置变更）。

边界：只做 HTTP 参数校验与响应装配，业务逻辑一律下沉到 application。
本步不预置任何 router——等第一个管理类 Vertical Slice 到来时再建。
"""
