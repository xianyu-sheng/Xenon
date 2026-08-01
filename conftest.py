"""仓库级 pytest 配置 — 对 ``tests/`` 和 ``xenon/tests/`` 两棵测试树同时生效。

这些 autouse fixture 原先只放在 ``tests/conftest.py``，因此 ``xenon/tests/``
下的用例一个都拿不到（pytest 的 conftest 按目录树生效）。其中
``_isolate_chat_completion_mock`` 负责兜住 ``chat_completion`` 的 mock 泄漏，
而 ``xenon/tests/`` 恰好放着直接打 LLM 客户端的 cache 相关用例，缺失该兜底
会让泄漏在同进程内污染后续测试。

提到仓库根目录后，两棵树行为一致。
"""
import pytest

# 在 conftest 加载时（最早时刻，mock 还没发生）保存 chat_completion 的真实原始引用。
# 后续无论哪个测试怎么 mock，autouse fixture 都能恢复到这个 orig。
import xenon.engine.base as _engine_base
import xenon.utils.llm_client as _llm_client

_ORIG_ENGINE_CHAT = _engine_base.chat_completion
_ORIG_UTIL_CHAT = _llm_client.chat_completion
_ORIG_UTIL_STREAM = _llm_client.chat_completion_stream

# 同理，在任何 fixture 打补丁之前保存真实的路径校验实现，供需要断言安全边界的
# 用例通过 ``real_path_validation`` fixture 取回。
from xenon.nodes.tool_node import ToolNode as _ToolNode  # noqa: E402

_ORIG_VALIDATE_PATH = _ToolNode._validate_path


@pytest.fixture
def real_path_validation():
    """opt-out：恢复真实的 ``ToolNode._validate_path``。

    ``_disable_security_for_tests`` 默认把路径校验换成宽松版本，方便绝大多数
    用例使用临时目录。但断言路径校验**本身**的用例（如安全边界测试）必须拿到
    真实实现，否则断言失去意义。这类用例显式声明本 fixture 即可。
    """
    from xenon.nodes.tool_node import ToolNode

    patched = ToolNode._validate_path
    ToolNode._validate_path = _ORIG_VALIDATE_PATH
    try:
        yield
    finally:
        ToolNode._validate_path = patched


@pytest.fixture(autouse=True)
def _disable_security_for_tests():
    """Disable ToolNode security for all tests.

    Tests use temp directories and non-existent paths that are outside
    the project directory, which would trigger path validation errors.

    需要真实校验的用例请声明 ``real_path_validation`` fixture 反转本行为。
    """
    from pathlib import Path

    from xenon.nodes.tool_node import ToolNode

    original = ToolNode._validate_path

    def permissive_validate(self, file_path, *, for_write=False):
        """Skip security checks in tests."""
        if not file_path:
            return Path(file_path)
        path = Path(file_path)
        if self.cwd and not path.is_absolute():
            path = Path(self.cwd) / path
        return path

    ToolNode._validate_path = permissive_validate
    yield
    ToolNode._validate_path = original


@pytest.fixture(autouse=True)
def _isolate_cache_telemetry(tmp_path, monkeypatch):
    """Never let mocked REPL calls write telemetry into the user's home."""
    monkeypatch.setenv("XENON_CACHE_DIR", str(tmp_path / "cache-telemetry"))
    yield


@pytest.fixture(autouse=True)
def _auto_confirm_destructive(monkeypatch):
    """P3-Q8：测试中破坏性操作的 Confirm.ask 自动确认，避免阻塞 stdin。

    需要测试"取消"路径时，在用例内 ``monkeypatch.delenv("XENON_ASSUME_YES")``
    并 patch ``_confirm`` 即可。
    """
    monkeypatch.setenv("XENON_ASSUME_YES", "1")
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
