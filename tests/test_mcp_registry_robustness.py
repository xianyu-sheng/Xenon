"""MCPRegistry 对齐修复的针对性测试。

覆盖评审提出的高优先级问题：
1. 短名歧义 → 拒绝隐式路由，强制 server:tool 全名（disambiguation）
2. 唯一短名仍可用（向后兼容）
3. discover 类型守卫：不合规工具条目跳过而非崩溃
4. call_tool 对缺失 name 的防御（不 KeyError）
5. evidence 记录脱敏（异常与结果摘要）
6. 并发锁：共享状态在多线程下不撕裂
7. infer_category 词边界匹配（"web" 不误中 "webhook"）
"""

from __future__ import annotations

import threading

import pytest

from xenon.mcp.registry import (
    MCPRegistry,
    _redact_text,
    infer_category,
)


class _FakeClient:
    """最小 MCPClient 替身：按构造参数返回固定工具列表。"""

    def __init__(self, tools: list[dict], fail_list: bool = False) -> None:
        self._tools = tools
        self._fail_list = fail_list
        self.calls: list[tuple[str, dict | None]] = []

    def list_tools(self) -> list[dict]:
        if self._fail_list:
            raise RuntimeError("list_tools timeout")
        return list(self._tools)

    def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        self.calls.append((name, arguments))
        return {"content": [{"type": "text", "text": f"ok:{name}"}]}

    def close(self) -> None:  # pragma: no cover - trivial
        pass


def _make_registry():
    reg = MCPRegistry()
    reg.clients["svc_a"] = _FakeClient([{"name": "read", "description": "read a"}])
    reg.clients["svc_b"] = _FakeClient([{"name": "read", "description": "read b"}])
    reg.discover_tools()
    return reg


class TestShortNameDisambiguation:
    def test_ambiguous_short_name_needs_full_name(self):
        reg = _make_registry()
        assert "read" in reg.ambiguous_short_names
        # 短名已从 tool_map 移除，避免隐式路由
        assert "read" not in reg.tool_map
        with pytest.raises(ValueError, match="歧义"):
            reg.call_tool("read")

    def test_full_name_still_routes_deterministically(self):
        reg = _make_registry()
        r1 = reg.call_tool("svc_a:read")
        r2 = reg.call_tool("svc_b:read")
        assert "ok:read" in str(r1) and "ok:read" in str(r2)
        assert reg.clients["svc_a"].calls[0][0] == "read"
        assert reg.clients["svc_b"].calls[0][0] == "read"

    def test_unique_short_name_still_works(self):
        reg = MCPRegistry()
        reg.clients["only"] = _FakeClient([{"name": "unique_tool"}])
        reg.discover_tools()
        assert "unique_tool" in reg.tool_map  # 唯一短名保留
        assert "unique_tool" not in reg.ambiguous_short_names
        result = reg.call_tool("unique_tool")
        assert "ok:unique_tool" in str(result)

    def test_error_message_offers_disambiguation(self):
        reg = _make_registry()
        with pytest.raises(ValueError) as excinfo:
            reg.call_tool("read")
        msg = str(excinfo.value)
        assert "svc_a" in msg and "svc_b" in msg
        assert "server:tool" in msg or "svc_a:read" in msg

    def test_removed_server_no_longer_blocks_short_name(self):
        """/mcp remove 后重新 discover：残留归属不应继续让短名判为歧义。"""
        reg = _make_registry()
        # 模拟 /mcp remove svc_b：断开、删除、重建映射
        reg.clients["svc_b"].close()
        del reg.clients["svc_b"]
        reg.tool_map.clear()
        reg.discover_tools()
        # 只剩 svc_a 提供 read → 短名恢复可用
        assert "read" not in reg.ambiguous_short_names
        assert "read" in reg.tool_map
        result = reg.call_tool("read")
        assert "ok:read" in str(result)


