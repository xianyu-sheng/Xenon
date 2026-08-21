"""spawn_agent 超时语义测试：超时必须触发协作式取消信号。

Bug 背景：_spawn_subagent 超时后仅 shutdown(wait=False) 放弃等待，子引擎
线程继续后台执行（可能写文件/调工具），与父 Agent 后续操作并发冲突。
代码检查了不存在的 cancel() 方法（react_engine.py:1165 附近），而
BaseEngine 的 F6 协作式中断通道 interrupt()（base.py:364，run 循环每轮
检查 _interrupted）从未被接上。

根因修复：超时路径调用 sub_engine.interrupt()，子引擎在下一个迭代
检查点自行退出；Python 无法强杀线程，协作式取消是正确语义。
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from xenon.engine.context import AgentContext
from xenon.engine.react_engine import ReActEngine


class _CancellableSub:
    """模拟子引擎：run 循环里检查 interrupt 标志（与 BaseEngine F6 同构）。"""

    def __init__(self, iterations: int = 100, iter_seconds: float = 0.05) -> None:
        self._interrupted = False
        self.interrupt_called = threading.Event()
        self.completed_iterations = 0
        self.exited_by_interrupt = False
        self._iterations = iterations
        self._iter_seconds = iter_seconds
        self._last_answer: str | None = None

    def interrupt(self) -> None:
        self._interrupted = True
        self.interrupt_called.set()

    def run(self, task: str, context: Any = None) -> str:
        for _ in range(self._iterations):
            if self._interrupted:
                self.exited_by_interrupt = True
                return "[interrupted]"
            self.completed_iterations += 1
            time.sleep(self._iter_seconds)
        return "done"

    def _format_result(self) -> str:  # pragma: no cover - helper
        return "formatted"


def _make_parent(sub: _CancellableSub, timeout: float = 1.0) -> ReActEngine:
    """构造 ReActEngine 并把子引擎构建替换为固定替身。"""
    eng = ReActEngine.__new__(ReActEngine)
    eng.llm = type("L", (), {"chat": staticmethod(lambda *a, **k: "x")})()
    eng.subagent_timeout = timeout
    eng.max_subagent_depth = 3
    eng._subagent_depth = 0
    eng._subagent_history = []
    eng._build_sub_engine = lambda etype, tid: sub  # type: ignore[method-assign]
    eng._format_sub_result = (  # type: ignore[method-assign]
        lambda tid, t, etype, ans, se, tr: f"R:{ans}"
    )
    return eng


class TestTimeoutCancelsSubagent:
    def test_timeout_signals_interrupt(self):
        """超时必须调用子引擎 interrupt()（F6 协作式取消）。"""
        sub = _CancellableSub(iterations=200, iter_seconds=0.05)  # ~10s 总时长
        eng = _make_parent(sub, timeout=0.5)
        t0 = time.time()
        result = eng._spawn_subagent({"task": "x", "timeout": 0.5}, AgentContext())
        elapsed = time.time() - t0

        assert elapsed < 2.0, "父引擎被卡住，未在超时后及时返回"
        assert "超时" in result or "R:" in result
        assert sub.interrupt_called.wait(timeout=3.0), (
            "超时后未向子引擎发送 interrupt() 取消信号"
        )

    def test_subengine_exits_after_interrupt(self):
        """收到 interrupt 的子引擎应在下一个检查点退出（不跑满全程）。"""
        sub = _CancellableSub(iterations=200, iter_seconds=0.05)
        eng = _make_parent(sub, timeout=0.4)
        eng._spawn_subagent({"task": "x", "timeout": 0.4}, AgentContext())

        # 等子线程跑到退出
        deadline = time.time() + 5.0
        while time.time() < deadline and not sub.exited_by_interrupt:
            time.sleep(0.05)
        assert sub.exited_by_interrupt, (
            f"子引擎未响应 interrupt 退出（跑了 {sub.completed_iterations} 轮）"
        )
        assert sub.completed_iterations < 200, "子引擎跑满全程 = 取消未生效"

    def test_no_timeout_runs_to_completion(self):
        """无超时路径行为不变：同步执行到完成。"""
        sub = _CancellableSub(iterations=3, iter_seconds=0.01)
        eng = _make_parent(sub, timeout=None)
        result = eng._spawn_subagent({"task": "x", "timeout": None}, AgentContext())
        assert "R:done" in result
        assert sub.completed_iterations == 3

    def test_batch_timeout_also_signals_interrupt(self):
        """批量并行 _spawn_all_subagents 的超时路径同样要发取消信号。"""
        subs = [_CancellableSub(iterations=200, iter_seconds=0.05) for _ in range(2)]
        eng = _make_parent(subs[0], timeout=0.5)
        it = iter(subs)
        eng._build_sub_engine = lambda etype, tid: next(it)  # type: ignore[method-assign]
        eng.subagent_timeout = 0.5

        t0 = time.time()
        eng._spawn_all_subagents(
            [{"task": "a", "timeout": 0.5}, {"task": "b", "timeout": 0.5}],
            AgentContext(),
        )
        elapsed = time.time() - t0
        assert elapsed < 3.0
        for s in subs:
            assert s.interrupt_called.wait(timeout=3.0), (
                "批量路径超时后未发送 interrupt()"
            )
