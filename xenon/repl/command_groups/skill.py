"""Skill-related slash commands.

The group owns skill discovery, creation, execution, and import workflows.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any

from xenon.repl.command_groups.common import console
from xenon.repl.command_registry import (
    _HANDLERS,
    command_handler,
    register_command,
)
from xenon.utils.github_auth import github_auth_headers

if TYPE_CHECKING:
    from xenon.repl.model_registry import ModelRegistry

# /skill ───────────────────────────────────────────────────

register_command(
    "/skill",
    "管理自定义技能（支持 LLM + 工具组合）",
    "/skill create|list|run|delete|import [参数]",
)

# v0.5.4: 模糊子命令匹配 —— "creat"/"lst"/"del" 等 typo 自动纠正
_SKILL_FUZZY: dict[str, str] = {}
_FUZZY_ALIASES = {
    "create": ["creat", "crate", "creaet", "add", "new", "mk"],
    "list": ["ls", "lst", "show", "all"],
    "delete": ["del", "rm", "remove", "delet"],
    "run": ["exec", "execute", "start"],
    "import": ["install", "get", "fetch", "clone", "load"],
    "reload": ["refresh", "rescan"],
    "doctor": ["check", "diagnose"],
}
for _canonical, _aliases in _FUZZY_ALIASES.items():
    for _a in _aliases:
        _SKILL_FUZZY[_a] = _canonical


def _fuzzy_match_subcommand(sub: str) -> str | None:
    """模糊匹配子命令名，返回规范名或 None。"""
    import difflib

    canonical = list(_FUZZY_ALIASES.keys())
    # 精确别名匹配
    if sub in _SKILL_FUZZY:
        return _SKILL_FUZZY[sub]
    # difflib 模糊匹配（截断到 1 的阈值）
    matches = difflib.get_close_matches(sub, canonical, n=1, cutoff=0.6)
    return matches[0] if matches else None


@command_handler("/skill")
def _cmd_skill(
    *, args: str, registry: ModelRegistry, session_state: dict[str, Any], **kwargs: Any
) -> str:
    from xenon.repl.skill_manager import SkillManager

    manager = SkillManager()
    parts = args.split(maxsplit=1) if args.strip() else []
    sub = parts[0].lower() if parts else "list"
    sub_args = parts[1] if len(parts) > 1 else ""

    # v0.5.4: 模糊匹配纠正 typo
    canonical = sub
    if sub not in {"list", "create", "run", "delete", "import", "reload", "doctor"}:
        matched = _fuzzy_match_subcommand(sub)
        if matched:
            canonical = matched

    if canonical == "list":
        skills = manager.list_all()

        # 已安装的技能
        installed = ""
        if skills:
            lines = [f"═══ 已安装技能（{len(skills)} 个）═══\n"]
            for s in skills:
                if s.is_agent_skill:
                    version = f" v{s.version}" if s.version else ""
                    lines.append(f"  /{s.name} — {s.description}")
                    lines.append(f"    Agent Skill{version} · {s.source} · 按需加载")
                    continue
                type_counts: dict[str, int] = {}
                for st in s.steps:
                    type_counts[st.type] = type_counts.get(st.type, 0) + 1
                step_summary = ", ".join(
                    f"{n}×{t}" for t, n in sorted(type_counts.items())
                )
                lines.append(f"  /{s.name} — {s.description}")
                lines.append(f"    {len(s.steps)} 步 ({step_summary})")
            installed = "\n".join(lines) + "\n"
        else:
            installed = "暂无已安装技能。\n"
        if manager.load_errors:
            installed += (
                f"⚠️  {len(manager.load_errors)} 个技能加载失败；"
                "运行 /skill doctor 查看详情。\n"
            )

        # 库浏览指引
        library_guide = """\
📡 浏览云端 Skill 库：
  /skill-discover              浏览全部
  /skill-discover <关键词>      搜索

📥 安装 Skill：
  /skill-install <名称>         一键安装
  /skill import <GitHub URL>   从 URL 导入

🛠 其他：
  /skill create                交互式创建
  /skill delete <名称>          删除
  /skill reload                从磁盘重新加载
