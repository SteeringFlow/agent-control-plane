"""Artifact 引用呈现（ACP V0.3 Phase 1）—— 纯 domain 渲染器。

定位：**把 `AgentResponse.artifacts` 渲染为「可安全呈现的引用文本块」**。
它只做**呈现**（presentation），不做**取回**（retrieval）：

- **不抓取 / 不下载 / 不代理** URI 指向的资源（零 I/O、零网络）；
- **不渲染** `Artifact.metadata`（agent 提供的自由 dict：不可信、无界）；
- **不存储** artifact 字节（本阶段无 artifact 存储 / 对象存储 / 签名 URL）；
- **不修改** Artifact 快照 / Interaction 持久化语义（呈现层与持久化层正交）。

V0.3 契约要点（权威来源：`docs/step-acp-v0.3-artifact-outbound-implementation-gate.md` §6.4）：

- **L1 展示长度限制**：最多呈现 10 个 artifact；自由文本展示字段上限 128 字符；
  引用块总长上限 2000 字符。**均为 Phase 1 显式设计值**——仓库现无同类限制，
  且 2000 **不是**任何渠道平台侧的文本长度上限。
- **L2 URI 原值的完整性**：消息中出现的 URI 必须与 `Artifact.uri` **逐字节相同**
  ——不截断、不改写、不消毒。
- **L3 超长 URI 的行为**：URI 是**原子令牌**——要么原样呈现，要么**整体省略**
  并给出明确原因。**绝不**输出「被截断的 URI 片段」并把它暗示成可用地址。
- **L4 快照持久化语义**：Interaction 响应快照中的 artifact 列表与 Local outbox 的
  `artifacts` 均保留**完整原值**；展示预算**不作用于此**。

安全呈现规则：

- 自由文本字段（name / kind / mime_type）：**不安全字符**（Unicode 类别 `Cc`
  控制符 / `Cf` 格式符 / `Zl` 行分隔符 / `Zp` 段分隔符 —— 含 `\r` `\n` `\t`、
  零宽字符、RTL override、**U+2028 / U+2029**）与标记字符 → 空格；
  超长 → 截断并显式标注（`…`）。截断的是**展示标签**，不是地址。
- URI：含 `Cc` / `Cf` → **整体省略**（格式非法）；含 `Zl` / `Zp` → **整体省略**
  （行/段分隔符）；含标记字符 → **整体省略**（不适合展示）；其余且能**完整**
  放入剩余预算 → **原样**呈现（置于代码跨度内，**不生成** Markdown 链接、
  **不生成** HTML 链接、**不生成**裸可执行标记）。**绝不**先消毒 URI 再把
  改写后的值当作地址呈现。
- **Phase 2 策略闸门**（`app.domain.artifact_uri_policy`）：URI 还必须是**符合策略的
  绝对 HTTP(S) URL**（仅 `https` / `http`；拒绝 userinfo、相对 URL、缺主机、
  非法端口、结构非法、超长）才允许呈现；不可呈现 ⇒ **整体省略**并给出明确原因，
  **不输出 URI 任何片段**。「可呈现」**不等于**「可访问」**不等于**「可信」
  **不等于**「已授权」——ACP **不取回字节**、**不提供文件级授权**。
- `mime_type` / `size_bytes` 是 **Agent 声明值**（ACP **未**验证）⇒ 呈现时
  **必须**标注「Agent 声明值，未验证」。
- 省略必须**可见**（逐条原因 + 计数）；省略**不是失败**——渲染器保持**全函数**
  （任何输入都不抛异常），且**确定性**（同输入 → 逐字节同输出）。
- 呈现文案**必含**「未验证」语义；**不含**任何「已上传 / 已存储 / 已下载」等完成态措辞。

边界：本模块**刻意不 import** 任何 I/O / 框架 / `app.persistence`，也**不 import**
`app.domain.artifact`——它对 artifact 采用**鸭子类型**读取（`getattr` + 缺省值），
因此本模块的 import 面**仅 stdlib**（与 `app/domain/outbound_response.py` 的
纯 stdlib 纪律一致），且对畸形元素**不抛异常**。
"""

import unicodedata

from app.domain.artifact_uri_policy import (
    DEFAULT_MAX_URI_LENGTH,
    UNSAFE_KIND_CONTROL,
    UNSAFE_KIND_SEPARATOR,
    URI_OMISSION_MESSAGES,
    classify_artifact_uri,
    unsafe_character_kind,
)

