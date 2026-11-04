"""Agent execution slash commands.

This group covers workflow execution, single-turn model queries,
code generation, and sub-agent delegation.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from xenon.repl.command_groups.common import confirm_action
from xenon.repl.command_registry import command_handler, register_command

if TYPE_CHECKING:
    from xenon.repl.context_manager import ContextManager
    from xenon.repl.model_registry import ModelRegistry


# /run ─────────────────────────────────────────────────────

register_command(
    "/run", "执行当前配置的工作流", "/run [workflow.yaml] [--init key=value]"
)


@command_handler("/run")
def _cmd_run(
    *, args: str, session_state: dict, registry: ModelRegistry, **kwargs: Any
) -> str:
    from xenon.engine.context import AgentContext
    from xenon.engine.scheduler import DAGScheduler
    from xenon.utils.config_parser import load_yaml, parse_workflow

    parts = args.split()
    workflow_path = None
    init_vars = {}

    i = 0
    while i < len(parts):
        if parts[i] == "--init" and i + 1 < len(parts):
            kv = parts[i + 1]
            if "=" in kv:
                k, v = kv.split("=", 1)
                init_vars[k] = v
            i += 2
        elif not workflow_path:
            workflow_path = parts[i]
            i += 1
        else:
            i += 1

    if not workflow_path:
        workflow_path = registry.get_current_mode().workflow_template
        if not workflow_path:
            return "❌ 未指定工作流文件且当前模式无默认模板"

    try:
        config = load_yaml(workflow_path)
        nodes, models = parse_workflow(config)
    except Exception as e:
        return f"❌ 配置解析失败: {e}"

    # 合并 session_state 中的 context 变量
    agent_ctx = session_state.get("agent_context")
    if agent_ctx:
        for k, v in init_vars.items():
            agent_ctx.set(k, v)
    else:
        agent_ctx = AgentContext(initial=init_vars)
        session_state["agent_context"] = agent_ctx

    start_node = config.get("start_node")
    if not start_node:
        for nid in nodes:
            if nodes[nid].__class__.__name__ != "RouterNode":
                start_node = nid
                break

    scheduler = DAGScheduler(nodes, start_node_id=start_node)
    try:
        result = scheduler.run(agent_ctx)
        lines = [f"✅ 工作流完成。状态: {result['status']}, 步数: {result['steps']}"]
        for entry in result.get("log", []):
            lines.append(f"  [{entry['step']}] {entry['node']}: {entry['status']}")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ 工作流执行失败: {e}"


# /ask ─────────────────────────────────────────────────────

register_command(
    "/ask",
    "向指定模型发送单次提问（不进入多轮对话）",
    "/ask <alias> <question>",
)


@command_handler("/ask")
def _cmd_ask(
    *, args: str, registry: ModelRegistry, ctx_mgr: ContextManager, **kwargs: Any
) -> str:
    from xenon.utils.llm_client import chat_completion

    parts = args.split(maxsplit=1)
    if len(parts) < 2:
        return "用法: /ask <alias> <question>"

    alias, question = parts[0], parts[1]
    model = registry.get_model(alias)
    if not model:
        return f"❌ 模型 '{alias}' 不存在。使用 /models 查看可用模型。"

    try:
        credentials = None
        if model.api_key and "/" in model.model_id:
            credentials = {model.model_id.split("/", 1)[0].lower(): model.api_key}
        response = chat_completion(
            model.model_id,
            [{"role": "user", "content": question}],
            max_tokens=model.max_tokens,
            temperature=model.temperature,
            credentials=credentials,
            base_url=model.base_url or None,
            reasoning_effort=model.reasoning_effort or None,
        )
        ctx_mgr.add_user_message(f"/ask {alias} {question}")
        ctx_mgr.add_assistant_message(response, model_used=model.model_id)
        return response
    except Exception as e:
        return f"❌ 调用失败: {e}"


# /code ────────────────────────────────────────────────────

register_command(
    "/code",
    "生成代码并写入文件，可选运行",
    "/code <任务描述> [--file path] [--run] [--lang python]",
)


@command_handler("/code")
def _cmd_code(
    *,
    args: str,
    registry: ModelRegistry,
    ctx_mgr: ContextManager,
    session_state: dict,
    **kwargs: Any,
) -> str:
    import re
    import subprocess
    import sys
    from pathlib import Path

    from xenon.utils.llm_client import chat_completion

    if not args:
        return "用法: /code <任务描述> [--file path] [--run] [--lang python]"

    # 解析参数
    parts = args.split()
    task_parts = []
    file_path = None
    run_code = False
    lang = "python"

    i = 0
    while i < len(parts):
        if parts[i] == "--file" and i + 1 < len(parts):
            file_path = parts[i + 1]
            i += 2
        elif parts[i] == "--run":
            run_code = True
            i += 1
        elif parts[i] == "--lang" and i + 1 < len(parts):
            lang = parts[i + 1]
            i += 2
        else:
            task_parts.append(parts[i])
            i += 1

    task = " ".join(task_parts)
    if not task:
        return "请提供任务描述"

    # 获取模型
    model_ids = registry.get_role_priority("coder") or registry.get_role_priority(
        "planner"
    )
    if not model_ids:
        return "❌ 未配置模型。请先 /set_model"

    # 生成代码
    prompt = f"""请根据以下任务生成 {lang} 代码。只输出代码，不要解释。

