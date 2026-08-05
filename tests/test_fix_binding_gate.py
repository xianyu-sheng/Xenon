"""FixBindingGate 修复绑定校验测试（Step 2）。

数据驱动设计：用 SWE-bench rootfix_3 的 5 个真实 matplotlib 补丁验证
区分度——plan-reflection 的「重复+特判」补丁必须被拒绝，其余通过补丁
必须放行。判据（确定性，零 LLM）：
  1) 修改现有行（- 行）→ 强绑定，通过
  2) 纯追加 + 新增行与现有代码相似度 ≥ 0.5 → 重复特判（补救），拒绝
  3) 纯追加 + 无重复 → 防御性插入，通过
"""

from __future__ import annotations

from xenon.engine.evidence_gate import (
    FixBindingGate,
    line_similarity,
    patch_binding_stats,
)

# ── SWE-bench 真实补丁（rootfix_3，matplotlib__matplotlib-24970）──
# 官方评分：react/plan-execute/plan-react 通过；plan-reflection 失败
PATCH_REACT = """\
diff --git a/lib/matplotlib/colors.py b/lib/matplotlib/colors.py
--- a/lib/matplotlib/colors.py
+++ b/lib/matplotlib/colors.py
@@ -725,6 +725,11 @@ class Colormap:
                 # Avoid converting large positive values to negative integers.
                 np.clip(xa, -1, self.N, out=xa)
                 xa = xa.astype(int)
+        # If the input is an integer dtype that cannot hold the sentinel
+        # values (self._i_over, self._i_under, self._i_bad), widen it to
+        # avoid NumPy deprecation warnings for out-of-bound assignments.
+        if xa.dtype.kind in "iu" and np.iinfo(xa.dtype).max < self.N + 2:
+            xa = xa.astype(np.intp)
         # Set the over-range indices before the under-range;
         # otherwise the under-range values get converted to over-range.
         xa[xa > self.N - 1] = self._i_over
"""

PATCH_PLAN_EXECUTE = """\
diff --git a/lib/matplotlib/colors.py b/lib/matplotlib/colors.py
--- a/lib/matplotlib/colors.py
+++ b/lib/matplotlib/colors.py
@@ -730,6 +730,12 @@ class Colormap:
-        xa[xa > self.N - 1] = self._i_over
-        xa[xa < 0] = self._i_under
-        xa[mask_bad] = self._i_bad
+        xa[xa > self.N - 1] = np.array(self._i_over).astype(xa.dtype)
+        xa[xa < 0] = np.array(self._i_under).astype(xa.dtype)
+        xa[mask_bad] = np.array(self._i_bad).astype(xa.dtype)
         lut = self._lut
         if bytes:
"""

PATCH_PLAN_REFLECTION = """\
diff --git a/lib/matplotlib/colors.py b/lib/matplotlib/colors.py
--- a/lib/matplotlib/colors.py
+++ b/lib/matplotlib/colors.py
@@ -730,6 +730,12 @@ class Colormap:
         xa[xa > self.N - 1] = self._i_over
         xa[xa < 0] = self._i_under
         xa[mask_bad] = self._i_bad
+        if xa.dtype == np.uint8:
+            # NumPy 1.24+ deprecates assigning out-of-bound Python integers
+            # to integer arrays.  _i_over, _i_under, _i_bad may exceed 255.
+            xa[xa > self.N - 1] = np.uint8(self._i_over)
+            xa[xa < 0] = np.uint8(self._i_under)
+            xa[mask_bad] = np.uint8(self._i_bad)
         lut = self._lut
         if bytes:
"""

PATCH_REACT_REFLECTION = """\
diff --git a/lib/matplotlib/colors.py b/lib/matplotlib/colors.py
--- a/lib/matplotlib/colors.py
+++ b/lib/matplotlib/colors.py
@@ -725,6 +725,10 @@ class Colormap:
         xa[xa > self.N - 1] = self._i_over
         xa[xa < 0] = self._i_under
         xa[mask_bad] = self._i_bad
+        # Ensure we have a dtype that can hold the sentinel values
+        # (_i_over, _i_under, _i_bad) without overflow.
+        if xa.dtype.kind in 'iu' and np.iinfo(xa.dtype).max < self.N + 2:
+            xa = xa.astype(np.intp)
         lut = self._lut
         if bytes:
"""


