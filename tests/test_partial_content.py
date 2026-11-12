"""Partial-content behaviour on an interrupted model call.

This file used to be a script with its own ``TestResults`` scoreboard: a
failing check printed ``FAIL`` and pytest still reported the test as passed,
because nothing raised.  That was proven by inverting ``PartialContent.is_valid``
and watching three assertions fail while the suite stayed green.

Every check here is an assertion, so a regression fails the suite.
"""

from __future__ import annotations

import pytest

from xenon.utils.partial_response import PartialContent, PartialResponseError


def _partial(content: str, **kwargs) -> PartialContent:
    return PartialContent(
        content=content,
        tokens_generated=kwargs.pop("tokens_generated", 25),
        model_id=kwargs.pop("model_id", "test/model"),
        **kwargs,
    )


# ── is_valid: the threshold is inclusive ────────────────────────────


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (99, False),  # one below the threshold
        (100, True),  # exactly at it
        (101, True),  # one above
        (0, False),  # empty
    ],
)
def test_is_valid_is_inclusive_at_the_threshold(size: int, expected: bool) -> None:
    assert _partial("x" * size).is_valid(min_length=100) is expected


def test_a_custom_threshold_is_honoured() -> None:
    partial = _partial("y" * 500)

    assert partial.is_valid(min_length=500) is True
    assert partial.is_valid(min_length=501) is False


# ── length is character count, not byte count ───────────────────────


def test_len_counts_characters_not_encoded_bytes() -> None:
    """A CJK character is one character but three UTF-8 bytes.

    Sizing the buffer by bytes under-fills it for Chinese text, so the check
    has to be on characters.
    """

    text = "中文内容"

    assert len(_partial(text)) == 4
    assert len(text.encode("utf-8")) == 12


def test_size_includes_whitespace_and_newlines() -> None:
    assert len(_partial("a\nb\tc")) == 5


# ── token estimation ────────────────────────────────────────────────


def test_given_token_count_wins_over_estimation() -> None:
    assert _partial("x" * 10, tokens_generated=999).estimate_tokens() == 999


def test_estimation_never_reports_zero() -> None:
    """An empty partial still costs the call that produced it."""

    assert _partial("", tokens_generated=0).estimate_tokens() >= 1


def test_estimation_scales_with_content() -> None:
    short = _partial("word " * 10, tokens_generated=0).estimate_tokens()
    long = _partial("word " * 400, tokens_generated=0).estimate_tokens()

    assert long > short


# ── the exception carries what a continuation needs ─────────────────


def test_the_error_reports_what_was_already_generated() -> None:
    """The message summarizes; the payload stays on ``.partial``.

    Dumping the partial text into the exception message would put a half-written
    answer into logs and error surfaces, so the summary names the size instead.
    """

    partial = _partial("partial answer", finish_reason="network_error")
    error = PartialResponseError(partial, RuntimeError("boom"))
    message = str(error)

    assert error.partial is partial
    assert "network_error" in message
    assert "14" in message, "the message must say how much arrived"
    assert "partial answer" not in message


def test_can_continue_follows_the_same_threshold() -> None:
    assert PartialResponseError(_partial("x" * 99)).can_continue(100) is False
    assert PartialResponseError(_partial("x" * 100)).can_continue(100) is True


def test_metadata_survives_on_the_partial() -> None:
    partial = _partial("abc", metadata={"checkpoint": 3})

    assert partial.metadata["checkpoint"] == 3


def test_a_fresh_partial_gets_a_timestamp() -> None:
    assert _partial("abc").timestamp > 0
