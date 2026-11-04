"""The command palette (#183): one catalog, searchable, recency-ordered.

Acceptance this file locks:

1. every registered command appears — and a newly registered one appears too
   (the palette is derived, never a second hand-written menu);
2. rows carry group + parameter hint (the meta column), plain text only;
3. fuzzy search ranks a name prefix above a description hit;
4. recents float to the top when the query is empty, without duplicating;
5. the palette closes once the user starts typing arguments;
6. model-alias completion still works (regression);
7. no PTY / no colour: nothing here needs a terminal.
"""

from __future__ import annotations

import json
from pathlib import Path

from prompt_toolkit.document import Document

from xenon.repl.command_catalog import (
    GROUP_ORDER,
    CommandEntry,
    CommandRecents,
    build_catalog,
    group_for,
    rank,
    score,
)
from xenon.repl.commands import COMMANDS
from xenon.repl.completer import OmniCompleter


def _completer(tmp_path: Path | None = None) -> OmniCompleter:
    recents = (
        CommandRecents(tmp_path / "recents.json") if tmp_path is not None else None
    )
    return OmniCompleter(list(COMMANDS), recents=recents)


# ── 1. derived, complete, auto-extending ─────────────────────────


def test_the_palette_covers_every_registered_command() -> None:
    names = {entry.name for entry in build_catalog()}
    assert names == set(COMMANDS)


def test_a_newly_registered_command_appears_automatically() -> None:
    fake = {
        "/existing": {"description": "already here", "usage": "/existing"},
        "/brand-new": {"description": "just added", "usage": "/brand-new [x]"},
    }
    names = [entry.name for entry in build_catalog(fake)]
    assert "/brand-new" in names


def test_update_commands_refreshes_the_palette() -> None:
    completer = OmniCompleter(["/a"])
    before = {entry.name for entry in completer.catalog}
    completer.update_commands(list(COMMANDS))
    after = {entry.name for entry in completer.catalog}
    assert after == set(COMMANDS)
    assert after != before


# ── 2. rows carry group + parameter hint ─────────────────────────


def test_group_assignment_is_longest_prefix_first() -> None:
    assert group_for("/plan") == "规划"
    assert group_for("/permissions") == "权限"
    assert group_for("/sub-agent") == "工具"
    assert group_for("/totally-new") == "其他"


def test_every_group_is_a_known_group_or_the_fallback() -> None:
    groups = {entry.group for entry in build_catalog()}
    assert groups <= set(GROUP_ORDER)


def test_the_meta_column_carries_group_and_param_hint() -> None:
    entry = CommandEntry(
        name="/plan",
        description="plan mode",
        usage="/plan [on|off|status|approve|reject]",
        group="规划",
    )
    assert entry.param_hint == "[on|off|status|approve|reject]"
    assert entry.palette_meta() == "规划 · [on|off|status|approve|reject]"


def test_a_command_without_params_has_no_hint() -> None:
    entry = CommandEntry(name="/exit", description="bye", usage="/exit", group="会话")
    assert entry.param_hint == ""
    assert entry.palette_meta() == "会话"


def test_meta_is_plain_text() -> None:
    """No colour codes anywhere: the colum must survive NO_COLOR."""
    for entry in build_catalog():
        assert "\x1b" not in entry.palette_meta()
        assert len(entry.palette_meta()) <= 60


# ── 3. fuzzy search ─────────────────────────────────────────────


def test_a_name_prefix_beats_a_description_hit() -> None:
    by_name = CommandEntry(name="/permissions", description="x", group="权限")
    by_desc = CommandEntry(
        name="/mode", description="permissions helper", group="模型"
    )
    assert score(by_name, "perm") > score(by_desc, "perm")


def test_a_subsequence_still_matches() -> None:
    entry = CommandEntry(name="/permissions", description="", group="权限")
    assert score(entry, "prm") > 0


def test_an_unrelated_query_matches_nothing() -> None:
    entry = CommandEntry(name="/exit", description="bye", group="会话")
    assert score(entry, "zzzz") == 0


def test_ranking_drops_non_matches() -> None:
    rows = rank(build_catalog(), "perm")
    assert [row.name for row in rows] == ["/permissions"]


# ── 4. recents ──────────────────────────────────────────────────


def test_recents_float_to_the_top_when_idle(tmp_path: Path) -> None:
    completer = _completer(tmp_path)
    completer.record_use("/plan")

    first = completer.palette("/")[0]
    assert first.name == "/plan"


def test_recents_do_not_duplicate_rows(tmp_path: Path) -> None:
    completer = _completer(tmp_path)
    completer.record_use("/plan")
    completer.record_use("/plan")

    names = [entry.name for entry in completer.palette("/")]
    assert names.count("/plan") == 1
    assert len(names) == len(set(names))


def test_recents_survive_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "recents.json"
    CommandRecents(path).record("/evidence")

    again = CommandRecents(path)

    assert again.names[0] == "/evidence"
    assert json.loads(path.read_text(encoding="utf-8"))[0] == "/evidence"


def test_recents_are_capped(tmp_path: Path) -> None:
    recents = CommandRecents(tmp_path / "r.json", limit=3)
    for name in ["/a", "/b", "/c", "/d"]:
        recents.record(name)

    assert recents.names == ("/d", "/c", "/b")


def test_a_broken_recents_file_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "recents.json"
    path.write_text("{not json", encoding="utf-8")

    assert CommandRecents(path).names == ()


