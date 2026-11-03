"""REPL-side progressive loading (#370): skill index in every turn's context,
the active skill's body only when that skill runs, and the skill trigger no
longer inlines the full SKILL.md into the objective.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from xenon.repl.runtime_adapter import session_context_blocks


def _skill_dir(tmp_path: Path) -> Path:
    skills = tmp_path / "skills"
    root = skills / "code-review"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        "---\nname: code-review\ndescription: 按清单审查代码改动\n---\n\n"
        "## 步骤\n1. 读 diff\n2. 逐条过清单\n",
        encoding="utf-8",
    )
    return skills


def _repl(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        _inject_project_context=None,
        _inject_memories=None,
        ctx_mgr=SimpleNamespace(context_layers=lambda: []),
        _active_skill_body=None,
    )


def test_the_skill_index_enters_every_turn(tmp_path: Path, monkeypatch) -> None:
    from xenon.repl import skill_manager as sm

    monkeypatch.setattr(sm, "_SKILLS_DIR", _skill_dir(tmp_path))
    repl = _repl(tmp_path)

    blocks = session_context_blocks(repl, "review the diff")

    labels = [key for key, _content in blocks]
    assert "skills" in labels
    index = dict(blocks)["skills"]
    assert "code-review" in index
    assert "按清单审查代码改动" in index
    assert "SKILL.md" in index  # the progressive channel: read it on demand


def test_the_skill_body_only_enters_when_that_skill_runs(tmp_path: Path) -> None:
    repl = _repl(tmp_path)
    repl._active_skill_body = "[code-review] 目录: /x\n\n## 步骤\n1. 读 diff"

    blocks = dict(session_context_blocks(repl, "review"))

    assert "active_skill" in blocks
    assert "## 步骤" in blocks["active_skill"]
    # Without the flag there is no body, only the index.
    repl._active_skill_body = None
    assert "active_skill" not in dict(session_context_blocks(repl, "review"))


def test_a_missing_active_skill_body_does_not_break_the_turn(tmp_path: Path) -> None:
    repl = _repl(tmp_path)
    repl.ctx_mgr = SimpleNamespace(context_layers=lambda: (_ for _ in ()).throw(OSError))

    blocks = session_context_blocks(repl, "x")  # must not raise

    assert isinstance(blocks, tuple)


def test_skill_trigger_no_longer_inlines_the_body(tmp_path: Path, monkeypatch) -> None:
    """The objective names the skill; the body travels separately (#370)."""

    from xenon.repl import skill_manager as sm
    from xenon.repl.command_groups.skill import _execute_installed_skill

    monkeypatch.setattr(sm, "_SKILLS_DIR", _skill_dir(tmp_path))
    manager = sm.SkillManager()
    calls: list[tuple[str, str, str]] = []

    class _Repl:
        def _handle_chat(self, user_input, *, skill_name, skill_args):
            calls.append((user_input, skill_name, skill_args))

    _execute_installed_skill(
        manager, "code-review", "重点看安全", registry=object(),
        session_state={"_repl": _Repl()},
    )

    assert len(calls) == 1
    user_input, skill_name, skill_args = calls[0]
    assert skill_name == "code-review"
    assert "SKILL.md" not in user_input
    assert "## 步骤" not in user_input
    assert "code-review" in user_input
