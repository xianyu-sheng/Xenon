"""
Setup Wizard — 交互式配置引导。

提供菜单式的 API Key 配置、模型选择、范式选择，
用户不需要记任何命令，全程跟着菜单走。
"""

from __future__ import annotations

import re
import sys
from typing import TYPE_CHECKING

from rich.console import Console
from rich.prompt import Prompt, IntPrompt, Confirm
from rich.table import Table

from omniagent.repl.provider_registry import (
    PROVIDERS,
    list_providers,
    load_credentials,
    set_provider_key,
    remove_provider_key,
    get_configured_providers,
    fetch_provider_models,
    MODEL_FETCH_ERRORS,
)

if TYPE_CHECKING:
    from omniagent.repl.model_registry import ModelRegistry

console = Console()


def _masked_input(prompt_text: str) -> str:
    """逐字符读取输入，实时显示 * 号掩码。回车确认，退格删除。
    粘贴时只取第一行（避免粘贴多行导致重复）。
    Linux/macOS 使用 getpass 实现安全的密码输入。"""
    if sys.platform != "win32":
        import getpass
        try:
            return getpass.getpass(f"{prompt_text}: ")
        except (KeyboardInterrupt, EOFError):
            return ""

    import msvcrt
    sys.stdout.write(f"{prompt_text}: ")
    sys.stdout.flush()
    chars: list[str] = []
    while True:
        ch = msvcrt.getwch()
        if ch in ('\r', '\n'):
            # 粘贴多行时，\r\n 中的 \n 会作为第二次回车 → 直接结束
            sys.stdout.write('\n')
            sys.stdout.flush()
            break
        elif ch == '\x03':  # Ctrl+C
            raise KeyboardInterrupt
        elif ch in ('\x08', '\x7f'):  # Backspace
            if chars:
                chars.pop()
                sys.stdout.write('\b \b')
                sys.stdout.flush()
        elif ch == '\x1b':  # Escape — 清空
            for _ in chars:
                sys.stdout.write('\b \b')
            chars.clear()
            sys.stdout.flush()
        else:
            chars.append(ch)
            sys.stdout.write('*')
            sys.stdout.flush()
    return ''.join(chars)


def _clean_api_key(raw: str) -> str:
    """清理粘贴 API Key 时常见的空白、引号和多行内容。

    P3-Q6 / §8.16.3：识别并剥离 ``export VAR=value`` / ``VAR=value`` 前缀——
    用户常从 ``export OPENAI_API_KEY="sk-xxx"`` 粘贴，原实现会把整行当 key 存入。
    """
    if not raw or not raw.strip():
        return ""
    first_line = raw.strip().splitlines()[0].strip()
    # 剥离 (export )?VAR= 前缀
    first_line = re.sub(r"^(?:export\s+)?[A-Za-z_][A-Za-z0-9_]*\s*=\s*", "", first_line)
    return first_line.strip().strip("'\"").strip()


def _test_key_connectivity(provider, api_key: str) -> tuple[bool, str]:
    """保存前连通性测试（P3-Q6 / §8.16.1）。

    用该 key 调一次厂商 ``/models`` 端点：成功返回 ``(True, 模型数描述)``，
    失败返回 ``(False, 错误详情)``。失败原因来自 ``MODEL_FETCH_ERRORS``。
    """
    models = fetch_provider_models(provider, api_key)
    if models:
        return True, f"获取到 {len(models)} 个模型"
    err = MODEL_FETCH_ERRORS.get(provider.key, "未知错误")
    return False, err


def _purge_provider_models(registry: "ModelRegistry", provider_key: str) -> int:
    """删 key 时联动清理 registry 中该 provider 的模型（P3-Q6 / §8.16.4）。

    移除 model_id 形如 ``provider_key/...`` 的全部模型（``remove_model`` 已联动
    清 role_priority 中的该别名），并删除清空后的空角色条目，返回移除模型数。
    """
    removed = 0
    for alias, mc in list(registry.models.items()):
        prefix = mc.model_id.split("/", 1)[0]
        if prefix == provider_key:
            registry.remove_model(alias)
            removed += 1
    # 清理空角色列表（重置优先级）
    for role in list(registry.role_priority):
        if not registry.role_priority[role]:
            del registry.role_priority[role]
    return removed


def interactive_setup(registry: ModelRegistry) -> None:
    """
    交互式配置引导。

    菜单式操作，用户选择数字即可。
    """
    console.print("\n[bold cyan]═══ OmniAgent-CLI 配置向导 ═══[/bold cyan]\n")

    while True:
        console.print("[bold]请选择操作:[/bold]")
        console.print("  [cyan]1[/cyan]. 配置 API Key")
        console.print("  [cyan]2[/cyan]. 查看已配置的厂商")
        console.print("  [cyan]3[/cyan]. 选择/切换模型")
        console.print("  [cyan]4[/cyan]. 选择思考范式")
        console.print("  [cyan]5[/cyan]. 删除 API Key")
        console.print("  [cyan]0[/cyan]. 退出配置\n")

        choice = Prompt.ask("请输入数字", choices=["0", "1", "2", "3", "4", "5"], default="0")

        if choice == "0":
            console.print("[dim]配置完成[/dim]\n")
            break
        elif choice == "1":
            _setup_api_key()
        elif choice == "2":
            _show_configured()
        elif choice == "3":
            _select_model(registry)
        elif choice == "4":
            _select_mode(registry)
        elif choice == "5":
            _remove_api_key(registry)


