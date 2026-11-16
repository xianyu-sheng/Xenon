"""Rich markup escaping: real tags survive, literal brackets survive too.

Rich silently drops any ``[word]`` it cannot resolve as a style.  Command
output carries identifiers in brackets, so both renderers (interactive REPL
and protocol host) must escape what rich does not own.
"""

from __future__ import annotations

from xenon.repl.rich_markup import escape_literal_tags, is_markup_tag


def _render(markup: str) -> str:
    """What rich actually prints for the escaped text."""

    from io import StringIO

    from rich.console import Console

    buffer = StringIO()
    Console(
        file=buffer, width=100, markup=True, color_system=None
    ).print(escape_literal_tags(markup), end="\n")
    return buffer.getvalue()


def test_a_lesson_id_is_text_not_a_tag() -> None:
    line = "  [lsn-3ffd045bb4383d75] preference/active · 应用 1"
    assert escape_literal_tags(line) == "  \\[lsn-3ffd045bb4383d75] preference/active · 应用 1"
    assert _render(line) == line + "\n"
    assert not is_markup_tag("lsn-3ffd045bb4383d75")


def test_fact_ids_and_markdown_links_are_text() -> None:
    assert escape_literal_tags("[fact-40-workspace] ok") == (
        "\\[fact-40-workspace] ok"
    )
    assert escape_literal_tags("[docs](https://example.com)") == (
        "\\[docs](https://example.com)"
    )


def test_real_styles_are_left_as_markup() -> None:
    text = "[bold]状态[/bold]: [cyan]COLD[/cyan] [dim]#67e8f9[/dim]"
    assert escape_literal_tags(text) == text
    for tag in ("bold", "cyan", "dim", "bold #67e8f9", "on blue", "error", "command"):
        assert is_markup_tag(tag), tag


def test_closing_and_special_tags_are_markup() -> None:
    for tag in ("/bold", "/", "link=https://example.com", "@click=open"):
        assert is_markup_tag(tag), tag


def test_escaping_is_idempotent() -> None:
    once = escape_literal_tags("[lsn-abc] x")
    assert once == "\\[lsn-abc] x"
    assert escape_literal_tags(once) == once


def test_prose_without_brackets_is_unchanged() -> None:
    text = "普通文本，含 `code` 和 **bold**"
    assert escape_literal_tags(text) == text
