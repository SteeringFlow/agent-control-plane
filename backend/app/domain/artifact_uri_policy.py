"""Artifact URI 策略（ACP V0.4 Phase 2）—— 纯标准库分类器。

定位：对 Agent 提供的 `Artifact.uri` 做**确定性的结构与策略判定**，回答**一个问题**：

    这个 URI 是否允许被呈现为「可点击的引用」？

**它不回答**（这些是彼此独立的能力，本阶段**全部不做**，不得混淆）：

- URI 是否**可达** —— 本模块**零**网络调用，从不发起任何请求；
- 目标是否为**私网 / 环回 / 元数据端点** —— 属「取回前置条件」，本阶段**不做**；
- 文件内容 / MIME / 大小是否**可信** —— `mime_type` / `size_bytes` 是 **Agent 声明值**，
  ACP **未**验证；
- **谁**有权访问该文件 —— ACP **不提供**文件级授权模型；
- 是否由 ACP **下载 / 代理 / 缓存 / 存储**该文件 —— 本阶段**零**字节传输。

因此「可呈现」**不等于**「可访问」**不等于**「可信」**不等于**「已授权」。
**URL 可访问不代表用户有权访问；HTTP 200 不代表文件可信。**

安全策略（**固定**，**不可**经配置放开）：

- scheme 仅允许 `https` / `http`（大小写不敏感）；
- 拒绝**相对 URL**（无 scheme）；
- 拒绝**含 userinfo**（`user:pass@`）的 URL；
- 拒绝**缺主机**、**非法端口**（含越界）、**结构非法**的 URL；
- 拒绝含 `Cc` / `Cf` / `Zl` / `Zp` 字符类别的 URI；
- URI 长度超过 **2048** ⇒ 拒绝呈现（**不截断**、**不输出任何片段**）。

判定使用标准库 `urllib.parse.urlsplit`（**不**用过宽的正则代替 URL 解析）；
**无法可靠分类的异常输入一律 fail closed**（拒绝呈现）。

本模块**纯 stdlib**、**零 I/O**、**确定性**、**全函数**（任何输入都不抛异常）。
"""

import ipaddress
import unicodedata
from dataclasses import dataclass
from urllib.parse import urlsplit

#: 允许呈现的 scheme（**固定**安全策略；**不**提供放开任意 scheme 的开关）。
ALLOWED_URI_SCHEMES = frozenset({"https", "http"})

#: URI 长度上限（Phase 2 **显式设计值**）。
#:
#: **不**做成配置项（`app/core/config.py`）：长度上限作用于一个**简单纯函数**，
#: 配置化需把值注入渲染路径（三个 Channel Adapter 构造函数 + 组合根守卫），
#: 属超出本阶段范围的接线工作；且渲染层必须保持**纯 stdlib**（见 Phase 1 C12）。
#: 待出现真实调参需求时，再以独立 Gate 引入配置项并**同时**补上启动期 fail-fast 校验。
DEFAULT_MAX_URI_LENGTH = 2048

#: 不安全字符的类别细分（Phase 1 的呈现层沿用它区分两种原因文案）。
UNSAFE_KIND_CONTROL = "control"        # Cc（控制符）/ Cf（格式符）
UNSAFE_KIND_SEPARATOR = "separator"    # Zl（行分隔符）/ Zp（段分隔符）

# ---- 分类原因（稳定语义；**不携带** URI 任何片段）----------------------

#: 没有可用引用（None / 非 str / 纯空白）。
REASON_NO_REFERENCE = "no_reference"
#: 不是绝对 URL（无 scheme；含协议相对 URL）。
REASON_NOT_ABSOLUTE = "not_absolute"
#: scheme 不在允许集合内（`javascript:` / `data:` / `file:` 等）。
REASON_SCHEME = "unsupported_scheme"
#: URL 含 userinfo（`user:pass@`）。
REASON_USERINFO = "contains_userinfo"
#: 主机缺失或结构非法。
REASON_HOST = "invalid_host"
#: 端口非法（非法值或越界）。
REASON_PORT = "invalid_port"
#: URL 整体结构非法（异常分隔符 / 括号不配对等）。
REASON_STRUCTURE = "invalid_structure"
#: 含 `Cc` / `Cf` / `Zl` / `Zp` 字符。
REASON_UNSAFE_CHARS = "unsafe_characters"
#: 超过长度上限。
REASON_TOO_LONG = "too_long"

