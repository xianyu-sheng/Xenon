"""
核心模块单元测试。

不依赖外部 API 调用，测试纯逻辑部分。
"""

from __future__ import annotations

import pytest

from xenon.engine.context import AgentContext
from xenon.nodes.base import BaseNode
from xenon.nodes.router_node import RouterNode
from xenon.nodes.tool_node import ToolNode
from xenon.engine.scheduler import DAGScheduler


# ── AgentContext 测试 ─────────────────────────────────────

class TestAgentContext:
    def test_init_empty(self):
        ctx = AgentContext()
        assert ctx.get("anything") is None

    def test_init_with_data(self):
        ctx = AgentContext(initial={"key": "value"})
        assert ctx.get("key") == "value"

    def test_set_and_get(self):
        ctx = AgentContext()
        ctx.set("name", "test")
        assert ctx.get("name") == "test"

    def test_update(self):
        ctx = AgentContext()
        ctx.update({"a": 1, "b": 2})
        assert ctx.get("a") == 1
        assert ctx.get("b") == 2

    def test_has(self):
        ctx = AgentContext(initial={"exists": True})
        assert ctx.has("exists") is True
        assert ctx.has("missing") is False

    def test_snapshot(self):
        ctx = AgentContext(initial={"step": 1})
        ctx.snapshot()
        ctx.set("step", 2)
        assert len(ctx.history) == 1
        assert ctx.history[0]["step"] == 1
        assert ctx.get("step") == 2

    def test_default_value(self):
        ctx = AgentContext()
        assert ctx.get("missing", "fallback") == "fallback"


# ── RouterNode 测试 ──────────────────────────────────────

class TestRouterNode:
    def test_eq_match(self):
        ctx = AgentContext(initial={"status": "done"})
        router = RouterNode("r1", rules=[
            {"condition": {"key": "status", "op": "==", "value": "done"}, "next": "end"},
        ])
        result = router.execute(ctx)
        assert result["next_node"] == "end"

    def test_no_match_with_default(self):
        ctx = AgentContext(initial={"status": "running"})
        router = RouterNode("r1", rules=[
            {"condition": {"key": "status", "op": "==", "value": "done"}, "next": "end"},
        ], default_next="loop")
        result = router.execute(ctx)
        assert result["next_node"] == "loop"

    def test_no_match_no_default_raises(self):
        ctx = AgentContext(initial={"status": "running"})
        router = RouterNode("r1", rules=[
            {"condition": {"key": "status", "op": "==", "value": "done"}, "next": "end"},
        ])
        with pytest.raises(RuntimeError, match="无规则命中"):
            router.execute(ctx)

    def test_contains_operator(self):
        ctx = AgentContext(initial={"output": "DONE - all steps complete"})
        router = RouterNode("r1", rules=[
            {"condition": {"key": "output", "op": "contains", "value": "DONE"}, "next": "review"},
        ])
        result = router.execute(ctx)
        assert result["next_node"] == "review"

    def test_numeric_comparison(self):
        ctx = AgentContext(initial={"retry_count": 3})
        router = RouterNode("r1", rules=[
            {"condition": {"key": "retry_count", "op": ">=", "value": 3}, "next": "error"},
        ])
        result = router.execute(ctx)
        assert result["next_node"] == "error"

    def test_is_truthy(self):
        ctx = AgentContext(initial={"plan": "some plan here"})
        router = RouterNode("r1", rules=[
            {"condition": {"key": "plan", "op": "is_truthy", "value": True}, "next": "exec"},
        ])
        result = router.execute(ctx)
        assert result["next_node"] == "exec"

    def test_rules_evaluated_in_order(self):
        ctx = AgentContext(initial={"a": 1, "b": 2})
        router = RouterNode("r1", rules=[
            {"condition": {"key": "a", "op": "==", "value": 1}, "next": "first"},
            {"condition": {"key": "b", "op": "==", "value": 2}, "next": "second"},
        ])
        result = router.execute(ctx)
        assert result["next_node"] == "first"


# ── DAGScheduler 测试 ────────────────────────────────────

class SimpleNode(BaseNode):
    """测试用简单节点：将固定值写入 context。"""

    def __init__(
        self,
        node_id: str,
        value: str,
        *,
        output_slot: str | None = None,
        default_next: str | None = None,
    ):
        super().__init__(node_id, output_slot=output_slot, default_next=default_next)
        self.value = value

    def execute(self, context: AgentContext) -> dict:
        self._write_output(context, self.value)
        return {"value": self.value}


