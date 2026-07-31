"""Extensible slash-command registry and dispatch boundary.

Command implementations live in ``repl.commands`` for now, but registration
and dispatch are intentionally dependency-light.  Future plugins can register
commands without importing the whole REPL command implementation module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from xenon.repl.context_manager import ContextManager
    from xenon.repl.model_registry import ModelRegistry


CommandHandler = Callable[..., str | None]
COMMANDS: dict[str, dict[str, Any]] = {}
_HANDLERS: dict[str, CommandHandler] = {}


class ExitSignal(Exception):
    """通知 REPL 主循环退出。"""


def register_command(name: str, description: str, usage: str = "") -> None:
    """Register or update slash-command metadata."""
    COMMANDS[name] = {"description": description, "usage": usage}


def command_handler(name: str):
    """Decorator registering a slash-command implementation."""
    def decorator(func: CommandHandler) -> CommandHandler:
        _HANDLERS[name] = func
        return func

    return decorator


def dispatch_command(
    name: str,
    args: str,
    *,
    registry: ModelRegistry,
    ctx_mgr: ContextManager,
    session_state: dict[str, Any],
) -> str | None:
    """Dispatch a command and convert handler failures to user-facing text."""
    handler = _HANDLERS.get(name)
    if not handler:
        return f"未知命令: {name}。输入 /help 查看可用命令。"
    try:
        return handler(
            args=args,
            registry=registry,
            ctx_mgr=ctx_mgr,
            session_state=session_state,
        )
    except ExitSignal:
        raise
    except Exception as exc:
        return f"❌ 命令执行失败 ({name}): {exc}"