def _setup_api_key() -> None:
    """配置 API Key — 展示厂商列表，用户选择并输入 Key。"""
    providers = list_providers()

    console.print("\n[bold]选择要配置的厂商:[/bold]\n")
    table = Table(show_header=True, header_style="bold")
    table.add_column("#", style="cyan", width=3)
    table.add_column("厂商", style="bold")
    table.add_column("模型示例")
    table.add_column("状态")

    creds = load_credentials()
    for i, p in enumerate(providers, 1):
        status = "[green]已配置[/green]" if p.key in creds and creds[p.key] else "[dim]未配置[/dim]"
        models_str = ", ".join(p.models[:3]) + ("..." if len(p.models) > 3 else "")
        table.add_row(str(i), p.name, models_str, status)

    console.print(table)
    console.print()

    idx = IntPrompt.ask(
        "输入厂商编号",
        choices=[str(i) for i in range(1, len(providers) + 1)],
    )
    provider = providers[idx - 1]

    console.print(f"\n[bold]{provider.name}[/bold]")
    console.print(f"  API 地址: [dim]{provider.base_url}[/dim]")
    console.print(f"  环境变量: [dim]{provider.env_key}[/dim]")
    console.print(f"  可用模型: [dim]{', '.join(provider.models)}[/dim]\n")

    api_key = _clean_api_key(_masked_input(f"请输入 {provider.name} 的 API Key（可粘贴，输入会显示，回车确认）"))

    if api_key:
        # P3-Q6 / §8.16.1：保存前连通性测试，失败时询问是否仍保存。
        ok, detail = _test_key_connectivity(provider, api_key)
        if ok:
            console.print(f"[green]✓ 连通性正常（{detail}）[/green]")
            set_provider_key(provider.key, api_key)
            console.print(f"[green]已保存 {provider.name} 的 API Key[/green]\n")
        else:
            console.print(f"[yellow]⚠ 连通性测试失败: {detail}[/yellow]")
            if Confirm.ask("仍要保存该 Key？", default=False):
                set_provider_key(provider.key, api_key)
                console.print(f"[green]已保存 {provider.name} 的 API Key[/green]\n")
            else:
                console.print("[yellow]已取消保存[/yellow]\n")
    else:
        console.print("\n[yellow]已取消[/yellow]\n")


def _show_configured() -> None:
    """显示已配置的厂商。"""
    configured = get_configured_providers()

    if not configured:
        console.print("\n[yellow]尚未配置任何 API Key[/yellow]")
        console.print("  使用菜单 [cyan]1[/cyan] 配置\n")
        return

    console.print("\n[bold]已配置的厂商:[/bold]\n")
    table = Table(show_header=True, header_style="bold")
    table.add_column("厂商", style="bold")
    table.add_column("API Key", style="dim")
    table.add_column("可用模型")

    for p in configured:
        key_display = p.api_key[:8] + "****" + p.api_key[-4:] if len(p.api_key) > 12 else "****"
        models = ", ".join(p.models[:4]) if p.models else f"获取失败: {p.model_error or '未知错误'}"
        table.add_row(p.name, key_display, models)

    console.print(table)
    console.print()


def _select_model(registry: ModelRegistry) -> None:
    """交互式选择模型 — 展示所有可用模型，用户选择。"""
    configured = get_configured_providers()

    if not configured:
        console.print("\n[yellow]请先配置 API Key（菜单 1）[/yellow]\n")
        return

    console.print("\n[bold]选择要使用的模型:[/bold]\n")

    # 展示所有可用模型
    all_models = []
    table = Table(show_header=True, header_style="bold")
    table.add_column("#", style="cyan", width=4)
    table.add_column("厂商", style="bold")
    table.add_column("模型")
    table.add_column("特点")

    idx = 1
    for p in configured:
        if not p.models:
            table.add_row("-", p.name, "实时获取失败", p.model_error or "请检查 API Key / 网络 / base_url")
            continue
        for m in p.models:
            model_id = f"{p.key}/{m}"
            hint = _model_hint(m)
            table.add_row(str(idx), p.name, m, hint)
            all_models.append((model_id, m, p.key))
            idx += 1

    console.print(table)
    console.print()

    if not all_models:
        console.print("[yellow]未能实时获取任何模型，请检查上方错误原因[/yellow]\n")
        return

    choice = IntPrompt.ask(
        "输入模型编号（可选择多个，用空格分隔，如 1 3 5）",
        default="1",
    )

    # 解析多选
    selections = [int(x) for x in str(choice).split() if x.isdigit()]
    selected_models = []
    for s in selections:
        if 1 <= s <= len(all_models):
            model_id, short_name, provider = all_models[s - 1]
            alias = short_name.replace(".", "-")
            registry.add_model(model_id, alias)
            selected_models.append(f"{alias} ({model_id})")

    if selected_models:
        # 自动设置 planner 角色
        aliases = [m.split(" (")[0] for m in selected_models]
        registry.assign_role("planner", aliases)
        console.print(f"\n[green]已选择模型:[/green]")
        for m in selected_models:
            console.print(f"  -> {m}")
        console.print(f"[dim]planner 角色优先级: {' -> '.join(aliases)}[/dim]\n")
    else:
        console.print("\n[yellow]未选择任何模型[/yellow]\n")


