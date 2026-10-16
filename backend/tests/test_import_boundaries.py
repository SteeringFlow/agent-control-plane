"""验收 D：导入边界无反向依赖（AST 机器化校验，不靠人工 Review）。

为什么用 AST 而不是文本搜索：docstring / 注释里经常会写「本层不 import SQLAlchemy」
这类否定式说明，文本匹配会把这些说明误判成真实违规。

规则表达的是「该层不得 import 什么」：

    domain      -> 不得出现 SQLAlchemy / FastAPI / starlette /
                   app.schemas（Pydantic API Schema）/ app.api /
                   app.application / app.persistence / app.adapters / app.repositories
    application -> 不得出现 FastAPI / starlette / SQLAlchemy /
                   app.api / app.persistence / app.adapters
    repositories -> 不得出现 SQLAlchemy（端口层只依赖 domain）/
                    FastAPI / starlette / app.persistence / app.adapters
    api          -> 不得直接 import SQLAlchemy，也不得碰 ORM 模型 / mapper /
                    具体仓储实现——DB 只能经 app.persistence.database.get_db
                    这一基础设施依赖机制获取（Step 5.2.12）
    persistence  -> 唯一允许 SQLAlchemy 的业务相关层；不得反向依赖
                    FastAPI / api / schemas / application / adapters
                    （依赖 repositories 端口 = 实现端口，允许，Step 5.3）

注意方向：persistence 可以依赖 repositories（实现端口），
但 repositories 绝不依赖 persistence——由下两条规则共同锁死。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

# backend/tests/ -> backend/ -> 取 backend/app
APP_DIR = Path(__file__).resolve().parents[1] / "app"

LAYER_RULES: dict[str, tuple[str, ...]] = {
    "domain": (
        "sqlalchemy",
        "fastapi",
        "starlette",
        "app.schemas",
        "app.api",
        "app.application",
        "app.persistence",
        "app.adapters",
        "app.repositories",
    ),
    "application": (
        "fastapi",
        "starlette",
        "sqlalchemy",
        "app.api",
        "app.persistence",
        "app.adapters",
    ),
    "repositories": (
        "sqlalchemy",
        "fastapi",
        "starlette",
        "app.api",
        "app.schemas",
        "app.application",
        "app.persistence",
        "app.adapters",
    ),
    # api 允许 import app.persistence.database（get_db 基础设施依赖机制），
    # 但不允许直接 import SQLAlchemy，也不允许操作具体业务持久化对象。
    "api": (
        "sqlalchemy",
        "app.persistence.models",
        "app.persistence.mappers",
        "app.persistence.repositories",
    ),
    # persistence 是唯一允许 SQLAlchemy 的业务相关层（models/database），
    # 但不得反向依赖任何外层：fastapi / api / schemas / application / adapters。
    # （persistence 依赖 repositories 端口是允许的——那是实现端口。）
    "persistence": (
        "fastapi",
        "starlette",
        "app.api",
        "app.schemas",
        "app.application",
        "app.adapters",
    ),
}


def _python_files(layer_dir: Path) -> list[Path]:
    return sorted(layer_dir.rglob("*.py"))


def _dotted_module(py_file: Path) -> tuple[str, bool]:
    """返回 (模块的点分路径, 是否为 __init__.py)。"""
    relative = py_file.relative_to(APP_DIR.parent)
    parts = list(relative.parts)
    is_init = parts[-1] == "__init__.py"
    if is_init:
        parts.pop()
    else:
        parts[-1] = parts[-1][: -len(".py")]
    return ".".join(parts), is_init


def _resolve_relative(level: int, module: str | None, anchor: str) -> str:
    """把 `from ..x import y` 解析成绝对模块名，便于与规则比较。"""
    parts = anchor.split(".")
    if level > 1:
        parts = parts[: -(level - 1)] if level - 1 <= len(parts) else []
    base = ".".join(parts)
    if module:
        return f"{base}.{module}" if base else module
    return base


def _imported_roots(py_file: Path) -> set[str]:
    """收集文件中所有被 import 的模块根（含相对导入解析后的绝对名）。"""
    tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    dotted, is_init = _dotted_module(py_file)
    anchor = dotted if is_init else dotted.rpartition(".")[0]

    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                roots.add(_resolve_relative(node.level, node.module, anchor))
            elif node.module:
                roots.add(node.module)
    return roots


def _violations(root: str, forbidden: tuple[str, ...]) -> list[str]:
    return [name for name in forbidden if root == name or root.startswith(name + ".")]


@pytest.mark.parametrize("layer", sorted(LAYER_RULES))
def test_layer_has_no_reverse_dependency(layer: str) -> None:
    layer_dir = APP_DIR / layer
    files = _python_files(layer_dir)
    # 空扫描会假绿：目录被误删/改名时必须失败，而不是「没有文件所以通过」
    assert files, f"{layer} 层没有任何 Python 文件，目录基线可能被破坏"

    forbidden = LAYER_RULES[layer]
    problems: list[str] = []
    for py_file in files:
        for root in sorted(_imported_roots(py_file)):
            for hit in _violations(root, forbidden):
                problems.append(f"{py_file.relative_to(APP_DIR.parent)} imports {root} (禁止: {hit})")

    assert not problems, f"{layer} 层出现反向依赖：\n" + "\n".join(problems)


@pytest.mark.parametrize("layer", sorted(set(LAYER_RULES) - {"application"}))
def test_layer_has_no_premature_service_implementation(layer: str) -> None:
    """5.1.3 控制点（更新）：*Service 只允许出现在 application 层。

    Step 6.1 起 application 是 Application Service 的正当归属（如
    AgentApplicationService），因此本守卫不再扫描 application；
    domain/repositories/api/persistence 出现 *Service 类即视为漂移。
    """
    offenders: list[str] = []
    for py_file in _python_files(APP_DIR / layer):
        if py_file.name.endswith("_service.py"):
            offenders.append(str(py_file.relative_to(APP_DIR.parent)))
            continue
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name.endswith("Service"):
                offenders.append(
                    f"{py_file.relative_to(APP_DIR.parent)}::{node.name}"
                )

    assert not offenders, (
        f"{layer} 层出现了过早实现的 Service（应先有 Vertical Slice 需求）：\n"
        + "\n".join(offenders)
    )


def test_directory_baseline_exists() -> None:
    """目录即架构边界：基线目录缺失必须立刻失败。"""
    expected = [
        "app/api/management",
        "app/api/runtime",
        "app/application",
        "app/domain",
        "app/adapters/channels",
        "app/adapters/agents",
        "app/repositories",
        "app/persistence/models",
        "app/persistence/mappers",
        "app/persistence/repositories",
        "app/schemas",
        "app/core",
    ]
    missing = [p for p in expected if not (APP_DIR.parent / p).is_dir()]
    assert not missing, f"目录基线缺失：{missing}"
