"""One credentials reader/writer: encrypted at rest, one format."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from xenon.utils import credentials_io


def test_a_saved_document_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "credentials.yaml"
    document = {"openai": "sk-openai", "providers": {"x": {"api_key": "k"}}}

    cipher = credentials_io.save_document(path, document)

    assert credentials_io.load_document(path) == document
    assert path.read_text(encoding="utf-8").startswith(credentials_io.MAGIC)
    described = credentials_io.describe(path)
    assert described["cipher"] == cipher
    if sys.platform == "win32":
        assert described["encrypted"] is True


def test_plaintext_yaml_is_still_read_and_migrates_on_windows(
    tmp_path: Path,
) -> None:
    path = tmp_path / "credentials.yaml"
    path.write_text("openai: sk-plain\n", encoding="utf-8")

    document = credentials_io.load_document(path)

    assert document == {"openai": "sk-plain"}
    if sys.platform == "win32":
        # DPAPI is available: reading rewrote it encrypted, and the secret is
        # no longer sitting in the file as plaintext.
        assert credentials_io.describe(path)["encrypted"] is True
        assert "sk-plain" not in path.read_text(encoding="utf-8")
        assert not list(tmp_path.glob("*.bak"))


def test_a_missing_file_is_an_empty_document(tmp_path: Path) -> None:
    assert credentials_io.load_document(tmp_path / "nope.yaml") == {}


def test_corrupt_ciphertext_does_not_raise(tmp_path: Path) -> None:
    path = tmp_path / "credentials.yaml"
    path.write_text(
        f"{credentials_io.MAGIC}dpapi/v1\nnot-base64!!\n", encoding="utf-8"
    )

    assert credentials_io.load_document(path) == {}


def test_the_path_can_be_redirected(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XENON_CREDENTIALS_PATH", str(tmp_path / "alt.yaml"))

    assert credentials_io.default_credentials_path() == tmp_path / "alt.yaml"


def test_describe_reports_missing_without_claiming_encryption(
    tmp_path: Path,
) -> None:
    described = credentials_io.describe(tmp_path / "absent.yaml")

    assert described["cipher"] == "missing"
    assert described["encrypted"] is False


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")
def test_dpapi_ciphertext_is_not_the_plaintext(tmp_path: Path) -> None:
    path = tmp_path / "credentials.yaml"

    credentials_io.save_document(path, {"k": "super-secret-value"})

    assert "super-secret-value" not in path.read_text(encoding="utf-8")
