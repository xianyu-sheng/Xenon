"""B8 验收：_verify_llm_file_claims 扩展工具集（batch_write/batch_edit/edit_file）。

关键场景：声称的文件 *不存在于磁盘*，但已通过 edit_file / batch_write /
batch_edit 工具成功调用 → 不再误报"未经工具验证"（B8 之前仅认
write_file/create_directory，会对 edit/batch 类调用误报）。
"""
from types import SimpleNamespace

from xenon.engine.plan_execute_engine import PlanExecuteEngine


def _call(tool: str, params: dict, success: bool = True) -> SimpleNamespace:
    return SimpleNamespace(tool_name=tool, params=params, success=success)


class TestVerifyLlmFileClaimsExtendedTools:
    def test_claim_without_any_tool_warns(self):
        out = PlanExecuteEngine._verify_llm_file_claims(
            "已保存 b8_missing.py 的内容",
            tracker=SimpleNamespace(calls=[]),
        )
        assert "未经工具验证" in out
        assert "b8_missing.py" in out

    def test_write_file_still_verifies(self):
        tracker = SimpleNamespace(calls=[
            _call("write_file", {"file_path": "b8_write_nonexistent.py"})])
        out = PlanExecuteEngine._verify_llm_file_claims(
            "已保存 b8_write_nonexistent.py 的内容", tracker=tracker)
        assert "未经工具验证" not in out

    def test_edit_file_verifies_claim(self):
        """B8: edit_file 现计入已验证（之前只认 write_file/create_directory）。"""
        tracker = SimpleNamespace(calls=[
            _call("edit_file", {"file_path": "b8_edit_nonexistent.py"})])
        out = PlanExecuteEngine._verify_llm_file_claims(
            "已保存 b8_edit_nonexistent.py 的修改", tracker=tracker)
        assert "未经工具验证" not in out

    def test_batch_write_verifies_all_paths(self):
        """B8: batch_write 的 files 列表逐个提路径（兼容 path 与 file_path 两种键）。"""
        tracker = SimpleNamespace(calls=[
            _call("batch_write", {"files": [
                {"path": "b8_bw_a_nonexistent.py", "content": "x"},
                {"file_path": "b8_bw_b_nonexistent.py", "content": "y"},
            ]}),
        ])
        out = PlanExecuteEngine._verify_llm_file_claims(
            "已保存 b8_bw_a_nonexistent.py 和 b8_bw_b_nonexistent.py", tracker=tracker)
        assert "未经工具验证" not in out

    def test_batch_edit_verifies_claim(self):
        """B8: batch_edit 的 edits 列表逐个提 file_path。"""
        tracker = SimpleNamespace(calls=[
            _call("batch_edit", {"edits": [
                {"file_path": "b8_be_nonexistent.py", "old_text": "a", "new_text": "b"},
            ]}),
        ])
        out = PlanExecuteEngine._verify_llm_file_claims(
            "已保存 b8_be_nonexistent.py 的修改", tracker=tracker)
        assert "未经工具验证" not in out

    def test_batch_write_partial_warns_for_unwritten(self):
        tracker = SimpleNamespace(calls=[
            _call("batch_write", {"files": [
                {"path": "b8_bw_a_nonexistent.py", "content": "x"}]}),
        ])
        out = PlanExecuteEngine._verify_llm_file_claims(
            "已保存 b8_bw_a_nonexistent.py 和 b8_missing.py", tracker=tracker)
        assert "b8_missing.py" in out
        assert "未经工具验证" in out

    def test_failed_tool_call_not_counted(self):
        """失败的工具调用不应计入已验证。"""
        tracker = SimpleNamespace(calls=[
            _call("write_file", {"file_path": "b8_write_nonexistent.py"}, success=False),
        ])
        out = PlanExecuteEngine._verify_llm_file_claims(
            "已保存 b8_missing.py", tracker=tracker)
        assert "未经工具验证" in out
