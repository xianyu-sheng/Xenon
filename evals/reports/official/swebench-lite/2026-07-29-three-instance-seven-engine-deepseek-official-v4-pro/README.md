# Xenon official SWE-bench Lite calibration — three instances, seven engines

This is an official-harness calibration covering all seven remaining Xenon
engines after `novel` was removed. It is not a claim about Xenon's overall
SWE-bench score: three instances are too small for that conclusion.

## Evaluation contract

- Dataset: `SWE-bench/SWE-bench_Lite`, official `test` split (300-instance dataset)
- Instances: `astropy__astropy-14182`, `django__django-10914`, `pytest-dev__pytest-11143`
- Model/provider: `deepseek/deepseek-v4-pro` through the user's DeepSeek API at `api.deepseek.com`
- Evaluator: unmodified official `swebench==4.1.0` Docker harness
- Official images and tests were reused unchanged; no task, fixture, reference patch, grader or test was modified.
- Each engine-instance cell had an independent worktree and prediction file.

## Separated aggregate results

- Verified Success Rate: **52.38% (11/21)**
- Official Result Assertion Pass Rate: **52.38% (11/21)**
- Tool Execution Success Rate: **94.18% (178/189)**
- Empty Patch Cells: **10/21**
- Unresolved Cells: **4/21**
- Error Cells: **0/21**
- Provider Calls: **216**
- Prompt Tokens: **2,603,586**
- Completion Tokens: **114,548**
- Total Tokens: **2,718,134**
- Cache Rails Hit Rate: **89.67%** (2,334,720 cache-hit tokens / 2,603,586 prompt tokens)
- Reusable Tokens: **2,334,720**
- Estimated Tokens Saved: **N/A** — cache reuse is observed, but an equal billing reduction is not proven.
- Estimated Cost Saved: **N/A** — no unsupported currency conversion is reported.
- Context Compression Benefit: **N/A** — compression was not triggered in these 21 traces.
- Generation elapsed time: **2,087.874 seconds**

Correctness, tool execution and cache reuse are separate measurements. Cache
Rails reuse does not increase the verified success rate.

## Per-engine matrix

| Engine | Official resolved | Unresolved | Empty patch | Workspace patches | Tool success | Calls | Cache hit rate |
|---|---:|---:|---:|---:|---:|---:|---:|
| direct | 0/3 | 0 | 3 | 0 | 0/0 | 3 | 0.00% |
| react | 2/3 | 1 | 0 | 3 | 31/33 | 32 | 91.23% |
| plan-execute | 0/3 | 0 | 3 | 0 | 1/1 | 4 | 34.52% |
| reflection | 0/3 | 0 | 3 | 0 | 0/0 | 6 | 47.21% |
| plan-react | 1/3 | 1 | 1 | 2 | 74/79 | 81 | 92.20% |
| plan-reflection | 2/3 | 1 | 0 | 3 | 32/34 | 42 | 89.16% |
| react-reflection | 2/3 | 1 | 0 | 3 | 40/42 | 48 | 89.55% |

The official harness, not model text or patch presence, decides the resolved
column. A read-only tool success is not task success.

## Empty and failure-chain findings

1. `direct` produced provider-specific tool markup/text but has no execution
   loop, so no workspace patch could be produced. This is an expected
   capability boundary, not an API empty response.
2. `plan-execute` and `reflection` frequently inspected or reasoned about the
   issue but left no state-changing edit. The fail-closed patch extraction
   correctly classified all six cells as `empty_patch`.
3. `plan-react` on `pytest-dev__pytest-11143` made one successful provider call,
   produced no tool calls, and returned `未能生成有效的执行计划。` The failure
   is at plan-output parsing/contract adherence, before the tool chain starts;
   it is not the earlier path-boundary `SecurityError` observed in another
   pytest trace. This is a general planner robustness finding, not an
   instance-specific override.
4. The four unresolved non-empty patches are genuine official test failures;
   they are not counted as successes merely because a workspace changed.

## Artifact layout

- `matrix.json` — authoritative machine-readable aggregation
- `traces.json`, `traces.checkpoint.json`, `traces.events.jsonl` — raw Xenon execution evidence
- `predictions.<engine>.jsonl` — exact per-engine official predictions
- `official-harness/` — unmodified official harness reports, one per engine

This calibration should be expanded with more official instances before
publishing any overall Xenon or engine leaderboard claim. The next engineering
work item is a general planner-output recovery/format-contract fix, followed by
rerunning the affected engines and the same official harness.
