"""Channel Inbound Use Case（Step 10.2.1）—— 入站边界的 Application 薄编排层。

链路（10.2.1 冻结）：

    HTTP facts
        ↓
    Application（本模块）
        ↓ ChannelRepository（定位渠道）
        ↓ InboundChannelAdapter（app/repositories/channel_inbound.py 端口）
        ↓ InboundInteraction（8.1 契约）
        ↓ intake.create_pending(inbound)  ← **12.9.4.6：ACK 边界，到此为止**

**12.9.4.6 Intake ACK 迁移**：本模块**彻底失去 Runtime 执行能力**——
`handle_message` 只把入站事实落为一条 PENDING 并**立即提交**，然后返回
interaction_id；Agent 执行由 Worker 在**响应之后**独立驱动（异步执行路径）。

    Inbound → create_pending → COMMIT → HTTP 200
                                  ↓（响应之后）
                            Worker.process(id)

因此 **ACK ≠ 执行**：HTTP 200 表示「已持久化接收」，不表示「Agent 已处理」。
本模块不再 import 任何 Runtime 执行契约（既不依赖具体 RuntimeCoordinator，
也不依赖 RuntimeExecutionCoordinator Protocol）。

**Application 只做一件事**：让指定 Channel 产生一次 InboundInteraction 并把它
交给 intake 落库。它知道「我要让这个 Channel 产生一个 InboundInteraction」，
但**不知道 WeCom 是怎么产生它的**、也**不再知道谁会执行它**：

不做（全部归属 InboundChannelAdapter，由 Port 抽象）：
- SHA1 验签 / AES 解密 / XML 解析 / MsgType 判断 / WeCom 字段 / WeCom 重试；
- 不操作 FastAPI Request（不 import FastAPI，test_import_boundaries 守卫）。

失败语义（10.2.1 正式裁决）：
- 入站协议失败（InboundSignatureError / InboundFormatError / InboundChannelConfigError）
  从 Adapter 原样传播 → HTTP 边界映射 403/400/500 —— **不进入** Runtime
  Failure Model（Runtime Failure Model 从 InboundInteraction 才开始）；
- Runtime typed error（Identity/Routing/Session/Dispatch 的 ApplicationError）
  12.9.4.6 起**不再经本路径传播**——执行已移出请求链路，改由 Worker 承担：
  HTTP 200 只确认「已持久化接收」，Runtime 失败落在 interaction 终态
  （processing=FAILED × delivery=PENDING）与 stage_failure Evidence 上，
  不再映射为 HTTP 5xx。

**12.9.4.9 状态感知重复接纳**：渠道重推（同一 `(channel_id,
external_interaction_id)` 再次入站）不再走到「create → UNIQUE 冲突 →
IntegrityError → HTTP 500」，而是**先回查**既有行：

- 已存在 → 直接返回既有 id：**ACK 200 + STOP**（不 create、不回灌队列）；
- 不存在 → 才 `create_pending`。

五种既有状态一律 STOP，**含 `retryable=True`**——重试 / 重执行是独立裁决，
不在「接纳」语义里（10.5.3 已记录「重推 = 重新执行的机会」是未决产品语义）。

**Cloud-0.1.3.1 WeCom Webhook Tenant Resolution**：入站接纳必须是 tenant-scoped 的。
顺序（写死，B §10/§12）：

    URL channel_id（仅定位）
        ↓ _load_channel            ← bootstrap 定位读（无 tenant 过滤，不构成信任）
        ↓ adapter.parse_message    ← WeCom 验签 / 解密 / 归一化（**信任闸门**）
        ↓ 已认证 Channel.tenant_id ← tenant 取自**已认证对象**，绝不取自 URL
        ↓ TenantScope
        ↓ tenant-scoped UoW        ← 幂等回查 + PENDING 落库（可信写入）

- **URL channel_id 只是路由定位**：它是「加载 Channel 的键」，不是 authentication
  proof，也不是 tenant proof；伪造它只命中 404/500 或该 Channel 实际所属租户。
- **验签前不得建立可信 TenantScope**：tenant 的解析与使用都排在验签**之后**；
  验签前的读取只是 bootstrap 定位（`find_tenant_id_by_id` 是在验签后才被调用）。
- **复用 B 的 `TenantScope` / UoW**：不新建 TenantResolver / 隔离机制；未注入
  `tenant_scoped_factory` 时退回缺省作用域（OSS / 单租户 legacy，行为 == 隔离前）。

**Cloud-0.1.5-CP2 Inbound Trust Gate（两段式）**：入站信任拆成**两段**，缺一不可：

    段 1（本模块） 路由信任模型：本入口是否服务该 `channel.type`？
                     —— `expected_channel_type` 由 HTTP 路由显式声明（见 M6.6-C3-CP2）
    段 2（Adapter） 渠道信任材料：签名 / 解密 / ReceiveId（`wecom`）或订阅连接（`wecom_bot`）

因此 `accept_message` / `handle_handshake` 新增 `expected_channel_type`：

- 位置**固定在 `_load_channel` 之后、`parse_message` 之前**（顺序即不变量）；
- 不匹配 → `InboundSignatureError` → HTTP 403；**不产生 Interaction**；
- 默认 `None` = 不施加该段判定（供 WS 长连接等**非 HTTP 入口**与既有调用方使用，
  行为与隔离前逐字节一致）；
- 为什么需要它：`MultiChannelAdapter` 的分派依据是 `channel.type`（`registry.py`），
  而 `wecom_bot` 与 `local` 的 Adapter **不做逐请求验签**（信任来自订阅连接 / 开发渠
  道语义）。若 HTTP 路由无条件按类型分派，匿名请求即可把 `aibot_msg_callback` 注入
  一个 `wecom_bot` 渠道——形成「未认证的入站面」（M6.6-C3-CP2 审计 §7）。
  **该判定不得下移至 Adapter**：WS 合法路径与 HTTP 注入路径调用的是同一个
  `parse_message`、同一组空参数，Adapter 无法区分二者。

纪律：本段判定**只做类型比对**，不触碰 Identity / Grant / Authorization / Router，
也不依赖 Agent；它不得被搬进 Bootstrap（Trust Gate 是 Bootstrap 的**前提**，不是
其中一段）。
"""

