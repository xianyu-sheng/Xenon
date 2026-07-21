"""Run Xenon mock or real-model evals and write a Markdown report."""

from __future__ import annotations

import argparse
import os
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

import yaml

try:
    from evals.mock_agent import MockAgent, estimate_tokens
except ImportError:  # pragma: no cover - script execution fallback
    from mock_agent import MockAgent, estimate_tokens


DEFAULT_TASKS_PATH = Path(__file__).with_name("tasks.yaml")
DEFAULT_REPORT_PATH = Path(__file__).parent / "reports" / "mock_report.md"


@contextmanager
def _change_directory(path: str):
    """Python 3.10-compatible equivalent of ``contextlib.chdir``."""
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def load_tasks(path: str | Path = DEFAULT_TASKS_PATH) -> list[dict[str, Any]]:
    """Load and validate eval tasks from YAML."""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    tasks = data.get("tasks", data)
    if not isinstance(tasks, list):
        raise ValueError("Eval tasks must be a list or a mapping with a 'tasks' list.")
    for task in tasks:
        validate_task(task)
    return tasks


def validate_task(task: dict[str, Any]) -> None:
    # success_criteria 不再必填（§8.14.4：原实现只做工具名包含检查，criteria 形同虚设；
    # 改为可选的人类复核提示，不参与自动评分）。保留字段供报告展示与人工 review。
    required = {"id", "category", "prompt", "expected_tools"}
    missing = required - set(task)
    if missing:
        raise ValueError(f"Task is missing required fields: {sorted(missing)}")
    if not isinstance(task["expected_tools"], list):
        raise ValueError(f"Task {task['id']} expected_tools must be a list.")


