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
    子 Agent 在后台运行，拥有完整的 ReAct 引擎和所有工具能力。
    结果可通过 AgentResultTool 查询。

    适用场景: 多文件重构、独立模块编写、并行探索等。
    """

    name = "spawn_agent"
    description = (
        "派生子 Agent 独立处理一个子任务。子 Agent 在后台运行，"
        "拥有完整的 ReAct 引擎和所有工具（读写文件、执行命令、搜索等）。"
        "返回 task_id，后续用 agent_result 查询结果。"
        "适用场景: 多文件重构、独立模块编写、并行探索等。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "goal": {"type": "string", "description": "子 Agent 的任务目标（详细的自然语言描述）"},
            "run_id": {"type": "string", "description": "父 run_id（可选）"},
            "model": {"type": "string", "description": "指定模型（可选，默认 deepseek/deepseek-v4-pro）"},
        },
        "required": ["goal"],
    }

    # 默认模型优先级（可通过实例属性覆盖）
    model_priority: list[str] = ["deepseek/deepseek-v4-pro"]

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        goal = str(params.get("goal", ""))
        parent_run_id = str(params.get("run_id", ""))
        model = str(params.get("model", "") or self.model_priority[0])

        if not goal:
            return ToolResult.schema_error("spawn_agent 需要 goal 参数")

        registry = get_background_registry()
        if registry.active_count >= 5:
            return ToolResult.error("子 Agent 数量已达上限 (5)，请等待现有任务完成")

        task = registry.create_task(goal, parent_run_id=parent_run_id)

        # 创建后台任务 — 传入 model 用于子 Agent
        future = registry.create_future(task.task_id)
        asyncio.create_task(self._run_subagent(task, registry, model))

        return ToolResult.ok(
            f"子 Agent 已启动 (task_id: {task.task_id})\n目标: {goal}\n使用 agent_result 工具查询结果。",
            task_id=task.task_id,
        )

    async def _run_subagent(
        self, task: SubagentTask, registry: BackgroundTaskRegistry, model: str = "",
    ) -> None:
        """后台执行子 Agent 任务 — 使用完整 ReAct 引擎 + 工具。

        与主 Agent 共享同一套工具（BUILTIN_TOOLS），
        拥有完整的 Think-Act-Observe 循环能力。
        """
        registry.mark_running(task.task_id)

        async with registry._semaphore:
            try:
                from omniagent.engine.react_engine import BUILTIN_TOOLS, ReActEngine
                from omniagent.engine.callbacks import SilentCallback

                model_priority = [model] if model else self.model_priority

                # 构建子 Agent 的 ReAct 引擎
                engine = ReActEngine(
                    model_priority=model_priority,
                    max_iterations=8,  # 子 Agent 迭代上限略低于主 Agent
                    tools=BUILTIN_TOOLS,
                    callback=SilentCallback(),
                )

                # 在线程池中运行同步 ReAct 引擎
                result = await asyncio.to_thread(engine.run, task.goal)
                registry.mark_done(task.task_id, result, success=True)
                logger.info(f"子 Agent {task.task_id} 完成: {result[:200]}")

            except asyncio.CancelledError:
                registry.mark_done(task.task_id, "子 Agent 被取消", success=False)
                logger.warning(f"子 Agent {task.task_id} 被取消")
            except Exception as e:
                logger.error(f"子 Agent {task.task_id} 异常: {e}", exc_info=True)
                registry.mark_done(task.task_id, f"执行异常: {e}", success=False)


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
