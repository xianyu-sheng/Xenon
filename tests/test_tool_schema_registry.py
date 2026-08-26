"""工具注册表 schema 与风险级别契约测试（issue #8）。

修复前的两个缺陷：

1. ``register_tool_handler`` 注册的工具**模型永远看不见** —— 引擎只读
   ``react_prompts.BUILTIN_TOOLS``（静态字典），与注册表无任何代码关联。
2. 插件工具被当成**只读工具**（INFO / level-1），与 ``read_file`` 同等信任，
   **绕过权限确认**。一个叫 ``db_drop_table`` 的插件工具不会触发任何确认。

第 2 点尤其矛盾：``tool_executor.py`` 在 MCP 分支已经写对了原则（未知远端工具
不因为走通用传输就假设只读），插件工具却违反了这条自己写下的原则。
"""

from __future__ import annotations

import pytest

from xenon.nodes.tool_executor import classify_tool, required_execution_level
from xenon.nodes.tool_registry import (
    BUILTIN_TOOL_REGISTRY,
    ToolDefinition,
    ToolRegistry,
    register_tool_handler,
)


def _handler(node, context):
    return {"success": True, "content": "ok"}


@pytest.fixture
def temp_tool():
    """临时注册插件工具，测试后移除，避免污染进程级注册表。"""
    registered: list[str] = []

    def _register(name: str, **kwargs):
        register_tool_handler(name, _handler, **kwargs)
        registered.append(name)
        return name

    yield _register
    for name in registered:
        BUILTIN_TOOL_REGISTRY.unregister(name)


class TestToolDefinitionSchema:
    def test_params_and_risk_are_carried(self):
        d = ToolDefinition(
            name="x",
            handler=_handler,
            params={"q": "查询词"},
            risk="WRITE",
        )
        assert d.params == {"q": "查询词"}
        assert d.risk == "WRITE"

    def test_risk_defaults_to_sensitive(self):
        """未声明 risk 必须默认 SENSITIVE —— 不安全应是显式选择。"""
        assert ToolDefinition(name="x", handler=_handler).risk == "SENSITIVE"

    def test_params_defaults_to_empty_dict_not_shared(self):
        a = ToolDefinition(name="a", handler=_handler)
        b = ToolDefinition(name="b", handler=_handler)
        assert a.params == {} and b.params == {}
        assert a.params is not b.params, "default_factory 必须每实例独立"


class TestRegisterToolHandlerFunctional:
    def test_function_form_registers_schema(self, temp_tool):
        temp_tool(
            "sqlite_query",
            description="对本地 SQLite 执行 SQL",
            params={"db_path": "数据库路径", "sql": "SQL 语句"},
            risk="WRITE",
        )
        d = BUILTIN_TOOL_REGISTRY.get("sqlite_query")
        assert d is not None
        assert d.description == "对本地 SQLite 执行 SQL"
        assert d.params == {"db_path": "数据库路径", "sql": "SQL 语句"}
        assert d.risk == "WRITE"
        assert d.category == "plugin"

    def test_decorator_form_registers_and_returns_function(self):
        @register_tool_handler(
            "deco_tool", description="装饰器注册", params={"q": "查询"}, risk="INFO"
        )
        def handler(node, context):
            return {"success": True}

        try:
            d = BUILTIN_TOOL_REGISTRY.get("deco_tool")
            assert d is not None
            assert d.risk == "INFO"
            assert d.params == {"q": "查询"}
            # 装饰器必须原样返回函数，不能吞掉
            assert callable(handler)
            assert d.handler is handler
        finally:
            BUILTIN_TOOL_REGISTRY.unregister("deco_tool")

    def test_plugin_schemas_only_returns_plugins(self, temp_tool):
        temp_tool("my_plugin", description="插件", params={"a": "参数"})
        schemas = BUILTIN_TOOL_REGISTRY.plugin_schemas()
        assert "my_plugin" in schemas
        assert schemas["my_plugin"] == {
            "name": "my_plugin",
            "description": "插件",
            "params": {"a": "参数"},
        }
        # 内置工具（category='builtin'）不应出现在 plugin_schemas 里
        assert "read_file" not in schemas
        assert "write_file" not in schemas


