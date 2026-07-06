"""F1 验收：ToolExecutor 7 阶段门面 + 参数幻觉校验 + 断路器 + 重试 + 结果封装。"""
from omniagent.engine.circuit_breaker import BreakerRegistry, CircuitBreaker
from omniagent.engine.context import AgentContext
from omniagent.engine.tool_tracker import ToolExecutionTracker
from omniagent.nodes import tool_executor as te_mod
from omniagent.nodes.tool_executor import (
    ToolExecuteResult,
    ToolExecutor,
    classify_tool,
    is_terminal_error,
    validate_tool_params,
)


# ── 工具分类 ───────────────────────────────────────────────
class TestClassifyTool:
    def test_sensitive(self):
        assert classify_tool("command") == "SENSITIVE"

    def test_write(self):
        assert classify_tool("write_file") == "WRITE"
        assert classify_tool("edit_file") == "WRITE"
        assert classify_tool("create_directory") == "WRITE"

    def test_info(self):
        assert classify_tool("read_file") == "INFO"
        assert classify_tool("list_files") == "INFO"


# ── 错误分类 ───────────────────────────────────────────────
class TestIsTerminalError:
    def test_terminal_not_found(self):
        assert is_terminal_error("文件不存在: /x") is True
        assert is_terminal_error("FileNotFoundError: not found") is True

    def test_terminal_permission(self):
        assert is_terminal_error("permission denied") is True
        assert is_terminal_error("权限拒绝") is True

    def test_terminal_already_exists(self):
        assert is_terminal_error("文件已存在") is True

    def test_transient_timeout(self):
        assert is_terminal_error("connection timeout") is False

    def test_transient_rate_limit(self):
        assert is_terminal_error("429 rate limit") is False

    def test_unknown_defaults_transient(self):
        assert is_terminal_error("一些未知错误") is False


# ── 参数幻觉校验 ───────────────────────────────────────────
class TestValidateToolParams:
    def test_legit_params_pass(self):
        ok, _ = validate_tool_params({"file_path": "/tmp/x.py", "content": "print(1)"})
        assert ok is True

    def test_content_whitelist_exempt(self):
        """content 即使像代码也不被拦（白名单豁免）。"""
        ok, _ = validate_tool_params({"content": "def foo():\n  return x}{"})
        assert ok is True

    def test_hallucination_blocked(self):
        """≥2 条件命中 → 拦截。"""
        ok, reason = validate_tool_params({
            "file_path": "def foo(x):->: <not a path>"  # 函数签名 + Windows 非法字符 + 末尾非法
        })
        assert ok is False
        assert "file_path" in reason

    def test_single_hit_not_blocked(self):
        """单条件命中不拦（防误杀合法长路径）。"""
        # 仅末尾非法字符一个条件
        ok, _ = validate_tool_params({"file_path": "/tmp/normal_path"})
        assert ok is True


# ── 断路器 ─────────────────────────────────────────────────
class TestCircuitBreaker:
    def test_opens_after_threshold(self):
        t = {"v": 0}
        clock = lambda: t["v"]
        b = CircuitBreaker(failure_threshold=3, cooldown=30, clock=clock)
        assert b.allow() is True
        b.record_failure(); b.record_failure()
        assert b.state == "closed"  # 2 < 3
        b.record_failure()
        assert b.state == "open"
        assert b.allow() is False  # 开启即拒绝

    def test_half_open_after_cooldown(self):
        t = {"v": 0}
        clock = lambda: t["v"]
        b = CircuitBreaker(failure_threshold=2, cooldown=10, clock=clock)
        b.record_failure(); b.record_failure()
        assert b.state == "open"
        assert b.allow() is False  # 未冷却
        t["v"] = 10  # 冷却到期
        assert b.allow() is True  # 转 half_open 放行试探
        assert b.state == "half_open"

    def test_half_open_success_closes(self):
        t = {"v": 0}
        clock = lambda: t["v"]
        b = CircuitBreaker(failure_threshold=2, cooldown=10, clock=clock)
        b.record_failure(); b.record_failure()
        t["v"] = 10
        b.allow()  # half_open
        b.record_success()
        assert b.state == "closed"
        assert b.failures == 0

    def test_half_open_failure_doubles_cooldown(self):
        t = {"v": 0}
        clock = lambda: t["v"]
        b = CircuitBreaker(failure_threshold=2, cooldown=10, clock=clock)
        b.record_failure(); b.record_failure()
        assert b.cooldown == 10
        t["v"] = 10
        b.allow()  # half_open
        b.record_failure()  # 试探失败
        assert b.state == "open"
        assert b.cooldown == 20  # 翻倍