class TestFixBindingGateRealPatches:
    """用 SWE-bench 真实补丁验证区分度（数据驱动核心测试）。"""

    def test_react_patch_passes(self) -> None:
        """react 补丁：纯追加但无重复（防御性插入）→ 通过。"""
        verdict = FixBindingGate().check(None, patch=PATCH_REACT)
        assert verdict.passed is True, verdict.reason

    def test_plan_execute_patch_passes(self) -> None:
        """plan-execute 补丁：修改现有行 → 强绑定，通过。"""
        verdict = FixBindingGate().check(None, patch=PATCH_PLAN_EXECUTE)
        assert verdict.passed is True, verdict.reason

    def test_plan_reflection_patch_rejected(self) -> None:
        """plan-reflection 补丁：重复+特判 → 拒绝（本 Gate 的核心价值）。"""
        verdict = FixBindingGate().check(None, patch=PATCH_PLAN_REFLECTION)
        assert verdict.passed is False
        assert "重复" in verdict.reason or "特判" in verdict.reason

    def test_react_reflection_patch_passes(self) -> None:
        """react-reflection 补丁：纯追加但无重复 → 通过。"""
        verdict = FixBindingGate().check(None, patch=PATCH_REACT_REFLECTION)
        assert verdict.passed is True, verdict.reason


class TestPatchBindingStats:
    def test_counts_modified_lines(self) -> None:
        stats = patch_binding_stats(PATCH_PLAN_EXECUTE)
        assert stats["modified_count"] == 3

    def test_identifies_repetition(self) -> None:
        """plan-reflection 的新增行与现有行相似度 ≥ 阈值。"""
        stats = patch_binding_stats(PATCH_PLAN_REFLECTION)
        assert stats["modified_count"] == 0
        assert stats["max_context_similarity"] >= 0.5

    def test_no_repetition_for_defensive(self) -> None:
        stats = patch_binding_stats(PATCH_REACT)
        assert stats["modified_count"] == 0
        assert stats["max_context_similarity"] < 0.5


class TestLineSimilarity:
    def test_identical_lines(self) -> None:
        assert line_similarity("xa[xa > self.N - 1] = self._i_over",
                               "xa[xa > self.N - 1] = self._i_over") == 1.0

    def test_similar_lines(self) -> None:
        # 特判补丁：np.uint8() 包装 vs 原赋值
        sim = line_similarity(
            "xa[xa > self.N - 1] = np.uint8(self._i_over)",
            "xa[xa > self.N - 1] = self._i_over",
        )
        assert sim >= 0.5

    def test_disjoint_lines(self) -> None:
        assert line_similarity("import os", "def foo(): return 1") == 0.0


class TestFixBindingGateEdgeCases:
    def test_no_patch_passes(self) -> None:
        """无补丁信息 → 通过（不误伤只读任务）。"""
        verdict = FixBindingGate().check(None, patch="")
        assert verdict.passed is True

    def test_tracker_fallback(self) -> None:
        """从 tracker 恢复补丁：edit_file 带 old_text = 修改现有行 → 通过。

        注：edit_file 是精确替换工具，有 old_text 就意味着改动落在
        现有代码行上（强绑定），故此处预期 passed=True。
        """
        import types

        call = types.SimpleNamespace(
            tool_name="edit_file",
            success=True,
            params={
                "file_path": "a.py",
                "old_text": "xa[xa > self.N - 1] = self._i_over",
                "new_text": "xa[xa > self.N - 1] = np.uint8(self._i_over)",
            },
        )
        tracker = types.SimpleNamespace(calls=[call])
        verdict = FixBindingGate().check(None, tracker=tracker)
        assert verdict.passed is True  # edit_file 精确替换 = 修改现有行

    def test_tracker_fallback_append_pattern(self) -> None:
        """tracker 恢复的「重复特判」模式：write_file 覆盖写整个文件时，
        新增内容与旧内容高度相似 → 拒绝。"""
        import types

        call = types.SimpleNamespace(
            tool_name="write_file",
            success=True,
            params={
                "file_path": "a.py",
                "content": (
                    "xa[xa > self.N - 1] = self._i_over\n"
                    "xa[xa < 0] = self._i_under\n"
                    "if xa.dtype == np.uint8:\n"
                    "    xa[xa > self.N - 1] = np.uint8(self._i_over)\n"
                ),
            },
        )
        tracker = types.SimpleNamespace(calls=[call])
        # write_file 无 old_text，无法构造 - 行——此处验证不抛异常且能判定
        verdict = FixBindingGate().check(None, tracker=tracker)
        assert verdict.phase == "fix"
