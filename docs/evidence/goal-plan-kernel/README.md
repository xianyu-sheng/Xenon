# Kernel `/goal` and `/plan status` projection

Date: 2026-10-27  
Source: real `xenon exec --plan` run, ledger copied from
`~/.xenon/ledger/repl/goal-real-plan.jsonl`  
Task: inspect `a.txt` with one `read` capability step

## What this proves

The plan was accepted and the step closed by the kernel. `/goal` shows the
objective, phase, plan step and finalize state from the ledger; `/plan status`
shows the same plan below the legacy plan-mode toggle. Neither command replays
the task or reads the legacy goal event log.

```text
🎯 Kernel 目标：inspect a.txt
🧭 阶段：finalized · accepted: ? · evidence: —
🧭 Kernel 计划（1 步）
  1. [closed] inspect
🏁 finalized: commit=- evidence=verified

📋 计划模式：关闭（/plan on|off 切换）

🧭 Kernel 计划（1 步）
  1. [closed] inspect
```

`accepted= ?` is expected here: the deterministic CLI run did not write the
optional `.result.json` sidecar. The ledger itself is valid; a REPL kernel run
writes the sidecar and shows the real accepted flag.

## Verification

```text
Ledger.verify(): valid=True, events=28, last_seq=28
```

The existing real TTY MCP ledger
(`docs/evidence/tty-approval/ledger.jsonl`) also renders through `/goal`:

```text
🎯 Kernel 目标：## 查询需求 Use the configured GitHub MCP server ...
🧭 阶段：finalized · accepted: True · evidence: verified
🧭 Kernel 计划：（无）
🏁 finalized: commit=- evidence=verified
```
