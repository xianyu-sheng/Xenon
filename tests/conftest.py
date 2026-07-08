"""
Test configuration — disable security path validation for unit tests.
"""
import pytest

# 在 conftest 加载时（最早时刻，mock 还没发生）保存 chat_completion 的真实原始引用。
# 后续无论哪个测试怎么 mock，autouse fixture 都能恢复到这个 orig。
import omniagent.engine.base as _engine_base
import omniagent.utils.llm_client as _llm_client
_ORIG_ENGINE_CHAT = _engine_base.chat_completion
_ORIG_UTIL_CHAT = _llm_client.chat_completion
_ORIG_UTIL_STREAM = _llm_client.chat_completion_stream


@pytest.fixture(autouse=True)
def _disable_security_for_tests():
    """Disable ToolNode security for all tests.

    Tests use temp directories and non-existent paths that are outside
    the project directory, which would trigger path validation errors.
    """
    from omniagent.nodes.tool_node import ToolNode

    original = ToolNode._validate_path

    def permissive_validate(self, file_path, *, for_write=False):
        """Skip security checks in tests."""
        if not file_path:
            from pathlib import Path
            return Path(file_path)
        path = __import__("pathlib").Path(file_path)
        if self.cwd and not path.is_absolute():
            path = __import__("pathlib").Path(self.cwd) / path
        return path

    ToolNode._validate_path = permissive_validate
    yield
    ToolNode._validate_path = original


@pytest.fixture(autouse=True)
def _auto_confirm_destructive(monkeypatch):
    """P3-Q8：测试中破坏性操作的 Confirm.ask 自动确认，避免阻塞 stdin。

    需要测试"取消"路径时，在用例内 ``monkeypatch.delenv("OMNIAGENT_ASSUME_YES")``
    并 patch ``_confirm`` 即可。
    """
    monkeypatch.setenv("OMNIAGENT_ASSUME_YES", "1")
    yield


@pytest.fixture(autouse=True)
def _isolate_chat_completion_mock():
    """强制隔离 ``chat_completion`` mock 状态（防止跨测试文件泄漏）。

    背景：``tests/test_repl_real_usage.py`` 的 ``_make_repl_mock`` 用直接赋值
    （``engine_base.chat_completion = fake``）改全局模块属性而非 ``monkeypatch.setattr``，
    当测试异常时 ``_restore_repl`` 不被调用，导致 mock 状态泄漏给后续测试文件。
    本 fixture 在每个测试后强制重置这些属性到 conftest 加载时保存的**真实原始**函数
    （而非当前 dict 值，因当前值可能已被 mock 污染）。
    """
    yield
    # 测试后强制恢复真实原始函数（无论中间被怎么 mock）
    _engine_base.chat_completion = _ORIG_ENGINE_CHAT
    _llm_client.chat_completion = _ORIG_UTIL_CHAT
    _llm_client.chat_completion_stream = _ORIG_UTIL_STREAM
