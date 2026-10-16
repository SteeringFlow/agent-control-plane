"""Step 10.8.2 —— Runtime Execution Result Contract Design（契约定义步）。

把 10.8.1 裁决的三个事实正式定义成最小、稳定的 Domain contract：
`app/domain/runtime_execution_result.py::RuntimeExecutionResult`（frozen 三字段）。

本文件把八项契约裁决钉成机器化断言（用户指定的审计点逐项对应）：

A  字段面冻结：恰好 {session_mode, session_id, agent_response}，全部必填无默认
   （= 禁止未经审计扩展的机器化锁定）；
B  frozen 只读：三字段不可变；
C  session_mode 复用 8.3 常量（REUSE_ACTIVE / CREATE_NEW，单一事实源），
   不新建 Enum；
D  agent_response 直接复用现有 frozen AgentResponse（不复制不包装）；
E  成功路径不变量成文（agent_response 必非 None；session 恒存在）；
F  零接线：coordinator / runtime / wrapper 不引用本契约，execute() 仍 -> None，
   RUNTIME_EXECUTION_ORDER 不变；
G  domain 纯净：只依赖 dataclasses + app.domain.*；
H  导出面与契约成文。

生产者校验（Fail Closed）与接线属未来步（10.1.4 Freeze Rule 六问门槛）。
"""

import ast
import dataclasses
from pathlib import Path
from typing import get_type_hints

import pytest

from app.domain import RuntimeExecutionResult
from app.domain.agent_response import AgentResponse
from app.domain.runtime_execution_result import (
    CREATE_NEW as RER_CREATE_NEW,
    REUSE_ACTIVE as RER_REUSE_ACTIVE,
)
from app.domain.session_resolution import CREATE_NEW, REUSE_ACTIVE

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"
APPLICATION = APP / "application"
REPO_ROOT = BACKEND.parent
CONTRACT = APP / "domain" / "runtime_execution_result.py"
DOC = REPO_ROOT / "docs" / "step-10.8.2-runtime-execution-result-contract.md"


# ================================================================== A/B. 字段面冻结与只读


def test_a1_field_surface_is_exactly_three_required_fields() -> None:
    """字段面恰好三个、全部必填无默认——「禁止未经审计扩展」的机器化锁定。"""
    fs = dataclasses.fields(RuntimeExecutionResult)
    assert [f.name for f in fs] == ["session_mode", "session_id",
                                    "agent_response"]
    for f in fs:
        assert f.default is dataclasses.MISSING
        assert f.default_factory is dataclasses.MISSING
    hints = get_type_hints(RuntimeExecutionResult)
    assert hints["session_mode"] is str
    assert hints["session_id"] is int
    assert hints["agent_response"] is AgentResponse


def test_b1_all_fields_are_frozen_readonly() -> None:
    """三字段全部只读（frozen=True）：契约事实不可变。"""
    result = RuntimeExecutionResult(
        session_mode=REUSE_ACTIVE, session_id=7,
        agent_response=AgentResponse(content="hi"))
    assert RuntimeExecutionResult.__dataclass_params__.frozen is True
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.session_id = 8                        # type: ignore[misc]


# ================================================================== C/D. 复用而非新建


def test_c1_session_mode_reuses_existing_constants_no_new_enum() -> None:
    """session_mode 复用 8.3 常量（单一事实源，identity 相等）；契约模块不新建
    Enum / 第二份常量。"""
    assert RER_REUSE_ACTIVE is REUSE_ACTIVE == "REUSE_ACTIVE"
    assert RER_CREATE_NEW is CREATE_NEW == "CREATE_NEW"

    tree = ast.parse(CONTRACT.read_text(encoding="utf-8"),
                     filename="runtime_execution_result.py")
    class_defs = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    assert [c.name for c in class_defs] == ["RuntimeExecutionResult"]  # 无 Enum 类


def test_d1_agent_response_reuses_the_frozen_contract() -> None:
    """agent_response 直接复用现有 frozen AgentResponse：注解即该类本身，
    不复制、不包装、不重定义。"""
    hints = get_type_hints(RuntimeExecutionResult)
    assert hints["agent_response"] is AgentResponse
    assert AgentResponse.__dataclass_params__.frozen is True


# ================================================================== E. 成功路径不变量


