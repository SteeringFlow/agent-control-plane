"""HTTP 契约层（Pydantic）。

只允许被 api 层依赖；不允许反向流入 application / domain。
这里定义的是「线上协议」，变更需谨慎：字段一旦发布即是对外承诺。
"""
