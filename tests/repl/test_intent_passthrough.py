"""#413: an answer to a question must reach the model unchanged.

A dogfood run ended by asking for the source path; the user replied
"D:\mybook-repo 具体产物在这个路径".  The prompt optimizer classified that as a
research request and wrapped it in a template demanding "compare maintenance
activity, PR response and last update" — requirements the user never wrote.
The model then re-ran the previous research instead of doing the work.
"""

from __future__ import annotations

from xenon.repl.prompt_optimizer import is_value_like, optimize_prompt


def test_a_path_is_information_not_a_task() -> None:
    for text in (
        r"D:\mybook-repo 具体产物在这个路径",
        "D:/mybook-repo",
        "https://github.com/xianyu-sheng/mybook",
        "v0.9.8",
        "README.md",
    ):
        assert is_value_like(text), text


def test_a_real_request_is_still_optimised() -> None:
    for text in (
        "请你调研这个仓库的维护活跃度和 PR 响应速度",
        "用 main 分支做这个改动",
        "写一个快速排序",
        "第二条",
        "你好",
    ):
        assert not is_value_like(text), text


def test_a_path_is_returned_verbatim_even_with_a_research_intent() -> None:
    """The intent layer may say "research"; the datum still must not be wrapped."""

    optimized, hint, was_optimized = optimize_prompt(
        r"D:\mybook-repo 具体产物在这个路径", intent="research"
    )

    assert optimized == r"D:\mybook-repo 具体产物在这个路径"
    assert hint is None
    assert was_optimized is False
    assert "调研要求" not in optimized


def test_a_genuine_research_question_still_gets_its_template() -> None:
    optimized, hint, was_optimized = optimize_prompt(
        "调研 agent 框架", intent="research"
    )

    assert was_optimized is True
    assert "调研要求" in optimized
    assert hint is not None


def test_after_a_question_the_next_message_bypasses_optimisation(monkeypatch) -> None:
    """The REPL flag is set by the turn that asked, consumed by the next one."""
    import xenon.repl.turn_flow as turn_flow
    from xenon.repl.model_registry import ModelRegistry
    from xenon.repl.repl import REPL

    registry = ModelRegistry()
    registry.add_model("openai/a", "a")
    repl = REPL(registry=registry, streaming=False)
    repl.model_pool.register("openai/a", alias="a")

    optimised: list[tuple] = []
    monkeypatch.setattr(
        turn_flow, "optimize_prompt", lambda *a, **k: optimised.append((a, k))
    )
    repl._commit_memory_usage = lambda: None
    repl._maybe_suggest_memory = lambda value: None

    repl._awaiting_user_answer = True
    repl._handle_chat("D:/mybook-repo 具体产物在这个路径")

    assert not optimised, "an answer to a question must not be re-templated"
    assert repl._awaiting_user_answer is False, "the flag is consumed once"
