"""Cloud-M6.3 —— UX Contract Verification。

M6.2 + M6.2-R1 定义的新 UX contract 的机器化守护（**只读**：不修改生产代码 /
API / DB / Runtime / 旧冻结页面）。

覆盖（静态源码断言 + 真实应用探针；不触生产 DB）：
  A. 新页面落地 / 静态可达 / Mount 仍最后
  B. C1  API allowlist（base 字面 ⊆ 4；health = 允许的派生路由）
  C.      无新端点（Backend = 0 changes 反向复核）
  D. C2  Context-aware Connect（Empty / Partial A/B/C 跳过已完成步骤）
  E. C3  POST /api/agents 409 → 不自动复用，按 active/inactive 裁决
  F. C4  POST /api/connections 409 → 不直接视为已接通，按绑定 status 裁决
  G.      health 派生路由（/api/connections/{id}/health）
  H.      First Success = processing=succeeded × delivery=delivered
  I.      Flow Home 五态（empty / partial / connecting / connected / error）
  J.      新页面不暴露 machinery（用户可见面）
  K.      无错误状态机 / 无 onboarding 持久化
  L.      旧页面与既有 guards 不受影响（冻结尾巴回归锚点）
  M.      只 POST/GET；无 PATCH/DELETE/PUT；无本地明文缓存

扫描纪律同 0.1.2.3.2 / 0.1.4.1 / 0.1.5：**先剥离注释**再看真实标记。
"""

from __future__ import annotations

import re

from tests.oss_export_boundary import oss_ui_required
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND = REPO_ROOT / "frontend"

HOME_HTML = FRONTEND / "home.html"
HOME_JS = FRONTEND / "home.js"
CONNECT_HTML = FRONTEND / "connect.html"
CONNECT_JS = FRONTEND / "connect.js"
MAIN_PY = REPO_ROOT / "backend" / "app" / "main.py"

# 冻结页（M6.3 实施边界：保持不动）
WORKSPACE_HTML = FRONTEND / "workspace.html"
WORKSPACE_JS = FRONTEND / "workspace.js"
AGENT_HTML = FRONTEND / "connect-agent.html"
CHANNEL_HTML = FRONTEND / "connect-channel.html"
CHANNEL_JS = FRONTEND / "connect-channel.js"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _strip_js_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    src = re.sub(r"//[^\n]*", " ", src)
    return src


def _connect_js() -> str:
    return _strip_js_comments(_read(CONNECT_JS))


def _home_js() -> str:
    return _strip_js_comments(_read(HOME_JS))


# ================================================================== A. 落地 / 静态可达


def test_a1_new_pages_exist() -> None:
    for path in (HOME_HTML, HOME_JS, CONNECT_HTML, CONNECT_JS):
        assert path.is_file(), f"缺少 {path.name}"


@oss_ui_required()
def test_a2_new_pages_are_served_and_mount_still_last() -> None:
    client = TestClient(app)
    for path in ("/home.html", "/home.js", "/connect.html", "/connect.js"):
        assert client.get(path).status_code == 200, path
    route_types = [type(r).__name__ for r in app.router.routes]
    assert route_types[-1] == "Mount", "静态 mount 必须最后注册（API 优先）"


# ================================================================== B. C1 API allowlist


def test_b1_connect_base_literals_are_exactly_the_four() -> None:
    """C1：connect.js 的 base API 字面恰好 = 四个既有端点。"""
    code = _connect_js()
    referenced = set(re.findall(r'"/api/[a-z]+"', code))
    assert referenced == {
        '"/api/agents"', '"/api/channels"', '"/api/connections"', '"/api/interactions"',
    }, f"connect.js 引用面漂移：{referenced}"


def test_b2_verify_is_the_readiness_derived_route() -> None:
    """C1（M6.6-F4-A Correction 对齐）：Connect 页 readiness 的**统一派生路由**是
    F3 的 `POST /api/connections/{id}/verify`，以及验证通过后的启用端点。

    旧假设「/health 是 Connect 的 readiness 入口」已被 F4-A Correction 取代：
    provider-specific 分派由后端完成，前端只调用统一 `/verify`
    （`/health` 端点本身仍在后端独立存在，供 Advanced 页 / 独立探活使用）。
    """
    code = _connect_js()
    assert "${CONNECTIONS_API}/${connectionId}/verify" in code
    assert "bootstrap-arm" in code
    assert "${CONNECTIONS_API}/${connectionId}/health" not in code


def test_b3_no_aggregated_or_cross_boundary_endpoints() -> None:
    """越界端点/概念禁用列表。

    注意：禁用项必须是**不与既有端点前缀冲突**的精确路径——
    `"/api/connect"` 是 `"/api/connections"` 的子串，会造成假阳性，故不使用。
    """
    code = _connect_js() + _home_js()
    for banned in ("/api/integrations", "/api/workspace", "/api/onboarding",
                   "/api/connect-agent", "/api/connect-channel",
                   "/api/users", "/api/identities", "Integration"):
        assert banned not in code, f"新页面出现越界端点/概念：{banned}"


