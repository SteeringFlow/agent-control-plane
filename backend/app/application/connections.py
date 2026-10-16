"""Connection Management Application Service（Step 7.2）。

Connection = Channel ↔ Agent 的受控访问路径（Access Path，7.1 冻结）。

与 6.6 Identity 同构，是**第三个跨实体编排点**：创建时必须回答三个问题——

1. channel_id 是否存在？   → uow.channels.get_by_id()   → 不存在抛 ChannelNotFoundError
2. agent_id 是否存在？     → uow.agents.get_by_id()     → 不存在抛 AgentNotFoundError
3. (channel_id, agent_id)
    是否已存在？          → uow.connections.find_by_channel_and_agent()
                              → 已存在抛 DuplicateConnectionError
                              （DB UNIQUE(channel_id, agent_id) 仍是并发硬保证）

**只编排，不复制业务逻辑**：本 Service 绝不构造 Channel/Agent、绝不调用
uow.channels.create / uow.agents.create——与 6.6 Identity 的原则一致。

Update 只允许可变字段（name/status/config/credential_ref）：
channel_id / agent_id 是 **immutable binding**——改绑定 = 删除旧 Access Path +
建立新 Access Path，不是更新。Application（UpdateConnection 无绑定字段）与
Persistence（apply_to_orm 不覆写绑定，7.1 已验证）两层都形成防线。

本 Slice 是 Management，不是 Runtime/Adapter：
Service 不含 FastAPI / Authentication / Authorization / RBAC /
Capability / Policy / Router / Resolver / Dispatcher / Session /
Interaction / Channel Adapter / Agent Adapter——尤其不做"Agent token 能不能
连接"之类的连接性测试（那是 Runtime/Adapter 的职责）。
HTTP 契约面由 api/management/connections.py 承担（11.11，同一注入纪律）。

11.11（Connection Management API）新增裁决：
- `list_connections`：management 陈列读（全部、含 inactive），与 runtime 的
  find_active_by_channel（9.4：单 Channel + active）互不替代；
- config 更新 = **整段替换**（刻意不同于 Channel 11.8 的 merge：Connection
  响应不脱敏、所见即所存，无「脱敏回显清空密钥」问题）；
- credential_ref = **optional opaque external reference**（Cloud-0.1.2.2.1）：
  create 侧 `None` / `""` / 纯空白一律归一为 `None`（不因 credential_ref 本身失败）；
  update 侧仍要求非空白（既有 PATCH 合同不变）。它是引用、不是凭据本身——真实凭据
  由 `Connection.config` 承载（如 tencent-acp-sse 的 bearer_token）。
"""

from collections.abc import Callable
from dataclasses import dataclass, replace

from app.application.agent_identity import (
    extract_workbuddy_identity,
    find_conflicting_agents,
)
from app.application.errors import (
    AgentNotFoundError,
    ChannelNotFoundError,
    ConnectionNotFoundError,
    DuplicateConnectionError,
    DuplicateExternalAgentError,
    InvalidConnectionInputError,
)
from app.domain.connection import Connection
from app.repositories.unit_of_work import UnitOfWork


@dataclass(frozen=True)
class CreateConnection:
    """创建 Connection 的输入契约（应用层校验点，非 Pydantic/API schema）。

    绑定骨架 = (channel_id, agent_id)（创建后不可变）。
    """

    channel_id: int
    agent_id: int
    name: str
    config: dict | None = None
    credential_ref: str | None = None


@dataclass(frozen=True)
class UpdateConnection:
    """更新 Connection 的输入契约（只改可变字段）。

    None 字段表示「不修改」。
    没有 channel_id / agent_id —— 绑定不可变（改绑定 = 删旧 + 建新）。
    """

    name: str | None = None
    status: str | None = None
    config: dict | None = None
    credential_ref: str | None = None


def _require_positive_id(value: int, field: str) -> None:
    if not isinstance(value, int) or value <= 0:
        raise InvalidConnectionInputError(f"{field} 必须是正整数")


def _require_non_blank(value: str | None, field: str) -> None:
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise InvalidConnectionInputError(f"{field} 不能为空")


