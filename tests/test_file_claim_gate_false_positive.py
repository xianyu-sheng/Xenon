"""FileClaimGate 误杀回归测试：代码引用不得被当作文件声称。"""
from __future__ import annotations

from xenon.engine.evidence_gate import (
    _extract_claimed_files,
    verify_file_claims,
)


class FakeTracker:
    def __init__(self, files: list[str]) -> None:
        self.files = files
        self.calls: list = []


class FakeCall:
    def __init__(self, tool: str, path: str, success: bool = True) -> None:
        self.tool_name = tool
        self.params = {"file_path": path}
        self.success = success


def _extract(output: str) -> set[str]:
    return _extract_claimed_files(output)


class TestCodeReferenceNotFileClaim:
    """代码片段中的变量/字段引用不是文件声称。"""

    def test_orm_field_reference(self) -> None:
        output = "已修复问题，现在 self.c 和 self.foo 都正确初始化。"
        assert "self.c" not in _extract(output)

    def test_dotted_attribute_chain(self) -> None:
        output = "obj.config.value 已更新，保存后生效。"
        assert "obj.config.value" not in _extract(output)

    def test_code_snippet_with_claims(self) -> None:
        output = (
            "修改已完成。示例：\n"
            "```python\n"
            "self.c = 3\n"
            "self.d = 4\n"
            "```\n"
            "以上修改已保存。"
        )
        assert "self.c" not in _extract(output)
        assert "self.d" not in _extract(output)

    def test_python_module_dot_notation(self) -> None:
        output = "import os.path 后调用 os.path.join 即可。"
        assert "os.path" not in _extract(output)


class TestRealPathClaimsStillDetected:
    """真实的路径声称必须仍然被检测。"""

    def test_relative_path_with_separator(self) -> None:
        output = "已修改 django/db/backends/base/creation.py。"
        files = _extract(output)
        assert any("creation.py" in f for f in files)

    def test_claimed_bare_filename_with_verb(self) -> None:
        output = "创建了 docstring.py 并写入解析逻辑。"
        files = _extract(output)
        assert any("docstring.py" in f for f in files)

    def test_tests_path(self) -> None:
        output = "新增 tests/forms_tests/test_formsets.py 覆盖该场景。"
        files = _extract(output)
        assert any("test_formsets.py" in f for f in files)

    def test_conjoined_bare_files(self) -> None:
        """'已保存 A 和 B' 中两个文件名都是声称（连接词延续）。"""
        output = "已保存 b8_bw_a_nonexistent.py 和 b8_missing.py"
        files = _extract(output)
        assert "b8_bw_a_nonexistent.py" in files
        assert "b8_missing.py" in files


class TestDiffContextNotFalsePositive:
    """diff 头部的 a/ b/ 前缀是格式约定，不是文件声称。"""

    def test_diff_git_header_paths(self) -> None:
        output = (
            "修改如下：\n"
            "diff --git a/django/db/backends/base/creation.py b/django/db/backends/base/creation.py\n"
            "index 1234567..89abcde 100644\n"
            "--- a/django/db/backends/base/creation.py\n"
            "+++ b/django/db/backends/base/creation.py\n"
            "@@ -5,7 +5,7 @@\n"
            " self.c = 3\n"
        )
        files = _extract(output)
        # a/、b/ 前缀剥除后应只剩真实的 relative 路径（去重后一个）
        assert "a/django/db/backends/base/creation.py" not in files
        assert "b/django/db/backends/base/creation.py" not in files
        assert any("django/db/backends/base/creation.py" in f for f in files)
        assert "self.c" not in files

    def test_diff_verified_by_tracker_passes(self) -> None:
        tracker = FakeTracker([])
        tracker.calls = [FakeCall("edit_file", "/tmp/w/django/db/backends/base/creation.py")]
        output = (
            "diff --git a/django/db/backends/base/creation.py b/django/db/backends/base/creation.py\n"
            "--- a/django/db/backends/base/creation.py\n"
            "+++ b/django/db/backends/base/creation.py\n"
            " self.c = 3\n"
            "修改完成。"
        )
        passed, unverified = verify_file_claims(output, tracker)
        assert passed is True, unverified

    def test_diff_real_change_without_tool_still_rejected(self) -> None:
        """只贴 diff 但未调用写工具（worktree 未变）→ 仍应拒绝。"""
        tracker = FakeTracker([])
        output = (
            "diff --git a/src/foo.py b/src/foo.py\n"
            "--- a/src/foo.py\n"
            "+++ b/src/foo.py\n"
            "+print(1)\n"
            "修改已保存，任务完成。"
        )
        passed, unverified = verify_file_claims(output, tracker)
        assert passed is False
        assert any("foo.py" in f for f in unverified)


class TestVerifyFileClaimsNoFalsePositive:
    def test_code_reference_output_passes(self) -> None:
        tracker = FakeTracker([])
        output = "已修复，self.c 现在正确。"
        passed, unverified = verify_file_claims(output, tracker)
        assert passed is True, unverified

    def test_real_claim_verified_by_tracker(self) -> None:
        tracker = FakeTracker([])
        tracker.calls = [FakeCall("edit_file", "/tmp/work/django/db/backends/base/creation.py")]
        output = "已修改 django/db/backends/base/creation.py。"
        passed, unverified = verify_file_claims(output, tracker)
        assert passed is True, unverified

    def test_real_claim_unverified_still_rejected(self) -> None:
        tracker = FakeTracker([])
        output = "创建了 brand_new_module.py 并实现功能。"
        passed, unverified = verify_file_claims(output, tracker)
        assert passed is False
        assert "brand_new_module.py" in unverified


class TestCommandCreatedFiles:
    """command 工具创建的辅助文件不应被误判为未验证声称（SWE-bench 实测）。

    sphinx-7738：LLM 通过 command 创建 tmp/repro.py 复现脚本，patch 已
    落盘。声称的相对路径与沙箱绝对路径（workspace_root）对不上，此前
    被误判为未验证文件。verify_file_claims 应结合 workspace_root 解析。
    """

    def test_relative_claim_resolves_under_workspace_root(self, tmp_path) -> None:
        target = tmp_path / "tmp" / "repro.py"
        target.parent.mkdir(parents=True)
        target.write_text("print(1)\n", encoding="utf-8")
        passed, unverified = verify_file_claims(
            "已创建 tmp/repro.py 复现脚本。", None,
            workspace_root=str(tmp_path),
        )
        assert passed is True, unverified

    def test_testbed_prefix_variant(self, tmp_path) -> None:
        target = tmp_path / "sympy" / "printing" / "latex.py"
        target.parent.mkdir(parents=True)
        target.write_text("x = 1\n", encoding="utf-8")
        passed, unverified = verify_file_claims(
            "已修改 testbed/sympy/printing/latex.py。", None,
            workspace_root=str(tmp_path),
        )
        assert passed is True, unverified

    def test_still_rejects_when_not_on_disk(self, tmp_path) -> None:
        passed, unverified = verify_file_claims(
            "创建了 brand_new_module.py 并实现功能。", None,
            workspace_root=str(tmp_path),
        )
        assert passed is False
        assert "brand_new_module.py" in unverified
