"""Shared presentation primitives for slash-command groups."""

from __future__ import annotations

from typing import Any

from rich.console import Console

console = Console()


def ordered_model_candidates(registry: Any, repl: Any = None, *roles: str) -> list[str]:
    """Candidate order for a one-shot LLM call: session choice first.

    ``_preferred_model_ids`` is the session's contract for "which model runs
    here" — ``--model`` seeds it and ``/set_model``//model`` rewrite it, and the
    status bar plus kernel turns read it.  Several command paths built their
    list from the role priority alone, so a session explicitly started on a
    working model still dispatched to whichever model happened to sit first in
    ``roles``: the request 402'd against a dead proxy while the status bar
    showed a healthy model, and the command either failed (``/code``,
    ``/sub-agent``) or spent minutes retrying before it reached the model the
    user chose (``/edit``).  Preferred first, then the role list, deduplicated.
    """

    candidates: list[str] = []

    def add(model_id: Any) -> None:
        text = str(model_id or "").strip()
        if not text:
            return
        # ``_preferred_model_ids`` may hold an alias (``deepseek-v4-flash``)
        # while the pool and ``chat_completion`` speak canonical ids
        # (``deepseek/deepseek-v4-flash``).  Normalise through the registry so
        # the same model is never queued twice under two spellings.
        configured = registry.get_model_by_id(text) if registry is not None else None
        if configured is not None:
            text = configured.model_id
        if text not in candidates:
            candidates.append(text)

    preferred = getattr(repl, "_preferred_model_ids", None) or []
    if isinstance(preferred, (list, tuple)):
        for model_id in preferred:
            add(model_id)
    if registry is not None:
        for role in roles:
            for model_id in registry.get_role_priority(role):
                add(model_id)
    return candidates


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
