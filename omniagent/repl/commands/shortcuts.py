"""
Shortcut and skill management commands.

Contains implementation functions for /shortcut and /skill commands.
Registration happens in __init__.py to avoid circular imports.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from omniagent.repl.model_registry import ModelRegistry

from rich.console import Console

console = Console()


# ═══════════════════════════════════════════════════════════════
# /shortcut — 快捷指令管理
# ═══════════════════════════════════════════════════════════════

def cmd_shortcut(*, args: str, registry: "ModelRegistry", session_state: dict[str, Any], **kwargs: Any) -> str:
    """管理自定义快捷指令。"""
    from omniagent.repl.shortcut_manager import ShortcutManager

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
    from rich.panel import Panel

    console.print("\n[bold cyan]创建快捷指令[/bold cyan]\n")

    name = _Prompt.ask("指令名称（不含 /）")
    description = _Prompt.ask("指令描述（一句话说明用途）")

    console.print("\n[dim]创建模式:[/dim]")
    console.print("  [bold]1[/bold]. 🤖 智能生成 — 只需描述，Agent 自动生成命令（推荐）")
    console.print("  [bold]2[/bold]. ✏️  手动配置 — 逐行输入命令")

    mode = _Prompt.ask("选择模式", choices=["1", "2"], default="1")

    if mode == "1":
        return _shortcut_auto_generate(name, description, manager, registry)
    else:
        return _shortcut_manual_create(name, description, manager)


def _shortcut_auto_generate(name: str, description: str, manager, registry=None) -> str:
    """智能生成快捷指令命令。"""
    from rich.panel import Panel
    from rich.prompt import Prompt as _Prompt

    console.print("\n[dim]🤖 正在根据你的描述生成命令...[/dim]\n")

    steps = _generate_shortcut_steps(description, registry)

    if not steps:
        console.print("[yellow]⚠️  自动生成失败，切换到手动模式。[/yellow]")
        return _shortcut_manual_create(name, description, manager)

    preview_lines = []
    for i, step in enumerate(steps, 1):
        preview_lines.append(f"  [bold]{i}.[/bold] [cyan]{step}[/cyan]")
    preview = "\n".join(preview_lines)

    console.print(Panel(
        preview,
        title="[bold green]✅ 自动生成的快捷指令[/bold green]",
        border_style="green",
        padding=(1, 2),
    ))

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
        from omniagent.utils.llm_client import chat_completion

        model_ids = registry.get_role_priority("planner") if registry else []
        if not model_ids:
            return []

        prompt = f"""根据以下描述，生成一组 shell 命令（Windows PowerShell 兼容）。

描述: {description}

要求:
- 返回 JSON 数组，每个元素是一条 shell 命令
- 命令要实用、安全
- 只返回 JSON 数组，不要其他内容

