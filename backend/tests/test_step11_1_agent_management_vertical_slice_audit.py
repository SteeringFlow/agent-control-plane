"""Step 11.1 —— Agent Management Vertical Slice Audit
（审计步，生产代码 0 行改动）。

清点 Agent 纵向切片各层真实状态，并裁决用户检查清单：
- 后端链路（Domain/ORM/Repository/Application Service）已完整（含 Create/Get/
  Update + 校验 + 去重守卫 + 三错误类型）——不是要新建，而是要被消费；
- 缺失：API Schema（Pydantic）/ HTTP API（FastAPI 路由，api/management 仅
  __init__ 空壳）/ Frontend（占位）；List / Delete 用例缺失；
- 政策缺口（待 API 切片收口）：credential_ref「引用入、引用不出」、
  status 受控集合、type 受控集合、name 补 DB UNIQUE、endpoint 最小 URL 校验；
- 真实 frontend consumer：否（与 10.9 G10 WATCH 互相印证）。

测试风格：能力断言 / 当前能力断言 / 缺口存在性断言 / 分类断言。详见
docs/step-11.1-agent-management-vertical-slice-audit.md。
"""

from dataclasses import fields
from pathlib import Path

import ast
import pytest

from tests.oss_export_boundary import oss_ui_required

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"
REPO_ROOT = BACKEND.parent
FRONTEND = REPO_ROOT / "frontend"
DOC = REPO_ROOT / "docs" / "step-11.1-agent-management-vertical-slice-audit.md"

DOMAIN = APP / "domain" / "agent.py"
ORM = APP / "persistence" / "models" / "agent.py"
PORT = APP / "repositories" / "agent.py"
ADAPTER = APP / "persistence" / "repositories" / "agent.py"
SERVICE = APP / "application" / "agents.py"
MANAGEMENT_API = APP / "api" / "management"
MAIN = APP / "main.py"


def _py_files(root: Path):
    return sorted(p for p in root.rglob("*.py"))


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ================================================================== A. 后端链路已完整（能力断言）


def test_a1_agent_domain_field_surface() -> None:
    """Agent Domain 字段面 == 9 字段：7 业务字段 + 11.3 补的 2 个只读审计
    时间戳回显（created_at/updated_at，与 Session 实体同一先例）；frozen。"""
    from app.domain.agent import Agent
    assert {f.name for f in fields(Agent)} == {
        "id", "name", "type", "endpoint", "status", "config", "credential_ref",
        "created_at", "updated_at"}
    assert Agent.__dataclass_params__.frozen


def test_a2_orm_has_audit_timestamps() -> None:
    """ORM 含 created_at/updated_at（审计/可观测）；且列集合恰好为已知集合——
    证明没有额外的 secret/明文承载列（credential_ref 只是 String 引用）。"""
    src = _read(ORM)
    assert "created_at" in src and "updated_at" in src
    assert "credential_ref" in src
    columns = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            columns.add(node.target.id)
    assert columns == {
        "id", "name", "type", "endpoint", "status", "config",
        "credential_ref", "created_at", "updated_at", "tenant_id"}


def test_a3_repository_port_method_surface() -> None:
    """Repository Port 方法面 == {get_by_id, create, update, find_by_name,
    list_agents}（11.3 补 List 用例）；仍无 delete_agent（DROP 裁决维持）。"""
    src = _read(PORT)
    methods = [n for n in ("get_by_id", "create", "update", "find_by_name",
                           "list_agents", "delete_agent")
               if f"def {n}(" in src]
    assert set(methods) == {"get_by_id", "create", "update", "find_by_name",
                            "list_agents"}


def test_a4_application_service_is_complete() -> None:
    """Application Service 提供 create_agent/get_agent/update_agent + 输入契约
    + 三错误类型（AgentNotFoundError/DuplicateAgentError/InvalidAgentInputError）。"""
    src = _read(SERVICE)
    for method in ("create_agent", "get_agent", "update_agent"):
        assert f"def {method}(" in src
    for cls in ("CreateAgent", "UpdateAgent"):
        assert f"class {cls}" in src
    from app.application import errors
    for err in ("AgentNotFoundError", "DuplicateAgentError",
                "InvalidAgentInputError"):
        assert hasattr(errors, err)


# ================================================================== B. 空壳 / 缺失（存在性断言）


def test_b1_management_api_has_agents_router() -> None:
    """11.3：api/management/ 不再是空壳——已落地 agents.py（HTTP API）。"""
    children = [p.name for p in MANAGEMENT_API.iterdir() if p.name != "__init__.py"]
    assert "agents.py" in children


