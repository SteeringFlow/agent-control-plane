"""Step 11.7 —— Channel Management Vertical Slice Audit（审计步，生产代码 0 行）。

清点 Channel 纵向切片各层真实状态，并冻结用户三问的裁决边界：
- ① Channel type：自由字符串（无受控集合）；受控集合应由未来 Adapter Registry 决定，
  第一版真实集合 = {wecom}；现在不枚举未来平台。
- ② Channel config：真实密键来源 = wecom.py；密键读时脱敏、非密键可返回；
  Update 必须 merge 保留密键（当前整段替换 = 待下游 API 修正的缺口）。
- ③ Channel credential 边界：Channel 无 credential_ref 字段；凭据住 config；
  11.7 不发明任何 credential 字段。

测试风格：能力断言 / 缺口存在性断言 / 三问裁决断言 / 冻结式分类断言。
详见 docs/step-11.7-channel-management-vertical-slice-audit.md。
"""

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"
REPO_ROOT = BACKEND.parent
FRONTEND = REPO_ROOT / "frontend"
DOC = REPO_ROOT / "docs" / "step-11.7-channel-management-vertical-slice-audit.md"

DOMAIN = APP / "domain" / "channel.py"
ORM = APP / "persistence" / "models" / "channel.py"
PORT = APP / "repositories" / "channel.py"
ADAPTER_IMPL = APP / "persistence" / "repositories" / "channel.py"
SERVICE = APP / "application" / "channels.py"
MANAGEMENT_API = APP / "api" / "management"
SCHEMAS = APP / "schemas"
MAIN = APP / "main.py"
AGENT_DOMAIN = APP / "domain" / "agent.py"
CONNECTION_DOMAIN = APP / "domain" / "connection.py"
CHANNEL_ADAPTERS_DIR = APP / "adapters" / "channels"
WECOM_ADAPTER = APP / "adapters" / "channels" / "wecom.py"
RUNTIME_CHANNEL_API = APP / "api" / "runtime" / "channels.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _class_field_names(src: str) -> dict[str, set[str]]:
    tree = ast.parse(src, filename="x")
    names: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            names[node.name] = {
                n.target.id for n in node.body
                if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)
            }
    return names


# ================================================================== A. Channel 后端已完整（能力断言）


def test_a1_channel_domain_field_surface() -> None:
    """Domain 字段面 == {name,type,id,status,config}；无 credential_ref；
    无 created_at/updated_at（与 Agent 不同，后者领域已含审计时间戳）。"""
    src = _read(DOMAIN)
    fields = _class_field_names(src).get("Channel", set())
    assert fields == {"name", "type", "id", "status", "config"}, fields
    assert "credential_ref" not in fields
    assert "created_at" not in fields and "updated_at" not in fields


def test_a2_channel_orm_column_surface() -> None:
    """ORM：id/name/type/status/config(JSON) + created_at/updated_at；无 credential_ref 列。"""
    src = _read(ORM)
    assert 'mapped_column(JSON, nullable=True)' in src  # config 为 JSON
    assert "created_at" in src and "updated_at" in src
    assert "credential_ref" not in src


def test_a3_channel_repository_port_method_surface() -> None:
    """Port 方法面 == {get_by_id, create, update, find_by_type_and_name, list_channels}
    （11.8 翻转：+ list_channels）；仍无 delete（DROP 裁决维持）。"""
    src = _read(PORT)
    for method in ("get_by_id", "create", "update", "find_by_type_and_name",
                   "list_channels"):
        assert f"def {method}(" in src
    assert "def delete(" not in src


def test_a4_channel_service_is_complete() -> None:
    """Service 提供 create/get/update + 3 错误类型；type 仅非空校验；无 delete。"""
    src = _read(SERVICE)
    for method in ("create_channel", "get_channel", "update_channel"):
        assert f"def {method}(" in src
    for err in ("ChannelNotFoundError", "DuplicateChannelError",
                "InvalidChannelInputError"):
        assert err in src
    # type 仅非空校验，没有受控集合成员判定
    assert '_require_non_blank(input.type, "type")' in src
    assert "def delete" not in src
    # Service 不为 Channel 定义 credential_ref 字段（无 credential_ref: 注解）
    assert "credential_ref:" not in src


# ================================================================== B. Channel API/UI 缺口（存在性断言）


def test_b1_channel_api_and_schema_missing() -> None:
    """11.8 翻转：api/management 已落地 channels.py、schemas 已有 channels.py；
    11.11 再翻转：connections.py 并列在场。"""
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
    assert "channels.py" in schema_children, "11.8：Channel schema 应已存在"
    assert set(schema_children) == {"agents.py", "auth.py", "bootstrap_arms.py",
                                    "channels.py",
                                    "connection_conversations.py",
                                    "connections.py",
                                    "health.py", "identities.py", "interactions.py",
                                    "users.py"}, \
        schema_children  # Cloud-0.1.1-C: + auth.py


