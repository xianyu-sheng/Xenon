"""Run OmniAgent mock or real-model evals and write a Markdown report."""

from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime
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
    required = {"id", "category", "prompt", "expected_tools", "success_criteria"}
    missing = required - set(task)
    if missing:
        raise ValueError(f"Task is missing required fields: {sorted(missing)}")
    if not isinstance(task["expected_tools"], list):
        raise ValueError(f"Task {task['id']} expected_tools must be a list.")


class RealAgent:
    """Real-model eval agent that scores a model's tool plan without mutating files."""

    def __init__(self, model: str) -> None:
        self.model_name = model

    def run_task(self, task: dict[str, Any]) -> dict[str, Any]:
        from omniagent.utils.llm_client import chat_completion

        expected_tools = list(task.get("expected_tools", []))
        prompt = self._build_prompt(task)
        try:
            response = chat_completion(
                self.model_name,
                [
                    {
                        "role": "system",
                        "content": (
                            "You are planning a coding agent task. Return only JSON with keys: "
                            "tools_used and notes. tools_used must be a list of tool names. "
                            "Do not execute tools or claim files were changed."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                max_tokens=900,
                temperature=0.0,
            )
            parsed = self._parse_response(response)
            tools_used = [str(tool) for tool in parsed.get("tools_used", [])]
            missing_tools = [tool for tool in expected_tools if tool not in tools_used]
            success = not missing_tools
            notes = str(parsed.get("notes", "")) or response[:200]
            tool_failures = len(missing_tools)
            token_count = estimate_tokens(prompt) + estimate_tokens(response)
        except Exception as exc:
            tools_used = []
            success = False
            notes = f"Real model call failed: {exc}"
            tool_failures = len(expected_tools) or 1
            token_count = estimate_tokens(prompt)

        return {
            "task_id": task["id"],
            "category": task["category"],
            "success": success,
            "model": self.model_name,
            "token_count": token_count,
            "tool_calls": len(tools_used),
            "tool_failures": tool_failures,
            "tools_used": tools_used,
            "notes": notes,
        }

    @staticmethod
    def _build_prompt(task: dict[str, Any]) -> str:
        return (
            f"Task: {task['prompt']}\n"
            f"Category: {task['category']}\n"
            f"Expected tools: {', '.join(task['expected_tools'])}\n"
            f"Success criteria: {task['success_criteria']}\n"
            "Return the tool sequence a coding agent should use. The runner will score your JSON "
            "against the expected tools; do not self-grade."
        )

    @staticmethod
    def _parse_response(response: str) -> dict[str, Any]:
        text = response.strip()
        match = re.search(r"\{.*\}", text, re.S)
        if match:
            text = match.group(0)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {"success": False, "tools_used": [], "failed_tools": [], "notes": response[:300]}
        return parsed if isinstance(parsed, dict) else {}


def run_eval(tasks: list[dict[str, Any]], *, mode: str, model: str | None = None) -> list[dict[str, Any]]:
    """Run tasks through mock or real agent."""
    if mode == "mock":
        agent = MockAgent()
    elif mode == "real":
        if not model:
            raise ValueError("--model is required when --mode real")
        agent = RealAgent(model)
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
    date = run_date or datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")

    lines = [
        "# OmniAgent Eval Report",
        "",
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
    ]
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
    parser = argparse.ArgumentParser(description="Run OmniAgent evals.")
    parser.add_argument("--mode", choices=["mock", "real"], default="mock")
    parser.add_argument("--model", default=None, help="Required for --mode real, e.g. deepseek/deepseek-v4-pro")
    parser.add_argument("--tasks", default=str(DEFAULT_TASKS_PATH))
    parser.add_argument("--output", default=str(DEFAULT_REPORT_PATH))
    args = parser.parse_args(argv)

    tasks = load_tasks(args.tasks)
    results = run_eval(tasks, mode=args.mode, model=args.model)
    model = args.model or "mock-agent"
    report = write_report(results, args.output, mode=args.mode, model=model)
    print(f"Wrote eval report: {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
