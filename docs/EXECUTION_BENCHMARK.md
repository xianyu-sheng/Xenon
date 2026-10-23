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
10/10 cases passed
calc:          8.9s,   6 tools, 0 failures
fix_bug:      41.4s,  13 tools, 1 failure
cli_summary: 223.8s,  29 tools, 9 failures
json_report:  51.6s,  16 tools, 1 failure
multi_file:   36.0s,  13 tools, 1 failure
rename_function: 104.4s, 33 tools, 3 failures
two_bugs:    138.1s,  36 tools, 4 failures
format_price: 37.6s,  13 tools, 1 failure
safe_div:     38.4s,  13 tools, 1 failure
config_default: 40.0s, 13 tools, 1 failure
```

The real SmartBench #26 slice also finished with target tests `2 passed`, a
source commit, a clean worktree and `TaskState.completed` in the same run
(81 tools, 17 failed tool calls, 334.4s).

## What this does not prove

- It is not a SWE-bench score and does not cover arbitrary repositories.
- Three cases are too small to estimate a general completion rate; add cases
  before making broader claims.
- The script uses the user's configured model credentials; do not run it in CI.