def test_b4_home_base_literals_are_existing_endpoints_only() -> None:
    """home.js 只引用既有端点（auth + 四个管理读端点），无新增。"""
    code = _home_js()
    referenced = set(re.findall(r'"/api/[a-z]+"', code))
    assert referenced == {
        '"/api/agents"', '"/api/channels"', '"/api/connections"',
        '"/api/interactions"', '"/api/auth"',
    }, f"home.js 引用面漂移：{referenced}"


def test_b4_no_new_backend_routes() -> None:
    """Backend = 0 changes 反向复核：组合根未新增任何 M6 路由。"""
    main_src = _read(MAIN_PY)
    for banned in ("/api/integrations", "/api/workspace", "/api/onboarding",
                   "/api/connect", "/api/connect-channel"):
        assert banned not in main_src, f"main.py 出现新增路由：{banned}"


# ================================================================== C/D. C2 Context-aware


def test_c1_context_detection_branches_on_existing_channel_and_agent() -> None:
    """C2：context-aware——按已有渠道 / 已有 Agent 决定跳过哪一步。"""
    code = _connect_js()
    assert "renderContext" in code
    assert "channels.length > 0" in code
    assert "agentsActive.length > 0" in code
    # 已有渠道：默认复用（不重填凭据）；也可显式「添加另一个渠道」
    assert "使用这个渠道" in code
    assert "添加另一个渠道" in code
    assert "reuseChannelId" in code


def test_c2_reuse_skips_channel_creation() -> None:
    """C2：复用已有渠道时不创建渠道（POST channel 受 needChannelForm 条件保护）。"""
    code = _connect_js()
    assert "const needChannelForm = reuseChannelId === null;" in code
    assert "if (!needChannelForm)" in code


def test_c3_empty_and_partial_entry_contexts() -> None:
    """C2：Empty / Partial A / B / C 四种入口上下文的默认值各就位。"""
    code = _connect_js()
    # Partial A（有渠道无 Agent）→ 复用渠道 + 强制新建 Agent
    assert 'fAgentMode.value = "new"' in code
    # Partial B / C（有 Agent）→ 默认选择已有 Agent
    assert 'fAgentMode.value = "existing"' in code
    assert "activeAgents" in code


# ================================================================== E. C3 Agent 409


def test_e1_agent_409_is_handled() -> None:
    """C3：POST /api/agents 返回 409 时进入裁决分支（既有：409 = 同名已存在）。"""
    code = _connect_js()
    assert "handleAgentConflict" in code
    assert 'request("POST", AGENTS_API' in code


def test_e2_no_automatic_reuse_on_409() -> None:
    """C3-1：不自动复用——409 分支必须 return（不静默沿用同名 Agent 继续接通）。"""
    code = _connect_js()
    block = code[code.index("handleAgentConflict("):]
    assert "return;" in block


def test_e3_409_queries_agents_and_branches_on_active() -> None:
    """C3-2/3：查询当前 tenant 的 agents，按 status 分叉裁决。"""
    code = _connect_js()
    assert 'same.status === "active"' in code
    assert "已存在同名 Agent" in code
    assert "使用这个已有 Agent" in code
    assert "换一个名字" in code


def test_e4_inactive_same_name_agent_is_not_offered_as_default() -> None:
    """C3-4：同名 Agent 为 inactive 时，不提供「使用」作为默认可用动作。"""
    code = _connect_js()
    assert "当前不可直接使用" in code
    assert "查看高级设置" in code


# ================================================================== F. C4 Connection 409


def test_f1_connection_409_is_handled() -> None:
    """C4-1：不直接将 409 视为 Connected——进入绑定裁决分支。"""
    code = _connect_js()
    assert "handleExistingBinding" in code
    assert "c.channel_id === chosen.channelId && c.agent_id === chosen.agentId" in code


def test_f2_finds_binding_then_checks_status() -> None:
    """C4-2/3/4/5：按 (channel_id, agent_id) 找既有 binding 并检查 status；
    active → Connected；inactive → Action Required。"""
    code = _connect_js()
    assert 'existing.status === "active"' in code
    assert "已接通" in code
    assert "需要处理" in code


def test_f3_no_second_connection_created() -> None:
    """C4-6：409 分支不得再 POST 一条 Connection（DB UNIQUE 硬保证之外的前端纪律）。"""
    code = _connect_js()
    start = code.index("async function handleExistingBinding")
    end = code.index("async function probe")
    block = code[start:end]
    assert 'request("POST", CONNECTIONS_API' not in block
    assert "未创建新的连接" in block


