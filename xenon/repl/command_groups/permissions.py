"""/permissions — 查看与撤销"记住的授权"（#373）。

Xenon 的产品主张是**证据校验**；权限是**安全边界**。安全的正确形态不是"每次都问"
（用户会直接 `--permission bypass`，等于没有），而是"问一次、记下来、看得见、撤得掉"。

本命令只做三件事：

- 不带参数：列出当前生效的授权（会话级 + 规则文件里的），并标出来源；
- `remove <selector>`：删掉一条规则（写回文件），立即生效；
- `clear`：清空本会话记住的授权（不动规则文件）。

会话级授权**只存内存、带时间期限**（技术文档 §9），所以列表里会显示还剩多久；
进程结束或到期后重新询问。账本记录的是 `user_approval`（人做了什么决定）和
`policy_decision`（这条调用为什么没问），授权本身不入账。
"""

from __future__ import annotations

from typing import Any

from xenon.repl.command_registry import command_handler, register_command

register_command(
    "/permissions",
    "权限模式 + 记住的授权（查看/切换/撤销）",
    "/permissions [default|accept_edits|bypass|plan] [remove <selector> | clear]",
)

# The mode names this command accepts.  Kept here, next to the handler, because
# this command is now the *only* owner of /permissions (#183): ``workspace.py``
# used to register the same name later in ``commands.py`` and silently shadow
# this module, so the grants view added in #373 never reached a user.
#
# A mode name maps to the *execution level*, because that is what the kernel
# actually reads (``runtime_adapter.permission_mode_for``).  It used to call
# ``PermissionGate.set_mode`` on a gate the kernel never received: the command
# reported success while changing nothing.
MODE_MAP: dict[str, str] = {
    "default": "WRITE",
    "accept_edits": "WRITE",
    "plan": "READ_ONLY",
    "bypass": "EXECUTE",
}


def _current_level(repl: Any) -> int | None:
    ctx = getattr(repl, "agent_context", None)
    getter = getattr(ctx, "get", None)
    if not callable(getter):
        return None
    try:
        return int(getter("_execution_level"))
    except (TypeError, ValueError):
        return None


def _current_mode_name(repl: Any) -> str:
    """The mode the session is actually in.

    The stored name is authoritative: ``default`` and ``accept_edits`` share
    one execution level, so a level-only reverse mapping reported
    ``accept_edits`` as ``default`` (and pressing Enter in the picker then
    silently switched the user to the other one).
    """

    ctx = getattr(repl, "agent_context", None)
    getter = getattr(ctx, "get", None)
    if callable(getter):
        try:
            stored = str(getter("_permission_mode") or "")
        except Exception:  # noqa: BLE001 — a broken context is not a mode
            stored = ""
        if stored in MODE_MAP:
            return stored
    level = _current_level(repl)
    if level is None:
        return ""
    for name, key in MODE_MAP.items():
        from xenon.repl.execution_policy import ExecutionLevel

        if int(getattr(ExecutionLevel, key)) == level:
            return name
    return ""


def _switch_mode(repl: Any, mode_name: str) -> str | None:
    """Set the session's persistent permission mode; ``None`` means "not a name".

    The mode becomes a *ceiling*: every turn's classification is clamped to
    its level, and ``runtime_adapter`` chooses the matching ``PermissionMode``
    (``default`` → SCOPED, so writes keep asking; ``bypass`` → BYPASS).  Writing
    only ``_execution_level`` was not enough — turn_flow recomputes that key
    every turn from the classifier, so the switch silently evaporated.
    """

    key = MODE_MAP.get(mode_name)
    if key is None:
        return None
    ctx = getattr(repl, "agent_context", None)
    setter = getattr(ctx, "set", None)
    getter = getattr(ctx, "get", None)
    if not callable(setter):
        return "权限系统未初始化"
    from xenon.repl.execution_policy import ExecutionLevel

    level = getattr(ExecutionLevel, key)
    changed = not (callable(getter) and getter("_permission_mode") == mode_name)
    setter("_permission_mode", mode_name)
    setter("_execution_level", int(level))
    setter("_execution_reason", f"/permissions {mode_name}")
    # A mode change is an authorization boundary: remembered answers were
    # granted under the previous level and must not cross it.  Re-selecting
    # the same mode is not a change and keeps them.
    book = getattr(repl, "_grant_book", None)
    if changed and book is not None:
        book.clear()
    return f"✅ 权限模式: [bold cyan]{mode_name}[/bold cyan]（{key.lower()} 上限，持续生效直至切换）"


