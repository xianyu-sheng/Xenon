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
# 路径类文件声称：必须包含路径分隔符（/ 或 \）。裸 `self.c`、`obj.config`
# 这类代码/属性引用不带分隔符，绝不会被误判为文件。这修复了 SWE-bench
# 实测 40% 误杀（django 实例 final output 含 ORM 字段引用 self.c 被当文件）。
_FILE_PATTERNS = [
    r"[\w.\-]+[/\\][\w./\\\-]+\.[A-Za-z0-9]+",
]
# 裸文件名声称：必须由声明动词开头（"创建了 docstring.py" / "saved models.py"），
# 动词后可跟连接词延续的并列文件名（"已保存 A 和 B"）。无动词开头的裸名
# （代码片段中的 self.c、foo.py 引用）不视为文件声称。
_CLAIMED_BARE_FILE_RE = re.compile(
    r"[\w.\-]+\.(?:py|js|ts|html|css|json|yaml|yml|toml|md|txt|sh|bat|ps1|go|rs|java|c|cpp|h)",
    re.IGNORECASE,
)
_CLAIMED_FILE_PATTERN = re.compile(
    r"(?:创建|新建|生成|写入|保存|写入到|created|written|saved|generated|"
    r"initialized|made)\s*(?:了)?\s*[`'\"\[]?[\w.\-]+\.(?:py|js|ts|html|css|"
    r"json|yaml|yml|toml|md|txt|sh|bat|ps1|go|rs|java|c|cpp|h)[`'\"]?"
    r"(?:\s*(?:、|，|,|和|与|及|以及|and|&)\s*[`'\"\[]?[\w.\-]+\.(?:py|js|ts|"
    r"html|css|json|yaml|yml|toml|md|txt|sh|bat|ps1|go|rs|java|c|cpp|h)[`'\"]?)*",
    re.IGNORECASE,
)


def _strip_diff_prefix(path: str) -> str:
    """剥掉 diff 头部的 a/、b/ 前缀（`diff --git a/x b/x`、`--- a/x`、`+++ b/x`）。

    保留单段路径（`a/foo.py` 剥成 `foo.py` 后交给动词/后缀匹配裁决），
    避免把 diff 上下文里的路径当成"声称的文件"。
    """
    for prefix in ("a/", "b/"):
        if path.startswith(prefix) and "/" in path[len(prefix):]:
            return path[len(prefix):]
    return path


def _extract_claimed_files(llm_output: str) -> set[str]:
    """提取 LLM 输出中声称涉及的文件（路径类 + 声明动词开头的裸名序列）。"""
    mentioned: set[str] = set()
    for pattern in _FILE_PATTERNS:
        for m in re.findall(pattern, llm_output):
            mentioned.add(_strip_diff_prefix(m))
    for m in _CLAIMED_FILE_PATTERN.finditer(llm_output):
        mentioned.update(_CLAIMED_BARE_FILE_RE.findall(m.group(0)))
    return mentioned


def _path_matches_verified(claimed: str, verified: set[str]) -> bool:
    """声称路径与已验证路径做后缀匹配（相对/绝对、./ 前缀兼容）。"""
    claimed_norm = Path(claimed).as_posix().lstrip("./")
    for v in verified:
        v_norm = Path(v).as_posix().lstrip("./")
        if claimed_norm == v_norm:
            return True
        if claimed_norm.endswith("/" + v_norm) or v_norm.endswith("/" + claimed_norm):
            return True
    return False


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

    mentioned_files = _extract_claimed_files(llm_output)
    if not mentioned_files:
        return True, []

    verified = _verified_files_from_tracker(tracker)
    unverified: list[str] = []
    for f in mentioned_files:
        if _path_matches_verified(f, verified):
            continue
        if Path(f).exists():
            continue
        unverified.append(f)
    return (len(unverified) == 0), unverified


