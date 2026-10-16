"""M7-AUI-10.2 —— Agent Credential Security Foundation 验收门。

目标：为未来的 Agent-level credential ownership 建立**安全基础**——只处理
`Agent.config` 的四件事：

1. **读脱敏**：`AgentResponse.config` 经 `sanitize_agent_config` 强制脱敏——
   `api_key` 在 GET / List / POST / PATCH 响应中恒为 `********`（镜像
   Channel 11.8 合同 #1 / Connection 16.1.2）；
2. **写哨兵**：Create/Update 的 config 中任何值 == "********" → 422
   （按值匹配、与键名无关；镜像 Connection 16.1.2）——占位符绝不进数据库；
3. **PATCH merge**：config 更新 = load → merge incoming → persist（镜像
   Channel 11.8 冻结合同 #3）——不填写的键（尤其 secret）保留；
4. **前端 secret 保全**：`agents.js` 编辑保存时值仍为 "********" 的键自动
   省略（镜像 channels.js buildConfigPayload）→ 不触发 422 → 真实值保留。

**本步明确不做**（边界由本文件钉住）：Connection credential fallback
（connection_verify.py / main.py / WorkBuddyProvisioner / acp.py 零改动）、
Home/Connect credential 迁移、runtime invalidation、Provider Registry。

方法：真实 FastAPI + 内存 SQLite 行为测试（TestClient 全链路）+ Node `vm`
真实执行 `agents.js`（缺 Node 自动 skip）。全部只读外部世界，不触网。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.oss_export_boundary import oss_ui_required
from sqlalchemy.orm import Session, sessionmaker

from app.application.agents import AgentApplicationService
from app.api.management.agents import build_agents_api
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork

BACKEND = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND.parent
AGENTS_JS = REPO_ROOT / "frontend" / "agents.js"
VERIFY_PY = BACKEND / "app" / "adapters" / "connection_verify.py"
MAIN_PY = BACKEND / "app" / "main.py"
ACP_PY = BACKEND / "app" / "adapters" / "agents" / "acp.py"

SECRET = "wb-api-key-REAL-SECRET"
ROTATED = "wb-api-key-ROTATED"
MASK = "********"

_NODE = shutil.which("node")
needs_node = pytest.mark.skipif(
    _NODE is None, reason="node 不可用（agents.js 行为级验收需要 Node）")


# ================================================================== 装配

def _uow_factory_for(db_engine):
    factory = sessionmaker(bind=db_engine, class_=Session, expire_on_commit=False)
    return lambda: SqlAlchemyUnitOfWork(session_factory=factory)


def _client(db_engine) -> TestClient:
    """真实 Agent 管理 API + 内存 SQLite（tenant-scoped 组合根的单租户同形）。"""
    app = FastAPI()
    app.include_router(build_agents_api(
        AgentApplicationService(uow_factory=_uow_factory_for(db_engine))))
    return TestClient(app)


def _create_wb_agent(client: TestClient, **config_overrides) -> dict:
    config = {"provider": "workbuddy", "workbuddy_agent_id": "wb-agent-1",
              "api_key": SECRET}
    config.update(config_overrides)
    resp = client.post("/api/agents", json={
        "name": "WorkBuddy", "type": "acp", "config": config})
    assert resp.status_code == 201, resp.text
    return resp.json()


# ================================================================== A. 读脱敏

def test_a1_post_response_masks_api_key_but_keeps_manager_keys(db_engine) -> None:
    """A1/A2/A5/A6：POST 响应 api_key → ********；provider / workbuddy_agent_id 原值。"""
    body = _create_wb_agent(_client(db_engine))
    assert body["config"]["api_key"] == MASK
    assert body["config"]["provider"] == "workbuddy"
    assert body["config"]["workbuddy_agent_id"] == "wb-agent-1"
    assert SECRET not in json.dumps(body)


def test_a2_get_and_list_mask_api_key(db_engine) -> None:
    """A3/A4：GET 单体与 List 响应 api_key 恒为 ********，真实值绝不出现在响应文本。"""
    client = _client(db_engine)
    _create_wb_agent(client)
    got = client.get("/api/agents/1").json()
    assert got["config"]["api_key"] == MASK
    listed = client.get("/api/agents").json()
    assert listed["items"][0]["config"]["api_key"] == MASK
    for payload in (json.dumps(got), json.dumps(listed)):
        assert SECRET not in payload


def test_a3_masking_lives_in_response_model_not_data(db_engine) -> None:
    """脱敏是响应面行为：数据库/领域对象仍保存真实 secret（否则 merge 无从保留）。"""
    client = _client(db_engine)
    _create_wb_agent(client)
    service = AgentApplicationService(uow_factory=_uow_factory_for(db_engine))
    raw = service.get_agent(1)
    assert raw.config["api_key"] == SECRET  # 存储面真实（pytest 进程内，不出 API）


def test_a4_masking_is_case_and_space_insensitive(db_engine) -> None:
    """键名匹配 strip + lower（宁可多脱敏不可漏，镜像 Channel/Connection 纪律）。"""
    client = _client(db_engine)
    body = _create_wb_agent(client, API_Key="v2-secret")
    assert body["config"]["API_Key"] == MASK


# ================================================================== B. 写哨兵

def test_b1_post_redacted_sentinel_is_422(db_engine) -> None:
    """B7：POST config 中 api_key == ******** → 422，占位符绝不入库。"""
    client = _client(db_engine)
    resp = client.post("/api/agents", json={
        "name": "WorkBuddy", "type": "acp",
        "config": {"provider": "workbuddy", "api_key": MASK}})
    assert resp.status_code == 422
    assert client.get("/api/agents").json()["total"] == 0


def test_b2_patch_redacted_sentinel_is_422(db_engine) -> None:
    """B8：PATCH config 中任何值 == ******** → 422（按值匹配、与键名无关）。"""
    client = _client(db_engine)
    _create_wb_agent(client)
    for payload in ({"api_key": MASK}, {"some_other_key": MASK}):
        resp = client.patch("/api/agents/1", json={"config": payload})
        assert resp.status_code == 422, payload


def test_b3_sentinel_is_not_over_broad(db_engine) -> None:
    """只拒绝精确占位符；普通字符串（含部分星号 / 长度不同）不受影响。"""
    client = _client(db_engine)
    body = _create_wb_agent(client, note="*********", star5="*****")
    assert body["config"]["note"] == "*********"
    assert body["config"]["star5"] == "*****"


# ================================================================== C. PATCH merge

def _patch_config(client: TestClient, config: dict) -> dict:
    resp = client.patch("/api/agents/1", json={"config": config})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_c1_patch_other_key_preserves_api_key(db_engine) -> None:
    """C9/C10：只改 workbuddy_agent_id → api_key 原值保留（响应脱敏 + 存储面真实）。"""
    client = _client(db_engine)
    _create_wb_agent(client)
    body = _patch_config(client, {"workbuddy_agent_id": "wb-agent-2"})
    assert body["config"] == {"provider": "workbuddy",
                              "workbuddy_agent_id": "wb-agent-2", "api_key": MASK}
    raw = AgentApplicationService(
        uow_factory=_uow_factory_for(db_engine)).get_agent(1)
    assert raw.config["api_key"] == SECRET


def test_c2_patch_provider_preserves_api_key(db_engine) -> None:
    """C11/C12：只改 provider → api_key 原值保留。"""
    client = _client(db_engine)
    _create_wb_agent(client)
    body = _patch_config(client, {"provider": "workbuddy-pro"})
    assert body["config"]["provider"] == "workbuddy-pro"
    raw = AgentApplicationService(
        uow_factory=_uow_factory_for(db_engine)).get_agent(1)
    assert raw.config["api_key"] == SECRET


def test_c3_patch_new_api_key_rotates(db_engine) -> None:
    """C13/C14：显式提交新 api_key → 替换旧值（轮换），其余键不动。"""
    client = _client(db_engine)
    _create_wb_agent(client)
    _patch_config(client, {"api_key": ROTATED})
    raw = AgentApplicationService(
        uow_factory=_uow_factory_for(db_engine)).get_agent(1)
    assert raw.config["api_key"] == ROTATED
    assert raw.config["provider"] == "workbuddy"
    assert raw.config["workbuddy_agent_id"] == "wb-agent-1"
    assert MASK not in json.dumps(raw.config)


def test_c4_omit_config_entirely_keeps_everything(db_engine) -> None:
    """PATCH 不带 config → 整个 config 原样保留（既有「缺省 = 不修改」不变）。"""
    client = _client(db_engine)
    _create_wb_agent(client)
    body = client.patch("/api/agents/1", json={"name": "WorkBuddy 2"}).json()
    assert body["config"]["api_key"] == MASK
    raw = AgentApplicationService(
        uow_factory=_uow_factory_for(db_engine)).get_agent(1)
    assert raw.config["api_key"] == SECRET


# ================================================================== D. credential_ref 语义不变

def test_d1_credential_ref_stays_write_only_opaque(db_engine) -> None:
    """E22：credential_ref 继续「引用入、引用不出」；api_key 不进 credential_ref。"""
    client = _client(db_engine)
    resp = client.post("/api/agents", json={
        "name": "ag", "type": "acp", "credential_ref": "vault://x"})
    assert resp.status_code == 201
    body = resp.json()
    assert "credential_ref" not in body
    listed = client.get("/api/agents").json()
    assert "credential_ref" not in json.dumps(listed)
    patched = client.patch("/api/agents/1", json={
        "credential_ref": "vault://y"}).json()
    assert "credential_ref" not in patched


# ================================================================== E. 本步边界钉住（未实施的保持未实施）

def test_e1_connection_verify_fallback_stays_in_memory() -> None:
    """E19/E20（M7-AUI-10.3 更新边界）：connection_verify.py 允许 agent_loader
    fallback，但 Agent credential 只在内存合成——acp.py 红线不变。"""
    src = VERIFY_PY.read_text(encoding="utf-8")
    assert "agent_loader" in src           # fallback 已在 verify/provisioning 面实施
    # 既有 WorkBuddy 分派仍以 Connection.config 为凭据事实源（优先）
    assert "PROVIDER_KEY" in src and "WORKBUDDY_PROVIDER" in src
    # 写回剥离：Agent credential 键绝不随 runtime cache 落回 Connection.config
    assert "_AGENT_CREDENTIAL_KEYS" in src
    acp_src = ACP_PY.read_text(encoding="utf-8")
    assert "agent.credential_ref" not in acp_src      # 运行时红线不变


def test_e2_composition_root_wires_tenant_scoped_agent_loader() -> None:
    """main.py 的 _load_agent 必须与 _load_channel 同一 tenant-scoped 纪律。"""
    src = MAIN_PY.read_text(encoding="utf-8")
    assert "_load_agent" in src
    assert "agent_loader=_load_agent" in src
    # tenant-scoped：经请求解析出的 TenantScope 绑定的同一 uow_factory
    load_agent = src[src.index("def _load_agent"):]
    load_agent = load_agent[:load_agent.index("agent_loader=_load_agent")]
    assert "uow_factory()" in load_agent


def test_e3_acp_architecture_red_line_unchanged() -> None:
    """E21：acp.py 仍只读 Connection.config，绝不引用 agent.credential_ref。"""
    src = ACP_PY.read_text(encoding="utf-8")
    assert "connection.config" in src
    assert "agent.credential_ref" not in src


def test_e4_connection_config_untouched_by_agent_masking(db_engine) -> None:
    """E19：Connection 侧脱敏 / 哨兵键集合不受影响（仍含 bearer_token 等）。"""
    from app.schemas.connections import (  # noqa: F401 局部导入验证存在
        SENSITIVE_CONNECTION_CONFIG_KEYS, sanitize_connection_config)
    assert "bearer_token" in SENSITIVE_CONNECTION_CONFIG_KEYS
    assert sanitize_connection_config({"bearer_token": "t"}) == {
        "bearer_token": MASK}


# ================================================================== F. 前端 agents.js（Node 行为）

_JS_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const src = fs.readFileSync(process.env.PROBE_JS, "utf8").replace(/^import\s[^;]*;/gm, "");

const calls = [];
const storage = { local: [], session: [] };
const els = {};
function fakeEl() {
  const classes = new Set();
  return {
    classList: {
      add(c) { classes.add(c); }, remove(c) { classes.delete(c); },
      contains(c) { return classes.has(c); },
    },
    textContent: "", innerHTML: "", className: "", href: "", value: "",
    placeholder: "", disabled: false, handlers: {},
    appendChild() {}, append() {},
    addEventListener(type, fn) { this.handlers[type] = fn; },
    focus() {}, reset() {}, options: [],
  };
}
function el(id) { if (!els[id]) els[id] = fakeEl(); return els[id]; }

const AGENT = JSON.parse(process.env.PROBE_AGENT || "null");

const sandbox = {
  console: console,
  document: { getElementById: el, createElement: () => fakeEl() },
  window: { location: { origin: "http://localhost" } },
  localStorage: { setItem(k, v) { storage.local.push([k, v]); },
                  getItem() { return null; }, removeItem() {}, clear() {} },
  sessionStorage: { setItem(k, v) { storage.session.push([k, v]); },
                    getItem() { return null; }, removeItem() {}, clear() {} },
  fetch: function (url, options) {
    const method = (options && options.method) || "GET";
    const payload = options && options.body ? JSON.parse(options.body) : null;
    const u = String(url);
    calls.push({ method, url: u, payload });
    const ok = (status, body) => Promise.resolve({
      ok: status >= 200 && status < 300, status, json: () => Promise.resolve(body) });
    if (u.indexOf("/api/agents") >= 0) {
      if (method === "GET") return ok(200, { items: [AGENT], total: 1 });
      return ok(200, AGENT);
    }
    return ok(404, {});
  },
};
vm.createContext(sandbox);
const sharedPath = process.env.PROBE_JS.replace(/[^/\\]+$/, "") + "shared/http.js";
const sharedSrc = fs.readFileSync(sharedPath, "utf8").replace(/^export /gm, "");
vm.runInContext(sharedSrc, sandbox);
vm.runInContext(src + "\n;globalThis.__t = { openEdit: openEdit, "
  + "submitForm: submitForm };", sandbox);

const settle = async () => { for (let i = 0; i < 8; i++) await new Promise(r => setTimeout(r, 0)); };

(async function () {
  const t = sandbox.__t;
  const out = {};
  await settle(); // loadList 完成

  // 打开编辑（GET 的 masked config 回填）
  t.openEdit(AGENT);
  out.formConfig = els["f-config"].value;
  out.maskEchoed = out.formConfig.indexOf("********") >= 0;
  out.realSecretEchoed = out.formConfig.indexOf(process.env.PROBE_SECRET) >= 0;

  // 用户只改 workbuddy_agent_id（api_key 保持占位符）→ 保存
  els["f-config"].value = JSON.stringify({
    provider: "workbuddy",
    workbuddy_agent_id: "wb-agent-2",
    api_key: "********",
  });
  calls.length = 0;
  await t.submitForm({ preventDefault() {} });
  await settle();
  const patch = calls.find(c => c.method === "PATCH");
  out.patchPayload = patch ? patch.payload : null;
  out.patchUrl = patch ? patch.url : null;
  out.maskSentInPatch = patch ? JSON.stringify(patch.payload).indexOf("********") >= 0 : null;
  out.secretSentInPatch = patch
    ? JSON.stringify(patch.payload).indexOf(process.env.PROBE_SECRET) >= 0 : null;

  // 轮换：显式输入新 api_key → PATCH 携带新值（真实 UI 会重新点「编辑」打开表单）
  t.openEdit(AGENT);
  els["f-config"].value = JSON.stringify({
    provider: "workbuddy",
    workbuddy_agent_id: "wb-agent-2",
    api_key: process.env.PROBE_ROTATED,
  });
  calls.length = 0;
  await t.submitForm({ preventDefault() {} });
  await settle();
  const patch2 = calls.find(c => c.method === "PATCH");
  out.rotationPayload = patch2 ? patch2.payload : null;
  out.rotatedSent = patch2
    ? patch2.payload.config && patch2.payload.config.api_key === process.env.PROBE_ROTATED
    : false;

  out.storageWrites = { local: storage.local, session: storage.session };
  process.stdout.write(JSON.stringify(out));
})().catch(function (e) {
  process.stderr.write(String((e && e.stack) || e));
  process.exit(2);
});
"""


