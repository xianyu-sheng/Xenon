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

from xenon.engine.evidence_contracts import WRITE_TOOL_NAMES

logger = logging.getLogger(__name__)


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
def task_requires_write(user_input: str, ctx: Any | None = None) -> bool:
    """判断任务是否需要写操作（基于执行契约，非领域关键词枚举）。

    WRITE(2)/EXECUTE(3) 级别意味着用户要求文件变更或命令执行；
    ANSWER_ONLY(0)/READ_ONLY(1) 级别不需要落盘。

    契约是唯一来源：已有 ``ctx["_turn_contract"]`` 时直接读取；缺失时在
    本边界惰性构建一次（正则回退）并写回 ctx，不再逐层用不同参数重算。
    """
    try:
        from xenon.repl.turn_contract import ensure_turn_contract

        contract = ensure_turn_contract(ctx, user_input)
        return bool(contract.requires_write)
    except Exception:  # 契约构建失败时保守视为需要写（SWE-bench 场景默认）
        logger.warning("任务写操作判定失败，保守视为需要写")
        return True


def plan_has_write_step(steps: list[dict[str, Any]]) -> bool:
    """计划中是否至少包含一个写类工具步骤。"""
    return any(
        isinstance(step, dict) and step.get("tool") in WRITE_TOOL_NAMES
        for step in steps
    )


def has_successful_write(tracker: Any | None) -> bool:
    """是否已有任何成功的写类工具执行（文件被真正修改）。

    command 和 git 是歧义工具（pytest/ls 也是 command），已被排除在
    WRITE_TOOL_NAMES 之外；它们的写入由 workspace_status（git diff）检测。
    """
    if tracker is None:
        return False
    return any(
        call.success and call.tool_name in WRITE_TOOL_NAMES for call in tracker.calls
    )


