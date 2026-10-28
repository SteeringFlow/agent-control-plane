"""M6.6-F4-A —— Browser First Success Orchestration 验收。

范围（用户冻结 T1–T12）：
- H1  Bootstrap Arm 接入 Connect 流程（Verify 成功 → 才启用；失败 → STOP 绝不启用）；
- H2  First Success 失败可诊断（读既有 failure_stage / failure_code → 用户可执行提示）；
- Q9  ACP 探针 detail 泛化（不泄漏 endpoint / host / port / URL / 凭据 / 异常原文）。

测试纪律（同 F1）：**直接执行真实 `frontend/connect.js` / `frontend/home.js`**——
用 Node `vm` 在桩 DOM + 脚本化 fetch 沙箱里加载源文件，取出真实的 `probe` /
`checkFirstSuccess` / `diagnoseFailure`，喂入脚本化响应并断言**真实调用序列与文案**，
而非静态字符串猜测。缺 Node 时自动 skip（不影响既有 pytest 套件）。
"""

from __future__ import annotations

import ast
import contextlib
import http.server
import json
import os
import shutil
import socketserver
import subprocess
import threading
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.agents.acp import ACPAdapter
from app.adapters.agents.workbuddy import (
    WorkBuddyManagementClient,
    WorkBuddyProvisioner,
)
from app.adapters.channels.wecom import WeComAdapter
from app.adapters.connection_verify import build_connection_verifier
from app.api.management.connections import build_connections_api
from app.application.agents import AgentApplicationService, CreateAgent
from app.application.channels import ChannelApplicationService, CreateChannel
from app.application.connections import ConnectionApplicationService, CreateConnection
from app.domain.agent_probe_snapshot import AgentProbeSnapshot
from app.domain.connection import Connection
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

BACKEND = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND.parent
FRONTEND = REPO_ROOT / "frontend"
CONNECT_JS = FRONTEND / "connect.js"
HOME_JS = FRONTEND / "home.js"
ACP_PY = BACKEND / "app" / "adapters" / "agents" / "acp.py"

_NODE = shutil.which("node")
needs_node = pytest.mark.skipif(
    _NODE is None, reason="node 不可用（F4-A 行为级测试需要 Node）")


# ================================================================== Node 执行沙箱
# 在桩 DOM（稳定 id → 元素单例，便于读取 textContent）+ 脚本化 fetch（按 URL 路由）
# 沙箱里加载真实页面 JS，暴露真实函数供断言。
_JS_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const src = fs.readFileSync(process.env.F4A_JS, "utf8").replace(/^import\s[^;]*;/gm, "");
const mode = process.env.F4A_MODE;

const calls = [];
const els = {};
function fakeEl() {
  return {
    classList: { add() {}, remove() {}, toggle() {} },
    textContent: "", innerHTML: "", href: "", value: "",
    open: false, parentNode: null,
    appendChild() {}, addEventListener() {}, focus() {},
  };
}
function el(id) { if (!els[id]) els[id] = fakeEl(); return els[id]; }

const plan = JSON.parse(process.env.F4A_PLAN || "{}");
function resp(entry) {
  entry = entry || {};
  if (entry.reject) {
    const e = new Error(entry.message || "network error");
    e.status = entry.status || 0;
    return Promise.reject(e);
  }
  const status = entry.status || 200;
  return Promise.resolve({
    ok: status < 400,
    status: status,
    json: function () { return Promise.resolve(entry.body || {}); },
  });
}
const sandbox = {
  console: console,
  document: { getElementById: el, createElement: function () { return fakeEl(); } },
  window: { location: { origin: "http://localhost", replace: function () {} } },
  fetch: function (url, options) {
    const method = (options && options.method) || "GET";
    const u = String(url);
    calls.push({ url: u, method: method });
    if (u.indexOf("bootstrap-arm") >= 0) return resp(plan.arm);
    if (u.indexOf("/verify") >= 0) return resp(plan.verify);
    if (u.indexOf("/interactions") >= 0) {
      return resp({ body: { items: plan.interactions || [] } });
    }
    return resp({ body: { items: [] } });
  },
};
vm.createContext(sandbox);
// M7-AUI-9 模块化：页面脚本 import 共享 HTTP 层——沙箱注入**真实实现**
// （剥 export 后按经典脚本求值），让按 URL 脚本化的 fetch 桩继续穿过
// 真实的 request（15s 超时 / 错误 detail 提取），而不是拿"总是拒绝"的假桩。
const sharedSrc = fs.readFileSync(process.env.F4A_SHARED, "utf8").replace(/^export /gm, "");
vm.runInContext(sharedSrc, sandbox);

