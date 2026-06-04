"""引擎回调接口 — 让引擎通知外部（REPL/测试）关键事件。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


class EngineCallback:
    """引擎回调基类。所有方法默认空实现，子类按需覆写。"""

    def on_think(self, thought: str) -> None:
        """LLM 思考过程。"""
        pass

    def on_act(self, action: str, action_input: dict) -> None:
        """即将执行工具。"""
        pass

    def on_observe(self, observation: str) -> None:
        """工具执行结果。"""
        pass

    def on_step(self, step_id: int, total: int, task: str) -> None:
        """Plan-Execute: 开始执行某一步。"""
        pass

    def on_step_done(self, step_id: int, success: bool, summary: str) -> None:
        """Plan-Execute: 某一步执行完成。"""
        pass

    def on_review(self, score: int, passed: bool, feedback: str) -> None:
        """Reflection: 审查结果。"""
        pass

    def on_error(self, error: str) -> None:
        """错误事件。"""
        pass

    def on_warning(self, warning: str) -> None:
        """警告事件。"""
        pass

    def on_finish(self, result: str) -> None:
        """引擎执行完成。"""
        pass


class SilentCallback(EngineCallback):
    """静默回调，用于测试。只记录事件不输出。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []

    def on_think(self, thought: str) -> None:
        self.events.append(("think", thought))

    def on_act(self, action: str, action_input: dict) -> None:
        self.events.append(("act", (action, action_input)))

    def on_observe(self, observation: str) -> None:
        self.events.append(("observe", observation))

    def on_step(self, step_id: int, total: int, task: str) -> None:
        self.events.append(("step", (step_id, total, task)))

    def on_step_done(self, step_id: int, success: bool, summary: str) -> None:
        self.events.append(("step_done", (step_id, success, summary)))

    def on_review(self, score: int, passed: bool, feedback: str) -> None:
        self.events.append(("review", (score, passed, feedback)))

    def on_error(self, error: str) -> None:
        self.events.append(("error", error))

    def on_warning(self, warning: str) -> None:
        self.events.append(("warning", warning))

    def on_finish(self, result: str) -> None:
        self.events.append(("finish", result))


class ConsoleCallback(EngineCallback):
    """控制台回调，REPL 使用。带颜色输出关键事件。"""

    def __init__(self, *, verbose: bool = False) -> None:
        self.verbose = verbose

    def on_think(self, thought: str) -> None:
        if self.verbose:
            print(f"  🤔 {thought[:200]}")

    def on_act(self, action: str, action_input: dict) -> None:
        params_str = ", ".join(f"{k}={repr(v)[:80]}" for k, v in action_input.items())
        print(f"  🔧 {action}({params_str})")

    def on_observe(self, observation: str) -> None:
        if self.verbose:
            obs_preview = observation[:300].replace("\n", " ")
            print(f"  👀 {obs_preview}")

    def on_step(self, step_id: int, total: int, task: str) -> None:
        print(f"  📋 步骤 {step_id}/{total}: {task}")

    def on_step_done(self, step_id: int, success: bool, summary: str) -> None:
        icon = "✓" if success else "✗"
        preview = summary[:100].replace("\n", " ")
        print(f"  {icon} 步骤 {step_id}: {preview}")

    def on_review(self, score: int, passed: bool, feedback: str) -> None:
        icon = "✓" if passed else "✗"
        print(f"  🔍 审查 {icon} 评分: {score}/10 — {feedback[:100]}")

    def on_error(self, error: str) -> None:
        print(f"  ❌ {error}")

    def on_warning(self, warning: str) -> None:
        print(f"  ⚠️  {warning}")

    def on_finish(self, result: str) -> None:
        if self.verbose:
            print(f"  ✅ 完成 ({len(result)} 字符)")
