# Step 9.6 — Delivery Execution

> 8.6 Delivery 语义的第一次真实执行——**Runtime 闭环的最后一段**：
> `AgentResponse → OutboundResponse → ChannelAdapter → Channel`。
> 与 9.5 完全对称；本阶段不接真实 WeCom，用 Test Channel Adapter 注入。

## 链路

```
AgentResponse
    ↓ OutboundResponse（channel_id / external_conversation_id / content / metadata?）
    ↓ ChannelAdapter.deliver(outbound)
    ↓ Channel
```

**Delivery is transport, not routing**（8.6 延续）。

## 交付物

- `app/repositories/channel_adapter.py`：`ChannelAdapter` **Port**
  （`deliver(outbound)`；未来实现落地 `app/adapters/channels/`，本阶段不写实现）；
- `app/application/delivery.py`：`DeliveryExecutor(channel_adapter)`——**不依赖
  UoW / Repository**（Delivery 不需要读库）；
- `app/application/errors.py`：`ChannelDeliveryFailedError`（CHANNEL_DELIVERY_FAILED）
  ——**没有新造** ChannelNotFound / ChannelAuthFailed / ChannelTimeout /
  ChannelRateLimited / ChannelTransportFailed。

## 边界锁定

- 只消费 OutboundResponse：channel_id 来自它、external_conversation_id **原样**
  交给 Adapter、content **原样**交付；
- 不重新 Routing / Identity Resolution / Session Resolution / 不找 Agent /
  不读 Connection 决定目的地（模块 import 面 = 只有 ChannelAdapter Port + 契约 +
  错误）；
- Adapter transport/protocol 失败 → `ChannelDeliveryFailedError`；
- Delivery failure 是 **delivery** 失败，不变成 processing failure
  （processing_status ≠ delivery_status，4.2/5.3 冻结）；
- 不直接访问 ORM / SQLAlchemy；不创建 DeliveryService / ChannelManager /
  WeComAdapter；Test Adapter 只存在于测试中（app/adapters 仍空壳）。

## 一个真实工程问题（留给 9.7 前决定）

9.3 的 `CREATE_NEW` 只是**决策**，目前还没有 Session Create——而 9.5 需要
`control_plane_session_id`。**谁在 CREATE_NEW 之后创建 Session** 是 Session
Persistence 执行边界的问题；9.6 只证明 Delivery，**现在不提前补**，等 9.7 E2E
编排时再落。

## 验收

`tests/test_step9_6_delivery_execution.py`（15 项）：成功把 outbound 原样交给
Adapter / 可选字段 / 不依赖任何 Repository/UoW（无 get_by_id / find_*）/
8 类非法输入 Fail Closed（Adapter 不被调用）/ transport 失败 →
CHANNEL_DELIVERY_FAILED / 不新造底层 Channel 错误（AST 精确类名）/
processing ≠ delivery 语义冻结 / 模块类面 = {DeliveryExecutor} / adapters 空壳 /
mutation（越层 import、注入 DeliveryService → 本守卫 + 8.6 组件扫描报红）。

既有冻结精确扩边：5.7 repositories 文件面 +channel_adapter.py、application 文件面
×5 +delivery.py、6.5 EXPECTED_ERRORS +1、6.7 test_m Port 文件白名单 +
channel_adapter.py、8.6 组件扫描白名单（ChannelAdapter / DeliveryExecutor /
ChannelDeliveryFailedError）。

全仓 `pytest -q` = 508 passed，lint 0。
