"""TaskState continuity through the REPL turn pipeline."""

from __future__ import annotations

from xenon.repl.llm_intent_classifier import ClassificationResult
from xenon.repl.model_registry import ModelRegistry
from xenon.repl.repl import REPL


class _Classifier:
    enabled = True
    confidence_threshold = 0.7

    def __init__(self) -> None:
        self.calls: list[str] = []

    def classify(self, text, *, context_messages=None, hints=None, task_state=None):
        self.calls.append(text)
        return ClassificationResult(
            intent="write_code",
            operations=("read", "write", "execute"),
            confidence=0.95,
            reasoning="implementation",
        )


def _repl(monkeypatch) -> tuple[REPL, list[str]]:
    registry = ModelRegistry()
    registry.add_model("openai/test", "test")
    registry.assign_role("planner", ["test"])
    repl = REPL(registry=registry, streaming=False, optimize_prompts=True)
    repl._intent_classifier = _Classifier()
    repl._intent_classifier_checked = True
    captured: list[str] = []
    monkeypatch.setattr(repl, "_inject_project_context", lambda: None)
    monkeypatch.setattr(repl, "_inject_memories", lambda _text: None)
    monkeypatch.setattr(repl, "_commit_memory_usage", lambda: None)
    monkeypatch.setattr(repl, "_maybe_suggest_memory", lambda _text: None)
    monkeypatch.setattr(
        repl, "_run_react_engine", lambda prompt, _models: captured.append(prompt)
    )
    return repl, captured


def test_issue_task_creates_state_and_continue_restores_it(monkeypatch):
    repl, captured = _repl(monkeypatch)

    repl._handle_chat("实现 #22")
    state = repl.agent_context.get("_task_state")
    assert state["objective"] == "实现 #22"
    assert "verify" in state["required"]
    assert "commit" in state["required"]
    assert repl.agent_context.get("_execution_level") == 3

    repl._handle_chat("继续你还没做完 没有测试 没有创建PR")
    state = repl.agent_context.get("_task_state")
    assert state["required"]
    assert "pr" in state["required"]
    assert "任务状态（跨回合，必须延续）" in captured[-1]
    assert "未完成交付" in captured[-1]
    assert repl.agent_context.get("_execution_level") == 3