示例: ["echo 'hello'", "dir"]"""

        messages = [
            {"role": "system", "content": "你是一个命令生成器。根据用户描述生成 shell 命令数组。只返回 JSON 数组。"},
            {"role": "user", "content": prompt},
        ]

        for model_id in model_ids:
            try:
                response = chat_completion(model_id, messages, max_tokens=500, temperature=0.3)
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


def _shortcut_manual_create(name: str, description: str, manager, pre_steps=None) -> str:
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


# ═══════════════════════════════════════════════════════════════
# /skill — 自定义技能管理
# ═══════════════════════════════════════════════════════════════

def cmd_skill(*, args: str, registry: "ModelRegistry", session_state: dict[str, Any], **kwargs: Any) -> str:
    """管理自定义技能（支持 LLM + 工具组合）。"""
    from omniagent.repl.skill_manager import SkillManager

    manager = SkillManager()
    parts = args.split(maxsplit=1) if args.strip() else []
    sub = parts[0].lower() if parts else "list"
    sub_args = parts[1] if len(parts) > 1 else ""

    if sub == "list":
        skills = manager.list_all()
        if not skills:
            return "暂无技能。使用 /skill create 创建。"

        lines = [f"共 {len(skills)} 个技能:\n"]
        for s in skills:
            lines.append(f"  /{s.name} — {s.description}")
            lines.append(f"    步骤: {len(s.steps)} 个")
        return "\n".join(lines)

    elif sub == "create":
        return _skill_create_interactive(manager, registry=registry)

    elif sub == "run":
        if not sub_args:
            return "用法: /skill run <name> [参数]"
        parts2 = sub_args.split(maxsplit=1)
        name = parts2[0]
        run_args = parts2[1] if len(parts2) > 1 else ""
        model_ids = registry.get_role_priority("planner")
        return manager.execute(name, run_args, model_priority=model_ids)

    elif sub == "delete":
        if not sub_args:
            return "用法: /skill delete <name>"
        if manager.remove(sub_args.strip()):
            return f"✅ 已删除技能: {sub_args.strip()}"
        return f"❌ 技能 /{sub_args.strip()} 不存在"

    else:
        return "用法: /skill create|list|run|delete [参数]"


def _skill_create_interactive(manager, registry=None) -> str:
    """交互式创建技能。支持智能生成和手动配置两种模式。"""
    from rich.prompt import Prompt as _Prompt
    from rich.panel import Panel

    console.print("\n[bold cyan]创建技能[/bold cyan]\n")

    name = _Prompt.ask("技能名称（不含 /）")
    description = _Prompt.ask("技能描述（用一句话说明这个技能做什么）")

    console.print("\n[dim]创建模式:[/dim]")
    console.print("  [bold]1[/bold]. 🤖 智能生成 — 只需描述，Agent 自动生成步骤（推荐）")
    console.print("  [bold]2[/bold]. ✏️  手动配置 — 逐步骤手动添加")

    mode = _Prompt.ask("选择模式", choices=["1", "2"], default="1")

    if mode == "1":
        return _skill_auto_generate(name, description, manager, registry)
    else:
        return _skill_manual_create(name, description, manager)


def _skill_auto_generate(name: str, description: str, manager, registry=None) -> str:
    """智能生成技能步骤。"""
    from rich.panel import Panel
    from rich.prompt import Prompt as _Prompt

    console.print("\n[dim]🤖 正在根据你的描述生成技能步骤...[/dim]\n")

    steps, system_prompt = _generate_skill_steps(description, registry)

    if not steps:
        console.print("[yellow]⚠️  自动生成失败，切换到手动模式。[/yellow]")
        return _skill_manual_create(name, description, manager)

    console.print(Panel(
        _format_skill_preview(steps, system_prompt),
        title="[bold green]✅ 自动生成的技能[/bold green]",
        border_style="green",
        padding=(1, 2),
    ))

    console.print("\n[dim]👆 以上是 Agent 根据你的描述自动生成的步骤。[/dim]")
    console.print("[dim]   你可以直接使用，也可以在此基础上修改。[/dim]\n")

    action = _Prompt.ask(
        "操作",
        choices=["ok", "edit", "cancel"],
        default="ok",
    )

    if action == "cancel":
        return "❌ 已取消创建。"

    if action == "edit":
        return _skill_manual_create(name, description, manager, pre_steps=steps, pre_system_prompt=system_prompt)

    skill = manager.create(name, description, steps, system_prompt=system_prompt)
    return f"✅ 技能 /{skill.name} 已创建！使用 /skill run {skill.name} 执行。"


def _generate_skill_steps(description: str, registry=None) -> tuple[list[dict], str]:
    """用 LLM 根据描述生成技能步骤。"""
    try:
        from omniagent.utils.llm_client import chat_completion

        model_ids = registry.get_role_priority("planner") if registry else []
        if not model_ids:
            return _fallback_skill_steps(description), ""

        prompt = f"""根据以下技能描述，生成对应的执行步骤。

技能描述: {description}

请返回 JSON 格式，包含两个字段:
1. "system_prompt": 系统提示词（字符串，可为空字符串）
2. "steps": 步骤数组，每个步骤是一个对象，包含:
   - "type": "llm" | "command" | "echo" | "write_file" | "read_file"
   - 对于 llm: "prompt" (提示词，可用 {{变量名}} 引用输入)
   - 对于 command: "action" (shell 命令)
   - 对于 echo: "prompt" (输出内容)
   - 对于 write_file: "file_path", "content"
   - 对于 read_file: "file_path"
   - 可选 "output_var": 输出变量名（用于步骤间传递数据）

注意:
- 如果用户输入是 {{input}}，在需要用户输入的地方使用 {{input}}
- 步骤要实用、可执行
- 只返回 JSON，不要其他内容