def test_an_unwritable_recents_path_never_raises(tmp_path: Path) -> None:
    target = tmp_path / "dir.json"
    target.mkdir()
    recents = CommandRecents(target)  # a directory: write must fail quietly
    recents.record("/plan")
    assert recents.names == ("/plan",)


# ── 5. the palette closes on arguments ──────────────────────────


def test_the_palette_closes_once_arguments_start() -> None:
    completer = _completer()
    assert completer.palette("/plan ") == ()
    assert list(completer.get_completions(Document("/plan on"), None)) == []


def test_typing_slash_offers_rows_with_meta() -> None:
    completer = _completer()
    completions = list(completer.get_completions(Document("/"), None))

    assert len(completions) == len(COMMANDS)
    assert all(item.display_meta_text for item in completions)


# ── 6. model alias completion still works ───────────────────────


def test_model_aliases_still_complete_after_the_command() -> None:
    completer = _completer()
    completer.update_models(["deepseek-v4-pro", "deepseek-v4-flash"])

    completions = list(completer.get_completions(Document("/model deep"), None))

    assert [item.text for item in completions] == [
        "deepseek-v4-pro",
        "deepseek-v4-flash",
    ]


# ── 7. no terminal required ─────────────────────────────────────


def test_the_palette_needs_no_terminal() -> None:
    """Everything the palette computes is plain data — no PTY, no colour."""

    completer = _completer()
    rows = completer.palette("/")

    assert rows
    assert all("\x1b" not in row.palette_meta() for row in rows)


# ── Enter runs the highlighted row ───────────────────────────────


def test_enter_runs_the_first_row_when_nothing_is_highlighted() -> None:
    from xenon.repl.command_catalog import enter_applies

    assert enter_applies("/perm", ["/permissions"], None) == "/permissions"


def test_enter_runs_the_highlighted_row() -> None:
    from xenon.repl.command_catalog import enter_applies

    assert enter_applies("/", ["/clear", "/plan", "/help"], 1) == "/plan"


def test_enter_does_not_rewrite_arguments() -> None:
    """Once the user types an argument the palette is closed: submit as typed."""
    from xenon.repl.command_catalog import enter_applies

    assert enter_applies("/plan on", ["/plan"], 0) is None


def test_enter_does_not_rewrite_file_paths() -> None:
    from xenon.repl.command_catalog import enter_applies

    assert enter_applies("src/main", ["src/main.py"], 0) is None


def test_enter_without_completions_submits_as_typed() -> None:
    from xenon.repl.command_catalog import enter_applies

    assert enter_applies("/nope", [], None) is None


# ── the extracted seams (so the key binding is one line) ─────────


class _FakeState:
    def __init__(self, texts: list[str], index: int | None = None) -> None:
        self.completions = [type("C", (), {"text": text})() for text in texts]
        self.complete_index = index


class _FakeBuffer:
    def __init__(self, text: str, state=None) -> None:
        self.document = type("D", (), {"text_before_cursor": text})()
        self.complete_state = state
        self.text = text
        self.cursor_position = len(text)
        self.submitted = False

    def validate_and_handle(self) -> None:
        self.submitted = True


def test_apply_enter_fills_the_row_then_submits() -> None:
    from xenon.repl.command_catalog import apply_palette_enter

    buffer = _FakeBuffer("/perm", _FakeState(["/permissions"]))
    apply_palette_enter(buffer)

    assert buffer.text == "/permissions"
    assert buffer.submitted is True


def test_apply_enter_keeps_the_typed_text_when_the_palette_is_closed() -> None:
    from xenon.repl.command_catalog import apply_palette_enter

    buffer = _FakeBuffer("/plan on", _FakeState(["/plan"]))
    apply_palette_enter(buffer)

    assert buffer.text == "/plan on"
    assert buffer.submitted is True


def test_record_use_never_raises() -> None:
    from xenon.repl.command_catalog import record_use

    class Boom:
        def record_use(self, name):
            raise RuntimeError("boom")

    record_use(Boom(), "/plan")   # must not raise
    record_use(None, "/plan")     # no completer at all
    record_use(object(), "/plan")  # no hook


# ── edge cases that the palette hits in real use ────────────────


def test_a_usage_without_brackets_has_no_hint() -> None:
    entry = CommandEntry(name="/x", description="d", usage="/x --flag", group="其他")
    assert entry.param_hint == ""


def test_an_unclosed_bracket_is_still_shown() -> None:
    entry = CommandEntry(name="/x", description="d", usage="/x [a|b", group="其他")
    assert entry.param_hint == "[a|b"


def test_an_exact_name_scores_highest() -> None:
    entry = CommandEntry(name="/plan", description="", group="规划")
    assert score(entry, "plan") == 1000


def test_an_empty_query_scores_everything() -> None:
    entry = CommandEntry(name="/plan", description="", group="规划")
    assert score(entry, "   ") == 1


def test_ranking_orders_multiple_matches_by_score() -> None:
    rows = rank(build_catalog(), "p")
    assert len(rows) > 1
    assert rows[0].name.casefold().lstrip("/").startswith("p")


def test_a_completer_without_arguments_uses_the_registry() -> None:
    completer = OmniCompleter()
    assert {entry.name for entry in completer.catalog} == set(COMMANDS)


def test_recents_without_a_path_are_in_memory_only() -> None:
    recents = CommandRecents(None)
    recents.record("/plan")
    assert recents.names == ("/plan",)


def test_an_empty_name_is_not_recorded() -> None:
    recents = CommandRecents(None)
    recents.record("")
    assert recents.names == ()
