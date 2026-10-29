# Real TTY — PlanFirst execution and final synthesis

Date: 2026-10-29
Runtime: `XENON_RUNTIME=kernel` via a real ConPTY (`pywinpty`)
Model: `deepseek/deepseek-v4-pro`
Mode: `/mode plan-execute`
Task: read a random one-line probe with the real `read` tool

## What this proves

The kernel PlanFirst path now does three things in a real REPL:

1. `/mode plan-execute` selects the kernel PlanFirst strategy;
2. the model submits a capability plan with one `read` step, which the runtime
   accepts and executes;
3. after the step completes, the PlanFirst finisher reads the deterministic
   working set and returns the actual token as the final answer.

The earlier version of this run returned the literal string `completed`; that
was the bug fixed by the final synthesis provider.

## Transcript excerpt

```text
❯ /mode plan-execute

╭─ /mode ──────────────────────────────────────────────────────╮
│ ✅ 已切换到范式: plan-execute — 先用强模型规划，再逐步执行，适合复杂任务
╰──────────────────────────────────────────────────────────────╯

❯ 请用 read 工具读取工作区根目录的 plan_probe.txt，并只回答文件中的字符串；不要猜。

🧠 Runtime: kernel (deepseek/deepseek-v4-pro)

  ⠿ exploring  read  ·  Ctrl+O details
  ✓ explored  1 tool call(s)  ·  Ctrl+O details
  💭 kernel · 3 steps · 1 tools · verified

● Kernel

  PLAN_EXEC_2F7799DADD02C6E9
```

## Ledger evidence

`ledger.jsonl` + `ledger.anchor` verify locally (`valid=True`):

```text
plan_accepted   one step: inspect/read plan_probe.txt
tool_receipt    read content: PLAN_EXEC_2F7799DADD02C6E9
delivered       answer:       PLAN_EXEC_2F7799DADD02C6E9
finalized       evidence_status=verified, commit=null
```

`result.json`:

```json
{
  "accepted": true,
  "answer": "PLAN_EXEC_2F7799DADD02C6E9",
  "claims": [],
  "debt": [],
  "evidence_status": "verified",
  "finalize_sha": null,
  "metrics": {"steps": 3.0, "tool_calls": 1.0}
}
```

This was a read-only task, so the finalizer correctly made no commit.

## Regression coverage

- `tests/harness/test_llm.py`: plan prompt real newlines; plan finisher rejects
  non-finish actions.
- `tests/kernel/test_strategies.py`: `PlanFirst` calls the finisher after steps.
- `tests/test_repl_kernel_runtime.py`: adapter plan path returns the synthesized
  answer.

## Scope

The finisher is a strategy provider, not a kernel writer: it only returns a
`Finish` action. It never executes a tool; if the model returns anything other
than `finish`, the provider falls back to a generic completed answer.
