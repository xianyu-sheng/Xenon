"""/permissions — 查看与撤销"记住的授权"（#373）。

Xenon 的产品主张是**证据校验**；权限是**安全边界**。安全的正确形态不是"每次都问"
（用户会直接 `--permission bypass`，等于没有），而是"问一次、记下来、看得见、撤得掉"。

本命令只做三件事：

- 不带参数：列出当前生效的授权（会话级 + 规则文件里的），并标出来源；
- `remove <selector>`：删掉一条规则（写回文件），立即生效；
- `clear`：清空本会话记住的授权（不动规则文件）。

每条被授予的权限在账本里都有 `grant_added`，回放时能回答"为什么这次没问"。
"""

from __future__ import annotations

from typing import Any

from xenon.repl.command_registry import command_handler, register_command

register_command(
    "/permissions",
    "查看/撤销记住的授权（会话级 + 规则文件）",
    "/permissions [remove <selector> | clear]",
)


def _format_rule(row: dict[str, Any]) -> str:
    selector = f"{row['capability']}({row['pattern']})"
    source = row.get("source") or "?"
    return f"  {selector:32s} [{source}]"


@command_handler("/permissions")
def _cmd_permissions(*, args: str = "", session_state: dict, **kwargs: Any) -> str:
    repl = session_state.get("_repl")
    if repl is None:
        return "❌ /permissions 需要交互会话"

    rules = getattr(repl, "_permission_rules", None)
    book = getattr(repl, "_grant_book", None)
    workspace = getattr(getattr(repl, "project_ctx", None), "root", None)

    command = (args or "").strip()
    head, _, rest = command.partition(" ")
    head = head.casefold()

    if head == "clear":
        if book is None:
            return "本会话没有记住的授权。"
        count = len(book)
        book.clear()
        return f"已清空本会话 {count} 条授权（规则文件未改动）。"

    if head == "remove":
        selector = rest.strip()
        if not selector:
            return "用法：/permissions remove shell(git log)"
        removed = False
        if rules is not None:
            removed = rules.remove(selector)
        if book is not None and removed is False:
            # Remove the session grant whose selector matches.
            for index, grant in enumerate(list(book), start=1):
                if f"{grant.capability.value}({grant.scope})" == selector:
                    book.revoke(index)
                    removed = True
                    break
        if not removed:
            return f"没有找到 {selector}。"
        _persist_rules(repl, rules, workspace)
        return f"已撤销 {selector}。"

    rows: list[str] = []
    if book is not None and len(book):
        rows.append("会话级（本次运行有效）：")
        rows.extend(
            f"  {grant.capability.value}({grant.scope}){'  *' * (grant.scope == '*')}"
            for grant in book
        )
    if rules is not None and getattr(rules, "rules", None):
        rows.append("规则文件（持久化）：")
        rows.extend(_format_rule(row) for row in rules.rows())
    if not rows:
        return (
            "当前没有记住的授权。\n"
            "· 审批时按 a = 本会话都允许，A = 永远允许（写入规则文件），"
            "s = 只允许这条命令。"
        )
    return "\n".join(rows) + "\n\n撤销：/permissions remove <selector>；清空会话：/permissions clear"


def _persist_rules(repl: Any, rules: Any, workspace: Any) -> None:
    """Rewrite the user rules file from the in-memory set after a removal."""

    if rules is None:
        return
    try:
        from xenon.kernel.permission_rules import default_rule_paths

        paths = dict(default_rule_paths(workspace))
        path = paths["user"]
        document = rules.to_document()
        # Only the user file is rewritten; project files stay under git.
        path.parent.mkdir(parents=True, exist_ok=True)
        import json

        path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001 — a failed write must not break the REPL
        pass
