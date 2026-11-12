"""Model API keys live in the encrypted credentials document, not models.yaml."""

from __future__ import annotations

from pathlib import Path

from xenon.utils.credentials_io import load_document


def test_saving_models_moves_keys_into_credentials(
    monkeypatch, tmp_path: Path
) -> None:
    from xenon.repl.model_registry import ModelRegistry

    credentials = tmp_path / "credentials.yaml"
    monkeypatch.setenv("XENON_CREDENTIALS_PATH", str(credentials))

    registry = ModelRegistry()
    registry.add_model("deepseek/deepseek-chat", alias="ds", api_key="sk-model-1")
    models = tmp_path / "models.yaml"
    registry.save_to_file(models)

    assert "sk-model-1" not in models.read_text(encoding="utf-8")
    document = load_document(credentials)
    assert document["_model_keys"]["ds"] == "sk-model-1"

    fresh = ModelRegistry()
    fresh.load_from_file(models)

    assert fresh.models["ds"].api_key == "sk-model-1"