class TestDAGScheduler:
    def test_simple_linear_flow(self):
        """三个节点线性执行: start -> check(router) -> end。"""
        n1 = SimpleNode("start", "hello", output_slot="msg", default_next="check")
        router = RouterNode("check", rules=[
            {"condition": {"key": "msg", "op": "==", "value": "hello"}, "next": "end"},
        ])
        n2 = SimpleNode("end", "done")

        nodes = {"start": n1, "check": router, "end": n2}
        scheduler = DAGScheduler(nodes, start_node_id="start")
        result = scheduler.run()

        assert result["status"] == "completed"
        assert result["steps"] == 3

    def test_max_steps_termination(self):
        """循环图超过最大步数时强制终止。"""
        n1 = SimpleNode("loop", "again", output_slot="x", default_next="decide")
        router = RouterNode("decide", rules=[
            {"condition": {"key": "x", "op": "==", "value": "never"}, "next": "end"},
        ], default_next="loop")
        n2 = SimpleNode("end", "done")

        nodes = {"loop": n1, "decide": router, "end": n2}
        scheduler = DAGScheduler(nodes, start_node_id="loop", max_steps=5)
        result = scheduler.run()

        assert result["status"] == "max_steps_reached"
        assert result["steps"] == 5

    def test_context_passed_through(self):
        """context 在节点间正确传递。"""
        ctx = AgentContext(initial={"greeting": "hi"})

        class ReadNode(BaseNode):
            def execute(self, context):
                val = context.get("greeting")
                context.set("echo", val)
                return {"echo": val}

        n1 = ReadNode("reader", default_next="check")
        router = RouterNode("check", rules=[
            {"condition": {"key": "echo", "op": "==", "value": "hi"}, "next": "end"},
        ])
        n2 = SimpleNode("end", "done")

        nodes = {"reader": n1, "check": router, "end": n2}
        scheduler = DAGScheduler(nodes, start_node_id="reader")
        result = scheduler.run(ctx)

        assert result["status"] == "completed"
        assert result["context"].get("echo") == "hi"

    def test_empty_nodes_raises(self):
        with pytest.raises(ValueError, match="不能为空"):
            DAGScheduler({}, start_node_id="x")

    def test_invalid_start_node_raises(self):
        n = SimpleNode("a", "v")
        with pytest.raises(ValueError, match="不在节点列表中"):
            DAGScheduler({"a": n}, start_node_id="missing")


# ── ToolNode 测试 ────────────────────────────────────────

class TestToolNode:
    def test_echo_command(self):
        ctx = AgentContext()
        node = ToolNode("t1", action="echo hello", output_slot="out")
        result = node.execute(ctx)
        assert "hello" in result["stdout"]
        assert result["returncode"] == 0
        assert ctx.get("out") == "hello"

    def test_template_variable(self):
        ctx = AgentContext(initial={"name": "world"})
        node = ToolNode("t1", action="echo {name}", output_slot="out")
        result = node.execute(ctx)
        assert "world" in result["stdout"]


# ── Config Parser 测试 ───────────────────────────────────

class TestConfigParser:
    def test_parse_workflow_basic(self):
        from xenon.utils.config_parser import parse_workflow

        config = {
            "models": {"planner": ["openai/gpt-4o"]},
            "nodes": [
                {
                    "id": "step1",
                    "type": "llm",
                    "model": "planner",
                    "prompt": "hello",
                    "output_slot": "out",
                },
                {
                    "id": "route",
                    "type": "router",
                    "rules": [
                        {"condition": {"key": "out", "op": "is_truthy", "value": True}, "next": "step1"},
                    ],
                    "default_next": "step1",
                },
            ],
        }
        nodes, models = parse_workflow(config)
        assert "step1" in nodes
        assert "route" in nodes
        assert models["planner"] == ["openai/gpt-4o"]

    def test_parse_workflow_direct_model(self):
        from xenon.utils.config_parser import parse_workflow

        config = {
            "models": {},
            "nodes": [
                {
                    "id": "step1",
                    "type": "llm",
                    "model": "openai/gpt-4o",
                    "prompt": "hello",
                },
            ],
        }
        nodes, _ = parse_workflow(config)
        assert nodes["step1"].model_priority == ["openai/gpt-4o"]