def _normalize_credential_ref(value: str | None) -> str | None:
    """credential_ref = **optional opaque external reference**（Cloud-0.1.2.2.1）。

    - `None` / `""` / 纯空白 → `None`（= 无外部引用），**不因 credential_ref 本身失败**；
    - 非空字符串 → 原样保留（opaque：不解析、不校验格式）。

    这不取消凭据校验：真正的连接所需信息来自 `Connection.config`（如 WorkBuddy 的
    runtime_id / bearer_token / endpoint），由 AgentAdapter 在 dispatch 时 Fail-Closed
    校验。credential_ref **不是**凭据本身，只是一个可选的外部引用。
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidConnectionInputError("credential_ref 必须是字符串或 null")
    return value if value.strip() else None


class ConnectionApplicationService:
    """Connection 管理用例的编排者（依赖 UoW Port，而非具体 SQLAlchemy 实现）。"""

    def __init__(self, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    # ------------------------------------------------------------ Create

    def create_connection(self, input: CreateConnection) -> Connection:
        """创建 Connection：Channel/Agent 存在性校验 + 绑定预检 → 写入 → commit。

        只读校验端点（get_by_id），不复制任何 Channel/Agent 创建逻辑。
        credential_ref 为 optional opaque reference（None/""/纯空白 → None，不因之失败）；
        真实凭据由 config 承载。
        """
        _require_positive_id(input.channel_id, "channel_id")
        _require_positive_id(input.agent_id, "agent_id")
        _require_non_blank(input.name, "name")

        connection = Connection(
            channel_id=input.channel_id,
            agent_id=input.agent_id,
            name=input.name,
            config=input.config,
            credential_ref=_normalize_credential_ref(input.credential_ref),
        )
        with self._uow_factory() as uow:
            if uow.channels.get_by_id(input.channel_id) is None:
                raise ChannelNotFoundError(input.channel_id)
            if uow.agents.get_by_id(input.agent_id) is None:
                raise AgentNotFoundError(input.agent_id)
            if uow.connections.find_by_channel_and_agent(
                input.channel_id, input.agent_id
            ) is not None:
                raise DuplicateConnectionError(input.channel_id, input.agent_id)
            # M7-AUI-10.5：Home Flow 的 WorkBuddy 身份随 Connection.config 落库
            # （POST /api/agents 只带 name/type）——绑定指向的 Agent **不是**该
            # 外部身份的代表时拒绝（排除自身绑定 → 同一 Agent 多 Channel 合法）。
            identity = extract_workbuddy_identity(input.config)
            if identity is not None:
                conflicts = find_conflicting_agents(
                    uow, identity, exclude_agent_id=input.agent_id)
                if conflicts:
                    raise DuplicateExternalAgentError(
                        identity, [(a.id, a.name) for a in conflicts])
            created = uow.connections.create(connection)
            uow.commit()
            return created

    # ------------------------------------------------------------ Get

    def get_connection(self, connection_id: int) -> Connection | None:
        """按 id 读取 Connection；不存在返回 None。"""
        with self._uow_factory() as uow:
            return uow.connections.get_by_id(connection_id)

    # ------------------------------------------------------------ List

    def list_connections(self) -> list[Connection]:
        """11.11：List 用例——返回全部 Connection（含 inactive，管理面陈列用）。

        MVP 无分页/过滤（与 11.3 list_agents / 11.8 list_channels 同一裁决）；
        与 runtime 读路径 find_active_by_channel（9.4）互不替代。
        """
        with self._uow_factory() as uow:
            return uow.connections.list_connections()

    # ------------------------------------------------------------ Update

    def update_connection(
        self, connection_id: int, change: UpdateConnection
    ) -> Connection:
        """部分更新 Connection 可变字段（name/status/config/credential_ref）。

        目标不存在抛 ConnectionNotFoundError；非法输入抛 InvalidConnectionInputError。
        绑定（channel_id/agent_id）不可改。
        config：提供 = 整段替换（11.11 冻结，刻意不同于 Channel 的 merge）。
        """
        _require_non_blank(change.name, "name")
        _require_non_blank(change.status, "status")
        _require_non_blank(change.credential_ref, "credential_ref")  # 11.11：空串不是合法引用

        with self._uow_factory() as uow:
            current = uow.connections.get_by_id(connection_id)
            if current is None:
                raise ConnectionNotFoundError(connection_id)
            updated = self._apply_change(current, change)
            # M7-AUI-10.5.1：PATCH 的 config 是**整段替换**（11.11 冻结）——替换后的
            # **有效身份**若已被同租户**其它** Agent 代表 → 拒绝（409）。
            # 排除自身绑定：同一 Agent 多 Channel 复用（既有 Connection 唯一性规则）
            # 不受影响；跨租户由 UoW 作用域隔离。拒绝发生在写入之前——不产生任何
            # 持久化副作用。verify 的 runtime 缓存写回（身份属于连接自己的 Agent /
            # 或无可比身份）经排除自身/None 短路，行为不变。
            identity = extract_workbuddy_identity(updated.config)
            if identity is not None:
                conflicts = find_conflicting_agents(
                    uow, identity, exclude_agent_id=current.agent_id)
                if conflicts:
                    raise DuplicateExternalAgentError(
                        identity, [(a.id, a.name) for a in conflicts])
            result = uow.connections.update(updated)
            uow.commit()
            return result

    @staticmethod
    def _apply_change(current: Connection, change: UpdateConnection) -> Connection:
        """11.11 冻结：config 更新 = 整段替换（不是 Channel 的 load→merge→persist）。

        Connection.config 是 Access Path 参数且响应不脱敏——管理员所见即所存，
        整段替换语义清晰且无「脱敏回显清空密钥」风险（那是 Channel 11.8 merge
        的存在理由，Connection 没有该问题，故不机械复制）。
        """
        return replace(
            current,
            name=change.name if change.name is not None else current.name,
            status=change.status if change.status is not None else current.status,
            config=change.config if change.config is not None else current.config,
            credential_ref=(
                change.credential_ref
                if change.credential_ref is not None
                else current.credential_ref
            ),
        )
