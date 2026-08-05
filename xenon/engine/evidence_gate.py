"""EvidenceGate — 会话级确定性验证门（横切贯穿层）。

设计背景（用户主导的架构决策，2026-08-05）：
  不做「工具层/引擎层接缝处的单一挂钩」，而是竖着贯穿整个会话生命周期的
  横切关注点（cross-cutting concern）：规划 / 执行 / 收尾每个阶段边界各挂
  一个确定性 Gate，层层过滤——前一层放过的错误由后一层兜底。

每个 Gate 满足：
  - 零 LLM：纯确定性校验，不烧 token（成本纪律）；
  - 校验与补救分离：Gate 只回答「通过/拒绝 + 原因」，如何补救由引擎决定
    （重新规划 / 追加补救步骤 / 追加警告），保证行为向后兼容；
  - 可观测：每次校验产出 GateVerdict，由引擎发 EventBus / callback 事件。

Step 1 范围（本文件）：把散落在 PlanExecuteEngine 的 4 个私有检查
（_ensure_plan_has_write_step / _ensure_task_completed /
_verify_llm_file_claims / ExecutionEvidence.capture 的校验部分）
提取为统一 Gate 契约，引擎侧保留补救逻辑。行为零变化。
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── 写类工具集合（单一真相源）────────────────────────────
# 与 tool_executor._WRITE_TOOLS 保持一致；command 也算——SWE-bench 场景中
# 修改通常经 command 跑脚本，或直接 write/edit 落盘。
WRITE_TOOL_NAMES = frozenset({
    "write_file", "edit_file", "create_directory", "batch_write",
    "batch_edit", "edit_with_llm", "append_file", "refactor",
    "git", "command",
})


# ── 校验结果契约 ──────────────────────────────────────────
@dataclass(slots=True)
class GateVerdict:
    """单个 Gate 的确定性校验结果。

    passed=False 时由引擎决定补救动作；reason 是人类可读原因，
    payload 携带可选的补救素材（如需要追加到 LLM 输出的警告文本）。
    """

    phase: str
    passed: bool
    reason: str = ""
    severity: str = "warning"  # info | warning | error
    payload: Any = None


# ── Gate 基类 ─────────────────────────────────────────────
class EvidenceGate(ABC):
    """会话级确定性验证门基类。

    子类实现 ``check`` 返回 GateVerdict；``phase`` 声明挂在生命周期
    的哪个阶段边界（plan / execution / completion / output）。
    """

    phase: str = "unknown"

    @abstractmethod
    def check(
        self,
        ctx: Any,
        *,
        user_input: str = "",
        plan: dict[str, Any] | None = None,
        results: list[dict[str, Any]] | None = None,
        tracker: Any | None = None,
        output: str = "",
        workspace_root: Path | None = None,
        max_steps: int = 0,
        **kwargs: Any,
    ) -> GateVerdict:
        """执行确定性校验，返回 GateVerdict。禁止调用任何 LLM。"""
        raise NotImplementedError


# ── 纯校验函数（从 PlanExecuteEngine 提取，单一真相源）───────
def task_requires_write(user_input: str) -> bool:
    """判断任务是否需要写操作（基于执行级别，非领域关键词枚举）。

    WRITE(2)/EXECUTE(3) 级别意味着用户要求文件变更或命令执行；
    ANSWER_ONLY(0)/READ_ONLY(1) 级别不需要落盘。
    """
    try:
        from xenon.repl.execution_policy import (
            ExecutionLevel,
            classify_execution_policy,
        )

        policy = classify_execution_policy(user_input)
        return int(policy.level) >= int(ExecutionLevel.WRITE)
    except Exception:  # 分类失败时保守视为需要写（SWE-bench 场景默认）
        logger.warning("任务写操作判定失败，保守视为需要写")
        return True


def plan_has_write_step(steps: list[dict[str, Any]]) -> bool:
    """计划中是否至少包含一个写类工具步骤。"""
    return any(
        isinstance(step, dict) and step.get("tool") in WRITE_TOOL_NAMES
        for step in steps
    )


def has_successful_write(tracker: Any | None) -> bool:
    """是否已有任何成功的写类工具执行（文件被真正修改）。"""
    if tracker is None:
        return False
    return any(
        call.success and call.tool_name in WRITE_TOOL_NAMES
        for call in tracker.calls
    )


# 文件声明校验正则（从 _verify_llm_file_claims 提取）
_CLAIM_PATTERNS = [
    r"(?:已|已经|成功)?(?:创建|新建|生成|写入|保存)(?:了)?",
    r"(?:created|written|saved|generated|initialized|made)",
    r"(?:文件|目录|文件夹)(?:已|已经)",
]
_FILE_PATTERNS = [
    r"[\w/\\.-]+\.(?:py|js|ts|html|css|json|yaml|yml|toml|md|txt|sh|bat|ps1|go|rs|java|c|cpp|h)",
    r"(?:src|lib|app|test|tests|dist|build|bin|config|docs)[/\\][\w/\\.-]+",
]


def _verified_files_from_tracker(tracker: Any | None) -> set[str]:
    """从工具调用中收集真正落盘的文件路径。"""
    verified: set[str] = set()
    if tracker is None:
        return verified
    for call in tracker.calls:
        if not call.success:
            continue
        tool = call.tool_name
        params = call.params
        if tool in ("write_file", "create_directory", "edit_file"):
            fp = params.get("file_path", "")
            if fp:
                verified.add(fp)
        elif tool == "batch_write":
            for spec in params.get("files", []) or []:
                fp = (spec.get("path") or spec.get("file_path", "")) if isinstance(spec, dict) else ""
                if fp:
                    verified.add(fp)
        elif tool == "batch_edit":
            for spec in params.get("edits", []) or []:
                fp = spec.get("file_path", "") if isinstance(spec, dict) else ""
                if fp:
                    verified.add(fp)
    return verified


def verify_file_claims(llm_output: str, tracker: Any | None = None) -> tuple[bool, list[str]]:
    """检查 LLM 输出中是否声称创建/写入了文件，但实际未通过工具执行。

    返回 (passed, unverified_files)。纯校验，不修改输出文本。
    """
    has_claim = any(re.search(p, llm_output, re.IGNORECASE) for p in _CLAIM_PATTERNS)
    if not has_claim:
        return True, []

    mentioned_files: set[str] = set()
    for pattern in _FILE_PATTERNS:
        mentioned_files.update(re.findall(pattern, llm_output))
    if not mentioned_files:
        return True, []

    verified = _verified_files_from_tracker(tracker)
    unverified: list[str] = []
    for f in mentioned_files:
        if f in verified:
            continue
        if not Path(f).exists():
            unverified.append(f)
    return (len(unverified) == 0), unverified


# ── 具体 Gate ──────────────────────────────────────────────
class PlanCompletenessGate(EvidenceGate):
    """Phase 1.5 计划完整性：任务需写但计划无写步骤 → 拒绝。

    对应原 ``_ensure_plan_has_write_step`` 的校验部分；补救（让 LLM 重新
    规划一次）由引擎执行。
    """

    phase = "plan"

    def check(
        self,
        ctx: Any,
        *,
        user_input: str = "",
        plan: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> GateVerdict:
        steps = list((plan or {}).get("steps", []) or [])
        if not steps:
            # 空计划由调用方另行处理（不在此 Gate 判定）
            return GateVerdict(self.phase, True, "空计划由调用方处理", "info")
        if not task_requires_write(user_input):
            return GateVerdict(self.phase, True, "任务不需要写操作", "info")
        if plan_has_write_step(steps):
            return GateVerdict(self.phase, True, "计划含写工具步骤", "info")
        return GateVerdict(
            self.phase,
            False,
            "任务需要写操作但计划 %d 步均无写工具" % len(steps),
            "warning",
        )


class TaskCompletionGate(EvidenceGate):
    """Phase 2.5 任务完成度：任务需写但无成功写类工具 → 拒绝。

    对应原 ``_ensure_task_completed`` 的校验部分；补救（强制追加一轮
    补救执行）由引擎执行。
    """

    phase = "completion"

    def check(
        self,
        ctx: Any,
        *,
        user_input: str = "",
        results: list[dict[str, Any]] | None = None,
        tracker: Any | None = None,
        max_steps: int = 0,
        **kwargs: Any,
    ) -> GateVerdict:
        if has_successful_write(tracker):
            return GateVerdict(self.phase, True, "已有成功写类工具执行", "info")
        if not task_requires_write(user_input):
            return GateVerdict(self.phase, True, "任务不需要写操作", "info")
        if results is not None and max_steps > 0 and len(results) >= max_steps:
            return GateVerdict(
                self.phase, True,
                "已达 max_steps=%d 上限，不追加补救" % max_steps, "info",
            )
        return GateVerdict(
            self.phase,
            False,
            "任务需要写操作但无写类工具执行",
            "warning",
        )


class FileClaimGate(EvidenceGate):
    """收尾：LLM 声称创建/写入文件但未通过工具验证 → 拒绝。

    对应原 ``_verify_llm_file_claims`` 的校验部分；payload 携带需要追加
    到 LLM 输出的警告文本，由引擎决定是否追加。
    """

    phase = "output"

    def check(
        self,
        ctx: Any,
        *,
        output: str = "",
        tracker: Any | None = None,
        **kwargs: Any,
    ) -> GateVerdict:
        passed, unverified = verify_file_claims(output, tracker)
        if passed:
            return GateVerdict(self.phase, True, "无未验证的文件声明", "info")
        warning = (
            "\n\n⚠️ **注意**: 以上内容中提到了创建文件 "
            "`" + "`, `".join(unverified) + "`，"
            "但这些文件未经工具验证，可能并未实际创建。"
            "如需真正创建文件，请使用 write_file 工具。"
        )
        return GateVerdict(
            self.phase, False,
            "LLM 声称创建但未经工具验证的文件: %s" % ", ".join(unverified),
            "warning",
            payload=warning,
        )


class EvidenceCaptureGate(EvidenceGate):
    """证据捕获：从 tracker + 工作区快照生成结构化 ExecutionEvidence。

    对应 ``ExecutionEvidence.capture``。它不是「拒绝型」校验门，而是
    「收集型」门——始终通过，payload 携带捕获到的证据对象，供引擎注入
    后续阶段的提示词。校验/补救分离在此同样成立：捕获即校验结果本身。
    """

    phase = "evidence"

    def check(
        self,
        ctx: Any,
        *,
        tracker: Any | None = None,
        workspace_root: Path | None = None,
        **kwargs: Any,
    ) -> GateVerdict:
        from xenon.engine.execution_evidence import ExecutionEvidence

        evidence = ExecutionEvidence.capture(tracker, workspace_root)
        return GateVerdict(
            self.phase, True,
            "证据捕获完成: %d 调用, %d 变更文件" % (
                len(evidence.calls), len(evidence.changed_files),
            ),
            "info",
            payload=evidence,
        )


# ── 便捷工厂：默认 Gate 管线 ───────────────────────────────
def default_gates() -> list[EvidenceGate]:
    """返回默认会话级 Gate 管线（竖着贯穿：plan → completion → output）。

    注意：EvidenceCaptureGate 是收集型门，由组合引擎按需挂载，不在
    默认管线内（避免对纯执行引擎造成多余开销）。
    """
    return [
        PlanCompletenessGate(),
        TaskCompletionGate(),
        FileClaimGate(),
    ]
