"""
Code Editor — 代码文件编辑器。

提供读取文件、精确文本替换、差异生成功能，
支持 LLM 生成修改 + 用户确认后应用的工作流。
"""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any

from xenon.utils.atomic_write import atomic_write_text as _atomic_write_text


class CodeEditor:
    """代码文件编辑器。"""

    @staticmethod
    def read_file(path: str | Path) -> tuple[str, int]:
        """
        读取文件内容，返回 (带行号的内容, 总行数)。

        Args:
            path: 文件路径。

        Returns:
            (带行号的文本, 行数)

        Raises:
            FileNotFoundError: 文件不存在。
        """
        p = Path(path).resolve()
        if not p.exists():
            raise FileNotFoundError(f"文件不存在: {p}")

        content = p.read_text(encoding="utf-8")
        lines = content.splitlines()
        numbered = "\n".join(f"{i+1:4d} | {line}" for i, line in enumerate(lines))
        return numbered, len(lines)

    @staticmethod
    def read_raw(path: str | Path) -> str:
        """读取原始文件内容（不带行号）。"""
        return Path(path).resolve().read_text(encoding="utf-8")

    @staticmethod
    def apply_edit(
        path: str | Path,
        old_text: str,
        new_text: str,
        *,
        confirm: bool = True,
    ) -> str:
        """
        精确文本替换。

        在文件中查找 old_text 并替换为 new_text。
        类似 Claude Code 的 Edit 工具。

        Args:
            path: 文件路径。
            old_text: 要替换的原始文本。
            new_text: 替换后的文本。
            confirm: 是否需要用户确认（交互模式）。

        Returns:
            操作结果描述。
        """
        p = Path(path).resolve()
        if not p.exists():
            return f"❌ 文件不存在: {p}"

        content = p.read_text(encoding="utf-8")

        # 检查 old_text 是否存在
        count = content.count(old_text)
        if count == 0:
            return f"❌ 在 {p.name} 中未找到匹配的文本。请检查原文是否准确。"
        if count > 1:
            return f"⚠️ 在 {p.name} 中找到 {count} 处匹配。请提供更多上下文使匹配唯一。"

        # 生成差异
        old_lines = content.splitlines(keepends=True)
        new_content = content.replace(old_text, new_text, 1)
        new_lines = new_content.splitlines(keepends=True)

        diff = list(difflib.unified_diff(
            old_lines, new_lines,
            fromfile=f"a/{p.name}",
            tofile=f"b/{p.name}",
            n=3,
        ))

        if confirm and diff:
            from rich.console import Console
            from rich.syntax import Syntax
            from rich.prompt import Confirm as RichConfirm

            console = Console()
            diff_text = "".join(diff)
            console.print(f"\n[bold]📝 修改 {p.name}:[/bold]")
            console.print(Syntax(diff_text, "diff", theme="monokai"))
            console.print()

            if not RichConfirm.ask("应用修改？", default=True):
                return "❌ 已取消修改。"

        # 应用修改（A9: 原子写入，防写入中途崩溃损坏文件）
        _atomic_write_text(p, new_content)
        return f"✅ 已修改 {p.name}（替换 1 处）"

    @staticmethod
    def generate_diff(old_text: str, new_text: str, filename: str = "file") -> str:
        """生成 unified diff。"""
        old_lines = old_text.splitlines(keepends=True)
        new_lines = new_text.splitlines(keepends=True)
        diff = difflib.unified_diff(
            old_lines, new_lines,
            fromfile=f"a/{filename}",
            tofile=f"b/{filename}",
            n=3,
        )
        return "".join(diff)

    @staticmethod
    def edit_with_llm(
        path: str | Path,
        instruction: str,
        model_priority: list[str],
        *,
        confirm: bool = True,
    ) -> str:
        """
        LLM 辅助编辑文件。

        流程: 读取文件 → LLM 生成修改 → 展示 diff → 用户确认 → 应用。

        Args:
            path: 文件路径。
            instruction: 修改指令。
            model_priority: 模型优先级列表。
            confirm: 是否需要确认。

        Returns:
            操作结果描述。
        """
        from rich.console import Console
        from rich.prompt import Confirm as RichConfirm

        console = Console()

        p = Path(path).resolve()
        if not p.exists():
            return f"❌ 文件不存在: {p}"

        content = p.read_text(encoding="utf-8")
        ext = p.suffix.lstrip(".")

        # 构建 LLM 请求
        prompt = f"""请根据以下指令修改代码文件。

文件: {p.name}
语言: {ext or 'unknown'}

修改指令: {instruction}

当前文件内容:
```{ext}
{content}
```

请返回修改后的完整文件内容。只返回代码，不要解释。
用 ```{ext} 包裹代码。"""

        try:
            from xenon.utils.llm_client import chat_completion

            console.print(f"\n[dim]🤖 正在根据指令生成修改...[/dim]")

            response = None
            for model_id in model_priority:
                try:
                    response = chat_completion(model_id, [
                        {"role": "system", "content": "你是一个代码编辑专家。根据指令修改代码，返回修改后的完整文件。只返回代码。"},
                        {"role": "user", "content": prompt},
                    ], max_tokens=8000, temperature=0.2)
                    break
                except Exception:
                    continue

            if not response:
                return "❌ LLM 调用失败，无法生成修改。"

            # 提取代码
            new_content = CodeEditor._extract_code(response, ext)
            if not new_content:
                return "❌ 无法从 LLM 输出中提取代码。"

            # A6: 截断防护 — LLM 返回内容显著短于原文则拒绝写入（防 max_tokens 截断覆盖原文件）
            orig_lines = len(content.splitlines())
            new_lines = len(new_content.splitlines())
            if not new_content.strip():
                return "❌ LLM 返回空内容，已拒绝写入。"
            if orig_lines >= 20 and new_lines < orig_lines * 0.5:
                return (f"❌ LLM 返回内容（{new_lines} 行）显著短于原文（{orig_lines} 行），"
                        f"疑似被 max_tokens 截断，已拒绝写入以防数据丢失。"
                        f"请改用 apply_edit（old_text/new_text）只传变更部分。")

            # 生成 diff
            diff = CodeEditor.generate_diff(content, new_content, p.name)

            if not diff.strip():
                return "ℹ️ 文件没有变化。"

            # 展示 diff
            from rich.syntax import Syntax
            console.print(f"\n[bold]📝 修改 {p.name}:[/bold]")
            console.print(Syntax(diff, "diff", theme="monokai"))
            console.print()

            if confirm:
                action = RichConfirm.ask("应用修改？", default=True)
                if not action:
                    return "❌ 已取消修改。"

            # 应用（A6+A9: .bak 备份 + 原子写入）
            _atomic_write_text(p, new_content, backup=True)
            return f"✅ 已修改 {p.name}"

        except Exception as e:
            return f"❌ 编辑失败: {e}"

    @staticmethod
    def _extract_code(response: str, ext: str) -> str | None:
        """从 LLM 输出中提取代码块。"""
        text = response.strip()

        # 尝试提取 ```ext ... ``` 代码块
        for marker in [f"```{ext}", "```"]:
            start = text.find(marker)
            if start != -1:
                code_start = text.find("\n", start) + 1
                end = text.find("```", code_start)
                if end != -1:
                    return text[code_start:end].strip()

        # 没有代码块，直接使用（看起来像代码就返回）
        lines = text.splitlines()
        if len(lines) >= 1 and not text.startswith("{") and not text.startswith("["):
            return text

        return None