def _select_mode(registry: ModelRegistry) -> None:
    """交互式选择思考范式。"""
    modes = registry.modes
    current = registry.current_mode

    console.print("\n[bold]选择思考范式:[/bold]\n")

    table = Table(show_header=True, header_style="bold")
    table.add_column("#", style="cyan", width=3)
    table.add_column("范式", style="bold")
    table.add_column("说明")
    table.add_column("推荐场景")
    table.add_column("状态")

    mode_list = list(modes.values())
    for i, mode in enumerate(mode_list, 1):
        status = "[green]当前[/green]" if mode.name == current else ""
        scene = _mode_scene(mode.name)
        table.add_row(str(i), mode.name, mode.description, scene, status)

    console.print(table)
    console.print()

    choice = IntPrompt.ask(
        "输入范式编号",
        choices=[str(i) for i in range(1, len(mode_list) + 1)],
    )

    selected = mode_list[choice - 1]
    registry.set_mode(selected.name)
    console.print(f"\n[green]已切换到: {selected.name}[/green] — {selected.description}\n")


def _remove_api_key(registry: "ModelRegistry") -> None:
    """删除 API Key。

    P3-Q6 / §8.16.4：删 key 时联动清理 registry 中该 provider 的模型并重置
    角色优先级，避免删 key 后 registry 仍指向该模型 → 运行时 401。
    """
    configured = get_configured_providers()

    if not configured:
        console.print("\n[yellow]没有可删除的配置[/yellow]\n")
        return

    console.print("\n[bold]已配置的厂商:[/bold]\n")
    for i, p in enumerate(configured, 1):
        console.print(f"  [cyan]{i}[/cyan]. {p.name}")

    console.print()
    choice = IntPrompt.ask(
        "输入要删除的编号（0 取消）",
        choices=["0"] + [str(i) for i in range(1, len(configured) + 1)],
    )

    if choice == "0":
        return

    provider = configured[int(choice) - 1]
    if Confirm.ask(f"确认删除 {provider.name} 的 API Key?"):
        remove_provider_key(provider.key)
        removed = _purge_provider_models(registry, provider.key)
        console.print(f"[green]已删除 {provider.name} 的 API Key[/green]")
        if removed:
            console.print(f"[dim]已联动移除 {removed} 个 {provider.name} 模型并重置角色优先级[/dim]")
        console.print()


def _model_hint(model_name: str) -> str:
    """返回模型的特点提示。"""
    hints = {
        "gpt-4o": "旗舰，全能",
        "gpt-4o-mini": "便宜，快速",
        "gpt-4-turbo": "上代旗舰",
        "gpt-3.5-turbo": "最便宜",
        "o1-preview": "推理增强",
        "o1-mini": "推理，便宜",
        "claude-sonnet-4-20250514": "最新旗舰",
        "claude-3-5-sonnet-20241022": "旗舰，编程强",
        "claude-3-5-haiku-20241022": "快速，便宜",
        "claude-3-opus-20240229": "最强推理",
        "deepseek-chat": "通用对话",
        "deepseek-coder": "编程专用",
        "deepseek-reasoner": "深度推理",
        "gemini-2.0-flash": "最新，快速",
        "gemini-1.5-pro": "长上下文",
        "glm-4-plus": "旗舰",
        "glm-4-flash": "快速，免费",
        "qwen-max": "旗舰",
        "qwen-plus": "性价比高",
        "qwen-turbo": "快速，便宜",
        "moonshot-v1-128k": "128K 上下文",
    }
    return hints.get(model_name, "")


def _mode_scene(mode_name: str) -> str:
    """返回范式的推荐场景。"""
    scenes = {
        "plan-execute": "复杂任务、多步骤编程",
        "react": "探索性任务、试错调试",
        "reflection": "高质量输出、代码审查",
        "plan-react": "全局规划+灵活执行",
        "plan-reflection": "规划执行+质量保证",
        "react-reflection": "探索执行+审查修正",
    }
    return scenes.get(mode_name, "")
