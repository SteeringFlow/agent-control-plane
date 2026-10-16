"""Application 层错误类型（Step 6.2：Application-level error mapping）。

- 只表达「用例失败」，不携带 SQLAlchemy / FastAPI / HTTP 概念；
- 未来 API 层把它们映射成 HTTP 响应/错误码；
- 跨层统一错误语义（core/errors.py）仍延后——等出现真正跨层错误时再建，
  这里不是它的替代品。
"""


class ApplicationError(Exception):
    """Application 层用例错误基类。"""


class AgentNotFoundError(ApplicationError):
    """按 id 操作一个不存在的 Agent（update 等需要实体存在的用例）。"""

    def __init__(self, agent_id: int) -> None:
        super().__init__(f"Agent {agent_id} 不存在")
        self.agent_id = agent_id


class DuplicateAgentError(ApplicationError):
    """同名 Agent 已存在（Application 层软约束，非 DB 唯一约束）。

    agents 表没有冻结 name 唯一约束，此处是管理面的应用级去重守卫，
    存在 TOCTOU 窗口；若真实需求要求硬保证，应通过 schema/migration
    增加唯一约束后再移除本守卫。
    """

    def __init__(self, name: str) -> None:
        super().__init__(f"Agent 已存在：{name}")
        self.name = name


class DuplicateExternalAgentError(ApplicationError):
    """同一 Tenant 内已有另一条 Agent 记录代表同一外部 Agent 身份（M7-AUI-10.5）。

    身份 = (tenant_id, provider=workbuddy, workbuddy_agent_id)；应用级软约束
    （无 DB UNIQUE，审计 C5 = NOT YET），存在 TOCTOU 窗口。API Key 是凭据，
    **绝不**参与身份判断，也绝不出现在本错误的信息里——错误只携带安全的
    定位信息（冲突 Agent 的 id / name），供前端解释冲突并引导复用。
    """

    def __init__(
        self,
        external_agent_id: str,
        existing_agents: list[tuple[int, str]],
    ) -> None:
        self.external_agent_id = external_agent_id
        self.existing_agents = list(existing_agents)
        if len(self.existing_agents) == 1:
            agent_id, name = self.existing_agents[0]
            tail = f"（已有 Agent {agent_id}「{name}」）"
        else:
            tail = f"（已有 {len(self.existing_agents)} 条 Agent 记录使用该身份）"
        super().__init__(
            f"WorkBuddy Agent 已存在：workbuddy_agent_id={external_agent_id}{tail}"
        )


class InvalidAgentInputError(ApplicationError):
    """Agent 输入不合法（输入契约校验失败）。"""


class ChannelNotFoundError(ApplicationError):
    """按 id 操作一个不存在的 Channel（update 等需要实体存在的用例）。"""

    def __init__(self, channel_id: int) -> None:
        super().__init__(f"Channel {channel_id} 不存在")
        self.channel_id = channel_id


class DuplicateChannelError(ApplicationError):
    """同平台（type）+ 同名的 Channel 已存在（应用级软约束，非 DB 唯一约束）。

    与 Agent 的全局 name 去重不同：Channel 的重复性是平台限定的——
    同一渠道类型下的同名渠道才是重复；不同平台（wecom / feishu / ...）
    的同名渠道（如都叫“客服台”）合法共存。

    channels 表没有冻结唯一约束，本守卫存在 TOCTOU 窗口；若真实需求要求
    硬保证，应通过 schema/migration 增加 (type, name) 唯一约束后再移除。
    """

    def __init__(self, channel_type: str, name: str) -> None:
        super().__init__(f"Channel 已存在：type={channel_type}, name={name}")
        self.channel_type = channel_type
        self.name = name


class InvalidChannelInputError(ApplicationError):
    """Channel 输入不合法（输入契约校验失败）。"""


