"""
OmniAgent-CLI 主入口。

直接运行 `omniagent` 即可启动交互式对话。
也支持 `omniagent run <workflow.yaml>` 批量执行工作流。
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.tree import Tree

console = Console()


def cli() -> None:
    """CLI 入口函数。直接 omniagent 启动 REPL。"""
    parser = argparse.ArgumentParser(
        prog="omniagent",
        description="OmniAgent-CLI — 多模型 AI 编程助手",
    )

    # 位置参数：可以是子命令(chat/run)或工作流文件
    parser.add_argument(
        "command_or_workflow",
        nargs="?",
        default=None,
        help="子命令 (chat/run) 或 YAML 工作流文件路径",
    )
    parser.add_argument(
        "workflow_path",
        nargs="?",
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "-m", "--model",
        nargs="+",
        metavar="PROVIDER/MODEL",
        help="初始模型列表",
    )
    parser.add_argument(
        "--mode",
        choices=["direct", "plan-execute", "react", "reflection"],
        default=None,
        help="初始思考范式",
    )
    parser.add_argument(
        "--system-prompt",
        default=None,
        help="自定义系统提示词",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="模型配置文件路径",
    )
    parser.add_argument(
        "--init-context",
        nargs="*",
        metavar="KEY=VALUE",
        help="初始上下文变量（run 模式）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅展示工作流结构，不执行（run 模式）",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="显示详细日志",
    )

    args = parser.parse_args()

    # 配置日志
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    cmd = args.command_or_workflow

    # 路由
    if cmd == "run":
        workflow = args.workflow_path
        if not workflow:
            console.print("[red]用法: omniagent run <workflow.yaml>[/red]")
            sys.exit(1)
        args.workflow = workflow
        _cmd_run(args)

    elif cmd == "chat":
        _cmd_chat(args)

    elif cmd and (cmd.endswith(".yaml") or cmd.endswith(".yml")):
        # omniagent workflow.yaml — 兼容旧用法
        args.workflow = cmd
        args.init_context = args.init_context or []
        args.dry_run = False
        _cmd_run(args)

    else:
        # 默认：直接启动 REPL
        _cmd_chat(args)


def _cmd_chat(args: argparse.Namespace) -> None:
    """启动交互式 REPL。"""
    from omniagent.repl.repl import start_repl

    start_repl(
        models=getattr(args, "model", None),
        mode=getattr(args, "mode", None),
        system_prompt=getattr(args, "system_prompt", None),
        config_path=getattr(args, "config", None),
    )


def _cmd_run(args: argparse.Namespace) -> None:
    """批量执行工作流。"""
    from omniagent.engine.context import AgentContext
    from omniagent.engine.scheduler import DAGScheduler
    from omniagent.utils.config_parser import load_yaml, parse_workflow

    config_path = Path(args.workflow)
    if not config_path.exists():
        console.print(f"[red]错误: 配置文件不存在: {config_path}[/red]")
        sys.exit(1)

    try:
        config = load_yaml(config_path)
        nodes, models = parse_workflow(config)
    except Exception as e:
        console.print(f"[red]配置解析失败: {e}[/red]")
        sys.exit(1)

    _display_workflow_info(config, nodes, models)

    if args.dry_run:
        console.print("\n[yellow]Dry-run 模式，不执行工作流。[/yellow]")
        return

    initial_ctx = {}
    if args.init_context:
        for kv in args.init_context:
            if "=" in kv:
                k, v = kv.split("=", 1)
                initial_ctx[k] = v

    context = AgentContext(initial=initial_ctx)
    start_node = config.get("start_node", _find_start_node(nodes))

    scheduler = DAGScheduler(nodes, start_node_id=start_node)
    try:
        result = scheduler.run(context)
        _display_result(result)
    except Exception as e:
        console.print(f"\n[red]工作流执行失败: {e}[/red]")
        sys.exit(1)


# ── 展示函数 ──────────────────────────────────────────────

def _display_workflow_info(
    config: dict,
    nodes: dict,
    models: dict[str, list[str]],
) -> None:
    """展示工作流概览信息。"""
    workflow_name = config.get("workflow", "unnamed")
    version = config.get("version", "unknown")

    console.print(Panel(
        f"[bold cyan]OmniAgent-CLI[/bold cyan] v{version}\n"
        f"工作流: [bold]{workflow_name}[/bold]",
        title="🚀 OmniAgent",
    ))

    if models:
        table = Table(title="📦 模型优先级配置")
        table.add_column("角色", style="cyan")
        table.add_column("优先级列表", style="green")
        for role, model_list in models.items():
            table.add_row(role, " → ".join(model_list))
        console.print(table)

    tree = Tree("📊 节点拓扑")
    for node_id, node in nodes.items():
        tree.add(f"[bold]{node_id}[/bold] ({node.__class__.__name__})")
    console.print(tree)


def _find_start_node(nodes: dict) -> str:
    from omniagent.nodes.router_node import RouterNode
    for node_id, node in nodes.items():
        if not isinstance(node, RouterNode):
            return node_id
    return next(iter(nodes))


def _display_result(result: dict) -> None:
    status = result.get("status", "unknown")
    steps = result.get("steps", 0)
    color = "green" if status == "completed" else "yellow"
    console.print(f"\n[{color}]状态: {status} | 执行步数: {steps}[/{color}]")

    log_entries = result.get("log", [])
    if log_entries:
        table = Table(title="📋 执行日志")
        table.add_column("步骤", style="dim")
        table.add_column("节点", style="cyan")
        table.add_column("状态", style="bold")
        table.add_column("详情")
        for entry in log_entries:
            step = str(entry["step"])
            node = entry["node"]
            st = entry["status"]
            detail = ""
            if st == "success" and entry.get("result"):
                res = entry["result"]
                if "content" in res:
                    detail = res["content"][:80] + ("..." if len(res["content"]) > 80 else "")
                elif "next_node" in res:
                    detail = f"→ {res['next_node']}"
                elif "stdout" in res:
                    detail = res["stdout"][:80]
            elif st == "error":
                detail = entry.get("error", "")
            st_style = "green" if st == "success" else "red"
            table.add_row(step, node, f"[{st_style}]{st}[/{st_style}]", detail)
        console.print(table)

    ctx = result.get("context")
    if ctx:
        console.print("\n[bold]📤 Context 输出:[/bold]")
        for key, value in ctx._store.items():
            preview = str(value)[:200]
            console.print(f"  [cyan]{key}[/cyan]: {preview}")
