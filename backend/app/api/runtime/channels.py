"""Runtime 数据面入站端点（Step 10.2.1 WeCom Inbound Boundary）。

职责边界（空壳期已预留，本步兑现）：

    FastAPI（本模块：HTTP 事实提取 + 应答序列化 + 协议异常→状态码映射）
        ↓
    application.channel_inbound.ChannelInboundService
        ↓
    WeComAdapter → InboundInteraction → intake.create_pending（PENDING + COMMIT）
        ↓
    HTTP 200（ACK）
        ↓  ← 响应之后
    Worker.process(interaction_id) → RuntimeCoordinator

入站端点只负责：
- 提取 HTTP 事实（path channel_id / query 四参数 / body）；
- 把入站协议异常映射为 HTTP 状态码（10.2.1 裁决：**不进入 Runtime Failure
  Model**）：InboundChannelConfigError→500、InboundSignatureError→403、
  InboundFormatError→400；
- Runtime typed error 不在此吞包 → 冒泡为 500（服务端处理失败）。

不负责：验签 / 解密 / XML / 编排逻辑 / 直接读写数据库（空壳期冻结纪律）。

**12.9.4.6 Intake ACK 迁移**：本模块成为 **ACK ≠ 执行** 的落地处——
`handle_message` 返回 interaction_id 后立即应答 200，Agent 执行经
`BackgroundTasks` 在**响应之后**交给 Worker。Runtime 执行不再位于请求链路内，
故 Runtime typed error 不再映射为 HTTP 5xx（落到 interaction 终态与
Evidence 上）。

装配方式：`build_channel_inbound_api(service, worker, worker_for=...)` 工厂 +
依赖注入（与 Runtime 六段一致的注入纪律）。**12.6 起 main.py 已装配生产实例**
（组合根同时注入 persistence phase API 与真实 ACPAdapter / WeComAdapter，
装配一致性由 test_step12_6 守护）；本 router 此前由测试（Layer 3）装配。

Cloud-0.1.3.3：入站 ACK 之后，执行经 `worker_for(已认证 TenantScope)` 落到
**该租户的 Worker**（0.1.3.2 Gate：Trigger 1 与 Trigger 2 汇合到同一租户的
Runtime）。`worker_for=None` = 单租户 legacy（所有租户落到 `worker`，
行为 == 隔离前）——既有测试装配零改动。

**Cloud-0.1.5-CP2 Inbound Trust Gate（本模块是段 1 的唯一声明处）**：
入站信任分两段——**段 1 = 本路由声明它服务哪种渠道类型**（= 承认哪种信任模型），
**段 2 = Adapter 的渠道信任材料**（wecom 逐请求验签 / wecom_bot 订阅连接）。
两段缺一不可，且顺序不可交换。

为什么必须由**路由**声明类型（而不是继续让渠道分派器按**渠道类型字段**自行
选择 Adapter）：

    GET/POST /{id}/wecom/webhook   → 信任模型 = 每请求验签（wecom）
    POST     /{id}/local/message   → 信任模型 = 无认证（local，开发渠道语义）
    WS       aibot_msg_callback    → 信任模型 = 订阅连接（wecom_bot，不在本模块）

`wecom_bot` 的 Adapter **不做逐请求验签**（长连接协议在帧上没有签名材料，
真实认证发生在 `aibot_subscribe`）。若允许 HTTP 路由按类型把它分派出去，
一个知道自己 `channel_id` 的匿名调用者即可 POST 一段 `aibot_msg_callback`
JSON，使该渠道产生一条真实 Interaction（M6.6-C3-CP2 审计 §7 矩阵：
3 条未认证注入面）。故每条 HTTP 路由**显式声明**它服务的类型，不匹配即
Fail Closed（403，不产生 Interaction）——见下方 `*_CHANNEL_TYPE` 常量。

对称性：`wecom_bot.verify_handshake` 早已对本路由 fail closed（「无 URL 握手
语义」）；CP-2 把同一纪律施加到消息分支，而非引入新机制。
"""

from collections.abc import Callable

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse

from app.application.channel_inbound import ChannelInboundService
from app.application.worker import Worker
from app.core.tenant import TenantScope
from app.repositories.channel_inbound import (
    InboundSignatureError,
    InboundChannelConfigError,
    InboundFormatError,
)

#: Cloud-0.1.5-CP2 —— 每条入站路由**显式声明它服务的渠道类型**（= 声明信任模型）。
#:
#: 这是路由边界的事实，不是可调参数：把某个渠道类型搬进一个与其信任模型不匹配的
#: 入口，等于让「无认证入口」冒充「有认证入口」。因此：
#:
#:   · `wecom_bot` **不得**出现在任何 HTTP 入站路由的声明里（唯一合法入口 = 活连接，
#:     由 `main.py::_on_bot_frame` 驱动，不经本模块）；
#:   · 新增路由 / 新增渠道类型时，必须在此登记对应关系（`test_m6_6_c3_cp2_*` 的
#:     源码 Guard 会拒绝「HTTP 路由未声明类型」与「HTTP 路由声明 wecom_bot」）。
WECOM_WEBHOOK_CHANNEL_TYPE = "wecom"
LOCAL_MESSAGE_CHANNEL_TYPE = "local"