def _normalize_selector(text: str) -> str:
    """`  shell: git log` and `shell:git log` name the same grant.

    The listing indents rows and puts a space after the colon; a user copying
    a row to `/permissions remove` must not need to know that.
    """

    collapsed = " ".join(str(text or "").split())
    if not collapsed:
        return ""
    capability, _, targets = collapsed.partition(":")
    return f"{capability.strip()}:{targets.strip()}"


def _format_grant(grant: Any) -> str:
    """One remembered answer, with the path it covers and how long it has left.

    The deadline is shown because it is the answer to "why did it ask again?":
    a grant that quietly expired looks exactly like one that never existed.
    """

    import time

    targets = "、".join(grant.targets) or "(无路径)"
    line = f"  {grant.capability.value}: {targets}"
    if grant.one_shot:
        return line + "  [本次]"
    if grant.targets == ("*",) and grant.turn:
        return line + f"  [仅本轮 {grant.turn}]"
    if grant.expires_at is None:
        return line
    remaining = int(grant.expires_at - time.time())
    if remaining <= 0:
        return line + "  [已过期]"
    if remaining < 60:
        return line + f"  [剩余 {remaining} 秒]"
    return line + f"  [剩余 {remaining // 60} 分钟]"


@command_handler("/permissions")
def _cmd_permissions(*, args: str = "", session_state: dict, **kwargs: Any) -> str:
    repl = session_state.get("_repl")
    if repl is None:
        return "❌ /permissions 需要交互会话"

    book = getattr(repl, "_grant_book", None)

    command = (args or "").strip()
    head, _, rest = command.partition(" ")
    head = head.casefold()

    # #183: no arguments → arrow-key picker (permission mode).  Without a
    # terminal the text summary below is still the answer.
    if not command:
        from xenon.repl.selector import ask_choice, permission_choices

        current = _current_mode_name(repl)
        picked = ask_choice(
            repl, title="选择权限模式", choices=permission_choices(current)
        )
        if picked is not None:
            return _switch_mode(repl, picked) or f"⚠️ 未能切换为 {picked}"

    # A bare mode name switches the mode; `remove`/`clear` below manage grants.
    switched = _switch_mode(repl, head) if head in MODE_MAP else None
    if switched is not None:
        return switched

    if head == "clear":
        if book is None:
            return "本会话没有记住的授权。"
        count = len(book)
        book.clear()
        return f"已清空本会话 {count} 条授权。"

    if head == "remove":
        selector = _normalize_selector(rest)
        if not selector:
            return "用法：/permissions remove shell:git status"
        removed = False
        if book is not None:
            for index, grant in enumerate(list(book), start=1):
                wanted = _normalize_selector(
                    f"{grant.capability.value}:{'、'.join(grant.targets)}"
                )
                if wanted == selector:
                    book.revoke(index)
                    removed = True
                    break
        if not removed:
            return f"没有找到 {selector}。"
        return f"已撤销 {selector}。"

    rows: list[str] = []
    if book is not None and len(book):
        rows.append("会话级（本进程有效，到期自动失效）：")
        rows.extend(_format_grant(grant) for grant in book)
    if not rows:
        # Always answer "what mode am I in": this is the only door to it now.
        mode = _current_mode_name(repl) or "未初始化"
        return (
            f"执行级别: {mode}\n"
            "当前没有记住的授权。\n"
            "· 审批时按 y = 本次允许，t = 本轮内该能力不再问，"
            "a = 本会话都允许（仅这条命令/路径），n = 拒绝。\n"
            "· 切换级别：/permissions default|accept_edits|plan|bypass"
        )
    return "\n".join(rows) + "\n\n撤销：/permissions remove <能力:路径>；清空会话：/permissions clear"
