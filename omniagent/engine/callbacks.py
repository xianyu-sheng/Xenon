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

    def on_observe(self, observation: str, card_data: dict | None = None) -> None:
        """工具执行结果。card_data 为可选的结构化数据（来自 ToolExecuteResult.to_card_data()）。"""
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

    def on_observe(self, observation: str, card_data: dict | None = None) -> None:
        self.events.append(("observe", observation, card_data))

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
        """Rich 协议：渲染思考面板 — 摘要行 + 折叠详情。"""
        from rich.text import Text

        if self.is_empty:
            return

        tool_count = self.tool_call_count
        step_count = len(self.steps)

        # ── 摘要行（始终显示）──
        action_names = [s.action for s in self.steps if s.action]
        action_summary = " → ".join(action_names) if action_names else "think"
        if tool_count > 0:
            summary = f"[dim]{step_count} rounds[/dim] [dim]·[/dim] [dim]{tool_count} tool calls[/dim] [dim]· {action_summary}[/dim]"
        else:
            summary = f"[dim]{step_count} rounds[/dim] [dim]· think[/dim]"

        yield Text.from_markup(summary)

        # ── 折叠详情（dim 文本行，无边框）──
        from omniagent.repl.cards import TOOL_ICONS

        for i, step in enumerate(self.steps, 1):
            parts: list[str] = []
            if step.thought:
                thought_short = step.thought[:120].replace("\n", " ")
                if len(step.thought) > 120:
                    thought_short += "..."
                parts.append(f"[dim]#{i} {thought_short}[/dim]")
            if step.action:
                icon = TOOL_ICONS.get(step.action, "🔧")
                params_parts = []
                for k, v in step.action_input.items():
                    v_str = repr(v)
                    if len(v_str) > 50:
                        v_str = v_str[:47] + "..."
                    params_parts.append(f"{k}={v_str}")
                params_str = ", ".join(params_parts)
                parts.append(f"[dim]{icon} {step.action}({params_str})[/dim]")
            if step.observation:
                obs_short = step.observation[:100].replace("\n", " ")
                if len(step.observation) > 100:
                    obs_short += "..."
                parts.append(f"[dim]  {obs_short}[/dim]")

            if parts:
                yield Text.from_markup("  " + "  ".join(parts))

        for err in self.errors:
            yield Text.from_markup(f"  [red]❌ {err}[/red]")
        for warn in self.warnings:
            yield Text.from_markup(f"  [yellow]⚠️  {warn}[/yellow]")


