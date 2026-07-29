# Xenon SWE-bench Lite engine matrix — DeepSeek official V4 Pro

This is an eight-engine matrix over one unchanged official SWE-bench Lite
instance. It is a pipeline calibration result, not Xenon's overall SWE-bench
score or an estimate of production correctness.

## Evaluation contract

- Dataset: `SWE-bench/SWE-bench_Lite`, `test` split
- Instance: `astropy__astropy-12907`
- Model: `deepseek/deepseek-v4-pro`
- Provider: DeepSeek official API (`api.deepseek.com`), not Ark/Coding Plan
- Evaluator: unmodified `swebench==4.1.0` harness
- Official image: `swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest`
- Image ID: `sha256:f3f63bb87d581c0e7b47f900dd82165b71040e1758d3c29e915e2b18da9baf63`
- Engine budget: 10 steps/rounds, 120 seconds per request, 900 seconds total
- Each engine has an independent prediction, trace, event stream, run ID, and
  official harness report.
- No official task, repository fixture, tests, image, or grader was modified.

## Separated results

- Verified Success Rate (engine-instance cells): **37.50% (3/8)**
- Official Result Assertion Pass Rate: **37.50% (3/8)**
- Tool Execution Success Rate: **93.85% (61/65)**
- Cache Rails Hit Rate (provider token-weighted): **82.40%**
- Reusable Tokens (provider cache hits): **855,296**
- Prompt Tokens: **1,037,954**
- Completion Tokens: **111,129**
- Provider Calls: **89**
- Estimated Tokens Saved: **N/A** — cache-hit tokens are evidence of reuse,
  not proof of an equal reduction in billed tokens.
- Estimated Cost Saved: **N/A** — no unsupported conversion from cache hits to
  currency is made.
- Context Compression Benefit: **N/A** — compression did not trigger in these
  traces.

Correctness, tool reliability, and cache reuse are intentionally reported as
separate metrics. The 82.40% cache hit rate does not raise the 37.50% verified
success rate.

## Per-engine matrix

| Engine | Official status | Patch | Tools | Calls | Prompt | Completion | Cache hits | Cache hit rate | Elapsed |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| direct | empty_patch | 0 B | — | 1 | 522 | 915 | 0 | 0.00% | 19.5 s |
| react | **resolved** | 504 B | 8/10 | 11 | 182,558 | 4,284 | 161,152 | 88.27% | 71.8 s |
| plan-execute | empty_patch | 0 B | — | 1 | 2,562 | 793 | 0 | 0.00% | 18.6 s |
| reflection | empty_patch | 0 B | — | 10 | 86,041 | 49,579 | 13,952 | 16.22% | 827.5 s |
| plan-react | **resolved** | 504 B | 40/41 | 49 | 595,979 | 31,222 | 546,816 | 91.75% | 462.3 s |
| plan-reflection | empty_patch | 0 B | 2/2 | 4 | 5,719 | 6,571 | 2,560 | 44.76% | 127.5 s |
| react-reflection | **resolved** | 504 B | 11/12 | 13 | 164,573 | 17,765 | 130,816 | 79.49% | 302.7 s |
| novel | empty_patch | 0 B | — | 0 | 0 | 0 | 0 | N/A | 9.1 s |

The three resolved engines independently ran through the official harness.
The other five predictions were independently classified by that harness as
`empty_patch`; they were not counted as unresolved test executions.

## Root-cause observations

1. **ReAct is the strongest baseline on this instance.** It produced the
   resolved 504-byte patch in 71.8 seconds and 186,842 total tokens.
2. **Plan-ReAct repeats completed work.** Its five planned steps each start a
   fresh ReAct run without prior conversation injection. It produced the same
   patch as ReAct but used 49 calls and 627,201 total tokens (3.36x ReAct).
3. **Reflection self-scores are not correctness evidence.** Reflection scored
   itself 8 but emitted a corrupt patch. Plan-Reflection scored itself 10 but
   emitted a patch that did not apply. Both became official `empty_patch`.
4. **Plan-Execute has a protocol mismatch.** The model returned a
   `<tool_req>` during planning, leaving no executable plan and no patch.
5. **Novel did not enter a provider/tool path.** It made zero provider calls
   and produced no patch for this task contract.
6. **Tool failures remained observable.** Failed command validation, an
   incorrect host/worktree path, and subsequent recovery are recorded as
   failures rather than successful tool calls.

## Artifact layout

Each `attempts/<engine>/` directory contains:

- `predictions.jsonl` — the engine-specific SWE-bench prediction
- `traces.json` — final output, patch provenance, tool lifecycle, usage, cache
  and timing metrics
- `traces.events.jsonl` — durable lifecycle events and heartbeats
- `official-harness/report.json` — the unmodified official harness verdict

Machine-readable aggregate values are in `matrix.json`.
