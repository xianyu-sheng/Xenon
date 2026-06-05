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


# ── 思考步骤数据结构 ──────────────────────────────────────────

@dataclass
class ThinkingStep:
    """一次 ReAct 迭代的完整记录。"""
    thought: str = ""
    action: str = ""
    action_input: dict = field(default_factory=dict)
    observation: str = ""
    is_error: bool = False


class ThinkingPanel:
    """
    思考过程折叠面板 — 自定义 Rich 可渲染组件。

    将 ReAct 引擎的思考步骤收集起来，渲染为一个简洁的面板：
    - 标题显示摘要（如 "深度思考 · 3 次工具调用"）
    - 内部展示每一步的思考、工具调用和观察结果
    - 使用 dim 边框，与最终答案面板形成视觉层次
    """

    def __init__(self) -> None:
        self.steps: list[ThinkingStep] = []
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self._current_step: ThinkingStep | None = None

    def add_thought(self, thought: str) -> None:
        """记录一次思考。如果当前没有活跃步骤，创建新步骤。"""
        if self._current_step is None:
            self._current_step = ThinkingStep()
        self._current_step.thought = thought

    def add_action(self, action: str, action_input: dict) -> None:
        """记录一次工具调用。"""
        if self._current_step is None:
            self._current_step = ThinkingStep()
        self._current_step.action = action
        self._current_step.action_input = action_input

    def add_observation(self, observation: str) -> None:
        """记录一次观察结果，并完成当前步骤。"""
        if self._current_step is None:
            self._current_step = ThinkingStep()
        self._current_step.observation = observation
        self.steps.append(self._current_step)
        self._current_step = None

    def add_error(self, error: str) -> None:
        self.errors.append(error)

    def add_warning(self, warning: str) -> None:
        self.warnings.append(warning)

    @property
    def tool_call_count(self) -> int:
        return len([s for s in self.steps if s.action])

    @property
    def is_empty(self) -> bool:
        return len(self.steps) == 0 and len(self.errors) == 0 and len(self.warnings) == 0

    def __rich_console__(self, console, options):
        """Rich 协议：渲染为可折叠的思考面板。"""
        from rich.text import Text
        from rich.panel import Panel
        from rich.console import Group

        if self.is_empty:
            return

        # 构建摘要标题
        tool_count = self.tool_call_count
        step_count = len(self.steps)
        if tool_count > 0:
            title = f"🧠 深度思考 · {step_count} 轮推理 · {tool_count} 次工具调用"
        else:
            title = f"🧠 深度思考 · {step_count} 轮推理"

        # 构建内部内容
        lines = []
        for i, step in enumerate(self.steps, 1):
            # 步骤分隔
            if i > 1:
                lines.append(Text("─" * 40, style="dim"))

            # 思考内容
            if step.thought:
                thought_preview = step.thought[:300]
                if len(step.thought) > 300:
                    thought_preview += "..."
                lines.append(Text(f"  🤔 {thought_preview}", style="dim"))

            # 工具调用
            if step.action:
                params_parts = []
                for k, v in step.action_input.items():
                    v_str = repr(v)
                    if len(v_str) > 60:
                        v_str = v_str[:57] + "..."
                    params_parts.append(f"{k}={v_str}")
                params_str = ", ".join(params_parts)
                lines.append(Text(f"  🔧 {step.action}({params_str})", style="cyan"))

            # 观察结果
            if step.observation:
                obs_preview = step.observation[:200].replace("\n", " ")
                if len(step.observation) > 200:
                    obs_preview += "..."
                lines.append(Text(f"  👀 {obs_preview}", style="dim"))

        # 错误和警告
        for err in self.errors:
            lines.append(Text(f"  ❌ {err}", style="red"))
        for warn in self.warnings:
            lines.append(Text(f"  ⚠️  {warn}", style="yellow"))

        # 渲染为 Panel
        content = Group(*lines)
        yield Panel(
            content,
            title=f"[dim]{title}[/dim]",
            border_style="dim",
            padding=(0, 1),
        )


class ConsoleCallback(EngineCallback):
    """
    控制台回调，REPL 使用。

    - 默认模式：收集思考步骤到 ThinkingPanel，不实时打印
    - verbose 模式：实时打印 + 收集（用于调试）
    """

    def __init__(self, *, verbose: bool = False) -> None:
        self.verbose = verbose
        self._panel = ThinkingPanel()

    def on_think(self, thought: str) -> None:
        self._panel.add_thought(thought)
        if self.verbose:
            print(f"  🤔 {thought[:200]}")

    def on_act(self, action: str, action_input: dict) -> None:
        self._panel.add_action(action, action_input)
        if self.verbose:
            params_str = ", ".join(f"{k}={repr(v)[:80]}" for k, v in action_input.items())
            print(f"  🔧 {action}({params_str})")

    def on_observe(self, observation: str) -> None:
        self._panel.add_observation(observation)
        if self.verbose:
            obs_preview = observation[:300].replace("\n", " ")
            print(f"  👀 {obs_preview}")

    def on_step(self, step_id: int, total: int, task: str) -> None:
        if self.verbose:
            print(f"  📋 步骤 {step_id}/{total}: {task}")

    def on_step_done(self, step_id: int, success: bool, summary: str) -> None:
        if self.verbose:
            icon = "✓" if success else "✗"
            preview = summary[:100].replace("\n", " ")
            print(f"  {icon} 步骤 {step_id}: {preview}")

    def on_review(self, score: int, passed: bool, feedback: str) -> None:
        if self.verbose:
            icon = "✓" if passed else "✗"
            print(f"  🔍 审查 {icon} 评分: {score}/10 — {feedback[:100]}")

    def on_error(self, error: str) -> None:
        self._panel.add_error(error)
        if self.verbose:
            print(f"  ❌ {error}")

    def on_warning(self, warning: str) -> None:
        self._panel.add_warning(warning)
        if self.verbose:
            print(f"  ⚠️  {warning}")

    def on_finish(self, result: str) -> None:
        if self.verbose:
            print(f"  ✅ 完成 ({len(result)} 字符)")

    def get_thinking_panel(self) -> ThinkingPanel | None:
        """获取思考面板，如果没有内容返回 None。"""
        if self._panel.is_empty:
            return None
        return self._panel
