"""
ToolNode 文件操作测试。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from omniagent.engine.context import AgentContext
from omniagent.nodes.tool_node import ToolNode
from omniagent.utils.config_parser import parse_workflow


class TestToolNodeFileOps:
    """测试 ToolNode 的文件读写能力。"""

    def test_write_file(self):
        """测试写入文件。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = os.path.join(tmpdir, "test.txt")
            ctx = AgentContext()
            node = ToolNode(
                "writer",
                action_type="write_file",
                file_path=filepath,
                content="hello world",
                output_slot="path",
            )
            result = node.execute(ctx)

            assert result["success"] is True
            assert result["action_type"] == "write_file"
            assert Path(filepath).exists()
            assert Path(filepath).read_text() == "hello world"
            assert ctx.get("path") == filepath

    def test_write_file_with_template(self):
        """测试带模板变量的文件写入。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = os.path.join(tmpdir, "{filename}.txt")
            ctx = AgentContext(initial={"filename": "output", "data": "template content"})
            node = ToolNode(
                "writer",
                action_type="write_file",
                file_path=filepath,
                content="{data}",
            )
            result = node.execute(ctx)

            assert result["success"] is True
            expected = os.path.join(tmpdir, "output.txt")
            assert Path(expected).exists()
            assert Path(expected).read_text() == "template content"

    def test_read_file(self):
        """测试读取文件。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = os.path.join(tmpdir, "input.txt")
            Path(filepath).write_text("file content here")

            ctx = AgentContext()
            node = ToolNode(
                "reader",
                action_type="read_file",
                file_path=filepath,
                output_slot="content",
            )
            result = node.execute(ctx)

            assert result["success"] is True
            assert result["exists"] is True
            assert result["content"] == "file content here"
            assert ctx.get("content") == "file content here"

    def test_read_file_not_found(self):
        """测试读取不存在的文件。"""
        ctx = AgentContext()
        node = ToolNode(
            "reader",
            action_type="read_file",
            file_path="/nonexistent/path.txt",
            output_slot="content",
        )
        result = node.execute(ctx)

        assert result["success"] is False
        assert result["exists"] is False
        assert ctx.get("content") == ""

    def test_write_file_creates_dirs(self):
        """测试写入文件时自动创建目录。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = os.path.join(tmpdir, "sub", "dir", "file.txt")
            ctx = AgentContext()
            node = ToolNode(
                "writer",
                action_type="write_file",
                file_path=filepath,
                content="nested",
            )
            result = node.execute(ctx)

            assert result["success"] is True
            assert Path(filepath).read_text() == "nested"

    def test_append_mode(self):
        """测试追加模式。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = os.path.join(tmpdir, "append.txt")
            Path(filepath).write_text("line1\n")

            ctx = AgentContext()
            node = ToolNode(
                "appender",
                action_type="write_file",
                file_path=filepath,
                content="line2\n",
                append=True,
            )
            node.execute(ctx)

            assert Path(filepath).read_text() == "line1\nline2\n"

    def test_command_action_type(self):
        """测试命令执行 action_type。"""
        ctx = AgentContext()
        node = ToolNode(
            "runner",
            action_type="command",
            action="echo hello",
            output_slot="out",
        )
        result = node.execute(ctx)

        assert result["success"] is True
        assert "hello" in result["stdout"]

    def test_invalid_action_type(self):
        """测试无效的 action_type。"""
        ctx = AgentContext()
        node = ToolNode(
            "bad",
            action_type="invalid",
        )
        with pytest.raises(ValueError, match="不支持的 action_type"):
            node.execute(ctx)