def build_channel_inbound_api(
    service: ChannelInboundService,
    worker: Worker,
    *,
    worker_for: Callable[[TenantScope], Worker] | None = None,
) -> APIRouter:
    """构造入站 webhook router（依赖注入：ChannelInboundService + 执行者）。

    12.9.4.6：执行者是必填项——intake 与执行者必须**同时**装配，避免出现
    「只入队、无人执行」的半迁移状态（12.9.3.6 §11 原子迁移）。

    Cloud-0.1.3.3：`worker_for` 把**已认证 TenantScope**（0.1.3.1 验签后解析，
    绝不取自 URL）映射到该租户的 Worker——per-tenant fan-out 的 Trigger 1 入口。
    None = 单租户 legacy：所有租户落到 `worker`（OSS 行为 == 隔离前）。
    """

    def _worker_for(scope: TenantScope) -> Worker:
        return worker_for(scope) if worker_for is not None else worker
    router = APIRouter(prefix="/api/channels", tags=["runtime-inbound"])

    def _map_protocol_errors(exc: Exception) -> HTTPException:
        """入站协议异常 → HTTP 状态码（唯一映射点，10.2.1 裁决表）。"""
        if isinstance(exc, InboundChannelConfigError):
            return HTTPException(status_code=500, detail="channel inbound misconfigured")
        if isinstance(exc, InboundSignatureError):
            return HTTPException(status_code=403, detail="inbound authentication failed")
        if isinstance(exc, InboundFormatError):
            return HTTPException(status_code=400, detail="inbound message malformed")
        return HTTPException(status_code=500, detail="inbound error")

    @router.get("/{channel_id}/wecom/webhook")
    def wecom_verify(
        channel_id: int,
        msg_signature: str,
        timestamp: str,
        nonce: str,
        echostr: str,
    ) -> PlainTextResponse:
        """WeCom URL 验证握手：**段 1 路由信任** → 验签 + 解密 echostr → 原文应答。

        CP-2：本入口只服务 `wecom`（每请求验签的信任模型）。其它类型 → 403。
        """
        try:
            plaintext = service.handle_handshake(
                channel_id,
                signature=msg_signature, timestamp=timestamp,
                nonce=nonce, echostr=echostr,
                expected_channel_type=WECOM_WEBHOOK_CHANNEL_TYPE,
            )
        except (InboundChannelConfigError, InboundSignatureError, InboundFormatError) as exc:
            raise _map_protocol_errors(exc) from exc
        return PlainTextResponse(plaintext)

    @router.post("/{channel_id}/wecom/webhook")
    async def wecom_message(
        channel_id: int,
        msg_signature: str,
        timestamp: str,
        nonce: str,
        request: Request,
        background_tasks: BackgroundTasks,
    ) -> Response:
        """WeCom 消息回调：归一化 → PENDING 落库并提交 → 200 空体（ACK）。

        12.9.4.6：执行在**响应之后**经 BackgroundTasks 交给 Worker（G4 / G5）——
        Runtime 不在请求链路内，故本端点不再映射 Runtime typed error。

        CP-2：本入口只服务 `wecom`（逐请求验签）。`wecom_bot` / `local` → 403，
        **不产生 Interaction**（长连接协议的帧没有 per-request 认证材料，
        本入口原理上无法认证它）。
        """
        body = (await request.body()).decode("utf-8", "replace")
        try:
            acceptance = service.accept_message(
                channel_id,
                signature=msg_signature, timestamp=timestamp,
                nonce=nonce, body=body,
                expected_channel_type=WECOM_WEBHOOK_CHANNEL_TYPE,
            )
        except (InboundChannelConfigError, InboundSignatureError, InboundFormatError) as exc:
            raise _map_protocol_errors(exc) from exc
        if acceptance.interaction_id is not None:
            # 响应发出之后才执行 —— ACK ≠ Agent Execution。
            # Cloud-0.1.3.3：落到「已认证租户」的 Worker（scope 取自验签后的
            # Channel，0.1.3.1——绝不取自 URL）。
            background_tasks.add_task(
                _worker_for(acceptance.tenant_scope).process,
                acceptance.interaction_id)
        return Response(status_code=200)

    @router.post("/{channel_id}/local/message")
    async def local_message(
        channel_id: int,
        request: Request,
        background_tasks: BackgroundTasks,
    ) -> dict:
        """本地渠道入站（15.2.3 Local First Success，external-system-free）。

        请求体即 local wire（JSON `{"external_id", "text", "message_id"?}`），
        由 MultiChannelAdapter 按渠道类型路由到 LocalChannelAdapter。

        与 WeCom 端点共享**同一条** ACK 边界（12.9.4.6）：
        归一化 → PENDING 落库并提交 → 200 只表示「已持久化接收」；
        执行在响应之后经 BackgroundTasks 交给 Worker —— **绝不在此同步执行 Runtime**。
        返回 interaction_id，便于开发者经 Observation（GET /api/interactions/{id}）
        读取 Agent 响应。

        CP-2：本入口只服务 `local`（本渠道的信任模型是**无认证**的开发语义）。
        `wecom`（走本入口会因空签名验签失败）与 `wecom_bot`（本入口无其任何信任
        依据）→ 403，**不产生 Interaction**。
        """
        body = (await request.body()).decode("utf-8", "replace")
        try:
            acceptance = service.accept_message(
                channel_id,
                signature="", timestamp="", nonce="", body=body,
                expected_channel_type=LOCAL_MESSAGE_CHANNEL_TYPE,
            )
        except (InboundChannelConfigError, InboundSignatureError, InboundFormatError) as exc:
            raise _map_protocol_errors(exc) from exc
        if acceptance.interaction_id is not None:
            # 响应发出之后才执行 —— ACK ≠ Agent Execution（同一异步边界；
            # Cloud-0.1.3.3：同一 per-tenant Worker 解析）
            background_tasks.add_task(
                _worker_for(acceptance.tenant_scope).process,
                acceptance.interaction_id)
        return {"interaction_id": acceptance.interaction_id, "status": "accepted"}

    return router
