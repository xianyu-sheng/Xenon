"""The interactive entry: TS is the only default UI, a missing build is an error.

There is deliberately no automatic fallback: silently starting a different UI
is the behaviour these tests pin down as removed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from xenon import main as xenon_main


def _args(**overrides):
    base = {"ui": None, "resume": None, "model": []}
    base.update(overrides)
    return argparse.Namespace(**base)


def test_auto_mode_errors_when_node_is_missing(monkeypatch, capsys):
    monkeypatch.setattr(xenon_main.shutil, "which", lambda _name: None)

    code = xenon_main._launch_ts(_args())

    assert code == 1
    out = capsys.readouterr().out
    assert "node" in out
    assert "npm run build" in out
    assert "--ui python" in out


def test_auto_mode_errors_when_the_build_is_missing(monkeypatch, capsys):
    monkeypatch.setattr(xenon_main.shutil, "which", lambda _name: "node")
    monkeypatch.setattr(xenon_main, "_ts_frontend_script", lambda: None)

    code = xenon_main._launch_ts(_args())

    assert code == 1
    assert "frontend/dist/cli.js" in capsys.readouterr().out


def test_explicit_python_mode_never_needs_node(monkeypatch):
    monkeypatch.setattr(xenon_main.shutil, "which", lambda _name: None)

    assert xenon_main._launch_ts(_args(ui="python")) is None


def test_piped_input_is_an_error_not_a_silent_downgrade(monkeypatch, capsys):
    monkeypatch.setattr(xenon_main.shutil, "which", lambda _name: "node")
    monkeypatch.setattr(
        xenon_main, "_ts_frontend_script", lambda: Path("cli.js")
    )
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)

    code = xenon_main._launch_ts(_args())

    assert code == 1
    out = capsys.readouterr().out
    assert "xenon exec" in out


def test_launch_passes_resume_and_models_to_the_front_end(monkeypatch, tmp_path):
    script = tmp_path / "cli.js"
    script.write_text("", encoding="utf-8")
    monkeypatch.setattr(xenon_main, "_ts_frontend_script", lambda: script)
    monkeypatch.setattr(xenon_main.shutil, "which", lambda _name: "node")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)

    seen: list[list[str]] = []
    monkeypatch.setattr(
        xenon_main.subprocess, "call", lambda argv: seen.append(argv) or 0
    )

    code = xenon_main._launch_ts(_args(resume="saved-session", model=["m1"]))

    assert code == 0
    argv = seen[0]
    assert argv[argv.index("--resume") + 1] == "saved-session"
    assert argv[argv.index("--model") + 1] == "m1"
