"""Cache-safe capability guidance for Xenon engines.

The stable guide stays in the system prompt. Task-specific advice is selected
from the existing PromptOptimizer intent and appended to the current user turn,
so it does not fragment the reusable system-prompt cache prefix.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


STRATEGY_GUIDE = """

## 🧠 通用原则

- 根据任务目标组合工具，不要反复依赖单一基础工具。
- 修改前先取得相关源码证据；修改后必须运行可复现验证。
- 优先使用语义工具定位符号和引用，文本搜索用于发现线索。
- 多个独立只读操作可并行；写入与有副作用操作必须有序执行。
- 只调用当前可用工具；工具失败时根据 Observation 调整策略，禁止编造结果。
"""


@dataclass(frozen=True)
class TaskSignals:
    """Deterministic structural signals used to scale advice, not to route intent."""

    multi_file: bool = False
    needs_verification: bool = False
    needs_recovery: bool = False

    @property
    def complex(self) -> bool:
        return self.multi_file or self.needs_verification or self.needs_recovery


@dataclass(frozen=True)
class StrategyAdvice:
    """One task-local strategy hint and its user-facing concise tip."""

    intent: str | None
    prompt: str
    tip: str
    signals: TaskSignals = TaskSignals()


@dataclass(frozen=True)
class _StrategySpec:
    label: str
    tools: tuple[str, ...]
    principle: str


_SPECS: dict[str, _StrategySpec] = {
    "debug": _StrategySpec(
        "调试任务",
        ("command", "search_files", "code_index", "read_file", "lsp_diagnostics", "edit_file", "write_file", "command"),
        "先复现并定位根因，再做最小正确修改，最后运行回归验证。",
    ),
    "write_test": _StrategySpec(
        "测试任务",
        ("search_files", "read_file", "code_index", "batch_write", "write_file", "command"),
        "先理解现有行为和测试风格，再覆盖正常、边界与异常路径并真实运行。",
    ),
    "write_code": _StrategySpec(
        "实现任务",
        ("list_files", "search_files", "read_file", "code_index", "batch_write", "write_file", "batch_edit", "edit_file", "command"),
        "先读取相邻实现和扩展接口，再落盘实现并用测试闭环。",
    ),
    "refactor": _StrategySpec(
        "重构任务",
        ("code_index", "lsp_find_refs", "ast_analyze", "refactor", "batch_edit", "edit_file", "command"),
        "先追踪定义与引用，保持外部契约，再运行完整回归测试。",
    ),
    "convert": _StrategySpec(
        "迁移任务",
        ("list_files", "search_files", "read_file", "batch_write", "batch_edit", "command"),
        "先确认源行为与目标约束，分层迁移后进行等价性验证。",
    ),
    "research": _StrategySpec(
        "调研任务",
        ("clone_repo", "docs_fetch", "github_fetch", "list_files", "search_files", "read_file", "code_index"),
        "先获取权威来源和项目结构，再交叉阅读关键实现，结论必须绑定真实证据。",
    ),
    "explain": _StrategySpec(
        "代码理解任务",
        ("code_index", "lsp_goto_def", "lsp_find_refs", "read_file", "ast_analyze"),
        "从入口、定义和调用关系解释，不凭文件名或局部片段猜测。",
    ),
    "write_doc": _StrategySpec(
        "文档任务",
        ("list_files", "search_files", "read_file", "docs_fetch", "write_file", "edit_file"),
        "先核对真实接口、用法和项目约定，再编写可复现文档。",
    ),
}


def infer_task_signals(task_text: str) -> TaskSignals:
    """Infer only structural complexity signals; domain words are not used."""

    text = task_text or ""
    # Paths are a stronger multi-file signal than words such as "project".
    paths = re.findall(r"(?:[A-Za-z]:[\\/]|/|\b(?:src|tests?|lib|app)[\\/])[^\s,，;；)）]+", text)
    multi_file = len({p.rstrip(".,。") for p in paths}) >= 2 or bool(
        re.search(r"跨文件|多个文件|多文件|across\s+files|multiple\s+files", text, re.I)
    )
    needs_verification = bool(
        re.search(r"运行(?:全量|完整|回归)?测试|跑(?:全量|完整|回归)?测试|验证|回归|test(?:s|ing)?", text, re.I)
    )
    needs_recovery = bool(
        re.search(r"失败后|如果失败|出错后|重试|重新运行|重新验证|retry|recover", text, re.I)
    )
    return TaskSignals(multi_file, needs_verification, needs_recovery)


def _select_available(tools: tuple[str, ...], available: set[str]) -> list[str]:
    """Filter unavailable tools while preserving meaningful phase repeats."""

    return [tool for tool in tools if tool in available]


def _format_chain(selected: list[str]) -> str:
    """Render alternatives explicitly so a chain is guidance, not a mandate."""

    rendered: list[str] = []
    i = 0
    while i < len(selected):
        if selected[i] == "edit_file" and i + 1 < len(selected) and selected[i + 1] == "write_file":
            rendered.append("edit_file 或 write_file")
            i += 2
            continue
        if selected[i] == "batch_edit" and i + 1 < len(selected) and selected[i + 1] == "edit_file":
            rendered.append("batch_edit 或 edit_file")
            i += 2
            continue
        rendered.append(selected[i])
        i += 1
    return " → ".join(rendered)


def get_strategy_advice(
    intent: str | None,
    available_tools: set[str] | frozenset[str],
    task_text: str = "",
) -> StrategyAdvice:
    """Return deterministic task-local guidance filtered by real capabilities."""

    spec = _SPECS.get(intent or "")
    if spec is None:
        return StrategyAdvice(intent=None, prompt="", tip="")

    selected = _select_available(spec.tools, set(available_tools))
    if not selected:
        return StrategyAdvice(intent=None, prompt="", tip="")

    signals = infer_task_signals(task_text)
    chain = _format_chain(selected)
    prompt = (
        "[Xenon 本轮策略]\n"
        f"任务类型：{spec.label}\n"
        f"建议能力链：{chain}\n"
        f"执行原则：{spec.principle}\n"
    )
    if signals.complex:
        extras: list[str] = []
        if signals.multi_file:
            extras.append("这是跨文件/多文件任务，先建立文件与符号关系，再批量但有序修改")
        if signals.needs_verification:
            extras.append("任务明确要求验证，修改后必须执行测试并依据真实输出判断")
        if signals.needs_recovery:
            extras.append("任务包含失败恢复要求，失败后分析 Observation 再调整，不要重复原调用")
        prompt += "复杂度提示：" + "；".join(extras) + "。\n"
    prompt += "这是按当前可用工具生成的建议；应根据 Observation 调整，不要机械调用每个工具。"
    tip = f"{spec.label}：优先采用 {chain} 的证据闭环，并按实际反馈调整。"
    if signals.complex:
        tip += " 检测到多阶段要求，将优先关注依赖关系与验证闭环。"
    return StrategyAdvice(intent=intent, prompt=prompt, tip=tip, signals=signals)
