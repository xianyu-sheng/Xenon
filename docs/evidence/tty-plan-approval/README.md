# Real TTY — kernel plan approval execution

Date: 2026-10-30
Runtime: `XENON_RUNTIME=kernel` through a real ConPTY (`pywinpty`)
Model: `deepseek/deepseek-v4-pro`
Workspace: temporary git repo with `plan_probe.txt` committed as baseline
Task: create `plan_out.txt` with the probe token, then verify the write

## What this proves

This is a real terminal run, not a mock or an in-process test:

1. `/plan on` routes a deferred-write turn to **kernel plan-only**;
2. the model submits an `edit` + `verify` plan; no tool executes and no file is written;
3. `/plan approve` records the session decision and calls `resume_kernel_plan()`;
4. the **same ledger** moves `PLANNED -> RUNNING` and executes the accepted plan;
5. V1 binds `file_created` to the real `apply_patch` receipt;
6. the finalizer creates commit `2de3a56f45be`;
7. `/plan status` shows `phase=finalized · commit=2de3a56f45be`;
8. `/evidence` verifies a 45-event chain with `evidence: verified`.

The file after execution:

```text
PLAN_APPROVAL_2F7799DADD02C6E9
```

## Transcript excerpt

```text
❯ /plan on
✅ 计划模式已开启

❯ 创建一个新文件 plan_out.txt，内容为 PLAN_APPROVAL_2F7799DADD02C6E9 加一个换行。
  只做这一件事，不要读取其他文件，不要 push。

● Kernel · plan
  Create new local file plan_out.txt ... using the edit capability,
  then verify the write deterministically with a read.

   1 edit · plan_out.txt
   2 verify · plan_out.txt
· 计划已接受，执行等待确认；本轮未写盘。

❯ /plan approve
  ✓ explored  apply_patch · ['...plan_out.txt']
  ✓ explored  read · Ctrl+O details
  ✓ explored  finalizer · Ctrl+O details
  💭 kernel · 3 steps · 2 tools · verified

● Kernel
  Plan execution completed; final summary unavailable.
  交付: commit 2de3a56f45be · evidence: verified

❯ /plan status
✅ 计划已批准并执行（phase=finalized · commit=2de3a56f45be）

❯ /evidence
链: ✅ 完整 · 45 条事件 · head 808abd4c3228
阶段: finalized · accepted: True · evidence: verified
🧾 提交: 2de3a56f45be420355e7b33656a02c28908dd05c
🧮 V2 覆盖：2 项（bound 2）
🏁 finalized: commit=2de3a56f45be420355e7b33656a02c28908dd05c evidence=verified
```

## Result

`result.json`:

```json
{
  "accepted": true,
  "answer": "Plan execution completed; final summary unavailable.",
  "debt": [],
  "evidence_status": "verified",
  "finalize_sha": "2de3a56f45be420355e7b33656a02c28908dd05c",
  "phase": "finalized",
  "metrics": {"steps": 3.0, "tool_calls": 2.0, "events": 45.0}
}
```

## Ledger

`ledger.jsonl` + `ledger.jsonl.anchor` verify locally with `Ledger.verify()`
(`valid=True`). Key sequence:

```text
task_started
plan_proposed
plan_accepted
state_transition: running -> planned
...
plan_approved (approval_id + plan_digest)
state_transition: planned -> running
step_started
tool_receipt: apply_patch (FsWrite.created_paths includes plan_out.txt)
step_verdict: satisfied
tool_receipt: read
step_verdict: satisfied
tool_receipt: finalizer
delivered
finalized: commit=2de3a56..., evidence=verified
```

## Bugs this TTY run found and fixed

The run was intentionally kept as evidence because it exposed real integration bugs:

- `no_write` from a scoped phrase (“不要修改 plan_probe.txt”) could strip an
  unrelated `create` operation; the run used an equivalent “保留不变” prompt.
  The scoping bug remains tracked separately.
- plan-only prompt originally told the planner the execution boundary was
  read-only, so it omitted write steps; plan-only now allows write steps in the
  plan while the runtime remains read-only.
- empty/invalid plans were accepted as `PLANNED`; they now fail closed.
- model plan vocabulary drift (`write` kind, `file_content_matches` expectation)
  is normalized by the plan parser.
- `apply_patch` was treated as an unknown-target boundary approval; its
  `files/edits` targets are now extracted.
- approved execution temporarily raises the REPL `_execution_level` to
  `EXECUTE`, then restores it.
- absolute `changed_paths` did not populate `FsWrite.created_paths`; both
  relative and absolute before-snapshots are now recorded.

## Reproduce

With a configured model and `winpty`:

```bash
XENON_TTY_WORKSPACE=C:/tmp/xenon-tty-plan \
XENON_TTY_TRANSCRIPT=docs/evidence/tty-plan-approval/transcript.txt \
python scripts/tty_plan_approval.py
```

The script creates a temporary git repo, commits `plan_probe.txt` as baseline,
runs `/plan on`, asks the model for a plan, runs `/plan approve`, and copies the
newest kernel ledger/result into the evidence directory.