class TestToolNodeConfig:
    """测试配置解析器对新 ToolNode 的支持。"""

    def test_parse_write_file_node(self):
        from omniagent.utils.config_parser import parse_workflow

        config = {
            "models": {},
            "nodes": [
                {
                    "id": "writer",
                    "type": "tool",
                    "action_type": "write_file",
                    "file_path": "output.txt",
                    "content": "hello",
                    "next": "end",
                },
                {
                    "id": "end",
                    "type": "tool",
                    "action": "echo done",
                },
            ],
        }
        nodes, _ = parse_workflow(config)
        writer = nodes["writer"]
        assert isinstance(writer, ToolNode)
        assert writer.action_type == "write_file"
        assert writer.file_path == "output.txt"
        assert writer.content == "hello"

    def test_parse_read_file_node(self):
        from omniagent.utils.config_parser import parse_workflow

        config = {
            "models": {},
            "nodes": [
                {
                    "id": "reader",
                    "type": "tool",
                    "action_type": "read_file",
                    "file_path": "input.txt",
                    "output_slot": "data",
                    "next": "end",
                },
                {
                    "id": "end",
                    "type": "tool",
                    "action": "echo done",
                },
            ],
        }
        nodes, _ = parse_workflow(config)
        reader = nodes["reader"]
        assert reader.action_type == "read_file"
        assert reader.file_path == "input.txt"

    def test_parse_command_node_backward_compat(self):
        """测试旧格式（只有 action 字段）仍然兼容。"""
        from omniagent.utils.config_parser import parse_workflow

        config = {
            "models": {},
            "nodes": [
                {
                    "id": "runner",
                    "type": "tool",
                    "action": "echo test",
                    "next": "end",
                },
                {
                    "id": "end",
                    "type": "tool",
                    "action": "echo done",
                },
            ],
        }
        nodes, _ = parse_workflow(config)
        runner = nodes["runner"]
        assert runner.action_type == "command"
        assert runner.action == "echo test"


