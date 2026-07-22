"""Regression tests for the Star Core terminal startup identity."""

from __future__ import annotations

import io

from xenon.utils import logo


def test_plain_logo_uses_open_star_core_not_hexagon_frame():
    assert "Xe" in logo.LOGO_PLAIN
    assert "✶" in logo.LOGO_PLAIN
    assert "X E N O N" in logo.LOGO_PLAIN
    assert "╭" not in logo.LOGO_PLAIN
    assert "╰" not in logo.LOGO_PLAIN


def test_redirected_logo_has_no_ansi_sequences(monkeypatch):
    output = io.StringIO()
    monkeypatch.setattr(logo.sys, "stdout", output)

    logo.print_logo(animated=True)

    rendered = output.getvalue()
    assert "X E N O N" in rendered
    assert "\x1b[" not in rendered


def test_animation_frame_height_and_constellation_are_stable():
    frame = logo._build_frame(phase=0, core_color=logo.CYAN)

    assert len(frame) == logo._FRAME_HEIGHT
    assert any("Xe" in line for line in frame)
    assert sum("✦" in line for line in frame) >= 1
