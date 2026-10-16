"""OSS 导出感知测试边界（B3.3/B3.3.1）—— 仓库来源判定 + frontend 消费分析。

决策依据：docs/strategy/oss-vs-cloud-boundary.md（A1/B1）。

两部分契约：
1. **三态 provenance**（repo_provenance）：source / oss_export / unknown（保守）。
2. **frontend 消费分析**（frontend_consuming_tests）：对任一测试文件做 AST 级
   函数消费分析——某测试函数「消费 frontend」= 其自身代码段（或其调用链上的
   模块级 helper）出现 ``"frontend"`` 字符串常量（路径构造）或引用了由
   ``"frontend"`` 构造的模块级变量（如 ``FRONTEND``/``HOME_JS``/``WS_HTML``）。
   conftest 据此在有效 oss_export 下做**函数级**收集跳过；source 下全跑；
   unknown 下不跳过（缺失文件显式失败）。

unknown 保守纪律：绝不静默跳过检查——依赖被排除文件的测试在 unknown 下
照常执行并显式失败；操作者须提供正证据（完整源仓库或带导出标记的树）。

导出标记契约（供未来快照生成流程）：导出树根写入非空 ``OSS_EXPORT_MANIFEST.txt``
（至少含 source_commit/generated/excludes 键值行）；探测只要求存在且非空。
"""

from __future__ import annotations

import ast
import functools
from pathlib import Path

import pytest

# backend/tests/oss_export_boundary.py → parents[2] = 仓库根
ROOT = Path(__file__).resolve().parents[2]

SOURCE_MARKER = ROOT / ".oss-export-exclude.txt"
EXPORT_MARKER = ROOT / "OSS_EXPORT_MANIFEST.txt"
FRONTEND_DIR = ROOT / "frontend"
FRONTEND_LITERAL = "frontend"  # 路径构造中的字面量段（ast.Constant 精确匹配）

SOURCE = "source"
OSS_EXPORT = "oss_export"
UNKNOWN_PROVENANCE = "unknown"
UNKNOWN = UNKNOWN_PROVENANCE  # 兼容别名


def repo_provenance(root: Path | None = None) -> str:
    """判定仓库来源（三态，保守，正证据优先）。

    ``root`` 仅供本契约单元测试注入临时树。
    """
    root = ROOT if root is None else root
    has_source = (root / SOURCE_MARKER.name).is_file()
    has_export = (root / EXPORT_MARKER.name).is_file()
    if has_source and not has_export:
        return SOURCE
    if has_export and not has_source and not (root / "frontend").is_dir():
        # 导出完整性：带标记却仍含 frontend/ = 矛盾状态 → 不可信
        return OSS_EXPORT
    return UNKNOWN


def oss_ui_required() -> pytest.MarkDecorator:
    """A1 标记：显式声明某测试消费 Cloud UI / frontend 资源。

    source 正常执行；oss_export 跳过；unknown **不跳过**（显式失败）。
    与 conftest 的 AST 分析互为冗余防线。
    """
    return pytest.mark.skipif(
        repo_provenance() == OSS_EXPORT,
        reason=(
            "A1: 依赖 frontend/（Cloud Web Console，初始 OSS 导出排除）；"
            "仅完整源仓库执行"
        ),
    )


# ==========================================================================
# frontend 消费分析（AST，调用闭包传递）
# ==========================================================================


def _has_frontend_literal(node: ast.AST) -> bool:
    """段内是否出现精确的 ``"frontend"`` 字符串常量（路径构造特征）。"""
    return any(
        isinstance(n, ast.Constant) and n.value == FRONTEND_LITERAL
        for n in ast.walk(node)
    )


def _segment_source(lines: list[str], node: ast.AST) -> str:
    return "\n".join(lines[(node.lineno - 1): (getattr(node, "end_lineno", node.lineno))])


def frontend_consuming_tests(path: Path) -> frozenset[str]:
    """返回该测试文件中消费 frontend 的测试函数名集合（AST + 调用闭包）。

    判定规则：
    - 模块级赋值目标（如 ``FRONTEND``/``HOME_JS``/``WS_HTML``）若其右侧段含
      ``"frontend"`` 字面量，记为 frontend 变量；
    - 某函数的代码段（排除无关命中）出现 frontend 变量名、``"frontend"``
      字面量，或调用了消费 frontend 的其他顶层函数（传递闭包）→ 消费。
    """
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    tree = ast.parse(source)

    top_funcs: dict[str, ast.FunctionDef] = {}
    fe_vars: set[str] = set()
    assignments = [n for n in tree.body if isinstance(n, ast.Assign)]
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            top_funcs[node.name] = node
        elif isinstance(node, ast.Assign) and _has_frontend_literal(node):
            for t in node.targets:
                for n in ast.walk(t):
                    if isinstance(n, ast.Name):
                        fe_vars.add(n.id)
    # 变量传播（不动点）：右侧引用 frontend 变量的赋值，其目标同为 frontend 变量
    # （如 HOME_JS = FRONTEND / "home.js" —— 链式派生路径）。
    changed = True
    while changed:
        changed = False
        for a in assignments:
            if any(isinstance(n, ast.Name) and n.id in fe_vars for n in ast.walk(a.value)):
                for t in a.targets:
                    for n in ast.walk(t):
                        if isinstance(n, ast.Name) and n.id not in fe_vars:
                            fe_vars.add(n.id)
                            changed = True

    def direct_fe(node: ast.AST) -> bool:
        for n in ast.walk(node):
            if isinstance(n, ast.Constant) and n.value == FRONTEND_LITERAL:
                return True
            if isinstance(n, ast.Name) and n.id in fe_vars:
                return True
        return False

    # 传递闭包：helper 消费 frontend 当且仅当自身段直接消费，或调用（经闭包）
    # 消费 frontend 的 helper。
    calls: dict[str, set[str]] = {}
    for name, fn in top_funcs.items():
        callees = set()
        for n in ast.walk(fn):
            if isinstance(n, ast.Call):
                callee = n.func
                if isinstance(callee, ast.Name) and callee.id in top_funcs:
                    callees.add(callee.id)
                elif isinstance(callee, ast.Attribute) and callee.attr in top_funcs:
                    callees.add(callee.attr)
        calls[name] = callees

    fe_funcs: set[str] = set()

    def consumes(name: str, seen: frozenset[str] = frozenset()) -> bool:
        if name in fe_funcs:
            return True
        if name in seen or name not in top_funcs:
            return False
        if direct_fe(top_funcs[name]):
            fe_funcs.add(name)
            return True
        seen = seen | {name}
        for callee in calls.get(name, ()):
            if callee != name and consumes(callee, seen):
                fe_funcs.add(name)
                return True
        return False

    consuming = {n for n in top_funcs if n.startswith("test_") and consumes(n)}
    return frozenset(consuming)


@functools.lru_cache(maxsize=None)
def _consuming_tests_cached(path_str: str) -> frozenset[str]:
    return frontend_consuming_tests(Path(path_str))


def consuming_tests_for(path: Path | str) -> frozenset[str]:
    """带缓存的对外入口（conftest 收集期按文件调用）。"""
    return _consuming_tests_cached(str(path))
