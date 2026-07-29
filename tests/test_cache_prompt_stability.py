"""Cache Rails prompt-prefix stability regression test.

The system prompt must be identical across multiple ReActEngine instantiations
within the same calendar day so that DeepSeek's prompt-cache and Xenon's Cache
Rails can reuse the prefix across turns.

Root cause fixed: _build_system_prompt() previously injected a seconds-precision
datetime.now() timestamp, causing a new cache lane every second and a false
cold-start on every REPL turn.  The fix replaces datetime with date.today()
(day-precision only).  This test verifies that invariant holds and catches any
future regression that re-introduces sub-day volatile data into the stable prefix.
"""

from __future__ import annotations

import time
from unittest.mock import patch

from xenon.engine.react_engine import ReActEngine


def _make_engine() -> ReActEngine:
    """Build a ReActEngine without a real LLM client."""
    with patch("xenon.engine.base.BaseEngine.__init__", lambda self, **kw: None):
        eng = ReActEngine.__new__(ReActEngine)
        eng.tools = {}
        eng._mcp_tools_list = ""
        eng.system_prompt = eng._build_system_prompt()
    return eng


class TestCachePromptStability:
    def test_system_prompt_stable_across_two_instantiations(self):
        """Two engines created in quick succession share the same system prompt."""
        eng1 = _make_engine()
        eng2 = _make_engine()
        assert eng1.system_prompt == eng2.system_prompt, (
            "system_prompt differs between two engines created on the same day — "
            "volatile data (e.g. seconds-precision timestamp) has been re-introduced "
            "into the stable prefix, breaking Cache Rails cross-turn reuse."
        )

    def test_system_prompt_stable_after_one_second(self):
        """System prompt must not change between calls one second apart."""
        eng1 = _make_engine()
        time.sleep(1.1)
        eng2 = _make_engine()
        assert eng1.system_prompt == eng2.system_prompt, (
            "system_prompt changed after sleeping 1 second — "
            "volatile time data is present in the system prompt."
        )

    def test_system_prompt_contains_date_not_time(self):
        """Prompt must contain a date string but must not contain HH:MM:SS."""
        import re

        eng = _make_engine()
        prompt = eng.system_prompt
        # Confirm date is present (年月日 pattern)
        assert re.search(r"\d{4}年\d{1,2}月\d{1,2}日", prompt), (
            "system_prompt no longer contains a date string — "
            "model will have no date context."
        )
        # Confirm no seconds-precision time (HH:MM:SS)
        assert not re.search(r"\d{2}:\d{2}:\d{2}", prompt), (
            "system_prompt contains a HH:MM:SS timestamp — "
            "this will fork the Cache Rails lane on every engine instantiation."
        )

    def test_system_prompt_stable_across_model_list_variants(self):
        """MCP tool list differences are the only allowed system-prompt variation."""
        eng_no_mcp = _make_engine()
        with patch("xenon.engine.base.BaseEngine.__init__", lambda self, **kw: None):
            eng_mcp = ReActEngine.__new__(ReActEngine)
            eng_mcp.tools = {}
            eng_mcp._mcp_tools_list = "12306: 查询高铁余票"
            eng_mcp.system_prompt = eng_mcp._build_system_prompt()
        # MCP tool lists differ so prompts differ — that is expected and documented.
        # The important thing is that *within* the same MCP configuration the prompt
        # is stable; the no-MCP case is always stable.
        eng_no_mcp2 = _make_engine()
        assert eng_no_mcp.system_prompt == eng_no_mcp2.system_prompt
