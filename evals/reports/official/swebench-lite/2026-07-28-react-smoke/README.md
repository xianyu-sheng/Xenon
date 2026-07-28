# Official SWE-bench Lite smoke

This is an official evaluator run, not Xenon's internal fixture score.

- Dataset: `SWE-bench/SWE-bench_Lite`, official `test` split
- Instance: `astropy__astropy-12907`
- Engine: Xenon `react`
- Model: `custom/deepseek-v4-flash-260425`
- Grader: `swebench==4.1.0`, `swebench.harness.run_evaluation`
- Docker: official harness-generated environment and instance images
- Result: `resolved = true` (1/1)

The gold run in `gold-environment-smoke.json` only validates that the official
dataset, Docker image construction, tests, and grader are healthy. It is not a
Xenon score. Xenon's prediction is in `predictions.jsonl`; the official report
is `official-report.json`.

## Execution-chain observations

Xenon recorded 15 tool attempts: 12 succeeded and 3 failed. The failures were
one hallucinated command parameter caught by validation, one wrong `/workspace`
path, and one missing local `erfa` dependency in the uninstalled source tree.
The agent then used `edit_file` to produce the patch. The official Docker
environment, rather than the host source tree, ran the test patch and grader.

Cache/cost metrics are intentionally separate from correctness. This smoke
artifact predates the adapter's usage-tracker fields, so no cache saving claim
is made for it.
