"""Step 11.13 — Management Plane Closure Audit（验收层，锁定真实边界）。

本文件不堆砌实现细节守卫，而是核验「三 Management Slice 合在一起后是否形成
真正闭合、互不污染的 Control Plane 管理面」这一高层不变量。对应审计文档维度
A–E；F 维度（守卫是否开始保护历史实现细节）只在 `docs/step-11.13-*.md` 中标记。

A. 三对象边界：管理面 API / Service / Schema 互不 import 对方实体、不依赖 Runtime；
B. 生命周期一致：每个管理 router 恰好 4 端点、无 DELETE；credential_ref /
   脱敏 / status 策略的*差异*是领域事实，被显式锁定为预期；
C. Management → Runtime 边界：管理面模块不 import 任何 Runtime 包
   （runtime / api.runtime / adapters / application.routing|delivery|dispatcher|
   coordinator|session*|interaction* / persistence 实现）；
D. Repository 双路径不混淆：runtime 用 find_active_by_channel、management 用
   list_connections，二者互不跨界（管理 Service 绝不调用 runtime 读路径）；
E. 前端三页独立：各脚本仅声明自身 /api/X 常量、不互相 import、Connection 页
   额外只引用 /api/channels + /api/agents（解耦解析），无跨页业务逻辑耦合。
"""

import re
from pathlib import Path