#: 呈现块抬头（**必含**「未验证」语义；不含任何完成态措辞）。
ARTIFACT_REFERENCE_HEADER = "Agent 提供的文件引用（ACP 未验证其可用性或安全性）："

#: 单次最多呈现的 artifact 数（Phase 1 **显式设计值**，仓库现无同类限制）。
MAX_PRESENTED_ARTIFACTS = 10

#: 自由文本展示字段（name / kind / mime_type）的字符上限（Phase 1 **显式设计值**）。
MAX_DISPLAY_TEXT_CHARS = 128

#: 引用块总字符上限（Phase 1 **显式设计值**；**不是**任何渠道平台侧上限）。
MAX_REFERENCE_BLOCK_CHARS = 2000

#: 为「未呈现」提示预留的字符数（保证提示本身不会被展示预算挤掉）。
_OMISSION_NOTICE_RESERVE_CHARS = 240

#: 自由文本中被替换为空格的字符类别（控制字符 Cc / 格式字符 Cf /
#: 行分隔符 Zl / 段分隔符 Zp —— 后两者亦为**不可见字形**，可造成视觉换行）。
_UNSAFE_TEXT_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})

#: 自由文本中必须替换为空格的标记字符（避免拼出 Markdown / HTML 结构）。
_UNSAFE_TEXT_CHARS = frozenset("[]<>`")

#: URI 中出现即**整体省略**的标记字符（URI 不可分割，不能靠消毒规避）。
_UNSAFE_URI_CHARS = frozenset("[]<>`")

#: `mime_type` / `size_bytes` 是 **Agent 声明值**（ACP **未**验证）⇒ 呈现必须标注。
_DECLARED_VALUES_NOTE = "（mime/大小为 Agent 声明值，未验证）"

#: 策略拒绝且原因码未登记时的兜底文案（**不含** URI 任何片段）。
_POLICY_FALLBACK_MESSAGE = "引用地址不符合呈现策略"

#: URI 省略原因（固定文案；供调用方与测试断言）。
_URI_OMISSION_FORMAT = "引用格式非法"
_URI_OMISSION_SEPARATOR = "引用包含行/段分隔符"
_URI_OMISSION_MARKUP = "引用包含不适合展示的字符"
_URI_OMISSION_BUDGET = "超出可展示预算"

#: URI 省略时使用的行文案（**不含** URI 的任何片段）。
_URI_OMISSION_LINE = "   引用未在消息中呈现（{reason}）"

#: 无可用引用时的行文案（`uri` 为 None / 空）。
_URI_ABSENT_LINE = "   未提供可用引用"

#: 引用行的行首缩进（3 空格：Markdown 列表续行，且不触发 4 空格代码块）。
_URI_LINE_INDENT = "   "


def _is_unsafe_text_char(ch: str) -> bool:
    """该字符是否必须从自由文本中剔除（控制 / 格式 / 标记字符）。"""
    return ch in _UNSAFE_TEXT_CHARS or unicodedata.category(ch) in _UNSAFE_TEXT_CATEGORIES


def _sanitize_display_text(value: object) -> str:
    """自由文本 → 安全展示文本（控制 / 标记字符 → 空格）。非 str → 空串。"""
    if not isinstance(value, str):
        return ""
    return "".join(" " if _is_unsafe_text_char(ch) else ch for ch in value)


def _cap_display_text(value: str) -> str:
    """超长 → 截断并显式标注（`…`）；截断后总长恰为上限。"""
    if len(value) <= MAX_DISPLAY_TEXT_CHARS:
        return value
    return value[: MAX_DISPLAY_TEXT_CHARS - 1] + "…"


def _display_field(value: object, fallback: str) -> str:
    """自由文本展示字段：净化 → 去首尾空白 → 截断标注；空 → fallback。"""
    text = _sanitize_display_text(value).strip()
    if not text:
        return fallback
    return _cap_display_text(text)


def _display_size(size_bytes: object) -> str:
    """字节数 → 展示文本；非法 / 负数 / 超大（≥ 1 PB）→ 「大小未知」。"""
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int):
        return "大小未知"
    if size_bytes < 0 or size_bytes >= 10 ** 15:
        return "大小未知"
    return f"{size_bytes} 字节"