#: 原因 → 呈现层省略提示文案（**不含** URI 任何片段；**不**暗示可点击地址）。
URI_OMISSION_MESSAGES = {
    REASON_NOT_ABSOLUTE: "引用不是绝对地址",
    REASON_SCHEME: "引用使用了不受支持的协议",
    REASON_USERINFO: "引用地址包含用户名或密码",
    REASON_HOST: "引用地址缺少有效主机",
    REASON_PORT: "引用地址端口无效",
    REASON_STRUCTURE: "引用地址结构无效",
    REASON_UNSAFE_CHARS: "引用包含不适合展示的字符",
    REASON_TOO_LONG: "引用地址过长",
}


@dataclass(frozen=True)
class UriClassification:
    """URI 策略分类结果（稳定语义；**不携带** URI 片段）。

    - ``presentable=True`` ⇒ 允许作为引用呈现（**仍不代表**可达 / 可信 / 已授权）；
    - ``presentable=False`` ⇒ 拒绝呈现，``reason`` 给出**稳定**原因码。
    """

    presentable: bool
    reason: str | None = None


def unsafe_character_kind(text: str) -> str | None:
    """文本中首个不安全字符的类别。

    返回 ``"control"``（Cc 控制符 / Cf 格式符）、``"separator"``（Zl 行分隔符 /
    Zp 段分隔符）或 ``None``（无不安全字符）。**全函数**。
    """
    if not isinstance(text, str):
        return None
    for ch in text:
        category = unicodedata.category(ch)
        if category in ("Cc", "Cf"):
            return UNSAFE_KIND_CONTROL
        if category in ("Zl", "Zp"):
            return UNSAFE_KIND_SEPARATOR
    return None


def _host_is_valid(host: str) -> bool:
    """主机结构校验。

    **只判结构，不判网络位置**——私网 / 环回 / 元数据端点的判定属「取回前置条件」，
    本阶段**不做**（本模块从不发起请求）。
    """
    if not host:
        return False
    if ":" in host:                      # IPv6 字面量（`hostname` 已剥离方括号）
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            return False
        return True
    for ch in host:
        if not (ch.isascii() and (ch.isalnum() or ch in "-._")):
            return False
    if host.startswith(".") or host.endswith("."):
        return False
    if ".." in host:
        return False
    if host.startswith("-") or host.endswith("-"):
        return False
    return True


def classify_artifact_uri(
    uri: object, *, max_length: int = DEFAULT_MAX_URI_LENGTH
) -> UriClassification:
    """把 Artifact URI 分类为「可呈现」或「拒绝呈现 + 稳定原因」。

    **全函数**：任何输入都不抛异常；无法可靠分类 ⇒ **fail closed**（拒绝呈现）。

    :param uri: Agent 提供的 URI 原值（可为 ``None``）。
    :param max_length: URI 长度上限（超过即拒绝，**不截断**）。
    """
    if not isinstance(uri, str) or not uri.strip():
        return UriClassification(False, REASON_NO_REFERENCE)
    if len(uri) > max_length:
        return UriClassification(False, REASON_TOO_LONG)
    if unsafe_character_kind(uri) is not None:
        return UriClassification(False, REASON_UNSAFE_CHARS)
    if any(ch.isspace() for ch in uri):
        return UriClassification(False, REASON_STRUCTURE)
    if "\\" in uri:                      # 反斜杠是异常分隔符（解析器间语义不一致）
        return UriClassification(False, REASON_STRUCTURE)
    try:
        parts = urlsplit(uri)
    except ValueError:
        return UriClassification(False, REASON_STRUCTURE)
    scheme = (parts.scheme or "").lower()
    if not scheme:
        return UriClassification(False, REASON_NOT_ABSOLUTE)
    if scheme not in ALLOWED_URI_SCHEMES:
        return UriClassification(False, REASON_SCHEME)
    if "@" in parts.netloc or parts.username or parts.password:
        return UriClassification(False, REASON_USERINFO)
    try:
        host = parts.hostname
    except ValueError:
        return UriClassification(False, REASON_HOST)
    if not host or not _host_is_valid(host):
        return UriClassification(False, REASON_HOST)
    try:
        port = parts.port
    except ValueError:
        return UriClassification(False, REASON_PORT)
    if port is not None and not (1 <= port <= 65535):
        return UriClassification(False, REASON_PORT)
    return UriClassification(True, None)