"""
        return installed + library_guide

    elif canonical == "create":
        return _skill_create_interactive(manager, registry=registry)

    elif canonical == "run":
        if not sub_args:
            return "用法: /skill run <name> [参数]"
        parts2 = sub_args.split(maxsplit=1)
        name = parts2[0]
        run_args = parts2[1] if len(parts2) > 1 else ""
        return _execute_installed_skill(
            manager,
            name,
            run_args,
            registry=registry,
            session_state=session_state,
        )

    elif canonical == "delete":
        if not sub_args:
            return "用法: /skill delete <name>"
        if manager.remove(sub_args.strip()):
            return f"✅ 已删除技能: {sub_args.strip()}"
        return f"❌ 技能 /{sub_args.strip()} 不存在"

    elif canonical == "import":
        if not sub_args:
            return "用法: /skill import <github-url>"
        return _skill_import_from_url(manager, sub_args.strip())

    elif canonical == "reload":
        manager.load()
        skills = manager.list_all()
        return f"✅ 已从磁盘重新加载 {len(skills)} 个技能"

    elif canonical == "doctor":
        report = manager.diagnostics()
        lines = [
            "═══ Skill Doctor ═══",
            f"已加载: {report['skill_count']}（Agent Skill "
            f"{report['agent_skill_count']}，旧 YAML {report['legacy_skill_count']}）",
            "",
            "扫描目录:",
        ]
        for root in report["roots"]:
            state = "存在" if root["exists"] else "不存在"
            lines.append(f"  [{root['source']}] {root['path']} — {state}")
        if report["errors"]:
            lines.extend(["", f"加载错误（{len(report['errors'])}）:"])
            lines.extend(f"  - {error}" for error in report["errors"])
        else:
            lines.extend(["", "✅ 未发现格式或读取错误"])
        return "\n".join(lines)

    else:
        # v0.5.4: 自然语言技能创建 —— 仅当 args 包含实质性描述时才触发。
        # 单个 typo 词（如 /skill xyz）显示帮助而非静默创建 skill。
        # 阈值：args 总长度 > 15 字符或包含中文（说明用户在描述需求）。
        full_args = args.strip()
        has_chinese = any("一" <= c <= "鿿" for c in full_args)
        if len(full_args) > 15 or has_chinese:
            name = _extract_skill_name(sub, sub_args)
            return _skill_auto_generate(
                name, args, manager, registry, interactive=False
            )
        else:
            # sub 可能是 typo — 显示帮助并给出模糊匹配建议
            hint = ""
            matched = _fuzzy_match_subcommand(sub)
            if matched:
                hint = f"\n\n💡 你是不是想用 [bold]/skill {matched}[/bold]？"
            return (
                f"无法识别的子命令: [bold]{sub}[/bold]{hint}\n\n"
                f"用法: /skill [list|create|run|delete|import|reload|doctor]\n\n"
                f"📡 浏览云端库: /skill-discover | /skill-install <名称>\n"
                f"💡 自然语言创建: /skill 帮我设计前端页面的技能"
            )


def _skill_create_interactive(manager, registry=None) -> str:
    """交互式创建技能。支持智能生成和手动配置两种模式。"""
    from rich.prompt import Prompt as _Prompt

    console.print("\n[bold cyan]创建技能[/bold cyan]\n")

    name = _Prompt.ask("技能名称（不含 /）")
    description = _Prompt.ask("技能描述（用一句话说明这个技能做什么）")

    # 选择创建模式
    console.print("\n[dim]创建模式:[/dim]")
    console.print(
        "  [bold]1[/bold]. 🤖 智能生成 — 只需描述，Agent 自动生成步骤（推荐）"
    )
    console.print("  [bold]2[/bold]. ✏️  手动配置 — 逐步骤手动添加")

    mode = _Prompt.ask("选择模式", choices=["1", "2"], default="1")

    if mode == "1":
        return _skill_auto_generate(name, description, manager, registry)
    else:
        return _skill_manual_create(name, description, manager)


def _skill_auto_generate(
    name: str, description: str, manager, registry=None, *, interactive: bool = True
) -> str:
    """智能生成技能步骤。

    Args:
        name: 技能名称
        description: 技能描述
        manager: SkillManager 实例
        registry: ModelRegistry 实例
        interactive: True 时展示生成结果并让用户确认/编辑/取消；
                     False 时直接保存（自然语言快速创建）。
    """
    from rich.panel import Panel
    from rich.prompt import Prompt as _Prompt

    console.print("\n[dim]🤖 正在根据你的描述生成技能步骤...[/dim]\n")

    # 用 LLM 生成步骤
    steps, system_prompt = _generate_skill_steps(description, registry)

    if not steps:
        if interactive:
            console.print("[yellow]⚠️  自动生成失败，切换到手动模式。[/yellow]")
            return _skill_manual_create(name, description, manager)
        else:
            console.print("[yellow]⚠️  自动生成失败，使用默认步骤。[/yellow]")
            steps = _fallback_skill_steps(description)
            system_prompt = ""

    # 展示生成结果供用户学习
    console.print(
        Panel(
            _format_skill_preview(steps, system_prompt),
            title="[bold green]✅ 自动生成的技能[/bold green]",
            border_style="green",
            padding=(1, 2),
        )
    )

    if interactive:
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
            return _skill_manual_create(
                name,
                description,
                manager,
                pre_steps=steps,
                pre_system_prompt=system_prompt,
            )

    # 直接保存
    skill = manager.create(name, description, steps, system_prompt=system_prompt)
    _register_skill_handler(skill, manager)
    return f"✅ 技能 /{skill.name} 已创建！使用 /{skill.name} 或 /skill run {skill.name} 执行。"


def _generate_skill_steps(description: str, registry=None) -> tuple[list[dict], str]:
    """用 LLM 根据描述生成技能步骤。"""
    try:
        from xenon.utils.llm_client import chat_completion

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
            {
                "role": "system",
                "content": "你是一个技能配置生成器。根据用户描述生成可执行的技能步骤配置。只返回 JSON。",
            },
            {"role": "user", "content": prompt},
        ]

        for model_id in model_ids:
            try:
                response = chat_completion(
                    model_id, messages, max_tokens=1000, temperature=0.3
                )
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

    # 提取 JSON
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
        # 尝试找 JSON 对象
        brace_start = text.find("{")
        brace_end = text.rfind("}")
        if brace_start != -1 and brace_end != -1:
            try:
                data = json.loads(text[brace_start : brace_end + 1])
            except json.JSONDecodeError:
                return [], ""
        else:
            return [], ""

    steps = data.get("steps", [])
    system_prompt = data.get("system_prompt", "")

    # 验证步骤格式
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
        {
            "type": "llm",
            "prompt": f"根据以下需求执行操作:\n{{input}}\n\n需求: {description}",
            "output_var": "result",
        },
    ]


def _format_skill_preview(steps: list[dict], system_prompt: str) -> str:
    """格式化技能预览。"""
    lines = []
    if system_prompt:
        lines.append(f"[bold]系统提示词:[/bold] {system_prompt}\n")

    for i, step in enumerate(steps, 1):
        stype = step.get("type", "?")
        icons = {
            "llm": "🧠",
            "command": "⚡",
            "echo": "📢",
            "write_file": "📝",
            "read_file": "📖",
        }
        icon = icons.get(stype, "❓")

        if stype == "llm":
            prompt_preview = step.get("prompt", "")[:80]
            lines.append(f"  {icon} 步骤 {i} [cyan]LLM[/cyan]: {prompt_preview}")
        elif stype == "command":
            lines.append(
                f"  {icon} 步骤 {i} [yellow]命令[/yellow]: {step.get('action', '')}"
            )
        elif stype == "echo":
            lines.append(
                f"  {icon} 步骤 {i} [green]输出[/green]: {step.get('prompt', '')[:60]}"
            )
        elif stype == "write_file":
            lines.append(
                f"  {icon} 步骤 {i} [magenta]写文件[/magenta]: {step.get('file_path', '')}"
            )
        elif stype == "read_file":
            lines.append(
                f"  {icon} 步骤 {i} [blue]读文件[/blue]: {step.get('file_path', '')}"
            )

        if step.get("output_var"):
            lines.append(f"       → 输出到: [dim]{step['output_var']}[/dim]")

    return "\n".join(lines)


def _skill_manual_create(
    name: str, description: str, manager, pre_steps=None, pre_system_prompt=""
) -> str:
    """手动配置技能步骤。"""
    from rich.prompt import Prompt as _Prompt

    system_prompt = _Prompt.ask("系统提示词（可选）", default=pre_system_prompt or "")

    if pre_steps:
        console.print(
            f"\n[dim]已有 {len(pre_steps)} 个生成的步骤，继续添加更多步骤。[/dim]"
        )
        steps = list(pre_steps)
    else:
        console.print(
            "\n添加步骤（支持类型: llm, command, echo, write_file, read_file）"
        )
        steps = []

    while True:
        console.print(f"\n[dim]步骤 {len(steps) + 1}[/dim]")
        step_type = _Prompt.ask(
            "  类型",
            choices=["llm", "command", "echo", "write_file", "read_file", "done"],
        )
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
    _register_skill_handler(skill, manager)
    return f"✅ 技能 /{skill.name} 已创建！使用 /{skill.name} 或 /skill run {skill.name} 执行。"


# ── skill 辅助函数 ─────────────────────────────────────────


def _execute_installed_skill(
    manager, name, args, *, registry, session_state=None
) -> str:
    """Execute a skill through the safe agent loop when a REPL is available."""
    skill = manager.get(name)
    if skill is None:
        return f"❌ 技能 /{name} 不存在"
    if skill.is_agent_skill and session_state:
        repl = session_state.get("_repl")
        if repl is not None:
            repl._handle_chat(
                manager.build_agent_prompt(skill.name, args),
                skill_name=skill.name,
                skill_args=args,
            )
            return ""
    model_ids = registry.get_role_priority("planner")
    return manager.execute(name, args, model_priority=model_ids)


def _register_skill_handler(skill, manager) -> None:
    """v0.5.4: 动态注册 skill 为命令处理器，无需重启即可用 /<name> 调用。"""
    cmd_name = f"/{skill.name}"
    if cmd_name not in _HANDLERS:

        def make_handler(sk_name):
            def handler(*, args: str, registry, session_state=None, **kw: Any) -> str:
                return _execute_installed_skill(
                    manager,
                    sk_name,
                    args,
                    registry=registry,
                    session_state=session_state,
                )

            return handler

        _HANDLERS[cmd_name] = make_handler(skill.name)
        register_command(cmd_name, f"[技能] {skill.description}", cmd_name)


def _extract_skill_name(sub: str, sub_args: str) -> str:
    """v0.5.4: 从自然语言输入中提取 skill 名称。

    优先级: 1) sub_args 中的英文标识符  2) "创建xxx skill" 模式
    3) sub 本身（如果是有效英文名）  4) 自动生成
    失败时返回 'my-skill'。
    """
    import re

    combined = f"{sub} {sub_args}".strip() if sub_args else sub

    # 1) 尝试从 sub_args 中提取英文标识符（优先 sub_args 因为 sub 可能是 typo）
    if sub_args:
        # "创建/设计 xxx skill" → xxx
        m = re.search(
            r"(?:创建|设计|一个|叫|名为)\s*[\"']?([a-zA-Z][a-zA-Z0-9_-]*)", sub_args
        )
        if m:
            return m.group(1).strip("-").lower()

    # 2) 从完整输入中提取英文标识符
    # 优先匹配含连字符的完整标识符（如 frontend-design），但排除已知 typo
    _KNOWN_TYPOS = {"creat", "crate", "creaet", "lst", "ls", "del", "rm", "exec"}
    m = re.search(r"([a-zA-Z][a-zA-Z0-9]+(?:[_-][a-zA-Z][a-zA-Z0-9]+)+)", combined)
    if m:
        name = m.group(1).lower()
        if name not in _KNOWN_TYPOS:
            return name
    # 再匹配单标识符（排除已知 typo 和太短的名字）
    for m in re.finditer(r"([a-zA-Z][a-zA-Z0-9_-]{2,})", combined):
        name = m.group(1).lower()
        if name not in _KNOWN_TYPOS and len(name) >= 3:
            return name

    # 3) sub 本身可能是英文名（但不包括已知的 typo）
    if sub not in _KNOWN_TYPOS and re.match(r"^[a-zA-Z][a-zA-Z0-9_-]{2,}$", sub):
        return sub.lower()

    # 4) 无法提取英文名——用描述内容的稳定哈希生成唯一名（比时间戳更稳定）
    import hashlib

    content_hash = hashlib.md5(combined.encode()).hexdigest()[:6]
    return f"skill-{content_hash}"


def _github_curl_get(url: str):
    """Fetch a GitHub URL without placing its token in process arguments."""
    command = ["curl", "-sL"]
    auth_headers = github_auth_headers()
    input_text = None
    if auth_headers:
        command.extend(["--header", "@-"])
        input_text = "".join(f"{key}: {value}\n" for key, value in auth_headers.items())
    command.append(url)
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=30,
        input=input_text,
    )


def _skill_import_from_url(manager, url: str) -> str:
    """v0.5.4: 从 URL 导入 skill。

    支持 GitHub URL 格式:
    - https://github.com/owner/repo
    - https://github.com/owner/repo/tree/main/skills/my-skill
    - owner/repo
    """
    import re

    # 解析 URL
    url = url.strip()
    if not url.startswith(("http://", "https://", "github.com/")) and "/" not in url:
        return f"❌ 无法识别的 URL 格式: {url}\n   支持: https://github.com/owner/repo 或 owner/repo"

    if not url.startswith("http"):
        url = f"https://github.com/{url}"

    # 提取 owner/repo
    m = re.search(r"github\.com/([^/]+)/([^/]+?)(?:\.git)?(?:/|$)", url)
    if not m:
        return f"❌ 无法解析 GitHub URL: {url}"

    owner, repo = m.group(1), m.group(2)
    console.print(f"[dim]· 正在从 GitHub 获取: {owner}/{repo}...[/dim]")

    try:
        # 使用 gh CLI 或 curl 获取仓库内容
        # 方法 1: 尝试 gh CLI
        gh_available = (
            subprocess.run(["which", "gh"], capture_output=True, text=True).returncode
            == 0
        )

        if gh_available:
            # 用 gh 获取仓库文件树
            result = subprocess.run(
                ["gh", "repo", "view", f"{owner}/{repo}", "--json", "name,description"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                import json

                try:
                    info = json.loads(result.stdout)
                    repo_desc = info.get("description", "无描述")
                    console.print(f"[dim]  仓库: {repo_desc}[/dim]")
                except json.JSONDecodeError:
                    pass

        # 方法 2: 直接用 curl 获取文件列表
        api_url = f"https://api.github.com/repos/{owner}/{repo}/contents"
        result = _github_curl_get(api_url)

        if result.returncode != 0 or not result.stdout.strip():
            return f"❌ 无法获取仓库内容: {api_url}"

        import json as _json

        try:
            contents = _json.loads(result.stdout)
        except _json.JSONDecodeError:
            return "❌ 无法解析仓库内容 (可能触发了 GitHub API 限流)"

        if not isinstance(contents, list):
            return "❌ 仓库内容格式异常"

        # 查找 YAML 文件（优先 .xenon/skills/ 目录下的）
        yaml_files = []
        for item in contents:
            name = item.get("name", "")
            if name.endswith((".yaml", ".yml")):
                yaml_files.append(item)
            if name == ".xenon" and item.get("type") == "dir":
                # 递归获取 .xenon/skills/ 目录
                sub_url = item.get("url", "")
                sub_result = _github_curl_get(sub_url)
                try:
                    sub_contents = _json.loads(sub_result.stdout)
                    if isinstance(sub_contents, list):
                        for si in sub_contents:
                            if si.get("name") == "skills" and si.get("type") == "dir":
                                skills_url = si.get("url", "")
                                skills_result = _github_curl_get(skills_url)
                                try:
                                    skills_contents = _json.loads(skills_result.stdout)
                                    if isinstance(skills_contents, list):
                                        for ski in skills_contents:
                                            if ski.get("name", "").endswith(
                                                (".yaml", ".yml")
                                            ):
                                                yaml_files.append(ski)
                                except _json.JSONDecodeError:
                                    pass
                except _json.JSONDecodeError:
                    pass

        if not yaml_files:
            return (
                f"❌ 未在仓库 {owner}/{repo} 中找到 skill YAML 文件。\n"
                f"   请确保仓库包含有效的 skill 配置（.yaml 文件）。\n"
                f"   Skill 文件应包含: name, description, steps 字段。"
            )

        # 下载并导入每个 YAML 文件
        imported = []
        for yf in yaml_files:
            download_url = yf.get("download_url", "")
            if not download_url:
                continue

            yaml_result = _github_curl_get(download_url)
            if yaml_result.returncode != 0:
                continue

            try:
                import yaml as _yaml

                data = _yaml.safe_load(yaml_result.stdout)
                if not data or "name" not in data:
                    continue

                # 导入 skill
                skill = manager.create(
                    name=data["name"],
                    description=data.get("description", f"从 {owner}/{repo} 导入"),
                    steps=data.get("steps", []),
                    system_prompt=data.get("system_prompt", ""),
                    params=data.get("params", []),
                )
                _register_skill_handler(skill, manager)
                imported.append(skill.name)

            except Exception as e:
                console.print(
                    f"[yellow]⚠️  导入 {yf.get('name', '?')} 失败: {e}[/yellow]"
                )

        if imported:
            names = ", ".join(f"/{n}" for n in imported)
            return f"✅ 已从 {owner}/{repo} 导入 {len(imported)} 个技能: {names}"
        return "❌ 未能成功导入任何技能，请检查仓库中的 YAML 文件格式。"

    except subprocess.TimeoutExpired:
        return "❌ GitHub API 请求超时，请稍后重试。"
    except Exception as e:
        return f"❌ 导入失败: {e}"
