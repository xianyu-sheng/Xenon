from __future__ import annotations

from pathlib import Path

from omniagent.repl.session import RuntimeSessionStore


def test_runtime_session_store_creates_thread_and_notes(tmp_path: Path):
    store = RuntimeSessionStore(root=tmp_path)
    session = store.create(title="Demo")

    assert session.meta_path.exists()
    assert session.thread_path.exists()
    assert session.notes_path.exists()
    assert session.runs_dir.exists()
    assert session.title == "Demo"


def test_runtime_session_store_appends_thread_messages(tmp_path: Path):
    store = RuntimeSessionStore(root=tmp_path)
    session = store.create(title="Demo", session_id="sess-test")

    store.append_message(
        session.id,
        role="user",
        content="hello",
        run_id="run-1",
        metadata={"mode": "direct"},
    )
    store.append_message(session.id, role="assistant", content="hi", run_id="run-1", model_used="test/model")

    entries = store.read_thread(session.id)

    assert len(entries) == 2
    assert entries[0]["role"] == "user"
    assert entries[0]["metadata"]["mode"] == "direct"
    assert entries[1]["model_used"] == "test/model"


def test_runtime_session_store_appends_notes(tmp_path: Path):
    store = RuntimeSessionStore(root=tmp_path)
    session = store.create(title="Demo")

    path = store.append_note(session.id, "Remember this decision.")
    notes = store.read_notes(session.id)

    assert path == session.notes_path
    assert "Remember this decision." in notes
    assert store.get(session.id).updated_at >= session.updated_at


def test_runtime_session_store_lists_recent_sessions(tmp_path: Path):
    store = RuntimeSessionStore(root=tmp_path)
    first = store.create(title="First", session_id="sess-first")
    second = store.create(title="Second", session_id="sess-second")

    sessions = store.list()

    assert {s.id for s in sessions} == {first.id, second.id}