from collections.abc import Callable
from dataclasses import dataclass

from app.application.interaction_recording import PersistingCoordinator
from app.core.tenant import TenantScope, default_tenant_scope
from app.domain.channel import Channel
from app.repositories.channel_inbound import (
    InboundChannelAdapter,
    InboundChannelConfigError,
    InboundSignatureError,
)
from app.repositories.unit_of_work import UnitOfWork


@dataclass(frozen=True)
class InboundAcceptance:
    """入站接纳结果：`interaction_id` + 该入站所属的 `TenantScope`。

    `interaction_id is None` = 已确认接收但不产生 CP 交互（acknowledge-ignore）。
    `tenant_scope` 是**已认证 Channel** 解析出的隔离上下文（Cloud-0.1.3.1），
    供调用方把随后的执行（Worker 触发）落到同一租户；非租户解析路径下即缺省作用域。
    """

    interaction_id: int | None
    tenant_scope: TenantScope


class ChannelInboundService:
    """入站用例编排者：Channel 定位 + Adapter 调用 + **PENDING 入队（ACK 边界）**。

    12.9.4.6：本类**不再持有任何 Runtime 执行能力**——注入的是 intake
    （persistence phase API），只用它的 `create_pending`。
    执行权归 Worker（`app/application/worker.py`），二者是同一次原子迁移
    （12.9.3.6 §11）拆开的两半，不可只部署其一。

    依赖全部构造注入（UoW Port / intake / 入站 Adapter Port），
    本类不实例化任何 Adapter / Repository / ORM，也不知道 WeCom 的存在。
    """

    def __init__(
        self,
        *,
        uow_factory: Callable[[], UnitOfWork],
        intake: PersistingCoordinator,
        inbound_adapter: InboundChannelAdapter,
        tenant_scoped_factory: Callable[[int], Callable[[], UnitOfWork]] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._intake = intake
        self._adapter = inbound_adapter
        # Cloud-0.1.3.1：由「已认证 Channel 的 tenant_id」绑定 UoW 的工厂提供者。
        # None = 未启用租户解析（单租户 / legacy 路径，行为 == 隔离前）。
        # 非 None 时：定位读走 self._uow_factory（bootstrap，无 tenant 过滤），
        # 可信写入走 self._tenant_scoped_factory(tenant_id)（tenant-scoped UoW）。
        self._tenant_scoped_factory = tenant_scoped_factory

    # ------------------------------------------------------------ channel 定位

    def _load_channel(self, channel_id: int) -> Channel:
        """定位可承接入站的 Channel；不存在 / 未启用 → InboundChannelConfigError。

        这是入站路径上唯一的 Repository 读取；ID 是 HTTP 路由事实（回调 URL 中
        显式携带），Application 不猜测渠道归属。
        """
        if not isinstance(channel_id, int) or channel_id <= 0:
            raise InboundChannelConfigError(f"channel_id 非法：{channel_id!r}")
        with self._uow_factory() as uow:
            channel = uow.channels.get_by_id(channel_id)
        if channel is None:
            raise InboundChannelConfigError(
                f"channel_id={channel_id} 不存在（回调 URL 指向未知渠道）")
        if channel.status != "active":
            raise InboundChannelConfigError(
                f"channel_id={channel_id} 未启用（status={channel.status}）")
        return channel

    # ------------------------------------------------------------ 入站 Trust Gate（CP-2）

    def _assert_route_trust(
        self, channel: Channel, expected_channel_type: str | None,
    ) -> None:
        """段 1 · 路由信任模型：本入口是否服务该 `channel.type`。

        顺序不变量：**`_load_channel` 之后、`parse_message` 之前**。
        不匹配 → `InboundSignatureError`（HTTP 403）→ 不产生 Interaction。

        为什么用 `InboundSignatureError`（而非 InboundFormatError / ConfigError）：
        请求形状合法、服务端配置也合法，**唯一的问题是它与本入口的信任模型不匹配**
        ——即「本请求在本入口不可认证」，与「坏签名」同一语义类（403）。
        不新造错误码（Package 2-C 纪律）。

        `expected_channel_type is None` → 不施加该段判定（非 HTTP 入口：WS 长连接）。
        """
        if expected_channel_type is None:
            return
        if channel.type != expected_channel_type:
            raise InboundSignatureError(
                f"channel_id={channel.id} type={channel.type!r} 不经本入站入口接入："
                f"本入口服务 type={expected_channel_type!r}；"
                "渠道信任模型与入口不匹配 → Fail Closed")

    # ------------------------------------------------------------ 租户解析（0.1.3.1）

    def _tenant_scope(self, channel: Channel) -> TenantScope:
        """由**已认证** Channel 解析 TenantScope（Cloud-0.1.3.1，B §10/§12）。

        **只在 WeCom 验签通过之后调用**——此前的 `_load_channel` 只是 URL routing
        hint 的定位读（bootstrap，无 tenant 过滤），**不构成信任**。tenant 取自
        已认证对象的 `tenant_id`（经 bootstrap 投影读出，`find_tenant_id_by_id`），
        **绝不取自 URL**。

        未启用租户解析（`tenant_scoped_factory is None`）→ 缺省作用域
        （OSS / 单租户 legacy，行为 == 隔离前）。
        """
        if self._tenant_scoped_factory is None:
            return default_tenant_scope()
        if channel.id is None:
            raise InboundChannelConfigError(
                "未持久化的 Channel（无 id）不能解析 tenant")
        with self._uow_factory() as uow:
            tenant_id = uow.channels.find_tenant_id_by_id(channel.id)
        if tenant_id is None:
            raise InboundChannelConfigError(
                f"channel_id={channel.id} 无 tenant 归属（已认证对象缺 tenant_id）")
        return TenantScope(tenant_id=tenant_id)

    def _write_factory(self, scope: TenantScope) -> Callable[[], UnitOfWork] | None:
        """可信写入所用的 UoW 工厂；未启用租户解析 → None（沿用 intake 缺省工厂）。

        由已认证租户 `scope.tenant_id` 绑定，使幂等回查与 PENDING 落库都落在
        该租户的 tenant-scoped UoW 上（而非入口缺省租户）。
        """
        if self._tenant_scoped_factory is None:
            return None
        return self._tenant_scoped_factory(scope.tenant_id)

    # ------------------------------------------------------------ 用例

    def handle_handshake(
        self,
        channel_id: int,
        *,
        signature: str,
        timestamp: str,
        nonce: str,
        echostr: str,
        expected_channel_type: str | None = None,
    ) -> str:
        """URL 验证握手：**段 1 路由信任 → 段 2 验签** → 明文 echostr。

        CP-2：`expected_channel_type`（HTTP 路由声明）在 `_load_channel` 之后立即判定
        ——`wecom_bot` / `local` 渠道在 WeCom 回调入口一律 Fail Closed（403），
        不会走到 `verify_handshake`（后者对 `wecom_bot` 本就抛 ConfigError）。
        """
        channel = self._load_channel(channel_id)
        self._assert_route_trust(channel, expected_channel_type)   # ← 段 1（CP-2）
        return self._adapter.verify_handshake(
            channel,
            signature=signature, timestamp=timestamp, nonce=nonce, echostr=echostr,
        )

    def accept_message(
        self,
        channel_id: int,
        *,
        signature: str,
        timestamp: str,
        nonce: str,
        body: str,
        expected_channel_type: str | None = None,
    ) -> InboundAcceptance:
        """消息入站：定位 → **段 1 路由信任** → **段 2 验签** → 可信租户 → tenant-scoped 接纳。

        Cloud-0.1.3.1 + M6.6-C3-CP2 顺序（写死，B §10/§12）：

            1. `_load_channel`：URL `channel_id` 仅作**定位**（bootstrap，无 tenant
               过滤）——不构成信任；
            2. `_assert_route_trust`：本入口是否服务该 `channel.type` —— **段 1**
               （HTTP 路由声明；不匹配 → 403，**不产生 Interaction**）；
            3. `adapter.parse_message`：WeCom 验签 / 解密 / 归一化 —— **段 2（信任闸门）**；
            4. 验签**通过之后**，才由已认证 Channel 解析 `TenantScope`（绝不取自 URL）；
            5. 幂等回查 + `create_pending` 均落在该租户的 tenant-scoped UoW 上。

        12.9.4.6 ACK 边界 + 12.9.4.9 状态感知重复接纳（语义完全不变）：
        Adapter 返回 None（不支持 MsgType）→ acknowledge-ignore（interaction_id=None）；
        同一 `(channel_id, external_interaction_id)` 已存在 → 返回既有 id：ACK + STOP；
        否则 `create_pending`（独立短事务）→ 返回新 id。**绝不执行 Runtime**。
        """
        channel = self._load_channel(channel_id)
        self._assert_route_trust(channel, expected_channel_type)   # ← 段 1（CP-2）
        inbound = self._adapter.parse_message(          # ← 段 2 信任闸门（验签）
            channel,
            signature=signature, timestamp=timestamp, nonce=nonce, body=body,
        )
        scope = self._tenant_scope(channel)             # ← 验签之后才建立可信作用域
        if inbound is None:
            return InboundAcceptance(None, scope)       # acknowledge-ignore
        factory = self._write_factory(scope)
        existing_id = self._existing_row_id(
            channel_id, inbound.external_interaction_id, factory)
        if existing_id is not None:
            return InboundAcceptance(existing_id, scope)  # 12.9.4.9：ACK，STOP
        # 仅 tenant 解析路径才把 tenant-scoped 工厂透传给 intake（未启用时保持
        # 既有单参调用形状，legacy / 轻量替身零改动）。
        if factory is None:
            return InboundAcceptance(self._intake.create_pending(inbound), scope)
        return InboundAcceptance(
            self._intake.create_pending(inbound, uow_factory=factory), scope)

    def handle_message(
        self,
        channel_id: int,
        *,
        signature: str,
        timestamp: str,
        nonce: str,
        body: str,
        expected_channel_type: str | None = None,
    ) -> int | None:
        """兼容入口：语义等价 `accept_message(...).interaction_id`。

        保留原签名（`int | None`）供既有调用方使用；需要拿到入站所属
        `TenantScope`（把执行落到同一租户）的调用方改用 `accept_message`。

        CP-2：`expected_channel_type` 原样透传（HTTP 入口必须显式声明）。
        """
        return self.accept_message(
            channel_id, signature=signature, timestamp=timestamp,
            nonce=nonce, body=body, expected_channel_type=expected_channel_type,
        ).interaction_id

    def _existing_row_id(
        self,
        channel_id: int,
        external_interaction_id: str,
        uow_factory: Callable[[], UnitOfWork] | None = None,
    ) -> int | None:
        """按 Interaction Identity 回查既有行的 id；不存在 → None。

        12.9.4.9：渠道重推携带同一 `(channel_id, external_interaction_id)`，此处
        只回答「这条交互是否已有」——**既不检查也不解释**它的状态。判定一旦进入
        接纳层，就会把「重推 = 重试机会」这个未决语义偷偷编码进生产路径。

        读回复用 10.5.2 已物化、但此前在生产无调用者的既有 primitive
        `uow.interactions.find_by_channel_and_external_id`——不新增 Port 方法、
        不改 schema。身份 scope 纪律：不同 Channel 的同名外部 ID 互不相干。

        Cloud-0.1.3.1：可选 `uow_factory`（tenant-scoped）——使幂等回查与随后的
        写入同处一个租户作用域（缺省 None = 沿用本实例工厂）。
        """
        factory = uow_factory if uow_factory is not None else self._uow_factory
        with factory() as uow:
            existing = uow.interactions.find_by_channel_and_external_id(
                channel_id, external_interaction_id)
        return existing.id if existing is not None else None
