"""Step 12.5 —— User / Identity Management UI 验收（管理闭环的最后两块拼图）。

按 11.5/11.9/11.12 同一 vanilla 形态与守卫结构，经源码 AST/文本断言锁定：

- A 落地与可达：四件套存在；真实应用静态可达；五页互链；
- B API 面：users.js 只消费 /api/users；identities.js = 自身 + 解耦解析
  （/api/users + /api/channels）；无 DELETE；
- C 冻结合同：active-only 下拉（UX 过滤，非后端门禁）；绑定骨架
  （user_id / channel_id / external_id）只在创建 payload 出现一次，
  编辑态只读展示、绝不进入 Update payload；
- D status / metadata：status 仅管理态（创建固定 active）；metadata
  无哨兵、所见即所存、未修改省略 / 有修改整段替换；
- E 脚本独立：每页只加载自身脚本，无 import/require；
- F 不做清单零出现：无硬删除、无 Identity Linking 入口、无越界概念。
"""

import re

from tests.oss_export_boundary import oss_ui_required
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND = Path(__file__).resolve().parents[1]
FRONTEND = BACKEND.parent / "frontend"

INDEX = FRONTEND / "agents.html"
USERS_HTML = FRONTEND / "users.html"
USERS_JS = FRONTEND / "users.js"
IDENTITIES_HTML = FRONTEND / "identities.html"
IDENTITIES_JS = FRONTEND / "identities.js"
STYLES = FRONTEND / "styles.css"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ================================================================== A. 落地与可达


def test_a1_user_identity_ui_files_exist() -> None:
    """User / Identity Management UI 落地（复用 styles.css，无新 CSS）。"""
    for path in (USERS_HTML, USERS_JS, IDENTITIES_HTML, IDENTITIES_JS, STYLES):
        assert path.is_file(), f"缺少 {path.name}"


@oss_ui_required()
def test_a2_static_ui_reachable_and_api_routes_alive() -> None:
    """真实应用：/users.html、/identities.html 与各自 js 可达；
    既有四页仍可达；/api/users、/api/identities 路由在册。"""
    client = TestClient(__import__("app.main", fromlist=["app"]).app)

    page = client.get("/users.html")
    assert page.status_code == 200
    # M7-AUI-8.2：页面标题改为用户任务语言（成员管理 / 外部身份），能力不变。
    assert "成员管理" in page.text and "user-form" in page.text

    page = client.get("/identities.html")
    assert page.status_code == 200
    assert "外部身份" in page.text and "identity-form" in page.text

    assert client.get("/users.js").status_code == 200
    assert client.get("/identities.js").status_code == 200
    assert client.get("/").status_code == 200
    assert client.get("/channels.html").status_code == 200
    assert client.get("/connections.html").status_code == 200

    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/users" in paths and "/api/identities" in paths


def test_a3_five_management_pages_are_cross_linked() -> None:
    """五页互链（Agent ⇄ Channel ⇄ Connection ⇄ User ⇄ Identity），同一服务、无构建链。"""
    index = _read(INDEX)
    assert 'href="/users.html"' in index
    assert 'href="/identities.html"' in index
    assert 'href="/channels.html"' in index
    assert 'href="/connections.html"' in index

    # 新页互相回链 + 回 Flow Home（M7-AUI-8.2：Agent 管理入口是显式 /agents.html）
    for html, others in (
        (USERS_HTML, ("/agents.html", "/identities.html")),
        (IDENTITIES_HTML, ("/agents.html", "/users.html")),
    ):
        src = _read(html)
        for target in others:
            assert f'href="{target}"' in src, f"{html.name} 缺少指向 {target} 的互链"

    # 既有页也带出新入口（管理员可从任意页进入 User/Identity）
    for name in ("channels.html", "connections.html"):
        src = _read(FRONTEND / name)
        assert 'href="/users.html"' in src, f"{name} 缺少 User 入口"
        assert 'href="/identities.html"' in src, f"{name} 缺少 Identity 入口"


# ================================================================== B. API 面（各管各的实体 + 解耦解析）