def _run_js(agent: dict) -> dict:
    env = dict(os.environ)
    env["PROBE_JS"] = str(AGENTS_JS)
    env["PROBE_AGENT"] = json.dumps(agent)
    env["PROBE_SECRET"] = SECRET
    env["PROBE_ROTATED"] = ROTATED
    proc = subprocess.run(
        [_NODE, "-e", _JS_HARNESS], capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env, check=False)
    assert proc.returncode == 0, f"node 执行失败：{proc.stderr}"
    return json.loads(proc.stdout)


@oss_ui_required()
@needs_node
def test_f1_frontend_masks_echo_and_omits_on_save(db_engine) -> None:
    """D15-D18：GET masked → 回填只含占位符；保存省略 ********；非密字段正常发送。"""
    client = _client(db_engine)
    agent = _create_wb_agent(client)  # 响应即 GET 形态（已脱敏）
    out = _run_js(agent)
    assert out["maskEchoed"] is True
    assert out["realSecretEchoed"] is False
    assert out["patchUrl"] == "/api/agents/1"
    assert out["patchPayload"]["config"] == {
        "provider": "workbuddy", "workbuddy_agent_id": "wb-agent-2"}
    assert out["maskSentInPatch"] is False
    assert out["secretSentInPatch"] is False


@oss_ui_required()
@needs_node
def test_f2_frontend_rotation_sends_new_secret(db_engine) -> None:
    """九：显式输入新 API Key → PATCH 携带新值（后端轮换）。"""
    client = _client(db_engine)
    agent = _create_wb_agent(client)
    out = _run_js(agent)
    assert out["rotatedSent"] is True
    assert out["rotationPayload"]["config"]["api_key"] == ROTATED


@oss_ui_required()
@needs_node
def test_f3_frontend_never_persists_secret_to_web_storage(db_engine) -> None:
    """十五：secret 不主动落 localStorage / sessionStorage。"""
    client = _client(db_engine)
    agent = _create_wb_agent(client)
    out = _run_js(agent)
    assert out["storageWrites"] == {"local": [], "session": []}
    assert SECRET not in json.dumps(out)


@oss_ui_required()
def test_f5_frontend_does_not_log_config() -> None:
    """十五（静态）：agents.js 不把 config / secret 写进 console。"""
    src = AGENTS_JS.read_text(encoding="utf-8")
    assert not re.search(r"console\.\w+\([^)]*config", src, re.I)