def _classify_uri(uri: object) -> str:
    """URI → Phase 1 呈现状态（**字符 / 标记**维度）。

    - ``present``：无不安全字符、无标记字符；
    - ``absent``：没有可用引用（None / 非 str / 纯空白）；
    - ``omit_format``：含 Cc / Cf 字符（控制符 / 格式符）；
    - ``omit_separator``：含 Zl / Zp 字符（行 / 段分隔符）；
    - ``omit_markup``：含标记字符（可能拼出 Markdown / HTML 结构）。

    **策略维度**（scheme / 绝对性 / userinfo / host / port / 结构 / 长度）由
    `artifact_uri_policy.classify_artifact_uri` 判定（见 `_policy_reason`）。
    """
    if not isinstance(uri, str) or not uri.strip():
        return "absent"
    kind = unsafe_character_kind(uri)
    if kind == UNSAFE_KIND_CONTROL:
        return "omit_format"
    if kind == UNSAFE_KIND_SEPARATOR:
        return "omit_separator"
    if any(ch in _UNSAFE_URI_CHARS for ch in uri):
        return "omit_markup"
    return "present"


def _uri_verdict_reason(uri: object, *, max_uri_length: int) -> str | None:
    """策略维度判定：返回**拒绝原因码**；可呈现 ⇒ ``None``。

    「可呈现」**不等于**「可访问 / 可信 / 已授权」—— 本判定只判结构与协议。

    命名说明：刻意**不含** `policy` 等词——仓库的「禁用概念扫描」把 `Policy`
    保留给授权 / RBAC 语义（ADR-4 / ADR-9 NON-GOAL）；本函数与授权无关。
    """
    verdict = classify_artifact_uri(uri, max_length=max_uri_length)
    if verdict.presentable:
        return None
    return verdict.reason


def _artifact_line(artifact: object, index: int) -> str:
    """单个 artifact 的首行（序号 + 名称 + 类型 + MIME + 大小）。

    `mime_type` / `size_bytes` 是 **Agent 声明值**（ACP **未**验证）⇒ 一旦呈现，
    **必须**标注「Agent 声明值，未验证」，不得让用户读成已验证的文件事实。
    """
    name = _display_field(getattr(artifact, "name", None), "未命名")
    kind = _display_field(getattr(artifact, "kind", None), "未标注")
    mime_raw = getattr(artifact, "mime_type", None)
    mime = _display_field(mime_raw, "类型未知")
    size_raw = getattr(artifact, "size_bytes", None)
    size = _display_size(size_raw)
    declared = ""
    if (isinstance(mime_raw, str) and mime_raw.strip()) \
            or (isinstance(size_raw, int) and not isinstance(size_raw, bool)):
        declared = _DECLARED_VALUES_NOTE
    return f"{index}. {name}（{kind}｜{mime}｜{size}）{declared}"


def _uri_line(artifact: object, state: str, reason: str | None = None) -> str:
    """单个 artifact 的 URI 行（按状态给出**确定**文案）。"""
    if state == "present":
        uri = getattr(artifact, "uri", None)
        # 代码跨度：非 Markdown 链接、非 HTML 链接；URI 本身**逐字节原样**。
        return f"{_URI_LINE_INDENT}引用：`{uri}`"
    if state == "absent":
        return _URI_ABSENT_LINE
    if state == "omit_format":
        return _URI_OMISSION_LINE.format(reason=_URI_OMISSION_FORMAT)
    if state == "omit_separator":
        return _URI_OMISSION_LINE.format(reason=_URI_OMISSION_SEPARATOR)
    if state == "omit_markup":
        return _URI_OMISSION_LINE.format(reason=_URI_OMISSION_MARKUP)
    if state == "omit_policy":
        return _URI_OMISSION_LINE.format(reason=reason or _POLICY_FALLBACK_MESSAGE)
    return _URI_OMISSION_LINE.format(reason=_URI_OMISSION_BUDGET)


def _omission_notice(omitted_artifacts: int, omitted_uris: int, budget_hit: bool) -> str:
    """未呈现的计数提示（**可见**：不静默丢弃）。"""
    segments: list[str] = []
    if omitted_artifacts:
        segments.append(f"另有 {omitted_artifacts} 条引用未在消息中呈现")
    if omitted_uris:
        segments.append(f"{omitted_uris} 条引用的地址未在消息中呈现")
    if budget_hit:
        segments.append("引用块已截断")
    if not segments:
        return ""
    return "（" + "；".join(segments) + "）"


