# Execution benchmark

Local, real-model benchmark for the full delivery loop.  It is deliberately
outside pytest/CI because it needs model credentials and mutates throwaway git
repositories.

## Run

```bash
python evals/execution_benchmark.py --workdir D:/tmp/xenon-bench
python evals/execution_benchmark.py --cases calc,fix_bug,cli_summary
```

Results are written to `<workdir>/benchmark.json` (or `--json PATH`).  The
script returns non-zero when any case fails.

## Cases

| Case | What it checks |
|---|---|
| `calc` | create source + test, run target pytest, commit, clean tree |
| `fix_bug` | fix a failing source assertion, rerun the same target test, commit |
| `cli_summary` | implement deterministic formatting, test, commit |

A case passes only when all of these are true:

- target test exits 0;
- a new commit exists;
- `git status --porcelain -uall` is empty;
- `TaskState.missing_delivery()` is empty;
- `TaskState.status == "completed"`;
- case-specific source behaviour is correct.

## Baseline (2026-10-22)

Model: `deepseek/deepseek-v4-flash`

```text
3/3 cases passed
calc:        7.9s,  6 tools, 0 failures
fix_bug:    85.5s, 22 tools, 3 failures
cli_summary:33.1s, 14 tools, 1 failure
```

The real SmartBench #26 slice also finished with target tests `2 passed`, a
source commit, a clean worktree and `TaskState.completed` in the same run
(81 tools, 17 failed tool calls, 334.4s).

## What this does not prove

- It is not a SWE-bench score and does not cover arbitrary repositories.
- Three cases are too small to estimate a general completion rate; add cases
  before making broader claims.
- The script uses the user's configured model credentials; do not run it in CI.
