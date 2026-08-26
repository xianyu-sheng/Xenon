"""Shared presentation primitives for slash-command groups."""

from __future__ import annotations


from rich.console import Console


console = Console()


def confirm_action(prompt: str, default: bool = False) -> bool:
    """破坏性操作确认对话框（P3-Q8 / §8.20.9）。

    - 脚本/测试可设 ``XENON_ASSUME_YES=1`` 或配置 ``interaction.assume_yes``
      跳过确认（非交互 seam）；
    - 非交互环境无 stdin（``EOFError``）时保守取 ``default``（通常取消），
      避免 hang 或崩；
    - 交互环境走 ``rich.prompt.Confirm.ask``。
    """
    from xenon.repl.system_config import get_config

    if get_config().interaction.assume_yes:
        return True
    from rich.prompt import Confirm as _Confirm

    try:
        return _Confirm.ask(prompt, default=default)
    except EOFError:
        return default
