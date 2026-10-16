"""Agent Management Application Service（Step 6.1 起步，Step 6.2 完善成 Slice）。

用例：Create / Get / Update + 输入契约校验 + Application-level error mapping。

职责：组合 domain + UoW Port 完成用例；事务边界归 UoW（commit/rollback）。

边界（AST 守护）：
- 不 import SQLAlchemy（Session / Engine / sessionmaker）
- 不 import app.persistence（models / mappers / repositories / database / unit_of_work）
- 不 import FastAPI / api / schemas / adapters
- 只依赖 app.domain、app.repositories（端口）、app.application（自身/错误类型）
"""

from collections.abc import Callable
from dataclasses import dataclass, replace

from app.application.agent_identity import (
    extract_workbuddy_identity,
    find_conflicting_agents,
)
from app.application.errors import (
    AgentNotFoundError,
    DuplicateAgentError,
    DuplicateExternalAgentError,
    InvalidAgentInputError,
)
from app.domain.agent import Agent
from app.repositories.unit_of_work import UnitOfWork

# ---------------------------------------------------------------- 政策（11.2 冻结）

# type 受控集合：对齐 app/adapters/agents/ 注册表（15.2.3 起含 local）。
# "acp" → ACPAdapter（Tencent ACP，外部系统）；"local" → LocalAgentAdapter
# （external-system-free，OSS 开发者 First Success 路径）。
ALLOWED_AGENT_TYPES: tuple[str, ...] = ("acp", "local")

# status 管理态受控集合（11.2 §1.2）：API/服务输入只接受这两个值。
# "error" 是运行态专有值——仅 Runtime dispatch 失败路径写入，输入侧一律拒绝。
ALLOWED_MANAGEMENT_STATUSES: tuple[str, ...] = ("active", "inactive")


@dataclass(frozen=True)
class CreateAgent:
    """创建 Agent 的输入契约（Application 层校验点，非 Pydantic/API schema）。

    11.3：补 status（可选，管理态 ∈ {active, inactive}，缺省由领域默认 active）。
    """

    name: str
    type: str
    endpoint: str | None = None
    status: str | None = None
    config: dict | None = None
    credential_ref: str | None = None


@dataclass(frozen=True)
class UpdateAgent:
    """更新 Agent 的输入契约。

    None 字段表示「不修改」（本 Slice 不支持把字段显式清空为 None，
    清空语义等真实管理需求出现后再定义）。
    """

    name: str | None = None
    type: str | None = None
    status: str | None = None
    endpoint: str | None = None
    config: dict | None = None
    credential_ref: str | None = None


def _require_non_blank(value: str | None, field: str) -> None:
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise InvalidAgentInputError(f"{field} 不能为空")


def _require_allowed_type(value: str | None) -> None:
    """type 受控集合（11.2 §3）：越界值一律拒绝（防类型漂移破坏 Routing 匹配）。"""
    if value is not None and value not in ALLOWED_AGENT_TYPES:
        raise InvalidAgentInputError(
            f"type 必须是 {list(ALLOWED_AGENT_TYPES)} 之一，得到：{value!r}")


def _require_management_status(value: str | None) -> None:
    """status 管理态受控集合（11.2 §1.2）：拒绝 error 及一切非管理态值。"""
    if value is not None and value not in ALLOWED_MANAGEMENT_STATUSES:
        raise InvalidAgentInputError(
            f"status 必须是管理态 {list(ALLOWED_MANAGEMENT_STATUSES)} 之一，得到：{value!r}")


def _require_valid_endpoint_url(value: str | None) -> None:
    """endpoint 最小 URL 校验（11.2 §4）：scheme ∈ {http, https} + 非空 host。

    刻意不用 urllib.parse：Application 层的既有边界守护（6.5/6.7/7.3/10.1.4）
    把 urllib.* 视为协议/传输泄漏；最小校验用纯字符串解析即可表达。
    不探活、不过度；endpoint 可为 None（领域字段可空），提供则必须合法。
    """
    if value is None:
        return
    scheme, sep, rest = value.strip().partition("://")
    if (not sep or scheme.lower() not in ("http", "https")) or not rest:
        raise InvalidAgentInputError(
            f"endpoint 必须是合法的 http/https URL（含 host），得到：{value!r}")
    host = rest.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if "@" in host:
        host = host.rsplit("@", 1)[-1]
    host = host.split(":", 1)[0]
    if not host:
        raise InvalidAgentInputError(
            f"endpoint 必须是合法的 http/https URL（含 host），得到：{value!r}")


def _require_dict_config(value: dict | None) -> None:
    if value is not None and not isinstance(value, dict):
        raise InvalidAgentInputError("config 必须是 JSON 对象（dict）")


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
        raise InvalidAgentInputError("credential_ref 必须是字符串或 null")
    return value if value.strip() else None