class RealAgent:
    """真实引擎 eval agent（§8.14.2 修复）：跑 ReAct 多轮闭环，按**实际执行**
    的工具评分，而非单轮裸 LLM 列工具名。

    - 在可选 ``workdir`` 下运行（``contextlib.chdir``），避免工具执行污染真实文件系统；
    - 通过包装 ``_execute_tool`` 记录**实际执行**的工具（收束阶段被门控的工具不计）；
    - 评分：``expected_tools ⊆ executed`` 且 final_answer 非空。``success_criteria``
      不自动评分（语义化标准无法通用机器判定），仅作人类复核提示写入报告；
    - ``engine_factory`` 可注入便于单测（默认构建 ``ReActEngine``）。

    **multi-turn 支持**（方案 C 根因 1 修复）：``max_turns`` 控制外部轮次，每轮 new
    ReActEngine 共享同一个 ``ContextManager`` 累积 user/assistant 消息，前一轮
    ``answer`` 注入后一轮 user_input 作为 review feedback。**通用机制**改进——
    不针对特定任务加白名单；不修改评分逻辑；不修改 expected_tools 列表。
    """

    def __init__(
        self,
        model: str,
        *,
        max_iterations: int = 8,
        max_turns: int = 3,
        workdir: str | None = None,
        engine_factory: Any = None,
    ) -> None:
        self.model = model
        self.max_iterations = max_iterations
        self.max_turns = max_turns
        self.workdir = workdir
        self._engine_factory = engine_factory

    def _default_engine_factory(self, callback: Any) -> Any:
        from xenon.engine.react_engine import ReActEngine

        return ReActEngine(
            [self.model], max_iterations=self.max_iterations, callback=callback,
        )

    def _build_context(self) -> Any:
        from xenon.engine.context import AgentContext

        return AgentContext()

    def _synthesize_review_prompt(
        self, original_prompt: str, prev_answer: str, turn: int,
    ) -> str:
        """生成第 N 轮的 review feedback prompt（通用机制，不针对特定任务）。

        第 1 轮用原任务；后续轮基于前一轮 answer + 原任务，让 LLM 自然产生
        '修订/补充/再确认' 行为，覆盖 multi_turn_revision 类任务。
        """
        return (
            f"Continue based on your previous answer (turn {turn}):\n"
            f"{prev_answer[:500]}\n\n"
            f"Original task: {original_prompt}"
        )

    def run_task(self, task: dict[str, Any]) -> dict[str, Any]:
        from xenon.engine.callbacks import EngineCallback
        from xenon.repl.context_manager import ContextManager
        factory = self._engine_factory or self._default_engine_factory
        executed: list[str] = []
        answer = ""
        expected = set(task.get("expected_tools", []))
        original_prompt = task["prompt"]

        # multi-turn：每轮共享 ContextManager 累积 history（F4 修复机制）
        cm = ContextManager()
        turns_used = 1
        try:
            with _change_directory(self.workdir) if self.workdir else nullcontext():
                for turn in range(self.max_turns):
                    turns_used = turn + 1
                    if turn == 0:
                        user_input = original_prompt
                    else:
                        user_input = self._synthesize_review_prompt(
                            original_prompt, answer, turn,
                        )
                    cm.add_user_message(user_input)

                    eng = factory(EngineCallback())
                    # 包装 _execute_tool 记录实际执行的工具（门控/拦截的不计）
                    orig_execute = eng._execute_tool

                    def _recording_execute(action, action_input, ctx, tracker=None):
                        executed.append(action)
                        return orig_execute(action, action_input, ctx, tracker)

                    eng._execute_tool = _recording_execute
                    # F4：ctx_mgr 注入，engine 消费（已压缩）历史
                    answer = eng.run(user_input, ctx_mgr=cm) or ""
                    cm.add_assistant_message(answer)

                    # 早停：所有 expected_tools 都调过了（无需进入下一轮）
                    if expected and expected.issubset(set(executed)):
                        break
                    # 早停：第 1 轮就空 executed + 强制拒绝过 → 不浪费 token
                    if turn == 0 and not executed:
                        break

                success, reason = self._score(task, executed, answer)
                notes = answer.strip()[:200] or reason
        except Exception as exc:  # noqa: BLE001 — eval 不应因单任务崩溃中断
            success, reason = False, f"engine run failed: {exc}"
            notes = reason[:200]

        missing = [t for t in expected if t not in executed]
        return {
            "task_id": task["id"],
            "category": task["category"],
            "success": success,
            "model": self.model,
            "token_count": estimate_tokens(task["prompt"]) + estimate_tokens(answer),
            "tool_calls": len(executed),
            "tool_failures": len(missing),
            "tools_used": executed,
            "notes": notes,
            "scoring": reason,
            "turns_used": turns_used,
        }

    @staticmethod
    def _score(task: dict[str, Any], executed: list[str], answer: str) -> tuple[bool, str]:
        """评分：实际执行了全部 expected_tools 且 final_answer 非空。"""
        expected = set(task.get("expected_tools", []))
        missing = expected - set(executed)
        if missing:
            return False, f"missing expected tools: {sorted(missing)}"
        if not (answer or "").strip():
            return False, "empty final answer"
        return True, f"executed all {len(expected)} expected tools"

    @staticmethod
    def _build_prompt(task: dict[str, Any]) -> str:
        """§8.14.1 修复：prompt **绝不暴露** expected_tools（仅评分用）。

        只给任务描述 + 类别 + success_criteria（作为背景，助模型理解验收标准）。
        expected_tools 仅由 runner 用于评分，不出现在 prompt 中——这样才能测出
        "模型从任务描述自主推断工具"的真实能力，而非复述喂给它的工具名。
        """
        criteria = task.get("success_criteria", "")
        lines = [
            f"Task: {task['prompt']}",
            f"Category: {task['category']}",
        ]
        if criteria:
            lines.append(f"Success criteria (for your understanding): {criteria}")
        lines.append(
            "Decide which tools to use and execute the task. The runner scores whether you "
            "actually executed the right tools—do not self-grade."
        )
        return "\n".join(lines)