class TestPluginToolsAreVisibleToModel:
    """核心断言 #1：注册的插件工具必须出现在引擎的工具集里。

    修复前 ``"sqlite_query" in engine.tools`` 为 False —— 工具注册成功但模型
    永远不会调用它，因为引擎只读 BUILTIN_TOOLS。
    """

    def test_registered_tool_appears_in_engine_tools(self, temp_tool):
        temp_tool(
            "sqlite_query",
            description="查询 SQLite",
            params={"sql": "SQL 语句"},
            risk="WRITE",
        )
        from xenon.engine.react_engine import ReActEngine

        engine = ReActEngine(model_priority=["deepseek/deepseek-v4-pro"])
        assert "sqlite_query" in engine.tools, (
            "插件工具未进入 engine.tools —— 模型看不见它，注册等于无效"
        )
        assert engine.tools["sqlite_query"]["description"] == "查询 SQLite"
        assert engine.tools["sqlite_query"]["params"] == {"sql": "SQL 语句"}

    def test_builtin_tools_still_present(self, temp_tool):
        """合并插件 schema 不能挤掉内置工具。"""
        temp_tool("my_plugin", description="插件")
        from xenon.engine.react_engine import ReActEngine

        engine = ReActEngine(model_priority=["deepseek/deepseek-v4-pro"])
        for builtin in ("read_file", "write_file", "command", "list_files"):
            assert builtin in engine.tools, f"内置工具 {builtin} 丢失"

    def test_explicit_tools_arg_still_wins_but_gets_plugins(self, temp_tool):
        """显式传 tools= 时仍应合并插件工具（否则插件在子 Agent 里又不可见）。"""
        temp_tool("my_plugin", description="插件")
        from xenon.engine.react_engine import ReActEngine

        custom = {"only_this": {"name": "only_this", "description": "x", "params": {}}}
        engine = ReActEngine(model_priority=["deepseek/deepseek-v4-pro"], tools=custom)
        assert "only_this" in engine.tools
        assert "my_plugin" in engine.tools

    def test_engine_tools_is_a_copy_not_the_global(self):
        """engine.tools 必须是副本，否则合并会污染全局 BUILTIN_TOOLS。"""
        from xenon.engine.react_prompts import BUILTIN_TOOLS
        from xenon.engine.react_engine import ReActEngine

        before = set(BUILTIN_TOOLS)
        engine = ReActEngine(model_priority=["deepseek/deepseek-v4-pro"])
        engine.tools["scratch_only"] = {"name": "x", "description": "", "params": {}}
        assert set(BUILTIN_TOOLS) == before, "BUILTIN_TOOLS 被引擎实例污染了"


class TestPluginToolRiskClassification:
    """核心断言 #2：插件工具不得默认被当成只读工具。

    修复前：
        db_drop_table  INFO   level 1   ← 与 read_file 同等信任，不触发确认
    """

    def test_undeclared_risk_is_sensitive(self, temp_tool):
        temp_tool("db_drop_table", description="删除数据库表")
        assert classify_tool("db_drop_table") == "SENSITIVE"
        assert required_execution_level("db_drop_table", {}) == 3

    def test_declared_risk_is_respected(self, temp_tool):
        temp_tool("safe_reader", description="只读", risk="INFO")
        temp_tool("file_writer", description="写", risk="WRITE")
        assert classify_tool("safe_reader") == "INFO"
        assert required_execution_level("safe_reader", {}) == 1
        assert classify_tool("file_writer") == "WRITE"
        assert required_execution_level("file_writer", {}) == 2

    def test_unknown_tool_is_sensitive_not_info(self):
        """完全未注册的工具名也必须从严，与 MCP 未知远端工具一致。"""
        assert classify_tool("never_registered_xyz") == "SENSITIVE"
        assert required_execution_level("never_registered_xyz", {}) == 3

    @pytest.mark.parametrize(
        "name,expect_class,expect_level",
        [
            ("read_file", "INFO", 1),
            ("list_files", "INFO", 1),
            ("search_files", "INFO", 1),
            ("web_fetch", "INFO", 1),
            ("write_file", "WRITE", 2),
            ("edit_file", "WRITE", 2),
            ("git", "WRITE", 2),
            ("command", "SENSITIVE", 3),
        ],
    )
    def test_builtin_classification_unchanged(self, name, expect_class, expect_level):
        """回归防护：内置工具的分类不能因为改用注册表而漂移。"""
        assert classify_tool(name) == expect_class, f"{name} 分类漂移"
        assert required_execution_level(name, {}) == expect_level, f"{name} 级别漂移"

    def test_mcp_call_without_params_stays_level_one(self):
        """``mcp_call`` 无参数时保持 level-1 —— 这是既有的刻意设计。

        见 ``tool_executor.py`` 的注释：schema 构建阶段还没有调用参数，此时保持
        MCP 传输可见；真正发请求前会用具体远端工具名再判一次。改用注册表查询
        不应破坏这条路径，所以单独钉住。
        """
        assert classify_tool("mcp_call") == "SENSITIVE"
        assert required_execution_level("mcp_call", {}) == 1


class TestMcpClassificationUnchanged:
    """MCP 的语义判断分支必须保持原样。"""

    @pytest.mark.parametrize(
        "remote,expect",
        [
            ("db:execute_sql", "SENSITIVE"),
            ("files:create_file", "WRITE"),
            ("weather:get_forecast", "INFO"),
            ("something:unrecognized", "SENSITIVE"),
        ],
    )
    def test_mcp_remote_name_semantics(self, remote, expect):
        assert classify_tool("mcp_call", {"tool_name": remote}) == expect


class TestRegistryIsolation:
    def test_registry_instances_are_independent(self):
        a, b = ToolRegistry(), ToolRegistry()
        a.register("x", _handler, risk="INFO")
        assert a.contains("x")
        assert not b.contains("x")

    def test_duplicate_rejected_without_replace(self, temp_tool):
        temp_tool("dupe_check")
        with pytest.raises(ValueError, match="已注册"):
            register_tool_handler("dupe_check", _handler)

    def test_replace_allows_override(self, temp_tool):
        temp_tool("override_me", description="v1", risk="INFO")
        register_tool_handler(
            "override_me", _handler, description="v2", risk="WRITE", replace=True
        )
        d = BUILTIN_TOOL_REGISTRY.get("override_me")
        assert d.description == "v2"
        assert d.risk == "WRITE"

    def test_invalid_risk_is_rejected(self):
        with pytest.raises(ValueError, match="风险"):
            ToolRegistry().register("bad_risk", _handler, risk="BAD")

    def test_public_registration_cannot_replace_builtin_tool(self):
        with pytest.raises(ValueError, match="内置"):
            register_tool_handler("command", _handler, risk="INFO", replace=True)