class TestToolNodeNewActions:
    """测试新增的工具类型。"""

    def test_list_files(self):
        """测试目录遍历。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "a.py").write_text("a")
            Path(tmpdir, "b.txt").write_text("b")
            Path(tmpdir, "sub").mkdir()
            Path(tmpdir, "sub", "c.py").write_text("c")

            ctx = AgentContext()
            node = ToolNode(
                "lister", action_type="list_files",
                file_path=tmpdir, pattern="*.py", output_slot="files",
            )
            result = node.execute(ctx)

            assert result["success"] is True
            assert result["count"] == 2  # a.py + sub/c.py
            assert ctx.get("files") != ""

    def test_list_files_nonexistent(self):
        """测试遍历不存在的目录。"""
        ctx = AgentContext()
        node = ToolNode("lister", action_type="list_files", file_path="/nonexistent")
        result = node.execute(ctx)
        assert result["success"] is False

    def test_search_files(self):
        """测试文件内容搜索。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "test.py").write_text("def hello():\n    return 'world'\n")
            Path(tmpdir, "other.py").write_text("x = 1\n")

            ctx = AgentContext()
            node = ToolNode(
                "searcher", action_type="search_files",
                file_path=tmpdir, search_pattern="hello", file_filter="*.py",
                output_slot="results",
            )
            result = node.execute(ctx)

            assert result["success"] is True
            assert result["match_count"] >= 1
            assert any("hello" in m["content"] for m in result["matches"])

    def test_search_files_no_pattern(self):
        """测试搜索缺少 pattern。"""
        ctx = AgentContext()
        node = ToolNode("searcher", action_type="search_files", file_path=".")
        with pytest.raises(ValueError, match="search_pattern"):
            node.execute(ctx)

    def test_git_status(self):
        """测试 git status 命令。"""
        ctx = AgentContext()
        node = ToolNode(
            "gitter", action_type="git", git_command="status",
            cwd=".", output_slot="git_out",
        )
        result = node.execute(ctx)
        # 可能不在 git 仓库中，但不应该崩溃
        assert result["action_type"] == "git"

    def test_web_fetch_missing_url(self):
        """测试 web_fetch 缺少 url。"""
        ctx = AgentContext()
        node = ToolNode("fetcher", action_type="web_fetch", url="")
        with pytest.raises(ValueError, match="url"):
            node.execute(ctx)

    def test_config_parse_list_files(self):
        """测试配置解析 list_files。"""
        from omniagent.utils.config_parser import parse_workflow

        config = {
            "models": {},
            "nodes": [
                {"id": "list", "type": "tool", "action_type": "list_files",
                 "file_path": ".", "pattern": "*.py", "max_depth": 3, "next": "end"},
                {"id": "end", "type": "tool", "action": "echo done"},
            ],
        }
        nodes, _ = parse_workflow(config)
        assert nodes["list"].action_type == "list_files"
        assert nodes["list"].pattern == "*.py"
        assert nodes["list"].max_depth == 3

    def test_config_parse_search_files(self):
        """测试配置解析 search_files。"""
        from omniagent.utils.config_parser import parse_workflow

        config = {
            "models": {},
            "nodes": [
                {"id": "search", "type": "tool", "action_type": "search_files",
                 "file_path": ".", "search_pattern": "TODO", "file_filter": "*.py", "next": "end"},
                {"id": "end", "type": "tool", "action": "echo done"},
            ],
        }
        nodes, _ = parse_workflow(config)
        assert nodes["search"].action_type == "search_files"
        assert nodes["search"].search_pattern == "TODO"

    def test_config_parse_git(self):
        """测试配置解析 git。"""
        from omniagent.utils.config_parser import parse_workflow

        config = {
            "models": {},
            "nodes": [
                {"id": "g", "type": "tool", "action_type": "git",
                 "git_command": "log", "next": "end"},
                {"id": "end", "type": "tool", "action": "echo done"},
            ],
        }
        nodes, _ = parse_workflow(config)
        assert nodes["g"].action_type == "git"
        assert nodes["g"].git_command == "log"

    def test_config_parse_web_fetch(self):
        """测试配置解析 web_fetch。"""
        from omniagent.utils.config_parser import parse_workflow

        config = {
            "models": {},
            "nodes": [
                {"id": "fetch", "type": "tool", "action_type": "web_fetch",
                 "url": "https://example.com", "next": "end"},
                {"id": "end", "type": "tool", "action": "echo done"},
            ],
        }
        nodes, _ = parse_workflow(config)
        assert nodes["fetch"].action_type == "web_fetch"
        assert nodes["fetch"].url == "https://example.com"

    def test_edit_file_replace(self, tmp_path):
        """edit_file 精确替换。"""
        f = tmp_path / "test.py"
        f.write_text("def hello():\n    print('old')\n")

        node = ToolNode(
            "e1",
            action_type="edit_file",
            file_path=str(f),
            old_text="print('old')",
            new_text="print('new')",
        )
        result = node.execute(AgentContext())
        assert result["success"] is True
        assert result["replacements"] == 1
        assert "print('new')" in f.read_text()

    def test_edit_file_not_found(self, tmp_path):
        """edit_file 未找到匹配。"""
        f = tmp_path / "test.py"
        f.write_text("hello world")

        node = ToolNode(
            "e2",
            action_type="edit_file",
            file_path=str(f),
            old_text="nonexistent",
            new_text="new",
        )
        result = node.execute(AgentContext())
        assert result["success"] is False
        assert "未找到" in result["error"]

    def test_edit_file_multiple_matches(self, tmp_path):
        """edit_file 多处匹配时拒绝。"""
        f = tmp_path / "test.py"
        f.write_text("x = 1\ny = 1\n")

        node = ToolNode(
            "e3",
            action_type="edit_file",
            file_path=str(f),
            old_text="1",
            new_text="2",
        )
        result = node.execute(AgentContext())
        assert result["success"] is False
        assert "2 处匹配" in result["error"]

    def test_edit_file_config_parse(self):
        """从配置解析 edit_file 节点。"""
        config = {
            "workflow": {"name": "test", "start": "edit"},
            "nodes": [
                {"id": "edit", "type": "tool", "action_type": "edit_file",
                 "file_path": "{target_file}", "old_text": "old", "new_text": "new"},
            ],
        }
        nodes, _ = parse_workflow(config)
        node = nodes["edit"]
        assert node.action_type == "edit_file"
        assert node.old_text == "old"
        assert node.new_text == "new"
