# TTY transcript — core tool profile coding smoke (#190)

Date: 2026-10-24  
Model: `deepseek/deepseek-v4-pro`  
Profile: `XENON_TOOL_PROFILE=core` (7 visible tools)  
Driver: ConPTY via `pywinpty`; isolated credentials and workspace  
Raw capture: `D:/tmp/xenon-core-smoke/tty.raw.txt`

Task:

> 修复 `math_utils.add` 的 bug：它现在返回 `a-b`，但 `test_math_utils.py` 期望 `a+b`。请实际修改源码，运行 `python -m pytest test_math_utils.py -q`，然后 `git commit`；不要改测试来绕过，不要 push。

Observed tool sequence:

1. `read` `math_utils.py`
2. `read` `test_math_utils.py`
3. `apply_patch` — change `a - b` to `a + b`
4. `bash` — `python -m pytest test_math_utils.py -q` → **1 passed**
5. `bash` — `git add` + `git commit`
6. `bash` — verification nudge reran pytest → **1 passed**
7. `bash` — `git log` + `git status --short` → clean

No `run_tests`, `git`, or `write_file` model-facing tools were used.

Final result:

- new commit `1db0f10`
- target test: **1 passed**
- worktree: clean
- `tools 7`
- no push (and no PR required after the negative-push delivery fix)

```text
● ReAct 结果
  验证完成，证据如下：

  改动：math_utils.py 中 add(a, b) 由 return a - b 改为 return a + b（仅此一处，未改测试）。

  验证命令与真实输出：


   > python -m pytest test_math_utils.py -q
   .                                                                        [100%]
   1 passed in 0.01s


  Git 状态：


   > git log --oneline -2
   1db0f10 Fix add() to return a + b instead of a - b
   8947f2b chore: seed failing add

   > git status --short
   （无输出 = 工作区干净，改动已全部提交）


   • 测试通过：1 passed；
   • 已 commit（1db0f10），未 push；
   • 工作区干净，无未提交改动。
  ❯
  ○ API configured  ·  auto · …seek/deepseek-v4-pro  ·  react  ·  context 5.1%  ·  cache 97%  ·  ¥0.01  ·  tools 7  ·  00:30─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
```

