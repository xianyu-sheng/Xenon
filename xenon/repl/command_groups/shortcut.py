"""Shortcut slash commands.

This group covers shortcut creation, listing, execution, and deletion.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rich.panel import Panel

from xenon.repl.command_groups.common import confirm_action, console
from xenon.repl.command_registry import command_handler, register_command

if TYPE_CHECKING:
    from xenon.repl.model_registry import ModelRegistry


# /shortcut ────────────────────────────────────────────────

register_command(
    "/shortcut",
    "管理自定义快捷指令",
    "/shortcut create|list|run|delete [参数]",
)


@command_handler("/shortcut")
def _cmd_shortcut(
    *, args: str, registry: ModelRegistry, session_state: dict[str, Any], **kwargs: Any
) -> str:
    from xenon.repl.shortcut_manager import ShortcutManager

    manager = ShortcutManager()
    parts = args.split(maxsplit=1) if args.strip() else []
    sub = parts[0].lower() if parts else "list"
    sub_args = parts[1] if len(parts) > 1 else ""

    if sub == "list":
        shortcuts = manager.list_all()
        if not shortcuts:
            return "暂无快捷指令。使用 /shortcut create 创建。"

        lines = [f"共 {len(shortcuts)} 个快捷指令:\n"]
        for s in shortcuts:
            lines.append(f"  /{s.name} — {s.description}")
            for i, step in enumerate(s.steps, 1):
                lines.append(f"    {i}. {step}")
        return "\n".join(lines)

    elif sub == "create":
        return _shortcut_create_interactive(manager, registry=registry)

    elif sub == "run":
        if not sub_args:
            return "用法: /shortcut run <name> [参数]"
        parts2 = sub_args.split(maxsplit=1)
        name = parts2[0]
        run_args = parts2[1] if len(parts2) > 1 else ""
        # P3-Q8 / §8.20.2/9：快捷指令可能含 LLM 生成的 shell 命令，运行前展示步骤并确认。
        sc = manager.get(name)
        if sc is None:
            return f"❌ 未找到快捷指令 '{name}'"
        steps_preview = "\n".join(f"  {i}. {s}" for i, s in enumerate(sc.steps, 1))
        console.print(
            Panel(steps_preview or "  (无步骤)", title=f"快捷指令 '{name}' 将执行")
        )
        if not confirm_action(
            f"运行快捷指令 '{name}'（将执行以上 {len(sc.steps)} 步命令）？",
            default=False,
        ):
            return "已取消"
        return manager.execute(name, run_args)

    elif sub == "delete":
        if not sub_args:
            return "用法: /shortcut delete <name>"
        if manager.remove(sub_args.strip()):
            return f"✅ 已删除快捷指令: {sub_args.strip()}"
        return f"❌ 快捷指令 /{sub_args.strip()} 不存在"

    else:
        return "用法: /shortcut create|list|run|delete [参数]"


def _shortcut_create_interactive(manager, registry=None) -> str:
    """交互式创建快捷指令。支持智能生成和手动配置。"""
    from rich.prompt import Prompt as _Prompt

    console.print("\n[bold cyan]创建快捷指令[/bold cyan]\n")

    name = _Prompt.ask("指令名称（不含 /）")
    description = _Prompt.ask("指令描述（一句话说明用途）")

    # 选择创建模式
    console.print("\n[dim]创建模式:[/dim]")
    console.print(
        "  [bold]1[/bold]. 🤖 智能生成 — 只需描述，Agent 自动生成命令（推荐）"
    )
    console.print("  [bold]2[/bold]. ✏️  手动配置 — 逐行输入命令")

    mode = _Prompt.ask("选择模式", choices=["1", "2"], default="1")

    if mode == "1":
        return _shortcut_auto_generate(name, description, manager, registry)
    else:
        return _shortcut_manual_create(name, description, manager)


def _shortcut_auto_generate(name: str, description: str, manager, registry=None) -> str:
    """智能生成快捷指令命令。"""
    from rich.prompt import Prompt as _Prompt

    console.print("\n[dim]🤖 正在根据你的描述生成命令...[/dim]\n")

    steps = _generate_shortcut_steps(description, registry)

    if not steps:
        console.print("[yellow]⚠️  自动生成失败，切换到手动模式。[/yellow]")
        return _shortcut_manual_create(name, description, manager)

    # 展示预览
    preview_lines = []
    for i, step in enumerate(steps, 1):
        preview_lines.append(f"  [bold]{i}.[/bold] [cyan]{step}[/cyan]")
    preview = "\n".join(preview_lines)

    console.print(
        Panel(
            preview,
            title="[bold green]✅ 自动生成的快捷指令[/bold green]",
            border_style="green",
            padding=(1, 2),
        )
    )

    console.print("\n[dim]👆 以上是 Agent 根据你的描述自动生成的命令。[/dim]\n")

    action = _Prompt.ask("操作", choices=["ok", "edit", "cancel"], default="ok")

    if action == "cancel":
        return "❌ 已取消创建。"

    if action == "edit":
        return _shortcut_manual_create(name, description, manager, pre_steps=steps)

    shortcut = manager.create(name, description, steps)
    return f"✅ 快捷指令 /{shortcut.name} 已创建！使用 /{shortcut.name} 执行。"


def _generate_shortcut_steps(description: str, registry=None) -> list[str]:
    """用 LLM 根据描述生成快捷指令命令。"""
    try:
        from xenon.utils.llm_client import chat_completion

        model_ids = registry.get_role_priority("planner") if registry else []
        if not model_ids:
            return []

        import sys

        if sys.platform == "win32":
            shell_hint = "Windows PowerShell"
            example = '["Write-Host \'hello\'", "Get-ChildItem"]'
        else:
            shell_hint = "Linux bash / macOS zsh"
            example = '["echo \'hello\'", "ls -la"]'

        prompt = f"""根据以下描述，生成一组 shell 命令（{shell_hint} 兼容）。