class ConsoleCallback(EngineCallback):
    """
    控制台回调，REPL 使用 — 卡片式 UI 渲染。

    - 默认模式：收集思考步骤到 ThinkingPanel，工具调用以卡片实时显示
    - verbose 模式：额外显示思考详细内容

    工具调用显示策略（卡片化）:
    - 写入/命令/Git 操作 → 完整 ToolCallCard（带边框 + 图标 + 参数列表）
    - 读取/搜索 → 紧凑 ToolCallCard（单行 dim 风格）
    - 成功/失败 → ToolResultCard（绿色/红色边框卡片）
    - 错误 → ErrorCard（红色醒目卡片）
    """

    # 写入/危险类工具（完整卡片）— 从 cards.py 统一来源导入
    from omniagent.repl.cards import NOTIFY_TOOLS as _NOTIFY_TOOLS

    def __init__(self, *, verbose: bool = False) -> None:
        self.verbose = verbose
        self._panel = ThinkingPanel()
        # 懒加载 Rich Console（模块级重用）
        self._console = None
        # 跟踪步骤总数（on_step 传入，on_step_done 复用）
        self._current_step_total: int = 0

    @property
    def _rich_console(self):
        """懒加载 Rich Console 实例。"""
        if self._console is None:
            from rich.console import Console
            self._console = Console(highlight=False)
        return self._console

    def on_think(self, thought: str) -> None:
        self._panel.add_thought(thought)
        if self.verbose:
            from omniagent.repl.cards import ThinkingCard
            self._rich_console.print(ThinkingCard(thought, compact=True))

    def on_act(self, action: str, action_input: dict) -> None:
        self._panel.add_action(action, action_input)
        from omniagent.repl.cards import ToolCallCard
        card = ToolCallCard(action, action_input, status="running")
        # 写入/敏感工具 → 始终显示完整卡片；读取工具 → 仅在 verbose 显示
        if action in self._NOTIFY_TOOLS:
            self._rich_console.print(card)
        elif self.verbose:
            self._rich_console.print(card)

    def on_observe(self, observation: str, card_data: dict | None = None) -> None:
        self._panel.add_observation(observation)

        from omniagent.repl.cards import ToolResultCard

        # ── 优先使用结构化 card_data（来自 ToolExecuteResult.to_card_data()）──
        if card_data:
            card = ToolResultCard(
                tool_name=card_data.get("tool_name", ""),
                success=card_data.get("success", False),
                summary=observation,
                permission_denied=card_data.get("permission_denied", False),
                circuit_breaker_tripped=card_data.get("circuit_breaker_tripped", False),
            )
            self._rich_console.print(card)
            return

        # ── 回退：从纯文本 observation 解析（兼容未升级的引擎/异步引擎）──
        is_success = observation.startswith("✅") or "执行完成" in observation[:50]
        is_failure = (
            observation.startswith(("❌", "🛑", "⛔"))
            or any(kw in observation[:50] for kw in ("失败", "错误", "拒绝"))
        )

        if is_success or is_failure:
            card = ToolResultCard(
                tool_name="",
                success=is_success,
                summary=observation,
                permission_denied=observation.startswith("⛔"),
                circuit_breaker_tripped=observation.startswith("🛑"),
            )
            self._rich_console.print(card)
        elif observation.startswith(("📖", "📋", "🔍", "🌐", "🐙")):
            # 信息类工具 — dim 显示
            obs_preview = observation[:150].replace("\n", " ")
            from rich.text import Text
            self._rich_console.print(Text.from_markup(f"  [dim]{obs_preview}[/dim]"))
        elif self.verbose:
            obs_preview = observation[:150].replace("\n", " ")
            from rich.text import Text
            self._rich_console.print(Text.from_markup(f"  [dim]👀 {obs_preview}[/dim]"))

    def on_step(self, step_id: int, total: int, task: str) -> None:
        self._current_step_total = total
        from omniagent.repl.cards import StepCard
        self._rich_console.print(StepCard(step_id, total, task, status="running"))

    def on_step_done(self, step_id: int, success: bool, summary: str) -> None:
        from omniagent.repl.cards import StepCard
        status = "done" if success else "failed"
        total = self._current_step_total  # 复用 on_step 传入的 total
        self._rich_console.print(StepCard(step_id, total, summary, status=status))

    def on_review(self, score: int, passed: bool, feedback: str) -> None:
        if self.verbose:
            icon = "✓" if passed else "✗"
            from rich.text import Text
            self._rich_console.print(
                Text.from_markup(f"  [dim]🔍 审查 {icon} 评分: {score}/10 — {feedback[:100]}[/dim]")
            )

    def on_error(self, error: str) -> None:
        self._panel.add_error(error)
        from omniagent.repl.cards import ErrorCard
        self._rich_console.print(ErrorCard(error))

    def on_warning(self, warning: str) -> None:
        self._panel.add_warning(warning)
        from omniagent.repl.cards import ErrorCard
        self._rich_console.print(ErrorCard(warning, title="警告", is_warning=True))

    def on_finish(self, result: str) -> None:
        if self.verbose:
            from rich.text import Text
            self._rich_console.print(
                Text.from_markup(f"  [dim]✅ 完成 ({len(result)} 字符)[/dim]")
            )

    def get_thinking_panel(self) -> ThinkingPanel | None:
        """获取思考面板，如果没有内容返回 None。"""
        if self._panel.is_empty:
            return None
        return self._panel