# 文件声明校验正则（从 _verify_llm_file_claims 提取）
# A claim requires completion/aspect: "已创建 X" or "创建了 X".  Bare verbs
# ("创建 fixture", "写入计划") are plans/quotes, not claims — the research
# answers that quote issue text used to be flagged with 18 unverified files.
_CLAIM_PATTERNS = [
    r"(?:已|已经|成功)(?:创建|新建|生成|写入|保存)(?:了)?",
    r"(?:创建|新建|生成|写入|保存)(?:了|完成|完毕)",
    r"(?:文件|目录|文件夹)(?:已|已经)(?:创建|新建|生成|写入|保存|建立)",
    r"(?:created|written|saved|generated|initialized|made)",
]
# 路径类文件声称：必须包含路径分隔符（/ 或 \）。裸 `self.c`、`obj.config`
# 这类代码/属性引用不带分隔符，绝不会被误判为文件。这修复了 SWE-bench
# 实测 40% 误杀（django 实例 final output 含 ORM 字段引用 self.c 被当文件）。
_FILE_PATTERNS = [
    r"[\w.\-]+[/\\][\w./\\\-]+\.[A-Za-z0-9]+",
]
# 版本号不是文件：3.10/3.11/3.12 这类纯数字点分段序列（Python 版本矩阵
# 等）不应被视为路径声称。
_VERSION_LIKE_PATH = re.compile(
    r"^\d+(?:\.\d+)+(?:[/.]\d+(?:\.\d+)*)*$"
)
# 裸文件名声称：必须由声明动词开头（"创建了 docstring.py" / "saved models.py"），
# 动词后可跟连接词延续的并列文件名（"已保存 A 和 B"）。无动词开头的裸名
# （代码片段中的 self.c、foo.py 引用）不视为文件声称。
_CLAIMED_BARE_FILE_RE = re.compile(
    r"[\w.\-]+\.(?:py|js|ts|html|css|json|yaml|yml|toml|md|txt|sh|bat|ps1|go|rs|java|c|cpp|h)",
    re.IGNORECASE,
)
_CLAIMED_FILE_PATTERN = re.compile(
    r"(?:(?:已|已经|成功)(?:创建|新建|生成|写入|保存)|"
    r"(?:创建|新建|生成|写入|保存)(?:了|完成|完毕)|"
    r"created|written|saved|generated|initialized|made)"
    r"\s*(?:了)?\s*[`'\"\[]?[\w.\-]+\.(?:py|js|ts|html|css|"
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
        if path.startswith(prefix) and "/" in path[len(prefix) :]:
            return path[len(prefix) :]
    return path


def _claim_windows(text: str) -> list[str]:
    """Text around each completion claim; paths outside are not claims."""

    windows: list[str] = []
    for pattern in _CLAIM_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            windows.append(text[max(0, match.start() - 80) : match.end() + 200])
    return windows


def _extract_claimed_files(
    llm_output: str, windows: list[str] | None = None
) -> set[str]:
    """提取 LLM 输出中声称涉及的文件（路径类 + 声明动词开头的裸名序列）。

    Path-like strings are only collected inside a claim window so a research
    answer that quotes ``smartbench/frontends/python.py`` from an issue body is
    not reported as "claimed but unverified".
    """

    mentioned: set[str] = set()
    for window in windows if windows is not None else [llm_output]:
        for pattern in _FILE_PATTERNS:
            for m in re.findall(pattern, window):
                m = _strip_diff_prefix(m)
                if _VERSION_LIKE_PATH.match(m):
                    continue
                mentioned.add(m)
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
                fp = (
                    (spec.get("path") or spec.get("file_path", ""))
                    if isinstance(spec, dict)
                    else ""
                )
                if fp:
                    verified.add(fp)
        elif tool == "batch_edit":
            for spec in params.get("edits", []) or []:
                fp = spec.get("file_path", "") if isinstance(spec, dict) else ""
                if fp:
                    verified.add(fp)
    return verified


def _claimed_path_exists(claimed: str, workspace_root: Any = None) -> bool:
    """声称的文件是否真实存在于磁盘（含 workspace_root 相对路径解析）。

    SWE-bench 实测：command 工具创建的辅助文件（复现脚本等）不在
    verified 集合（那里只认文件工具），此前裸 ``Path(f).exists()`` 只按
    进程 cwd 判断，声称的相对路径（"tmp/repro.py"）与沙箱绝对路径
    （"/testbed/tmp/repro.py"）对不上就误杀。这里同时尝试：原始路径、
    workspace_root 相对路径、以及去掉前导 ./ 与 testbed/ 前缀的变体。
    """
    candidates = [claimed]
    root = str(workspace_root or "").strip()
    if root:
        root = root.rstrip("/")
        candidates.append(f"{root}/{claimed.lstrip('./')}")
    for variant in list(candidates):
        stripped = variant.lstrip("./")
        if stripped.startswith("testbed/"):
            candidates.append(stripped[len("testbed/") :])
    for candidate in candidates:
        try:
            if Path(candidate).exists():
                return True
        except (OSError, RuntimeError, ValueError):
            continue
    return False


def verify_file_claims(
    llm_output: str,
    tracker: Any | None = None,
    workspace_root: Any = None,
) -> tuple[bool, list[str]]:
    """检查 LLM 输出中是否声称创建/写入了文件，但实际未通过工具执行。

    返回 (passed, unverified_files)。纯校验，不修改输出文本。
    """
    has_claim = any(re.search(p, llm_output, re.IGNORECASE) for p in _CLAIM_PATTERNS)
    if not has_claim:
        return True, []

    mentioned_files = _extract_claimed_files(
        llm_output, _claim_windows(llm_output)
    )
    if not mentioned_files:
        return True, []

    verified = _verified_files_from_tracker(tracker)
    unverified: list[str] = []
    for f in mentioned_files:
        if _path_matches_verified(f, verified):
            continue
        if _claimed_path_exists(f, workspace_root):
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
        1
        for line in patch.splitlines()
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

# The five concrete gates (PlanCompleteness / TaskCompletion / FileClaim /
# EvidenceCapture / FixBinding / FactBinding) and ``default_gates()`` were the
# engine's delivery pipeline.  The kernel supersedes them: obligations come from
# the plan, coverage is decided by the V2 binder, claims bind to receipts with
# ``since_seq``, and an unverified claim becomes debt instead of a prompt
# nudge (#338).  ``GateVerdict`` and ``verify_file_claims`` stay because the
# REPL turn gate and the tool executor still use them.
