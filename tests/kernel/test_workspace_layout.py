"""The model should navigate the workspace, not guess at it.

A real SmartBench run lost its final third to this: it wrote
``tests/test_resource_leak_regression.py``, the tool answered "path does not
exist", it tried another name, that failed too, and it abandoned the step.  The
repo kept its tests in ``tests/`` the whole time.  Claude Code and Pi put a
listing in context; Xenon only said "Workspace: D:\\SmartBench".

These tests pin that the listing is produced deterministically, bounded, and
recorded as a fact the model's view is built from.
"""

from __future__ import annotations

from pathlib import Path

from xenon.kernel.contracts import TaskMode, TaskSpec
from xenon.kernel.view import build_model_view
from xenon.kernel.workspace import describe_workspace
from xenon.ledger.events import EventKind, EventSource
from xenon.ledger.store import Ledger


def _make_project(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("SECRET_BODY", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text("SECRET_BODY", encoding="utf-8")
    (tmp_path / "tests" / "conftest.py").write_text("SECRET_BODY", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("SECRET_BODY", encoding="utf-8")
    return tmp_path


def test_the_layout_names_the_real_test_directory(tmp_path: Path) -> None:
    layout = describe_workspace(_make_project(tmp_path))

    assert layout.test_dirs == ("tests",)
    assert "tests/test_app.py" in layout.test_files
    assert any(item.endswith("pyproject.toml") for item in layout.manifests)


def test_the_layout_lists_directories_without_reading_contents(tmp_path: Path) -> None:
    layout = describe_workspace(_make_project(tmp_path))

    root = next(item for item in layout.entries if item.path == "")
    assert "src" in root.dirs
    assert "tests" in root.dirs
    # A listing is names only; file bodies never enter the view.
    assert "SECRET_BODY" not in layout.render()


def test_vcs_and_cache_directories_stay_out_of_the_listing(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("x", encoding="utf-8")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "a.pyc").write_text("x", encoding="utf-8")
    (tmp_path / "keep.py").write_text("x", encoding="utf-8")

    layout = describe_workspace(tmp_path)
    text = layout.render()

    assert ".git" not in text
    assert "__pycache__" not in text
    assert "keep.py" in text


def test_a_missing_test_directory_is_stated_only_when_the_walk_was_complete(
    tmp_path: Path,
) -> None:
    (tmp_path / "main.py").write_text("x", encoding="utf-8")

    layout = describe_workspace(tmp_path)

    assert not layout.truncated
    assert any("不要假设" in note for note in layout.notes)


def test_the_listing_is_bounded(tmp_path: Path) -> None:
    for index in range(80):
        (tmp_path / f"f{index}.py").write_text("x", encoding="utf-8")

    layout = describe_workspace(tmp_path, max_dirs=2)

    assert len(layout.entries) <= 2 or layout.truncated


def test_the_view_carries_the_recorded_layout(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    spec = TaskSpec(task_id="t1", objective="fix app", workspace=str(project))
    ledger = Ledger.in_memory("t1")
    ledger.append(
        EventKind.TASK_STARTED,
        EventSource.RUNTIME,
        {"task_id": "t1", "objective": "fix app", "workspace": str(project)},
    )
    ledger.append(
        EventKind.WORKSPACE_LAYOUT,
        EventSource.RUNTIME,
        {
            "root": str(project),
            "rendered": describe_workspace(project).render(),
            "test_dirs": ["tests"],
            "test_files": ["tests/test_app.py"],
        },
    )

    class Catalog:
        def specs(self, profile: str = "core"):
            return ()

    view = build_model_view(spec=spec, catalog=Catalog(), ledger=ledger)

    assert "Workspace layout" in view.system
    assert "tests/test_app.py" in view.system


def test_a_view_without_a_recorded_layout_does_not_probe_on_its_own(
    tmp_path: Path,
) -> None:
    """Replay must answer "what did the model see?", not "what is on disk now"."""

    project = _make_project(tmp_path)
    spec = TaskSpec(task_id="t1", objective="fix app", workspace=str(project))
    ledger = Ledger.in_memory("t1")
    ledger.append(
        EventKind.TASK_STARTED,
        EventSource.RUNTIME,
        {"task_id": "t1", "objective": "fix app", "workspace": str(project)},
    )

    class Catalog:
        def specs(self, profile: str = "core"):
            return ()

    view = build_model_view(spec=spec, catalog=Catalog(), ledger=ledger)

    assert "Workspace layout" not in view.system


def test_a_chat_task_records_no_layout(tmp_path: Path) -> None:
    from xenon.kernel.controller import VerificationController
    from xenon.kernel.runtime import LoopRuntime

    class Catalog:
        def specs(self, profile: str = "core"):
            return ()

    class Gateway:
        def execute(self, call, services):
            raise AssertionError("a chat task runs no tools")

    ledger = Ledger.in_memory("chat")
    runtime = LoopRuntime(
        catalog=Catalog(),
        gateway=Gateway(),
        controller=VerificationController(),
        ledger=ledger,
    )
    spec = TaskSpec(
        task_id="chat",
        objective="what is this repo",
        workspace=str(tmp_path),
        mode=TaskMode.CHAT,
    )

    runtime._spec = spec
    runtime._record_workspace_layout(spec)

    assert not ledger.read(EventKind.WORKSPACE_LAYOUT)


def test_the_directory_a_task_names_is_listed_in_full(tmp_path: Path) -> None:
    """A clipped listing is what made a real run invent file names.

    SmartBench kept 9 rule modules; the model saw none of them and wrote
    ``smartbench/core/rules/registry.py``, which does not exist.
    """

    target = tmp_path / "pkg" / "rules"
    target.mkdir(parents=True)
    for name in ("common.py", "base.py", "quality.py", "security.py", "flow.py"):
        (target / name).write_text("x", encoding="utf-8")
    (tmp_path / "pkg" / "other").mkdir()
    for index in range(40):
        (tmp_path / "pkg" / "other" / f"mod{index}.py").write_text("x", encoding="utf-8")

    layout = describe_workspace(
        tmp_path,
        focus_paths=("pkg/rules/common.py",),
        max_dirs=6,
    )
    rendered = layout.render()

    assert "pkg/rules" in [entry.path for entry in layout.entries]
    for name in ("common.py", "base.py", "quality.py", "security.py", "flow.py"):
        assert name in rendered


def test_a_deep_target_survives_a_tight_directory_budget(tmp_path: Path) -> None:
    """Siblings at the same level must not crowd out the named branch."""

    for name in ("alpha", "beta", "gamma", "delta"):
        (tmp_path / "pkg" / name).mkdir(parents=True)
    (tmp_path / "pkg" / "zzz" / "rules").mkdir(parents=True)
    (tmp_path / "pkg" / "zzz" / "rules" / "common.py").write_text("x", encoding="utf-8")

    layout = describe_workspace(
        tmp_path,
        focus_paths=("pkg/zzz/rules/common.py",),
        max_dirs=5,
    )
    paths = [entry.path for entry in layout.entries]

    assert "pkg/zzz/rules" in paths


def test_the_test_directory_keeps_its_naming_convention(tmp_path: Path) -> None:
    """The model needs to see how this project names tests before adding one."""

    tests = tmp_path / "tests"
    tests.mkdir()
    for index in range(30):
        (tests / f"test_module_{index}.py").write_text("x", encoding="utf-8")

    layout = describe_workspace(tmp_path)

    assert len(layout.test_files) > 20, layout.test_files
    assert "test_module_29.py" in layout.render()
