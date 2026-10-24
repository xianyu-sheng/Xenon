# Execution benchmark

Local, real-model benchmark for the full delivery loop.  It is deliberately
outside pytest/CI because it needs model credentials and mutates throwaway git
repositories.

## Run

```bash
python evals/execution_benchmark.py --workdir D:/tmp/xenon-bench
python evals/execution_benchmark.py --cases calc,fix_bug,cli_summary
python evals/execution_benchmark.py --cases calc,fix_bug --repeat 3
```

Results are written to `<workdir>/benchmark.json` (or `--json PATH`).  Each
repeat gets a fresh repository (`<case>-rN`); raw runs are never replaced by
an aggregate.  The script returns non-zero when any run fails.

## Metrics

The JSON contains both raw runs and aggregate fields.  Failed and
infrastructure-failed runs stay in the denominator:

```text
completion_rate
false_completion_count       # TaskState completed but checks failed
repair_success_rate          # failed-tool runs that still delivered
median/p95 duration + tool calls
failure_categories
infrastructure_failure_count
completion_rate_excluding_infrastructure
```

`api_or_credentials`, `timeout`, `test_failure`, `dirty_worktree`,
`no_source_write`, `missing_commit`, `tool_security` and
`task_state_incomplete` are stable failure categories.  A benchmark is not a
guarantee; a single run is not a completion-rate claim.

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

## Latest repeated runs (2026-10-24)

Model: `deepseek/deepseek-v4-flash`; selected cases:
`calc,fix_bug,cli_summary`; `--repeat 3`.

### Run A — before stale-claim reconciliation

`D:/tmp/xenon-bench-repeat/report.json`

```text
7/9 passed, completion_rate=0.7778, false_completion_count=0
median 158.4s / 32 tools, p95 393.1s / 82 tools
failures: 2 × task_state_incomplete
```

The two `cli_summary` failures were the same real issue: a rejected
`worktree_clean`/`commit_created` claim remained after a later successful
clean/commit.  The fix is `TaskState.reconcile_satisfied_claims()`: later
delivery facts supersede old rejections while keeping them as audit history.

### Run B — after the fix attempt

`D:/tmp/xenon-bench-repeat2/report.json`

```text
4/9 passed, completion_rate=0.4444, false_completion_count=0
5 runs failed with api_or_credentials (HTTP 402)
infrastructure_failure_count=5
completion_rate_excluding_infrastructure=1.0
```

The provider returned `402 Payment Required`; those runs are retained in the
raw JSON and the denominator.  They do **not** validate the post-fix code, so
this document does not claim 9/9.  Re-run after restoring credentials and
publish the new raw JSON before making a stable-rate claim.

## Historical baseline (2026-10-22)

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
