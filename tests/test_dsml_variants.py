"""C14：DeepSeek 截断后的 DSML 变体（`<||DSML|| calls>`）也要能解析。

真实 SmartBench 复跑中，模型在 native 响应截断后输出了带空格的
`<｜｜DSML｜｜ calls>`，旧解析器只认 `tool_calls`，最终把整轮判成格式错误。
"""

from __future__ import annotations

from xenon.utils.llm_client import _structured_response_kind
from xenon.utils.response_adapter import parse_react

VARIANT = (
    "<｜｜DSML｜｜ calls>\n"
    '<｜｜DSML｜｜ invoke name="read_file">\n'
    '<｜｜DSML｜｜ parameter name="file_path" string="true">README.md\n'
    "</｜｜DSML｜｜ parameter>\n"
    "</｜｜DSML｜｜ invoke>\n"
    "</｜｜DSML｜｜ calls>"
)


def test_space_calls_variant_is_parsed() -> None:
    parsed = parse_react(VARIANT)
    assert isinstance(parsed, list)
    assert parsed[0]["action"] == "read_file"
    assert parsed[0]["action_input"] == {"file_path": "README.md"}


def test_structured_kind_recognizes_space_calls_variant() -> None:
    assert _structured_response_kind(VARIANT) == "dsml"


def test_canonical_tool_calls_still_parses() -> None:
    canonical = VARIANT.replace("｜｜ calls", "｜｜tool_calls").replace(
        "｜｜ invoke", "｜｜invoke"
    ).replace("｜｜ parameter", "｜｜parameter")
    parsed = parse_react(canonical)
    assert parsed[0]["action"] == "read_file"