def test_f4_no_saga_rollback_or_new_idempotency() -> None:
    """C4-7：不实现 Saga / rollback / 新幂等 API。"""
    code = (_connect_js() + _home_js()).lower()
    for banned in ("saga", "rollback", "compensat", "transactionmanager"):
        assert banned not in code, f"出现回滚/事务机制：{banned}"
    assert "idempotency" not in code


# ================================================================== G. health 派生路由


def test_g1_probe_is_advisory_and_never_blocks() -> None:
    """G：探针建议性——503 = 探针不可用（非连接结论）；不落库。"""
    code = _connect_js()
    assert "err.status === 503" in code
    assert "非连接结论" in code
    assert "snap.reachable" in code


# ================================================================== H. First Success 双状态


def test_h1_first_success_derived_from_real_dual_status() -> None:
    """H：First Success 由原生双状态推导（succeeded × delivered）。"""
    for code in (_connect_js(), _home_js()):
        assert 'const PROCESSING_SUCCEEDED = "succeeded";' in code
        assert 'const DELIVERY_DELIVERED = "delivered";' in code
        assert "processing_status === PROCESSING_SUCCEEDED" in code
        assert "delivery_status === DELIVERY_DELIVERED" in code


def test_h2_success_copy_is_user_language_not_machinery() -> None:
    """H：成功语义用用户语言，绝不等于「配置成功」。"""
    code = _connect_js()
    assert "已连接到你的 Agent" in code
    for banned in ("Connection created", "Configuration ready",
                   "Create Agent", "Create Connection"):
        assert banned not in code, f"出现内部成功语义：{banned}"


# ================================================================== I. Flow Home 五态


def test_i1_five_states_are_implemented() -> None:
    code = _home_js()
    for state in ('"empty"', '"partial"', '"connected"', '"error"'):
        assert state in code, f"缺失状态：{state}"
    assert "renderConnecting" in code  # 五态之 Connecting（瞬时）


def test_i2_flow_home_renders_the_three_segment_flow() -> None:
    html = _read(HOME_HTML)
    for node in ("node-channel", "node-hub", "node-agent"):
        assert f'id="{node}"' in html, f"缺失流节点：{node}"
    assert "flow-arrow" in html


def test_i3_home_checks_auth_and_falls_back_to_workspace() -> None:
    code = _home_js()
    assert 'const AUTH_API = "/api/auth";' in code
    assert 'window.location.replace("/workspace.html")' in code


# ================================================================== J. 新页面不暴露 machinery


def test_j1_home_primary_surface_exposes_no_machinery() -> None:
    """J：Flow Home 主表面（#home-section）不出现内部 Domain Model 名词。
    （能力入口保留在折叠的 Advanced 组内——不删除，只是首用不突出。）"""
    html = _read(HOME_HTML)
    start = html.index('<section class="card" id="home-section">')
    end = html.index("</section>", start)
    primary = html[start:end]
    for banned in ("Connection", "Identity", "Session", "Activity",
                   "Interaction", "Config", "Endpoint", "Binding", "Tenant"):
        assert banned not in primary, f"Flow Home 主表面暴露 machinery：{banned}"


def test_j2_connect_page_exposes_no_internal_action_labels() -> None:
    """J：统一 Connect 页不出现 Create Agent/Channel/Connection 等内部动作名
    与 Connection config 等内部配置术语。"""
    html = _read(CONNECT_HTML)
    code = _connect_js()
    for banned in ("Create Agent", "Create Channel", "Create Connection",
                   "Configuration ready", "Connection config",
                   "Connection Configuration", "Probe", "绑定名称",
                   "Identity", "Session", "Activity"):
        assert banned not in html, f"connect.html 暴露内部标签：{banned}"
        assert banned not in code, f"connect.js 暴露内部标签：{banned}"


def test_j3_progressive_disclosure_of_advanced_fields() -> None:
    """J：地址 / 类型 / 访问配置等 machinery 下移到「高级」折叠区。"""
    html = _read(CONNECT_HTML)
    assert 'class="payload-box"' in html
    assert "高级：地址 / 类型 / 访问配置（可选）" in html


# ================================================================== K. 无错误状态机 / 无持久化


def test_k1_no_new_error_state_machine() -> None:
    code = (_connect_js() + _home_js()).lower()
    for banned in ("errorstate", "failurestate", "error_state_machine",
                   "failure_state_machine", "errormachine"):
        assert banned not in code, f"出现新的错误状态机概念：{banned}"


def test_k2_no_onboarding_persistence() -> None:
    code = (_connect_js() + _home_js()).lower()
    for banned in ("onboarding", "provisioning", "first_success", "connect_status"):
        assert banned not in code, f"出现 onboarding/provisioning 状态：{banned}"


