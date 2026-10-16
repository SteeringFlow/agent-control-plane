"""Step 11.10 —— Connection Management Vertical Slice Audit（再审计，生产代码 0 行）。

在 UI/API 联动前冻结 Connection 真实现状，并逐条固化用户对五个焦点的裁决
（`docs/step-11.10-connection-management-vertical-slice-audit.md`）：

- A. Connection 后端仍完整（domain/ORM/Port/Service），且 create 仍 existence-only；
- B. 11.6 阻塞前置已解除：Channel 现已可管理（API 装配 + UI 落地）；
- C. 焦点1：下拉 active-only 是 UI 过滤；后端 create 不引入 status 门禁；
- D. 焦点2：config = Access Path 参数（响应面敏感键脱敏见 16.1.2）；
      credential_ref 写-only（复制 AgentResponse 的「字段不在场」护栏）；
- E. 焦点3：status 纯管理态（无 runtime error 双轴）；
- F. 焦点4：immutable binding（UpdateConnection 无 channel/agent；mapper 不覆写）；
- G. 焦点5：后端就绪 + 唯一缺口 `list_connections`（待 11.11 补齐）；
- H. 冻结式分类断言（防漂移）。

注：不触生产 DB；纯静态 AST / 文件存在性断言 + app.router 装配断言。
"""

import ast
from pathlib import Path

from tests.oss_export_boundary import oss_ui_required

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"
REPO_ROOT = BACKEND.parent
FRONTEND = REPO_ROOT / "frontend"
DOC = REPO_ROOT / "docs" / "step-11.10-connection-management-vertical-slice-audit.md"

DOMAIN = APP / "domain" / "connection.py"
ORM = APP / "persistence" / "models" / "connection.py"
PORT = APP / "repositories" / "connection.py"
ADAPTER = APP / "persistence" / "repositories" / "connection.py"
MAPPER = APP / "persistence" / "mappers" / "connection.py"
SERVICE = APP / "application" / "connections.py"
MANAGEMENT_API = APP / "api" / "management"
SCHEMAS = APP / "schemas"
AGENT_SCHEMA = APP / "schemas" / "agents.py"
MAIN = APP / "main.py"
CHANNEL_APP = APP / "application" / "channels.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _dataclass_field_names(src: str, class_name: str) -> set[str]:
    """取指定 dataclass 的字段名集合（frozen dataclass 的 AnnAssign）。"""
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {
                n.target.id for n in node.body
                if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)
            }
    raise AssertionError(f"{class_name} 未找到")


def _class_field_names(src: str, class_name: str) -> set[str]:
    """取指定（BaseModel）类的字段名集合。"""
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            names: set[str] = set()
            for n in node.body:
                if isinstance(n, (ast.AnnAssign, ast.Assign)):
                    targets = n.targets if isinstance(n, ast.Assign) else [n.target]
                    for t in targets:
                        if isinstance(t, ast.Name):
                            names.add(t.id)
                elif isinstance(n, ast.FunctionDef):
                    names.add(n.name)
            return names
    raise AssertionError(f"{class_name} 未找到")


# ================================================================== A. 后端仍完整（复审计）


def test_a1_connection_backend_still_complete() -> None:
    """domain/ORM/Port/Service 四层能力面与 11.6 一致，无 delete。"""
    port_src = _read(PORT)
    for method in ("get_by_id", "create", "update",
                   "find_by_channel_and_agent", "find_active_by_channel"):
        assert f"def {method}(" in port_src
    assert "delete" not in port_src

    svc_src = _read(SERVICE)
    for method in ("create_connection", "get_connection", "update_connection"):
        assert f"def {method}(" in svc_src
    for err in ("ChannelNotFoundError", "AgentNotFoundError",
                "DuplicateConnectionError", "ConnectionNotFoundError",
                "InvalidConnectionInputError"):
        assert err in svc_src
    assert "delete" not in svc_src


def test_a2_create_is_existence_only_no_status_gate() -> None:
    """create_connection 仅做存在性校验（Channel/Agent），不引入任何 status 门禁
    （status 比较只属于 UI 下拉过滤，不属于后端契约，焦点1）。"""
    src = _read(SERVICE)
    assert "uow.channels.get_by_id" in src
    assert "ChannelNotFoundError(input.channel_id)" in src
    assert "uow.agents.get_by_id" in src
    assert "AgentNotFoundError(input.agent_id)" in src
    # 不应出现基于 status 的创建期拒绝（如 .status == "active" / "inactive" / "error"）
    assert "status" not in src[src.index("def create_connection"):src.index("def get_connection")]


# ================================================================== B. 11.6 阻塞前置已解除（Channel 现已可管理）


@oss_ui_required()
def test_b1_channel_now_managable_production_write_path_exists() -> None:
    """11.6 的 Q8/Q9 阻塞前置（生产无法造 Channel）已解除：
    Channel 管理 API 已在 main.py 装配 + Channel UI 已落地 → 真实 Channel 写入路径存在。"""
    main_src = _read(MAIN)
    assert "build_channels_api" in main_src, "11.8：Channel 管理 API 应在 main 装配"
    assert "ChannelApplicationService" in main_src

    ch_src = _read(CHANNEL_APP)
    assert "def create_channel" in ch_src

    channel_ui = [p.name for p in FRONTEND.rglob("*")
                  if p.is_file() and "channel" in p.name.lower()]
    assert channel_ui, "11.9：Channel UI 应已落地（提供真实 Channel 写入）"


# ================================================================== C. 焦点1：下拉 active-only（UI）vs 后端 existence-only


