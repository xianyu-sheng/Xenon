# Xenon SWE-bench Lite seven-engine calibration — combined-engine root fix

This report covers every Xenon execution engine that remains after removing the
`novel` engine. It evaluates one unchanged official SWE-bench Lite instance
across seven engine-instance cells. It is a pipeline and engine calibration,
not Xenon's overall SWE-bench score or an estimate of production correctness.

## Evaluation contract

- Dataset: `SWE-bench/SWE-bench_Lite`, `test` split
- Instance: `astropy__astropy-12907`
- Model: `deepseek/deepseek-v4-pro`
- Provider: DeepSeek official API (`api.deepseek.com`), not Ark or Coding Plan
- Evaluator: unmodified `swebench==4.1.0` harness
- Official image: `swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest`
- Image ID: `sha256:f3f63bb87d581c0e7b47f900dd82165b71040e1758d3c29e915e2b18da9baf63`
- Each engine used an independent worktree, prediction and official harness run.
- No official task, fixture, repository tests, image or grader was modified.
- The six non-`plan-reflection` rows were generated from `7f21e92`.
- The final `plan-reflection` row is the independent r2 rerun from `73e243c`,
  after its repair budget was aligned with the engine execution budget.

## Separated results

- Verified Success Rate (engine-instance cells): **57.14% (4/7)**
- Official Result Assertion Pass Rate: **57.14% (4/7)**
- Tool Execution Success Rate: **90.70% (39/43)**
- Cache Rails Hit Rate (provider token-weighted): **89.33%**
- Reusable Tokens (provider cache-hit tokens): **544,000**
- Provider Calls: **55**
- Prompt Tokens: **608,995**
- Completion Tokens: **32,681**
- Total Tokens: **641,676**
- Generation elapsed time: **696.109 seconds**
- Estimated Tokens Saved: **N/A** — cache-hit tokens prove reuse, but do not
  prove an equal reduction in billed tokens.
- Estimated Cost Saved: **N/A** — this report does not invent a currency value
  without a supported provider billing conversion.
- Context Compression Benefit: **N/A** — compression did not trigger in these
  traces.

Correctness, tool reliability and cache reuse are deliberately separate. The
89.33% cache-hit rate does not increase the 57.14% verified success rate.

## Per-engine matrix

| Engine | Official status | Patch | Tools | Calls | Prompt | Completion | Cache hits | Cache hit rate | Elapsed |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| direct | empty_patch | 0 B | 0/0 | 1 | 522 | 659 | 512 | 98.08% | 30.520 s |
| react | **resolved** | 504 B | 8/9 | 10 | 188,154 | 4,577 | 168,832 | 89.73% | 96.838 s |
| plan-execute | empty_patch | 0 B | 3/3 | 2 | 3,683 | 1,086 | 2,560 | 69.51% | 39.269 s |
| reflection | empty_patch | 0 B | 0/0 | 4 | 6,777 | 10,634 | 2,944 | 43.44% | 227.552 s |
| plan-react | **resolved** | 504 B | 9/10 | 12 | 134,589 | 4,686 | 119,552 | 88.83% | 94.869 s |
| plan-reflection (r2) | **resolved** | 504 B | 10/11 | 14 | 137,802 | 5,667 | 124,160 | 90.10% | 104.557 s |
| react-reflection | **resolved** | 504 B | 9/10 | 12 | 137,468 | 5,372 | 125,440 | 91.25% | 102.504 s |

The four non-empty patches independently passed the official harness. The
other three predictions were classified by the official harness as
`empty_patch`; they are failures for the verified-success calculation even
when their model text claimed progress or their read-only tools succeeded.

## Root causes addressed

The combined engines were failing at orchestration boundaries rather than at
one Astropy-specific code path. The fixes are task-independent:

1. **Plan/ReAct state loss and repeated work.** Substeps now execute in isolated
   contexts and hand off structured execution evidence. Successful edits and
   tests suppress already-satisfied implementation, test, verification and
   summary steps instead of relying on a natural-language warning.
2. **Reflection could replace facts with plausible text.** Reviews are bounded,
   fail-closed JSON. A write task cannot pass without a successful state-change
   tool record, and a repair response cannot replace the original result unless
   it actually changes the workspace.
3. **Repair lacked a real execution path.** Failed reviews now enter an
   independent tool-capable ReAct repairer. Initial and repair traces are
   aggregated so failed commands remain visible.
4. **Repair budget was disconnected from engine budget.** Reflection repair now
   follows the configured execution budget, capped at ten rounds, instead of a
   fixed five-round cutoff. The `plan-reflection` r2 rerun verifies this fix.
5. **Capabilities stopped at the wrapper.** MCP tools, execution policy,
   `ToolRuntime`, and the actual child-engine model identity now propagate
   through the complete engine graph.
6. **Execution evidence was ambiguous.** Tool success/failure, state-changing
   tools, changed files, test commands, error details and Git state are recorded
   separately. Calling a tool is no longer treated as successful execution.
7. **`novel` was an incompatible execution mode.** Its engine, manager, REPL
   command, dispatch, evaluation entry points, tests and package metadata were
   removed. Clean wheel verification prevents stale build artifacts from
   reintroducing deleted modules.

These changes are intentionally general orchestration invariants; no benchmark
instance, file name, Astropy path or expected patch is special-cased.

## Remaining findings

- `direct` returned provider-specific tool markup but did not enter an executed
  tool path, so the official prediction was empty.
- `plan-execute` successfully inspected and reproduced the bug but did not make
  a state-changing edit, so its successful read-only tools do not count as task
  completion.
- `reflection` produced no executable workspace change and was correctly
  reported as an empty patch.

These failures remain visible rather than being hidden by the four successful
engines. More official instances are required before making any claim about
overall Xenon or per-engine correctness.

## Efficiency comparison with the previous calibration

The prior eight-engine calibration used 89 provider calls, 1,149,083 total
tokens and 1,838.911 seconds. This seven-engine post-fix calibration used 55
calls, 641,676 total tokens and 696.109 seconds:

- Total tokens: **44.16% lower**
- Elapsed generation time: **62.15% lower**
- `plan-react`: 49 to 12 calls, **77.79% fewer tokens**, **79.48% less elapsed
  time**, while remaining officially resolved.
- `plan-reflection`: changed from `empty_patch` to officially resolved. Its
  additional repair work is correctness cost, not presented as an efficiency
  gain.
- `react-reflection`: 13 to 12 calls, **21.66% fewer tokens**, **66.13% less
  elapsed time**, while remaining officially resolved.

This comparison is diagnostic only because the implementation changed between
runs. It is not a provider-price or benchmark-score claim.

## Artifact layout

Each `attempts/<engine>/` directory contains:

- `predictions.jsonl` — the exact engine-specific SWE-bench prediction
- `xenon_result.json` — Xenon's execution result and evidence
- `official-harness/report.json` — the unmodified official harness verdict

`source-traces/seven-engine/` preserves the original six final rows plus the
superseded first `plan-reflection` attempt. `source-traces/plan-reflection-r2/`
preserves the final budget-fix rerun. `matrix.json` is the authoritative
machine-readable aggregation and selects r2 for the final `plan-reflection`
row.