def test_b2_channel_ui_missing() -> None:
    """11.9 翻转：Channel UI 已落地（channels.html + channels.js，复用 styles.css）；
    Agent UI 四件套仍在。

    Cloud-0.1.4.1 追加 connect-channel.html——Connect Channel Golden Path 的
    **用户任务编排页**，不是第二套 Channel 管理 UI。

    M7-AUI-8.2（Advanced Surface Reduction）：该页面收口为**兼容跳转页**，
    旧脚本 `connect-channel.js` 已删除（页面不再加载任何脚本）。
    因此 Channel 命名文件面 = 管理页两件 + 兼容页一件。
    """
    assert FRONTEND.exists()
    for name in ("agents.html", "agents.js", "styles.css", "README.md"):
        assert (FRONTEND / name).is_file()
    ch_files = sorted(p.name for p in FRONTEND.rglob("*")
                      if p.is_file() and "channel" in p.name.lower())
    assert ch_files == ["channels.html", "channels.js",
                        "connect-channel.html"], \
        f"Channel UI 文件面漂移：{ch_files}"


# ================================================================== C. ① type：自由字符串 + 受控集合来源


def test_c1_type_set_is_controlled_at_service_layer() -> None:
    """15.2.3 裁决（11.7 deferred decision 到期）：Domain `type: str` 仍是自由
    字符串，但 Application Service 新增受控集合
    ALLOWED_CHANNEL_TYPES = ("wecom", "local") —— 第二个 Channel Adapter（local）
    出现时收口；不建 Python enum。"""
    domain_src = _read(DOMAIN)
    assert "type: str" in domain_src
    service_src = _read(SERVICE)
    assert "ALLOWED_CHANNEL_TYPES" in service_src
    assert '"wecom"' in service_src and '"local"' in service_src
    assert "def _require_allowed_type" in service_src


def test_c2_channel_adapters_are_wecom_and_local() -> None:
    """adapters/channels = wecom.py + local.py（真实 Adapter）+ registry.py
    （15.2.3 type→Adapter 分派）；无 feishu/dingtalk/webhook。"""
    children = [p.name for p in CHANNEL_ADAPTERS_DIR.iterdir()
                if p.is_file() and p.suffix == ".py"]
    assert set(children) == {
        "__init__.py", "wecom.py", "local.py", "registry.py", "wecom_bot.py"}, (
        f"Channel Adapter 集合漂移：{children}"
    )


def test_c3_inbound_routing_hardcodes_wecom_not_dispatch_by_type() -> None:
    """入站路由硬编码 wecom、不读 channel.type 派发 —— 印证 type 当前非 Adapter 派发键。"""
    src = _read(RUNTIME_CHANNEL_API)
    assert "/{channel_id}/wecom/webhook" in src
    assert "WeComAdapter" in src
    # 没有「按 channel.type 选择 Adapter」的逻辑
    assert "channel.type" not in src
    assert "type == " not in src


# ================================================================== D. ② config 真实字段 + ③ credential 边界


def test_d1_wecom_config_keys_are_source_of_truth() -> None:
    """wecom.py 读取的 config 真实键（冻结事实来源）：
    入站 token/encoding_aes_key/corp_id；出站 corpsecret/agentid/api_base/timeout_s。"""
    src = _read(WECOM_ADAPTER)
    for key in ("token", "encoding_aes_key", "corp_id",
                "corpsecret", "agentid", "api_base", "timeout_s"):
        assert key in src, f"wecom Adapter 未引用 config 键 {key}（与审计文档不符）"


def test_d2_channel_has_no_credential_ref() -> None:
    """Channel（domain + ORM）无 credential_ref；对照 Agent/Connection 有。
    冻结「11.7 不发明 Channel credential 字段」的边界。"""
    assert "credential_ref" not in _read(DOMAIN)
    assert "credential_ref" not in _read(ORM)
    # 对照：Agent / Connection 确有 credential_ref
    assert "credential_ref" in _read(AGENT_DOMAIN)
    assert "credential_ref" in _read(CONNECTION_DOMAIN)


def test_d3_config_update_is_merge_not_whole_replace() -> None:
    """11.8 已闭合 11.7 记录的缺口：config 更新 = load → merge incoming → persist
    （提供的键覆写、未提供的键——尤其密键——保留），不再整段替换。"""
    src = _read(SERVICE)
    assert "{**" in src, "config 更新应实现 merge 语义（11.8 合同 #3）"
    assert "config=change.config" not in src, "config 不得整段替换"


# ================================================================== E. 冻结式分类断言（防漂移）


def test_e1_readiness_ledger_is_written_down() -> None:
    """闭环就绪度结论 + 三问裁决 + 下游 API 切片建议成文（防漂移）。"""
    assert DOC.exists(), f"缺少纵向切片审计文档：{DOC}"
    text = DOC.read_text(encoding="utf-8")
    for token in (
        "受控集合",
        "wecom",
        "corpsecret",
        "token",
        "encoding_aes_key",
        "credential_ref",
        "脱敏",
        "合并",
        "merge",
        "DROP",
        "API",
        "UI",
        "Channel",
        "引用",
        "Adapter Registry",
    ):
        assert token in text, f"审计文档缺少 token：{token}"
