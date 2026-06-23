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
        """Rich 协议：渲染思考面板 — 摘要行 + 折叠详情。"""
        from rich.text import Text
        from rich.panel import Panel
        from rich.console import Group
        from rich.table import Table

        if self.is_empty:
            return

        tool_count = self.tool_call_count
        step_count = len(self.steps)

        # ── 摘要行（始终显示，一目了然）──
        action_names = [s.action for s in self.steps if s.action]
        action_summary = " → ".join(action_names) if action_names else "纯思考"
        if tool_count > 0:
            summary = f"[bold cyan]🧠 {step_count} 轮推理[/bold cyan] [dim]·[/dim] [cyan]{tool_count} 次工具调用[/cyan] [dim]· {action_summary}[/dim]"
        else:
            summary = f"[bold cyan]🧠 {step_count} 轮推理[/bold cyan] [dim]· 纯思考[/dim]"

        yield Text.from_markup(summary)

        # ── 折叠详情（dim 风格，紧凑排列）──
        detail_lines = []
        for i, step in enumerate(self.steps, 1):
            parts = []
            if step.thought:
                thought_short = step.thought[:120].replace("\n", " ")
                if len(step.thought) > 120:
                    thought_short += "..."
                parts.append(f"🤔 {thought_short}")
            if step.action:
                params_parts = []
                for k, v in step.action_input.items():
                    v_str = repr(v)
                    if len(v_str) > 50:
                        v_str = v_str[:47] + "..."
                    params_parts.append(f"{k}={v_str}")
                params_str = ", ".join(params_parts)
                parts.append(f"🔧 {step.action}({params_str})")
            if step.observation:
                obs_short = step.observation[:100].replace("\n", " ")
                if len(step.observation) > 100:
                    obs_short += "..."
                parts.append(f"👀 {obs_short}")

            step_text = " | ".join(parts)
            detail_lines.append(Text(f"  {i:>2}. {step_text}", style="dim"))

        for err in self.errors:
            detail_lines.append(Text(f"  ❌ {err}", style="red"))
        for warn in self.warnings:
            detail_lines.append(Text(f"  ⚠️  {warn}", style="yellow"))

        if detail_lines:
            yield Panel(
                Group(*detail_lines),
                title="[dim]推理详情[/dim]",
                border_style="dim",
                padding=(0, 1),
            )


class ConsoleCallback(EngineCallback):
    """
    控制台回调，REPL 使用。

    - 默认模式：收集思考步骤到 ThinkingPanel，工具调用实时显示简要通知
    - verbose 模式：实时打印 + 收集详细信息（用于调试）

    工具调用显示策略（类似 Claude Code 的权限通知）:
    - 写入/命令/Git 操作 → 醒目显示（绿色/黄色边框）
    - 读取/搜索 → 低调显示（dim 灰色）
    - 失败 → 红色醒目显示
    """

    # 写入/危险类工具（始终显示）
    _NOTIFY_TOOLS = {
        "write_file", "edit_file", "batch_write", "batch_edit",
        "create_directory", "move_file", "copy_file", "delete_file",
        "command", "git", "mcp_call", "spawn_agent",
    }

    def __init__(self, *, verbose: bool = False) -> None:
        self.verbose = verbose
        self._panel = ThinkingPanel()

    def on_think(self, thought: str) -> None:
        self._panel.add_thought(thought)
        if self.verbose:
            print(f"  🤔 {thought[:200]}")

    def on_act(self, action: str, action_input: dict) -> None:
        self._panel.add_action(action, action_input)
        # 简要参数预览
        params_brief = self._brief_params(action, action_input)
        if action in self._NOTIFY_TOOLS:
            # 写入/命令类工具 → 醒目显示
            icon = "📄" if action in ("write_file", "batch_write") else \
                  "✏️" if action in ("edit_file", "batch_edit") else \
                  "📁" if action == "create_directory" else \
                  "⚡" if action == "command" else \
                  "🔀" if action == "git" else "🔧"
            print(f"\n  {icon} {action} {params_brief}")
        elif self.verbose:
            print(f"  🔧 {action}({params_brief})")

    def on_observe(self, observation: str) -> None:
        self._panel.add_observation(observation)
        obs_preview = observation[:150].replace("\n", " ")

        # 成功标记: ✅ 开头, 或包含 "执行完成"
        if observation.startswith("✅") or "执行完成" in observation[:50]:
            print(f"  ✅ {obs_preview}")
        # 失败标记: ❌/🛑/⛔ 开头, 或包含 "失败"/"错误"/"拒绝"
        elif (
            observation.startswith(("❌", "🛑", "⛔"))
            or any(kw in observation[:50] for kw in ("失败", "错误", "拒绝"))
        ):
            print(f"  ❌ {obs_preview}")
        # 信息类工具 — dim 显示（始终显示，不仅 verbose）
        elif observation.startswith(("📖", "📋", "🔍", "🌐", "🐙")):
            print(f"  {obs_preview}")
        elif self.verbose:
            print(f"  👀 {obs_preview}")

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
        print(f"  📋 步骤 {step_id}/{total}: {task[:100]}")

    def on_step_done(self, step_id: int, success: bool, summary: str) -> None:
        icon = "✅" if success else "❌"
        preview = summary[:100].replace("\n", " ")
        print(f"  {icon} 步骤 {step_id}: {preview}")

    def on_review(self, score: int, passed: bool, feedback: str) -> None:
        if self.verbose:
            icon = "✓" if passed else "✗"
            print(f"  🔍 审查 {icon} 评分: {score}/10 — {feedback[:100]}")

    def on_error(self, error: str) -> None:
        self._panel.add_error(error)
        print(f"  ❌ {error[:200]}")

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
