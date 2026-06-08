# OmniAgent Real Eval Sample Report

This is the checked-in shape of a manual real-model report. Real Eval is intentionally not run in CI because it requires an API key, network access, and may incur model cost. Generate fresh numbers locally before using this as portfolio evidence:

```bash
python evals/runner.py --mode real --model deepseek/deepseek-v4-pro --output evals/reports/real_report.md
```

- Mode: `real`
- Model: `deepseek/deepseek-v4-pro`
- Run date: `manual run required`
- Tasks: 20
- Success Rate: `replace after local run`
- Average Tokens: `replace after local run`
- Tool Calls: `replace after local run`
- Tool Failures: `replace after local run`

| Task | Category | Success | Tokens | Tool Calls | Tool Failures | Notes |
| --- | --- | --- | ---: | ---: | ---: | --- |
| `edit-python-function` | file_edit | pending | 0 | 0 | 0 | Run real eval locally with API key. |
| `code-search-entrypoint` | code_search | pending | 0 | 0 | 0 | Run real eval locally with API key. |
| `run-focused-tests` | tool_call | pending | 0 | 0 | 0 | Run real eval locally with API key. |

## Failure Summary

Populate this section with failed task IDs and short causes after a real run.
