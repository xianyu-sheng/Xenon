"""增强输出渲染器 — 类 Claude Code 体验。

特性:
- 代码语法高亮: 使用 Rich Syntax 渲染代码块
- 数学公式渲染: LaTeX $...$ / $$...$$ 转换为终端友好格式
- 推理过程折叠: 思考步骤默认折叠, 只显示摘要行
- 答案优先布局: 最终答案在前, 推理过程在后

使用方式:
    renderer = OutputRenderer()
    renderer.render_answer(result, thinking_panel)
"""

from __future__ import annotations

import re
from typing import Any

from rich.console import Console, Group, RenderableType
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text
from rich.rule import Rule


class OutputRenderer:
    """增强输出渲染器 — 类 Claude Code 的回答展示体验。

    与 Claude Code 对齐的设计:
    1. 推理过程折叠 — 默认只显示摘要, verbose 模式展开
    2. 代码高亮 — 自动检测语言并语法高亮
    3. 数学公式 — 终端友好的公式渲染
    """

    def __init__(self, *, verbose: bool = False) -> None:
        self.verbose = verbose
        self._console = Console()

    # ═══════════════════════════════════════════════════════════
    # 卡片渲染便捷方法
    # ═══════════════════════════════════════════════════════════

    def render_tool_call(self, tool_name: str, params: dict, *, status: str = "running") -> None:
        """使用 ToolCallCard 渲染工具调用。"""
        from omniagent.repl.cards import ToolCallCard
        self._console.print(ToolCallCard(tool_name, params, status=status))

    def render_tool_result(
        self, tool_name: str, success: bool, summary: str,
        *, error: str | None = None, permission_denied: bool = False,
        circuit_breaker_tripped: bool = False,
    ) -> None:
        """使用 ToolResultCard 渲染工具结果。"""
        from omniagent.repl.cards import ToolResultCard
        self._console.print(ToolResultCard(
            tool_name, success, summary,
            error=error,
            permission_denied=permission_denied,
            circuit_breaker_tripped=circuit_breaker_tripped,
        ))

    def render_error(self, message: str, *, title: str = "错误", details: str | None = None) -> None:
        """使用 ErrorCard 渲染错误。"""
        from omniagent.repl.cards import ErrorCard
        self._console.print(ErrorCard(message, title=title, details=details))

    # ═══════════════════════════════════════════════════════════
    # 主渲染入口
    # ═══════════════════════════════════════════════════════════

    def render_answer(
        self,
        result: str,
        thinking_panel: Any = None,
        *,
        title: str = "Assistant",
        border_style: str = "green",
    ) -> None:
        """渲染引擎最终回答 — 答案优先 + 思考折叠。

        Claude Code 风格布局:
        ┌──────────────────────────────────────┐
        │  🤖 Assistant                       │
        │                                      │
        │  (带语法高亮的 Markdown 内容)         │
        │                                      │
        └──────────────────────────────────────┘
        🧠 深度思考 · 3次工具调用 [展开查看 /verbose]
        """
        # ── 1. 渲染最终答案 (主体) ──
        rendered = self._render_markdown_enhanced(result)
        self._console.print(Panel(
            rendered,
            title=f"[bold green]{title}[/bold green]",
            border_style=border_style,
            padding=(1, 2),
        ))

        # ── 2. 渲染思考摘要 (折叠) ──
        if thinking_panel is not None and hasattr(thinking_panel, 'is_empty'):
            if not thinking_panel.is_empty:
                self._render_thinking_fold(thinking_panel)

    def render_thinking_panel(self, panel: Any) -> None:
        """渲染思考面板 — verbose 模式展开全部细节。"""
        if self.verbose and hasattr(panel, '__rich_console__'):
            self._console.print(panel)
        else:
            self._render_thinking_fold(panel)

    # ═══════════════════════════════════════════════════════════
    # 思考折叠渲染
    # ═══════════════════════════════════════════════════════════

    def _render_thinking_fold(self, panel: Any) -> None:
        """渲染折叠的思考摘要 — Claude Code 风格。

        非 verbose: 仅一行摘要 (dim 样式)
        verbose:   全部步骤 (通过 render_thinking_panel 调用)
        """
        if not hasattr(panel, 'steps'):
            return

        tool_count = panel.tool_call_count if hasattr(panel, 'tool_call_count') else 0
        step_count = len(panel.steps) if hasattr(panel, 'steps') else 0

        if step_count == 0:
            return

        # ── 构建摘要行 ──
        action_names = []
        for s in panel.steps:
            if hasattr(s, 'action') and s.action:
                action_names.append(s.action)

        action_summary = " → ".join(action_names[:5]) if action_names else "纯思考"
        if len(action_names) > 5:
            action_summary += f" +{len(action_names) - 5}"

        summary = Text()
        if tool_count > 0:
            summary.append("🧠 ", style="bold cyan")
            summary.append(f"{step_count}轮推理", style="cyan")
            summary.append(" · ", style="dim")
            summary.append(f"{tool_count}次工具调用", style="cyan")
            summary.append(" · ", style="dim")
            summary.append(action_summary, style="dim italic")
        else:
            summary.append("🧠 ", style="bold cyan")
            summary.append(f"{step_count}轮推理", style="cyan")
            summary.append(" · 纯思考", style="dim")

        if not self.verbose:
            summary.append("  [输入 /verbose 展开]", style="dim italic")

        self._console.print(summary)

        # ── Verbose 展开详情 ──
        if self.verbose:
            self._render_thinking_details(panel)

    def _render_thinking_details(self, panel: Any) -> None:
        """展开思考详情 — 结构化展示每一步。"""
        from rich.table import Table

        table = Table(show_header=False, box=None, padding=(0, 1))
        table.add_column("icon", width=2)
        table.add_column("step", width=4, style="dim")
        table.add_column("detail")

        for i, step in enumerate(panel.steps, 1):
            parts = []
            if hasattr(step, 'thought') and step.thought:
                thought = step.thought[:120].replace("\n", " ")
                if len(step.thought) > 120:
                    thought += "..."
                parts.append(Text(f"🤔 {thought}", style="dim"))

            if hasattr(step, 'action') and step.action:
                params_str = ", ".join(
                    f"{k}={repr(v)[:40]}"
                    for k, v in (step.action_input.items() if hasattr(step, 'action_input') else [])
                )
                parts.append(Text(f"🔧 {step.action}({params_str})", style="yellow"))

            if hasattr(step, 'observation') and step.observation:
                obs = step.observation[:100].replace("\n", " ")
                if len(step.observation) > 100:
                    obs += "..."
                parts.append(Text(f"👀 {obs}", style="dim"))

            if parts:
                table.add_row("", str(i), Group(*parts))

        if panel.errors if hasattr(panel, 'errors') else []:
            for err in panel.errors:
                table.add_row("", "❌", Text(err, style="red"))

        self._console.print(Panel(
            table,
            title="[dim]推理详情[/dim]",
            border_style="dim",
            padding=(0, 1),
        ))

    # ═══════════════════════════════════════════════════════════
    # 增强 Markdown 渲染 (代码高亮 + 公式)
    # ═══════════════════════════════════════════════════════════

    def _render_markdown_enhanced(self, content: str) -> RenderableType:
        """增强 Markdown 渲染 — 代码高亮 + 公式渲染。

        处理流程:
        1. 提取代码块，用 Rich Syntax 渲染
        2. 处理 LaTeX 公式 ($...$ / $$...$$)
        3. 其余内容用 Markdown 渲染
        """
        # ── 预处理: 提取并保护代码块 ──
        code_blocks: dict[str, str] = {}
        processed = self._extract_code_blocks(content, code_blocks)

        # ── 预处理: 渲染数学公式 ──
        processed = self._render_math_inline(processed)

        # ── 恢复代码块并渲染 ──
        if code_blocks:
            # 有代码块: 用自定义渲染
            return self._render_with_syntax_blocks(processed, code_blocks)
        else:
            # 无代码块: 直接用 Rich Markdown
            return Markdown(processed, code_theme="monokai")

    def _extract_code_blocks(self, content: str, store: dict[str, str]) -> str:
        """提取 markdown 代码块，替换为占位符用于后续恢复。"""
        pattern = re.compile(r'```(\w*)\s*\n(.*?)```', re.DOTALL)
        counter = 0

        def _replace(m: re.Match) -> str:
            nonlocal counter
            lang = m.group(1) or "text"
            code = m.group(2)
            placeholder = f"__CODE_BLOCK_{counter}__"
            store[placeholder] = f"{lang}\n{code}"
            counter += 1
            return placeholder

        return pattern.sub(_replace, content)

    def _render_with_syntax_blocks(
        self, content: str, code_blocks: dict[str, str],
    ) -> RenderableType:
        """将内容分段渲染，代码块用 Syntax 高亮。"""
        parts: list[RenderableType] = []

        # 按占位符分割
        segments = re.split(r'(__CODE_BLOCK_\d+__)', content)

        for seg in segments:
            if seg.startswith("__CODE_BLOCK_"):
                # 代码块 — 用 Rich Syntax 高亮
                block_data = code_blocks.get(seg, "text\n")
                if "\n" in block_data:
                    lang, code = block_data.split("\n", 1)
                else:
                    lang, code = "text", block_data

                # 自动检测语言
                if not lang or lang == "text":
                    lang = self._detect_language(code)

                syntax = Syntax(
                    code.rstrip(),
                    lang,
                    theme="monokai",
                    line_numbers=False,
                    word_wrap=False,
                    background_color="default",
                )
                parts.append(Panel(
                    syntax,
                    title=f"[dim]{lang}[/dim]",
                    border_style="dim",
                    padding=(0, 1),
                ))
            elif seg.strip():
                # 普通文本 — 用 Markdown 渲染
                parts.append(Markdown(seg.strip(), code_theme="monokai"))

        if len(parts) == 1:
            return parts[0]
        return Group(*parts)

    # ═══════════════════════════════════════════════════════════
    # 数学公式渲染
    # ═══════════════════════════════════════════════════════════

    # LaTeX → Unicode 映射 (常用符号)
    _LATEX_UNICODE: dict[str, str] = {
        # 希腊字母
        r"\alpha": "α", r"\beta": "β", r"\gamma": "γ", r"\delta": "δ",
        r"\epsilon": "ε", r"\theta": "θ", r"\lambda": "λ", r"\mu": "μ",
        r"\pi": "π", r"\sigma": "σ", r"\tau": "τ", r"\phi": "φ",
        r"\omega": "ω", r"\Gamma": "Γ", r"\Delta": "Δ", r"\Theta": "Θ",
        r"\Lambda": "Λ", r"\Pi": "Π", r"\Sigma": "Σ", r"\Omega": "Ω",
        # 运算符
        r"\times": "×", r"\div": "÷", r"\pm": "±", r"\cdot": "·",
        r"\leq": "≤", r"\geq": "≥", r"\neq": "≠", r"\approx": "≈",
        r"\equiv": "≡", r"\propto": "∝", r"\infty": "∞",
        r"\sum": "Σ", r"\prod": "Π", r"\int": "∫",
        r"\sqrt": "√", r"\partial": "∂", r"\nabla": "∇",
        # 集合
        r"\in": "∈", r"\notin": "∉", r"\subset": "⊂", r"\supset": "⊃",
        r"\subseteq": "⊆", r"\cup": "∪", r"\cap": "∩",
        r"\forall": "∀", r"\exists": "∃", r"\emptyset": "∅",
        # 箭头
        r"\rightarrow": "→", r"\Rightarrow": "⇒", r"\leftarrow": "←",
        r"\Leftarrow": "⇐", r"\leftrightarrow": "↔",
        # 其他
        r"\ldots": "…", r"\cdots": "⋯",
    }

    def _render_math_inline(self, text: str) -> str:
        """处理内联公式 $...$ 和块公式 $$...$$。"""
        # ── 块公式 $$...$$ ──
        text = re.sub(
            r'\$\$\s*(.+?)\s*\$\$',
            lambda m: self._format_math_block(m.group(1)),
            text,
            flags=re.DOTALL,
        )

        # ── 内联公式 $...$ ──
        text = re.sub(
            r'\$(.+?)\$',
            lambda m: self._format_math_inline(m.group(1)),
            text,
        )

        return text

    def _format_math_block(self, formula: str) -> str:
        """格式化块公式 — 转 Unicode 并加边框标记。"""
        converted = self._convert_latex_to_unicode(formula.strip())
        return f"\n```math\n{converted}\n```\n"

    def _format_math_inline(self, formula: str) -> str:
        """格式化内联公式 — 转 Unicode。"""
        return self._convert_latex_to_unicode(formula.strip())

    @classmethod
    def _convert_latex_to_unicode(cls, formula: str) -> str:
        """将 LaTeX 公式中的符号转换为对应的 Unicode 字符。"""
        result = formula
        # 先处理下标 _{...} 和上标 ^{...}
        result = re.sub(r'_{(\w+)}', r'_\1', result)
        result = re.sub(r'\^{(\w+)}', r'^\1', result)

        for latex, unicode_char in cls._LATEX_UNICODE.items():
            result = result.replace(latex, unicode_char)

        # 处理 \frac{a}{b} → (a)/(b)
        result = re.sub(r'\\frac\{([^}]+)\}\{([^}]+)\}', r'(\1)/(\2)', result)

        return result

    # ═══════════════════════════════════════════════════════════
    # 语言检测
    # ═══════════════════════════════════════════════════════════

    _LANG_PATTERNS: dict[str, list[str]] = {
        "python": [r"^\s*(import |from |def |class |print\(|if __name__)", r"^\s*#"],
        "javascript": [r"^\s*(const |let |var |import |export |function |=>)", r"^\s*//"],
        "typescript": [r"^\s*(interface |type |enum |async )", r"^\s*//"],
        "bash": [r"^\s*(#!/bin/bash|#!/bin/sh|\b(echo|cd |ls |grep |curl |pip |npm ))", r"^\s*#"],
        "sql": [r"^\s*(SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP)\b", r"^\s*--"],
        "html": [r"^\s*(<!DOCTYPE|<html|<head|<body|<div)", r"^\s*<!--"],
        "css": [r"^\s*([.#@][\w-]+\s*\{)", r"^\s*/\*"],
        "json": [r'^\s*[\[{]', r"^\s*\""],
        "yaml": [r"^\s*[\w-]+:\s", r"^\s*#"],
        "rust": [r"^\s*(fn |let |impl |use |mod |pub )", r"^\s*//"],
        "go": [r"^\s*(package |func |import |type |var )", r"^\s*//"],
        "java": [r"^\s*(public |private |protected |class |import |package )", r"^\s*//"],
        "powershell": [r"^\s*(Get-|Set-|New-|Write-|foreach|\$\w+\s*=)", r"^\s*#"],
        "cpp": [r"^\s*(#include|int main|std::|template<)", r"^\s*//"],
    }

    @classmethod
    def _detect_language(cls, code: str) -> str:
        """基于代码内容启发式检测编程语言。"""
        first_lines = code.strip()[:500]

        scores: dict[str, int] = {}
        for lang, patterns in cls._LANG_PATTERNS.items():
            score = 0
            for pat in patterns:
                if re.search(pat, first_lines, re.MULTILINE):
                    score += 1
            if score > 0:
                scores[lang] = score

        if scores:
            return max(scores, key=lambda k: scores[k])
        return "text"


# ═══════════════════════════════════════════════════════════════
# 便捷函数
# ═══════════════════════════════════════════════════════════════

def render_agent_output(
    result: str,
    thinking_panel: Any = None,
    *,
    title: str = "Assistant",
    verbose: bool = False,
    border_style: str = "green",
) -> None:
    """便捷函数: 渲染 Agent 输出。"""
    renderer = OutputRenderer(verbose=verbose)
    renderer.render_answer(result, thinking_panel, title=title, border_style=border_style)