def test_b2_http_api_registers_health_and_agents() -> None:
    """11.3：main.py 注册 health_router + agents 管理 router
    （组合根绑定生产 UoW；测试经 build_agents_api 注入内存库实例）。"""
    src = _read(MAIN)
    assert "health_router" in src
    assert "include_router" in src
    assert "build_agents_api" in src
    assert "include_router(build_agents_api" in src


@oss_ui_required()
def test_b3_frontend_now_has_real_agent_ui() -> None:
    """11.5：frontend 不再是占位——Agent Management UI 已落地
    （agents.html / agents.js / styles.css，无框架无构建的 vanilla 实现）。"""
    assert FRONTEND.exists(), "frontend 目录缺失"
    for name in ("agents.html", "agents.js", "styles.css"):
        assert (FRONTEND / name).is_file(), f"frontend 缺少 {name}"


def test_b4_list_now_exists_delete_still_missing() -> None:
    """11.3：List 用例已补（port+service 均有 list_agents）；
    Delete 维持 DROP 裁决（均无 delete_agent）。"""
    for path in (PORT, SERVICE):
        src = _read(path)
        assert "list_agents" in src, f"{path.name} 缺 list_agents"
        assert "delete_agent" not in src, f"{path.name} 出现 delete_agent"


# ================================================================== C. 字段策略（credential_ref）


def test_c1_credential_ref_reference_only_write_in_read_out() -> None:
    """credential_ref 为引用字段（无明文/secret 类型）；11.3 落实「引用入、
    引用不出」：请求模型（AgentCreate/AgentUpdate）含 credential_ref 字段，
    响应模型（AgentResponse/AgentListResponse）的**字段面**绝不含它
    （AST 解析类字段，不受 docstring 文字影响）。"""
    domain_src = _read(DOMAIN)
    assert "credential_ref" in domain_src
    assert "secret" in domain_src.lower()      # 注释承诺「指向外部 Secret 存储」
    schemas_path = APP / "schemas" / "agents.py"
    schemas_src = _read(schemas_path)
    assert "credential_ref" in schemas_src     # 写路径接收引用
    tree = ast.parse(schemas_src, filename=str(schemas_path))
    field_names: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            names = {
                n.target.id for n in node.body
                if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)
            }
            field_names[node.name] = names
    assert "credential_ref" in field_names["AgentCreate"]
    assert "credential_ref" in field_names["AgentUpdate"]
    assert "credential_ref" not in field_names["AgentResponse"]    # 引用不出
    assert "credential_ref" not in field_names["AgentListResponse"]


# ================================================================== D. 政策缺口事实（源码断言）


def test_d1_policies_now_enforced_in_service() -> None:
    """11.2 留下的政策缺口已随 11.3 收口（源码断言）：
    - type / status：服务层白名单（ALLOWED_AGENT_TYPES / ALLOWED_MANAGEMENT_STATUSES）；
    - endpoint：urlsplit 最小 URL 校验；
    - name：仍无 DB UNIQUE（维持 11.2 裁决：migration 不在 11.3 偷偷扩大）。"""
    domain_src = _read(DOMAIN)
    assert "type: str" in domain_src                       # 领域保持裸 str
    assert "Literal" not in domain_src and "Enum" not in domain_src

    svc_src = _read(SERVICE)
    assert "ALLOWED_AGENT_TYPES" in svc_src
    assert "ALLOWED_MANAGEMENT_STATUSES" in svc_src
    assert 'partition("://")' in svc_src       # 纯字符串最小 URL 校验（不用 urllib）

    orm_src = _read(ORM)
    assert "UniqueConstraint" not in orm_src               # name 无 DB UNIQUE
    assert "unique=True" not in orm_src


# ================================================================== E. 闭环就绪度结论（分类断言）


def test_e1_readiness_ledger_is_written_down() -> None:
    """闭环就绪度结论 + 最小 API 切片建议成文（防漂移）。"""
    assert DOC.exists(), f"缺少纵向切片审计文档：{DOC}"
    text = DOC.read_text(encoding="utf-8")
    for token in (
        "Domain → ORM → Repository → Application Service",   # 后端已就绪
        "已存在且功能完整",
        "空壳",
        "缺失",
        "list_agents",
        "DROP",
        "引用入、引用不出",
        "status",
        "运行态",
        "管理态",
        "type",
        "受控集合",
        "name",
        "UNIQUE",
        "endpoint",
        "URL 格式校验",
        "真实 frontend consumer",
        "G10 WATCH",
        "最小 API 切片建议",
    ):
        assert token in text, f"纵向切片审计文档缺少 token：{token}"
