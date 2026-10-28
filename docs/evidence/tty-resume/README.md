# Real TTY `/resume` with a kernel ledger pointer

Date: 2026-10-27  
Runtime: `XENON_RUNTIME=kernel` through a real ConPTY (`pywinpty`)  
Model: `deepseek/deepseek-v4-pro`  
Task: read-only `README.md` first-line query

## What this proves

The session was saved with `/save tty-resume-kernel` after a kernel turn.
The REPL was exited and restarted; `/resume tty-resume-kernel` restored only
the ledger pointer. `/goal` and `/evidence` then read the real kernel ledger
without replaying the task or restoring an approval.

Transcript excerpt:

```text
📂 恢复会话
  1  tty-resume-kernel · 3 条消息 ·  · 刚刚
选择会话: 0

❯ /resume tty-resume-kernel
✅ 已恢复会话 (刚刚) · 3 条消息 · 范式: direct

❯ /goal
🎯 Kernel 目标：...
🧭 阶段：finalized · accepted: True · evidence: verified
🧭 Kernel 计划：（无）
🏁 finalized: commit=- evidence=verified

❯ /evidence
🔗 Kernel evidence · repl-3a946fb773d4
链: ✅ 完整 · 22 条事件 · head 641b0f22c92d
   🔧 1 次（read ×1）· 失败 0
📤 README.md 的第一行是：# Xenon
🏁 finalized: commit=- evidence=verified
```

## Files

- `transcript.raw.txt` — raw plain-text TTY capture (includes startup animation)
- `ledger.jsonl` + `ledger.jsonl.anchor` — kernel turn ledger
- `result.json` — kernel task result

`Ledger.verify()` is valid with the copied anchor.

## Notes

- `/resume` restores a pointer; it does not replay the read or resurrect the
  approval state.
- The `/goal` objective includes the REPL's existing intent-prompt wrapper
  (`## 查询需求 ...`), because that was the objective passed to the kernel.
- This test used a read-only task and produced no commit.
