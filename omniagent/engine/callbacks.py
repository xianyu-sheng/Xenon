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
        """Rich 协议：渲染思考面板 — 摘要行 + 折叠详情（卡片式）。"""
        from rich.text import Text
        from rich.panel import Panel
        from rich.console import Group

        if self.is_empty:
            return

        tool_count = self.tool_call_count
        step_count = len(self.steps)

        # ── 摘要行（始终显示）──
        action_names = [s.action for s in self.steps if s.action]
        action_summary = " → ".join(action_names) if action_names else "纯思考"
        if tool_count > 0:
            summary = f"[bold cyan]🧠 {step_count} 轮推理[/bold cyan] [dim]·[/dim] [cyan]{tool_count} 次工具调用[/cyan] [dim]· {action_summary}[/dim]"
        else:
            summary = f"[bold cyan]🧠 {step_count} 轮推理[/bold cyan] [dim]· 纯思考[/dim]"

        yield Text.from_markup(summary)

        # ── 折叠详情（使用卡片组件）──
        detail_items = []
        from omniagent.repl.cards import ThinkingCard, ToolCallCard

        for i, step in enumerate(self.steps, 1):
            step_group: list = []
            if step.thought:
                thought_short = step.thought[:120].replace("\n", " ")
                if len(step.thought) > 120:
                    thought_short += "..."
                step_group.append(
                    Text.from_markup(f"[dim]#{i} 🤔 {thought_short}[/dim]")
                )
            if step.action:
                params_parts = []
                for k, v in step.action_input.items():
                    v_str = repr(v)
                    if len(v_str) > 50:
                        v_str = v_str[:47] + "..."
                    params_parts.append(f"{k}={v_str}")
                params_str = ", ".join(params_parts)
                from omniagent.repl.cards import TOOL_ICONS
                icon = TOOL_ICONS.get(step.action, "🔧")
                step_group.append(
                    Text.from_markup(f"[dim]{icon} {step.action}({params_str})[/dim]")
                )
            if step.observation:
                obs_short = step.observation[:100].replace("\n", " ")
                if len(step.observation) > 100:
                    obs_short += "..."
                step_group.append(
                    Text.from_markup(f"[dim]👀 {obs_short}[/dim]")
                )

            if step_group:
                detail_items.append(Text("  ").join(step_group))

        for err in self.errors:
            detail_items.append(Text.from_markup(f"  [red]❌ {err}[/red]"))
        for warn in self.warnings:
            detail_items.append(Text.from_markup(f"  [yellow]⚠️  {warn}[/yellow]"))

        if detail_items:
            yield Panel(
                Group(*detail_items),
                title="[dim]推理详情[/dim]",
                border_style="dim",
                padding=(0, 1),
            )


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

    # 写入/危险类工具（完整卡片）
    _NOTIFY_TOOLS = {
        "write_file", "edit_file", "batch_write", "batch_edit",
        "create_directory", "move_file", "copy_file", "delete_file",
        "command", "git", "mcp_call", "spawn_agent",
    }

    def __init__(self, *, verbose: bool = False) -> None:
        self.verbose = verbose
        self._panel = ThinkingPanel()
        # 懒加载 Rich Console（模块级重用）
        self._console = None

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

    def on_observe(self, observation: str) -> None:
        self._panel.add_observation(observation)

        from omniagent.repl.cards import ToolResultCard

        # 检测通知类型
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

    @staticmethod
    def _brief_params(action: str, params: dict) -> str:
        """生成简要参数预览（一行，控制长度）。"""
        if action == "command":
            cmd = str(params.get("command", ""))
            return cmd[:100] if cmd else ""
        elif action == "git":
            git_cmd = str(params.get("git_command") or params.get("command", ""))
            return git_cmd[:80] if git_cmd else ""
        elif action in ("write_file", "edit_file", "read_file"):
            path = str(params.get("file_path", ""))
            return path[:80] if path else ""
        elif action in ("list_files", "create_directory"):
            path = str(params.get("file_path") or params.get("path", ""))
            return path[:60] if path else ""
        elif action == "search_files":
            pattern = str(params.get("search_pattern") or params.get("pattern", ""))
            return pattern[:60] if pattern else ""
        elif action in ("web_fetch", "github_fetch"):
            url = str(params.get("url", ""))
            return url[:80] if url else ""
        # 通用：显示第一个关键参数
        for key in ("file_path", "path", "url", "query", "search_pattern"):
            if key in params:
                return str(params[key])[:60]
        return ""

    def on_step(self, step_id: int, total: int, task: str) -> None:
        from omniagent.repl.cards import StepCard
        self._rich_console.print(StepCard(step_id, total, task, status="running"))

    def on_step_done(self, step_id: int, success: bool, summary: str) -> None:
        from omniagent.repl.cards import StepCard
        status = "done" if success else "failed"
        self._rich_console.print(StepCard(step_id, 0, summary, status=status))

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
