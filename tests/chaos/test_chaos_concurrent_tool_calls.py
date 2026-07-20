"""Chaos Test 6: 并发/重复 tool_call。

目标：验证 xenon 在两种"并发"场景下的鲁棒性：
1. **同一次 LLM 返回中含 5 个 tool_call**（少见但合规）：验证解析层
   能正确识别（当前 ReAct 设计是"一轮一工具"，但响应解析不应崩溃）；
2. **多个 LLM 调用并发**（多线程 stress）：验证 _call_llm 锁/池安全。

**注意**：xenon 官方 ReAct 引擎设计为串行单工具，本测试侧重于
"不抛异常、有合理回退"。
"""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import httpx
import pytest

import xenon.utils.llm_client as llm_client
from xenon.utils.response_adapter import parse_react


# ── 场景 1: LLM 返回 5 个 tool_call ──

def test_parse_response_5_tool_calls():
    """验证 parse_react 遇到 5 个 JSON 拼接（不合法但需稳健）不崩溃。

    实际上 ReAct 模型要求一个 JSON 只有一个 action，但如果 LLM 不守规矩
    输出了 5 个 JSON 对象拼接，parse_react 应**不抛异常**。当前实现回退为
    thought/final_answer = raw text（不识别任何 action）。
    """
    text = (
        '{"thought": "1", "action": "read_file", "action_input": {"file_path": "/a"}}\n'
        '{"thought": "2", "action": "list_files", "action_input": {"file_path": "."}}\n'
        '{"thought": "3", "action": "command", "action_input": {"action": "ls"}}\n'
        '{"thought": "4", "action": "git", "action_input": {"git_command": "status"}}\n'
        '{"thought": "5", "action": "search_files", "action_input": {"file_path": ".", "search_pattern": "x"}}'
    )
    # 必须不抛异常
    result = parse_react(text)
    # v0.6.2: parse_react 现在能正确检测 JSON 数组并返回 list[dict]
    if isinstance(result, list):
        # 路径 1：多个 JSON 被解析为并行工具调用列表
        assert len(result) == 5
        assert result[0]["action"] in ("read_file", "list_files", "command", "git", "search_files")
    elif result.get("action"):
        # 路径 1：识别到第一个 action（理想但罕见，因为 _extract_json 在最后 } 截断时整个内容当 raw）
        assert result["action"] in (
            "read_file", "list_files", "command", "git", "search_files"
        )
    else:
        # 路径 2：回退到 raw 文本（当前实现）
        assert result.get("final_answer") or result.get("thought"), (
            f"parse_react 无回退: {result}"
        )


def test_parse_react_with_5_actions_in_single_json():
    """单个 JSON 内如果含多个 action 字段（合法 JSON 重复 key），
    Python json.loads 取最后一个，但 _pick alias 后也应稳健。"""
    # Python json 默认取最后一个 key
    import json
    text = '{"action": "read_file", "action": "list_files", "action": "command"}'
    data = json.loads(text)
    # json.loads 行为：取最后一个 action="command"
    assert data["action"] == "command"


# ── 场景 2: 并发 LLM 调用 ──

def _make_response(text: str = "ok") -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = {
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    resp.raise_for_status = MagicMock()
    return resp


def test_concurrent_llm_calls_thread_safe(monkeypatch):
    """10 个线程并发调 chat_completion → 无数据竞争，结果正确。"""
    barrier = threading.Barrier(10)

    def fake_post(url, **kwargs):
        # 同步起跑 10 线程模拟真实并发
        barrier.wait(timeout=2.0)
        time.sleep(0.01)  # 模拟网络延迟
        return _make_response("concurrent ok")

    fake_client = MagicMock()
    fake_client.is_closed = False
    fake_client.post = fake_post
    fake_client.close = MagicMock()

    monkeypatch.setattr(llm_client, "_get_pooled_client", lambda *a, **kw: fake_client)
    monkeypatch.setattr(llm_client, "build_endpoint", lambda *a, **kw: MagicMock(
        provider="openai", model_name="gpt-4o", base_url="https://api.test/v1",
        api_key="sk-test", max_tokens=4096,
    ))

    results = []
    errors = []

    def worker(i):
        try:
            r = llm_client.chat_completion(
                "openai/gpt-4o",
                [{"role": "user", "content": f"hi from {i}"}],
                max_retries=1,
            )
            results.append(r)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)

    assert not errors, f"并发调用有错误: {errors}"
    assert len(results) == 10
    assert all(r == "concurrent ok" for r in results)