def run_eval(
    tasks: list[dict[str, Any]],
    *,
    mode: str,
    model: str | None = None,
    workdir: str | None = None,
) -> list[dict[str, Any]]:
    """Run tasks through mock or real agent."""
    if mode == "mock":
        agent = MockAgent()
    elif mode == "real":
        if not model:
            raise ValueError("--model is required when --mode real")
        agent = RealAgent(model, workdir=workdir)
    else:
        raise ValueError(f"Unsupported eval mode: {mode}")
    return [agent.run_task(task) for task in tasks]


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(results)
    successes = sum(1 for result in results if result["success"])
    return {
        "tasks": total,
        "successes": successes,
        "success_rate": (successes / total * 100) if total else 0.0,
        "average_tokens": mean(result["token_count"] for result in results) if results else 0,
        "tool_calls": sum(result["tool_calls"] for result in results),
        "tool_failures": sum(result["tool_failures"] for result in results),
    }


def write_report(
    results: list[dict[str, Any]],
    output_path: str | Path,
    *,
    mode: str,
    model: str,
    run_date: str | None = None,
) -> Path:
    """Write a Markdown eval report and return the path."""
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    summary = summarize(results)
    date = run_date or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    lines = [
        "# Xenon Eval Report",
        "",
    ]
    # §8.14.3 修复：mock 模式显式标注为框架自检，不代表 agent 能力
    if mode == "mock":
        lines.extend([
            "> ⚠️ **Framework smoke test — NOT an agent capability measurement.**",
            "> mock 模式仅验证 eval 框架自身能跑通 + YAML 可解析，success_rate 恒 100%，",
            "> 与模型/引擎能力无关。判断 agent 能力请用 `--mode real`。",
            "",
        ])
    elif mode == "real":
        lines.extend([
            "> Scoring: real 模式跑 ReAct 多轮闭环，按**实际执行**的工具评分",
            ">（`expected_tools ⊆ executed` 且 final_answer 非空）。`success_criteria` 为人工复核提示，不自动评分。",
            "",
        ])
    lines.extend([
        f"- Mode: `{mode}`",
        f"- Model: `{model}`",
        f"- Run date: `{date}`",
        f"- Tasks: {summary['tasks']}",
        f"- Success Rate: {summary['success_rate']:.1f}%",
        f"- Average Tokens: {summary['average_tokens']:.1f}",
        f"- Tool Calls: {summary['tool_calls']}",
        f"- Tool Failures: {summary['tool_failures']}",
        "",
        "| Task | Category | Success | Tokens | Tool Calls | Tool Failures | Notes |",
        "| --- | --- | --- | ---: | ---: | ---: | --- |",
    ])
    for result in results:
        notes = str(result.get("notes", "")).replace("\n", " ")[:140]
        success = "yes" if result["success"] else "no"
        lines.append(
            f"| `{result['task_id']}` | {result['category']} | {success} | "
            f"{result['token_count']} | {result['tool_calls']} | {result['tool_failures']} | {notes} |"
        )

    failures = [result for result in results if not result["success"]]
    if failures:
        lines.extend(["", "## Failure Summary", ""])
        for result in failures:
            lines.append(f"- `{result['task_id']}`: {result.get('notes', '')}")

    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Xenon evals.")
    parser.add_argument("--mode", choices=["mock", "real"], default="mock")
    parser.add_argument("--model", default=None, help="Required for --mode real, e.g. deepseek/deepseek-v4-pro")
    parser.add_argument("--tasks", default=str(DEFAULT_TASKS_PATH))
    parser.add_argument("--output", default=str(DEFAULT_REPORT_PATH))
    parser.add_argument(
        "--workdir", default=None,
        help="Optional working directory for --mode real (tool execution sandbox).",
    )
    args = parser.parse_args(argv)

    tasks = load_tasks(args.tasks)
    results = run_eval(tasks, mode=args.mode, model=args.model, workdir=args.workdir)
    model = args.model or "mock-agent"
    report = write_report(results, args.output, mode=args.mode, model=model)
    print(f"Wrote eval report: {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
