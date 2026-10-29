# Mode catalog routing — real TTY evidence

Date: 2026-10-29  
Code baseline: `ee73f79` (`feat(repl): route modes by kernel capability`)  
Runtime: `XENON_RUNTIME=kernel` via a real ConPTY (`pywinpty`)  
Model: `deepseek/deepseek-v4-pro`  
Mode: `/mode react-reflection`  
Task: read a random one-line probe file with the real `read` tool

## What this proves

`react-reflection` is one of the modes that has no kernel strategy yet. Before
this slice it was routed to the kernel and silently executed as ActFirst; after
this slice it stays on the frozen legacy path.

The transcript shows all three facts in one real session:

1. `/mode react-reflection` is accepted and the status bar changes to
   `react-reflection`;
2. the real `read` tool runs against `mode_catalog_probe.txt` and returns the
   random token;
3. the result panel is `React+Reflection 结果` and the transcript never contains
   `Runtime: kernel`.

The transcript is the artifact for this legacy path; there is no kernel ledger
for this turn.

## Transcript excerpt

```text
❯ /mode react-reflection

╭─ /mode ──────────────────────────────────────────────────────╮
│ ✅ 已切换到范式: react-reflection — ReAct 探索 + 反思审查，适合需要工具且要求高质量的任务
╰──────────────────────────────────────────────────────────────╯

❯ 请必须调用 read 工具读取工作区根目录的 mode_catalog_probe.txt，并只回答文件中的字符串；不要猜。

  ⠿ exploring  read · mode_catalog_probe.txt  ·  Ctrl+O details
  ✓ explored  1 tool call(s)  ·  Ctrl+O details
  🔍 reviewing  ✓ 10/10
  ⠿ exploring  read · mode_catalog_probe.txt  ·  Ctrl+O details
  ✓ explored  2 tool call(s)  ·  Ctrl+O details
  💭 3 步 · 2 个工具  [Ctrl+O 展开详情]

● React+Reflection 结果

  MODE_CATALOG_7B0B41C81A4B3A70
```

Checked against the raw transcript:

```text
/mode react-reflection     present
MODE_CATALOG_7B0B...       present
Runtime: kernel            absent
Kernel                     absent
```

## Why not `/mode reflection`?

`reflection` is an LLM-only review loop; it does not call tools. Running it
would prove the mode label stayed legacy, but would not exercise a tool turn.
`react-reflection` is also legacy-only and does use `read`, so it covers both
the routing guard and real tool execution.

The unit matrix in `tests/test_mode_catalog.py` and
`tests/test_repl_kernel_runtime.py` covers the full set:

```text
direct             act
react              act
plan-execute       plan
reflection         legacy
plan-react         legacy
plan-reflection    legacy
react-reflection   legacy
unknown            fail closed
```

## Scope note

While preparing this evidence, the legacy combined-engine
`ExecutionEvidence._workspace_snapshot` hit a Windows locale decoding issue on
an uncommitted Chinese diff (`diff.stdout` was `None`). That issue was fixed
separately in #257: the snapshot now decodes Git output as UTF-8 with
replacement, and treats a missing `stdout` as an empty snapshot. This evidence
itself was rerun from a clean code baseline before that fix, so it does not
exercise the encoding path.
