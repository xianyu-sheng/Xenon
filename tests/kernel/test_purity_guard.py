"""Tests for the kernel purity guard (the guard itself is real code)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _guard():
    path = ROOT / "scripts" / "check_kernel_purity.py"
    spec = importlib.util.spec_from_file_location("check_kernel_purity", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


guard = _guard()


def test_kernel_tree_is_clean() -> None:
    violations = guard.check_tree(ROOT / "xenon" / "kernel")
    assert violations == [], [item.render(ROOT) for item in violations]


def test_seeded_tool_name_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "bad.py"
    target.write_text('NAME = "write_file"\n', encoding="utf-8")
    violations = guard.check_path(target)
    assert [item.code for item in violations] == ["K003"]


def test_seeded_legacy_import_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "bad.py"
    target.write_text("from xenon.engine.base import BaseEngine\n", encoding="utf-8")
    violations = guard.check_path(target)
    assert [item.code for item in violations] == ["K001"]


def test_seeded_vocabulary_import_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "bad.py"
    target.write_text("from xenon.engine.evidence_gate import WRITE_TOOL_NAMES\n", encoding="utf-8")
    violations = guard.check_path(target)
    assert {item.code for item in violations} == {"K001", "K002"}


def test_seeded_from_xenon_import_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "bad.py"
    target.write_text("from xenon import engine\n", encoding="utf-8")
    violations = guard.check_path(target)
    assert [item.code for item in violations] == ["K001"]


def test_seeded_context_key_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "bad.py"
    target.write_text('KEY = "_tool_profile"\n', encoding="utf-8")
    violations = guard.check_path(target)
    assert [item.code for item in violations] == ["K004"]


def test_seeded_file_copy_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "bad.py"
    target.write_text('import shutil\nshutil.copy("a", "b")\n', encoding="utf-8")
    violations = guard.check_path(target)
    assert [item.code for item in violations] == ["K005"]


def test_seeded_subprocess_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "bad.py"
    target.write_text('import subprocess\nsubprocess.run(["ls"])\n', encoding="utf-8")
    violations = guard.check_path(target)
    assert [item.code for item in violations] == ["K005"]


def test_seeded_open_write_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "bad.py"
    target.write_text('open("a.txt", "w").write("x")\n', encoding="utf-8")
    violations = guard.check_path(target)
    assert [item.code for item in violations] == ["K005"]


def test_seeded_path_open_write_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "bad.py"
    target.write_text('from pathlib import Path\nPath("a").open("w")\n', encoding="utf-8")
    violations = guard.check_path(target)
    assert [item.code for item in violations] == ["K005"]