from app.schemas.agents import AgentCreate, AgentResponse, AgentUpdate
from app.schemas.channels import (
    ChannelCreate,
    ChannelResponse,
    ChannelUpdate,
    sanitize_channel_config,
)
from app.schemas.connections import (
    CONNECTION_MANAGEMENT_STATUSES,
    ConnectionCreate,
    ConnectionResponse,
    ConnectionUpdate,
    sanitize_connection_config,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
APP = REPO_ROOT / "backend" / "app"
MGMT_API = APP / "api" / "management"
FRONTEND = REPO_ROOT / "frontend"

MANAGEMENT_API_FILES = {
    "agents": MGMT_API / "agents.py",
    "channels": MGMT_API / "channels.py",
    "connections": MGMT_API / "connections.py",
}
MANAGEMENT_SERVICE_FILES = {
    "agents": APP / "application" / "agents.py",
    "channels": APP / "application" / "channels.py",
    "connections": APP / "application" / "connections.py",
}
SCHEMA_FILES = {
    "agents": APP / "schemas" / "agents.py",
    "channels": APP / "schemas" / "channels.py",
    "connections": APP / "schemas" / "connections.py",
}
ENTITIES = {"agents", "channels", "connections"}

# 管理面严禁越界依赖 Runtime / Persistence 实现（C 维度真实边界）
FORBIDDEN_RUNTIME_PREFIXES = (
    "app.runtime",
    "app.api.runtime",
    "app.adapters",
    "app.application.routing",
    "app.application.delivery",
    "app.application.dispatcher",
    "app.application.coordinator",
    "app.application.session",
    "app.application.interaction",
    "app.persistence",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _imported_app_modules(src: str) -> list[str]:
    mods = []
    for m in re.finditer(
        r"^\s*(?:from\s+(app\.[\w\.]+)\s+import|import\s+(app\.[\w\.]+))", src, re.M
    ):
        mods.append(m.group(1) or m.group(2))
    return mods


# ================================================================== A. 三对象边界


def test_a_management_modules_import_only_their_own_and_no_runtime() -> None:
    """管理面 API / Service / Schema 只依赖自身实体 + errors/schemas/pydantic，
    且**绝不** import 任何 Runtime 包或 persistence 实现（否则即污染）。"""
    for entity, path in {**MANAGEMENT_API_FILES,
                         **MANAGEMENT_SERVICE_FILES,
                         **SCHEMA_FILES}.items():
        src = _read(path)
        others = ENTITIES - {entity}
        for mod in _imported_app_modules(src):
            assert mod not in FORBIDDEN_RUNTIME_PREFIXES, \
                f"{path.name} 越界依赖 Runtime/persistence：{mod}"
            for other in others:
                assert not mod.startswith(f"app.application.{other}"), \
                    f"{path.name} 跨实体耦合：import {mod}"
                assert not mod.startswith(f"app.schemas.{other}"), \
                    f"{path.name} 跨实体耦合：import {mod}"


# ================================================================== B. 生命周期一致性


def test_b_each_management_router_has_4_endpoints_no_delete() -> None:
    """生命周期一致：每个管理 router 端点面 = POST/GET/GET{id}/PATCH（恰好 4），
    无 DELETE（软下线 = PATCH status=inactive）。这是三切片共同的形态合同。

    Cloud-0.1.2.2.2.1 例外：connections 增加**即时诊断探针**
    `GET /{id}/health`（派生快照，非持久子系统）→ connections 为 (2 POST, 3 GET,
    1 PATCH)（M6.6-E3-B 再增 `POST /{id}/verify` = Provider execution verification）；
    agents / channels 仍为 (1, 2, 1)。端点面按实体显式锁定。"""
    expected_posts = {"agents": 1, "channels": 1, "connections": 2}
    expected_gets = {"agents": 2, "channels": 2, "connections": 3}
    for entity, path in MANAGEMENT_API_FILES.items():
        src = _read(path)
        posts = src.count("@router.post(")
        gets = src.count("@router.get(")
        patches = src.count("@router.patch(")
        deletes = src.count("@router.delete(")
        assert deletes == 0, f"{entity} 管理面不应有 DELETE：{deletes}"
        assert patches == 1, f"{entity} 端点面应为 1 PATCH，实际 {patches}"
        assert posts == expected_posts[entity], \
            f"{entity} POST 端点面应为 {expected_posts[entity]}，实际 {posts}"
        assert gets == expected_gets[entity], \
            f"{entity} GET 端点面应为 {expected_gets[entity]}，实际 {gets}"


def test_b_lifecycle_differences_are_locked_domain_facts() -> None:
    """B 维度的「故意不同」是领域事实，显式锁定：

    - credential_ref 仅存在于 Agent + Connection，且二者响应体**均排除**它
      （引用入、引用不出）；Channel 根本没有 credential_ref（凭据住 config）；
    - Channel 与 Connection config 均读时脱敏（sentinel；Connection 为
      16.1.2 补齐——P1 真实接入证实其 config 携带 runtime 凭据）；
    - Connection status 在 schema 层强制为纯管理态 {active, inactive}
      （无 runtime error 双轴），与 Channel 同构、与 Agent 不同构。
    """
    # credential_ref：Agent / Connection 请求有、响应无；Channel 完全无
    assert "credential_ref" in AgentCreate.model_fields
    assert "credential_ref" in AgentUpdate.model_fields
    assert "credential_ref" not in AgentResponse.model_fields
    assert "credential_ref" in ConnectionCreate.model_fields
    assert "credential_ref" in ConnectionUpdate.model_fields
    assert "credential_ref" not in ConnectionResponse.model_fields
    assert "credential_ref" not in ChannelCreate.model_fields
    assert "credential_ref" not in ChannelUpdate.model_fields
    assert "credential_ref" not in ChannelResponse.model_fields

    # config 脱敏：Channel 与 Connection 均有哨兵（Connection 为 16.1.2 补齐）
    assert sanitize_channel_config({"token": "x", "corp_id": "1"}) == {
        "token": "********",
        "corp_id": "1",
    }
    assert sanitize_connection_config(
        {"bearer_token": "t", "endpoint": "http://x"}) == {
        "bearer_token": "********", "endpoint": "http://x"}
    conn_echo = ConnectionResponse.model_validate(
        {"id": 1, "name": "n", "channel_id": 1, "agent_id": 1,
         "status": "active", "config": {"bearer_token": "t", "k": "v"}})
    assert conn_echo.config["bearer_token"] == "********"
    assert conn_echo.config["k"] == "v"

    # status 策略：Connection 为 schema 级纯管理态白名单；Agent 无此白名单
    assert CONNECTION_MANAGEMENT_STATUSES == {"active", "inactive"}
    assert "status" in AgentResponse.model_fields  # 存在
    # Agent 的 status 不是字面量/枚举约束（运行时 error 双轴由服务层管理）
    assert AgentResponse.model_fields["status"].annotation is str


# ================================================================== C. Management → Runtime 边界


def test_c_management_api_dir_has_no_runtime_dependency() -> None:
    """api/management 整个目录不 import 任何 Runtime 包/模块。"""
    for path in MGMT_API.rglob("*.py"):
        for mod in _imported_app_modules(_read(path)):
            assert mod not in FORBIDDEN_RUNTIME_PREFIXES, \
                f"{path.name} 越界依赖 Runtime：{mod}"


# ================================================================== D. Repository 双路径不混淆


def test_d_repository_dual_read_path_not_conflated() -> None:
    """D 维度：Connection repository 的两条读路径**互不替代**——

    - runtime（9.4 Routing / Agent Dispatch）只用 find_active_by_channel
      （单 Channel + active 过滤）；
    - management（11.11）只用 list_connections（全量、含 inactive）。

    污染判据：管理 Service / API 绝不调用 find_active_by_channel；
    runtime 绝不调用 list_connections。"""
    port_src = _read(APP / "repositories" / "connection.py")
    assert "def find_active_by_channel(" in port_src
    assert "def list_connections(" in port_src

    mgmt_service = _read(MANAGEMENT_SERVICE_FILES["connections"])
    # 注：docstring 会*提及* find_active_by_channel（说明不复用），但绝不应*调用*
    # （带 "(" 的才是调用）；管理 Service 只走 list_connections。
    assert "find_active_by_channel(" not in mgmt_service, \
        "管理 Service 不应调用 runtime 的 active-only 读路径"
    assert "list_connections" in mgmt_service

    mgmt_api = _read(MANAGEMENT_API_FILES["connections"])
    assert "find_active_by_channel(" not in mgmt_api
    assert "list_connections" in mgmt_api

    runtime_routing = _read(APP / "application" / "routing.py")
    assert "find_active_by_channel(" in runtime_routing, \
        "runtime Routing 应使用 find_active_by_channel"
    assert "list_connections" not in runtime_routing, \
        "runtime 不应复用 management 的陈列读"


# ================================================================== E. 前端三页独立


def test_e_frontend_pages_are_independent() -> None:
    """E 维度：三页互链但**不**互耦业务逻辑——

    - 各脚本声明唯一自身的 /api/X 常量，三者互异；
    - 脚本之间不互相 import / require / 引用兄弟页面脚本；
    - Connection 页额外只引用 /api/channels + /api/agents（解耦解析），
      且不引用 /api/users、/api/identities 等越界端点。
    """
    expected_api = {
        FRONTEND / "agents.js": "/api/agents",
        FRONTEND / "channels.js": "/api/channels",
        FRONTEND / "connections.js": "/api/connections",
    }
    declared = {}
    for js_path, api in expected_api.items():
        src = _read(js_path)
        assert f'const API = "{api}";' in src, f"{js_path.name} 缺少自身 API 常量"
        declared[js_path.name] = api
        # 无跨页/跨模块耦合。M7-AUI-9 模块化：允许 import 共享 HTTP 层与统一
        # 顶栏模块；**兄弟页面脚本**仍然是禁区（跨页业务逻辑耦合零容忍）。
        allowed_imports = {
            'import { request } from "/shared/http.js";',
            'import "/nav.js";',
        }
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith("import "):
                assert stripped in allowed_imports, \
                    f"{js_path.name} 出现越界 import：{stripped}"
        assert "require(" not in src, f"{js_path.name} 不应 require 其它模块"
        assert "../" not in src, f"{js_path.name} 不应引用兄弟页面脚本"

    apis = set(declared.values())
    assert apis == {"/api/agents", "/api/channels", "/api/connections"}

    # Connection 页：仅自身 + 解耦解析的两个参考 API（findall 返回带引号串）
    conn_js = _read(FRONTEND / "connections.js")
    referenced = set(re.findall(r'"/api/[a-z]+"', conn_js))
    assert referenced == {'"/api/connections"', '"/api/channels"', '"/api/agents"'}, \
        f"Connection 页 API 引用面漂移：{referenced}"
    for banned in ("/api/users", "/api/identities"):
        assert banned not in conn_js, f"Connection 页出现越界端点：{banned}"

    # Agent 页 / Channel 页：只引用自身 API（不含参考解析端点）
    agent_js = _read(FRONTEND / "agents.js")
    assert set(re.findall(r'"/api/[a-z]+"', agent_js)) == {'"/api/agents"'}
    channel_js = _read(FRONTEND / "channels.js")
    assert set(re.findall(r'"/api/[a-z]+"', channel_js)) == {'"/api/channels"'}


def test_e_each_html_loads_only_its_own_script() -> None:
    """每个管理页只加载自身脚本（导航是 HTML 链接，不是 JS 业务耦合）。"""
    mapping = {
        FRONTEND / "agents.html": "agents.js",
        FRONTEND / "channels.html": "channels.js",
        FRONTEND / "connections.html": "connections.js",
    }
    for html_path, own_script in mapping.items():
        html = _read(html_path)
        assert f'src="{own_script}"' in html, f"{html_path.name} 未加载 {own_script}"
        for other in ("agents.js", "channels.js", "connections.js"):
            if other != own_script:
                assert f'src="{other}"' not in html, \
                    f"{html_path.name} 不应加载兄弟页面脚本 {other}"
