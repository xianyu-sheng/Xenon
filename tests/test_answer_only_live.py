"""Opt-in live regression for answer-only code generation."""

from __future__ import annotations

import ast

import pytest

from xenon.repl.model_registry import ModelRegistry
from xenon.repl.repl import REPL


def _live_credentials_available() -> bool:
    """deepseek 直连凭据是否可用（llm_client.py:502 只认 DEEPSEEK_API_KEY）。"""
    import os

    return bool(os.environ.get("DEEPSEEK_API_KEY"))


pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not _live_credentials_available(),
        reason="live 测试需要 DEEPSEEK_API_KEY 或 ~/.xenon/credentials.yaml 中的 deepseek 凭据",
    ),
]
def test_real_model_keeps_chat_only_code_out_of_tools_and_disk(tmp_path, monkeypatch):
    model_id = "deepseek/deepseek-v4-flash"
    registry = ModelRegistry()
    registry.add_model(model_id, "live")
    registry.assign_role("planner", ["live"])
    repl = REPL(registry=registry, streaming=False, optimize_prompts=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(repl, "_inject_project_context", lambda: None)
    monkeypatch.setattr(repl, "_inject_memories", lambda _text: None)
    monkeypatch.setattr(repl, "_commit_memory_usage", lambda: None)
    monkeypatch.setattr(repl, "_maybe_suggest_memory", lambda _text: None)

    rendered: list[str] = []
    monkeypatch.setattr(
        repl,
        "_render_assistant_text",
        lambda content, **_kwargs: rendered.append(content),
    )

    def fail_if_react(*_args, **_kwargs):
        raise AssertionError("answer-only request must not enter ReAct")

    monkeypatch.setattr(repl, "_run_react_engine", fail_if_react)
    before = set(tmp_path.iterdir())

    repl._handle_chat(
        "为我写一个python实现的快速排序的核心算法代码，并给出详细注释，"
        "输出到对话区域，不写入文件，也不要执行命令"
    )

    assert len(rendered) == 1
    assert rendered[0].startswith("```python\n")
    code = rendered[0].removeprefix("```python\n").removesuffix("\n```")
    ast.parse(code)
    assert set(tmp_path.iterdir()) == before
