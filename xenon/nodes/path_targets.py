"""Canonical resolution for tool path arguments.

Why this exists
---------------
Models routinely emit ``~/Desktop/quicksort.py`` or ``桌面/quicksort.py``.
Before this module those strings were joined to the project root verbatim, so
a Windows run created a literal directory named ``~``:

    C:\\Users\\Administrator\\~\\Desktop\\quicksort.py

The fence then accepted it because it was still inside the project root, the
receipt recorded the poisoned path, and the session working memory kept
feeding it back.  Resolution (``~`` / ``%VAR%`` / language-level folders) must
happen *before* the fence, and the fence must never accept a literal ``~``
component again.

The resolver is deliberately pure and side-effect free: path expansion plus
canonicalisation, nothing else.  The path fence keeps owning containment and
sensitive-path checks.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from xenon.nodes.network_security import SecurityError

# Language-level folder words.  Only unambiguous Chinese aliases are mapped;
# an English relative "Desktop/" keeps its normal meaning (project subfolder),
# because a user may legitimately have one of those.
_FOLDER_ALIASES: dict[str, str] = {
    "桌面": "Desktop",
    "我的桌面": "Desktop",
    "桌面目录": "Desktop",
    "下载": "Downloads",
    "我的下载": "Downloads",
    "下载目录": "Downloads",
    "文档": "Documents",
    "我的文档": "Documents",
    "文档目录": "Documents",
    "图片": "Pictures",
    "我的图片": "Pictures",
    "图片目录": "Pictures",
    "音乐": "Music",
    "我的音乐": "Music",
    "音乐目录": "Music",
    "视频": "Videos",
    "我的视频": "Videos",
    "视频目录": "Videos",
}
_HOME_ALIASES = frozenset({"主目录", "家目录", "用户目录", "用户主目录"})
_CWD_ALIASES = frozenset(
    {"项目根", "项目目录", "项目根目录", "当前目录", "工作目录", "当前文件夹", "这里"}
)

_ENV_WINDOWS = re.compile(r"%([^%]+)%")
_ENV_POSIX = re.compile(r"\$\{([^}]+)\}|\$([A-Za-z_][A-Za-z0-9_]*)")
_SPLIT_HEAD = re.compile(r"[\\/]+")


def _path_home() -> Path:
    """Indirection so tests can point ``~`` at a temporary home."""

    return Path.home()


def _expand_environment(value: str) -> str:
    value = _ENV_WINDOWS.sub(
        lambda m: os.environ.get(m.group(1), m.group(0)), value
    )
    return _ENV_POSIX.sub(
        lambda m: os.environ.get(m.group(1) or m.group(2), m.group(0)), value
    )


def _strip_wrapping(value: str) -> str:
    text = (value or "").strip()
    changed = True
    while text and changed:
        changed = False
        for left, right in (("`", "`"), ("'", "'"), ('"', '"')):
            if len(text) >= 2 and text.startswith(left) and text.endswith(right):
                text = text[1:-1].strip()
                changed = True
                break
        # Trailing sentence punctuation the model sometimes appends to a path.
        stripped = text.rstrip("。，,;；").rstrip()
        if stripped != text:
            text = stripped
            changed = True
    return text


def resolve_target_path(
    raw: str,
    *,
    cwd: str | os.PathLike[str] | None = None,
    home: str | os.PathLike[str] | None = None,
) -> Path:
    """Resolve one model-supplied path to a canonical absolute-ish ``Path``.

    Handles tilde home references, environment variables, and language-level
    folder words (桌面/下载/文档/主目录/项目根).  The returned path is
    normalised but **not** symlink-resolved: the fence still owns that.
    """

    value = _strip_wrapping(raw)
    if not value:
        raise SecurityError("文件路径不能为空")
    if "\x00" in value:
        raise SecurityError("文件路径包含非法字符")

    value = _expand_environment(value)
    base_home = Path(home) if home is not None else _path_home()
    base_cwd = Path(cwd) if cwd is not None else None

    if value == "~":
        value = str(base_home)
    elif value.startswith(("~/", "~\\")):
        tail = value[2:]
        if "\\" in tail:
            # ``~\Desktop\x`` 是 Windows 风格引用；在 POSIX 上反斜杠不是
            # 分隔符，统一转换，保证跨平台解析一致。
            tail = tail.replace("\\", "/")
        value = str(base_home / tail)
    else:
        match = _SPLIT_HEAD.search(value)
        if match:
            head = value[: match.start()]
            tail = value[match.end() :]
        else:
            head, tail = value, ""
        if head in _FOLDER_ALIASES:
            folder = base_home / _FOLDER_ALIASES[head]
            value = str(folder / tail) if tail else str(folder)
        elif head in _HOME_ALIASES:
            value = str(base_home / tail) if tail else str(base_home)
        elif head in _CWD_ALIASES:
            if base_cwd is None:
                raise SecurityError(f"无法解析“{head}”：当前没有项目目录")
            value = str(base_cwd / tail) if tail else str(base_cwd)

    path = Path(value)
    if any(part == "~" for part in path.parts):
        raise SecurityError(
            "路径包含字面量 ~ 目录（通常是想写用户主目录）。"
            "请使用 ~/、主目录或完整绝对路径。"
        )

    if base_cwd is not None and not path.is_absolute():
        path = base_cwd / path

    return Path(os.path.normpath(str(path)))