class TestTypeGuard:
    def test_non_dict_tool_skipped(self):
        reg = MCPRegistry()
        reg.clients["bad"] = _FakeClient([{"name": "ok"}, "not-a-dict", None])
        reg.discover_tools()
        assert "bad:ok" in reg.tool_map
        assert "bad:not-a-dict" not in reg.tool_map

    def test_missing_name_tool_skipped(self):
        reg = MCPRegistry()
        reg.clients["bad"] = _FakeClient([{"description": "no name"}, {"name": "fine"}])
        reg.discover_tools()
        assert "bad:fine" in reg.tool_map
        # 无 name 条目不再以 "unknown" 占位注册
        assert "bad:unknown" not in reg.tool_map
        # 唯一短名 fine 仍注册为别名（向后兼容）
        assert "fine" in reg.tool_map

    def test_call_tool_defensive_when_name_missing(self):
        reg = MCPRegistry()
        reg.clients["s"] = _FakeClient([{"name": "t"}])
        reg.discover_tools()
        # tool_info 缺 name 时用调用名兜底，不 KeyError
        reg.tool_map["s:t"] = ("s", {"description": "no name key"})
        result = reg.call_tool("s:t")
        assert "ok:t" in str(result)

    def test_failed_discovery_does_not_abort_others(self):
        reg = MCPRegistry()
        reg.clients["failing"] = _FakeClient([], fail_list=True)
        reg.clients["ok"] = _FakeClient([{"name": "t"}])
        reg.discover_tools()
        assert "ok:t" in reg.tool_map
        assert "failing" not in reg.tool_map


class TestRedaction:
    def test_exception_summary_redacted(self):
        class Boom:
            def call_tool(self, name, arguments=None):
                raise RuntimeError("auth failed: api_key=sk-SUPERSECRET123")

        reg = MCPRegistry()

        class Ctx:
            def __init__(self):
                self.obs = []

            @property
            def evidence(self):
                return self

            def record_tool_request(self, **kw):
                pass

            def record_tool_observation(self, **kw):
                self.obs.append(kw)

        reg.clients["s"] = Boom()  # type: ignore[assignment]
        reg.tool_map["s:read"] = ("s", {"name": "read"})
        ctx = Ctx()
        with pytest.raises(RuntimeError):
            reg.call_tool("s:read", context=ctx)
        assert len(ctx.obs) == 1
        assert "sk-SUPERSECRET123" not in ctx.obs[0]["summary"]
        assert "<redacted>" in ctx.obs[0]["summary"]

    def test_success_summary_redacted(self):
        class Leaky:
            def call_tool(self, name, arguments=None):
                return {"content": [{"type": "text", "text": "ok token=abc123"}]}

        reg = MCPRegistry()

        class Ctx:
            def __init__(self):
                self.obs = []

            @property
            def evidence(self):
                return self

            def record_tool_request(self, **kw):
                pass

            def record_tool_observation(self, **kw):
                self.obs.append(kw)

        reg.clients["s"] = Leaky()  # type: ignore[assignment]
        reg.tool_map["s:read"] = ("s", {"name": "read"})
        ctx = Ctx()
        reg.call_tool("s:read", context=ctx)
        assert len(ctx.obs) == 1
        assert ctx.obs[0]["success"] is True
        assert "abc123" not in ctx.obs[0]["summary"]
        assert "<redacted>" in ctx.obs[0]["summary"]

    def test_redact_text_forms(self):
        assert "api_key=<redacted>" in _redact_text("err api_key=sk-123")
        assert "<redacted>" in _redact_text("Authorization: Bearer abc.def.ghi")
        assert "token=<redacted>" in _redact_text("http://h?token=xyz&a=b")


