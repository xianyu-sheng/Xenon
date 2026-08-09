"""断路器错误分类回归测试。

Bug 背景（模拟调用实测复现）：ToolExecutor 的 except 分支对一切异常
无条件 breaker.record_failure()，LLM 连续 3 次构造非法参数
（如 read_file 缺 file_path）后断路器开启，后续参数完全合法的调用
也被「连败熔断」拒绝——断路器本应度量工具自身故障（超时/网络/限流），
却惩罚了调用方（LLM）的错误。

修复：终端错误（is_terminal_error：参数缺失/文件不存在/安全拦截等）
不再计入断路器；仅瞬时故障计入。
"""

from __future__ import annotations

import logging

import pytest

from xenon.engine.context import AgentContext
from xenon.nodes.tool_executor import ToolExecutor, is_terminal_error


@pytest.fixture(autouse=True)
def _quiet():
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


def _ctx(ws: str) -> AgentContext:
    ctx = AgentContext()
    ctx.set("workspace_root", ws)
    return ctx


class TestTerminalErrorClassification:
    @pytest.mark.parametrize(
        "err",
        [
            "[exec_read_file] read_file 需要 file_path",
            "clean_imports 需要 file_path 参数",
            "路径越界: /etc/hostname 不在允许的目录 /tmp 下",
            "危险命令被拦截: 匹配到禁止模式",
            "文件不存在: /x/y.py",
            "ValueError: 非法参数",
        ],
    )
    def test_caller_faults_are_terminal(self, err):
        assert is_terminal_error(err) is True

    @pytest.mark.parametrize(
        "err",
        [
            "Connection reset by peer",
            "timeout after 30s",
            "429 rate limit exceeded",
            "broken pipe",
        ],
    )
    def test_tool_faults_are_transient(self, err):
        assert is_terminal_error(err) is False


class TestBreakerNotTrippedByCallerFaults:
    def test_param_errors_do_not_trip_breaker(self, tmp_path):
        (tmp_path / "real.txt").write_text("data")
        ctx = _ctx(str(tmp_path))
        ex = ToolExecutor()
        for _ in range(5):  # 远超默认熔断阈值 3
            ex.execute("read_file", {}, ctx)
        ok = ex.execute("read_file", {"file_path": str(tmp_path / "real.txt")}, ctx)
        assert "data" in ok.observation
        assert "断路器" not in ok.observation

    def test_security_denials_do_not_trip_breaker(self, tmp_path):
        (tmp_path / "real.txt").write_text("data")
        ctx = _ctx(str(tmp_path))
        ex = ToolExecutor()
        for _ in range(4):
            ex.execute("read_file", {"file_path": "/etc/hostname"}, ctx)
        ok = ex.execute("read_file", {"file_path": str(tmp_path / "real.txt")}, ctx)
        assert "data" in ok.observation

    def test_file_not_found_does_not_trip_breaker(self, tmp_path):
        (tmp_path / "real.txt").write_text("data")
        ctx = _ctx(str(tmp_path))
        ex = ToolExecutor()
        for i in range(4):
            ex.execute("read_file", {"file_path": str(tmp_path / f"ghost{i}.txt")}, ctx)
        ok = ex.execute("read_file", {"file_path": str(tmp_path / "real.txt")}, ctx)
        assert "data" in ok.observation
