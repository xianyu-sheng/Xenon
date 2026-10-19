"""Non-native in-run compaction is deterministic and keeps result ids."""

from __future__ import annotations

from xenon.engine.base import BaseEngine
from xenon.repl.context_manager import ContextManager


class _Engine(BaseEngine):
    def run(self, user_input, context=None, ctx_mgr=None):  # pragma: no cover
        return ""


def _engine(window: int = 128000) -> _Engine:
    eng = _Engine.__new__(_Engine)
    eng.model_priority = []
    eng.model_configs = {}
    eng._ctx_mgr = None
    eng._context_window = lambda: window  # type: ignore[method-assign]
    return eng


def test_non_native_inrun_compaction_uses_no_llm_and_keeps_result_id(monkeypatch):
    messages = [{"role": "system", "content": "system"}]
    messages.append({"role": "user", "content": "修复登录失败"})
    messages.append(
        {
            "role": "user",
            "content": "Observation: command [FAIL] 2 failed result_id=tr_deadbeef",
        }
    )
    for i in range(7):
        messages.append({"role": "assistant", "content": f"步骤 {i} 已完成"})
        messages.append({"role": "user", "content": f"继续 {i}"})

    import xenon.utils.llm_client as llm

    monkeypatch.setattr(
        llm,
        "chat_completion",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must be 0-LLM")),
    )
    monkeypatch.setattr(ContextManager, "save_snapshot", lambda self: None)
    monkeypatch.setattr(ContextManager, "_persist_compact_md", lambda *a, **k: None)

    compacted = _engine()._maybe_compact_messages(messages, turn=5, every=5)

    assert compacted is not messages
    assert compacted[0]["role"] == "system"
    joined = "\n".join(str(m.get("content") or "") for m in compacted)
    assert "0-LLM" in joined
    assert "tr_deadbeef" in joined
    assert "修复登录失败" in joined
