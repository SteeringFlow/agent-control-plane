"""外部世界适配器（最外层）。

- ``channels/`` 入站渠道适配器：WeCom、飞书、钉钉、Webhook 等。
  负责把渠道私有协议翻译成应用内部可用的入站消息。
- ``agents/``   出站 Agent 适配器：以 ACP 协议调用下游 Agent，
  并把 Agent 响应翻译回内部结果。

边界：适配器可以认识渠道 / Agent 的协议细节与 HTTP 客户端，
但不承载业务规则——业务规则归 domain/application。
"""
