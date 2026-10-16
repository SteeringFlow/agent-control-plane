"""B3.3 —— 导出感知测试边界回归。

覆盖（对应 Phase B3.3 验收 §6）：
1. source / 有效 oss_export / unknown(部分检出) / 矛盾树 的 provenance 判定；
2. 「绝不因 frontend/ 缺失而推断 oss_export」「绝不把部分检出当有效导出」；
3. F2：source 模式守卫含 certbot-renew.sh；oss_export 模式显式剔除该文件但
   保留其余部署资产守卫；unknown 保守返回全量（缺失=显式失败）；
4. 纯 UI 模块清单不含混合职责文件（混合模块的 backend 断言在导出中继续执行）。

真实双环境证据见 Phase B3.3 报告（源仓库全量 + 导出模拟树全量）。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from tests import oss_export_boundary as boundary
from tests.oss_export_boundary import repo_provenance


def _make_root(tmp_path: Path, *, source_marker: bool = False,
               export_manifest: bool = False, frontend: bool = False) -> Path:
    # 本机 pytest tmp_path 夹具存在已知 WinError 5 权限问题（M6_6 留档）——
    # 改用 tempfile 自管，并在权限失败时显式 skip（不静默，记入报告）。
    try:
        base = Path(tempfile.mkdtemp(prefix="b33_prov_"))
        root = base / "repo"
        root.mkdir()
    except PermissionError:
        pytest.skip("Windows tmpdir 权限问题（本机已知环境限制）")
    if source_marker:
        (root / ".oss-export-exclude.txt").write_text("# control\n", encoding="utf-8")
    if export_manifest:
        (root / "OSS_EXPORT_MANIFEST.txt").write_text(
            "source_commit=0aa7e42\ngenerated=2026-10-15\n", encoding="utf-8"
        )
    if frontend:
        (root / "frontend").mkdir()
        (root / "frontend" / "index.html").write_text("<html></html>", encoding="utf-8")
    return root


# ------------------------------------------------------- provenance 三态判定


def test_source_marker_means_source_even_with_frontend(tmp_path: Path) -> None:
    root = _make_root(tmp_path, source_marker=True, frontend=True)
    assert repo_provenance(root) == boundary.SOURCE


def test_export_marker_without_frontend_means_oss_export(tmp_path: Path) -> None:
    root = _make_root(tmp_path, export_manifest=True)
    assert repo_provenance(root) == boundary.OSS_EXPORT


def test_partial_checkout_is_unknown_never_export(tmp_path: Path) -> None:
    """无任何正证据（frontend 也缺失）→ unknown：绝不把部分检出当有效导出。"""
    root = _make_root(tmp_path)
    assert not (root / "frontend").exists()
    assert repo_provenance(root) == boundary.UNKNOWN_PROVENANCE


def test_source_marker_is_authoritative_even_if_frontend_deleted(tmp_path: Path) -> None:
    """source 判定以控制面文件为**权威正证据**：即使 frontend/ 被误删仍判
    source——损坏会以 UI 测试的缺失文件**显式失败**暴露（保守契约：
    显式失败优于静默改判/静默跳过）；绝不因此降级为 export。"""
    root = _make_root(tmp_path, source_marker=True)
    assert repo_provenance(root) == boundary.SOURCE


def test_both_markers_is_unknown(tmp_path: Path) -> None:
    root = _make_root(tmp_path, source_marker=True, export_manifest=True)
    assert repo_provenance(root) == boundary.UNKNOWN_PROVENANCE


def test_export_marker_with_frontend_present_is_unknown(tmp_path: Path) -> None:
    """带导出标记却仍含 frontend/ = 矛盾导出 → 不可信 → unknown。"""
    root = _make_root(tmp_path, export_manifest=True, frontend=True)
    assert repo_provenance(root) == boundary.UNKNOWN_PROVENANCE


def test_real_source_repo_is_source() -> None:
    """真实现场分类：完整源仓库 = source；有效导出树 = oss_export。

    本测试断言的是「当前真实仓库」的分类，两种形态各有其正确答案，
    故按 provenance 分支而非假定单一形态（unknown → 显式失败）。
    """
    prov = repo_provenance()
    if prov == boundary.OSS_EXPORT:
        pytest.skip("当前运行于导出树——其正确分类即 oss_export（本断言仅在源仓库有意义）")
    assert prov == boundary.SOURCE, (
        f"provenance={prov}：既非 source（完整源仓库正证据缺失）也非有效导出"
        "（无 OSS_EXPORT_MANIFEST.txt）——保守契约下显式失败"
    )


# ------------------------------------------------------- F2 部署资产守卫


def test_f2_source_mode_keeps_renew_script_guard() -> None:
    """source 模式：certbot-renew.sh 安全断言保持。

    仅在 source 下有意义；导出树中该文件被有意排除、清单按契约剔除
    （由 test_f2_oss_export_mode_excludes_renew_script_only 反向守护）。
    unknown → 显式失败（保守契约）。
    """
    from tests.test_m7_aui_10_8_12_fail_closed_compose import DEPLOY_SCRIPTS

    prov = repo_provenance()
    if prov == boundary.OSS_EXPORT:
        pytest.skip("导出树不含 certbot-renew.sh——source 专属守卫不适用（F2 契约）")
    assert any(p.name == "certbot-renew.sh" for p in DEPLOY_SCRIPTS), (
        "source 模式必须继续守卫 certbot-renew.sh 的安全断言"
    )


def test_f2_oss_export_mode_excludes_renew_script_only() -> None:
    from tests.test_m7_aui_10_8_12_fail_closed_compose import _deploy_script_paths

    oss = _deploy_script_paths("oss_export")
    assert all(p.name != "certbot-renew.sh" for p in oss), (
        "有效导出树显式剔除被排除的续期脚本（绝不为了测试重新纳入）"
    )
    assert {p.name for p in oss} >= {"deploy-website.sh", "certbot-deploy.sh"}, (
        "其余部署资产守卫不得被顺带缩小"
    )


def test_f2_unknown_mode_conservative_full_list() -> None:
    from tests.test_m7_aui_10_8_12_fail_closed_compose import _deploy_script_paths

    full = _deploy_script_paths("unknown")
    assert any(p.name == "certbot-renew.sh" for p in full), (
        "unknown 保守返回全量：缺失文件应显式失败而非静默缩小检查面"
    )


# ------------------------------------------------------- 混合职责模块边界


def test_analyzer_mixed_file_backend_tests_not_consuming() -> None:
    """B3.3.1：混合职责文件的 backend 断言**不在**消费集合内——
    它们在 OSS 导出中继续执行（B3.1 审计的 4 个 ADAPT 文件 + m5_2 证据）。"""
    from tests.oss_export_boundary import frontend_consuming_tests

    tests_dir = Path(__file__).resolve().parent
    consuming = frontend_consuming_tests(tests_dir / "test_m5_2_cloud_product_entry.py")
    # m5_2：仅 3 个静态页面对照测试消费 frontend；Gate1 认证行为/compose 守卫不消费
    assert consuming == frozenset({
        "test_connect_pages_are_compat_redirects",
        "test_workspace_html_has_signup_and_login_toggle",
        "test_workspace_js_login_uses_existing_login_endpoint",
    })
    assert "test_gate1_signup_and_login_publicly_reachable" not in consuming
    assert "test_dockerfile_and_compose_inject_git_sha" not in consuming

    consuming_10_2 = frontend_consuming_tests(
        tests_dir / "test_m7_aui_10_2_agent_credential_security.py")
    assert {"test_f1_frontend_masks_echo_and_omits_on_save",
            "test_f5_frontend_does_not_log_config"} <= consuming_10_2
    # 后端凭据安全断言（16 个 a/b/c/d/e 组测试，不消费 frontend）继续在导出中执行
    assert "test_a1_post_response_masks_api_key_but_keeps_manager_keys" not in consuming_10_2
    assert "test_d1_credential_ref_stays_write_only_opaque" not in consuming_10_2
    assert len(consuming_10_2) == 4


def test_analyzer_pure_and_gate_files() -> None:
    """纯 UI 文件全量消费；gate 文件（backend-only，仅命名含 frontend）零消费。"""
    from tests.oss_export_boundary import frontend_consuming_tests

    tests_dir = Path(__file__).resolve().parent
    pure = frontend_consuming_tests(tests_dir / "test_m7_aui_6_channel_credential_panel.py")
    assert len(pure) == 16, "m7_aui_6 全部 16 个测试均消费 frontend（B3.3 已验证）"
    gate = frontend_consuming_tests(
        tests_dir / "test_cloud_0_1_2_3_connect_agent_golden_path_architecture_gate.py")
    assert gate == frozenset(), "gate 文件零 frontend 消费（RUN_IN_BOTH）"


def test_oss_ui_required_mark_is_export_conditional() -> None:
    """标记语义：仅 oss_export 触发跳过（source/unknown 不跳过——保守契约）。

    动态断言：skipif 条件值必须恰等于「当前 provenance 是否为有效导出」，
    因而同一测试在源仓库与导出树中都验证该语义。
    """
    mark = boundary.oss_ui_required()
    assert mark.markname == "skipif"
    condition = mark.kwargs.get("condition", mark.args[0] if mark.args else None)
    expected = repo_provenance() == boundary.OSS_EXPORT
    assert condition is expected, (
        f"skipif 条件（{condition}）应等于 provenance==oss_export（{expected}）"
    )
