"""
子 Agent 系统测试 — 结构化结果、上下文注入、混合通知。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from omniagent.engine.subagent import (
    BackgroundTaskRegistry,
    SubagentNotifier,
    _build_context_seed_prompt,
    _parse_structured_result,
)


class TestStructuredResult:
    """结构化结果解析测试。"""

    def test_parse_valid_json(self):
        """正确解析 JSON 块。"""
        text = """
Blah blah...
```json
{
  "summary": "已完成搜索",
  "files_modified": ["a.py"],
  "files_created": [],
  "key_findings": ["找到3个文件"],
  "errors": [],
  "next_steps": ["检查b.py"]
}
```
        """
        result = _parse_structured_result(text)
        assert result["summary"] == "已完成搜索"
        assert result["files_modified"] == ["a.py"]
        assert result["key_findings"] == ["找到3个文件"]
        assert result["next_steps"] == ["检查b.py"]

    def test_parse_inline_json(self):
        """解析内联 JSON（无代码块标记）。"""
        text = '{"summary": "done", "files_modified": [], "files_created": [], "key_findings": [], "errors": [], "next_steps": []}'
        result = _parse_structured_result(text)
        assert result["summary"] == "done"

    def test_parse_fallback_no_json(self):
        """无法解析 JSON 时回退到全文摘要。"""
        text = "这是一段普通的文本结果，没有 JSON 结构。"
        result = _parse_structured_result(text)
        assert "这是一段普通的文本结果" in result["summary"]
        assert result["files_modified"] == []
        assert result["errors"] == []

    def test_parse_empty_string(self):
        """空字符串安全。"""
        result = _parse_structured_result("")
        assert result["summary"] == ""
        assert result["files_modified"] == []


class TestContextSeed:
    """Context Seed 注入测试。"""

    def test_full_seed(self):
        """完整的 context_seed 生成正确的注入文本。"""
        seed = {
            "parent_goal": "修复 auth 模块 bug",
            "discovered_files": ["src/auth.py", "tests/test_auth.py"],
            "constraints": ["不要修改 API", "保持向后兼容"],
            "working_directory": "/project",
        }
        text = _build_context_seed_prompt(seed)
        assert "[父 Agent 上下文]" in text
        assert "修复 auth 模块 bug" in text
        assert "src/auth.py" in text
        assert "不要修改 API" in text
        assert "/project" in text

    def test_empty_seed(self):
        """空 seed 返回空字符串。"""
        assert _build_context_seed_prompt(None) == ""
        assert _build_context_seed_prompt({}) == ""

    def test_minimal_seed(self):
        """只有目标的 seed。"""
        seed = {"parent_goal": "test goal"}
        text = _build_context_seed_prompt(seed)
        assert "test goal" in text
        assert "parent_goal" not in text  # key 不应出现在输出中


class TestBackgroundTaskRegistry:
    """BackgroundTaskRegistry 测试。"""

    def test_create_and_get_task(self):
        """创建任务后能正确获取。"""
        registry = BackgroundTaskRegistry()
        task = registry.create_task("测试目标", "parent-run-1")
        assert task.task_id.startswith("subagent-")
        assert task.status == "pending"
        assert task.goal == "测试目标"

        retrieved = registry.get_task(task.task_id)
        assert retrieved is not None
        assert retrieved.goal == "测试目标"

    def test_mark_running_and_done(self):
        """任务状态流转正确。"""
        registry = BackgroundTaskRegistry()
        task = registry.create_task("测试", "run-1")
        registry.mark_running(task.task_id)
        assert registry.get_task(task.task_id).status == "running"

        registry.mark_done(task.task_id, "结果文本", success=True)
        t = registry.get_task(task.task_id)
        assert t.status == "success"
        assert t.result == "结果文本"
        assert t.finished_at != ""

    def test_list_tasks_filtered_by_parent(self):
        """按 parent_run_id 过滤任务。"""
        registry = BackgroundTaskRegistry()
        registry.create_task("task A", "parent-1")
        registry.create_task("task B", "parent-2")
        registry.create_task("task C", "parent-1")

        parent1_tasks = registry.list_tasks(parent_run_id="parent-1")
        assert len(parent1_tasks) == 2
        parent2_tasks = registry.list_tasks(parent_run_id="parent-2")
        assert len(parent2_tasks) == 1

    def test_active_count(self):
        """active_count 正确计算活跃任务数。"""
        registry = BackgroundTaskRegistry()
        assert registry.active_count == 0
        registry.create_task("task 1", "run-1")
        assert registry.active_count == 1
        registry.create_task("task 2", "run-1")
        assert registry.active_count == 2


class TestSubagentNotifier:
    """SubagentNotifier 混合通知测试。"""

    def test_on_complete_adds_to_cache(self):
        """完成通知加入缓存。"""
        registry = BackgroundTaskRegistry()
        notifier = SubagentNotifier(registry)
        task = registry.create_task("test", "run-1")
        notifier.on_complete(task)
        assert notifier.has_pending is True

    def test_poll_completed_returns_and_removes(self):
        """poll 返回完成的任务并从缓存移除。"""
        registry = BackgroundTaskRegistry()
        notifier = SubagentNotifier(registry)
        task = registry.create_task("test", "run-1")
        notifier.on_complete(task)

        results = notifier.poll_completed([task.task_id])
        assert task.task_id in results
        assert notifier.has_pending is False  # 已消耗

    def test_poll_completed_checks_registry_fallback(self):
        """poll 在 registry 中检查（未 push 但已完成）。"""
        registry = BackgroundTaskRegistry()
        notifier = SubagentNotifier(registry)
        task = registry.create_task("test", "run-1")
        registry.mark_running(task.task_id)
        registry.mark_done(task.task_id, "done")

        results = notifier.poll_completed([task.task_id])
        assert task.task_id in results
        assert results[task.task_id].status == "success"

    def test_poll_unknown_task_returns_empty(self):
        """未知 task_id 返回空。"""
        registry = BackgroundTaskRegistry()
        notifier = SubagentNotifier(registry)
        results = notifier.poll_completed(["nonexistent"])
        assert len(results) == 0

    def test_clear_removes_all(self):
        """clear 清空缓存。"""
        registry = BackgroundTaskRegistry()
        notifier = SubagentNotifier(registry)
        task = registry.create_task("test", "run-1")
        notifier.on_complete(task)
        notifier.clear()
        assert notifier.has_pending is False