任务: {task}

要求:
1. 只输出代码，不要 markdown 代码块标记
2. 代码必须完整可运行
3. 包含必要的 import 和注释"""

    try:
        model = registry.get_model_by_id(model_ids[0])
        credentials = None
        if model and model.api_key and "/" in model.model_id:
            credentials = {model.model_id.split("/", 1)[0].lower(): model.api_key}
        code = chat_completion(
            model_ids[0],
            [{"role": "user", "content": prompt}],
            max_tokens=model.max_tokens if model else 4096,
            temperature=model.temperature if model else 0.7,
            credentials=credentials,
            base_url=(model.base_url or None) if model else None,
            reasoning_effort=(model.reasoning_effort or None) if model else None,
        )
    except Exception as e:
        return f"❌ 代码生成失败: {e}"

    # 清理代码（移除可能的 markdown 标记）
    code = re.sub(r"^```\w*\n?", "", code, flags=re.MULTILINE)
    code = re.sub(r"\n?```$", "", code, flags=re.MULTILINE)
    code = code.strip()

    # 确定文件路径
    if not file_path:
        ext = {
            "python": ".py",
            "javascript": ".js",
            "typescript": ".ts",
            "bash": ".sh",
        }.get(lang, ".txt")
        file_path = f"generated_code{ext}"

    # 写入文件
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(code, encoding="utf-8")

    result_lines = [f"✅ 代码已写入: {path.absolute()}"]
    result_lines.append(f"  语言: {lang}")
    result_lines.append(f"  行数: {len(code.splitlines())}")

    # 记录到 context
    ctx_mgr.add_user_message(f"/code {task}")
    ctx_mgr.add_assistant_message(f"生成代码并写入 {path}", model_used=model_ids[0])

    # 可选运行
    if run_code and lang == "python":
        # A11: 执行 LLM 生成代码前人机确认，显示完整代码
        from rich.console import Console as _Console
        from rich.syntax import Syntax as _Syntax

        console = kwargs.get("console") or _Console()
        console.print("\n[bold]⚠️ 即将执行 LLM 生成的代码:[/bold]")
        console.print(_Syntax(code, "python", theme="monokai", line_numbers=True))
        if not confirm_action("确认执行以上代码？", default=False):
            result_lines.append("⏭️ 已取消执行")
            return "\n".join(result_lines)
        result_lines.append("\n▶️  运行代码...")
        try:
            proc = subprocess.run(
                [sys.executable, str(path)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode == 0:
                result_lines.append("✅ 运行成功:")
                if proc.stdout:
                    result_lines.append(proc.stdout)
            else:
                result_lines.append(f"❌ 运行失败 (返回码 {proc.returncode}):")
                if proc.stderr:
                    result_lines.append(proc.stderr)
                if proc.stdout:
                    result_lines.append(proc.stdout)
        except subprocess.TimeoutExpired:
            result_lines.append("⏰ 运行超时 (30s)")
        except Exception as e:
            result_lines.append(f"❌ 运行异常: {e}")

    return "\n".join(result_lines)


# Runtime toggle commands live in repl.command_groups.runtime.


# /sub-agent ──────────────────────────────────────────────

register_command(
    "/sub-agent",
    "委派子 Agent 执行任务（单循环的阶段配置 + 并行）",
    "/sub-agent <task> [--profile react|plan-execute|reflection|plan-react] [--timeout N] [--parallel task1|task2|...]",
)


def _sub_agent_usage() -> str:
    """Usage text derived from the *accepted* profiles, never a second list.

    The old text advertised seven "engines", three of which this command has
    never accepted (``direct``, ``plan-reflection``, ``react-reflection``) — a
    menu that lies about what it can do (#294).
    """

    from xenon.harness.child import CHILD_PROFILES

    labels = {
        "kernel": "默认 kernel 循环（无额外阶段）",
        "react": "执行（react 阶段），适合多步工具任务",
        "plan-execute": "规划 + 按计划执行（follow_plan）",
        "plan-react": "规划 + 每步限定调用次数的执行",
        "reflection": "执行 + 一轮反思审查",
    }
    lines = [
        "",
        "用法:",
        "  /sub-agent <task>                        默认 profile（react）",
        "  /sub-agent <task> --profile plan-execute 指定阶段配置",
        "  /sub-agent <task> --timeout 30           设置超时（秒）",
        "  /sub-agent --parallel taskA|taskB|taskC  并行执行（最多 10 个）",
        "",
        "可选 profile（同一个循环的阶段配置）:",
    ]
    for name in sorted(CHILD_PROFILES):
        lines.append(f"  {name:<16} {labels.get(name, name)}")
    lines.extend(
        [
            "",
            "示例:",
            "  /sub-agent 分析 xenon/nodes/tool_node.py 的代码质量",
            "  /sub-agent 给 lsp_provider.py 写单元测试 --profile plan-execute",
            '  /sub-agent --parallel "审查repl.py"|"审查commands.py"',
        ]
    )
    return chr(10).join(lines) + chr(10)


@command_handler("/sub-agent")
def _cmd_sub_agent(*, args: str, session_state: dict, repl=None, **kwargs: Any) -> str:
    """v0.6.1: 显式委派子 Agent 执行任务。

    用法:
      /sub-agent <task>                         # 默认 profile（react）
      /sub-agent <task> --profile plan-execute  # 指定阶段配置
      /sub-agent <task> --timeout 30            # 30 秒超时
      /sub-agent --parallel taskA|taskB|taskC   # 并行 3 个子任务

    ``--engine`` 仍然接受（旧脚本），但语义一直是「单循环的阶段配置」。
    """
    if not args or not args.strip():
        return "📋 /sub-agent — 委派子 Agent 执行任务" + chr(10) + chr(10) + _sub_agent_usage()

    # 解析参数
    import shlex

    parts = shlex.split(args)

    engine_type = "react"
    timeout = None
    parallel_tasks = None

    i = 0
    task_parts = []
    while i < len(parts):
        if parts[i] in {"--engine", "--profile"} and i + 1 < len(parts):
            engine_type = parts[i + 1].lower()
            i += 2
        elif parts[i] == "--timeout" and i + 1 < len(parts):
            try:
                timeout = int(parts[i + 1])
            except ValueError:
                return f"❌ --timeout 必须为整数，收到: {parts[i + 1]}"
            i += 2
        elif parts[i] == "--parallel":
            if i + 1 < len(parts):
                parallel_tasks = [
                    t.strip() for t in parts[i + 1].split("|") if t.strip()
                ]
            else:
                return "❌ --parallel 需要任务列表（用 | 分隔）"
            i += 2
        else:
            task_parts.append(parts[i])
            i += 1

    task = " ".join(task_parts).strip()

    # 获取模型配置
    if repl is None:
        return "❌ 无法获取 REPL 实例"

    from xenon.harness.child import (
        CHILD_PROFILES,
        child_evidence,
        run_child_task,
        run_child_tasks,
    )
    from xenon.repl.runtime_adapter import (
        KernelApprovalProvider,
        model_config_for,
        permission_mode_for,
    )

    profile = engine_type.replace("_", "-")
    if profile not in CHILD_PROFILES:
        return (
            f"❌ 未知子任务 profile: {engine_type}"
            f"（可用: {', '.join(sorted(CHILD_PROFILES))}）"
        )

    model_ids = [
        e.model_id
        for e in (repl.model_pool.get_healthy() or repl.model_pool.list_all())
    ]
    if not model_ids:
        return "❌ 模型池为空，请先运行 /setup 配置模型。"

    workspace = (
        getattr(getattr(repl, "project_ctx", None), "root", None) or Path.cwd()
    )
    parent_task_id = getattr(repl, "_last_kernel_task_id", "") or "repl"
    ledger_dir = Path.home() / ".xenon" / "ledger" / "children"
    model = model_config_for(repl.registry, model_ids)
    # The parent's authority travels with the delegation: the child asks, the
    # REPL answers using the same confirmation UI a kernel turn would use.  A
    # child can therefore never obtain more than the session already allows.
    approval = KernelApprovalProvider(repl)
    parent_permission = permission_mode_for(
        getattr(repl, "_execution_level", 0)
    )
    # The child inherits the session's remembered decisions, filtered to this
    # workspace (#373).  It still cannot create new ones: the broker forwards
    # the request, and the parent answers with a degraded y/n/q prompt.
    from xenon.kernel.session_grants import GrantBook

    book = getattr(repl, "_grant_book", None)
    if book is None:
        book = GrantBook(workspace=str(workspace))
        repl._grant_book = book
    inherited = tuple(book.active(str(workspace)))

    if parallel_tasks:
        outcomes = run_child_tasks(
            objectives=parallel_tasks,
            workspace=workspace,
            model=model,
            profile=profile,
            parent_task_id=parent_task_id,
            ledger_dir=ledger_dir,
            max_steps=15,
            timeout=timeout,
            approval=approval,
            parent_permission=parent_permission,
            session_grants=inherited,
        )
    else:
        if not task:
            return "❌ 请提供任务描述"
        outcomes = (
            run_child_task(
                objective=task,
                workspace=workspace,
                model=model,
                profile=profile,
                parent_task_id=parent_task_id,
                ledger_dir=ledger_dir,
                max_steps=15,
                timeout=timeout,
                approval=approval,
                parent_permission=parent_permission,
                session_grants=inherited,
            ),
        )

    # The parent records a pointer to each child ledger; it never copies the
    # child's events into its own ledger (single-writer stays intact).
    log = getattr(repl, "_session_events", None)
    if log is not None:
        for outcome in outcomes:
            try:
                log.append(
                    "child_task",
                    role="tool",
                    content=outcome.objective[:200],
                    metadata=child_evidence(outcome),
                )
            except Exception:  # noqa: BLE001 — evidence must not break the run
                pass

    delivered = sum(1 for outcome in outcomes if outcome.delivered)
    return (
        f"🧬 子任务 {delivered}/{len(outcomes)} 已交付\n\n"
        + "\n\n".join(outcome.summary() for outcome in outcomes)
    )
