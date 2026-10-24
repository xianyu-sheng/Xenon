# Live plan panel: TTY verification

The live panel is driven by `TaskStateProjection -> EventBus(plan_state)`, not
callback-owned text.  Automated coverage renders it deterministically:
`tests/test_plan_panel_render.py`.  The real ConPTY transcript for #174 is in
`docs/TTY_TRANSCRIPT_174.md`.

## Manual check (real terminal)

Use a throwaway repository and a real model key, then run:

```bash
mkdir /tmp/xenon-plan-panel && cd /tmp/xenon-plan-panel
git init -q
printf 'def add(a, b):\n    return a - b\n' > mathx.py
printf 'from mathx import add\n\ndef test_add():\n    assert add(2, 3) == 5\n' > test_mathx.py
git add . && git commit -qm baseline

xenon --mode plan-execute
```

Ask for: “修复 mathx.add 使测试通过，运行 pytest，提交修复，保持工作区干净”。

Expected observations:

1. `计划 vN · 执行: ...` appears when a step starts.
2. A second, changed signature updates the line; repeated identical snapshots
   do not spam duplicate lines.
3. A replan/remediation gets a new plan version/step row.
4. Final `Plan vN` panel shows step statuses, failed row errors, and
   `交付缺口: ...` when delivery is incomplete.
5. The final panel comes from `TaskState.plan_summary()`, not from a callback
   summary.

Capture the terminal transcript/screenshot without secrets or private paths.
Known limitation: Windows terminals with ambiguous-width CJK fonts may wrap a
long step label differently; the renderer intentionally truncates labels to
100 characters for the live line and 160 for the final panel.
