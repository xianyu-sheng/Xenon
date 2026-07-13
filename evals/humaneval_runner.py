"""
HumanEval benchmark adapter for omniagent.

HumanEval (OpenAI, 2021): 164 Python function-completion tasks.
Metric: pass@k (probability that at least 1 of k samples passes all tests).

Usage:
    python evals/humaneval_runner.py --model deepseek/deepseek-v4-pro --num-tasks 20
    python evals/humaneval_runner.py --model deepseek/deepseek-v4-pro --num-tasks 164
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

_DATASET_PATH = Path("/tmp/HumanEval.jsonl")


def load_tasks(path: Path | None = None) -> list[dict[str, Any]]:
    path = path or _DATASET_PATH
    tasks = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                tasks.append(json.loads(line))
    return tasks


def build_prompt(task: dict[str, Any]) -> str:
    """Build a natural code-completion prompt.

    Give the LLM the full function stub (imports + helpers + signature + docstring)
    and ask it to complete the implementation. The extract_code function will
    reliably pull out just the body.
    """
    return (
        "Complete the following Python function. Output ONLY the full function "
        "including the def line and body. No explanation, no markdown.\n\n"
        f"{task['prompt']}"
    )


def extract_code(generated: str, entry_point: str) -> str:
    """Extract the indented function body from LLM output.

    Handles three common LLM output patterns:
    1. Markdown code block wrapping
    2. Full function including def line
    3. Bare indented body

    Returns only the indented body lines (suitable for appending to the prompt).
    """
    code = generated

    # 1) Extract from markdown code block if present
    blocks = list(re.finditer(r"```(?:python)?\s*\n(.*?)```", code, re.DOTALL))
    for m in reversed(blocks):
        block = m.group(1)
        # Prefer the block that contains our function
        if f"def {entry_point}" in block:
            code = block
            break
    else:
        # Fallback: use the largest block with indented content
        if blocks:
            code = max(blocks, key=lambda m: len(m.group(1))).group(1)

    lines = code.split("\n")

    # 2) Locate the function definition
    def_idx = -1
    for i, line in enumerate(lines):
        if line.strip().startswith(f"def {entry_point}("):
            def_idx = i
            break

    if def_idx >= 0:
        # We found the def line — extract everything after the signature + docstring
        body_start = def_idx + 1
        # Determine base indentation from the def line
        def_indent = len(lines[def_idx]) - len(lines[def_idx].lstrip())
        body_indent = def_indent + 4  # standard 4-space body indent

        # Skip docstring if present
        for i in range(def_idx + 1, len(lines)):
            s = lines[i].strip()
            if s.startswith('"""') or s.startswith("'''"):
                dq = s[:3]
                # Multi-line docstring: skip until closing
                if s.count(dq) < 2 or len(s) == 3:
                    for j in range(i + 1, len(lines)):
                        if dq in lines[j]:
                            body_start = j + 1
                            break
                else:
                    body_start = i + 1
                break
            elif s:  # Non-empty, non-docstring line = body started
                body_start = i
                break
            else:
                body_start = i + 1

        # Collect body lines — stop at lines with less indentation (end of function)
        body_lines: list[str] = []
        for i in range(body_start, len(lines)):
            line = lines[i]
            stripped = line.strip()
            # Empty lines are part of body
            if not stripped:
                body_lines.append("")
                continue
            # Line with less/equal indent than def = outside function, stop
            line_indent = len(line) - len(line.lstrip())
            if line_indent <= def_indent:
                break
            body_lines.append(line)
        return "\n".join(body_lines)

    else:
        # No def line found — the output is already just body lines
        # Filter out trailing noise (markdown fence, explanations)
        body_lines: list[str] = []
        for line in lines:
            stripped = line.strip()
            if stripped in ("```",) or stripped.startswith("Here"):
                continue
            body_lines.append(line)
        # Trim trailing empty/noise lines
        while body_lines and (not body_lines[-1].strip()
                              or body_lines[-1].strip() in ("```",)
                              or body_lines[-1].strip().startswith("Here")):
            body_lines.pop()
        # If all we have is comment lines with no indentation, something went wrong
        if body_lines and all(not l.startswith((" ", "\t")) for l in body_lines if l.strip()):
            # Try to find any indented section
            for i, l in enumerate(body_lines):
                if l.startswith("    ") or l.startswith("\t"):
                    body_lines = body_lines[i:]
                    break
        return "\n".join(body_lines)


