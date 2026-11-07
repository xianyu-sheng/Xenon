"""What the workspace actually looks like, told to the model before it guesses.

A real SmartBench run failed its last third because it had to *guess* where the
tests lived.  It wrote ``tests/test_resource_leak_regression.py``, the tool said
the path did not exist, it guessed a second name, that failed too, and it gave
up.  The information it needed -- "this repo keeps tests in ``test/``, not
``tests/``" -- was on disk the whole time.  Claude Code and Pi put a directory
listing in the model's context; Xenon did not.

This module produces that listing deterministically.  It never reads file
contents and it never asks a model: a layout is a fact about the worktree, and
the point of showing it is to replace a guess with an observation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

#: Directory names that are never part of "what this project looks like".
_SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        ".venv",
        "venv",
        "env",
        "node_modules",
        "build",
        "dist",
        ".eggs",
        ".idea",
        ".vscode",
        ".xenon",
    }
)

#: A test directory by any of the names projects actually use.  The list is
#: deliberately structural ("a directory that holds tests") rather than a
#: closed set of project names.
_TEST_DIR_NAMES = frozenset({"test", "tests", "spec", "specs", "__tests__"})

_MAX_FILES_PER_DIR = 14
#: Directories the task actually names (and test directories) are listed in
#: full.  The model needs the naming convention of the place it must add a
#: file to; showing it 14 of 60 tests invents the other 46.
_MAX_FILES_IN_FOCUS_DIR = 60
_MAX_DIRS = 30
_MAX_DEPTH = 3

_MANIFEST_NAMES = frozenset(
    {
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "package.json",
        "go.mod",
        "Cargo.toml",
        "Makefile",
        "tox.ini",
        "pytest.ini",
    }
)


def _focus_directories(paths: Sequence[str]) -> set[str]:
    """Every directory on the path to a file the task named.

    Not just the immediate parent: ordering by "how deep is the target" is what
    makes ``smartbench/core/rules`` win over its siblings ``smartbench/analysis``
    and company, otherwise the target package is the one thing a clipped walk
    drops.
    """

    found: set[str] = set()
    for raw in paths:
        text = str(raw or "").replace("\\", "/").strip().lstrip("./")
        if not text:
            continue
        parts = text.split("/")
        for index in range(1, len(parts)):
            found.add("/".join(parts[:index]))
        found.add("/".join(parts[:-1]) if len(parts) > 1 else "")
    return found


def _focus_depth(relative: str, focus_dirs: set[str]) -> int:
    """How far ``relative`` is along a path to a named file (0 = not on one)."""

    best = 0
    for prefix in focus_dirs:
        if not prefix:
            continue
        if prefix == relative or prefix.startswith(relative + "/"):
            best = max(best, relative.count("/") + (1 if relative else 0))
    return best


def _is_focused_subdir(relative: str, name: str, focus_dirs: set[str]) -> bool:
    candidate = f"{relative}/{name}" if relative else name
    return any(
        prefix == candidate or prefix.startswith(candidate + "/")
        for prefix in focus_dirs
        if prefix
    )


def _is_test_dir(relative: str) -> bool:
    if not relative:
        return False
    return relative.rsplit("/", 1)[-1].lower() in _TEST_DIR_NAMES


@dataclass(frozen=True, slots=True)
class DirectoryEntry:
    path: str
    files: tuple[str, ...]
    dirs: tuple[str, ...]
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class WorkspaceLayout:
    """A bounded, deterministic view of the tree the task runs against."""

    root: str
    entries: tuple[DirectoryEntry, ...] = ()
    test_dirs: tuple[str, ...] = ()
    test_files: tuple[str, ...] = ()
    manifests: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    #: True when the listing hit a bound, so a reader knows absence is not
    #: proof the thing is missing.
    truncated: bool = False

    def render(self, *, max_chars: int = 1800) -> str:
        """A compact block for the system prompt."""

        if not self.entries:
            return ""
        lines = [
            "Workspace layout (observed, not guessed — prefer these paths "
            "over inventing new ones):"
        ]
        for entry in self.entries:
            label = entry.path or "."
            children = [f"{name}/" for name in entry.dirs]
            children.extend(entry.files)
            if not children:
                continue
            suffix = " …" if entry.truncated else ""
            lines.append(f"  {label}: {', '.join(children)}{suffix}")
        if self.test_dirs:
            lines.append(
                "  test directories: " + ", ".join(self.test_dirs)
            )
        if self.test_files:
            shown = ", ".join(self.test_files[:12])
            more = len(self.test_files) - 12
            lines.append(
                "  existing test files: " + shown + (f" (+{more} more)" if more > 0 else "")
            )
        if self.manifests:
            lines.append("  manifests: " + ", ".join(self.manifests))
        for note in self.notes:
            lines.append(f"  note: {note}")
        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[:max_chars].rstrip() + "\n  … (layout truncated)"
        return text


def _is_test_file(name: str) -> bool:
    lowered = name.lower()
    if not lowered.endswith((".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java", ".rb")):
        return False
    stem = lowered.rsplit(".", 1)[0]
    return (
        stem.startswith("test_")
        or stem.endswith("_test")
        or stem.endswith(".test")
        or stem.endswith(".spec")
        or stem == "conftest"
    )


def _visible_names(
    directory: Path, *, max_files: int = _MAX_FILES_PER_DIR
) -> tuple[list[str], list[str], bool]:
    files: list[str] = []
    dirs: list[str] = []
    truncated = False
    try:
        with os.scandir(directory) as stream:
            for item in stream:
                name = item.name
                try:
                    if item.is_dir(follow_symlinks=False):
                        if name in _SKIP_DIRS or name.startswith("."):
                            continue
                        dirs.append(name)
                    else:
                        if name.startswith(".") or name in {"Thumbs.db"}:
                            continue
                        files.append(name)
                except OSError:
                    continue
    except OSError:
        return [], [], False
    dirs.sort()
    files.sort()
    if len(files) > max_files:
        files = files[:max_files]
        truncated = True
    return files, dirs, truncated


def describe_workspace(
    workspace: str | Path,
    *,
    max_dirs: int = _MAX_DIRS,
    max_depth: int = _MAX_DEPTH,
    focus_paths: Sequence[str] = (),
) -> WorkspaceLayout:
    """Walk the workspace breadth-first, within fixed bounds.

    Breadth-first on purpose: the directories nearest the root are the ones a
    model needs to orient itself, and a depth-first walk would spend the whole
    budget inside ``docs/`` before ever reaching ``src/``.

    ``focus_paths`` are the files the task named.  Their directories -- and any
    test directory -- are listed in full and walked first, because those are
    exactly the places the model is about to add files to.  Showing 14 of 60
    test names and none of the target package was how a model ended up
    inventing ``tests/test_resource_leak_regression.py`` and
    ``smartbench/core/rules/registry.py``: both directories were clipped.
    """

    root = Path(workspace).expanduser()
    try:
        root = root.resolve()
    except OSError:
        pass
    if not root.is_dir():
        return WorkspaceLayout(root=str(root), notes=("工作区不可读",))

    focus_dirs = _focus_directories(focus_paths)
    entries: list[DirectoryEntry] = []
    test_dirs: list[str] = []
    test_files: list[str] = []
    manifests: list[str] = []
    truncated = False

    queue: list[tuple[Path, str, int]] = [(root, "", 0)]
    while queue:
        # A priority queue, not a plain BFS: a level with many siblings can
        # exhaust the directory budget before the walk ever turns down the
        # branch the task named.  Focused branches go first; breadth-first is
        # still the tie-break, so the top of the tree is described first.
        queue.sort(
            key=lambda item: (
                -item[2],
                -_focus_depth(item[1], focus_dirs),
                0 if _is_test_dir(item[1]) else 1,
            )
        )
        directory, relative, depth = queue.pop(0)
        if len(entries) >= max_dirs:
            truncated = True
            break
        in_focus = (
            relative in focus_dirs
            or any(
                relative == prefix or relative.startswith(prefix + "/")
                for prefix in focus_dirs
            )
            or _is_test_dir(relative)
        )
        files, dirs, cut = _visible_names(
            directory,
            max_files=_MAX_FILES_IN_FOCUS_DIR if in_focus else _MAX_FILES_PER_DIR,
        )
        if cut:
            truncated = True
        entries.append(
            DirectoryEntry(path=relative, files=tuple(files), dirs=tuple(dirs), truncated=cut)
        )
        if _is_test_dir(relative):
            test_dirs.append(relative)
        for name in files:
            if _is_test_file(name):
                test_files.append(f"{relative}/{name}" if relative else name)
            elif name in _MANIFEST_NAMES:
                manifests.append(f"{relative}/{name}" if relative else name)
        if depth < max_depth:
            # Focused subtrees are visited before the rest of the level, and
            # deeper targets before shallower siblings: a task naming
            # ``a/b/c.py`` needs ``a/b`` reached before the budget runs out on
            # ``a/x``, ``a/y``, ...
            ordered = sorted(
                dirs,
                key=lambda name: (
                    0 if _is_focused_subdir(relative, name, focus_dirs) else 1,
                    -_focus_depth(
                        f"{relative}/{name}" if relative else name, focus_dirs
                    ),
                    0 if name.lower() in _TEST_DIR_NAMES else 1,
                    name,
                ),
            )
            for name in ordered:
                queue.append(
                    (directory / name, f"{relative}/{name}" if relative else name, depth + 1)
                )

    notes: list[str] = []
    if not test_dirs and not test_files:
        # Absence is only meaningful when the walk was complete.
        if not truncated:
            notes.append("没有发现测试目录或测试文件；不要假设某个测试路径存在")
    if truncated:
        notes.append("目录列表已截断，未列出的路径也可能存在")

    return WorkspaceLayout(
        root=str(root),
        entries=tuple(entries),
        test_dirs=tuple(test_dirs),
        test_files=tuple(test_files),
        manifests=tuple(manifests),
        notes=tuple(notes),
        truncated=truncated,
    )


__all__ = ["DirectoryEntry", "WorkspaceLayout", "describe_workspace"]
