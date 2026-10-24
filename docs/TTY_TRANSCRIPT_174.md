# #174 real-TTY transcript (2026-10-24)

Environment: Windows 11, Python 3.14, Xenon 0.9.5, ConPTY via `pywinpty`,
real `deepseek/deepseek-v4-pro`, `xenon --mode plan-execute`, throwaway git
repositories under `D:/tmp`.  Permission prompts were auto-confirmed via
`XENON_ASSUME_YES=1` so the transcript focuses on plan rendering.

## Live plan, claim rejection and replan

Real task: create `calc.py` + `tests/test_calc.py`, run pytest, commit.

```text
⠿ planning  1/6 · 确认工作区结构与现有文件
✓ plan step 1 complete
⠿ planning  2/6 · 创建 calc.py 定义 add 函数
⠿ planning  3/6 · 创建 tests/test_calc.py 测试文件
⚠ Claim gate 拒绝: [step:3:source_write:] source_write:
  期望 本步骤必须新增成功的非测试源码写入，未通过（缺少源码写入证据）
⠿ exploring  batch_write · [{'path': 'calc.py', ...}]
✓ explored  2 tool call(s)
✓ plan step 2 complete
✓ plan step 3 complete
⠿ planning  4/6 · 运行 pytest 验证测试通过
✓ plan step 4 complete
⠿ planning  5/6 · git add 两个新文件
✓ plan step 5 complete
⠿ planning  6/6 · git commit 提交改动
⚠ Claim gate 拒绝: [step:6:commit_created:] commit_created:
  期望 本步骤必须产生新的真实 commit，未通过（缺少 commit）
⚠ 步骤失败，已重规划剩余 1 步 (1/3)
⠿ planning  6/6 · git commit 提交改动
```

The task eventually produced a real commit (`de2459a feat: add calc`) and its
target test passed (`2 passed`).  This confirms live step transitions and a
real replan are driven by execution facts; the claim rejection is shown, not
hidden.

## Final panel found a real display bug

A second, read-only Plan-Execute turn initially had live plan lines but **no
final panel**: the intent path did not create `TaskState`, while the renderer
only read `TaskState.from_context()`.  Fixed in
`EngineHost._render_plan_state`: when TaskState is absent, replay the same
`EvidenceLedger` PLAN events into a display-only `TaskStateProjection`; never
read callback-owned text.

After the fix, the same real TTY turn rendered:

```text
● Plan-Execute 结果
  最终总结
  成功：
   • 读取 README.md：成功，工具执行记录确认 read_file 返回内容为 # read fixture。
   • 一句话总结第一行：成功，README.md 第一行是 Markdown 一级标题，内容为“read fixture”。
  失败：无。
  跳过：无。

Plan v1
[OK] 读取 README.md 文件内容
[OK] 用一句话总结 README.md 第一行内容
```

The panel is from the event projection and shows step status.  Delivery-gap
rendering is covered by `tests/test_plan_panel_render.py` (a TaskState with
`required=["verify","commit"]` renders `交付缺口`), and live-line deduplication
is covered by the same test file.

## Limitations

- This is a terminal transcript, not a screenshot; ANSI color/width is
  intentionally stripped.
- Approval prompts were auto-confirmed for the run; a manual approval prompt
  is still covered by the existing REPL tests.
- Temporary fixture paths are test-only and contain no secrets.
