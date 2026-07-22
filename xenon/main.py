"""
Xenon 主入口。

直接运行 `xenon` 即可启动交互式对话。
也支持 `xenon run <workflow.yaml>` 批量执行工作流。
"""

from __future__ import annotations

import sys

# ── 设置 UTF-8 编码（必须在其他导入之前）──
if sys.platform == "win32":
    import os
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    # 启用 Windows ANSI 支持
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleOutputCP(65001)  # UTF-8
        kernel32.SetConsoleCP(65001)
    except Exception:
        pass

import argparse
import logging
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.tree import Tree

from xenon.repl.model_registry import BUILTIN_MODES

console = Console()


class _DimNetworkFormatter(logging.Formatter):
    """交互终端中降低网络请求日志亮度，不影响重定向和日志采集。"""

    _DIM_LOGGERS = ("httpx", "httpcore", "openai")

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        if (
            record.name.startswith(self._DIM_LOGGERS)
            and getattr(sys.stderr, "isatty", lambda: False)()
        ):
            return f"\033[2m{rendered}\033[0m"
        return rendered


def cli() -> None:
    """CLI 入口函数。直接 xenon 启动 REPL。"""
    parser = argparse.ArgumentParser(
        prog="xenon",
        description="Xenon — 多模型 AI 编程助手",
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
        choices=list(BUILTIN_MODES.keys()),
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
        "-f", "--file",
        default=None,
        help="models import 子命令的配置文件路径",
    )
    parser.add_argument(
        "--no-probe",
        action="store_true",
        help="models import 时跳过单 token 探针验活",
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
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__import__('xenon').__version__}",
        help="显示版本号",
    )

    args = parser.parse_args()

    # 配置日志
    log_level = logging.DEBUG if args.verbose else logging.INFO
    log_handler = logging.StreamHandler()
    log_handler.setFormatter(_DimNetworkFormatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    ))
    logging.basicConfig(level=log_level, handlers=[log_handler])

    cmd = args.command_or_workflow

    # 路由
    if cmd == "run":
        workflow = args.workflow_path
        if not workflow:
            console.print("[red]用法: xenon run <workflow.yaml>[/red]")
            sys.exit(1)
        args.workflow = workflow
        _cmd_run(args)

    elif cmd == "chat":
        _cmd_chat(args)

    elif cmd == "models":
        sub = args.workflow_path
        if sub == "import":
            _cmd_models_import(args)
        else:
            console.print("[red]用法: xenon models import -f <file>[/red]")
            sys.exit(1)

    elif cmd and (cmd.endswith(".yaml") or cmd.endswith(".yml")):
        # xenon workflow.yaml — 兼容旧用法
        args.workflow = cmd
        args.init_context = args.init_context or []
        args.dry_run = False
        _cmd_run(args)

    else:
        # 默认：直接启动 REPL
        _cmd_chat(args)


def _cmd_chat(args: argparse.Namespace) -> None:
    """启动交互式 REPL。"""
    from xenon.repl.repl import start_repl

    start_repl(
        models=getattr(args, "model", None),
        mode=getattr(args, "mode", None),
        system_prompt=getattr(args, "system_prompt", None),
        config_path=getattr(args, "config", None),
        verbose=getattr(args, "verbose", False),
    )


def _cmd_models_import(args: argparse.Namespace) -> None:
    """P1-A: 非交互批量注册模型(脚本/CI 友好)。"""
    from xenon.repl.batch_register import batch_register
    from xenon.repl.model_registry import ModelRegistry
    from xenon.repl.model_pool import ModelPool

    file = getattr(args, "file", None)
    if not file:
        console.print("[red]用法: xenon models import -f <file>[/red]")
        sys.exit(1)

    registry = ModelRegistry()
    pool = ModelPool()
    result = batch_register(file, registry, pool, probe=not args.no_probe, dry_run=args.dry_run)
    console.print(result.summary())

    if not args.dry_run and (result.registered or result.updated):
        try:
            persist = Path.home() / ".xenon" / "models.yaml"
            registry.save_to_file(persist)
            console.print(f"[dim]💾 已持久化到 {persist}[/dim]")
        except Exception as e:
            console.print(f"[yellow]⚠️  持久化失败: {e}[/yellow]")
    if result.failed:
        sys.exit(1)


def _cmd_run(args: argparse.Namespace) -> None:
    """批量执行工作流。"""
    from xenon.engine.context import AgentContext
    from xenon.engine.scheduler import DAGScheduler
    from xenon.utils.config_parser import load_yaml, parse_workflow

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
        f"[bold cyan]Xenon[/bold cyan] v{version}\n"
        f"工作流: [bold]{workflow_name}[/bold]",
        title="✶ Xenon",
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
    from xenon.nodes.router_node import RouterNode
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
        for key, value in ctx.items():
            preview = str(value)[:200]
            console.print(f"  [cyan]{key}[/cyan]: {preview}")


if __name__ == "__main__":
    cli()