def test_k3_no_client_side_secret_caching() -> None:
    """客户端不缓存任何 secret（M6.3 冻结）。

    M7-AUI-10.6.2-C 合同收窄：home.js 允许以 sessionStorage 保存**非敏感**的
    Flow 选择（key 固定 "sf.flow.selection"，仅 {ch, ag} 两个 ID，见 10.4
    test_a6 的定向合同）；localStorage 仍全量禁止；凭据类字面仍禁止出现。
    """
    code = _connect_js() + _home_js()
    assert "localStorage" not in code
    ss_calls = re.findall(r"sessionStorage\.(\w+)\(", code)
    assert set(ss_calls) <= {"getItem", "setItem", "removeItem"}, ss_calls
    assert 'const FLOW_SELECTION_KEY = "sf.flow.selection";' in code
    home = _home_js()
    sel_block = home[home.index("function saveFlowSelection")
                     :home.index("function extractWorkbuddyIdentity")]
    for banned in ("api_key", "bearer_token", "runtime_id", "credential"):
        assert banned not in sel_block, f"Flow 选择状态不得携带 {banned}"


# ================================================================== L. 旧页面 / 既有 guards 不受影响


def test_l1_frozen_pages_still_hold_their_markers() -> None:
    """L：冻结页仍履行它们的**冻结职责**（与既有 guards 同源）。

    M7-AUI-8.2 更新：
    - workspace 的两个并列接入 CTA（目标相同）合并为一个「开始接入 →」；
    - 旧接入页只保留兼容职责（HTTP 200 + 客户端跳转 /connect.html）——
      旧流程 body 标记与旧脚本随死代码一并移除（页面文件本身仍然存在）。"""
    workspace = _read(WORKSPACE_HTML)
    assert 'id="cta-start-connect"' in workspace
    assert 'href="/connect.html"' in workspace
    assert "开始接入" in workspace

    for legacy in (AGENT_HTML, CHANNEL_HTML):
        src = _read(legacy)
        # 兼容契约不变：客户端跳转到统一接入页（避免 404 / 破坏书签）。
        assert "url=/connect.html" in src
        assert 'window.location.replace("/connect.html")' in src
        # 旧脚本不再被加载（脚本文件已删除）。
        assert "connect-agent.js" not in src
        assert "connect-channel.js" not in src


def test_l2_workspace_ui_still_only_talks_to_auth_api() -> None:
    js = _read(WORKSPACE_JS)
    assert 'const API = "/api/auth";' in js
    for banned in ("/api/agents", "/api/channels", "/api/connections",
                   "/api/users", "/api/identities", "/api/interactions"):
        assert banned not in js, f"Workspace UI 越界调用：{banned}"
    # M6.3 新增：已认证后进入 Flow Home
    assert 'window.location.replace("/home.html")' in js


def test_l3_first_success_contract_lives_on_unified_connect() -> None:
    """L：First Success 判定契约仍在**活跃的**接入页上。

    M7-AUI-8.2：旧 `connect-channel.js` 已删除（页面只做兼容跳转），
    因此双状态派生的守卫点迁移到统一接入页 `connect.js`
    （同一冻结语义：processing=succeeded × delivery=delivered）。"""
    js = _connect_js()
    assert 'const PROCESSING_SUCCEEDED = "succeeded";' in js
    assert 'const DELIVERY_DELIVERED = "delivered";' in js
    assert "processing_status === PROCESSING_SUCCEEDED" in js
    assert "delivery_status === DELIVERY_DELIVERED" in js


# ================================================================== M. HTTP 方法面


def test_m1_only_post_and_get() -> None:
    """M：新页面只做创建 + 读取；编辑归管理页。

    M7-AUI-6 范围更新（用户明确要求「已保存凭据的查看 / 更换」，且只读检查确认
    后端 `PATCH /api/channels/{id}` 已存在、config 为 merge 语义——留空即保留旧 Secret）：
    Flow Home 允许对 **Channel 凭据** 做 PATCH；仍禁止 PUT / DELETE，
    也禁止在 connect.js 里做任何编辑。
    """
    connect = _connect_js()
    assert '"GET"' in connect
    for banned in ('"PATCH"', '"DELETE"', '"PUT"'):
        assert banned not in connect, f"Connect 页出现 {banned}"

    home = _home_js()
    assert '"GET"' in home
    for banned in ('"DELETE"', '"PUT"'):
        assert banned not in home, f"Flow Home 出现 {banned}"
    assert home.count('"PATCH"') == 1, "Flow Home 只允许渠道凭据更新这一处 PATCH"
    at = home.index('"PATCH"')
    assert "CHANNELS_API" in home[max(0, at - 400):at + 400], \
        "PATCH 只能作用于 channels 资源"
