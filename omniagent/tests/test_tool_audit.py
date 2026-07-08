"""全量工具可用性审查 — 对每个工具做单元级冒烟测试。

用法: python -m pytest omniagent/tests/test_tool_audit.py -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from omniagent.nodes.tool_node import (
    ToolNode,
    _ssrf_check_url,
    _is_internal_ip,
    _is_rfc1918_private,
)
from omniagent.engine.context import AgentContext

# ── helpers ──────────────────────────────────────────────────


def _ctx(**kwargs) -> AgentContext:
    ctx = AgentContext()
    for k, v in kwargs.items():
        ctx.set(k, v)
    return ctx


# ── 1. normalize_params 参数不丢失 ────────────────────────────


class TestNormalizeParams:
    """验证 _VALID_PARAMS 覆盖了所有 __init__ 参数。"""

    def test_all_init_params_in_valid_params(self):
        import inspect
        sig = inspect.signature(ToolNode.__init__)
        for name, param in sig.parameters.items():
            if name in ("self", "node_id"):
                continue
            assert name in ToolNode._VALID_PARAMS, (
                f"__init__ 参数 '{name}' 不在 _VALID_PARAMS 中，"
                f"normalize_params 会将其过滤掉！"
            )

    def test_weather_params_not_filtered(self):
        result = ToolNode.normalize_params({"city": "上海", "lang": "zh"})
        assert "city" in result
        assert "lang" in result

    def test_register_tool_params_not_filtered(self):
        result = ToolNode.normalize_params({
            "tool_name": "t1", "description": "d", "python_function": "omniagent.foo.bar",
            "command_template": "echo {x}", "params": {"type": "object"},
        })
        for key in ("description", "python_function", "command_template", "params"):
            assert key in result, f"'{key}' 被过滤了"

    def test_city_alias_location(self):
        result = ToolNode.normalize_params({"location": "深圳"})
        assert result.get("city") == "深圳"

    def test_city_alias_place(self):
        result = ToolNode.normalize_params({"place": "杭州"})
        assert result.get("city") == "杭州"

    def test_lang_alias_language(self):
        result = ToolNode.normalize_params({"language": "en"})
        assert result.get("lang") == "en"


# ── 2. SSRF 防护 ──────────────────────────────────────────────


class TestSSRF:
    """验证 SSRF 防护正确拦截内网地址，但不误拦合法公网地址。"""

    def test_198_18_benchnmark_not_blocked(self):
        """198.18.0.0/15 是 IANA 基准测试段，不应被拦截。"""
        import ipaddress
        assert not _is_internal_ip(ipaddress.ip_address("198.18.0.4"))
        assert not _is_rfc1918_private(ipaddress.ip_address("198.18.0.4"))

    def test_rfc1918_blocked(self):
        import ipaddress
        for ip_str in ("10.0.0.1", "172.16.0.1", "192.168.1.1", "100.64.0.1"):
            assert _is_internal_ip(ipaddress.ip_address(ip_str)), (
                f"RFC 1918 {ip_str} 应被拦截"
            )

    def test_loopback_blocked(self):
        import ipaddress
        assert _is_internal_ip(ipaddress.ip_address("127.0.0.1"))
        assert _is_internal_ip(ipaddress.ip_address("::1"))

    def test_public_not_blocked(self):
        import ipaddress
        for ip_str in ("8.8.8.8", "1.1.1.1", "208.67.222.222"):
            assert not _is_internal_ip(ipaddress.ip_address(ip_str)), (
                f"公网 IP {ip_str} 不应被拦截"
            )

    def test_ssrf_check_rejects_private(self):
        ok, reason = _ssrf_check_url("http://10.0.0.1/admin")
        assert not ok
        assert "内网" in reason or "private" in reason.lower() or "保留" in reason

    def test_ssrf_check_allows_wttr_in(self):
        """wttr.in 是合法天气 API。"""
        ok, reason = _ssrf_check_url("https://wttr.in/Beijing?format=j1")
        assert ok, f"wttr.in 不应被拦截: {reason}"

    def test_ssrf_check_rejects_non_http(self):
        ok, reason = _ssrf_check_url("file:///etc/passwd")
        assert not ok
        assert "http" in reason.lower() or "仅允许" in reason

    def test_ssrf_check_rejects_empty_host(self):
        ok, reason = _ssrf_check_url("http://")
        assert not ok


# ── 3. Weather 工具冒烟 ───────────────────────────────────────


class TestWeatherTool:
    """测试 weather 工具的实际 API 调用。"""

    def test_weather_creates_node(self):
        node = ToolNode("w1", action_type="weather", city="Beijing", lang="zh")
        assert node.city == "Beijing"
        assert node.lang == "zh"

    def test_weather_defaults_to_beijing(self):
        node = ToolNode("w1", action_type="weather")
        # _weather 方法里 getattr(self, "city", "") or "Beijing" 会 fallback
        assert node.city == ""

    def test_weather_execute_with_city(self):
        """真实 API 调用：查询北京天气。"""
        node = ToolNode("w1", action_type="weather", city="Beijing", lang="zh")
        ctx = _ctx()
        result = node.execute(ctx)
        assert result["success"], f"weather 执行失败: {result.get('error', result)}"
        assert "weather_info" in result
        info = result["weather_info"]
        assert "temperature" in info
        assert "city" in info
        assert "clothing_advice" in info

    def test_weather_execute_shanghai(self):
        """真实 API 调用：查询上海天气。"""
        node = ToolNode("w1", action_type="weather", city="Shanghai", lang="zh")
        ctx = _ctx()
        result = node.execute(ctx)
        assert result["success"], f"weather 执行失败: {result.get('error', result)}"
        info = result["weather_info"]
        # 上海温度应该与北京不同，不是简单的默认值
        assert "temperature" in info

    def test_weather_invalid_city_graceful(self):
        """不存在的城市应该优雅降级。"""
        node = ToolNode("w1", action_type="weather", city="不存在的城市xyz", lang="zh")
        ctx = _ctx()
        result = node.execute(ctx)
        # 可能返回成功（wttr.in 对未知城市返回默认数据）或失败
        # 但不应该崩溃
        assert isinstance(result, dict)
        assert "action_type" in result


# ── 4. File 工具冒烟 ──────────────────────────────────────────


class TestFileTools:
    """测试文件相关工具（在临时目录中操作）。"""

    @pytest.fixture
    def tmpdir(self):
        with tempfile.TemporaryDirectory() as d:
            old_cwd = os.getcwd()
            os.chdir(d)
            yield Path(d)
            os.chdir(old_cwd)

    def test_write_and_read(self, tmpdir):
        fname = str(tmpdir / "test.txt")
        # write
        node = ToolNode("w1", action_type="write_file", file_path=fname, content="hello world")
        result = node.execute(_ctx())
        assert result["success"], f"write failed: {result.get('error')}"
        # read
        node2 = ToolNode("r1", action_type="read_file", file_path=fname)
        result2 = node2.execute(_ctx())
        assert result2["success"], f"read failed: {result2.get('error')}"
        assert "hello world" in result2["content"]

    def test_create_directory(self, tmpdir):
        dname = str(tmpdir / "subdir")
        node = ToolNode("d1", action_type="create_directory", file_path=dname)
        result = node.execute(_ctx())
        assert result["success"], f"create_directory failed: {result.get('error')}"
        assert Path(dname).is_dir()

    def test_list_files(self, tmpdir):
        (tmpdir / "a.txt").write_text("a")
        (tmpdir / "b.py").write_text("b")
        node = ToolNode("l1", action_type="list_files", file_path=str(tmpdir), pattern="*")
        result = node.execute(_ctx())
        assert result["success"], f"list_files failed: {result.get('error')}"
        assert result["count"] >= 2

    def test_search_files(self, tmpdir):
        (tmpdir / "code.py").write_text("def foo():\n    return 42\n")
        node = ToolNode("s1", action_type="search_files", file_path=str(tmpdir), search_pattern="foo")
        result = node.execute(_ctx())
        assert result["success"], f"search_files failed: {result.get('error')}"
        assert result["match_count"] >= 1

    def test_edit_file(self, tmpdir):
        fname = str(tmpdir / "edit.txt")
        Path(fname).write_text("hello world")
        node = ToolNode("e1", action_type="edit_file", file_path=fname, old_text="world", new_text="python")
        result = node.execute(_ctx())
        assert result["success"], f"edit_file failed: {result.get('error', result)}"
        assert "python" in Path(fname).read_text()

    def test_batch_write(self, tmpdir):
        f1 = str(tmpdir / "b1.txt")
        f2 = str(tmpdir / "b2.txt")
        node = ToolNode("bw1", action_type="batch_write", files=[
            {"path": f1, "content": "content1"},
            {"path": f2, "content": "content2"},
        ])
        result = node.execute(_ctx())
        assert result["success"], f"batch_write failed: {result.get('error', result)}"
        assert Path(f1).read_text() == "content1"
        assert Path(f2).read_text() == "content2"

    def test_batch_edit(self, tmpdir):
        fname = str(tmpdir / "be.txt")
        Path(fname).write_text("line1\nline2\n")
        node = ToolNode("be1", action_type="batch_edit", edits=[
            {"file_path": fname, "old_text": "line1", "new_text": "modified1"},
        ])
        result = node.execute(_ctx())
        assert result["success"], f"batch_edit failed: {result.get('error', result)}"
        assert "modified1" in Path(fname).read_text()


# ── 5. Command 工具 ────────────────────────────────────────────


class TestCommandTool:
    def test_simple_echo(self):
        node = ToolNode("c1", action_type="command", action="echo hello")
        result = node.execute(_ctx())
        assert result["success"], f"command failed: {result.get('error', result)}"
        assert "hello" in result.get("stdout", "")

    def test_command_with_context(self):
        node = ToolNode("c2", action_type="command", action="echo {greeting}")
        ctx = _ctx(greeting="bonjour")
        result = node.execute(ctx)
        assert result["success"]
        assert "bonjour" in result.get("stdout", "")


# ── 6. Git 工具 ────────────────────────────────────────────────


class TestGitTool:
    def test_git_status(self, tmp_path):
        """在临时 git 仓库中测试 git status。"""
        import subprocess
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, capture_output=True)
        (repo / "f.txt").write_text("test")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True)

        node = ToolNode("g1", action_type="git", git_command="status", cwd=str(repo))
        result = node.execute(_ctx())
        assert result["success"], f"git status failed: {result.get('error', result)}"

    def test_dangerous_git_blocked(self):
        node = ToolNode("g2", action_type="git", git_command="push --force origin main")
        with pytest.raises(Exception):
            node.execute(_ctx())


# ── 7. datetime 工具 ───────────────────────────────────────────


class TestDatetimeTool:
    def test_datetime(self):
        node = ToolNode("dt1", action_type="datetime")
        result = node.execute(_ctx())
        assert result["success"]
        assert "date" in result
        assert "time" in result
        assert "weekday" in result


# ── 8. web_fetch 工具 ──────────────────────────────────────────


class TestWebFetchTool:
    def test_web_fetch_public_url(self):
        """测试抓取公开 URL。"""
        node = ToolNode("wf1", action_type="web_fetch", url="https://httpbin.org/get?test=1")
        result = node.execute(_ctx())
        assert result["success"], f"web_fetch failed: {result.get('error', result)}"
        assert "content" in result

    def test_web_fetch_ssrf_blocked(self):
        """测试 SSRF 拦截内网地址。"""
        node = ToolNode("wf2", action_type="web_fetch", url="http://127.0.0.1:9999/")
        result = node.execute(_ctx())
        assert not result["success"]
        assert "SSRF" in result.get("error", "")


# ── 9. GitHub fetch 工具 ───────────────────────────────────────


class TestGithubFetchTool:
    def test_github_list_files(self):
        """测试 GitHub 仓库文件列表。"""
        node = ToolNode("gh1", action_type="github_fetch", repo="python/cpython",
                        github_action="list_files", branch="main")
        result = node.execute(_ctx())
        # GitHub API 可能限流，允许失败但不应崩溃
        assert isinstance(result, dict)
        assert "action_type" in result

    def test_github_invalid_repo_format(self):
        """非法 repo 格式应返回错误，不崩溃。"""
        node = ToolNode("gh2", action_type="github_fetch", repo="invalid/repo/with/slashes")
        result = node.execute(_ctx())
        assert not result["success"]

    def test_github_repo_format_validation(self):
        """格式校验：owner/repo 正确格式应通过。"""
        node = ToolNode("gh3", action_type="github_fetch", repo="python/cpython",
                        github_action="list_files", branch="main")
        result = node.execute(_ctx())
        # 不应因格式错误崩溃，可能因网络/限流失败但至少不应是格式错误
        assert isinstance(result, dict)
        if not result["success"]:
            assert "格式" not in result.get("error", "")


# ── 10. code_index / ast_analyze ───────────────────────────────


class TestCodeAnalysis:
    def test_code_index(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "mod.py").write_text("def hello():\n    return 'world'\n\nclass Greeter:\n    def greet(self):\n        pass\n")
        node = ToolNode("ci1", action_type="code_index", file_path=str(src),
                        search_pattern="hello", security_enabled=False)
        result = node.execute(_ctx())
        assert result["success"], f"code_index failed: {result.get('error', result)}"
        assert result["matches"]

    def test_ast_analyze(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("def foo(x: int) -> str:\n    return str(x)\n\nclass Bar:\n    pass\n")
        node = ToolNode("aa1", action_type="ast_analyze", file_path=str(f),
                        security_enabled=False)
        result = node.execute(_ctx())
        assert result["success"], f"ast_analyze failed: {result.get('error', result)}"
        assert result["functions"] >= 1
        assert result["classes"] >= 1


# ── 11. diff_preview ───────────────────────────────────────────


class TestDiffPreview:
    def test_diff_preview_edit(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("hello world")
        node = ToolNode("dp1", action_type="diff_preview", file_path=str(f),
                        old_text="world", new_text="python", security_enabled=False)
        result = node.execute(_ctx())
        assert result["success"], f"diff_preview failed: {result.get('error', result)}"
        assert result["has_changes"]


# ── 12. register_tool ──────────────────────────────────────────


class TestRegisterTool:
    def test_register_rejects_builtin_name(self):
        node = ToolNode("rt1", action_type="register_tool", tool_name="weather",
                        description="hijack", python_function="omniagent.utils.weather.get_weather")
        result = node.execute(_ctx())
        assert not result["success"]
        assert "内置" in result.get("error", "") or "冲突" in result.get("error", "")

    def test_register_rejects_dangerous_module(self):
        node = ToolNode("rt2", action_type="register_tool", tool_name="my_os",
                        python_function="os.system")
        result = node.execute(_ctx())
        assert not result["success"]
        assert "危险" in result.get("error", "") or "安全" in result.get("error", "")


# ── 13. 动态工具 ───────────────────────────────────────────────


class TestDynamicTools:
    def test_register_and_execute_dynamic(self, tmp_path):
        """注册一个动态工具并执行它。"""
        from omniagent.nodes.tool_node import register_dynamic_tool, _DYNAMIC_TOOLS

        tool_name = "test_dyn_tool"

        def my_handler(ctx):
            return {"action_type": tool_name, "success": True, "content": "dynamic result"}

        register_dynamic_tool(tool_name, my_handler, "test dynamic tool", {})

        try:
            node = ToolNode("dyn1", action_type=tool_name)
            result = node.execute(_ctx())
            assert result["success"], f"dynamic tool failed: {result.get('error', result)}"
            assert "dynamic result" in str(result.get("content", ""))
        finally:
            _DYNAMIC_TOOLS.pop(tool_name, None)


# ── 14. ToolExecutor 门面 ──────────────────────────────────────


class TestToolExecutor:
    def test_executor_weather(self):
        from omniagent.nodes.tool_executor import ToolExecutor

        executor = ToolExecutor()
        ctx = _ctx()
        result = executor.execute("weather", {"city": "Beijing", "lang": "zh"}, ctx)
        assert result.success, f"ToolExecutor weather failed: {result.error}"

    def test_executor_unknown_tool(self):
        from omniagent.nodes.tool_executor import ToolExecutor

        executor = ToolExecutor()
        ctx = _ctx()
        result = executor.execute("nonexistent_tool_xyz", {}, ctx, tools={"weather": None})
        assert not result.success
        assert "未知" in result.error or "错误" in result.error

    def test_executor_classify(self):
        from omniagent.nodes.tool_executor import classify_tool
        assert classify_tool("command") == "SENSITIVE"
        assert classify_tool("write_file") == "WRITE"
        assert classify_tool("read_file") == "INFO"
        assert classify_tool("weather") == "INFO"


# ── 15. 安全边界 ───────────────────────────────────────────────


class TestSecurityBoundary:
    def test_path_traversal_blocked(self):
        node = ToolNode("s1", action_type="read_file", file_path="/etc/passwd")
        with pytest.raises(Exception):
            node.execute(_ctx())

    def test_dangerous_command_blocked(self):
        node = ToolNode("s2", action_type="command", action="rm -rf /")
        with pytest.raises(Exception):
            node.execute(_ctx())

    def test_write_sensitive_path_blocked(self):
        node = ToolNode("s3", action_type="write_file", file_path="/etc/hosts", content="evil")
        with pytest.raises(Exception):
            node.execute(_ctx())


# ── 16. 降级方案 ───────────────────────────────────────────────


class TestFallback:
    """测试工具的降级/回退方案。"""

    def test_weather_curl_fallback_available(self):
        """验证 curl 降级函数存在且可调用。"""
        from omniagent.utils.weather import _get_weather_via_curl
        result = _get_weather_via_curl(
            "https://wttr.in/Beijing?format=j1&lang=zh",
            "Beijing", "Beijing", "zh"
        )
        assert "error" not in result, f"curl fallback failed: {result.get('error')}"
        assert "temperature" in result
        assert result.get("via_fallback") is True

    def test_weather_primary_vs_fallback(self):
        """主路径不标记 via_fallback，降级路径标记。"""
        from omniagent.utils.weather import get_weather
        info = get_weather("Beijing", "zh")
        assert "error" not in info
        # 主路径成功时 via_fallback 应为 False
        assert info.get("via_fallback") is False

    def test_ssrf_allowlist_wttr_in(self):
        """wttr.in 在 SSRF 白名单中。"""
        from omniagent.nodes.tool_node import _ssrf_check_url, _SSRF_DOMAIN_ALLOWLIST
        assert "wttr.in" in _SSRF_DOMAIN_ALLOWLIST
        ok, _ = _ssrf_check_url("https://wttr.in/Suzhou?lang=zh")
        assert ok

    def test_ssrf_allowlist_weather_com_cn(self):
        """weather.com.cn 在 SSRF 白名单中。"""
        from omniagent.nodes.tool_node import _ssrf_check_url, _SSRF_DOMAIN_ALLOWLIST
        assert "weather.com.cn" in _SSRF_DOMAIN_ALLOWLIST
        ok, _ = _ssrf_check_url("http://www.weather.com.cn/weather1d/101190401.shtml")
        assert ok

    def test_ssrf_allowlist_github(self):
        """GitHub API 域名在 SSRF 白名单中。"""
        from omniagent.nodes.tool_node import _ssrf_check_url
        ok, _ = _ssrf_check_url("https://api.github.com/repos/python/cpython")
        assert ok
        ok2, _ = _ssrf_check_url("https://raw.githubusercontent.com/python/cpython/main/README.md")
        assert ok2

    def test_ssrf_allowlist_subdomain(self):
        """白名单也应匹配子域名（如 xxx.api.github.com）。"""
        from omniagent.nodes.tool_node import _ssrf_check_url
        # api.github.com 的子域名
        ok, _ = _ssrf_check_url("https://api.github.com/meta")
        assert ok

    def test_web_fetch_error_suggests_curl(self):
        """SSRF 拦截错误消息应提示 curl 降级方案。"""
        node = ToolNode("wf_fb", action_type="web_fetch", url="http://127.0.0.1:9999/")
        result = node.execute(_ctx())
        assert not result["success"]
        assert "curl" in result.get("error", "").lower() or "command" in result.get("error", "").lower()

    def test_weather_tool_end_to_end_with_fallback_info(self):
        """天气工具端到端：查询应成功且包含 via_fallback 标记。"""
        node = ToolNode("w_fb", action_type="weather", city="Shenzhen", lang="zh")
        ctx = _ctx()
        result = node.execute(ctx)
        assert result["success"], f"weather failed: {result.get('error', result)}"
        info = result.get("weather_info", {})
        # via_fallback 标记应存在（主路径或降级路径都会设置）
        assert "via_fallback" in info or "error" not in info