def render_artifact_references(
    artifacts: object, *, max_uri_length: int = DEFAULT_MAX_URI_LENGTH
) -> str:
    """把 artifact 列表渲染为**确定性**引用文本块；无 artifact → 空串。

    **全函数**：任何输入（None / 空列表 / 畸形元素 / 超长字符串 / 非 str 字段）
    都不抛异常。非列表输入 → 空串（调用侧视为「无引用」）。

    :param max_uri_length: URI 长度上限（策略层，超过即**整体省略**，不截断）。
    """
    if not isinstance(artifacts, list) or not artifacts:
        return ""
    lines: list[str] = [ARTIFACT_REFERENCE_HEADER]
    used = len(ARTIFACT_REFERENCE_HEADER) + 1
    available = MAX_REFERENCE_BLOCK_CHARS - _OMISSION_NOTICE_RESERVE_CHARS
    included = 0
    omitted_artifacts = 0
    omitted_uris = 0
    budget_hit = False
    for artifact in artifacts:
        if included >= MAX_PRESENTED_ARTIFACTS:
            omitted_artifacts += 1
            continue
        head = _artifact_line(artifact, included + 1)
        if used + len(head) + 1 > available:
            omitted_artifacts += 1
            budget_hit = True
            continue
        state = _classify_uri(getattr(artifact, "uri", None))
        reason = None
        if state == "present":
            # Phase 2 策略闸门：协议 / 绝对性 / userinfo / host / port / 结构 / 长度。
            # 不可呈现 ⇒ **整体省略**（不输出 URI 任何片段）。
            policy_reason = _uri_verdict_reason(
                getattr(artifact, "uri", None), max_uri_length=max_uri_length)
            if policy_reason is not None:
                state = "omit_policy"
                reason = URI_OMISSION_MESSAGES.get(
                    policy_reason, _POLICY_FALLBACK_MESSAGE)
        tail = _uri_line(artifact, state, reason)
        if state == "present" and used + len(head) + 1 + len(tail) + 1 > available:
            # URI **不可分割**：放不下就**整体省略**，绝不截断后当作地址呈现。
            state = "omit_budget"
            tail = _uri_line(artifact, state)
        lines.append(head)
        used += len(head) + 1
        lines.append(tail)
        used += len(tail) + 1
        included += 1
        if state in ("omit_format", "omit_markup", "omit_policy", "omit_budget"):
            omitted_uris += 1
    notice = _omission_notice(omitted_artifacts, omitted_uris, budget_hit)
    if notice:
        lines.append(notice)
    return "\n".join(lines)


def render_outbound_content(
    content: object, artifacts: object, *, max_uri_length: int = DEFAULT_MAX_URI_LENGTH
) -> str:
    """出站文本 = 原始 content（+ 引用块，**仅当**确有引用块）。

    无 artifact → **原样返回** content（逐字节一致，旧出站消息零变化）。
    """
    if not isinstance(content, str):
        return content
    block = render_artifact_references(artifacts, max_uri_length=max_uri_length)
    if not block:
        return content
    return content + "\n\n" + block


def artifact_reference_records(artifacts: object) -> list[dict]:
    """artifact 列表 → **结构化**引用记录（**完整原值**，不做展示层截断/消毒）。

    形状与 Interaction 响应快照中 artifact 列表的显式字段映射一致
    （`artifact_id` / `kind` / `name` / `uri` / `mime_type` / `size_bytes` /
    `metadata`）——供 Local outbox 的 `artifacts` 键使用（L4：展示预算**不作用于此**）。
    """
    if not isinstance(artifacts, list):
        return []
    return [
        {
            "artifact_id": getattr(artifact, "artifact_id", None),
            "kind": getattr(artifact, "kind", None),
            "name": getattr(artifact, "name", None),
            "uri": getattr(artifact, "uri", None),
            "mime_type": getattr(artifact, "mime_type", None),
            "size_bytes": getattr(artifact, "size_bytes", None),
            "metadata": getattr(artifact, "metadata", None),
        }
        for artifact in artifacts
    ]
