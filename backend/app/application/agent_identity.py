"""WorkBuddy External Agent Identity（M7-AUI-10.5）。

审计基线：`M7-AUI-10.5 External Agent Identity Uniqueness Audit Report`。

**身份契约（本文件是它的唯一应用层裁决点）**：

    External Agent Identity = (tenant_id, provider=workbuddy, workbuddy_agent_id)

- 唯一性作用域 = 一个 Tenant；跨 Tenant 引用同一外部 Agent 合法（互不可见）。
- **API Key 是凭据，不是身份**：绝不参与身份相等判断、绝不进入任何错误信息/日志。
- 身份的**事实源**有两处（审计 §6：实测 100% 住在 Connection.config）：
    1. `Agent.config`（M7-AUI-10.3 的 fallback 通道）；
    2. `Connection.config`（Home Flow 的主存放位置）。
  冲突查找必须扫**两处并集**；同一 Agent 在两处值不一致时不做任何改写/调和
  （不扩成本任务的 canonical identity 存储重设计）——任何一处命中即视为该
  Agent 代表了该身份。
- 无 provider、provider 非 workbuddy、或 workbuddy_agent_id 为空/非字符串
  → **没有可比较的身份**（generic ACP endpoint / bearer token 绝不被解释成
  WorkBuddy 身份）；verify 侧既有 fail-closed 校验不受影响。

**键名常量**：镜像 `app/adapters/agents/workbuddy.py` 的 PROVIDER_KEY /
AGENT_ID_KEY / WORKBUDDY_PROVIDER。Application 层 AST 边界（6.x）禁止 import
adapters，因此这里独立声明同一字符串常量——两侧键名若变更必须同步（有测试锁定）。

**租户隔离保证**：冲突查找完全运行在调用方给定的 UoW 内——UoW 由组合根按请求
TenantScope 绑定 tenant_id（Cloud-0.1.1-B），仓储层显式过滤，跨租户数据
结构性不可见。本模块不自己开库、不做任何跨 UoW 读取。
"""

from __future__ import annotations

from app.domain.agent import Agent

#: Provider 标志键 / WorkBuddy Agent ID 键（镜像 adapters/agents/workbuddy.py）。
PROVIDER_KEY = "provider"
AGENT_ID_KEY = "workbuddy_agent_id"
WORKBUDDY_PROVIDER = "workbuddy"


def extract_workbuddy_identity(config: object) -> str | None:
    """从 config 提取 WorkBuddy 外部身份（strip 后的 Agent ID）；无可比身份返回 None。

    判定条件（三者同时成立）：config 是 dict、`provider == "workbuddy"`、
    `workbuddy_agent_id` 是非空白字符串。API Key 不参与、不被读取。
    """
    if not isinstance(config, dict):
        return None
    if config.get(PROVIDER_KEY) != WORKBUDDY_PROVIDER:
        return None
    value = config.get(AGENT_ID_KEY)
    if not isinstance(value, str):
        return None
    return value.strip() or None


def find_conflicting_agents(
    uow, external_agent_id: str, *, exclude_agent_id: int | None = None
) -> list[Agent]:
    """同租户内已代表该外部身份的**其它** Agent（按 id 升序）。

    扫描两处事实源的并集：
    - `Agent.config` 直接命中；
    - `Connection.config` 命中 → 归属到该 Connection 绑定的 Agent。

    `exclude_agent_id`：update 场景排除当前 Agent 自身（自己代表自己是合法现状，
    包括「身份只出现在自己名下 Connection.config」的情形）。
    历史不一致（同一 Agent 两处身份不同）：不做调和，任一处命中即计入。
    """
    conflicts: dict[int, Agent] = {}
    for agent in uow.agents.list_agents():
        if exclude_agent_id is not None and agent.id == exclude_agent_id:
            continue
        if extract_workbuddy_identity(agent.config) == external_agent_id:
            conflicts[agent.id] = agent
    for connection in uow.connections.list_connections():
        if extract_workbuddy_identity(connection.config) != external_agent_id:
            continue
        owner = connection.agent_id
        if exclude_agent_id is not None and owner == exclude_agent_id:
            continue
        if owner not in conflicts:
            agent = uow.agents.get_by_id(owner)
            if agent is not None:
                conflicts[owner] = agent
    return [conflicts[key] for key in sorted(conflicts)]
