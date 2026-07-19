"""
prompt_toolkit 自定义补全器 — 支持斜杠命令、文件路径、模型名补全。
"""

from __future__ import annotations

from prompt_toolkit.completion import Completer, Completion, PathCompleter, WordCompleter
from prompt_toolkit.document import Document


class OmniCompleter(Completer):
    """上下文感知的智能补全器。

    - ``/`` 开头 → 斜杠命令补全
    - ``/set_model `` 后 → 模型别名补全
    - 其他 → 文件路径补全（当前工作目录）
    """

    def __init__(self, command_names: list[str] | None = None) -> None:
        self._command_completer = WordCompleter(
            command_names or [],
            ignore_case=True,
            sentence=True,
            meta_dict={}  # 可选：添加命令描述
        )
        self._path_completer = PathCompleter(
            only_directories=False,
            expanduser=True,
            file_filter=None,  # 不过滤，所有文件都显示
        )
        self._model_aliases: list[str] = []
        # /set_model 的别名
        self._set_model_aliases = frozenset({"/set_model", "/model"})

    def update_commands(self, command_names: list[str]) -> None:
        """更新可用命令列表（动态注册的命令变化时调用）。"""
        self._command_completer = WordCompleter(
            command_names, ignore_case=True, sentence=True,
        )

    def update_models(self, aliases: list[str]) -> None:
        """更新可用模型别名列表。"""
        self._model_aliases = list(aliases)

    def get_completions(self, document: Document, complete_event):
        text = document.text_before_cursor

        # ── 斜杠命令补全 ──
        if text.startswith("/"):
            # /set_model 或 /model 后面补全模型别名
            parts = text.split(maxsplit=1)
            if parts[0] in self._set_model_aliases and len(parts) > 1:
                prefix = parts[1]
                for alias in self._model_aliases:
                    if alias.startswith(prefix) or not prefix:
                        yield Completion(alias, start_position=-len(prefix))
                return

            # 普通斜杠命令补全
            yield from self._command_completer.get_completions(document, complete_event)
            return

        # ── 文件路径补全（非命令输入时） ──
        # 只在文本非空且可能包含路径时触发
        if text.strip():
            yield from self._path_completer.get_completions(document, complete_event)
