"""Presentation helpers — rendering and diff previews (R1: extracted from repl.py).

Pure functions: no REPL state, no side effects beyond ``console`` output.
"""

from __future__ import annotations

import difflib
import os
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.text import Text

# 延迟导入 console：与 repl.py 共用同一实例（测试 monkeypatch 依赖同一对象）。
def _console() -> Console:
    from xenon.repl.repl import console

    return console

__all__ = [
    "diff_preview",
    "render_assistant_text",
    "render_secondary_text",
    "render_verification_failure",
    "unwrap_json_result",
]


def diff_preview(tool_name: str, params: dict) -> str:
    """审批面板里的 diff 预览（写入/编辑/批量），最多 30 行。"""

    try:
        if tool_name in {"write_file"}:
            path = params.get("file_path")
            content = params.get("content")
            if not path or content is None:
                return ""
            old_lines: list[str] = []
            if os.path.exists(str(path)):
                try:
                    old_lines = Path(str(path)).read_text(
                        encoding="utf-8", errors="replace"
                    ).splitlines()
                except OSError:
                    old_lines = []
            return "\n".join(
                list(
                    difflib.unified_diff(
                        old_lines,
                        str(content).splitlines(),
                        fromfile=str(path),
                        tofile=f"{path}（拟写入）",
                        lineterm="",
                    )
                )[:30]
            )
        if tool_name == "edit_file":
            path = params.get("file_path")
            if not path:
                return ""
            return "\n".join(
                list(
                    difflib.unified_diff(
                        str(params.get("old_text") or "").splitlines(),
                        str(params.get("new_text") or "").splitlines(),
                        fromfile=str(path),
                        tofile=f"{path}（修改后）",
                        lineterm="",
                    )
                )[:30]
            )
        if tool_name in {"batch_write", "batch_edit"}:
            items = params.get("files") or params.get("edits") or []
            lines = [
                f"  - {item.get('file_path') or item.get('path')}"
                for item in items[:10]
                if isinstance(item, dict)
            ]
            return "涉及文件:\n" + "\n".join(lines)
    except Exception:  # noqa: BLE001 — 预览失败只影响展示
        return ""
    return ""


def render_verification_failure(draft: str, reasons: list[str]) -> None:
    """未通过发布门的答案：醒目标记 + 草稿内容，不渲染为成功结果。"""

    console = _console()
    console.print()
    header = Text()
    header.append("● ", style="bold red")
    header.append("任务校验未通过", style="bold red")
    console.print(header)
    console.print(
        Panel(
            "\n".join(f"- {r}" for r in reasons)
            + "\n\n以下回答未通过校验，按未完成处理（草稿）：\n\n"
            + str(draft or ""),
            border_style="red",
            padding=(0, 1),
        )
    )


def render_assistant_text(
    content: str, *, title: str = "Assistant", model_id: str | None = None
) -> None:
    """无边框渲染模型正文，让内容成为视觉焦点。"""

    console = _console()
    console.print()
    header = Text()
    header.append("● ", style="bold #67e8f9")
    header.append(title, style="bold")
    if model_id:
        header.append(f"  {model_id}", style="dim")
    console.print(header)
    console.print(Padding(Markdown(content), (0, 0, 0, 2)))


def render_secondary_text(title: str, content: str) -> None:
    """无边框渲染提示词等辅助信息，并整体降低视觉权重。"""

    console = _console()
    console.print(Text(f"  {title}", style="dim"))
    console.print(Padding(Text(content, style="dim"), (0, 0, 0, 4)))


def unwrap_json_result(result: str) -> str:
    """安全网：如果 result 是裸 JSON 文本，提取 final_answer。

    当 parse_react 因内嵌 JSON/特殊字符解析失败时，引擎可能返回
    原始 JSON 字符串而非提取后的 final_answer。此方法做最终兜底。
    """

    if not result or not result.strip():
        return result
    text = result.strip()
    # 检测是否为 JSON 对象或数组
    if not (text.startswith("{") or text.startswith("[")):
        return result
    if '"final_answer"' not in text and '"answer"' not in text:
        return result
    try:
        import json as _json

        data = _json.loads(text)
        # 单对象
        if isinstance(data, dict):
            fa = data.get("final_answer") or data.get("answer") or data.get("result")
            if fa and isinstance(fa, str) and len(fa) > 20:
                return fa
        # 数组：取首个含 final_answer 的对象
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    fa = (
                        item.get("final_answer")
                        or item.get("answer")
                        or item.get("result")
                    )
                    if fa and isinstance(fa, str) and len(fa) > 20:
                        return fa
    except Exception:  # noqa: BLE001
        # JSON 解析失败，尝试正则提取
        import re

        for key in ("final_answer", "answer"):
            m = re.search(rf'"{key}"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
            if m:
                val = m.group(1)
                # 还原转义
                val = (
                    val.replace("\\n", "\n")
                    .replace("\\t", "\t")
                    .replace('\\"', '"')
                )
                if len(val) > 20:
                    return val
    return result
