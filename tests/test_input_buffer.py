"""Correctness coverage for multiline and compact pasted input."""

from __future__ import annotations

from xenon.repl.context_manager import ContextManager
from xenon.repl.input_buffer import PastedTextStore, normalize_input_newlines
from xenon.repl.input_history import InputHistory


def test_normalize_input_newlines_changes_only_line_endings():
    assert normalize_input_newlines(" a\r\nb\rc\n ") == " a\nb\nc\n "


def test_long_paste_compacts_and_expands_losslessly():
    store = PastedTextStore(threshold=10)
    original = "  第一行\r\nsecond\r  "

    visible = store.compact(original)

    assert visible == "[Pasted #1 +15 chars]"
    assert original not in visible
    assert store.expand(visible) == "  第一行\nsecond\n  "


def test_short_and_multiple_pastes_preserve_order():
    store = PastedTextStore(threshold=5)
    assert store.compact("tiny") == "tiny"
    first = store.compact("abcdef")
    second = store.compact("uvwxyz")

    assert store.expand(f"before {first} middle {second} after") == (
        "before abcdef middle uvwxyz after"
    )


def test_paste_token_avoids_existing_visible_text_and_is_atomic():
    store = PastedTextStore(threshold=1)
    occupied = "already [Pasted #1 +3 chars]"
    token = store.compact("abc", occupied_text=occupied)

    assert token == "[Pasted #2 +3 chars]"
    combined = f"x{token}y"
    end = 1 + len(token)
    assert store.token_after_cursor(combined, 1) == token
    assert store.token_before_cursor(combined, end) == token


def test_history_navigates_back_to_the_draft(tmp_path):
    history = InputHistory(tmp_path / "history.txt", limit=10)
    history.add("first")
    history.add("second")

    assert history.previous("typing") == "second"
    assert history.previous("typing") == "first"
    assert history.previous("typing") == "first"
    assert history.next() == "second"
    assert history.next() == "typing"


def test_history_persists_one_line_entries_only(tmp_path):
    path = tmp_path / "history.txt"
    history = InputHistory(path, limit=10)
    history.add("hello")
    history.add("multi\nline")

    assert history.previous("") == "multi\nline"
    reloaded = InputHistory(path, limit=10)

    assert list(reloaded._entries) == ["hello"]
    assert path.read_text(encoding="utf-8").splitlines() == ["hello"]


def test_history_drops_consecutive_duplicates_and_blanks(tmp_path):
    path = tmp_path / "history.txt"
    history = InputHistory(path, limit=10)
    history.add("same")
    history.add("same")
    history.add("   ")

    assert history.previous("") == "same"
    assert path.read_text(encoding="utf-8").splitlines() == ["same"]


def test_history_reads_the_old_prompt_toolkit_format(tmp_path):
    path = tmp_path / "history.txt"
    path.write_text(
        "\n# 2026-01-01 10:00:00\n+first\n\n# 2026-01-01 10:01:00\n+second\n",
        encoding="utf-8",
    )

    history = InputHistory(path, limit=10)

    assert history.previous("") == "second"
    assert history.previous("") == "first"


def test_history_file_is_compacted_when_it_grows(tmp_path):
    path = tmp_path / "history.txt"
    path.write_text("".join(f"line{i}\n" for i in range(100)), encoding="utf-8")

    history = InputHistory(path, limit=10)

    assert history.previous("") == "line99"
    assert len(path.read_text(encoding="utf-8").splitlines()) == 10


def test_multiline_request_is_one_cache_friendly_user_turn():
    manager = ContextManager()
    multiline = "请修改：\n```python\nprint('ok')\n```"

    manager.add_request_message(multiline)

    assert len(manager.history) == 1
    assert manager.history[0].role == "user"
    assert manager.history[0].content == multiline
    assert manager.get_messages() == [{"role": "user", "content": multiline}]
