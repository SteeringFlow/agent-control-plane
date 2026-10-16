"""WorkBuddy（腾讯 CodeBuddy Enterprise ACP Runtime）Provider-native 管理面（M6.6-E3-B）。

位置：`app/adapters/agents/workbuddy.py`（**Adapter 层**，非 Domain / 非 ACP Core）。

定位（E3-A 可行性审计裁决）：
- WorkBuddy 仍是 `Agent(type="acp")`——**不新增** `WorkBuddyAgent` / `WorkBuddyRuntime`
  / `WorkBuddySession` 等 Domain 实体（架构守卫
  `test_cloud_0_1_2_2_connect_agent_architecture_gate.py` 亦硬锁）；
- 本模块只做 **Provider-specific provisioning**：用 WorkBuddy Management API
  （`POST /v2/agentos/runtimes`）把 `API Key + WorkBuddy Agent ID` 解析为一个
  ACP Runtime（`runtime.id` + `links.acpLink.url` / `links.acpLink.token`）；
- 结果作为 **Connection.config 缓存**写回，reuse **既有 tencent-acp-sse 键**
  （`protocol` / `endpoint` / `bearer_token` / `runtime_id`），从而让**既有
  `acp.py` 通信路径零改动直接消费**——**绝不重新实现 ACP SSE**。

Runtime 生命周期（E3-B 冻结）：
- **Lazy creation on Verify**：Connect（POST /api/connections）**不创建** Runtime；
  只有 Verify 时（无缓存）才创建；
- **Reuse**：`Connection.config` 已有可用 `endpoint + bearer_token + runtime_id`
  缓存时直接复用（不重复创建）；
- **Fail closed**：已缓存但不可达 → 绝不盲目重复创建（无可靠状态信息，见 E3-B §18）。

Credential 边界（E3-B 冻结）：
- `api_key`（Management API `Authorization: Bearer`）与缓存 `bearer_token`
  （acpLink.token）均为敏感值，命中 `SENSITIVE_CONNECTION_CONFIG_KEYS`（读时脱敏）；
- `api_key` 同时按官方契约注入 `agentManifest.secrets`（键 `CODEBUDDY_API_KEY`，
  Agent 沙箱运行依赖；缺失 → Management API 502）。该值只随创建请求发送，
  不写日志（只记录失败类型）、不进 Interaction / Activity / URL。

不做（E3-B 边界）：
- 不新增 Domain Entity / migration；不改 ACP Core（Router / Worker / Session /
  Interaction / Identity / Grant / Authorization / Bootstrap / Delivery / Runtime
  pipeline / stage codes）；
- 不实现 Agent discovery（V0：由用户提供 Agent ID）；
- 不新增 ACP 错误码（失败一律收敛为既有 `AgentDispatchFailedError` 语义 / 探针
  `reachable=False`）。
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import urllib.parse
from dataclasses import dataclass, replace

from app.domain.agent_probe_snapshot import AgentProbeSnapshot
from app.domain.connection import Connection

LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------- Provider 标识与 config 键

#: `Connection.config["provider"]` 的 WorkBuddy 取值（Provider-native 标志）。
WORKBUDDY_PROVIDER = "workbuddy"

#: Provider 标志键 / WorkBuddy Agent ID 键 / API Key 键（用户 Connect 输入）。
PROVIDER_KEY = "provider"
AGENT_ID_KEY = "workbuddy_agent_id"
API_KEY_KEY = "api_key"

#: Runtime 缓存键 —— 与 `acp.py` 的 tencent-acp-sse binding **完全一致**，
#: 因此 Provisioner 写回的缓存可被既有通信路径零改动消费。
PROTOCOL_KEY = "protocol"
ENDPOINT_KEY = "endpoint"
BEARER_TOKEN_KEY = "bearer_token"
RUNTIME_ID_KEY = "runtime_id"
PROTOCOL_TENCENT_ACP_SSE = "tencent-acp-sse"

#: 探针 `probe` 标签（非 WorkBuddy 缓存时的诊断标签）。
WORKBUDDY_PROBE_LABEL = "workbuddy"

# ---------------------------------------------------------------- Management API 契约
# 来源：docs/step-10.1.3-tencent-acp-wire.md（「Tencent ACP Runtime 事实」）。
DEFAULT_MANAGEMENT_BASE_URL = "https://www.codebuddy.cn"
RUNTIME_CREATE_PATH = "/v2/agentos/runtimes"
SOURCE_APP_HEADER = "X-Source-App"
#: `X-Source-App` 的实际取值在仓库中尚未确认（E3-A §9）——**不猜生产值**：
#: 由 provider configuration 提供（Connection.config["source_app"] 或环境变量
#: `WORKBUDDY_SOURCE_APP`），未提供时不发送该头。
SOURCE_APP_ENV_VAR = "WORKBUDDY_SOURCE_APP"
MANIFEST_VERSION = "1.0"
DEFAULT_MANAGEMENT_TIMEOUT_S = 30.0

#: `agentManifest.secrets` 中承载 Agent 沙箱 CodeBuddy API Key 的键名
#: （官方契约：secrets 必填，缺失 → 上游 502）。
RUNTIME_SECRET_API_KEY = "CODEBUDDY_API_KEY"

#: 用户可见的失败文案（**泛化**——不含 HTTP 状态 / runtime_id / acpLink / token）。
_PROBE_DETAIL_UNVERIFIED = "workbuddy connection could not be verified"


@dataclass(frozen=True)
class AcpProvision:
    """Management API 创建的 Runtime + acpLink（**Adapter 内部值对象**，非 Domain Entity）。

    对应 `POST /v2/agentos/runtimes` 响应：
    `data.id`（runtime.id）、`data.links.acpLink.url` / `data.links.acpLink.token`。
    """

    runtime_id: str
    url: str
    token: str


class WorkBuddyManagementError(Exception):
    """WorkBuddy Management API 失败（**Adapter 内部**；不泄漏成 ACP 错误码）。"""


def _default_transport(
    method: str, url: str, headers: dict[str, str], body: bytes, timeout_s: float,
) -> tuple[int, bytes]:
    """默认 HTTP transport（urllib 之上的裸 http.client，与 acp.py 同族）。"""
    parts = urllib.parse.urlsplit(url)
    scheme = (parts.scheme or "https").lower()
    if scheme not in ("http", "https"):
        raise WorkBuddyManagementError(f"unsupported scheme: {scheme}")
    host = parts.hostname or ""
    if not host:
        raise WorkBuddyManagementError("missing host")
    port = parts.port or (443 if scheme == "https" else 80)
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    conn_cls = (
        http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
    )
    conn = conn_cls(host, port, timeout=timeout_s)
    try:
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 关闭失败不影响结论
            pass


def _parse_runtime_response(raw: bytes) -> AcpProvision:
    """`POST /v2/agentos/runtimes` 响应 → `AcpLink`（Fail Closed）。"""
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkBuddyManagementError("management API returned non-JSON") from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise WorkBuddyManagementError("management API response missing data")
    raw_id = data.get("id")
    runtime_id = str(raw_id).strip() if isinstance(raw_id, (str, int)) else ""
    links = data.get("links")
    acp_link = links.get("acpLink") if isinstance(links, dict) else None
    url = acp_link.get("url") if isinstance(acp_link, dict) else None
    token = acp_link.get("token") if isinstance(acp_link, dict) else None
    if not runtime_id:
        raise WorkBuddyManagementError("management API response missing runtime id")
    if not isinstance(url, str) or not url.strip().startswith(("http://", "https://")):
        raise WorkBuddyManagementError("management API response missing acpLink.url")
    if not isinstance(token, str) or not token.strip():
        raise WorkBuddyManagementError("management API response missing acpLink.token")
    return AcpProvision(runtime_id=runtime_id, url=url.strip(), token=token.strip())


class WorkBuddyManagementClient:
    """最小 WorkBuddy Management API 客户端（职责仅 `create_runtime`）。

    - 鉴权：`Authorization: Bearer <API Key>`（**只用于管理面**）+ 可选
      `X-Source-App`（provider configuration / 环境变量；未配置则不发送）；
    - body：`runtimeName + agentManifest{id,name,manifestVersion,secrets}`；
      secrets 必含 `CODEBUDDY_API_KEY`（官方契约，Agent 沙箱依赖）；
    - `transport` 可注入（测试用确定性桩，不触网）。
    """

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_MANAGEMENT_BASE_URL,
        timeout_s: float = DEFAULT_MANAGEMENT_TIMEOUT_S,
        source_app: str | None = None,
        transport=None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._source_app = (
            source_app if source_app is not None else os.environ.get(SOURCE_APP_ENV_VAR)
        )
        self._transport = transport or _default_transport

    def create_runtime(self, *, api_key: str, agent_id: str, runtime_name: str) -> AcpProvision:
        """创建 Runtime；任何失败 → `WorkBuddyManagementError`（不泄漏细节）。

        官方契约（CodeBuddy 后台对接说明，2026-10 实测核验）：
        `agentManifest.secrets` **必填**，且必须包含
        `{"key": "CODEBUDDY_API_KEY", "value": <API Key>}`——Agent 沙箱依赖它
        调 CodeBuddy API；缺失时上游直接 **502**（Fail Closed，无错误细节）。
        这覆盖了 E3-A §12 的「不注入 secrets」裁决：真实 wire 证明该裁决与
        官方契约冲突，以真实契约为准。
        """
        body = json.dumps({
            "runtimeName": runtime_name,
            "agentManifest": {
                "id": agent_id,
                "name": runtime_name,
                "manifestVersion": MANIFEST_VERSION,
                "secrets": [
                    {"key": RUNTIME_SECRET_API_KEY, "value": api_key},
                ],
            },
        }).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if isinstance(self._source_app, str) and self._source_app.strip():
            headers[SOURCE_APP_HEADER] = self._source_app.strip()
        url = f"{self._base_url}{RUNTIME_CREATE_PATH}"
        try:
            status, raw = self._transport("POST", url, headers, body, self._timeout_s)
        except WorkBuddyManagementError:
            raise
        except Exception as exc:  # noqa: BLE001 传输失败统一收口（只留类型，不带密钥）
            raise WorkBuddyManagementError(
                f"management API transport error: {type(exc).__name__}") from exc
        if status not in (200, 201):
            raise WorkBuddyManagementError(f"management API status {status}")
        return _parse_runtime_response(raw)


def _runtime_name(connection: Connection, agent_id: str) -> str:
    """稳定、可追踪但**不泄漏敏感信息**的 runtimeName（E3-B §16）。

    仅由 connection id + agent id 组成——绝不含 tenant secret / API key / internal token。
    """
    conn_part = connection.id if connection.id is not None else "new"
    return f"steeringflow-conn{conn_part}-agent{agent_id}"


def _cached_acp(config: dict) -> AcpProvision | None:
    """从 config 读回**已缓存且结构完整**的 acpLink（不可用则 None）。"""
    endpoint = config.get(ENDPOINT_KEY)
    token = config.get(BEARER_TOKEN_KEY)
    runtime_id = config.get(RUNTIME_ID_KEY)
    if not isinstance(endpoint, str) or not endpoint.strip().startswith(
        ("http://", "https://")
    ):
        return None
    if not isinstance(token, str) or not token.strip():
        return None
    if runtime_id is None:
        return None
    rid = str(runtime_id).strip()
    if not rid:
        return None
    return AcpProvision(runtime_id=rid, url=endpoint.strip(), token=token.strip())


def _with_acp(config: dict, provision: AcpProvision) -> dict:
    """把 acpLink 写入 config（reuse 既有 tencent-acp-sse 键 → 通信路径零改动）。"""
    return {
        **config,
        PROTOCOL_KEY: PROTOCOL_TENCENT_ACP_SSE,
        ENDPOINT_KEY: provision.url,
        BEARER_TOKEN_KEY: provision.token,
        RUNTIME_ID_KEY: provision.runtime_id,
    }


class WorkBuddyProvisioner:
    """WorkBuddy Provider-native 的 **lazy provisioning** 组合（verify 入口）。

    组合（不重新实现任何 ACP 通信）：
    - `probe`：既有的 `ACPAdapter.probe(connection)`（可达性探针）；
    - `client`：`WorkBuddyManagementClient`（管理面；可注入 fake）。

    `verify(connection) -> (AgentProbeSnapshot, updated_config | None)`：
    - `updated_config is None` → **无写入**（非 WorkBuddy，或缓存已可复用）；
    - `updated_config is not None` → 新缓存，由调用方（管理 API）持久化到
      `Connection.config`（**本类不写库**——与 ACPAdapter 只读纪律一致）。
    """

    def __init__(self, probe, client: WorkBuddyManagementClient | None = None) -> None:
        self._probe = probe
        self._client = client

    def verify(self, connection: Connection) -> tuple[AgentProbeSnapshot, dict | None]:
        if not isinstance(connection, Connection):
            return AgentProbeSnapshot(False, "invalid connection", None), None
        config = connection.config if isinstance(connection.config, dict) else {}
        if config.get(PROVIDER_KEY) != WORKBUDDY_PROVIDER:
            # 非 WorkBuddy：只读探针（无写入；保持既有 health 语义）。
            return self._probe(connection), None

        cached = _cached_acp(config)
        if cached is not None:
            # 缓存已存在 → **只复用，绝不重复创建**（无可靠状态信息，Fail closed）。
            return self._probe(connection), None

        link = self._create(connection, config)
        if link is None:
            return (
                AgentProbeSnapshot(False, _PROBE_DETAIL_UNVERIFIED, WORKBUDDY_PROBE_LABEL),
                None,
            )
        updated = _with_acp(config, link)
        snapshot = self._probe(replace(connection, config=updated))
        # 创建成功即缓存（即便本次探活未通）——保证后续 Verify 复用同一 Runtime，
        # 不给平台制造重复 Runtime；运行时就绪由后续 Verify 收敛（E3-B §8/§17/§18）。
        return snapshot, updated

    def _create(self, connection: Connection, config: dict) -> AcpLink | None:
        if self._client is None:
            return None
        api_key = config.get(API_KEY_KEY)
        agent_id = config.get(AGENT_ID_KEY)
        if not isinstance(api_key, str) or not api_key.strip():
            return None
        if isinstance(agent_id, str):
            agent_id = agent_id.strip()
        if agent_id is None or (isinstance(agent_id, str) and not agent_id):
            return None
        try:
            return self._client.create_runtime(
                api_key=api_key.strip(),
                agent_id=str(agent_id),
                runtime_name=_runtime_name(connection, str(agent_id)),
            )
        except WorkBuddyManagementError as exc:
            # 服务端诊断只记类型（不含 API Key / token / 完整 URL）。
            LOGGER.warning("workbuddy runtime provisioning failed: %s", exc)
            return None