def test_b1_users_ui_only_talks_to_users_api() -> None:
    """users.js 的全部 fetch 目标恰好 = /api/users（禁止越界端点）。"""
    js = _read(USERS_JS)
    assert 'const API = "/api/users";' in js
    assert set(re.findall(r'"/api/[a-z]+"', js)) == {'"/api/users"'}, \
        f"User 页 API 引用面漂移：{re.findall(r'\"/api/[a-z]+\"', js)}"
    for banned in ("/api/identities", "/api/channels", "/api/connections",
                   "chat", "websocket"):
        assert banned not in js.lower(), f"User UI 出现越界端点/概念：{banned}"


def test_b2_identities_ui_self_plus_decoupled_resolution() -> None:
    """identities.js 的 fetch 目标恰好 = /api/identities + /api/users +
    /api/channels（解耦解析合同：列表只返回绑定 ID，名称由 UI 解析）。"""
    js = _read(IDENTITIES_JS)
    assert 'const API = "/api/identities";' in js
    assert 'const USERS_API = "/api/users";' in js
    assert 'const CHANNELS_API = "/api/channels";' in js
    assert set(re.findall(r'"/api/[a-z]+"', js)) == {
        '"/api/identities"', '"/api/users"', '"/api/channels"'}


def test_b3_ui_has_no_delete() -> None:
    """无 Delete：生命周期 = active ⇄ inactive 软下线（列表提供停用/启用）。"""
    for js in (USERS_JS, IDENTITIES_JS):
        assert "DELETE" not in _read(js)
    for html in (USERS_HTML, IDENTITIES_HTML):
        assert "删除" not in _read(html)


# ================================================================== C. 冻结合同：active-only / immutable binding


def test_c1_identity_create_dropdowns_are_active_only() -> None:
    """新建下拉仅列 active（UX 过滤；后端创建仍 existence-only，无状态门禁——
    与 11.10 Connection 下拉同一裁决）；共享过滤 helper 同时服务两个下拉。"""
    js = _read(IDENTITIES_JS)
    assert 'entity.status !== "active"' in js          # 共享的 active-only 过滤
    assert "fillSelectActiveOnly(fUser" in js
    assert "fillSelectActiveOnly(fChannel" in js
    # 编辑态不渲染绑定下拉（只读文本），不存在「为编辑回填下拉选中值」的逻辑
    assert "fUser.value =" not in js
    assert "fChannel.value =" not in js


def test_c2_binding_never_enters_update_payload() -> None:
    """绑定骨架只在创建 payload 出现（唯一入口）；编辑 payload 从 {} 起步、
    仅追加档案字段（后端 IdentityUpdate extra="forbid" 是第二道防线）。"""
    js = _read(IDENTITIES_JS)
    # 绑定字段字面量在全文件恰好出现一次——只在创建 payload
    assert js.count('"user_id"') == 1
    assert js.count('"channel_id"') == 1
    assert js.count('"external_id"') == 1
    assert '"user_id": Number(fUser.value)' in js
    assert '"channel_id": Number(fChannel.value)' in js
    assert '"external_id": ext' in js
    # 编辑分支 payload 只从 { } 起步（display_name/status/metadata 按需追加）
    assert "const payload = {};" in js


def test_c3_edit_shows_binding_as_readonly_text() -> None:
    """编辑态绑定为只读展示（immutable binding 的 UI 形态），并提示本页不提供换绑。"""
    js = _read(IDENTITIES_JS)
    assert "fUserReadonly.value = bindingLabel(" in js
    assert "fChannelReadonly.value = bindingLabel(" in js
    assert "fExternalReadonly.value = identity.external_id;" in js
    html = _read(IDENTITIES_HTML)
    for field_id in ("f-user-readonly", "f-channel-readonly", "f-external-readonly"):
        assert f'<input type="text" id="{field_id}" readonly>' in html
    assert "绑定不可变" in html


def test_c4_user_page_has_no_binding_concepts() -> None:
    """User 页没有绑定概念：无 channel / external_id（绑定只属于 Identity 页）。"""
    js = _read(USERS_JS)
    for banned in ('"user_id"', '"channel_id"', '"external_id"', "external", "channel"):
        assert banned not in js.lower(), f"User UI 出现越界概念：{banned}"
    assert "external" not in _read(USERS_HTML).lower()


# ================================================================== D. status / metadata 冻结合同


