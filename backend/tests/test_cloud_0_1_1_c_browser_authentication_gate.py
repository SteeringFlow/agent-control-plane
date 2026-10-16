"""Cloud-0.1.1-C Browser Authentication —— Architecture Gate（静态护栏）。

本 Gate 的中心约束（见 docs/step-cloud-0.1.1-C-browser-authentication-gate.md）：

  Browser → Who is this user? → Which Workspace/Tenant? → What Cloud resources?

C 只负责前两环（操作员身份 + 派生 tenant_id）；后两环复用 B 的 Tenant Isolation。
本测试是**设计期静态护栏**，旨在把 C 的硬约束机器化锁定，当前即可绿灯：

  - §3 七大禁区（OAuth Platform / SSO / RBAC / Organization / SCIM / Enterprise IAM / JWT complexity）
    绝不被顺手引入；
  - §2 四者严格分离（Browser / WeCom / Agent Connection / Tenant Isolation）；
  - §8 红线 D/F：User 域实体与 users ORM 保持干净；OSS/本地 TENANT_LOCAL 回退保留。

进入实现期后，本文件在保持绿的前提下，按 Gate §10 追加「实现后不变量」断言。
"""

import dataclasses
from pathlib import Path

from app.core.tenant import TENANT_LOCAL, TenantScope, default_tenant_scope
from app.domain.user import User

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"


# ---------------------------------------------------------------- helpers

import io
import tokenize


def _backend_sources() -> list[Path]:
    out: list[Path] = []
    for py in APP.rglob("*.py"):
        if "__pycache__" in py.parts:
            continue
        out.append(py)
    return out


def _code_without_prose(path: Path) -> str:
    """只返回「真实代码标识符」，剥离注释与字符串字面量。

    关键：backend 的 docstring/注释里大量出现『不引入 OAuth / 不是 RBAC』之类的
    *拒斥* 性表述——这些恰恰是 Gate 纪律的证据，不应触发护栏。只有作为**实际代码
    标识符/导入**出现的越界符号才应被拦截。
    """
    src = path.read_text(encoding="utf-8")
    try:
        toks = tokenize.generate_tokens(io.StringIO(src).readline)
    except Exception:
        return src  # 解析失败则回退为原文（宁可误报而非漏报）
    names = [tok.string for tok in toks if tok.type == tokenize.NAME]
    return " ".join(names)


def _all_code_text() -> str:
    return "\n".join(_code_without_prose(p) for p in _backend_sources())


def _code_contains_word(word: str) -> bool:
    """在剥离 prose 后的真实代码标识符中做整词匹配（大小写不敏感）。"""
    text = _all_code_text().lower()
    return f" {word.lower()} " in f" {text} "


# ---------------------------------------------------------------- §3 七大禁区


def test_no_forbidden_auth_concepts_in_backend() -> None:
    """静态锁死 §3：禁止引入 OAuth Platform / SSO / RBAC / Organization / SCIM / Enterprise IAM / JWT complexity。

    只扫描真实代码标识符（剥离注释/字符串），因此 docstring 中『不引入 OAuth /
    不是 RBAC』之类的拒斥性表述不会误报——那正是 Gate 纪律的证据。
    """
    forbidden = [
        "oauth",          # OAuth Platform / OP
        "sso",            # 单点登录
        "rbac",           # 角色权限引擎
        "scim",           # 用户供给协议
        "jwks",           # JWT 密钥集
        "jwt",            # JWT 库使用
        "refresh_token",  # 刷新令牌（JWT 复杂度家族）
        "organization_id","organisation_id",  # 组织层级建模
        "enterpriseiam",  # 企业身份（无空格，作为标识符）
    ]
    hits = [tok for tok in forbidden if _code_contains_word(tok)]
    assert not hits, f"backend 出现越界认证标识符：{hits}"


def test_browser_session_uses_opaque_cookie_not_jwt() -> None:
    """§3 JWT 禁区：backend 真实代码中不得出现 jwt 库使用 / 签名声明链。"""
    jwt_markers = ["jwt", "jwks", "refresh_token"]
    hits = [m for m in jwt_markers if _code_contains_word(m)]
    assert not hits, f"backend 出现 JWT 复杂度使用：{hits}"


# ---------------------------------------------------------------- §2 四者分离


def test_browser_auth_distinct_from_wecom_and_agent() -> None:
    """§2 护栏：WeCom 认证的模块（wecom.py）不得耦合 Browser Auth；三者命名空间分离。

    当前 wecom.py 只负责入站签名校验；若将来出现 app/core/browser_auth.py，
    它不得被 wecom.py import（反向耦合同样禁止）。
    """
    wecom_src = (APP / "adapters" / "channels" / "wecom.py").read_text(encoding="utf-8")
    assert "browser_auth" not in wecom_src, "wecom.py 不应 import/依赖 browser_auth"
    assert "app.core.browser_auth" not in wecom_src.replace(" ", ""), \
        "wecom.py 不应耦合 Browser Auth 模块"


# ---------------------------------------------------------------- §8 红线 D/F


def test_user_domain_has_no_credential_field() -> None:
    """§8 红线 D：User 域实体字段恰为 {name, id, status}，无密码/密钥/凭据字段。"""
    fields = {f.name for f in dataclasses.fields(User)}
    assert fields == {"name", "id", "status"}, f"User 域实体不应含凭据字段：{fields}"


def test_users_orm_has_no_inline_credential_column() -> None:
    """§7/§8：users ORM 列集不含 password/secret/credential（加性纪律）。"""
    from app.persistence import models as _models_pkg  # noqa: F401  注册表到 Base.metadata
    from app.persistence.database import Base

    cols = {c.name for c in Base.metadata.tables["users"].columns}
    credential_like = {"password", "password_hash", "secret", "credential", "api_key", "token"}
    overlap = cols & credential_like
    assert not overlap, f"users ORM 不应内联凭据列：{overlap}"


def test_tenant_isolation_reused_not_rebuilt() -> None:
    """§1 L3：浏览器认证路径复用 B 的 TenantScope，不为 Browser 新建第二套 tenant 原语。"""
    # TenantScope 来自 B 的 app.core.tenant（已建成），而非新建 browser tenant 类型。
    assert [f.name for f in dataclasses.fields(TenantScope)] == ["tenant_id"]
    # 不应出现 Browser 专属的「第二套租户原语」命名（真实代码标识符层面）。
    assert not _code_contains_word("browsertenant")
    assert not _code_contains_word("browser_tenant")


def test_local_single_tenant_fallback_preserved() -> None:
    """§8 红线 F：OSS/本地 TENANT_LOCAL 回退保留（既有行为逐字节不变）。"""
    assert TENANT_LOCAL == 1
    assert default_tenant_scope().tenant_id == TENANT_LOCAL
