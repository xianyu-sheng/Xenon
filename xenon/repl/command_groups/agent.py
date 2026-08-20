"""Agent execution slash commands.

This group covers workflow execution, single-turn model queries,
code generation, and sub-agent delegation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from xenon.repl.command_groups.common import confirm_action
from xenon.repl.command_registry import command_handler, register_command

if TYPE_CHECKING:
    from xenon.repl.model_registry import ModelRegistry
    from xenon.repl.context_manager import ContextManager


# /run ─────────────────────────────────────────────────────

register_command("/run", "执行当前配置的工作流", "/run [workflow.yaml] [--init key=value]")

@command_handler("/run")
def _cmd_run(*, args: str, session_state: dict, registry: ModelRegistry, **kwargs: Any) -> str:
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
            return "❌ 未指定工作流文件且当前范式无默认模板"

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
def _cmd_ask(*, args: str, registry: ModelRegistry, ctx_mgr: ContextManager, **kwargs: Any) -> str:
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
def _cmd_code(*, args: str, registry: ModelRegistry, ctx_mgr: ContextManager, session_state: dict, **kwargs: Any) -> str:
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
    model_ids = registry.get_role_priority("coder") or registry.get_role_priority("planner")
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
    code = re.sub(r'^```\w*\n?', '', code, flags=re.MULTILINE)
    code = re.sub(r'\n?```$', '', code, flags=re.MULTILINE)
    code = code.strip()

    # 确定文件路径
    if not file_path:
        ext = {"python": ".py", "javascript": ".js", "typescript": ".ts", "bash": ".sh"}.get(lang, ".txt")
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
    "委派子 Agent 执行任务（支持多引擎和并行）",
    "/sub-agent <task> [--engine react|plan_execute|reflection|plan_react|plan_reflection|react_reflection|direct] [--timeout N] [--parallel task1|task2|...]",
)

@command_handler("/sub-agent")
def _cmd_sub_agent(*, args: str, session_state: dict, repl=None, **kwargs: Any) -> str:
    """v0.6.1: 显式委派子 Agent 执行任务。

    用法:
      /sub-agent <task>                         # 默认 ReAct 引擎
      /sub-agent <task> --engine plan_execute   # 指定引擎
      /sub-agent <task> --timeout 30            # 30 秒超时
      /sub-agent --parallel taskA|taskB|taskC   # 并行 3 个子任务
    """
    from xenon.engine.react_engine import ReActEngine
    from xenon.engine.context import AgentContext

    if not args or not args.strip():
        return (
            "📋 /sub-agent — 委派子 Agent 执行任务\n\n"
            "用法:\n"
            "  /sub-agent <task>                        默认 ReAct 引擎\n"
            "  /sub-agent <task> --engine plan_execute  指定引擎类型\n"
            "  /sub-agent <task> --timeout 30           设置超时（秒）\n"
            "  /sub-agent --parallel taskA|taskB|taskC  并行执行（最多 10 个）\n\n"
            "引擎类型:\n"
            "  react              思考-行动循环（默认，适合复杂多步任务）\n"
            "  plan_execute       规划-执行（适合多步骤结构化任务）\n"
            "  reflection         反思-修正（适合需要自我审查的任务）\n"
            "  plan_react         规划+ReAct 组合（先规划再逐步执行）\n"
            "  plan_reflection    规划+反思组合（规划执行后自我审查）\n"
            "  react_reflection   ReAct+反思组合（探索后自我审查）\n"
            "  direct             直答（无工具，适合简单问答）\n\n"
            "示例:\n"
            "  /sub-agent 分析 xenon/nodes/tool_node.py 的代码质量\n"
            "  /sub-agent 给 lsp_provider.py 写单元测试 --engine plan_execute\n"
            '  /sub-agent --parallel "审查repl.py"|"审查commands.py"|"审查react_engine.py"\n'
        )

    # 解析参数
    import shlex
    parts = shlex.split(args)

    engine_type = "react"
    timeout = None
    parallel_tasks = None

    i = 0
    task_parts = []
    while i < len(parts):
        if parts[i] == "--engine" and i + 1 < len(parts):
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
                parallel_tasks = [t.strip() for t in parts[i + 1].split("|") if t.strip()]
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

    model_ids = [e.model_id for e in (repl.model_pool.get_healthy() or repl.model_pool.list_all())]
    if not model_ids:
        return "❌ 模型池为空，请先运行 /setup 配置模型。"
    model_configs = getattr(repl, '_model_configs', None) or {}

    # 构建引擎
    engine = ReActEngine(
        model_ids,
        max_iterations=15,
        callback=getattr(repl, '_engine_callback', None),
        model_configs=model_configs,
        subagent_timeout=timeout,
    )

    # 构建上下文
    ctx = AgentContext()
    # 复制当前对话历史（最近 10 条）
    try:
        history = repl.ctx_mgr.get_messages()[-10:]
        ctx.set_conversation_messages(list(history))
    except Exception:
        pass

    # 构建 action_input
    if parallel_tasks:
        action_input: dict[str, Any] = {
            "task_list": [
                {"task": t, "engine": "react"} for t in parallel_tasks
            ]
        }
        display_task = f"并行 {len(parallel_tasks)} 个子任务"
    else:
        if not task:
            return "❌ 请提供任务描述"
        action_input = {"task": task, "engine": engine_type}
        if timeout:
            action_input["timeout"] = timeout
        display_task = task

    import logging
    logger = logging.getLogger(__name__)
    logger.info("用户 /sub-agent 委派: %s (引擎=%s)", display_task[:80], engine_type)

    try:
        result = engine._spawn_subagent(action_input, ctx, None)
        return result
    except Exception as e:
        logger.exception("/sub-agent 执行失败")
        return f"❌ 子 Agent 执行失败: {e}"