def test_c1_dropdown_filter_is_ui_concern_backend_stays_existence_only() -> None:
    """后端 create 不比较 status（焦点1 冻结）：status 过滤是 UI 下拉的 UX 选择，
    不应泄漏进 Service / API 契约。"""
    # A.2 已断言 create 段无 status；此处再断言 Service 全文无「按 status 拒绝创建」逻辑
    src = _read(SERVICE)
    assert "status" not in src.split("def create_connection")[1].split("def get_connection")[0]
    # Domain 注释明确 active/inactive 为管理态、非运行态
    dom = _read(DOMAIN)
    assert "active/inactive" in dom


# ================================================================== D. 焦点2：config=Access Path 参数；credential_ref 写-only


def test_d1_connection_config_is_access_path_not_secret() -> None:
    """Connection.config 是 Access Path 参数；domain/orm 层不引入脱敏概念
    （响应面敏感键脱敏在 schemas 层，16.1.2）；
    credential_ref 是参考（String），与 Channel secret 语义无关（焦点2）。"""
    dom = _read(DOMAIN)
    assert "Access Path" in dom
    assert "credential_ref" in dom
    # 刻意不应出现 Channel 那套脱敏/哨兵概念
    assert "sanitize" not in dom
    assert "********" not in dom
    orm = _read(ORM)
    # credential_ref 是 String 参考列，不是 JSON
    assert "credential_ref: Mapped[str | None] = mapped_column(String(255)" in orm


def test_d2_connection_response_must_exclude_credential_ref_like_agent() -> None:
    """焦点2 冻结：11.11 的 ConnectionResponse 必须复制 Agent 的「引用入、引用不出」
    字段面护栏——AgentResponse 已在场排除 credential_ref（写-only）。"""
    agent_schema = _read(AGENT_SCHEMA)
    assert "class AgentResponse(BaseModel):" in agent_schema
    # AgentResponse 类体内不得声明 credential_ref（引用不出）
    assert "credential_ref" not in _class_field_names(agent_schema, "AgentResponse")
    # 写-only 意图在 schema 注释/请求模型中明确
    assert "写-only" in agent_schema


# ================================================================== E. 焦点3：status 纯管理态（无 runtime error 双轴）


def test_e1_connection_status_pure_management_no_error_axis() -> None:
    """Connection 全链路无 runtime error 双轴（焦点3）：
    - domain / service 全文件不得写入 status="error"；
    - ORM 默认 active，无 error 选项。"""
    dom = _read(DOMAIN)
    svc = _read(SERVICE)
    orm = _read(ORM)
    assert 'status="error"' not in dom
    assert 'status="error"' not in svc
    assert 'server_default="active"' in orm
    assert 'status=="active"' in _read(PORT)  # 仅 runtime 读路径用 active 过滤


def test_e2_update_does_not_special_case_error_status() -> None:
    """update_connection 不特判 error（status 为自由管理态字符串，schema 层再白名单）。"""
    src = _read(SERVICE)
    assert '"error"' not in src
    assert "'error'" not in src


# ================================================================== F. 焦点4：immutable binding


def test_f1_update_connection_has_no_binding_fields() -> None:
    """UpdateConnection 无 channel_id / agent_id（绑定不可变，焦点4）。"""
    svc = _read(SERVICE)
    fields = _dataclass_field_names(svc, "UpdateConnection")
    assert "channel_id" not in fields
    assert "agent_id" not in fields
    # 可变字段面只能是管理态字段
    assert fields == {"name", "status", "config", "credential_ref"}


def test_f2_mapper_does_not_overwrite_binding() -> None:
    """apply_to_orm 只覆写可变字段，绝不写 channel_id / agent_id（第二道防线）。"""
    src = _read(MAPPER)
    assert "def apply_to_orm" in src
    # 绑定列在 apply_to_orm 函数体内不应被赋值
    fn = src.split("def apply_to_orm")[1]
    assert "row.channel_id =" not in fn
    assert "row.agent_id =" not in fn


# ================================================================== G. 焦点5：就绪度 + 唯一缺口 list_connections


def test_g1_connection_backend_ready_and_precondition_lifted() -> None:
    """后端就绪 + 11.6 前置解除（焦点5）：Channel 已可管理（见 B.1），
    故 Connection 创建闭环在产品层首次可行。"""
    assert DOC.exists()
    doc = _read(DOC)
    for token in ("阻塞前置", "已解除", "list_connections", "纯管理态",
                  "immutable", "引用", "Access Path"):
        assert token in doc, f"审计文档缺少 token：{token}"


def test_g2_list_connections_gap_closed_by_11_11() -> None:
    """11.11 翻转：唯一后端缺口已补齐——Port/Impl 均有 `list_connections`
    （management 陈列读，含 inactive，与 11.3 list_agents / 11.8 list_channels
    同形）；runtime 读路径 `find_active_by_channel` 必须保留（9.4 最小读）。"""
    port_src = _read(PORT)
    adapter_src = _read(ADAPTER)
    assert "def list_connections(" in port_src
    assert "def list_connections(" in adapter_src
    assert "def find_active_by_channel(" in port_src
    assert "def find_active_by_channel(" in adapter_src


# ================================================================== H. 冻结式分类断言（防漂移）


def test_h_readiness_ledger_is_written_down() -> None:
    """再审计结论 + 五焦点裁决 + 唯一缺口成文（防漂移）。"""
    assert DOC.exists()
    text = _read(DOC)
    for token in (
        "Channel", "可管理", "list_connections", "credential_ref",
        "纯管理态", "immutable", "Access Path", "active",
        "11.11", "无硬删除", "引用",
    ):
        assert token in text, f"审计文档缺少 token：{token}"
