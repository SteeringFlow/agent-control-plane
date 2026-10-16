"""Step 11.6 —— Connection Management Vertical Slice Audit（审计步，生产代码 0 行）。

冻结当前真实分层状态，并裁决「下一刀做 Channel 还是 Connection」：
- Connection 后端（Domain/ORM/Repository/Service）已完整，但 **API/UI 缺失**；
- Channel 后端完整，但 **无管理 API、无 UI、生产零样本**；
- Connection.create 强依赖已存在的 Channel（ChannelNotFoundError/AgentNotFoundError）
  → 生产环境 channels 为空 → 真实闭环必须先做 Channel Management。

测试风格：能力断言 / 缺口存在性断言 / 依赖事实断言 / 分类断言。
详见 docs/step-11.6-connection-management-vertical-slice-audit.md。
"""

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"
REPO_ROOT = BACKEND.parent
FRONTEND = REPO_ROOT / "frontend"
DOC = REPO_ROOT / "docs" / "step-11.6-connection-management-vertical-slice-audit.md"

DOMAIN = APP / "domain" / "connection.py"
ORM = APP / "persistence" / "models" / "connection.py"
PORT = APP / "repositories" / "connection.py"
ADAPTER = APP / "persistence" / "repositories" / "connection.py"
SERVICE = APP / "application" / "connections.py"
MANAGEMENT_API = APP / "api" / "management"
SCHEMAS = APP / "schemas"
MAIN = APP / "main.py"
CHANNEL_APP = APP / "application" / "channels.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _class_field_names(src: str) -> dict[str, set[str]]:
    tree = ast.parse(src, filename=str(path := "x"))
    names: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            names[node.name] = {
                n.target.id for n in node.body
                if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)
            }
    return names


# ================================================================== A. Connection 后端已完整（能力断言）


def test_a1_connection_domain_field_surface() -> None:
    """Connection Domain 字段面：绑定骨架(channel_id,agent_id)+可变字段+审计时间戳。"""
    src = _read(DOMAIN)
    assert "channel_id" in src and "agent_id" in src and "name" in src
    assert "credential_ref" in src
    assert "UniqueConstraint" not in src  # 约束在 ORM 层，不在 domain


def test_a2_connection_orm_has_unique_and_fks() -> None:
    """ORM：UNIQUE(channel_id, agent_id) + FK→channels.id + FK→agents.id +
    credential_ref 参考 + config JSON + 审计时间戳。"""
    src = _read(ORM)
    assert '"tenant_id", "channel_id", "agent_id"' in src
    assert "uq_connections_tenant_channel_agent_id" in src
    assert 'ForeignKey("channels.id")' in src
    assert 'ForeignKey("agents.id")' in src
    assert "credential_ref" in src and "config" in src
    assert "created_at" in src and "updated_at" in src


def test_a3_connection_repository_port_method_surface() -> None:
    """Port 方法面 == {get_by_id, create, update, find_by_channel_and_agent,
    find_active_by_channel}；无 delete（DROP 裁决维持）。"""
    src = _read(PORT)
    for method in ("get_by_id", "create", "update",
                   "find_by_channel_and_agent", "find_active_by_channel"):
        assert f"def {method}(" in src
    assert "delete" not in src


def test_a4_connection_service_is_complete() -> None:
    """Service 提供 create/get/update + 5 错误类型 + 三存在性预检；无删除。"""
    src = _read(SERVICE)
    for method in ("create_connection", "get_connection", "update_connection"):
        assert f"def {method}(" in src
    for err in ("ChannelNotFoundError", "AgentNotFoundError",
                "DuplicateConnectionError", "ConnectionNotFoundError",
                "InvalidConnectionInputError"):
        assert err in src
    assert "delete" not in src


# ================================================================== B. Connection API/UI 缺口（存在性断言）


