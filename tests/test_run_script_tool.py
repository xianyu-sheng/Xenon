"""C4: run_script executes inline Python/PowerShell without shell quoting."""

from __future__ import annotations

import shutil

import pytest

from xenon.engine.context import AgentContext
from xenon.engine.react_engine import ReActEngine
from xenon.nodes.tool_node import ToolNode


def test_run_python_script(tmp_path) -> None:
    result = ToolNode(
        "t",
        action_type="run_script",
        language="python",
        code="print('run-script-ok')",
        cwd=str(tmp_path),
    ).execute(AgentContext({}))

    assert result["success"] is True
    assert result["returncode"] == 0
    assert "run-script-ok" in result["stdout"]
    assert result["script"].endswith(".py")


def test_run_python_failure_keeps_exit_code_and_stderr(tmp_path) -> None:
    result = ToolNode(
        "t",
        action_type="run_script",
        language="python",
        code="import sys; print('boom', file=sys.stderr); sys.exit(4)",
        cwd=str(tmp_path),
    ).execute(AgentContext({}))

    assert result["success"] is False
    assert result["returncode"] == 4
    assert "boom" in result["stderr"]


def test_run_script_rejects_bad_language_and_empty_code(tmp_path) -> None:
    bad = ToolNode(
        "t", action_type="run_script", language="ruby", code="puts 1", cwd=str(tmp_path)
    ).execute(AgentContext({}))
    assert bad["success"] is False
    assert "不支持的 language" in bad["error"]

    empty = ToolNode(
        "t", action_type="run_script", language="python", code="", cwd=str(tmp_path)
    ).execute(AgentContext({}))
    assert empty["success"] is False
    assert "非空 code" in empty["error"]


@pytest.mark.skipif(
    shutil.which("powershell") is None, reason="PowerShell not available"
)
def test_run_powershell_script(tmp_path) -> None:
    result = ToolNode(
        "t",
        action_type="run_script",
        language="powershell",
        code="Write-Output 'ps-ok'",
        cwd=str(tmp_path),
    ).execute(AgentContext({}))

    assert result["success"] is True
    assert "ps-ok" in result["stdout"]


def test_run_script_is_visible_and_sensitive() -> None:
    engine = ReActEngine(["fake/model"], native_fc=True)
    assert "run_script" in engine.tools
    assert "run_script" in engine.system_prompt
    assert "不要用 `python -c`" in engine.system_prompt
    from xenon.nodes.tool_registry import BUILTIN_TOOL_REGISTRY

    assert BUILTIN_TOOL_REGISTRY.get("run_script").risk == "SENSITIVE"


def test_run_script_native_schema_requires_code() -> None:
    engine = ReActEngine(["fake/model"], native_fc=True)
    schema = engine._build_tools_schema()
    item = next(x for x in schema if x["function"]["name"] == "run_script")
    params = item["function"]["parameters"]
    assert params["required"] == ["code"]
    assert params["properties"]["language"]["enum"] == ["python", "powershell"]
