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
| `json_report` | expose top-level JSON fields from nested input, test, commit |
| `multi_file` | fix a package module and commit only the source file |
| `rename_function` | add a requested API name while preserving compatibility |
| `two_bugs` | fix two independent arithmetic bugs |
| `format_price` | implement deterministic money formatting |
| `safe_div` | fix a boundary condition without changing tests |
| `config_default` | implement a missing default value |

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
Latest run: 9/10 cases passed
calc:           49.8s, 18 tools, 0 failures  PASS
fix_bug:        42.6s, 12 tools, 0 failures  PASS
cli_summary:    40.6s, 12 tools, 0 failures  PASS
json_report:    79.9s, 22 tools, 0 failures  PASS
multi_file:     44.8s, 13 tools, 0 failures  PASS
rename_function:29.3s, 11 tools, 1 failure   PASS
two_bugs:       47.8s, 12 tools, 1 failure   PASS
format_price:   46.0s, 13 tools, 0 failures  PASS
safe_div:      103.8s, 32 tools, 0 failures  FAIL
config_default: 86.5s, 19 tools, 2 failures  PASS

safe_div failure: target tests passed and a commit existed, but the final
worktree still had an uncommitted `mathx.py` change.  The delivery gate kept
TaskState active instead of blessing it.  A previous run was 10/10; this is a
real variance sample, not a stable guarantee.
```

The real SmartBench #26 slice also finished with target tests `2 passed`, a
source commit, a clean worktree and `TaskState.completed` in the same run
(81 tools, 17 failed tool calls, 334.4s).

## What this does not prove

- It is not a SWE-bench score and does not cover arbitrary repositories.
- Three cases are too small to estimate a general completion rate; add cases
  before making broader claims.
- The script uses the user's configured model credentials; do not run it in CI.