def test_b1_connection_api_surface() -> None:
    """11.11 翻转：Connection 管理 API 已落地——api/management/connections.py +
    schemas/connections.py 与 agents/channels 并列（11.6 当时冻结的缺口已闭合）。"""
    children = [p.name for p in MANAGEMENT_API.iterdir()
                if p.is_file() and p.suffix == ".py" and p.name != "__init__.py"]
    # 12.4：users.py / identities.py 并列在场；13.1：interactions.py（Observability 只读）
    # M6.6-C3 / ADR-12: + bootstrap_arms.py（Verify Connection / Arm 端点）
    assert sorted(children) == ["agents.py", "bootstrap_arms.py",
                                "channels.py", "connection_conversations.py",
                                "connections.py",
                                "identities.py", "interactions.py", "users.py"], \
        f"management API 面漂移：{children}"
    schema_children = [p.name for p in SCHEMAS.iterdir()
                       if p.is_file() and p.suffix == ".py" and p.name != "__init__.py"]
    # M6.6-C3 / ADR-12: + bootstrap_arms.py（Arm 响应契约）
    assert set(schema_children) == {
        "agents.py", "auth.py", "bootstrap_arms.py", "channels.py",
        "connection_conversations.py",
        "connections.py", "health.py",
        "identities.py", "interactions.py", "users.py"}, schema_children  # C: +auth.py


def test_b2_connection_ui_surface() -> None:
    """11.12 翻转：Connection UI 已落地（connections.html + connections.js，
    复用 styles.css；Agent UI 四件套仍在）。"""
    assert FRONTEND.exists()
    for name in ("agents.html", "agents.js", "styles.css", "README.md"):
        assert (FRONTEND / name).is_file()
    conn_files = sorted(p.name for p in FRONTEND.rglob("*")
                        if p.is_file() and "connection" in p.name.lower())
    assert conn_files == ["connections.html", "connections.js"], \
        f"Connection UI 文件面漂移：{conn_files}"


# ================================================================== C. Channel 侧缺口（Connection 的前置）


def test_c1_channel_app_service_exists_but_no_management_api() -> None:
    """Channel 应用服务存在（create_channel）。11.8 翻转：管理 API 已落地并装配
    （api/management/channels.py + main.py build_channels_api）；frontend 仍无
    channel UI（UI 缺口保留）。"""
    ch_src = _read(CHANNEL_APP)
    assert "class ChannelApplicationService" in ch_src
    assert "def create_channel" in ch_src

    main_src = _read(MAIN)
    assert "build_channels_api" in main_src, "11.8：main.py 应装配 channel 管理 API"

    frontend_has_channel = any(
        "channel" in p.name.lower()
        for p in FRONTEND.rglob("*") if p.is_file()
    )
    # 11.9 翻转：Channel UI 已落地（channels.html / channels.js）
    assert frontend_has_channel, "11.9：frontend 应已有 channel UI"


# ================================================================== D. 依赖事实（Connection 强依赖已存在 Channel）


def test_d1_create_connection_requires_existing_channel() -> None:
    """create_connection 必须先 `uow.channels.get_by_id` 并抛 ChannelNotFoundError
    —— 证明无 Channel 即无法创建 Connection（生产闭环必须先做 Channel Management）。"""
    src = _read(SERVICE)
    assert "uow.channels.get_by_id" in src
    assert "ChannelNotFoundError(input.channel_id)" in src
    assert "uow.agents.get_by_id" in src
    assert "AgentNotFoundError(input.agent_id)" in src


def test_d2_no_production_seed_for_channels() -> None:
    """生产代码不播种 Channel（main 无 seed/insert/init_db/create_all 调用）；
    channels 在生产库为空 —— 真实闭环必须先做 Channel Management。"""
    main_src = _read(MAIN).lower()
    assert "seed" not in main_src
    assert "create_all" not in main_src
    assert "init_db" not in main_src


# ================================================================== E. 冻结式分类断言（防漂移）


def test_e1_readiness_ledger_is_written_down() -> None:
    """闭环就绪度结论 + 下一刀裁决成文（防漂移）。"""
    assert DOC.exists(), f"缺少纵向切片审计文档：{DOC}"
    text = DOC.read_text(encoding="utf-8")
    for token in (
        "Connection 后端",
        "API",
        "UI",
        "Channel",
        "下一刀",
        "Channel Management",
        "依赖",
        "ChannelNotFoundError",
        "DROP",
        "引用",
        "唯一约束",
        "管理态",
    ):
        assert token in text, f"审计文档缺少 token：{token}"