def _normalize_endpoint(value: str | None) -> str | None:
    """endpoint = Agent 的**可选描述性元数据**（Cloud-0.1.2.3.1.1）。

    - `None` / `""` / 纯空白 → `None`（= 无元数据），**不因 endpoint 本身失败**；
    - 非空字符串 → 原样保留（是否合法 URL 由 `_require_valid_endpoint_url` 判定）。

    **只处理 create 侧**：镜像 `_normalize_credential_ref` 的归一化纪律。PATCH
    （`update_agent`）保持既有冻结合同不变——`""` 在 PATCH 下是「是否清除已有值」的
    独立合同问题，不在本步。endpoint 是描述性元数据，**不是**实际访问地址：访问
    endpoint 由 `Connection.config["endpoint"]` 承载（Cloud-0.1.2.3.1 裁决）。
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidAgentInputError("endpoint 必须是字符串或 null")
    return value if value.strip() else None


class AgentApplicationService:
    """Agent 管理用例的编排者（依赖 UoW Port，而非具体 SQLAlchemy 实现）。"""

    def __init__(self, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    # ------------------------------------------------------------ Create

    def create_agent(self, input: CreateAgent) -> Agent:
        """创建 Agent。

        校验（输入契约）→ 应用级去重守卫 → 写入 → commit。
        重复/非法输入分别抛出 DuplicateAgentError / InvalidAgentInputError；
        M7-AUI-10.5：config 携带 WorkBuddy 外部身份且同租户已有其它 Agent
        代表该身份 → DuplicateExternalAgentError（409，不创建）。

        endpoint 是**可选描述性元数据**（0.1.2.3.1.1）：`None` / `""` / 纯空白
        一律归一为 `None`（不因 endpoint 本身失败）；提供则须为合法 http/https URL。
        credential_ref 同为可选引用（0.1.2.2.1）。
        """
        _require_non_blank(input.name, "name")
        _require_non_blank(input.type, "type")
        _require_allowed_type(input.type)
        _require_management_status(input.status)
        endpoint = _normalize_endpoint(input.endpoint)
        _require_valid_endpoint_url(endpoint)
        _require_dict_config(input.config)

        agent = Agent(
            name=input.name,
            type=input.type,
            endpoint=endpoint,
            status=input.status if input.status is not None else "active",
            config=input.config,
            credential_ref=_normalize_credential_ref(input.credential_ref),
        )
        with self._uow_factory() as uow:
            if uow.agents.find_by_name(input.name) is not None:
                raise DuplicateAgentError(input.name)
            # M7-AUI-10.5：WorkBuddy 外部身份租户内唯一（应用级守卫；
            # 身份事实源 = Agent.config ∪ 该租户 Connections.config，见 agent_identity）。
            identity = extract_workbuddy_identity(input.config)
            if identity is not None:
                conflicts = find_conflicting_agents(uow, identity)
                if conflicts:
                    raise DuplicateExternalAgentError(
                        identity, [(a.id, a.name) for a in conflicts])
            created = uow.agents.create(agent)
            uow.commit()
            return created

    # ------------------------------------------------------------ Get

    def get_agent(self, agent_id: int) -> Agent | None:
        """按 id 读取 Agent；不存在返回 None。"""
        with self._uow_factory() as uow:
            return uow.agents.get_by_id(agent_id)

    # ------------------------------------------------------------ List

    def list_agents(self) -> list[Agent]:
        """11.3：List 用例——返回全部 Agent（管理面陈列/选择用）。

        MVP 无分页/过滤（11.2 §5.4）；读取不裁切 status（error 是要暴露
        给管理员的运行态信号，11.2 §1.2）。
        """
        with self._uow_factory() as uow:
            return uow.agents.list_agents()

    # ------------------------------------------------------------ Update

    def update_agent(self, agent_id: int, change: UpdateAgent) -> Agent:
        """部分更新 Agent（None 字段不改）。

        目标不存在抛 AgentNotFoundError；非法输入抛 InvalidAgentInputError。
        """
        _require_non_blank(change.name, "name")
        _require_non_blank(change.type, "type")
        _require_non_blank(change.status, "status")
        _require_non_blank(change.endpoint, "endpoint")
        _require_allowed_type(change.type)
        _require_management_status(change.status)
        _require_valid_endpoint_url(change.endpoint)
        _require_dict_config(change.config)
        _require_non_blank(change.credential_ref, "credential_ref")

        with self._uow_factory() as uow:
            current = uow.agents.get_by_id(agent_id)
            if current is None:
                raise AgentNotFoundError(agent_id)
            # 11.2 §5.3：PATCH 改名撞已有 name → 409（与 create 同一应用级守卫，
            # 同样存在 TOCTOU 窗口；DB UNIQUE 为后续硬兜底）。
            if (change.name is not None and change.name != current.name
                    and uow.agents.find_by_name(change.name) is not None):
                raise DuplicateAgentError(change.name)
            updated = self._apply_change(current, change)
            # M7-AUI-10.5：update 引入/改写 WorkBuddy 外部身份 → 与**其它** Agent
            # 查重（排除自身：自己名下 Connection.config 携带同一身份是合法现状）。
            identity = extract_workbuddy_identity(updated.config)
            if identity is not None:
                conflicts = find_conflicting_agents(
                    uow, identity, exclude_agent_id=current.id)
                if conflicts:
                    raise DuplicateExternalAgentError(
                        identity, [(a.id, a.name) for a in conflicts])
            result = uow.agents.update(updated)
            uow.commit()
            return result

    @staticmethod
    def _apply_change(current: Agent, change: UpdateAgent) -> Agent:
        """M7-AUI-10.2：config 更新 = load → merge incoming → persist。

        提供的键覆写、未提供的键保留（尤其 api_key 等 credential-like
        material）——管理响应是脱敏回显，整段替换会在管理员只改非密字段
        时把密键整段清空（镜像 Channel 11.8 冻结合同 #3）。写路径哨兵
        （schemas/agents.py）保证 "********" 占位符进不了 merge。
        """
        merged_config = (
            {**(current.config or {}), **change.config}
            if change.config is not None
            else current.config
        )
        return replace(
            current,
            name=change.name if change.name is not None else current.name,
            type=change.type if change.type is not None else current.type,
            status=change.status if change.status is not None else current.status,
            endpoint=change.endpoint if change.endpoint is not None else current.endpoint,
            config=merged_config,
            credential_ref=(
                change.credential_ref
                if change.credential_ref is not None
                else current.credential_ref
            ),
        )
