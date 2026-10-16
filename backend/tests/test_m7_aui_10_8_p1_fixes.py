"""M7-AUI-10.8 —— P1-1 / P1-2 修复验收（frontend home.js）。

P1-1：Home 渠道保存失败文案按结构化 code 区分——`err.code === "duplicate_bot_channel"`
（M7-AUI-10.6.7 结构化 detail）→ 透出后端安全 message（复用指引）；普通同名 409
维持既有提示；后端防护语义零改动。

P1-2：
- 编辑锚点始终锚定当前显示对象，不因类型选择器不一致被静默清空后转「自动命名新建」；
- 类型不一致时 testChannel 显式阻止保存并提示；
- 选择器切换后，展开中的编辑面板跟随新选中对象（锚点/标题/类型选择器一致）。

验收面：静态断言（源码结构/顺序）+ Node `vm` 行为级（channelErrorMessage /
channelTypeMismatchMessage / syncChannelPanel 三场景）。缺 Node 自动 skip。
只读 DB / 不触网 / 不触真实租户。真实浏览器验收未执行（见交付报告）。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOME_JS = REPO_ROOT / "frontend" / "home.js"
HTTP_JS = REPO_ROOT / "frontend" / "shared" / "http.js"

_NODE = shutil.which("node")
needs_node = pytest.mark.skipif(_NODE is None, reason="node 不可用（行为级验收需要 Node）")

_BOT_CONFLICT_MSG = ("已存在使用同一企业微信机器人（bot_id）的启用渠道："
                     "企微-A（#1），请复用该渠道或先停用它。")


def _js() -> str:
    return HOME_JS.read_text(encoding="utf-8")


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


# ================================================================== A. 静态断言


def test_a1_p1_1_structured_409_branch_and_preserved_semantics() -> None:
    """P1-1：channelErrorMessage 按 err.code 分支；同名/404/422 语义保留；
    testChannel 的失败文案改走 channelErrorMessage。"""
    code = _js()
    body = _fn(code, "channelErrorMessage")
    assert 'err.code === "duplicate_bot_channel"' in body
    assert "已存在同名渠道：换一个渠道名" in body, "普通同名 409 语义保留"
    assert "请检查 Bot ID / Bot Secret 是否正确。" in body
    assert "这条渠道已不存在" in body
    assert 'raw.startsWith("HTTP 409: ")' in body, "剥除 http.js 的状态码前缀"
    # testChannel 失败路径接入
    tc = _fn(code, "testChannel")
    assert "channelErrorMessage(err)" in tc
    assert "已存在同名渠道" not in tc, "内联文案已收敛到 channelErrorMessage"


def test_a2_p1_2_anchor_never_cleared_by_type_mismatch() -> None:
    """P1-2：编辑锚点 = 当前显示对象（不因类型选择器不一致被清空）。"""
    body = _fn(_js(), "syncChannelPanel")
    assert "editingChannelId = shown ? shown.id : null;" in body
    assert "editingChannelId = saved ? saved.id : null" not in body, "旧实现已移除"
    assert "channelById(selectedChannelId) || channelById(renderedChannelId)" in body


def test_a3_p1_2_type_mismatch_blocks_save_before_payload() -> None:
    """P1-2：类型不一致在 buildChannelPayload 之前显式阻止保存（绝不转新建）。"""
    tc = _fn(_js(), "testChannel")
    guard = 'editingShown.type !== pcType.value'
    assert guard in tc
    assert "channelTypeMismatchMessage(editingShown, pcType.value)" in tc
    assert tc.index(guard) < tc.index("const built = buildChannelPayload()"), \
        "阻止必须发生在构造请求之前"


def test_a4_p1_2_panels_follow_selection_switch() -> None:
    """P1-2：选择器切换后，展开中的 existing 面板跟随新选中对象。"""
    code = _js()
    cp = _fn(code, "onChannelPicked")
    ap = _fn(code, "onAgentPicked")
    assert 'openPanel === "channel" && channelPanelMode === "existing"' in cp
    assert "syncChannelType()" in cp, "重对齐类型选择器并重解析面板"
    assert 'openPanel === "agent" && agentPanelMode === "existing"' in ap
    assert "syncAgentPanel()" in ap


def test_a5_p1_2_edit_entry_aligns_type_selector() -> None:
    """P1-2：「编辑」入口打开面板时，类型选择器与实际对象对齐（显示即编辑）。"""
    code = _js()
    handler = code[code.index('ctaEditChannel.addEventListener'):]
    handler = handler[:handler.index("\n});")]
    assert "pcType.value = shown.type" in handler
    assert "channelById(selectedChannelId) || channelById(renderedChannelId)" in handler


def test_a6_credential_prefill_contract_unchanged() -> None:
    """既有合同不回归：syncChannelPanel 回填逻辑不触碰 Secret（M7-AUI-6 冻结）。"""
    code = _js()
    prefill = code[code.index("function syncChannelPanel"):]
    prefill = prefill[:prefill.index("function channelChangeRequested")]
    assert "bot_secret" not in prefill
    assert 'pcBotSecret.value = ""' in prefill


def test_a7_http_js_code_channel_available() -> None:
    """前置：shared/http.js 把结构化 detail 的 code 附加到 err.code（10.6.7）。"""
    http_src = HTTP_JS.read_text(encoding="utf-8")
    assert "err.code = d.code" in http_src


# ================================================================== B. Node 行为级


@needs_node
def test_b1_channel_message_branches() -> None:
    """channelErrorMessage 五分支行为（真实源码执行）。"""
    fn = _fn(_js(), "channelErrorMessage")
    cases = [
        # Bot 身份冲突（含后端 message）→ 透出后端安全 message
        ({"status": 409, "code": "duplicate_bot_channel",
          "message": f"HTTP 409: {_BOT_CONFLICT_MSG}"}, _BOT_CONFLICT_MSG),
        # Bot 身份冲突（message 缺失）→ 兜底复用指引
        ({"status": 409, "code": "duplicate_bot_channel", "message": "HTTP 409: "},
         "已存在使用同一企业微信机器人的启用渠道"),
        # 普通同名 409（无 code）→ 维持同名提示
        ({"status": 409}, "已存在同名渠道：换一个渠道名"),
        ({"status": 404}, "这条渠道已不存在"),
        ({"status": 422}, "请检查 Bot ID / Bot Secret 是否正确。"),
    ]
    script = fn + "\nconst __out = [];\n" + "\n".join(
        f"__out.push(channelErrorMessage({json.dumps(c, ensure_ascii=False)}));"
        for c, _ in cases) + "\nconsole.log(JSON.stringify(__out));"
    out = subprocess.run([_NODE, "-e", script], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=30)
    assert out.returncode == 0, out.stderr
    results = json.loads(out.stdout.strip().splitlines()[-1])
    for (case, expected), actual in zip(cases, results):
        assert expected in actual, f"{case} → {actual}"
    # 全部文案零凭据/零 config
    joined = " ".join(results)
    for banned in ("bot_secret", "********", '"config"', "PROBE-"):
        assert banned not in joined


@needs_node
def test_b2_type_mismatch_message() -> None:
    """channelTypeMismatchMessage：包含双方类型的人读名称与阻止语义。"""
    code = _js()
    fn = _fn(code, "channelTypeMismatchMessage")
    base = _fn(code, "channelBaseName")
    stub = "function channelLabel(t){ return t; }\n"
    script = stub + base + "\n" + fn + "\nconsole.log(JSON.stringify(["
    script += 'channelTypeMismatchMessage({type: "wecom_bot"}, "local"), '
    script += 'channelTypeMismatchMessage({type: "local"}, "wecom_bot")'
    script += "]));"
    out = subprocess.run([_NODE, "-e", script], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=30)
    assert out.returncode == 0, out.stderr
    results = json.loads(out.stdout.strip().splitlines()[-1])
    assert "企业微信" in results[0] and "本地测试渠道" in results[0]
    assert "本地测试渠道" in results[1] and "企业微信" in results[1]
    for msg in results:
        assert "不能在本面板" in msg and "请取消编辑" in msg


@needs_node
def test_b3_sync_channel_panel_anchor_and_hint() -> None:
    """syncChannelPanel 三场景：正常编辑（锚点+回填）/ 类型不一致（锚点保留+
    阻止提示）/ 对象缺失（阻断）。"""
    code = _js()
    parts = (
        _fn(code, "channelById"),
        _fn(code, "channelBaseName"),
        _fn(code, "channelTypeMismatchMessage"),
        _fn(code, "syncChannelPanel"),
    )
    harness = """
