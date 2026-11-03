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


def test_the_skill_index_truncates_with_a_count(tmp_path: Path, monkeypatch) -> None:
    from xenon.repl import skill_manager as sm
    from xenon.repl.runtime_adapter import _skill_index

    skills = tmp_path / "skills"
    for i in range(45):
        root = skills / f"skill-{i:02d}"
        root.mkdir(parents=True)
        (root / "SKILL.md").write_text(
            f"---\nname: skill-{i:02d}\ndescription: 技能 {i} 的说明\n---\n\n正文\n",
            encoding="utf-8",
        )
    monkeypatch.setattr(sm, "_SKILLS_DIR", skills)

    index = _skill_index(None, limit=40)

    assert "skill-00" in index
    assert "skill-44" not in index
    assert "5 more skills" in index
    assert "SKILL.md" in index  # every row names its progressive read path


def test_the_skill_index_without_skills_is_empty(tmp_path: Path, monkeypatch) -> None:
    from xenon.repl import skill_manager as sm
    from xenon.repl.runtime_adapter import _skill_index

    skills = tmp_path / "empty-skills"
    skills.mkdir()
    # Both roots must be empty: the shared ~/.agents/skills is a real one.
    monkeypatch.setattr(sm, "_SKILLS_DIR", skills)
    monkeypatch.setattr(sm, "_SHARED_SKILLS_DIR", skills)

    assert _skill_index(None) == ""


def test_load_skill_body_enters_reference_context(tmp_path: Path, monkeypatch) -> None:
    """turn_flow's skill branch loads the body lazily, and a missing skill
    is a no-op rather than a crash (#370)."""

    from xenon.repl import skill_manager as sm
    from xenon.repl.turn_flow import TurnFlowMixin

    skills = _skill_dir(tmp_path)
    monkeypatch.setattr(sm, "_SKILLS_DIR", skills)
    monkeypatch.setattr(sm, "_SHARED_SKILLS_DIR", skills)

    flow = object.__new__(TurnFlowMixin)
    flow._active_skill_body = None
    flow._load_skill_body("code-review")

    assert flow._active_skill_body is not None
    assert "## 步骤" in flow._active_skill_body
    assert "[code-review]" in flow._active_skill_body

    # A skill that does not exist leaves the body unset.
    flow._load_skill_body("does-not-exist")
    assert flow._active_skill_body is None


def test_load_skill_body_without_a_name_is_a_noop() -> None:
    from xenon.repl.turn_flow import TurnFlowMixin

    flow = object.__new__(TurnFlowMixin)
    flow._active_skill_body = "stale"
    flow._load_skill_body(None)
    assert flow._active_skill_body is None


def test_load_skill_body_survives_a_broken_skill_manager(tmp_path: Path, monkeypatch) -> None:
    from xenon.repl.turn_flow import TurnFlowMixin

    def boom(*args, **kwargs):
        raise OSError("skill dir unreadable")

    monkeypatch.setattr(
        "xenon.repl.skill_manager.SkillManager", boom
    )
    flow = object.__new__(TurnFlowMixin)
    flow._active_skill_body = None
    flow._load_skill_body("any")
    assert flow._active_skill_body is None


def test_the_skill_index_survives_a_broken_skill_manager(monkeypatch) -> None:
    from xenon.repl.runtime_adapter import _skill_index

    def boom(*args, **kwargs):
        raise OSError("skill dir unreadable")

    monkeypatch.setattr(
        "xenon.repl.skill_manager.SkillManager", boom
    )
    assert _skill_index(None) == ""


def test_a_skill_turn_runs_the_kernel_with_a_lazy_body(
    tmp_path: Path, monkeypatch
) -> None:
    """The real turn_flow skill branch: objective stays short, the body goes
    into reference context, and the turn runs on the kernel (#370)."""

    from xenon.repl.model_registry import ModelRegistry
    from xenon.repl.repl import REPL
    from xenon.repl.turn_contract import build_turn_contract

    monkeypatch.chdir(tmp_path)
    registry = ModelRegistry()
    registry.add_model("test/fake", "fake")
    registry.assign_role("planner", ["fake"])
    repl = REPL(registry=registry, streaming=False, optimize_prompts=False)
    contract = build_turn_contract("用 code-review 技能审查")
    monkeypatch.setattr(
        repl,
        "_resolve_turn_contract",
        lambda *args, **kwargs: (
            contract,
            contract.to_execution_policy(),
            contract.intent,
            None,
        ),
    )
    monkeypatch.setattr(repl, "_has_mcp_tools", lambda: False)
    calls: list[dict] = []
    monkeypatch.setattr(
        "xenon.repl.runtime_adapter.run_kernel_turn",
        lambda *args, **kwargs: calls.append(kwargs),
    )

    repl._handle_chat("用 code-review 技能审查", skill_name="code-review")

    assert calls, "a skill turn must run on the kernel"
    objective = calls[0].get("objective") or ""
    assert "code-review" in objective
    assert "## 步骤" not in objective, "the body must not be inlined"
    assert "SKILL.md" not in objective