class DuplicateBotChannelError(ApplicationError):
    """同一 tenant 范围内已有 active `wecom_bot` Channel 使用相同 bot_id（M7-AUI-10.6.7）。

    bot_id 是 Channel.config 中的外部 Bot 身份（管理面**非敏感键**——敏感键集合
    是 token / encoding_aes_key / corpsecret / bot_secret，见 schemas/channels.py）；
    本错误的信息面**只含冲突渠道的 name / id**，绝不含 bot_id、bot_secret 或 config。

    与 `DuplicateChannelError` 分离的原因：409 语义不同（名称冲突 vs 外部身份冲突），
    前端需要可判别的错误结构（`code: "duplicate_bot_channel"`）给出「复用已有渠道」
    的下一步指引——不能让所有 409 都显示成名称重复。

    应用级软约束：channels 表没有（本轮也不加）唯一索引（10.6.6 审计 deferred
    decision），本守卫与 `(type, name)` 守卫同一 TOCTOU 窗口——并发创建的残余风险
    如实保留，不宣称数据库级并发唯一性保证。
    """

    def __init__(self, existing_name: str, existing_channel_id: int) -> None:
        super().__init__(
            f"已存在使用同一企业微信机器人（bot_id）的启用渠道："
            f"{existing_name}（#{existing_channel_id}），请复用该渠道或先停用它。")
        self.existing_channel_id = existing_channel_id
        self.existing_channel_name = existing_name


class UserNotFoundError(ApplicationError):
    """按 id 操作一个不存在的 User（update 等需要实体存在的用例）。"""

    def __init__(self, user_id: int) -> None:
        super().__init__(f"User {user_id} 不存在")
        self.user_id = user_id


class InvalidUserInputError(ApplicationError):
    """User 输入不合法（输入契约校验失败）。"""


class DuplicateBrowserLoginError(ApplicationError):
    """Browser 登录名（login）已被占用（`user_credentials.login` 全局唯一）。

    应用级预检给出明确语义；并发插入兜底由 DB 唯一约束完成（同 Identity /
    Connection 的重复处理纪律）。"""
    def __init__(self, login: str) -> None:
        super().__init__(f"Browser 登录名已存在：{login}")
        self.login = login


class InvalidBrowserAuthInputError(ApplicationError):
    """Browser Auth 输入不合法（login / password 为空，校验失败 → 422）。"""


class IdentityNotFoundError(ApplicationError):
    """按 id 操作一个不存在的 Identity（update 等需要实体存在的用例）。"""

    def __init__(self, identity_id: int) -> None:
        super().__init__(f"Identity {identity_id} 不存在")
        self.identity_id = identity_id


class DuplicateIdentityError(ApplicationError):
    """同渠道同外部身份已存在：UNIQUE(channel_id, external_id) 已被占用。

    与 Agent/Channel 不同，这是**有 DB 硬约束背书的重复**（4.2 冻结）：
    应用级预检（find_by_channel_and_external_id）给出明确的 Application
    错误语义；并发插入兜底由 DB 唯一约束完成。
    """

    def __init__(self, channel_id: int, external_id: str) -> None:
        super().__init__(
            f"Identity 已存在：channel_id={channel_id}, external_id={external_id}"
        )
        self.channel_id = channel_id
        self.external_id = external_id


class InvalidIdentityInputError(ApplicationError):
    """Identity 输入不合法（输入契约校验失败）。"""


class ConnectionNotFoundError(ApplicationError):
    """按 id 操作一个不存在的 Connection（update 等需要实体存在的用例）。"""

    def __init__(self, connection_id: int) -> None:
        super().__init__(f"Connection {connection_id} 不存在")
        self.connection_id = connection_id


class DuplicateConnectionError(ApplicationError):
    """同一对 (channel_id, agent_id) 的 Connection 已存在（绑定重复）。

    Access Path 的唯一键 = (channel_id, agent_id)，4.2 冻结的 UNIQUE 是硬兜底；
    应用级预检（find_by_channel_and_agent）给出明确的 Application 错误语义。
    一个 Channel → 一个 Agent 只允许一条受控 Connection（MVP 假设）。
    """

    def __init__(self, channel_id: int, agent_id: int) -> None:
        super().__init__(
            f"Connection 已存在：channel_id={channel_id}, agent_id={agent_id}"
        )
        self.channel_id = channel_id
        self.agent_id = agent_id


class InvalidConnectionInputError(ApplicationError):
    """Connection 输入不合法（输入契约校验失败）。"""


