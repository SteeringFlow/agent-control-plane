"""Connection Verify 的 provider 分派（M6.6-F3）。

位置：`app/adapters/connection_verify.py`（**Adapter 层**，非 Domain / 非 API）。

一句话定位：`POST /api/connections/{id}/verify` 注入的**薄分派器**——按 Connection
的 provider 身份选择「谁来验」。**不是** Provider framework：刻意不做
ProviderRegistry / CapabilityRegistry / Plugin / Dynamic discovery。

分派（M6.6-F3 冻结 + M7-AUI-10.3 Agent credential fallback）：

    Connection.config["provider"] == "workbuddy"
        credential 完整                              → WorkBuddyProvisioner.verify
        credential 不完整 + Agent credential 可补全   → effective config → 同上
        credential 不完整且无法补全                    → WorkBuddyProvisioner.verify
                                                        （既有 fail-closed 路径）
    Channel.type == "wecom"                        → WeComAdapter.verify_connection
        （Channel 侧 Provider：凭据住 Channel.config，需 tenant-scoped 解析 Channel）
    其它（未知 / 无 provider-specific verifier）
        Agent.config 有完整 WorkBuddy credential      → effective config → 同上
        否则                                          → ACPAdapter.probe
                                                        （既有只读探针，F3 行为不变）

Credential precedence（M7-AUI-10.3 §六 + M7-AUI-10.5.2 扩展）：
Connection-owned credential 优先，仅在其缺失 / 不完整时依次 fallback：
  1. 本 Connection.config（既有，优先级最高）；
  2. 同 tenant 的 `Agent.config`（M7-AUI-10.3，经 tenant-scoped agent_loader）；
  3. 同 tenant **同一 Agent** 的兄弟 Connection.config（M7-AUI-10.5.2，经
     tenant-scoped sibling_loader）——**只取 credential 三键**
     （provider / workbuddy_agent_id / api_key），绝不复用兄弟连接的
     runtime cache（runtime_id / endpoint / bearer_token 等连接专属状态）；
     多个候选按连接 id 升序取第一个（确定性）；候选来源的 workbuddy_agent_id
     不一致 → **Fail Closed**（安全诊断，绝不静默二选一，也绝不掉进 generic
     ACP 探针）。
发现到的 credential 只在 verify/provisioning 内存路径中使用，**绝不写回**
Connection.config（runtime cache 写回前剥离 credential 键；兄弟凭据与
Agent credential 共用同一剥离纪律）。

Provider 身份来源（M6.6-F3 方案 A 决策）：
- Agent 侧 provider（WorkBuddy）住 `Connection.config["provider"]`（既有事实，保持）；
- Channel 侧集成身份（WeCom）住 **Channel 自身的 `type` 字段**——**不**在 Connection
  上复制 `provider:"wecom"`（WeCom 的规范事实源是 Channel；见 Cloud-0.1.4 Gate G2：
  WeCom 平台凭据只住 Channel.config，Connection.config 只住 ACP access path）。
  因此分派前必须经注入的 `channel_loader` 解析 Channel。

契约：`verify(connection) -> (AgentProbeSnapshot, updated_config | None)`——与既有
注入式 verify **完全同形**，故 HTTP 契约 / 响应模型 / 前端契约一律不变。

tenant 隔离（硬约束）：`channel_loader` 必须是 **tenant-scoped**——组合根经请求解析
出的 `TenantScope` 绑定 UoW 工厂；跨租户解析不得返回他租户 Channel（返回 None → 回退）。
本模块**不**自己开库、**不**引入新的 Channel persistence 抽象。

只读：本模块除把 WorkBuddy 返回的 `updated_config` 原样透传外，自身**绝不写库**；
WeCom 与回退路径一律 `updated_config=None`。

失败语义：WorkBuddy / ACP 探针的失败语义**原样透传**（不吞不改）；WeCom 由
`WeComAdapter.verify_connection` 自行 Fail-Closed。Channel 解析整体失败（读库异常 /
非正 id / None）→ 回退 `ACPAdapter.probe`（F3-8：没有 provider-specific verifier 的
Connection 保持原有行为），Verify 端点绝不因此 500。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from app.adapters.agents.workbuddy import (
    AGENT_ID_KEY,
    API_KEY_KEY,
    PROVIDER_KEY,
    WORKBUDDY_PROVIDER,
)
from app.domain.agent import Agent
from app.domain.agent_probe_snapshot import AgentProbeSnapshot
from app.domain.channel import Channel
from app.domain.connection import Connection

#: WeCom 集成的**规范身份** == Channel.type（M6.6-F3 方案 A；非凭据推断）。
WECOM_CHANNEL_TYPE = "wecom"

#: 归一化 verify 签名（与 api/management/connections.py 注入点一致）。
ConnectionVerifier = Callable[[Connection], "tuple[AgentProbeSnapshot, dict | None]"]

#: Agent 侧 WorkBuddy credential 的三个键（M7-AUI-10.3）。
#: 它们允许在 verify/provisioning 的**内存路径**中合成 effective config，
#: 但绝不允许随 runtime cache 写回 Connection.config（credential ownership
#: 仍然属于 Agent）。
_AGENT_CREDENTIAL_KEYS = frozenset({PROVIDER_KEY, AGENT_ID_KEY, API_KEY_KEY})


def build_connection_verifier(
    *,
    probe: Callable[[Connection], AgentProbeSnapshot],
    workbuddy_verify: Callable[[Connection], tuple[AgentProbeSnapshot, dict | None]],
    wecom_verify: Callable[[Channel], AgentProbeSnapshot],
    channel_loader: Callable[[int], Channel | None],
    agent_loader: Callable[[int], Agent | None] | None = None,
    sibling_loader: Callable[[int], list[Connection]] | None = None,
) -> ConnectionVerifier:
    """构造 `POST /api/connections/{id}/verify` 的 provider 分派器（薄 wiring）。

    能力全部由组合根注入（Adapter 层的既有实现），本函数只做**选择**：
    不实现任何协议、不写库、不做 provider 发现。

    M7-AUI-10.3：`agent_loader`（**必须 tenant-scoped**，与 channel_loader 同一
    纪律）启用 Agent credential fallback——Connection.config 没有完整 WorkBuddy
    credential 时，从同 tenant 的 `Agent.config`（provider/workbuddy_agent_id/
    api_key）在内存中合成 effective config，交给既有 WorkBuddyProvisioner；
    Agent credential 绝不写回 Connection.config（写回前剥离 credential 键，
    只留 per-connection runtime cache）。`agent_loader=None` = 关闭 fallback
    （旧行为逐字节不变）。

    M7-AUI-10.5.2：`sibling_loader`（**必须 tenant-scoped**，同一纪律）启用
    兄弟连接凭据发现——Agent.config 也补不全时，从**同一 Agent** 的其它
    Connection.config 只取 credential 三键在内存合成 effective config；
    `sibling_loader=None` = 关闭发现（10.3 行为逐字节不变）。
    """

    def verify_connection(
        connection: Connection,
    ) -> tuple[AgentProbeSnapshot, dict | None]:
        if not isinstance(connection, Connection):
            return AgentProbeSnapshot(False, "invalid connection", None), None
        config = connection.config if isinstance(connection.config, dict) else {}
        if config.get(PROVIDER_KEY) == WORKBUDDY_PROVIDER:
            if _credential_complete(config):
                # Connection-owned credential 完整 → 优先（M7-AUI-10.3 §六）。
                # 兄弟/Agent fallback 均不征询（不必要的不征询）。
                return workbuddy_verify(connection)
            # Connection credential 不完整 → Agent fallback → 兄弟连接发现；
            # 全部落空 → 仍走 WorkBuddy verify 的既有 fail-closed 路径。
            agent = _load_agent_obj(agent_loader, connection.agent_id)
            credential = _agent_credential(agent) if agent is not None else None
            hints = _config_agent_id_hint(config) | _agent_agent_id_hint(agent)
            if credential is not None:
                return _verify_with_agent_credential(
                    workbuddy_verify, connection, {**credential, **config})
            credential, conflict = _sibling_credential(
                sibling_loader, connection, config, extra_hints=hints)
            if conflict is not None:
                return AgentProbeSnapshot(False, conflict, None), None
            if credential is not None:
                return _verify_with_agent_credential(
                    workbuddy_verify, connection, {**credential, **config})
            return workbuddy_verify(connection)

        channel = _load_channel(channel_loader, connection.channel_id)
        if _channel_type(channel) == WECOM_CHANNEL_TYPE:
            # Channel 侧 Provider（WeCom）：只读凭据校验，无写入。
            # （渠道侧集成身份优先于 Agent 侧 fallback——M6.6-F3 方案 A 对称裁决。）
            return wecom_verify(channel), None

        # 无 provider-specific verifier：Agent fallback → 兄弟连接发现；
        # 全部落空 → 既有只读探针（F3-8，行为不变）。
        agent = _load_agent_obj(agent_loader, connection.agent_id)
        credential = _agent_credential(agent) if agent is not None else None
        hints = _config_agent_id_hint(config) | _agent_agent_id_hint(agent)
        if credential is not None:
            return _verify_with_agent_credential(
                workbuddy_verify, connection, {**credential, **config})
        credential, conflict = _sibling_credential(
            sibling_loader, connection, config, extra_hints=hints)
        if conflict is not None:
            # 来源不一致 = 该连接**意图使用 WorkBuddy** 却拿不到一致凭据 →
            # Fail Closed（绝不静默掉进 generic ACP 探针）。
            return AgentProbeSnapshot(False, conflict, None), None
        if credential is not None:
            return _verify_with_agent_credential(
                workbuddy_verify, connection, {**credential, **config})
        return probe(connection), None

    return verify_connection


def _credential_complete(config: dict) -> bool:
    """WorkBuddy credential 三键齐全（provider 已由调用方判定）。"""
    agent_id = config.get(AGENT_ID_KEY)
    api_key = config.get(API_KEY_KEY)
    return (isinstance(agent_id, str) and bool(agent_id.strip())
            and isinstance(api_key, str) and bool(api_key.strip()))


def _agent_credential(agent: object) -> dict | None:
    """从 Agent.config 读取完整 WorkBuddy credential；不完整 / 非 workbuddy → None。"""
    config = getattr(agent, "config", None)
    if not isinstance(config, dict):
        return None
    if config.get(PROVIDER_KEY) != WORKBUDDY_PROVIDER:
        return None
    if not _credential_complete(config):
        return None
    return {
        PROVIDER_KEY: WORKBUDDY_PROVIDER,
        AGENT_ID_KEY: str(config[AGENT_ID_KEY]).strip(),
        API_KEY_KEY: str(config[API_KEY_KEY]).strip(),
    }


def _effective_with_agent(
    agent_loader: Callable[[int], Agent | None] | None,
    connection: Connection,
    config: dict,
) -> dict | None:
    """Connection.config 缺口由同 tenant Agent.config 补全（仅内存，不落库）。

    Connection 既有键优先（含 runtime cache），Agent 只补 credential 缺口；
    loader 任何失败（不存在 / 异常 / 跨租户不可见）→ None（fail closed）。
    """
    agent = _load_agent_obj(agent_loader, connection.agent_id)
    credential = _agent_credential(agent) if agent is not None else None
    if credential is None:
        return None
    return {**credential, **config}


def _load_agent_obj(
    agent_loader: Callable[[int], Agent | None] | None, agent_id: object,
) -> Agent | None:
    """经 tenant-scoped loader 读取 Agent；任何不可读取 → None（fail closed）。"""
    if agent_loader is None or not isinstance(agent_id, int) or agent_id <= 0:
        return None
    try:
        return agent_loader(agent_id)
    except Exception:  # noqa: BLE001 解析失败 → fail closed（不炸 Verify）
        return None


def _config_agent_id_hint(config: dict) -> set[str]:
    """目标连接自身携带的 workbuddy_agent_id 提示（用于来源一致性裁决）。"""
    value = config.get(AGENT_ID_KEY)
    if isinstance(value, str) and value.strip():
        return {value.strip()}
    return set()


def _agent_agent_id_hint(agent: object) -> set[str]:
    """Agent.config 携带的 workbuddy_agent_id 提示（即使 credential 不完整）。"""
    config = getattr(agent, "config", None)
    if not isinstance(config, dict):
        return set()
    if config.get(PROVIDER_KEY) != WORKBUDDY_PROVIDER:
        return set()
    value = config.get(AGENT_ID_KEY)
    if isinstance(value, str) and value.strip():
        return {value.strip()}
    return set()


def _sibling_credential(
    sibling_loader: Callable[[int], list[Connection]] | None,
    connection: Connection,
    config: dict,
    *,
    extra_hints: set[str] | None = None,
) -> tuple[dict | None, str | None]:
    """M7-AUI-10.5.2：从**同 tenant 同 Agent** 的兄弟连接发现完整 credential。

    - 只取 credential 三键（provider / workbuddy_agent_id / api_key），
      **绝不**复用兄弟连接的 runtime cache（runtime_id / endpoint /
      bearer_token 等连接专属状态归其自身连接所有）；
    - 候选按连接 id 升序取第一个（确定性选择）；
    - 候选来源（含目标连接自身与传入的 Agent.config 提示）的
      workbuddy_agent_id 不一致 → 返回 fail-closed 诊断（安全：不含 API Key）；
    - loader 任何失败 / 未注入 / 无候选 → (None, None)（回退既有路径）。
    """
    if sibling_loader is None or not isinstance(connection.agent_id, int) \
            or connection.agent_id <= 0:
        return None, None
    try:
        siblings = sibling_loader(connection.agent_id)
    except Exception:  # noqa: BLE001 解析失败 → fail closed（不炸 Verify）
        return None, None
    if not isinstance(siblings, list):
        return None, None
    hints: set[str] = set(extra_hints or ())
    candidates: list[tuple[int, dict]] = []
    for row in siblings:
        if not isinstance(row, Connection):
            continue
        if row.id == connection.id or row.agent_id != connection.agent_id:
            continue  # 排除自身；绝不借用其它 Agent 的凭据
        row_config = row.config if isinstance(row.config, dict) else {}
        if row_config.get(PROVIDER_KEY) != WORKBUDDY_PROVIDER:
            continue
        ext_id = row_config.get(AGENT_ID_KEY)
        if not (isinstance(ext_id, str) and ext_id.strip()):
            continue
        if not _credential_complete(row_config):
            continue  # 不完整来源不构成候选，也不产生一致性提示
        hints.add(ext_id.strip())
        candidates.append((row.id, {
            PROVIDER_KEY: WORKBUDDY_PROVIDER,
            AGENT_ID_KEY: ext_id.strip(),
            API_KEY_KEY: str(row_config[API_KEY_KEY]).strip(),
        }))
    if len(hints) > 1:
        return None, (
            "WorkBuddy 凭据来源不一致（Fail Closed）：这条 Agent 名下的多个连接"
            "使用了不同的 workbuddy_agent_id（" + "、".join(sorted(hints)) + "）。"
            "请检查各连接的 WorkBuddy Agent ID 后重试。")
    if not candidates:
        return None, None
    _, credential = min(candidates, key=lambda pair: pair[0])
    return credential, None


def _verify_with_agent_credential(
    workbuddy_verify: Callable[[Connection], tuple[AgentProbeSnapshot, dict | None]],
    connection: Connection,
    effective: dict,
) -> tuple[AgentProbeSnapshot, dict | None]:
    """用 effective config 调既有 WorkBuddyProvisioner，并剥离 credential 键。

    Provisioner 写回的 updated_config = {**input_config, **runtime cache}——
    其中的 Agent credential 三键在此处剥离，保证落回 Connection.config 的
    只有 per-connection runtime state（protocol/endpoint/bearer_token/
    runtime_id 等）；Agent credential 的 ownership 仍属于 Agent。
    """
    snapshot, updated = workbuddy_verify(replace(connection, config=effective))
    if updated is not None:
        updated = {
            key: value for key, value in updated.items()
            if key not in _AGENT_CREDENTIAL_KEYS
        }
    return snapshot, updated


def _channel_type(channel: object) -> str | None:
    value = getattr(channel, "type", None)
    return value if isinstance(value, str) else None


def _load_channel(
    channel_loader: Callable[[int], Channel | None], channel_id: object,
) -> Channel | None:
    """经 tenant-scoped loader 读取 Channel；任何不可读取 → None（回退探针）。"""
    if not isinstance(channel_id, int) or channel_id <= 0:
        return None
    try:
        return channel_loader(channel_id)
    except Exception:  # noqa: BLE001 解析失败 → 回退（Fail Closed，不炸 Verify）
        return None