描述: {description}

要求:
- 返回 JSON 数组，每个元素是一条 shell 命令
- 命令要实用、安全
- 只返回 JSON 数组，不要其他内容

示例: {example}"""

        messages = [
            {
                "role": "system",
                "content": "你是一个命令生成器。根据用户描述生成 shell 命令数组。只返回 JSON 数组。",
            },
            {"role": "user", "content": prompt},
        ]

        for model_id in model_ids:
            try:
                response = chat_completion(
                    model_id, messages, max_tokens=500, temperature=0.3
                )
                return _parse_shortcut_steps(response)
            except Exception:
                continue

        return []

    except Exception:
        return []


def _parse_shortcut_steps(response: str) -> list[str]:
    """解析 LLM 返回的命令数组。"""
    import json

    text = response.strip()

    if "```json" in text:
        start = text.find("```json") + 7
        end = text.find("```", start)
        if end != -1:
            text = text[start:end].strip()
    elif "```" in text:
        start = text.find("```") + 3
        end = text.find("```", start)
        if end != -1:
            text = text[start:end].strip()

    try:
        data = json.loads(text)
        if isinstance(data, list):
            return [str(cmd) for cmd in data if cmd]
    except json.JSONDecodeError:
        pass

    return []


def _shortcut_manual_create(
    name: str, description: str, manager, pre_steps=None
) -> str:
    """手动配置快捷指令。"""
    from rich.prompt import Prompt as _Prompt

    if pre_steps:
        console.print(f"\n[dim]已有 {len(pre_steps)} 条生成的命令，继续添加。[/dim]")
        steps = list(pre_steps)
    else:
        steps_str = _Prompt.ask("执行步骤（每行一个命令，输入 END 结束）")
        steps = []
        if steps_str.strip().upper() != "END":
            steps.append(steps_str)
            while True:
                line = _Prompt.ask("下一步", default="END")
                if line.strip().upper() == "END":
                    break
                steps.append(line)

    if not steps:
        return "❌ 至少需要一个步骤"

    shortcut = manager.create(name, description, steps)
    return f"✅ 快捷指令 /{shortcut.name} 已创建！使用 /{shortcut.name} 执行。"
