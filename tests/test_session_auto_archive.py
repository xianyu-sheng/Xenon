"""Automatic session rotation: starting a new conversation must not erase the old one."""

from __future__ import annotations

from xenon.repl import session as session_mod


def _save(conversation_id: str, text: str, monkeypatch, tmp_path):
    monkeypatch.setattr(session_mod, "SESSIONS_DIR", tmp_path)
    return session_mod.auto_save(
        history=[
            {"role": "user", "content": text},
            {"role": "assistant", "content": "done"},
        ],
        context_store={},
        model_config={},
        extra={"conversation_id": conversation_id, "cwd": str(tmp_path)},
    )


def test_new_conversation_archives_previous_auto(tmp_path, monkeypatch):
    _save("conversation-a", "first", monkeypatch, tmp_path)
    _save("conversation-b", "second", monkeypatch, tmp_path)

    names = {s["name"] for s in session_mod.list_sessions()}
    archived = [name for name in names if name.startswith("auto-")]
    assert len(archived) == 1
    assert "_auto" in names

    archived_data = session_mod.load_session(archived[0])
    assert archived_data["history"][0]["content"] == "first"
    assert archived_data["extra"]["conversation_id"] == "conversation-a"
    current = session_mod.load_session("_auto")
    assert current["history"][0]["content"] == "second"


def test_same_conversation_keeps_overwriting_auto(tmp_path, monkeypatch):
    _save("conversation-a", "first", monkeypatch, tmp_path)
    _save("conversation-a", "second", monkeypatch, tmp_path)

    sessions = session_mod.list_sessions()
    assert [s["name"] for s in sessions] == ["_auto"]
    assert session_mod.load_session("_auto")["history"][0]["content"] == "second"


def test_auto_display_name_distinguishes_current_and_archived(tmp_path, monkeypatch):
    _save("conversation-a", "first", monkeypatch, tmp_path)
    _save("conversation-b", "second", monkeypatch, tmp_path)
    sessions = session_mod.list_sessions()
    labels = {session_mod.session_display_name(s) for s in sessions}
    assert "[上次自动保存]" in labels
    assert any(label.startswith("[自动保存]") for label in labels)