# ── 修复绑定分析（FixBindingGate 的纯函数，数据驱动设计）────────
# 背景（SWE-bench 实测，rootfix_3 的 matplotlib 案例）：
#   plan-reflection 的补丁是「重复+特判」模式——新增行复制了 context 中
#   已有的赋值语句（xa[xa>N-1] = np.uint8(...) ≃ 现有 xa[xa>N-1] = ...），
#   修复表面症状而非根因；react/plan-execute 等通过补丁要么修改现有行，
#   要么插入全新的防御逻辑。据此设计三条确定性判据：
#     1) 修改现有行（- 行）→ 强绑定信号
#     2) 纯追加 + 新增行与现有代码相似度 ≥ 阈值 → 重复特判（补救）信号
#     3) 纯追加 + 无重复 → 防御性插入（合法）信号
_ADD_LINE_RE = re.compile(r"^\+[^+]")
_DEL_LINE_RE = re.compile(r"^-")

_FIX_SIMILARITY_THRESHOLD = 0.5  # 与现有行 token 重叠率 ≥ 50% 视为「重复特判」


def _patch_from_tracker(tracker: Any | None) -> str:
    """从 tracker 的写工具调用恢复简化补丁文本（无 git diff 时的兜底）。

    把 edit_file 的 new_text 标 ``+``、old_text 标 ``-``，拼成 diff 风格
    文本，供 patch_binding_stats 分析「新增 vs 现有」模式。
    """
    if tracker is None:
        return ""
    parts: list[str] = []
    for call in getattr(tracker, "calls", []):
        if not call.success:
            continue
        params = getattr(call, "params", {}) or {}
        tool = getattr(call, "tool_name", "")
        if tool == "edit_file":
            old_text = params.get("old_text", "")
            new_text = params.get("new_text", "")
            if new_text:
                for line in str(new_text).splitlines():
                    parts.append("+" + line)
                if old_text:
                    for line in str(old_text).splitlines():
                        parts.append("-" + line)
    return "\n".join(parts)


def _token_set(text: str) -> set[str]:
    return set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text))