class TestConcurrency:
    def test_parallel_discover_no_crash(self):
        reg = MCPRegistry()
        for i in range(8):
            reg.clients[f"s{i}"] = _FakeClient(
                [{"name": f"tool_{i}_a"}, {"name": "shared"}]
            )

        errors: list[Exception] = []
        barrier = threading.Barrier(8)

        def worker():
            try:
                barrier.wait(timeout=5)
                reg.discover_tools()
            except Exception as e:  # pragma: no cover - 失败才走到
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert not errors
        # 所有全名都在（RLock 保护下不丢半写状态）
        for i in range(8):
            assert f"s{i}:tool_{i}_a" in reg.tool_map
        # "shared" 是全部 server 都有的短名 → 歧义，必须从 tool_map 移除
        assert "shared" not in reg.tool_map
        assert "shared" in reg.ambiguous_short_names

    def test_add_server_parallel_safe(self):
        reg = MCPRegistry()

        def add(i: int) -> None:
            for _ in range(50):
                reg.add_server(f"s{i % 4}", command="echo")

        threads = [threading.Thread(target=add, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        assert len(reg.clients) == 4  # 4 个不同名字


class TestCategoryWordBoundary:
    def test_substring_no_longer_matches(self):
        # 旧实现 "web" in "webhook" → True（误分类）；词边界后应为 other
        assert infer_category("webhook", "") == "other"
        assert infer_category("web_search", "") != "other"
        # 描述内的子串也不穿越：中文描述 + webhook 不应触发 "web"
        assert infer_category("wx", "微信小程序 webhook 订阅") == "other"

    def test_normal_categories_still_work(self):
        assert infer_category("read_file", "") == "read_file"
        assert infer_category("run_shell", "") == "command"
        assert infer_category("git_status", "") == "git"
        assert (
            infer_category("fetch_url", "") == "read_file"
        )  # read_file 先于 web 命中 "fetch"


class TestToolSetShrink:
    """回归：server 工具集收缩后 tool_map 不得残留幽灵条目。

    根因：discover_tools() 重建 _short_name_owners/ambiguous_short_names/
    tool_categories 三个共享态，但 tool_map 只增不删（registry.py:306），
    只有 close_all()（registry.py:479）才清空。server 热更新工具集或
    断线重连后，已消失的工具仍可被 call_tool 路由到。
    """

    def test_shrunk_tool_not_left_in_tool_map(self):
        reg = MCPRegistry()
        client = _FakeClient(
            [
                {"name": "read", "description": "r"},
                {"name": "write", "description": "w"},
            ]
        )
        reg.clients["fs"] = client
        reg.discover_tools()
        assert "fs:write" in reg.tool_map and "write" in reg.tool_map

        # server 撤掉 write 工具后重新发现
        client._tools = [{"name": "read", "description": "r"}]
        reg.discover_tools()

        assert "fs:write" not in reg.tool_map, "全名幽灵条目残留"
        assert "write" not in reg.tool_map, "短名幽灵条目残留"
        assert sorted(reg.tool_map.keys()) == ["fs:read", "read"]

    def test_shrunk_tool_call_rejected_after_rediscover(self):
        reg = MCPRegistry()
        client = _FakeClient(
            [
                {"name": "read", "description": "r"},
                {"name": "write", "description": "w"},
            ]
        )
        reg.clients["fs"] = client
        reg.discover_tools()

        client._tools = [{"name": "read", "description": "r"}]
        reg.discover_tools()

        # 幽灵调用应被既有未知工具防御拒绝（ValueError + 可用列表提示）
        with pytest.raises(ValueError, match="未知 MCP 工具"):
            reg.call_tool("fs:write")

    def test_tool_set_growth_still_accumulates(self):
        reg = MCPRegistry()
        client = _FakeClient([{"name": "read", "description": "r"}])
        reg.clients["fs"] = client
        reg.discover_tools()

        client._tools.append({"name": "stat", "description": "s"})
        reg.discover_tools()

        assert {"fs:read", "read", "fs:stat", "stat"} <= set(reg.tool_map.keys())

    def test_failed_server_keeps_others_tools(self):
        """单 server 发现失败不得把其他 server 的工具一并清掉。"""
        reg = MCPRegistry()
        ok_client = _FakeClient([{"name": "read", "description": "r"}])
        bad_client = _FakeClient([], fail_list=True)
        reg.clients["good"] = ok_client
        reg.clients["bad"] = bad_client
        reg.discover_tools()

        assert "good:read" in reg.tool_map
        # bad server 无工具 → 不产生条目，也不影响 good
        assert not any(k.startswith("bad:") for k in reg.tool_map)
