"""Correctness coverage for multiline and compact pasted input."""

from __future__ import annotations

from types import SimpleNamespace

from prompt_toolkit.buffer import Buffer
from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
from prompt_toolkit.keys import Keys

from xenon.repl.context_manager import ContextManager
from xenon.repl.input_buffer import PastedTextStore, normalize_input_newlines
from xenon.repl.repl import REPL


def _binding(repl: REPL, keys: tuple[Keys, ...]):
    return next(
        binding
        for binding in repl._pt_session.key_bindings.bindings
        if binding.keys == keys
    )


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


def test_prompt_toolkit_paste_binding_folds_only_visible_buffer():
    repl = REPL()
    repl._paste_store.reset()
    buffer = Buffer()
    payload = "开头\r\n" + "x" * 1200 + "\r结尾"
    event = SimpleNamespace(current_buffer=buffer, data=payload)

    _binding(repl, (Keys.BracketedPaste,)).handler(event)

    assert buffer.text == "[Pasted #1 +1,206 chars]"
    assert repl._paste_store.expand(buffer.text) == normalize_input_newlines(payload)


def test_prompt_toolkit_folded_paste_backspace_deletes_whole_block():
    repl = REPL()
    repl._paste_store.reset()
    buffer = Buffer()
    token = repl._paste_store.compact("x" * 1200)
    buffer.text = f"prefix{token}"
    buffer.cursor_position = len(buffer.text)

    event = SimpleNamespace(current_buffer=buffer)
    _binding(repl, (Keys.Backspace,)).handler(event)

    assert buffer.text == "prefix"


def test_shift_enter_encodings_remain_distinct_from_plain_enter():
    repl = REPL()

    expected = (Keys.Escape, Keys.ControlM)
    assert ANSI_SEQUENCES["\x1b[13;2u"] == expected
    assert ANSI_SEQUENCES["\x1b[27;2;13~"] == expected
    assert ANSI_SEQUENCES["\r"] == Keys.ControlM

    buffer = Buffer()
    event = SimpleNamespace(current_buffer=buffer)
    _binding(repl, expected).handler(event)
    assert buffer.text == "\n"


def test_read_input_restores_paste_before_returning_to_repl(monkeypatch):
    repl = REPL()
    payload = "  def f():\r\n    return 1\r\n"
    payload += "#" * 1200

    def fake_prompt(_message):
        return repl._paste_store.compact(payload)

    monkeypatch.setattr(repl._pt_session, "prompt", fake_prompt)

    assert repl._read_input_pt() == normalize_input_newlines(payload)


def test_multiline_request_is_one_cache_friendly_user_turn():
    manager = ContextManager()
    multiline = "请修改：\n```python\nprint('ok')\n```"

    manager.add_request_message(multiline)

    assert len(manager.history) == 1
    assert manager.history[0].role == "user"
    assert manager.history[0].content == multiline
    assert manager.get_messages() == [{"role": "user", "content": multiline}]