def line_similarity(a: str, b: str) -> float:
    """基于标识符 token 的 Jaccard 相似度（0~1）。"""
    ta, tb = _token_set(a), _token_set(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def patch_binding_stats(patch: str) -> dict[str, Any]:
    """分析补丁的根因绑定特征（确定性，零 LLM）。

    返回:
        added_lines: 新增行列表（去 +/- 前缀）
        context_lines: 现有行列表（context 行 + 被删除行）
        modified_count: 修改的现有行数（- 行数）
        max_context_similarity: 新增行与任一现有行的最大相似度
    """
    added: list[str] = []
    context: list[str] = []
    for line in patch.splitlines():
        if _ADD_LINE_RE.match(line):
            added.append(line[1:].strip())
        elif line.startswith("---") or line.startswith("+++"):
            continue
        elif line.startswith(" "):
            context.append(line[1:].strip())
        elif _DEL_LINE_RE.match(line):
            context.append(line[1:].strip())

    modified_count = sum(
        1 for line in patch.splitlines()
        if _DEL_LINE_RE.match(line) and not line.startswith("---")
    )
    max_sim = 0.0
    for a in added:
        for c in context:
            sim = line_similarity(a, c)
            if sim > max_sim:
                max_sim = sim
    return {
        "added_lines": added,
        "context_lines": context,
        "modified_count": modified_count,
        "max_context_similarity": max_sim,
    }


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


class FixBindingGate(EvidenceGate):
    """修复绑定：补丁必须「绑定根因」而非「表面特判」→ 拒绝/警告。

    SWE-bench 实测（rootfix_3 matplotlib）：plan-reflection 的补丁是
    「重复+特判」模式——新增行复制 context 中已有的赋值语句
    （xa[xa>N-1] = np.uint8(...) ≃ 现有 xa[xa>N-1] = ...），修复表面
    症状而非根因，导致修好 uint8 却破坏 ~160 个 colormap 测试。
    本 Gate 用三条确定性判据识别该模式（零 LLM）：

      1) 修改现有行（- 行）→ 强绑定信号，通过
      2) 纯追加 + 新增行与现有代码相似度 ≥ 阈值 → 重复特判（补救），拒绝
      3) 纯追加 + 无重复 → 防御性插入（如加 import/边界检查），通过
    """

    phase = "fix"

    def check(
        self,
        ctx: Any,
        *,
        patch: str = "",
        output: str = "",
        tracker: Any | None = None,
        workspace_root: Path | None = None,
        **kwargs: Any,
    ) -> GateVerdict:
        patch_text = patch or ""
        if not patch_text and tracker is not None:
            # 从 tracker 的写工具恢复补丁（无 git diff 时的兜底）
            patch_text = _patch_from_tracker(tracker)
        if not patch_text:
            # 没有补丁信息时无法判定——按通过处理（不误伤只读任务）
            return GateVerdict(self.phase, True, "无补丁信息，跳过绑定校验", "info")

        stats = patch_binding_stats(patch_text)
        if stats["modified_count"] > 0:
            return GateVerdict(
                self.phase, True,
                "补丁修改了 %d 个现有行，绑定根因" % stats["modified_count"],
                "info",
            )

        max_sim = stats["max_context_similarity"]
        if max_sim >= _FIX_SIMILARITY_THRESHOLD:
            reason = (
                "补丁为纯追加，且新增行与现有代码相似度 %.0f%%，"
                "疑似「重复+特判」的表面修复而非根因修复" % (max_sim * 100)
            )
            return GateVerdict(self.phase, False, reason, "warning")

        return GateVerdict(
            self.phase, True,
            "补丁为纯追加且无重复模式（防御性插入）", "info",
        )


class FactBindingGate(EvidenceGate):
    """事实绑定：写文件前必须先读过该文件 → 拒绝盲写。

    SWE-bench 实测发现：glm-5.1 / deepseek-v4-flash 等中小模型经常
    在没有 read_file / search 的情况下直接 write_file / edit_file，
    导致补丁改错位置或基于幻觉的文件结构。本 Gate 检查 tracker 中
    每个成功写操作的目标文件是否在写之前已被读取过（确定性，零 LLM）。

    判据：对每个成功的 write/edit 调用，检查同一文件路径是否在更早的
    call 中有成功的 read_file / search_files。若无 → 拒绝（盲写）。
    """

    phase = "fact"

    _READ_TOOLS = frozenset({
        "read_file", "search_files", "interpreter", "search", "grep", "find",
    })

    @staticmethod
    def _command_reads(params: dict[str, Any]) -> bool:
        """仅把明确的查看命令视为读取；不能把所有 command 当作读。"""
        text = str(params.get("action") or params.get("command") or "").strip().lower()
        return bool(re.match(r"^(cat|head|tail|less|sed\s+-n|grep|rg|find)\b", text))

    def check(
        self,
        ctx: Any,
        *,
        tracker: Any | None = None,
        **kwargs: Any,
    ) -> GateVerdict:
        if tracker is None:
            return GateVerdict(self.phase, True, "无 tracker，跳过", "info")
        calls = getattr(tracker, "calls", [])
        if not calls:
            return GateVerdict(self.phase, True, "无工具调用", "info")

        read_files: set[str] = set()
        blind_writes: list[str] = []

        for call in calls:
            if not call.success:
                continue
            tool = call.tool_name
            params = call.params or {}
            if tool in self._READ_TOOLS or (
                tool == "command" and self._command_reads(params)
            ):
                path = params.get("file_path") or params.get("path") or params.get("pattern", "")
                if path:
                    read_files.add(str(path))
            elif tool in WRITE_TOOL_NAMES:
                target = params.get("file_path") or params.get("path", "")
                if target and str(target) not in read_files:
                    blind_writes.append(str(target))
                # 写之后该文件算作"已知"
                if target:
                    read_files.add(str(target))

        if blind_writes:
            uniq = list(dict.fromkeys(blind_writes))
            return GateVerdict(
                self.phase, False,
                "检测到 %d 个文件在写入前未被读取（盲写）: %s"
                % (len(uniq), ", ".join(uniq[:3])),
                "warning",
                payload={"blind_files": uniq},
            )
        return GateVerdict(self.phase, True, "所有写入文件均已先读取", "info")


# ── 便捷工厂：默认 Gate 管线 ───────────────────────────────
def default_gates() -> list[EvidenceGate]:
    """返回默认会话级 Gate 管线（竖着贯穿：fact → plan → completion → fix → output）。

    注意：EvidenceCaptureGate 是收集型门，由组合引擎按需挂载，不在
    默认管线内（避免对纯执行引擎造成多余开销）。
    """
    return [
        FactBindingGate(),
        PlanCompletenessGate(),
        TaskCompletionGate(),
        FixBindingGate(),
        FileClaimGate(),
    ]
