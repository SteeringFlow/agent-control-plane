"""M7-AUI-10.6.7 —— Duplicate WeCom Bot Channel Guard 实现验收。

按 M7-AUI-10_6_6_DUPLICATE_BOT_CHANNEL_GUARD_AUDIT.md §6 的最小方案锁定：

- 同一 tenant 范围内，相同 bot_id 不允许第二条 active `wecom_bot` Channel
  （创建 / 编辑 bot_id / inactive 重新启用三个入口统一守卫）→ 409；
- 冲突错误为结构化 detail `{code: "duplicate_bot_channel", message}`，前端按
  code 区分 Bot 身份冲突与普通同名冲突；
- 冲突信息只含冲突渠道 name / id，绝不含 bot_id / bot_secret / config；
- 跨租户同 bot_id 保持 10.6.6 的「待决策」状态（本轮不实施跨租户策略）；
- 应用级守卫保留 TOCTOU 残余风险（无 DB 唯一索引，本轮不加迁移）。

测试全部使用独立内存 SQLite，不触碰真实租户数据。
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from tests.oss_export_boundary import oss_ui_required

from app.api.management.channels import build_channels_api
from app.application.channels import ChannelApplicationService
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = Path(__file__).resolve().parents[1]
ERRORS = BACKEND / "app" / "application" / "errors.py"
SERVICE = BACKEND / "app" / "application" / "channels.py"
API = BACKEND / "app" / "api" / "management" / "channels.py"
CHANNELS_JS = REPO_ROOT / "frontend" / "channels.js"
HTTP_JS = REPO_ROOT / "frontend" / "shared" / "http.js"

_NODE = shutil.which("node")
needs_node = pytest.mark.skipif(_NODE is None, reason="node 不可用（行为级验收需要 Node）")

_BOT_CONFIG = {"bot_id": "bot-a", "bot_secret": "secret-do-not-leak"}


def _factory(db_engine) -> sessionmaker:
    return sessionmaker(bind=db_engine, class_=Session, expire_on_commit=False)


def _service(db_engine) -> ChannelApplicationService:
    return ChannelApplicationService(
        uow_factory=lambda: SqlAlchemyUnitOfWork(session_factory=_factory(db_engine))
    )


def _tenant_service(db_engine, tenant_id: int) -> ChannelApplicationService:
    return ChannelApplicationService(
        uow_factory=lambda: SqlAlchemyUnitOfWork(
            session_factory=_factory(db_engine), tenant_id=tenant_id)
    )


@pytest.fixture()
def client(db_engine):
    test_app = FastAPI()
    test_app.include_router(build_channels_api(_service(db_engine)))
    return TestClient(test_app)


def _create(client, name: str, config: dict | None = None,
            channel_type: str = "wecom_bot", expect: int = 201) -> dict:
    payload = {"name": name, "type": channel_type}
    if config is not None:
        payload["config"] = config
    resp = client.post("/api/channels", json=payload)
    assert resp.status_code == expect, resp.text
    return resp.json()


# ================================================================== A. 创建入口


def test_a1_create_duplicate_bot_id_same_tenant_409(client) -> None:
    """同租户创建相同 bot_id 的第二条 active wecom_bot → 409（结构化 detail）。"""
    _create(client, "企业微信 11", dict(_BOT_CONFIG))
    resp = client.post("/api/channels", json={
        "name": "企业微信 12", "type": "wecom_bot",
        "config": {"bot_id": "bot-a", "bot_secret": "another-secret"}})
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert isinstance(detail, dict) and detail["code"] == "duplicate_bot_channel"
    assert "企业微信 11" in detail["message"] and "#1" in detail["message"]


def test_a2_create_different_bot_id_succeeds(client) -> None:
    """不同 bot_id 的 wecom_bot 渠道正常创建（回归 HD1 语义）。"""
    first = _create(client, "bot-a 渠道", {"bot_id": "bot-a", "bot_secret": "s1"})
    second = _create(client, "bot-b 渠道", {"bot_id": "bot-b", "bot_secret": "s2"})
    assert first["id"] != second["id"]
    assert second["status"] == "active"


def test_a3_create_duplicate_ignores_whitespace_normalization(client) -> None:
    """bot_id 规范化（strip）后比对：' bot-a ' 视为同一身份。"""
    _create(client, "c1", dict(_BOT_CONFIG))
    resp = client.post("/api/channels", json={
        "name": "c2", "type": "wecom_bot",
        "config": {"bot_id": "  bot-a  ", "bot_secret": "s"}})
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "duplicate_bot_channel"


def test_a4_create_without_reliable_bot_id_keeps_existing_semantics(client) -> None:
    """bot_id 缺失 / 非字符串 / 空白 → 无法可靠提取身份 → 保持既有语义
    （不新增 422、不伪造身份；config 完整性由运行时 fail-closed 把关）。"""
    cases = (
        ("missing", None),
        ("only-secret", {"bot_secret": "s"}),
        ("non-string", {"bot_id": 123}),
        ("blank", {"bot_id": "   "}),
    )
    for suffix, config in cases:
        body = _create(client, f"no-identity-{suffix}", config)
        assert body["status"] == "active"


# ================================================================== B. 更新入口（编辑 bot_id）


def test_b1_update_bot_id_to_conflicting_identity_409(client) -> None:
    """把 B 的 bot_id 改成另一 active 渠道的身份 → 409（且 B 未被改坏）。"""
    _create(client, "A", dict(_BOT_CONFIG))
    b = _create(client, "B", {"bot_id": "bot-b", "bot_secret": "s"})
    resp = client.patch(f"/api/channels/{b['id']}",
                        json={"config": {"bot_id": "bot-a"}})
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "duplicate_bot_channel"

    listing = client.get("/api/channels").json()["items"]
    b_row = next(c for c in listing if c["id"] == b["id"])
    assert b_row["config"]["bot_id"] == "bot-b", "冲突被拒后原身份保留（未落库）"


def test_b2_update_own_bot_id_unchanged_is_self_not_conflict(client) -> None:
    """把 bot_id 改成自身当前值（无变化）→ 不因自身被误判 → 200。"""
    a = _create(client, "A", dict(_BOT_CONFIG))
    resp = client.patch(f"/api/channels/{a['id']}",
                        json={"config": {"bot_id": "bot-a"}})
    assert resp.status_code == 200


# ================================================================== C. 重新启用入口


def test_c1_reenable_inactive_without_conflict_succeeds(client) -> None:
    """inactive 渠道在无冲突时重新启用 → 200。"""
    a = _create(client, "A", dict(_BOT_CONFIG))
    assert client.patch(f"/api/channels/{a['id']}",
                        json={"status": "inactive"}).status_code == 200
    resp = client.patch(f"/api/channels/{a['id']}", json={"status": "active"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "active"


def test_c2_inactive_channel_does_not_occupy_identity(client) -> None:
    """inactive 渠道不占用身份：A 停用后，B 可正常使用同一 bot_id。"""
    a = _create(client, "A", dict(_BOT_CONFIG))
    client.patch(f"/api/channels/{a['id']}", json={"status": "inactive"})
    _create(client, "B", dict(_BOT_CONFIG))  # 201：A inactive 不阻挡


def test_c3_reenable_inactive_with_conflict_409(client) -> None:
    """inactive 渠道在另一 active 渠道已占用同一 bot_id 时重新启用 → 409。"""
    _create(client, "A", dict(_BOT_CONFIG))
    b = _create(client, "B", {"bot_id": "bot-b", "bot_secret": "s"})
    # B 停用期间把 bot_id 改成与 A 相同（inactive 不做身份检查，可改）
    assert client.patch(
        f"/api/channels/{b['id']}",
        json={"status": "inactive", "config": {"bot_id": "bot-a"}},
    ).status_code == 200
    resp = client.patch(f"/api/channels/{b['id']}", json={"status": "active"})
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "duplicate_bot_channel"

    # 冲突被拒后 B 保持 inactive（未落库）
    listing = client.get("/api/channels").json()["items"]
    assert next(c for c in listing if c["id"] == b["id"])["status"] == "inactive"


# ================================================================== D. 非身份字段 / 非目标类型不受影响


def test_d1_rename_and_unrelated_config_change_not_flagged(client) -> None:
    """改名 / 改无关 config 键不触发身份检查（不误判、不误伤普通 PATCH）。"""
    a = _create(client, "A", dict(_BOT_CONFIG))
    assert client.patch(f"/api/channels/{a['id']}",
                        json={"name": "A-renamed"}).status_code == 200
    assert client.patch(f"/api/channels/{a['id']}",
                        json={"config": {"ws_url": "ws://example"}}).status_code == 200
    listing = client.get("/api/channels").json()["items"]
    row = next(c for c in listing if c["id"] == a["id"])
    assert row["name"] == "A-renamed"
    assert row["config"]["bot_id"] == "bot-a", "无关 config 变更不影响身份键"


def test_d2_deactivate_stays_allowed(client) -> None:
    """active → inactive 停用路径永不触发身份检查。"""
    a = _create(client, "A", dict(_BOT_CONFIG))
    assert client.patch(f"/api/channels/{a['id']}",
                        json={"status": "inactive"}).status_code == 200


def test_d3_non_wecom_bot_types_unaffected(client) -> None:
    """非 wecom_bot 类型不受新规则影响：wecom / local 同名同名身份均照旧语义。"""
    _create(client, "wecom-main", {"bot_id": "shared-bot", "token": "t"},
            channel_type="wecom")
    second = _create(client, "wecom-second", {"bot_id": "shared-bot", "token": "t"},
                     channel_type="wecom")
    assert second["type"] == "wecom"
    third = _create(client, "local-1", None, channel_type="local")
    assert third["type"] == "local"


# ================================================================== E. 冲突响应安全面


def test_e1_conflict_response_contains_no_secrets_or_config(client) -> None:
    """409 响应体不含 bot_secret、脱敏占位、原始 config 或 bot_id 值。"""
    _create(client, "A", dict(_BOT_CONFIG))
    resp = client.post("/api/channels", json={
        "name": "B", "type": "wecom_bot",
        "config": {"bot_id": "bot-a", "bot_secret": "another-secret"}})
    assert resp.status_code == 409
    raw = resp.text
    for banned in ("secret-do-not-leak", "another-secret", "bot_secret",
                   "********", '"config"', "bot-a"):
        assert banned not in raw, f"409 响应泄漏：{banned}"
    detail = resp.json()["detail"]
    assert set(detail) == {"code", "message"}
    assert "A" in detail["message"]  # 只有冲突渠道的名称/ID


def test_e2_same_name_conflict_keeps_plain_detail(client) -> None:
    """同名冲突维持既有纯字符串 detail（向后兼容，不误包装成 Bot 身份冲突）。"""
    _create(client, "dup", dict(_BOT_CONFIG))
    resp = client.post("/api/channels", json={
        "name": "dup", "type": "wecom_bot", "config": dict(_BOT_CONFIG)})
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert isinstance(detail, str), "同名冲突仍是字符串 detail"
    assert "Channel 已存在" in detail


# ================================================================== F. tenant 作用域


def test_f1_guard_is_tenant_scoped_cross_tenant_policy_untouched(db_engine) -> None:
    """守卫按租户作用域生效；跨租户同 bot_id 保持 10.6.6「待决策」状态
    （本轮不实施跨租户唯一性策略，也不得误伤）。"""
    from app.persistence.models.tenant import TenantModel

    with Session(db_engine) as session:  # FK：tenant_id → tenants.id
        # Tenant #1 已由 create_all 的 after_create 监听自动种子（conftest 基线）。
        session.add(TenantModel(id=2, name="tenant-2"))
        session.commit()

    svc_t1 = _tenant_service(db_engine, tenant_id=1)
    svc_t2 = _tenant_service(db_engine, tenant_id=2)

    svc_t1.create_channel(type("C", (), {"name": "t1-a", "type": "wecom_bot",
                                         "config": dict(_BOT_CONFIG)})())
    # 租户 2 配置同一外部 bot_id：不被本守卫拦截（跨租户策略待决策）
    svc_t2.create_channel(type("C", (), {"name": "t2-a", "type": "wecom_bot",
                                         "config": dict(_BOT_CONFIG)})())
    # 但租户 2 内部第二条同 bot_id → 冲突
    with pytest.raises(Exception) as exc_info:
        svc_t2.create_channel(type("C", (), {"name": "t2-b", "type": "wecom_bot",
                                             "config": dict(_BOT_CONFIG)})())
    assert "t2-a" in str(exc_info.value)


# ================================================================== G. TOCTOU 风险如实登记（静态）


def test_g1_toctou_residual_risk_registered_not_hidden() -> None:
    """应用级守卫保留 TOCTOU 残余风险，错误契约 docstring 如实声明；
    本轮不加 DB 唯一索引、不加迁移。"""
    errors_src = ERRORS.read_text(encoding="utf-8")
    assert "TOCTOU" in errors_src
    assert "没有（本轮也不加）唯一索引" in errors_src
    service_src = SERVICE.read_text(encoding="utf-8")
    assert "DuplicateBotChannelError" in service_src
    # 守卫三入口共用同一集中式函数（create + update 两处调用点）
    assert "_ensure_no_active_bot_identity_conflict" in service_src
    assert service_src.count("_ensure_no_active_bot_identity_conflict(") >= 3


# ================================================================== H. 前端冲突区分


@oss_ui_required()
def test_h1_frontend_static_branching_is_code_based_not_text_matching() -> None:
    """channels.js 按 err.code 分支（不依赖模糊文案匹配）；http.js 透传 code；
    既有同名冲突提示语义保留。"""
    js = CHANNELS_JS.read_text(encoding="utf-8")
    assert 'err.code === "duplicate_bot_channel"' in js
    assert "已经存在同名渠道" in js, "普通同名 409 语义保留"
    http_src = HTTP_JS.read_text(encoding="utf-8")
    assert "err.code = d.code" in http_src
    assert 'd.code === "string"' in http_src or 'typeof d.code === "string"' in http_src


def _fn(code: str, name: str) -> str:
    needle = name if name.startswith("function ") else f"function {name}"
    start = code.index(needle)
    brace = code.index("{", start)
    depth = 0
    for i in range(brace, len(code)):
        if code[i] == "{":
            depth += 1
        elif code[i] == "}":
            depth -= 1
            if depth == 0:
                return code[start:i + 1]
    raise AssertionError(f"函数 {name} 未闭合")


@oss_ui_required()
@needs_node
def test_h2_frontend_user_message_distinguishes_conflict_types() -> None:
    """Node 行为级：userMessage 对三类 409 给出正确文案（Bot 冲突透出后端
    message；无 message 时给复用指引；普通 409 维持同名提示）。"""
    js = CHANNELS_JS.read_text(encoding="utf-8")
    fn_src = _fn(js, "userMessage")
    script = f"{fn_src}\n" + "\n".join(
        f"__out.push(userMessage({json.dumps(case)}))" for case in (
            {"status": 409, "code": "duplicate_bot_channel",
             "message": "HTTP 409: 已存在使用同一企业微信机器人（bot_id）的启用渠道：企业微信 11（#15），请复用该渠道或先停用它。"},
            {"status": 409, "code": "duplicate_bot_channel", "message": "HTTP 409: "},
            {"status": 409},
        ))
    harness = """
    const __out = [];
    %s
    console.log(JSON.stringify(__out));
    """ % script
    out = subprocess.run(
        [_NODE, "-e", harness], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=30)
    assert out.returncode == 0, out.stderr
    lines = [ln for ln in out.stdout.strip().splitlines() if ln.strip()]
    results = eval(lines[-1])  # noqa: S307  受控测试脚本输出
    assert "企业微信 11" in results[0] and "复用" in results[0]
    assert "复用" in results[1] and "企业微信 11" not in results[1]
    assert results[2] == "已经存在同名渠道，请换一个名称。"