const expose = mode === "connect"
  ? "globalThis.__t = { diagnoseFailure: diagnoseFailure, probe: probe, "
    + "checkFirstSuccess: checkFirstSuccess, "
    + "setChosen: function (c) { chosen = c; } };"
  : "globalThis.__t = { diagnoseFailure: diagnoseFailure, "
    + "checkFirstSuccess: checkFirstSuccess, "
    + "setChannels: function (list) { ref.channels = list; } };";
vm.runInContext(src + "\n;" + expose, sandbox);

(async function () {
  const t = sandbox.__t;
  const out = { diagnosis: [], probe: null, firstSuccess: null };
  // M7-AUI-9.2：First Success 严格按当前渠道 scoped——First Success 用例必须
  // 提供渠道上下文（connect = 本次流程的 chosen 渠道；home = 当前渠道），
  // 且注入的 interaction 记录需携带匹配的 channel_id。
  if (process.env.F4A_CHANNEL_ID) {
    const chId = Number(process.env.F4A_CHANNEL_ID);
    if (mode === "connect") {
      t.setChosen({ channelId: chId, channelType: "wecom",
                    channelName: "渠道", agentId: 1 });
    } else {
      t.setChannels([{ id: chId, name: "渠道", type: "wecom_bot", status: "active" }]);
    }
  }
  (JSON.parse(process.env.F4A_DIAG || "[]")).forEach(function (item) {
    out.diagnosis.push(t.diagnoseFailure(item));
  });
  if (mode === "connect" && process.env.F4A_RUN_PROBE === "1") {
    const before = calls.length;
    const ok = await t.probe(7);
    out.probe = {
      result: ok,
      calls: calls.slice(before),
      detail: els["result-detail"].textContent,
      error: els["result-error"].textContent,
    };
  }
  const b2 = calls.length;
  await t.checkFirstSuccess();
  out.firstSuccess = {
    detail: els[mode === "connect" ? "result-detail" : "home-detail"].textContent,
    calls: calls.slice(b2),
  };
  process.stdout.write(JSON.stringify(out));
})().catch(function (e) {
  process.stderr.write(String((e && e.stack) || e));
  process.exit(2);
});
"""


def _run_js(*, js_file: Path, mode: str, plan: dict | None = None,
            diag: list | None = None, run_probe: bool = False) -> dict:
    env = dict(os.environ)
    env["F4A_JS"] = str(js_file)
    env["F4A_SHARED"] = str(FRONTEND / "shared" / "http.js")
    env["F4A_MODE"] = mode
    env["F4A_PLAN"] = json.dumps(plan or {})
    env["F4A_DIAG"] = json.dumps(diag or [])
    env["F4A_RUN_PROBE"] = "1" if run_probe else "0"
    # M7-AUI-9.2：First Success 按当前渠道（id=7）scoped——interaction 数据
    # 需带 channel_id=7 才参与判定。
    env["F4A_CHANNEL_ID"] = "7"
    proc = subprocess.run(
        [_NODE, "-e", _JS_HARNESS], capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env, check=False)
    assert proc.returncode == 0, f"node 执行失败：{proc.stderr}"
    return json.loads(proc.stdout)


def _probe(plan: dict) -> dict:
    return _run_js(js_file=CONNECT_JS, mode="connect", plan=plan,
                   run_probe=True)["probe"]


def _first_success(items: list, *, js_file: Path = CONNECT_JS, mode: str = "connect") -> str:
    out = _run_js(js_file=js_file, mode=mode, plan={"interactions": items})
    return out["firstSuccess"]["detail"]


def _diagnose(item: dict, *, js_file: Path = CONNECT_JS, mode: str = "connect"):
    return _run_js(js_file=js_file, mode=mode, diag=[item])["diagnosis"][0]


def _client_methods(calls: list) -> list[tuple[str, str]]:
    return [(c["method"], c["url"]) for c in calls]


def _has_arm(calls: list) -> bool:
    return any("bootstrap-arm" in c["url"] for c in calls)


# ================================================================== T1 Verify 成功 → 启用


@needs_node
def test_t1_readiness_uses_verify_then_arms() -> None:
    """T1（F3 对齐）：readiness 统一 POST /verify → 成功 → POST /bootstrap-arm。"""
    probe = _probe({"verify": {"body": {"reachable": True, "probe": "wecom"}},
                    "arm": {"status": 201}})
    assert probe["result"] is True
    methods = _client_methods(probe["calls"])
    assert len(methods) == 2, f"应恰好两步（验证 + 启用）：{methods}"
    assert methods[0] == ("POST", "/api/connections/7/verify")
    assert methods[1] == ("POST", "/api/connections/7/bootstrap-arm")


@needs_node
def test_t1_readiness_never_calls_health_for_any_provider() -> None:
    """T1/T4/T5（前端 provider-agnostic）：无论后端返回哪种 provider 结论，
    前端都只调用统一 /verify——绝不回退 /health，也不区分 provider。"""
    for label in ("wecom", "tencent-acp-sse", "minimal"):
        probe = _probe({"verify": {"body": {"reachable": True, "probe": label}},
                        "arm": {"status": 201}})
        methods = _client_methods(probe["calls"])
        assert methods[0] == ("POST", "/api/connections/7/verify"), label
        assert all("/health" not in url for _, url in methods), label


# ================================================================== T2 Verify 失败 → 不启用


@needs_node
@pytest.mark.parametrize("verify_entry", [
    {"body": {"reachable": False, "probe": "minimal"}},   # 探针明确不可达
    {"status": 503, "body": {}},                          # 探针不可用（非连接结论）
    {"status": 500, "body": {}},                          # 请求失败
    {"reject": True, "message": "boom"},                  # 网络异常
])
def test_t2_verify_failure_never_arms(verify_entry: dict) -> None:
    """T2：任何 Verify 未通过 → 绝不启用（顺序铁律）。"""
    probe = _probe({"verify": verify_entry, "arm": {"status": 201}})
    assert probe["result"] is False
    assert not _has_arm(probe["calls"]), "Verify 未通过却调用了启用端点"


# ================================================================== T3 启用成功 → 明确下一步


@needs_node
def test_t3_arm_success_asks_user_to_send_first_message() -> None:
    """T3：启用成功后，UI 明确要求用户从渠道发第一条消息。"""
    probe = _probe({"verify": {"body": {"reachable": True, "probe": "minimal"}},
                    "arm": {"status": 201}})
    assert probe["result"] is True
    assert "发送一条消息" in probe["detail"], probe["detail"]


# ================================================================== T4 启用失败 → 可行动


@needs_node
@pytest.mark.parametrize("arm_entry", [{"status": 500, "body": {}}, {"status": 401, "body": {}}])
def test_t4_arm_failure_is_actionable(arm_entry: dict) -> None:
    """T4：启用失败 → 可行动错误（不静默、不谎报成功）。"""
    probe = _probe({"verify": {"body": {"reachable": True, "probe": "minimal"}},
                    "arm": arm_entry})
    assert probe["result"] is False
    assert _has_arm(probe["calls"])                       # 确实尝试过启用
    assert "启用" in (probe["error"] + probe["detail"])    # 明确告知「启用」这一步
    assert "重新「接通」" in probe["detail"]


# ================================================================== T5 First Success 成功


@needs_node
def test_t5_first_success_success_ui() -> None:
    """T5：当前渠道（channel_id=7）的 succeeded × delivered → 成功文案。"""
    detail = _first_success([
        {"id": 42, "channel_id": 7,
         "processing_status": "succeeded", "delivery_status": "delivered"},
    ])
    assert "成功送达" in detail, detail


@needs_node
def test_t5_first_success_success_ui_home() -> None:
    """T5（Flow Home 同构）：当前渠道（channel_id=7）的 succeeded × delivered → 成功文案。"""
    detail = _first_success([
        {"id": 42, "channel_id": 7,
         "processing_status": "succeeded", "delivery_status": "delivered"},
    ], js_file=HOME_JS, mode="home")
    assert "成功送达" in detail, detail


# ================================================================== T6–T9 失败层级诊断


_STAGE_ITEM = {
    # channel_id=7 = 沙箱注入的当前渠道（M7-AUI-9.2：First Success 按当前渠道 scoped）
    "identity": {"channel_id": 7, "failure_stage": "identity_resolution",
                 "failure_code": "IDENTITY_RESOLUTION_FAILED"},
    "authorization": {"channel_id": 7, "failure_stage": "authorization",
                      "failure_code": "UNAUTHORIZED"},
    "agent": {"channel_id": 7, "failure_stage": "agent_dispatch",
              "failure_code": "AGENT_DISPATCH_FAILED"},
    "processing": {"channel_id": 7, "processing_status": "failed",
                   "failure_stage": None, "failure_code": None},
    "unexpected": {"channel_id": 7, "processing_status": "failed",
                   "failure_code": "UNEXPECTED_FAILURE"},
    "delivery": {"channel_id": 7, "failure_stage": "delivery",
                 "failure_code": "CHANNEL_DELIVERY_FAILED"},
}


@needs_node
@pytest.mark.parametrize("label", list(_STAGE_ITEM))
def test_t6_to_t9_failure_is_diagnosed(label: str) -> None:
    """T6–T9：既有 failure_stage / failure_code → 可执行诊断（步骤 + 原因 + 下一步）。"""
    item = _STAGE_ITEM[label]
    d = _diagnose(item)
    assert d is not None, f"{label} 未被诊断"
    assert d["step"] and d["reason"] and d["next"], d
    assert "下一步" in d["next"] or d["next"].startswith("请"), d


@needs_node
@pytest.mark.parametrize("label", ["identity", "authorization", "agent", "processing", "delivery"])
def test_t6_to_t9_failure_diagnosed_in_ui(label: str) -> None:
    """T6–T9（UI 面）：未成功且存在失败事实时，UI 明确告知「哪一步没走通 + 下一步」。"""
    item = _STAGE_ITEM[label]
    detail = _first_success([item])
    assert "没有走通" in detail, detail
    assert "下一步" in detail, detail


# 共享阶段（agent_dispatch / 通用失败兜底 / delivery）：同一 failure 事实 → 同一结论
# ——M7-AUI-5（真实事故对齐）冻结的不变量：两页的 step/reason/next 必须逐字一致，
# 且不得出现某页独有的额外字段（home.js 额外暴露 stage，connect.js 不暴露）。
_SHARED_DIAGNOSIS_LABELS = ("agent", "processing", "delivery")
# 页面特化阶段（identity_resolution / authorization）：Home 的前提是「首条消息引导窗口
# 未开启/已过期」，其下一步必须是 Home 真实可执行的动作（M7-AUI-10.6.2-B：「重新验证
# 并引导」+ 窗口内重发一条新的单聊消息）；connect 是独立接入/恢复页，没有该入口，
# 其下一步是「重新「接通」」⇒ 两页文案**按设计不同**，不再要求逐字一致。
_PAGE_SPECIFIC_DIAGNOSIS_LABELS = ("identity", "authorization")


def _visible_conclusion(diag: dict | None) -> tuple:
    """用户可见结论三元组（step/reason/next）——一致性不变量只约束这三项。"""
    assert diag is not None
    return (diag.get("step"), diag.get("reason"), diag.get("next"))


@needs_node
def test_t6_to_t9_diagnosis_mapping_identical_in_home() -> None:
    """共享阶段两页结论逐字一致；identity/authorization 为页面特化（契约分层）。

    - 共享阶段：M7-AUI-5 事故对齐不变量 → step/reason/next 逐字一致，且字段集
      不越界（home 侧至多多一个 stage，connect 侧不得多出字段）。
    - identity/authorization：M7-AUI-10.6.2-B 之后按页面特化 → step 相同；next
      各自指向**本页**可执行动作；home 侧 stage 与输入 failure 事实一致。
    """
    for label in _SHARED_DIAGNOSIS_LABELS:
        item = _STAGE_ITEM[label]
        conn = _diagnose(item)
        home = _diagnose(item, js_file=HOME_JS, mode="home")
        assert _visible_conclusion(conn) == _visible_conclusion(home), label
        assert set(conn).issubset({"step", "reason", "next"}), label
        assert set(home).issubset({"step", "reason", "next", "stage"}), label
    for label in _PAGE_SPECIFIC_DIAGNOSIS_LABELS:
        item = _STAGE_ITEM[label]
        conn = _diagnose(item)
        home = _diagnose(item, js_file=HOME_JS, mode="home")
        assert conn["step"] == home["step"], label
        assert "重新「接通」" in conn["next"], label
        assert "重新验证并引导" in home["next"], label
        assert home.get("stage") == item["failure_stage"], label


@needs_node
def test_diagnosis_is_null_when_not_failed() -> None:
    """非失败（pending / 无 failure 事实）→ 不产生诊断（不误报）。"""
    assert _diagnose({"processing_status": "pending", "delivery_status": "pending"}) is None


# ================================================================== T10 ACP 探针不泄漏


def test_t10_acp_probe_detail_has_no_connection_details() -> None:
    """T10（后端，行为）：不可达探针的 detail 不含 host / port / URL / 异常原文。"""
    conn = Connection(channel_id=1, agent_id=1, name="c", id=7, config={
        "protocol": "minimal", "endpoint": "http://127.0.0.1:1", "timeout_s": 2})
    snap = ACPAdapter().probe(conn)
    assert snap.reachable is False
    detail = snap.detail or ""
    assert detail == "agent endpoint could not be reached"
    assert "127.0.0.1" not in detail and "http" not in detail and ":" not in detail


def test_t10_acp_probe_invalid_endpoint_is_generic() -> None:
    """T10（后端）：非法 endpoint 不回显 scheme / 内部原因。"""
    conn = Connection(channel_id=1, agent_id=1, name="c", id=7, config={
        "protocol": "minimal", "endpoint": "http://", "timeout_s": 2})
    snap = ACPAdapter().probe(conn)
    assert snap.reachable is False
    assert snap.detail == "connection config missing/invalid endpoint"


def test_t10_acp_probe_source_never_interpolates_exception_text() -> None:
    """T10（源码，AST）：两个探针方法绝不把 `exc`/异常类型插进 detail。"""
    tree = ast.parse(ACP_PY.read_text(encoding="utf-8"))
    for func_name in ("_probe_tencent_acp_sse", "_probe_minimal"):
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == func_name)
        interpolated: set[str] = set()
        for node in ast.walk(fn):
            if isinstance(node, ast.JoinedStr):
                for value in node.values:
                    if isinstance(value, ast.FormattedValue):
                        for sub in ast.walk(value.value):
                            if isinstance(sub, ast.Name):
                                interpolated.add(sub.id)
        assert "exc" not in interpolated, f"{func_name} 把异常对象写进了 detail"


def test_t10_connect_ui_never_renders_raw_probe_detail() -> None:
    """T10（前端）：Cloud UI 不再把裸 `snap.detail` 当用户文案。"""
    src = CONNECT_JS.read_text(encoding="utf-8")
    assert "snap.detail" not in src, "Cloud UI 仍在渲染裸探针 detail"


# ================================================================== T11/T12 回归锚点


def test_t11_readiness_route_is_verify_only() -> None:
    """T11：readiness 端点唯一（/verify）；前端不再出现 /health 或 provider 分派。"""
    src = CONNECT_JS.read_text(encoding="utf-8")
    assert 'request("POST", `${CONNECTIONS_API}/${connectionId}/verify`)' in src
    assert "${CONNECTIONS_API}/${connectionId}/health" not in src
    assert "markWorkBuddy" not in src          # 前端 provider 分派 helper 已移除
    assert "workbuddy ?" not in src            # 不再按 provider 选端点/方法


def test_t12_connect_ui_frozen_literals_intact() -> None:
    """T12：F1/F3 冻结字面仍在（Connect 页契约未被 F4-A 破坏）。"""
    src = CONNECT_JS.read_text(encoding="utf-8")
    assert "async function probe" in src          # m6_3 护栏依赖的字面
    assert "async function handleExistingBinding" in src
    assert 'const PROCESSING_SUCCEEDED = "succeeded";' in src
    assert 'const DELIVERY_DELIVERED = "delivered";' in src
    assert "processing_status === PROCESSING_SUCCEEDED" in src
    assert "delivery_status === DELIVERY_DELIVERED" in src
    assert "未创建新的连接" in src
    assert "err.status === 503" in src
    assert "非连接结论" in src


# ================================================================== 后端集成：统一 /verify
# F4-A 的 readiness 入口（POST /verify）对三类 provider 都成立——分派在后端（F3），
# 前端只需调用同一个端点。此处按组合根同形装配真实 dispatcher。

_CORP_SECRET = "corpsecret-super-secret-value"


class _GetTokenHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.server.calls += 1
        payload = (
            json.dumps({"errcode": 0, "access_token": "tok"}).encode()
            if self.server.ok
            else json.dumps({"errcode": 40001, "errmsg": "invalid"}).encode()
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        return


class _GetTokenServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


@contextlib.contextmanager
def _gettoken_server(*, ok: bool):
    server = _GetTokenServer(("127.0.0.1", 0), _GetTokenHandler)
    server.ok = ok
    server.calls = 0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


class _FakeWbTransport:
    """WorkBuddy Management API 确定性桩（不触网）。"""

    _RESPONSE = json.dumps({
        "data": {"id": "rt-1", "links": {"acpLink": {
            "url": "https://runtime.example/acp", "token": "acp-link-token"}}},
    }).encode("utf-8")

    def __call__(self, method, url, headers, body, timeout_s):
        return 200, self._RESPONSE


def _wb_probe(connection) -> AgentProbeSnapshot:
    return AgentProbeSnapshot(True, None, "tencent-acp-sse")


def _factory(db_engine):
    return sessionmaker(bind=db_engine, class_=Session, expire_on_commit=False)


def _uow_factory(db_engine):
    factory = _factory(db_engine)
    return lambda: SqlAlchemyUnitOfWork(session_factory=factory)


def _seed_connection(db_engine, *, channel_type="wecom", channel_config=None,
                     connection_config=None):
    uow_factory = _uow_factory(db_engine)
    channel = ChannelApplicationService(uow_factory=uow_factory).create_channel(
        CreateChannel(name=f"ch-{channel_type}", type=channel_type, config=channel_config))
    agent = AgentApplicationService(uow_factory=uow_factory).create_agent(
        CreateAgent(name="ag", type="acp"))
    return ConnectionApplicationService(uow_factory=uow_factory).create_connection(
        CreateConnection(channel_id=channel.id, agent_id=agent.id, name="c",
                         config=connection_config))


def _verify_client(db_engine) -> TestClient:
    """按组合根同形的 readiness 入口：verify_provider = F3 dispatcher。"""
    uow_factory = _uow_factory(db_engine)

    def _load_channel(channel_id):
        with uow_factory() as uow:
            return uow.channels.get_by_id(channel_id)

    dispatcher = build_connection_verifier(
        probe=ACPAdapter().probe,
        workbuddy_verify=WorkBuddyProvisioner(
            probe=_wb_probe,
            client=WorkBuddyManagementClient(transport=_FakeWbTransport())).verify,
        wecom_verify=WeComAdapter.verify_connection,
        channel_loader=_load_channel,
    )
    app = FastAPI()
    app.include_router(build_connections_api(
        ConnectionApplicationService(uow_factory=uow_factory),
        verify_provider=lambda: dispatcher))
    return TestClient(app)


def _wecom_channel_config(base_url: str) -> dict:
    return {"corp_id": "corp-x", "corpsecret": _CORP_SECRET, "agentid": 1000002,
            "api_base": base_url, "timeout_s": 5}


def test_t1_backend_unified_verify_wecom_ready(db_engine) -> None:
    """T1（WeCom）：POST /verify → provider-specific 校验 → reachable=true。"""
    with _gettoken_server(ok=True) as base:
        conn = _seed_connection(db_engine, channel_config=_wecom_channel_config(base))
        resp = _verify_client(db_engine).post(f"/api/connections/{conn.id}/verify")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reachable"] is True and body["probe"] == "wecom"


def test_t2_backend_unified_verify_wecom_invalid(db_engine) -> None:
    """T2（WeCom 非法凭据）：POST /verify → reachable=false，且不泄漏凭据。"""
    with _gettoken_server(ok=False) as base:
        conn = _seed_connection(db_engine, channel_config=_wecom_channel_config(base))
        resp = _verify_client(db_engine).post(f"/api/connections/{conn.id}/verify")
    assert resp.status_code == 200
    assert resp.json()["reachable"] is False
    assert _CORP_SECRET not in resp.text


def test_t3_backend_unified_verify_wecom_network_failure(db_engine) -> None:
    """T3（网络失败）：POST /verify → Fail Closed（reachable=false）。"""
    conn = _seed_connection(db_engine, channel_config={
        "corp_id": "c", "corpsecret": _CORP_SECRET, "agentid": 1,
        "api_base": "http://127.0.0.1:1", "timeout_s": 2})
    resp = _verify_client(db_engine).post(f"/api/connections/{conn.id}/verify")
    assert resp.status_code == 200
    assert resp.json()["reachable"] is False
    assert _CORP_SECRET not in resp.text


def test_t4_backend_unified_verify_workbuddy_ready(db_engine) -> None:
    """T4（WorkBuddy）：POST /verify → lazy provisioning → reachable=true。"""
    conn = _seed_connection(db_engine, connection_config={
        "provider": "workbuddy", "workbuddy_agent_id": "a", "api_key": "k"})
    resp = _verify_client(db_engine).post(f"/api/connections/{conn.id}/verify")
    assert resp.status_code == 200
    assert resp.json()["reachable"] is True


def test_t5_backend_unified_verify_fallback_acp_probe(db_engine) -> None:
    """T5（fallback）：无 provider-specific verifier 的 Connection → ACP 探针结论。"""
    conn = _seed_connection(db_engine, channel_type="local")  # 无 config → ACP 探针
    resp = _verify_client(db_engine).post(f"/api/connections/{conn.id}/verify")
    assert resp.status_code == 200
    body = resp.json()
    assert body["reachable"] is False and body["probe"] == "minimal"