示例:
{{"system_prompt": "你是一个代码审查专家", "steps": [{{"type": "llm", "prompt": "请审查以下代码:\\n{{input}}", "output_var": "review"}}]}}"""

        messages = [
            {"role": "system", "content": "你是一个技能配置生成器。根据用户描述生成可执行的技能步骤配置。只返回 JSON。"},
            {"role": "user", "content": prompt},
        ]

        for model_id in model_ids:
            try:
                response = chat_completion(model_id, messages, max_tokens=1000, temperature=0.3)
                return _parse_skill_steps(response)
            except Exception:
                continue

        return _fallback_skill_steps(description), ""

    except Exception:
        return _fallback_skill_steps(description), ""


def _parse_skill_steps(response: str) -> tuple[list[dict], str]:
    """解析 LLM 返回的技能步骤 JSON。"""
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
    except json.JSONDecodeError:
        brace_start = text.find("{")
        brace_end = text.rfind("}")
        if brace_start != -1 and brace_end != -1:
            try:
                data = json.loads(text[brace_start:brace_end + 1])
            except json.JSONDecodeError:
                return [], ""
        else:
            return [], ""

    steps = data.get("steps", [])
    system_prompt = data.get("system_prompt", "")

    valid_steps = []
    for step in steps:
        if not isinstance(step, dict) or "type" not in step:
            continue
        if step["type"] not in ("llm", "command", "echo", "write_file", "read_file"):
            continue
        valid_steps.append(step)

    return valid_steps, system_prompt


def _fallback_skill_steps(description: str) -> list[dict]:
    """LLM 不可用时的默认步骤。"""
    return [
        {"type": "llm", "prompt": f"根据以下需求执行操作:\n{{input}}\n\n需求: {description}", "output_var": "result"},
    ]


def _format_skill_preview(steps: list[dict], system_prompt: str) -> str:
    """格式化技能预览。"""
    lines = []
    if system_prompt:
        lines.append(f"[bold]系统提示词:[/bold] {system_prompt}\n")

    for i, step in enumerate(steps, 1):
        stype = step.get("type", "?")
        icons = {"llm": "🧠", "command": "⚡", "echo": "📢", "write_file": "📝", "read_file": "📖"}
        icon = icons.get(stype, "❓")

        if stype == "llm":
            prompt_preview = step.get("prompt", "")[:80]
            lines.append(f"  {icon} 步骤 {i} [cyan]LLM[/cyan]: {prompt_preview}")
        elif stype == "command":
            lines.append(f"  {icon} 步骤 {i} [yellow]命令[/yellow]: {step.get('action', '')}")
        elif stype == "echo":
            lines.append(f"  {icon} 步骤 {i} [green]输出[/green]: {step.get('prompt', '')[:60]}")
        elif stype == "write_file":
            lines.append(f"  {icon} 步骤 {i} [magenta]写文件[/magenta]: {step.get('file_path', '')}")
        elif stype == "read_file":
            lines.append(f"  {icon} 步骤 {i} [blue]读文件[/blue]: {step.get('file_path', '')}")

        if step.get("output_var"):
            lines.append(f"       → 输出到: [dim]{step['output_var']}[/dim]")

    return "\n".join(lines)


def _skill_manual_create(name: str, description: str, manager, pre_steps=None, pre_system_prompt="") -> str:
    """手动配置技能步骤。"""
    from rich.prompt import Prompt as _Prompt

    system_prompt = _Prompt.ask("系统提示词（可选）", default=pre_system_prompt or "")

    if pre_steps:
        console.print(f"\n[dim]已有 {len(pre_steps)} 个生成的步骤，继续添加更多步骤。[/dim]")
        steps = list(pre_steps)
    else:
        console.print("\n添加步骤（支持类型: llm, command, echo, write_file, read_file）")
        steps = []

    while True:
        console.print(f"\n[dim]步骤 {len(steps) + 1}[/dim]")
        step_type = _Prompt.ask("  类型", choices=["llm", "command", "echo", "write_file", "read_file", "done"])
        if step_type == "done":
            break

        step: dict[str, str] = {"type": step_type}

        if step_type == "llm":
            step["prompt"] = _Prompt.ask("  提示词（可用 {变量名}）")
        elif step_type == "command":
            step["action"] = _Prompt.ask("  命令")
        elif step_type == "echo":
            step["prompt"] = _Prompt.ask("  输出内容")
        elif step_type == "write_file":
            step["file_path"] = _Prompt.ask("  文件路径")
            step["content"] = _Prompt.ask("  文件内容")
        elif step_type == "read_file":
            step["file_path"] = _Prompt.ask("  文件路径")

        output_var = _Prompt.ask("  输出变量名（可选）", default="")
        if output_var:
            step["output_var"] = output_var

        steps.append(step)

    if not steps:
        return "❌ 至少需要一个步骤"

    skill = manager.create(name, description, steps, system_prompt=system_prompt)
    return f"✅ 技能 /{skill.name} 已创建！使用 /skill run {skill.name} 执行。"