const __els = {};
function el(id){ if(!__els[id]) __els[id] = { value:"", textContent:"",
  classList:{ add(){}, remove(){}, toggle(){} }, style:{}, dataset:{} };
  return __els[id]; }
function setPanelTitle(elRef, text){ if (elRef) elRef.textContent = text; }
function setPanelError(elRef, message){ elRef.textContent = message; elRef.__shown = true; }
function hide(elRef){ elRef.__shown = false; }
function show(elRef){ elRef.__shown = true; }
const channelLabel = function(t){ return t; };
let editingChannelId = null;
let editingChannelMissing = false;
let selectedChannelId = null;
let renderedChannelId = null;
let channelPanelMode = "existing";
const ref = { channels: [
  { id: 15, name: "企业微信 11", type: "wecom_bot", status: "active",
    config: { bot_id: "bot-A" } },
] };
const pcType = { value: "wecom_bot" };
const pcSavedHint = el("pc-saved-hint");
const pcBotId = el("pc-bot-id");
const pcBotSecret = el("pc-bot-secret");
const panelChannelTitle = el("panel-channel-title");
const pcError = el("pc-error");
const credentialGuard = { markTyped(){} };
__PARTS__
__SETUP__
console.log(JSON.stringify({
  anchor: editingChannelId, missing: editingChannelMissing,
  title: panelChannelTitle.textContent, hint: pcSavedHint.textContent,
  error: pcError.textContent, botId: pcBotId.value,
  secretShown: pcBotSecret.value !== "",
}));
"""
    def run(setup: str) -> dict:
        script = (harness
                  .replace("__PARTS__", "\n".join(parts))
                  .replace("__SETUP__", setup))
        out = subprocess.run([_NODE, "-e", script], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=30)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout.strip().splitlines()[-1])

    # 场景 1：正常编辑（类型一致）→ 锚点 15 + 标题 + Bot ID 回填 + Secret 不回显
    o1 = run("selectedChannelId = 15; pcType.value = 'wecom_bot'; syncChannelPanel();")
    assert o1["anchor"] == 15 and o1["missing"] is False
    assert o1["title"] == "编辑渠道：企业微信 11"
    assert o1["botId"] == "bot-A" and o1["secretShown"] is False
    assert o1["hint"] != "" and "已保存" in o1["hint"]

    # 场景 2：类型不一致 → 锚点仍为 15（不静默清空）+ 阻止提示 + 字段清空
    o2 = run("selectedChannelId = 15; pcType.value = 'local'; syncChannelPanel();")
    assert o2["anchor"] == 15, "类型不一致不得清空编辑锚点"
    assert "保存会被阻止" in o2["hint"]
    assert o2["botId"] == "" and o2["secretShown"] is False
    assert o2["error"] == "", "类型不一致不是对象缺失错误"

    # 场景 3：对象缺失（无选择、无渲染对象）→ 锚点 null + 阻断错误
    o3 = run("selectedChannelId = null; renderedChannelId = null; syncChannelPanel();")
    assert o3["anchor"] is None and o3["missing"] is True
    assert "没有找到当前渠道" in o3["error"]
