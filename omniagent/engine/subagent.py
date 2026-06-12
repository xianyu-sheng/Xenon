"""子 Agent 系统 — 借鉴 KamaClaude 的 SpawnAgentTool + BackgroundTaskRegistry。

支持:
- 主 Agent 派生子 Agent 处理独立子任务
- 子 Agent 后台运行，结果通过 Future 回调
- BackgroundTaskRegistry 管理跨 run 的子任务
- AgentResultTool 查询子任务结果
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from omniagent.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class SubagentTask:
    """子 Agent 任务描述。"""
    task_id: str
    goal: str
    parent_run_id: str
    status: str = "pending"  # pending | running | success | failed
    result: str = ""
    error: str | None = None
    created_at: str = field(default_factory=_now)
    finished_at: str = ""


class BackgroundTaskRegistry:
    """后台子 Agent 任务注册表（跨 run 共享）。"""

    def __init__(self) -> None:
        self._tasks: dict[str, SubagentTask] = {}
        self._futures: dict[str, asyncio.Future[str]] = {}
        self._max_concurrent: int = 5
        self._semaphore = asyncio.Semaphore(self._max_concurrent)

    @property
    def active_count(self) -> int:
        return sum(1 for t in self._tasks.values() if t.status in ("pending", "running"))

    @property
    def total_count(self) -> int:
        return len(self._tasks)

    def create_task(self, goal: str, parent_run_id: str) -> SubagentTask:
        """创建子任务并返回 task_id。"""
        task_id = f"subagent-{uuid.uuid4().hex[:8]}"
        task = SubagentTask(
            task_id=task_id,
            goal=goal,
            parent_run_id=parent_run_id,
        )
        self._tasks[task_id] = task
        logger.info(f"子 Agent 任务已创建: {task_id} — {goal[:60]}")
        return task

    def get_task(self, task_id: str) -> SubagentTask | None:
        """查询子任务。"""
        return self._tasks.get(task_id)

    def list_tasks(self, parent_run_id: str | None = None) -> list[SubagentTask]:
        """列出子任务，可按父 run 过滤。"""
        tasks = list(self._tasks.values())
        if parent_run_id:
            tasks = [t for t in tasks if t.parent_run_id == parent_run_id]
        return sorted(tasks, key=lambda t: t.created_at, reverse=True)

    def mark_running(self, task_id: str) -> None:
        """标记任务为运行中。"""
        task = self._tasks.get(task_id)
        if task:
            task.status = "running"

    def mark_done(self, task_id: str, result: str, success: bool = True) -> None:
        """标记任务完成。"""
        task = self._tasks.get(task_id)
        if task:
            task.status = "success" if success else "failed"
            task.result = result
            task.finished_at = _now()

        future = self._futures.pop(task_id, None)
        if future and not future.done():
            future.set_result(result)

    def get_future(self, task_id: str) -> asyncio.Future[str] | None:
        """获取任务的 Future（用于等待结果）。"""
        return self._futures.get(task_id)

    def create_future(self, task_id: str) -> asyncio.Future[str]:
        """为任务创建一个 Future。"""
        future: asyncio.Future[str] = asyncio.Future()
        self._futures[task_id] = future
        return future


# ── 全局单例 ────────────────────────────────────────────────

_background_registry: BackgroundTaskRegistry | None = None


def get_background_registry() -> BackgroundTaskRegistry:
    """获取后台任务注册表单例。"""
    global _background_registry
    if _background_registry is None:
        _background_registry = BackgroundTaskRegistry()
    return _background_registry


# ── 工具 ────────────────────────────────────────────────────


class SpawnAgentTool(BaseTool):
    """派生子 Agent 处理独立子任务。

    主 Agent 可以调用此工具将子任务委派给独立的子 Agent。
    子 Agent 在后台运行，结果可通过 AgentResultTool 查询。
    """

    name = "spawn_agent"
    description = (
        "派生子 Agent 独立处理一个子任务。子 Agent 在后台运行，"
        "可使用所有工具。返回 task_id，后续用 agent_result 查询结果。"
        "适用场景: 多文件重构、独立模块编写、并行探索等。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "goal": {"type": "string", "description": "子 Agent 的任务目标"},
            "run_id": {"type": "string", "description": "父 run_id"},
        },
        "required": ["goal"],
    }

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        goal = str(params.get("goal", ""))
        parent_run_id = str(params.get("run_id", ""))

        if not goal:
            return ToolResult.schema_error("spawn_agent 需要 goal 参数")

        registry = get_background_registry()
        if registry.active_count >= 5:
            return ToolResult.error("子 Agent 数量已达上限 (5)，请等待现有任务完成")

        task = registry.create_task(goal, parent_run_id=parent_run_id)

        # 创建后台任务
        future = registry.create_future(task.task_id)
        asyncio.create_task(self._run_subagent(task, registry))

        return ToolResult.ok(
            f"子 Agent 已启动 (task_id: {task.task_id})\n目标: {goal}\n使用 agent_result 工具查询结果。",
            task_id=task.task_id,
        )

    async def _run_subagent(self, task: SubagentTask, registry: BackgroundTaskRegistry) -> None:
        """后台执行子 Agent 任务。"""
        registry.mark_running(task.task_id)
        try:
            # 简化实现: 直接调用 LLM
            from omniagent.utils.llm_client import chat_completion
            result = chat_completion(
                "deepseek/deepseek-v4-pro",
                [{"role": "user", "content": task.goal}],
                max_tokens=4096,
                temperature=0.3,
            )
            registry.mark_done(task.task_id, result, success=True)
        except Exception as e:
            registry.mark_done(task.task_id, str(e), success=False)


class AgentResultTool(BaseTool):
    """查询子 Agent 任务结果。"""

    name = "agent_result"
    description = "查询子 Agent 任务的结果。传入 task_id 获取结果，不传则列出所有子任务。"
    input_schema = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "子任务 ID（可选，不传则列出所有）"},
        },
        "required": [],
    }

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        task_id = str(params.get("task_id", "") or "")
        registry = get_background_registry()

        if task_id:
            task = registry.get_task(task_id)
            if not task:
                return ToolResult.error(f"未找到任务: {task_id}")
            if task.status == "pending":
                return ToolResult.ok(f"任务 {task_id}: 等待执行中...")
            if task.status == "running":
                return ToolResult.ok(f"任务 {task_id}: 执行中...")
            return ToolResult.ok(
                f"任务 {task_id}: {task.status}\n结果:\n{task.result[:3000]}",
                task_id=task_id, status=task.status,
            )

        # 列出所有任务
        tasks = registry.list_tasks()
        if not tasks:
            return ToolResult.ok("无活跃子 Agent 任务")

        lines = [f"{'='*60}"]
        for t in tasks[:10]:
            status_icon = {"pending": "⏳", "running": "🔄", "success": "✅", "failed": "❌"}
            lines.append(
                f"{status_icon.get(t.status, '?')} [{t.status}] {t.task_id}\n"
                f"   目标: {t.goal[:100]}\n"
                f"   创建: {t.created_at}"
            )
            if t.result:
                lines.append(f"   结果: {t.result[:200]}")
            lines.append(f"{'='*60}")

        return ToolResult.ok("\n".join(lines), task_count=registry.total_count)
