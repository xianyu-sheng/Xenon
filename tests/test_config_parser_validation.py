"""工作流配置解析器的输入校验回归测试。

Bug 背景（v0.8.0 模拟调用发现）：
parse_workflow 对用户 YAML 的结构假设全部为 dict/list，未做类型校验，
畸形配置泄漏 Python 内部 AttributeError（如 "'str' object has no attribute
'get'"）给用户。修复后所有输入错误必须是带中文定位信息的 ValueError。
"""

from __future__ import annotations

import pytest

from xenon.utils.config_parser import load_yaml, parse_workflow

_BASE = {"version": "1.1", "models": {"planner": "deepseek/deepseek-v4-flash"}}
_LLM = {"id": "n1", "type": "llm", "model": "planner", "prompt": "hi"}


class TestLoadYamlValidation:
    def test_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="配置文件不存在"):
            load_yaml(tmp_path / "nope.yaml")

    def test_empty_file(self, tmp_path):
        p = tmp_path / "empty.yaml"
        p.write_text("")
        with pytest.raises(ValueError, match="配置文件为空"):
            load_yaml(p)

    def test_yaml_syntax_error(self, tmp_path):
        p = tmp_path / "bad.yaml"
        p.write_text("a: [unclosed\n")
        with pytest.raises(ValueError, match="YAML 语法错误"):
            load_yaml(p)

    def test_scalar_content(self, tmp_path):
        p = tmp_path / "scalar.yaml"
        p.write_text("just a string\n")
        with pytest.raises(ValueError, match="期望 dict"):
            load_yaml(p)


class TestParseWorkflowValidation:
    def test_nodes_as_dict(self):
        cfg = {**_BASE, "nodes": {"n1": _LLM}}
        with pytest.raises(ValueError, match="nodes 字段格式错误"):
            parse_workflow(cfg)

    def test_nodes_as_string(self):
        cfg = {**_BASE, "nodes": "n1"}
        with pytest.raises(ValueError, match="nodes 字段格式错误"):
            parse_workflow(cfg)

    def test_node_as_string(self):
        cfg = {**_BASE, "nodes": ["n1"]}
        with pytest.raises(ValueError, match="第 1 个节点配置格式错误"):
            parse_workflow(cfg)

    def test_node_as_none(self):
        cfg = {**_BASE, "nodes": [None]}
        with pytest.raises(ValueError, match="第 1 个节点配置格式错误"):
            parse_workflow(cfg)

    def test_duplicate_node_id(self):
        cfg = {**_BASE, "nodes": [_LLM, dict(_LLM)]}
        with pytest.raises(ValueError, match="节点 id 重复"):
            parse_workflow(cfg)

    def test_models_as_string(self):
        cfg = {**_BASE, "models": "deepseek/x", "nodes": [_LLM]}
        with pytest.raises(ValueError, match="models 字段格式错误"):
            parse_workflow(cfg)

    def test_models_bad_value(self):
        cfg = {**_BASE, "models": {"planner": 3}, "nodes": [_LLM]}
        with pytest.raises(ValueError, match="models.planner 格式错误"):
            parse_workflow(cfg)

    def test_tool_node_unknown_key(self):
        cfg = {
            **_BASE,
            "nodes": [{"id": "t", "type": "tool", "action": "ls", "timeot": 5}],
        }
        with pytest.raises(ValueError, match="未知配置字段.*timeot"):
            parse_workflow(cfg)

    def test_tool_node_extra_keys_passed_through(self):
        """之前 _build_tool_node 只转发 17 个固定参数，limit/cursor 等被静默丢弃。"""
        cfg = {
            **_BASE,
            "nodes": [
                {
                    "id": "t",
                    "type": "tool",
                    "action_type": "command",
                    "action": "echo ok",
                    "timeout": 5,
                }
            ],
        }
        nodes, _ = parse_workflow(cfg)
        assert getattr(nodes["t"], "timeout") == 5

    def test_valid_llm_node(self):
        nodes, models = parse_workflow({**_BASE, "nodes": [_LLM]})
        assert list(nodes) == ["n1"]
        assert models == {"planner": ["deepseek/deepseek-v4-flash"]}
