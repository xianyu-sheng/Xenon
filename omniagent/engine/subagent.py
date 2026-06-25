"""子 Agent 系统 — A2A AgentCard 协议 + 混合通知。

支持:
- 主 Agent 派生子 Agent 处理独立子任务
- AgentCard 自描述能力 → 按 capability 动态裁剪工具集
- Context Seed 最小上下文注入（已发现文件、目标、约束）
- 结构化结果 Schema（files/errors/findings/next_steps）
- 混合通知：Push（完成事件）+ Poll（每轮检查）+ 长周期兜底
- BackgroundTaskRegistry 管理跨 run 的子任务
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
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


# ── 混合通知 ────────────────────────────────────────────────


class SubagentNotifier:
    """子 Agent 通知管理器 — push + poll 混合。

    Push: 子 Agent 完成时主动写入完成事件
    Poll:  主 Agent 每轮 ReAct 开始时非阻塞检查
    长周期: 每 60s 全量扫描兜底
    """

    def __init__(self, registry: BackgroundTaskRegistry) -> None:
        self.registry = registry
        self._completed: dict[str, SubagentTask] = {}  # push 事件缓存
        self._event = asyncio.Event()
        self._last_full_poll = 0.0  # 上次全量扫描时间戳

    def on_complete(self, task: SubagentTask) -> None:
        """Push: 子 Agent 完成时调用。"""
        self._completed[task.task_id] = task
        self._event.set()

    def poll_completed(self, task_ids: list[str]) -> dict[str, SubagentTask]:
        """Poll: 非阻塞检查指定 task_id 是否完成。

        返回 {task_id: SubagentTask} 仅已完成的任务。
        """
        results: dict[str, SubagentTask] = {}
        for tid in task_ids:
            if tid in self._completed:
                results[tid] = self._completed.pop(tid)
                continue
            # 回退到 registry 检查
            task = self.registry.get_task(tid)
            if task and task.status in ("success", "failed"):
                results[tid] = task
        return results

    def poll_all(self, *, force: bool = False) -> list[SubagentTask]:
        """长周期全量扫描 — 兜底检查。

        每 60s 执行一次（force=True 时强制立即执行）。
        """
        import time
        now = time.monotonic()
        if not force and now - self._last_full_poll < 60:
            return []
        self._last_full_poll = now

        completed: list[SubagentTask] = []
        for task in self.registry.list_tasks():
            if task.status in ("success", "failed") and task.task_id not in self._completed:
                completed.append(task)
                self._completed[task.task_id] = task
        if completed:
            self._event.set()
        return completed

    @property
    def has_pending(self) -> bool:
        """是否有未处理的通知。"""
        return bool(self._completed)

    def clear(self) -> None:
        """清空通知缓存。"""
        self._completed.clear()
        self._event.clear()


# ── 结构化结果 ──────────────────────────────────────────────

_RESULT_SCHEMA_HINT = """
[输出格式要求]
当你完成子任务后，final_answer 必须按以下 JSON 格式输出（放在最后）：
```json
{
  "summary": "一句话总结完成的工作",
  "files_modified": ["path/to/file.py"],
  "files_created": ["path/to/new.py"],
  "key_findings": ["发现1", "发现2"],
  "errors": [],
  "next_steps": ["建议的下一步"]
}
```
如果某字段没有内容，用空列表 []。"""


def _parse_structured_result(text: str) -> dict[str, Any]:
    """从子 Agent 输出中解析结构化 JSON 结果。

    回退策略：若无法解析 JSON，返回整个文本作为 summary。
    """
    # 尝试匹配 JSON 块
    json_match = re.search(r'\{[^{}]*"summary"[^{}]*\}', text, re.DOTALL)
    if not json_match:
        # 尝试匹配代码块中的 JSON
        json_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)

    if json_match:
        try:
            return json.loads(json_match.group(1) if json_match.lastindex else json_match.group(0))
        except (json.JSONDecodeError, IndexError):
            pass

    # 回退：整段文本作为 summary
    return {
        "summary": text[:500].strip(),
        "files_modified": [],
        "files_created": [],
        "key_findings": [],
        "errors": [],
        "next_steps": [],
    }


def _build_context_seed_prompt(seed: dict[str, Any] | None) -> str:
    """将 context_seed 字典转换为注入子 Agent system prompt 的文本。"""
    if not seed:
        return ""

    parts = ["[父 Agent 上下文]"]
    if seed.get("parent_goal"):
        parts.append(f"主任务: {seed['parent_goal']}")
    if seed.get("discovered_files"):
        files = seed["discovered_files"]
        if isinstance(files, list):
            parts.append(f"已发现相关文件: {', '.join(files[:20])}")
    if seed.get("constraints"):
        constraints = seed["constraints"]
        if isinstance(constraints, list):
            parts.append(f"约束: {'; '.join(constraints)}")
    if seed.get("working_directory"):
        parts.append(f"工作目录: {seed['working_directory']}")

    return "\n".join(parts) if len(parts) > 1 else ""


# ── 工具 ────────────────────────────────────────────────────


class SpawnAgentTool(BaseTool):
    """派生子 Agent 处理独立子任务 — 支持 AgentCard 能力裁剪。

    主 Agent 可以调用此工具将子任务委派给独立的子 Agent。
    通过 capability 参数指定子 Agent 类型（如 "code-explorer"），
    子 Agent 自动按能力名片裁剪工具集。

    适用场景: 并行探索、多文件重构、独立模块编写、测试验证等。
    """

    name = "spawn_agent"
    description = (
        "派生子 Agent 独立处理一个子任务。子 Agent 在后台运行，拥有独立上下文。"
        "通过 capability 参数指定子 Agent 类型（先用 discover_agents 查看可用类型）。"
        "返回 task_id，后续用 agent_result 查询结果。"
        "支持并行 spawn 多个 Agent（通过多次调用此工具）。"
        "适用场景: 并行搜索、多文件重构、独立测试验证等。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "goal": {"type": "string", "description": "子 Agent 的任务目标（详细的自然语言描述）"},
            "capability": {
                "type": "string",
                "description": "子 Agent 能力类型（code-explorer / file-writer / test-runner / general-purpose），"
                               "不传则使用 general-purpose（全部工具）",
            },
            "context_seed": {
                "type": "object",
                "description": "父 Agent 传递的上下文（可选）：{parent_goal, discovered_files, constraints}",
            },
            "run_id": {"type": "string", "description": "父 run_id（可选）"},
            "model": {"type": "string", "description": "指定模型（可选，默认继承父 Agent）"},
        },
        "required": ["goal"],
    }

    # 默认模型优先级（可通过实例属性覆盖）
    model_priority: list[str] = ["deepseek/deepseek-v4-pro"]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._notifier: SubagentNotifier | None = None

    @property
    def notifier(self) -> SubagentNotifier:
        if self._notifier is None:
            self._notifier = SubagentNotifier(get_background_registry())
        return self._notifier

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        goal = str(params.get("goal", ""))
        parent_run_id = str(params.get("run_id", ""))
        model = str(params.get("model", "") or self.model_priority[0])
        capability = str(params.get("capability", "") or "general-purpose")
        context_seed_raw = params.get("context_seed", None)

        if not goal:
            return ToolResult.schema_error("spawn_agent 需要 goal 参数")

        registry = get_background_registry()
        if registry.active_count >= 5:
            return ToolResult.error("子 Agent 数量已达上限 (5)，请等待现有任务完成")

        # 解析 context_seed
        context_seed: dict[str, Any] | None = None
        if isinstance(context_seed_raw, dict):
            context_seed = {str(k): v for k, v in context_seed_raw.items()}
        elif isinstance(context_seed_raw, str) and context_seed_raw.strip():
            context_seed = {"parent_goal": str(context_seed_raw)}

        task = registry.create_task(goal, parent_run_id=parent_run_id)

        # 创建后台任务
        future = registry.create_future(task.task_id)
        asyncio.create_task(
            self._run_subagent(task, registry, model, capability, context_seed)
        )

        return ToolResult.ok(
            f"子 Agent 已启动 (task_id: {task.task_id}, capability: {capability})\n"
            f"目标: {goal}\n使用 agent_result 工具查询结果。",
            task_id=task.task_id,
        )

    async def _run_subagent(
        self,
        task: SubagentTask,
        registry: BackgroundTaskRegistry,
        model: str = "",
        capability: str = "general-purpose",
        context_seed: dict[str, Any] | None = None,
    ) -> None:
        """后台执行子 Agent — AgentCard 裁剪 + 上下文注入 + 结构化结果。"""
        registry.mark_running(task.task_id)

        async with registry._semaphore:
            try:
                from omniagent.engine.react_engine import BUILTIN_TOOLS, REACT_SYSTEM_PROMPT, ReActEngine
                from omniagent.engine.callbacks import SilentCallback
                from omniagent.engine.agent_card import get_card_registry

                model_priority = [model] if model else self.model_priority

                # ── 1. AgentCard 能力裁剪 ──
                card_registry = get_card_registry()
                card = card_registry.get(capability)
                if card:
                    tools = card.resolve_tools(BUILTIN_TOOLS)
                    max_iter = card.max_iterations
                else:
                    tools = dict(BUILTIN_TOOLS)
                    max_iter = 8
                    logger.debug("名片 %s 不存在，使用全部工具和默认迭代数", capability)

                # ── 2. 构建增强 system prompt（上下文注入 + 结构化输出）──
                enhanced_prompt = REACT_SYSTEM_PROMPT
                context_block = _build_context_seed_prompt(context_seed)
                if context_block:
                    enhanced_prompt += "\n\n" + context_block
                enhanced_prompt += "\n\n" + _RESULT_SCHEMA_HINT

                # ── 3. 构建并运行子 Agent ──
                engine = ReActEngine(
                    model_priority=model_priority,
                    max_iterations=max_iter,
                    system_prompt=enhanced_prompt,
                    tools=tools,
                    callback=SilentCallback(),
                )

                result = await asyncio.to_thread(engine.run, task.goal)
                registry.mark_done(task.task_id, result, success=True)
                # Push 通知
                self.notifier.on_complete(task)
                logger.info(
                    "子 Agent %s [%s] 完成 (%d chars)",
                    task.task_id, capability, len(result),
                )

            except asyncio.CancelledError:
                registry.mark_done(task.task_id, "子 Agent 被取消", success=False)
                logger.warning(f"子 Agent {task.task_id} 被取消")
            except Exception as e:
                logger.error(f"子 Agent {task.task_id} 异常: {e}", exc_info=True)
                registry.mark_done(task.task_id, f"执行异常: {e}", success=False)

    # ── 通知 API（供主 Agent ReAct 循环调用）──

    def poll_completed(self, task_ids: list[str]) -> dict[str, SubagentTask]:
        """Poll: 检查指定子 Agent 是否完成（非阻塞）。"""
        return self.notifier.poll_completed(task_ids)

    def poll_all_completed(self, *, force: bool = False) -> list[SubagentTask]:
        """长周期全量扫描。"""
        return self.notifier.poll_all(force=force)


class AgentResultTool(BaseTool):
    """查询子 Agent 任务结果 — 支持结构化输出。"""

    name = "agent_result"
    description = (
        "查询子 Agent 任务的结果。传入 task_id 获取结果（自动解析结构化 JSON），"
        "不传则列出所有子任务及其状态。"
    )
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
                return ToolResult.ok(f"⏳ 任务 {task_id}: 等待执行中...")
            if task.status == "running":
                return ToolResult.ok(f"🔄 任务 {task_id}: 执行中...")

            # 尝试结构化解析
            structured = _parse_structured_result(task.result) if task.result else {}
            summary = structured.get("summary", task.result[:500] if task.result else "")

            output = f"任务 {task_id}: {task.status}\n\n📋 摘要: {summary}"
            if structured.get("files_modified"):
                output += f"\n📝 已修改: {', '.join(structured['files_modified'][:10])}"
            if structured.get("files_created"):
                output += f"\n📄 已创建: {', '.join(structured['files_created'][:10])}"
            if structured.get("key_findings"):
                output += f"\n🔍 关键发现: {'; '.join(structured['key_findings'][:5])}"
            if structured.get("errors"):
                output += f"\n⚠️ 错误: {'; '.join(structured['errors'][:5])}"
            if structured.get("next_steps"):
                output += f"\n👉 建议下一步: {'; '.join(structured['next_steps'][:3])}"

            return ToolResult.ok(
                output,
                task_id=task_id,
                status=task.status,
                structured=structured,
            )

        # 列出所有任务
        tasks = registry.list_tasks()
        if not tasks:
            return ToolResult.ok("无活跃子 Agent 任务")

        lines: list[str] = []
        for t in tasks[:10]:
            status_icon = {"pending": "⏳", "running": "🔄", "success": "✅", "failed": "❌"}
            lines.append(
                f"{status_icon.get(t.status, '?')} [{t.status}] {t.task_id}\n"
                f"   目标: {t.goal[:100]}\n"
                f"   创建: {t.created_at}"
            )
            if t.result:
                # 尝试提取结构化摘要
                structured = _parse_structured_result(t.result)
                lines.append(f"   摘要: {structured.get('summary', t.result[:150])}")
        return ToolResult.ok("\n".join(lines), task_count=registry.total_count)
