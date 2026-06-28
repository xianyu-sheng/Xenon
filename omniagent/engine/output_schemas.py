"""
Structured Output Schemas — Pydantic 模型约束 LLM 输出。

消除对 LLM 文本输出的正则 JSON 解析依赖，
通过 JSON Schema 约束确保输出格式正确。

所有模型同时支持 `response_format` 参数传递和传统 JSON 解析回退。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


# ── ReAct 引擎输出 ──────────────────────────────────────────

class ReActAction(BaseModel):
    """ReAct 工具调用。"""
    action: str = Field(description="工具名称，如 read_file, write_file, command 等")
    action_input: dict[str, Any] = Field(default_factory=dict, description="工具参数")


class ReActOutput(BaseModel):
    """ReAct 引擎单轮输出。

    必须包含 thought，以及 action+action_input 或 final_answer 之一。
    """
    thought: str = Field(description="分析当前状态，决定下一步做什么")
    action: str | None = Field(default=None, description="要调用的工具名（调用工具时必填）")
    action_input: dict[str, Any] | None = Field(default=None, description="工具参数（调用工具时必填）")
    final_answer: str | None = Field(default=None, description="给用户的最终回答（任务完成时必填）")

    @property
    def is_tool_call(self) -> bool:
        return self.action is not None

    @property
    def is_final(self) -> bool:
        return self.final_answer is not None

    def to_legacy_dict(self) -> dict[str, Any]:
        """转换为兼容 parse_react() 返回格式的字典。"""
        result: dict[str, Any] = {"thought": self.thought, "raw_text": ""}
        if self.action and self.action_input is not None:
            result["action"] = self.action
            result["action_input"] = self.action_input
        if self.final_answer:
            result["final_answer"] = self.final_answer
        return result


# ── Plan 引擎输出 ───────────────────────────────────────────

class PlanStep(BaseModel):
    """单个执行步骤。"""
    id: int = Field(description="步骤序号")
    task: str = Field(description="步骤描述")
    tool: str | None = Field(default=None, description="工具名，不需要工具时设为 null")
    params: dict[str, Any] = Field(default_factory=dict, description="工具参数")
    depends_on: list[int] = Field(default_factory=list, description="依赖的步骤 ID 列表")


class PlanOutput(BaseModel):
    """Plan-Execute 引擎规划输出。"""
    analysis: str = Field(description="任务目标和策略分析")
    steps: list[PlanStep] = Field(description="执行步骤列表")


# ── Reflection 审查输出 ─────────────────────────────────────

class ReviewOutput(BaseModel):
    """Reflection 引擎审查输出。"""
    pass_: bool = Field(alias="pass", description="是否通过审查")
    score: int = Field(ge=1, le=10, description="1-10 评分")
    feedback: str = Field(description="具体评价")
    issues: list[str] = Field(default_factory=list, description="问题列表")

    def to_legacy_dict(self) -> dict[str, Any]:
        return {
            "pass": self.pass_,
            "score": self.score,
            "feedback": self.feedback,
            "issues": self.issues,
        }


# ── JSON Schema 生成 ────────────────────────────────────────

def get_react_schema() -> dict[str, Any]:
    """返回 ReAct 输出的 JSON Schema（OpenAI 格式）。"""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "react_output",
            "strict": True,
            "schema": ReActOutput.model_json_schema(),
        },
    }


def get_plan_schema() -> dict[str, Any]:
    """返回 Plan 输出的 JSON Schema（OpenAI 格式）。"""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "plan_output",
            "strict": True,
            "schema": PlanOutput.model_json_schema(),
        },
    }


def get_review_schema() -> dict[str, Any]:
    """返回 Review 输出的 JSON Schema（OpenAI 格式）。"""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "review_output",
            "strict": True,
            "schema": ReviewOutput.model_json_schema(),
        },
    }