# ── ToolExecutor 流水线 ────────────────────────────────────
class _FakeNode:
    """替代 ToolNode：按脚本返回结果。"""

    script: list = []

    def __init__(self, name, action_type=None, **params):
        self.action_type = action_type

    @staticmethod
    def normalize_params(p):
        return p

    def execute(self, context):
        if not _FakeNode.script:
            return {"success": True, "content": "default"}
        return _FakeNode.script.pop(0)


def _executor(monkeypatch, *, retry_attempts=2):
    monkeypatch.setattr(te_mod, "ToolNode", _FakeNode)
    return ToolExecutor(retry_attempts=retry_attempts)


class TestToolExecutorPipeline:
    def test_unknown_tool_returns_failure(self, monkeypatch):
        ex = _executor(monkeypatch)
        r = ex.execute("no_such_tool", {"x": 1}, AgentContext(), tools={"read_file": {}})
        assert r.success is False
        assert "未知工具" in r.observation

    def test_param_hallucination_blocked(self, monkeypatch):
        ex = _executor(monkeypatch)
        r = ex.execute(
            "write_file",
            {"file_path": "def foo():->: <bad>", "content": "x"},
            AgentContext(), tools={"write_file": {}},
        )
        assert r.success is False
        assert "参数校验失败" in r.observation

    def test_success_path(self, monkeypatch):
        _FakeNode.script = [{"success": True, "content": "hello"}]
        ex = _executor(monkeypatch)
        r = ex.execute("read_file", {"file_path": "/x"}, AgentContext(), tools={"read_file": {}})
        assert r.success is True
        assert r.observation == "hello"
        assert r.tool_class == "INFO"
        assert r.attempts == 1

    def test_retry_on_transient_then_success(self, monkeypatch):
        _FakeNode.script = [
            {"success": False, "error": "connection timeout"},
            {"success": False, "error": "429 rate limit"},
            {"success": True, "content": "ok"},
        ]
        ex = _executor(monkeypatch, retry_attempts=3)
        r = ex.execute("read_file", {"file_path": "/x"}, AgentContext(), tools={"read_file": {}})
        assert r.success is True
        assert r.attempts == 3

    def test_terminal_error_no_retry(self, monkeypatch):
        _FakeNode.script = [{"success": False, "error": "文件不存在"}]
        ex = _executor(monkeypatch, retry_attempts=3)
        r = ex.execute("read_file", {"file_path": "/x"}, AgentContext(), tools={"read_file": {}})
        assert r.success is False
        assert r.attempts == 1  # 终端错误未重试

    def test_breaker_opens_after_consecutive_failures(self, monkeypatch):
        _FakeNode.script = [{"success": False, "error": "timeout"}] * 10
        ex = _executor(monkeypatch, retry_attempts=1)
        # 连续 3 次 execute 失败 → 第 4 次断路器拒绝
        for _ in range(3):
            r = ex.execute("read_file", {"file_path": "/x"}, AgentContext(), tools={"read_file": {}})
            assert r.success is False
        r4 = ex.execute("read_file", {"file_path": "/x"}, AgentContext(), tools={"read_file": {}})
        assert r4.success is False
        assert "断路器" in r4.observation

    def test_tracker_recorded(self, monkeypatch):
        _FakeNode.script = [{"success": True, "content": "data"}]
        ex = _executor(monkeypatch)
        tracker = ToolExecutionTracker()
        ex.execute("read_file", {"file_path": "/x"}, AgentContext(), tracker=tracker, tools={"read_file": {}})
        assert tracker.has_executions()
        rec = tracker.get_history()[0]
        assert rec["success"] is True

    def test_next_hint_contextual(self, monkeypatch):
        _FakeNode.script = [{"success": False, "error": "文件不存在: /x"}]
        ex = _executor(monkeypatch, retry_attempts=1)
        r = ex.execute("read_file", {"file_path": "/x"}, AgentContext(), tools={"read_file": {}})
        hint = r.next_hint()
        assert "list_files" in hint or "确认路径" in hint

    def test_no_tools_arg_skips_existence_check(self, monkeypatch):
        """tools=None（Plan-Execute 场景）→ 不预检，交给 ToolNode 分发。"""
        _FakeNode.script = [{"success": True, "content": "ok"}]
        ex = _executor(monkeypatch)
        r = ex.execute("any_tool", {"x": 1}, AgentContext())  # tools=None
        assert r.success is True