class DuplicateConnectionConversationError(ApplicationError):
    """同一 (channel_id, external_conversation_id, connection_id) 的授权已存在。

    与 DuplicateIdentityError 同类：**有 DB 硬约束背书**（四元组 UNIQUE，
    Package 2 / Step 1 冻结）——应用级预检给出明确语义，并发插入由
    DB 唯一约束兜底。
    """

    def __init__(self, channel_id: int, external_conversation_id: str,
                 connection_id: int) -> None:
        super().__init__(
            "授权已存在："
            f"channel_id={channel_id}, "
            f"external_conversation_id={external_conversation_id!r}, "
            f"connection_id={connection_id}"
        )
        self.channel_id = channel_id
        self.external_conversation_id = external_conversation_id
        self.connection_id = connection_id


class ConnectionConversationNotFoundError(ApplicationError):
    """按 id 操作一条不存在的授权（delete 等需要实体存在的用例）。"""

    def __init__(self, connection_conversation_id: int) -> None:
        super().__init__(f"授权 {connection_conversation_id} 不存在")
        self.connection_conversation_id = connection_conversation_id


class InvalidConnectionConversationInputError(ApplicationError):
    """授权输入不合法（键须为正整数、会话键非空白、connection 须属于该 channel）。"""


class IdentityResolutionFailedError(ApplicationError):
    """Runtime Identity Resolution 失败（Step 9.2，Fail Closed）。

    未找到 (channel_id, external_id) 对应的 Identity 时抛出——执行**停止**，
    绝不创建 Identity / User、绝不猜测。

    **失败条件收窄（Gate 2.6 D3 / Implementation Package 1）**：
    「找到 Identity 但其 user 缺失」**不再是失败**——ACP User 关联是可选的
    管理绑定，缺失时解析**成功**且 `IdentityResolution.user is None`。
    本失败码现在只表达「Identity 未命中」这一种情形。
    """

    def __init__(self, channel_id: int, external_identity_id: str) -> None:
        super().__init__(
            f"Identity Resolution 失败：channel_id={channel_id}, "
            f"external_identity_id={external_identity_id}"
        )
        self.channel_id = channel_id
        self.external_identity_id = external_identity_id


class AccessAuthorizationDeniedError(ApplicationError):
    """Runtime Access Authorization 拒绝（Implementation Package 2 / Step 2，Fail Closed）。

    该 Conversation 在 `connection_conversations` 中**没有任何授权记录**时抛出——
    执行**停止**：不进入 Routing、不创建 Session、不调用 Agent、不产生 Delivery。

    冻结映射（Package 2-C §7.6）：

        processing_status = failed
        delivery_status   = pending
        failure_stage     = authorization
        failure_code      = UNAUTHORIZED
        retryable         = false
        session_id        = NULL

    **本错误只表达「没有许可」，不表达「路径不可用」**：

    - 有许可但被许可的连接全部 inactive → 授权**通过**，随后由 Routing 抛
      `ROUTING_FAILED`（把「路径不可用」谎报成「未授权」会误导排查方向）；
    - 有许可但 `active ∩ granted` 多于一条 → 同样 `ROUTING_FAILED`
      （唯一性裁决归 Routing，授权不做这件事）。

    也**不表达**「身份问题」：未绑定 ACP User 的 External Identity
    （`Identity.user_id is None`）只要会话有许可就正常通过（Gate 2.6 D1/D2 +
    ADR-3 的 Conversation 作用域）。
    """

    def __init__(self, channel_id: int | None,
                 external_conversation_id: str | None) -> None:
        super().__init__(
            "Access Authorization 拒绝（UNAUTHORIZED，Fail Closed）："
            f"channel_id={channel_id}, "
            f"external_conversation_id={external_conversation_id!r}"
        )
        self.channel_id = channel_id
        self.external_conversation_id = external_conversation_id


class SessionResolutionFailedError(ApplicationError):
    """Runtime Session Resolution 失败（Step 9.3，Fail Closed）。

    会话解析输入不合法（identity_id / connection_id 非正整数、
    external_conversation_id 为空）时抛出——执行**停止**，绝不创建 Session /
    Identity / Connection，绝不猜 conversation、绝不跨 conversation 复用会话。
    """

    def __init__(self, identity_id: int | None, connection_id: int | None,
                 external_conversation_id: str | None) -> None:
        super().__init__(
            "Session Resolution 失败（Fail Closed）："
            f"identity_id={identity_id}, connection_id={connection_id}, "
            f"external_conversation_id={external_conversation_id!r}"
        )
        self.identity_id = identity_id
        self.connection_id = connection_id
        self.external_conversation_id = external_conversation_id