def test_e1_both_modes_construct_with_the_same_invariants() -> None:
    """两种 mode 合法构造：session 恒存在（REUSE=复用那条 / CREATE=新建那条），
    agent_response 恒非 None——mode 只改变 session_id 的解释。"""
    response = AgentResponse(content="echo", external_session_id="s-1",
                             metadata={"k": "v"})
    reused = RuntimeExecutionResult(
        session_mode=REUSE_ACTIVE, session_id=7, agent_response=response)
    created = RuntimeExecutionResult(
        session_mode=CREATE_NEW, session_id=8, agent_response=response)
    assert reused.session_mode == "REUSE_ACTIVE" and reused.session_id == 7
    assert created.session_mode == "CREATE_NEW" and created.session_id == 8
    assert reused.agent_response is response and created.agent_response is response


def test_e2_agent_response_is_required_never_none() -> None:
    """agent_response 必填（无默认值 → 缺参 TypeError）；契约文档明裁
    「None 不允许」——失败走异常通道，不产生结果对象。"""
    with pytest.raises(TypeError):
        RuntimeExecutionResult(session_mode=REUSE_ACTIVE, session_id=7)  # type: ignore[call-arg]
    doc = CONTRACT.read_text(encoding="utf-8")
    assert "agent_response is None 不允许" in doc
    assert "必非 None" in doc


# ================================================================== F. 零接线


def test_f1_contract_is_wired_into_runtime_coordinator() -> None:
    """10.8.3 接线翻转：coordinator / runtime 正式引用 RuntimeExecutionResult
    （execute -> 该契约）；**10.8.4 翻转：wrapper 也已接线**（10.8.3 曾锁死
    wrapper 未引用——彼时结果被丢弃，现由 wrapper 消费）；
    RUNTIME_EXECUTION_ORDER 不变。"""
    for name in ("coordinator.py", "runtime.py"):
        src = (APPLICATION / name).read_text(encoding="utf-8")
        assert "RuntimeExecutionResult" in src, f"{name} 未接线"

    wrapper_src = (APPLICATION / "interaction_recording.py").read_text(
        encoding="utf-8")
    # （10.8.4 翻转）wrapper 已完成接线：接住并返回同一个 Result
    assert "RuntimeExecutionResult" in wrapper_src

    runtime_src = (APPLICATION / "runtime.py").read_text(encoding="utf-8")
    tree = ast.parse(runtime_src, filename="runtime.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "execute":
            assert node.returns is not None
            assert ast.unparse(node.returns) == "RuntimeExecutionResult"
            break
    else:  # pragma: no cover
        raise AssertionError("RuntimeExecutionCoordinator.execute 不存在")

    from app.application.runtime import RUNTIME_EXECUTION_ORDER
    assert RUNTIME_EXECUTION_ORDER == (
        "identity_resolution", "authorization", "routing", "session_resolution",
        "session_creation", "agent_dispatch", "delivery",
    )


# ================================================================== G/H. 纯净性与导出面


def test_g1_domain_purity_imports() -> None:
    """domain 纯净：契约模块只依赖 dataclasses 与 app.domain.*（不触
    application / persistence / adapters / 第三方）。"""
    tree = ast.parse(CONTRACT.read_text(encoding="utf-8"),
                     filename="runtime_execution_result.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name == "dataclasses", f"非法 import：{alias.name}"
        elif isinstance(node, ast.ImportFrom):
            assert node.module in ("dataclasses", "typing",
                                   "app.domain.agent_response",
                                   "app.domain.session_resolution"), \
                f"非法 import：{node.module}"


def test_h1_export_surface_and_contract_doc() -> None:
    """导出面：app.domain 重导出 RuntimeExecutionResult；契约文档成文
    （八项裁决 token 齐全）。"""
    import app.domain as domain_pkg

    assert domain_pkg.RuntimeExecutionResult is RuntimeExecutionResult
    assert "RuntimeExecutionResult" in domain_pkg.__all__

    assert DOC.exists(), f"缺少契约文档：{DOC}"
    text = DOC.read_text(encoding="utf-8")
    for token in ("八项契约裁决",
                  "不新建 Enum",
                  "直接复用现有 frozen AgentResponse",
                  "必非 None",
                  "session_id 为空」的合法组合",
                  "禁止未经审计扩展",
                  "execute() 仍 -> None",
                  "10.1.4",
                  "不携带 outcome"):
        assert token in text, f"契约文档缺少 token：{token}"
