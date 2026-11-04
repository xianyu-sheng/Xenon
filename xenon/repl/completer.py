"""
prompt_toolkit 自定义补全器 — 支持斜杠命令、文件路径、模型名补全。

#183: the slash-command branch is now a real **command palette** — rows come
from the command registry (``command_catalog``), each row carries its group and
parameter hint as the right-hand meta column, and the list is ordered by
recency when the query is empty and by fuzzy score when the user types.  It is
still plain prompt_toolkit completion, so it inherits search, arrow keys, Enter
and Esc for free, and it degrades to nothing when there is no PTY.
"""

from __future__ import annotations

import re

from prompt_toolkit.completion import (
    Completer,
    Completion,
    PathCompleter,
)
from prompt_toolkit.document import Document

from xenon.repl.command_catalog import (
    CommandEntry,
    CommandRecents,
    build_catalog,
    rank,
)

# ``@path`` at the end of the input: a file reference the palette can insert.
_AT_REFERENCE = re.compile(r"(?:^|\s)@([^\s@]*)$")


class OmniCompleter(Completer):
    """上下文感知的智能补全器。

    - ``/`` 开头 → 命令面板（分组 + 参数提示 + 最近使用置顶 + 模糊搜索）
    - ``/set_model `` 后 → 模型别名补全
    - 其他 → 文件路径补全（当前工作目录）
    """

    def __init__(
        self,
        command_names: list[str] | None = None,
        *,
        recents: CommandRecents | None = None,
    ) -> None:
        self._command_names: list[str] = []
        self._catalog: tuple[CommandEntry, ...] = ()
        self._recents = recents
        if command_names is None:
            self._catalog = build_catalog()
            self._command_names = [entry.name for entry in self._catalog]
        else:
            self.update_commands(list(command_names))
        self._path_completer = PathCompleter(
            only_directories=False,
            expanduser=True,
            file_filter=None,  # 不过滤，所有文件都显示
        )
        self._model_aliases: list[str] = []
        # /set_model 的别名
        self._set_model_aliases = frozenset({"/set_model", "/model"})

    # ── registry / state ────────────────────────────────────────────

    def update_commands(self, command_names: list[str]) -> None:
        """Re-derive the palette for this command set (a new command appears here).

        Metadata always comes from the registry, so a name without an entry
        still shows up (as an unfiled row) instead of vanishing.
        """

        from xenon.repl.commands import COMMANDS

        self._command_names = list(command_names)
        self._catalog = build_catalog(
            {name: COMMANDS.get(name, {}) for name in self._command_names}
        )

    def update_models(self, aliases: list[str]) -> None:
        """更新可用模型别名列表。"""
        self._model_aliases = list(aliases)

    def record_use(self, name: str) -> None:
        """Remember that a command ran, so the palette puts it on top."""

        if self._recents is not None:
            self._recents.record(name)

    @property
    def catalog(self) -> tuple[CommandEntry, ...]:
        return self._catalog

    def palette(self, prefix: str = "") -> tuple[CommandEntry, ...]:
        """The rows the palette shows for a given ``/prefix`` (testable seam)."""

        query = prefix[1:] if prefix.startswith("/") else prefix
        # A trailing space means the user finished the command; the palette is
        # done and the command's own arguments take over.
        if prefix.endswith(" "):
            return ()
        recents = self._recents.names if self._recents is not None else ()
        return rank(self._catalog, query, recents=recents)

    def get_completions(self, document: Document, complete_event):
        text = document.text_before_cursor

        # ── 斜杠命令面板 ──
        if text.startswith("/"):
            # /set_model 或 /model 后面补全模型别名
            parts = text.split(maxsplit=1)
            if parts[0] in self._set_model_aliases and len(parts) > 1:
                prefix = parts[1]
                for alias in self._model_aliases:
                    if alias.startswith(prefix) or not prefix:
                        yield Completion(alias, start_position=-len(prefix))
                return

            head = parts[0]
            if " " in text:
                return  # arguments: the palette is closed
            for entry in self.palette(head):
                yield Completion(
                    entry.name,
                    start_position=-len(head),
                    display=entry.name,
                    display_meta=entry.palette_meta(),
                )
            return

        # ── @file 引用（#183）：选中的路径直接插入，不用手打 ──
        reference = _AT_REFERENCE.search(text)
        if reference is not None:
            prefix = reference.group(1)
            sub = Document(prefix, cursor_position=len(prefix))
            # PathCompleter emits only the *differing* suffix with
            # ``start_position=0`` (it strips the typed directory), so the
            # cursor-relative insert is what completes ``@xenon/ma`` →
            # ``@xenon/main.py``.  Recomputing the offset would eat real path
            # characters.
            for item in self._path_completer.get_completions(sub, complete_event):
                yield Completion(
                    item.text,
                    start_position=item.start_position,
                    display=item.text,
                    display_meta="文件引用",
                )
            return

        # ── 文件路径补全（非命令输入时） ──
        # 只在文本非空且可能包含路径时触发
        if text.strip():
            yield from self._path_completer.get_completions(document, complete_event)