def test_d1_status_form_only_management_states() -> None:
    """status 仅管理态：编辑仅 {不修改, active, inactive}；创建固定 active
    （disabled，后端创建合同无 status 字段）。error 不出现于任何表单选项。"""
    for js in (USERS_JS, IDENTITIES_JS):
        src = _read(js)
        assert src.count('for (const value of ["active", "inactive"])') == 1
        assert 'option.value = "error"' not in src
    for html in (USERS_HTML, IDENTITIES_HTML):
        assert '<option value="error"' not in _read(html)


def test_d2_metadata_no_sentinel_full_replace() -> None:
    """metadata 刻意不复制 Channel 哨兵：所见即所存；未修改（与回填原文一致）
    → 省略；有修改 → 整段替换（Connection config 同一形态）。"""
    js = _read(IDENTITIES_JS)
    assert "SECRET_PLACEHOLDER" not in js
    assert '"********"' not in js
    assert "JSON.parse" in js and "JSON.stringify" in js
    assert "originalMetadataJson" in js                # 未修改省略（避免无谓整段替换）
    html = _read(IDENTITIES_HTML)
    assert "整段替换" in html


def test_d3_metadata_no_invented_keys() -> None:
    """metadata 是不透明 JSON：identities.js 不预设任何业务键
    （档案信息语义归用户，UI 只做 JSON 对象校验）。"""
    js = _read(IDENTITIES_JS)
    for invented in ("token", "encoding_aes_key", "corpsecret", "corp_id",
                     "agentid", "api_base", "timeout_s"):
        assert f'"{invented}"' not in js, f"UI 预设了 metadata 业务键：{invented}"


def test_d4_metadata_belongs_to_identity_page_only() -> None:
    """metadata 是 Identity 档案字段：User 页不出现该概念。"""
    for path in (USERS_JS, USERS_HTML):
        assert "metadata" not in _read(path).lower(), \
            f"{path.name} 出现 metadata（属 Identity 页）"


# ================================================================== E. 脚本独立


def test_e_each_new_html_loads_only_its_own_script() -> None:
    """新页只加载自身脚本（与 11.13 三页独立同一不变量的扩展）。"""
    mapping = {
        USERS_HTML: "users.js",
        IDENTITIES_HTML: "identities.js",
    }
    all_scripts = ("agents.js", "channels.js", "connections.js",
                   "users.js", "identities.js")
    for html_path, own_script in mapping.items():
        html = _read(html_path)
        assert f'src="{own_script}"' in html, f"{html_path.name} 未加载 {own_script}"
        for other in all_scripts:
            if other != own_script:
                assert f'src="{other}"' not in html, \
                    f"{html_path.name} 不应加载兄弟页面脚本 {other}"


def test_e_no_module_coupling_in_new_scripts() -> None:
    """脚本独立：只允许 import 共享 HTTP 层与统一顶栏（M7-AUI-9 模块化裁定）；
    require / 兄弟页面脚本 / 相对路径引用仍然是零容忍（11.13 E 同一纪律）。"""
    allowed_imports = {
        'import { request } from "/shared/http.js";',
        'import "/nav.js";',
    }
    for js in (USERS_JS, IDENTITIES_JS):
        src = _read(js)
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith("import "):
                assert stripped in allowed_imports, \
                    f"{js.name} 出现越界 import：{stripped}"
        assert "require(" not in src, f"{js.name} 不应 require 其它模块"
        assert "../" not in src, f"{js.name} 不应引用兄弟页面脚本"


# ================================================================== F. 不做清单（零出现）


def test_f_no_forbidden_features() -> None:
    """不做清单在 User/Identity UI 源码中零出现：硬删除、Identity Linking 入口、
    Health / Routing / Retry / Capability / Permission / Vault / Graph 等。"""
    for path in (USERS_HTML, USERS_JS, IDENTITIES_HTML, IDENTITIES_JS):
        src = _read(path).lower()
        for banned in ("health", "failover", "routing", "retry", "vault",
                       "rbac", "webhook", "capability", "permission",
                       "graph", "ping", "delete", "linking", "merge"):
            assert banned not in src, f"{path.name} 出现越界概念：{banned}"
    # 中文「删除」只约束页面文案
    for html in (USERS_HTML, IDENTITIES_HTML):
        assert "删除" not in _read(html)
