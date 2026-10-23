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
Latest run: 10/10 cases passed
calc:            8.4s,  7 tools, 0 failures
fix_bug:        44.0s, 13 tools, 0 failures
cli_summary:    38.4s, 13 tools, 1 failure
json_report:    45.0s, 13 tools, 1 failure
multi_file:     44.5s, 13 tools, 1 failure
rename_function:46.3s, 13 tools, 1 failure
two_bugs:       52.0s, 12 tools, 1 failure
format_price:   54.1s, 15 tools, 1 failure
safe_div:       44.1s, 13 tools, 0 failures
config_default: 38.1s, 14 tools, 0 failures

A previous run was 9/10: `safe_div` had staged-but-uncommitted source and the
delivery gate kept TaskState active.  That failure is retained in the history;
it is evidence that completion is variable, not a fixed guarantee.

## What this does not prove

- It is not a SWE-bench score and does not cover arbitrary repositories.
- Three cases are too small to estimate a general completion rate; add cases
  before making broader claims.
- The script uses the user's configured model credentials; do not run it in CI.