class RoutingFailedError(ApplicationError):
    """Runtime Routing 失败（Step 9.4，Fail Closed）。

    channel_id 不合法，或该 Channel 的 active Connection 数量 != 1
    （0 条 / 多条 → 无法唯一确定 Access Path）时抛出——执行**停止**，
    不猜测、不 fallback、不做优先级/权重/负载均衡。
    """

    def __init__(self, channel_id: int | None) -> None:
        super().__init__(f"Routing 失败（Fail Closed）：channel_id={channel_id}")
        self.channel_id = channel_id


class AgentDispatchFailedError(ApplicationError):
    """Runtime Agent Dispatch 失败（Step 9.5，Fail Closed；对应 AGENT_DISPATCH_FAILED）。

    Connection 缺失 / Connection 与 RoutingResult 不一致 / Adapter transport 失败
    时抛出。Adapter 的底层错误（auth / protocol / connection）一律**在此映射**，
    不新造 AgentConnectionFailed / AgentAuthenticationFailed / AgentProtocolFailed。
    """

    def __init__(self, agent_id: int | None = None,
                 connection_id: int | None = None) -> None:
        super().__init__(
            "Agent Dispatch 失败（Fail Closed）："
            f"agent_id={agent_id}, connection_id={connection_id}"
        )
        self.agent_id = agent_id
        self.connection_id = connection_id


class AgentResponseInvalidError(ApplicationError):
    """Agent 返回的响应不满足 AgentResponse 契约（Step 9.5；对应 AGENT_RESPONSE_INVALID）。

    Adapter 没有正确产生符合字段面的统一响应（content 非 str / external_session_id
    非 str|None / metadata 非 dict|None / 返回了非 AgentResponse 对象）时抛出——
    不猜 content、不继续 Runtime 成功链。
    """

    def __init__(self, agent_id: int | None = None) -> None:
        super().__init__(f"Agent 响应无效（AGENT_RESPONSE_INVALID）：agent_id={agent_id}")
        self.agent_id = agent_id


class ChannelDeliveryFailedError(ApplicationError):
    """Runtime Channel Delivery 失败（Step 9.6，Fail Closed；对应 CHANNEL_DELIVERY_FAILED）。

    OutboundResponse 不合法或 Channel Adapter transport/protocol 失败时抛出。
    底层失败（auth / protocol / transport / rate limited）一律**在此映射**，
    不新造 ChannelNotFound / ChannelAuthFailed / ChannelTimeout /
    ChannelRateLimited / ChannelTransportFailed。
    """

    def __init__(self, channel_id: int | None = None,
                 external_conversation_id: str | None = None,
                 *, retryable: bool = True) -> None:
        super().__init__(
            "Channel Delivery 失败（Fail Closed）："
            f"channel_id={channel_id}, external_conversation_id={external_conversation_id!r}"
        )
        self.channel_id = channel_id
        self.external_conversation_id = external_conversation_id
        # 12.8.2：可重试性由**抛出点**裁决——response_url（智能机器人回执地址）
        # 官方定义为「每个 URL 只能调用一次」，其失败不可重试（retryable=False）。
        # 不新增错误码：只在既有 CHANNEL_DELIVERY_FAILED 上区分可重试性。
        self.retryable = retryable


# 注意：这里**故意没有** DuplicateUserError。
#
# User 的唯一性不归 User 自己定义：Control Plane 的 User 是内部主体，
# “他是谁”由未来的 Identity（channel + external_id，DB 层
# UNIQUE(channel_id, external_id)）承载；name 只是显示名，两个真人可以同名。
# 因此 6.4 不为与 Agent(name) / Channel(type, name) 对称而预设去重错误类型，
# 也不为对称性强行给 users.name 加唯一约束（schema 维持 4.2 冻结）。
# 唯一性规则将随 Identity Slice 一起定义。
# 详见 docs/step-6.4-user-management-slice.md。
