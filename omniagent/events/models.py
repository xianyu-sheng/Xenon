"""事件数据模型 — Pydantic 强类型事件定义。

每个事件都是独立的数据类，包含:
- 事件类型标识
- 关联的 run_id / session_id
- 时间戳
- 事件特定数据
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


def _now() -> str:
    """返回当前 UTC 时间的 ISO 8601 字符串。"""
    return datetime.now(UTC).isoformat()


# ── 基础事件 ────────────────────────────────────────────────

class BaseEvent(BaseModel):
    """所有事件的基类。"""
    event_type: str = ""
    ts: str = Field(default_factory=_now)


# ── Run 生命周期 ────────────────────────────────────────────

class RunStartedEvent(BaseEvent):
    """Agent run 开始。"""
    event_type: str = "run.started"
    run_id: str
    session_id: str = ""
    goal: str
    mode: str
    model_ids: list[str] = Field(default_factory=list)
    cwd: str = ""


class RunFinishedEvent(BaseEvent):
    """Agent run 结束。"""
    event_type: str = "run.finished"
    run_id: str
    status: str  # "success" | "error" | "cancelled"
    result: str = ""
    reason: str | None = None


# ── Step 生命周期 ───────────────────────────────────────────

class StepStartedEvent(BaseEvent):
    """执行步骤开始。"""
    event_type: str = "step.started"
    run_id: str
    step: int


class StepFinishedEvent(BaseEvent):
    """执行步骤完成。"""
    event_type: str = "step.finished"
    run_id: str
    step: int
    success: bool
    summary: str = ""


# ── 工具调用 ────────────────────────────────────────────────

class ToolCallStartedEvent(BaseEvent):
    """工具调用开始。"""
    event_type: str = "tool.call_started"
    run_id: str
    tool_use_id: str
    tool_name: str
    params: dict[str, Any] = Field(default_factory=dict)


class ToolCallFinishedEvent(BaseEvent):
    """工具调用完成。"""
    event_type: str = "tool.call_finished"
    run_id: str
    tool_use_id: str
    tool_name: str
    output: str
    is_error: bool = False
    elapsed_ms: int = 0


# ── LLM 交互 ────────────────────────────────────────────────

class LlmModelSelectedEvent(BaseEvent):
    """模型被选中。"""
    event_type: str = "llm.model_selected"
    run_id: str
    model: str
    strategy: str = "planner_priority"


class LlmTokenEvent(BaseEvent):
    """流式 token 到达。"""
    event_type: str = "llm.token"
    run_id: str
    model: str
    token: str


class LlmUsageEvent(BaseEvent):
    """LLM 用量统计。"""
    event_type: str = "llm.usage"
    run_id: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0


# ── Agent 思考与回答 ────────────────────────────────────────

class AgentThoughtEvent(BaseEvent):
    """Agent 思考内容。"""
    event_type: str = "agent.thought"
    run_id: str
    thought: str


class AgentFinalAnswerEvent(BaseEvent):
    """Agent 最终回答。"""
    event_type: str = "agent.final_answer"
    run_id: str
    result: str


# ── 上下文压缩 ──────────────────────────────────────────────

class ContextCompactedEvent(BaseEvent):
    """上下文被压缩。"""
    event_type: str = "context.compacted"
    session_id: str
    run_id: str
    original_tokens: int
    summary_tokens: int


# ── 权限审批 ────────────────────────────────────────────────

class PermissionRequestEvent(BaseEvent):
    """请求权限审批（交互式）。"""
    event_type: str = "permission.request"
    session_id: str
    tool_use_id: str
    tool_name: str
    params_preview: str
    reason: str = ""


# ── 审查 ────────────────────────────────────────────────────

class ReviewFinishedEvent(BaseEvent):
    """审查完成。"""
    event_type: str = "review.finished"
    run_id: str
    score: int
    passed: bool
    feedback: str = ""


# ── 运行时异常 ──────────────────────────────────────────────

class RunErrorEvent(BaseEvent):
    """运行错误。"""
    event_type: str = "run.error"
    run_id: str
    error: str


class RunWarningEvent(BaseEvent):
    """运行警告。"""
    event_type: str = "run.warning"
    run_id: str
    warning: str
