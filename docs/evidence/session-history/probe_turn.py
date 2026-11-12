"""Capture what a real kernel turn actually sends to the model.

The question this answers: when the model "ignores" get_state, was the tool
offered, was the pointer in the prompt, and what did the model reply?  A probe
that fakes the transport cannot answer it — the native path is chosen only when
the real transport is used, so the transport is wrapped, never replaced.

Usage:  python docs/evidence/session-history/probe_turn.py <workspace>
        (the workspace's ledger/ must already hold at least one earlier task)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

MODEL = "anthropic/deepseek-v4.1-flash"


def main() -> int:
    workspace = Path(sys.argv[1]).resolve()
    ledger_dir = workspace / "ledger"
    entry = yaml.safe_load(
        (Path.home() / ".xenon" / "models.yaml").read_text(encoding="utf-8")
    )["models"][MODEL]

    from xenon.harness.llm import ModelConfig, build_llm_loop_strategy
    from xenon.harness.runner import run_task
    from xenon.kernel.policies import PermissionMode
    from xenon.kernel.strategies import DEFAULT_PROFILE
    import xenon.harness.llm as llm_mod
    import xenon.utils.llm_client as lc

    config = ModelConfig(
        model_id=MODEL,
        base_url=str(entry.get("base_url") or ""),
        api_key=str(entry.get("api_key") or ""),
        max_tokens=int(entry.get("max_tokens") or 8192),
    )

    calls: list[dict] = []
    original_native = lc.chat_completion_with_tools
    original_text = llm_mod.chat_completion

    def record(kind, messages, tools, response, content):
        calls.append(
            {
                "kind": kind,
                "tools": [str(t.get("name")) for t in tools or ()],
                "messages": messages,
                "tool_calls": getattr(response, "tool_calls", None),
                "content": content,
            }
        )

    def spy_native(model_id, messages, **kwargs):
        response = original_native(model_id, messages, **kwargs)
        record("native", messages, kwargs.get("tools"), response, getattr(response, "content", ""))
        return response

    def spy_text(model_id, messages, **kwargs):
        response = original_text(model_id, messages, **kwargs)
        record("text", messages, None, None, response)
        return response

    lc.chat_completion_with_tools = spy_native
    llm_mod.chat_completion = spy_text
    try:
        run = run_task(
            "回答：上一轮我让你读的是哪个文件？请先确认会话历史，再作答。",
            strategy=build_llm_loop_strategy(config, DEFAULT_PROFILE),
            workspace=str(workspace),
            ledger_dir=str(ledger_dir),
            permission=PermissionMode.BYPASS,
        )
    finally:
        lc.chat_completion_with_tools = original_native
        llm_mod.chat_completion = original_text

    print("phase:", run.result.phase.value)
    for index, call in enumerate(calls, 1):
        turn = call["messages"][-1]["content"]
        print(
            f"--- call {index} [{call['kind']}]: tools={len(call['tools'])} "
            f"get_state_offered={'get_state' in call['tools']} "
            f"pointer={'Earlier tasks this session' in turn}"
        )
        print("    tool_calls:", json.dumps(call["tool_calls"], ensure_ascii=False)[:400])
        if call["content"]:
            print("    content:", str(call["content"])[:200].replace("\n", " "))
    print("answer:", str(run.result.answer)[:400].replace("\n", " "))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