def evaluate_task(task: dict[str, Any], completion: str) -> dict[str, Any]:
    """Evaluate a HumanEval task.

    The prompt contains imports + helper functions + function signature.
    The completion is just the indented body.
    Full code = prompt + body + test + check().
    """
    full_code = (
        task["prompt"] + "\n"
        + completion + "\n\n"
        + task["test"] + "\n\n"
        + f"check({task['entry_point']})\n"
    )

    result = {"task_id": task["task_id"], "passed": False, "error": None}
    namespace: dict[str, Any] = {}
    try:
        exec(full_code, namespace)
        result["passed"] = True
    except AssertionError:
        result["error"] = "AssertionError"
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"

    return result


def run_humaneval(
    model: str = "deepseek/deepseek-v4-pro",
    num_tasks: int = 20,
    num_samples: int = 1,
    dataset_path: Path | None = None,
) -> list[dict[str, Any]]:
    from omniagent.utils.llm_client import chat_completion

    tasks = load_tasks(dataset_path)[:num_tasks]
    results = []

    for i, task in enumerate(tasks):
        task_id = task["task_id"]
        prompt = build_prompt(task)
        print(f"[{i+1}/{num_tasks}] {task_id} ...", end=" ", flush=True)

        samples = []
        for _ in range(num_samples):
            try:
                for v in list(os.environ.keys()):
                    if "proxy" in v.lower():
                        os.environ.pop(v, None)

                response = chat_completion(
                    model_id=model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.0 if num_samples == 1 else 0.8,
                    max_tokens=1024,
                )
                completion = extract_code(response, task["entry_point"])
                eval_result = evaluate_task(task, completion)
                samples.append(eval_result)
                if eval_result["passed"]:
                    break
            except Exception as e:
                samples.append({
                    "task_id": task_id,
                    "passed": False,
                    "error": f"API: {e}",
                })

        any_passed = any(s["passed"] for s in samples)
        status = "PASS" if any_passed else "FAIL"
        err = samples[0].get("error", "")
        detail = f"({err})" if err and not any_passed else ""
        print(f"{status} {detail}")

        results.append({
            "task_id": task_id,
            "passed": samples[0]["passed"] if samples else False,
            "pass_at_k": any_passed,
            "samples": len(samples),
            "error": samples[0].get("error") if not any_passed else None,
        })

    return results


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(results)
    passed = sum(1 for r in results if r["pass_at_k"])
    return {
        "total": total,
        "passed": passed,
        "pass_rate": f"{passed}/{total} ({passed/total*100:.1f}%)" if total else "N/A",
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="HumanEval benchmark for omniagent")
    p.add_argument("--model", default="deepseek/deepseek-v4-pro")
    p.add_argument("--num-tasks", type=int, default=20)
    p.add_argument("--num-samples", type=int, default=1)
    p.add_argument("--output", default="/tmp/humaneval_report.json")
    args = p.parse_args(argv)

    print(f"HumanEval via omniagent")
    print(f"  Model:  {args.model}")
    print(f"  Tasks:  {args.num_tasks}")
    print(f"  Pass@k: k={args.num_samples}")
    print()

    results = run_humaneval(
        model=args.model,
        num_tasks=args.num_tasks,
        num_samples=args.num_samples,
    )

    summary = summarize(results)
    print(f"\n{'='*50}")
    print(f"Result: {summary['pass_rate']}")
    print(f"{'='*50}")

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    print(f"Report: {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
