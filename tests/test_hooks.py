"""本地 hooks：配置解析、四个事件语义、执行器接线。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from xenon.hooks.runner import HookRunner
from xenon.nodes import tool_executor as te_mod
from xenon.nodes.tool_executor import ToolExecutor
from xenon.engine.context import AgentContext


@pytest.fixture(autouse=True)
def _enable_hooks_for_this_module(monkeypatch):
    """conftest 全局禁用 hooks，本模块需要显式恢复。"""

    monkeypatch.delenv("XENON_HOOKS", raising=False)


def _write_hook(tmp_path: Path, name: str, script: str) -> Path:
    hook = tmp_path / name
    hook.write_text(script, encoding="utf-8")
    if name.endswith(".sh") and sys.platform != "win32":
        hook.chmod(0o755)
    return hook


def _cfg(tmp_path: Path, hooks: dict) -> Path:
    import yaml

    cfg = tmp_path / "hooks.yaml"
    cfg.write_text(yaml.safe_dump({"hooks": hooks}), encoding="utf-8")
    return cfg


def test_malformed_config_is_tolerated(tmp_path):
    (tmp_path / "hooks.yaml").write_text(":::not-yaml", encoding="utf-8")
    runner = HookRunner(user_dir=tmp_path / "hooks.yaml")
    assert runner.run("PreToolUse", "write_file", {}) .blocked is False


def test_pretooluse_allow_and_block(tmp_path):
    allow = tmp_path / "allow.py"
    allow.write_text("import sys; sys.exit(0)", encoding="utf-8")
    block = tmp_path / "block.py"
    block.write_text(
        'import sys; sys.stderr.write("policy says no"); sys.exit(2)',
        encoding="utf-8",
    )
    runner = HookRunner(
        user_dir=_cfg(
            tmp_path,
            {
                "PreToolUse": [
                    {"match": "write_file", "command": f'"{sys.executable}" "{block}"'},
                    {"match": "read_file", "command": f'"{sys.executable}" "{allow}"'},
                ]
            },
        )
    )
    assert runner.run("PreToolUse", "read_file", {}).blocked is False
    outcome = runner.run("PreToolUse", "write_file", {})
    assert outcome.blocked is True
    assert "policy says no" in outcome.message


def test_hook_receives_json_on_stdin(tmp_path):
    marker = tmp_path / "payload.json"
    hook = tmp_path / "capture.py"
    hook.write_text(
        f"import sys, json; json.dump(json.load(sys.stdin), open(r'{marker}', 'w', encoding='utf-8'))",
        encoding="utf-8",
    )
    runner = HookRunner(
        user_dir=_cfg(
            tmp_path,
            {"PostToolUse": [{"match": "*", "command": f'"{sys.executable}" "{hook}"'}]},
        ),
        session_id="s1",
    )
    runner.run("PostToolUse", "write_file", {"file_path": "a.py"})

    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["event"] == "PostToolUse"
    assert payload["tool_name"] == "write_file"
    assert payload["tool_input"] == {"file_path": "a.py"}
    assert payload["session_id"] == "s1"


def test_stop_hook_exit_zero_requests_stop(tmp_path):
    stop = tmp_path / "stop.py"
    stop.write_text("import sys; sys.exit(0)", encoding="utf-8")
    runner = HookRunner(
        user_dir=_cfg(tmp_path, {"Stop": [{"match": "*", "command": f'"{sys.executable}" "{stop}"'}]})
    )
    assert runner.run("Stop", "", {}).stop is True


def test_env_disable(tmp_path):
    import os

    os.environ["XENON_HOOKS"] = "0"
    try:
        runner = HookRunner(user_dir=_cfg(tmp_path, {"Stop": [{"match": "*", "command": "true"}]}))
        assert runner.run("Stop", "", {}).stop is False
    finally:
        os.environ.pop("XENON_HOOKS", None)


class _FakeNode:
    script: list = []

    def __init__(self, name, action_type=None, **params):
        self.action_type = action_type

    @staticmethod
    def normalize_params(p):
        return p

    def execute(self, context):
        if not _FakeNode.script:
            return {"success": True, "content": "default"}
        return _FakeNode.script.pop(0)


def test_executor_blocks_on_pretooluse_exit_2(monkeypatch, tmp_path):
    monkeypatch.setattr(te_mod, "ToolNode", _FakeNode)
    _FakeNode.script = [{"success": True, "content": "ran"}]

    block = tmp_path / "block.py"
    block.write_text("import sys; sys.exit(2)", encoding="utf-8")
    runner = HookRunner(
        user_dir=_cfg(
            tmp_path,
            {"PreToolUse": [{"match": "command", "command": f'"{sys.executable}" "{block}"'}]},
        )
    )

    ctx = AgentContext({"_execution_level": 3})
    result = ToolExecutor(retry_attempts=1, hooks=runner).execute(
        "command", {"command": "echo hi"}, ctx, tools={"command": {}}
    )

    assert result.success is False
    assert "hook 阻断" in result.observation
    assert _FakeNode.script  # 工具体没有执行
