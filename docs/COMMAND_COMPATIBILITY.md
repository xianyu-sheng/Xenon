# Command compatibility (#176)

`command` stays a shell tool, but Xenon recognizes a few narrow, shell-free
shapes.  Everything else either runs unchanged or fails closed with a safe
alternative; Xenon never silently rewrites a conditional.

| Real failing shape | Supported form |
|---|---|
| `Set-Location repo; pytest tests -q` | shell-free pytest plan; `Set-Location`/`sl`/`cd /d` are handled |
| `$env:NO_BYTECODE='1'; cd repo; pytest ... 2>&1 \| Select-Object` | shell-free pytest plan preserves env and strips the pipeline |
| `git add app.py && commit -m "feat"` | shell-free git plan; bare `commit` after `git add` is treated as a git subcommand |
| `pytest "tests/test_a.py::case" -k "one\|two"` | quote-aware args; quoted pipe is not split |
| `if (Test-Path app.py) { pytest -q }` | **not rewritten**; returns an actionable error pointing to `run_script(language="powershell", code=...)` |
| `foreach ($f in ...) { ... }` / `while (...)` | **not rewritten**; same `run_script` alternative, no partial execution |
| `Set-Location repo; npm test` | not a supported narrow plan; use `cwd` on the tool or `run_script` |

Security boundaries remain unchanged: `python -c`, `$()`/backticks, unquoted
variables and process substitution are still rejected.  The conditional check
is quote-aware, so `echo "if (x) { y }"` is data, not control flow.

Regression coverage: `tests/test_command_compatibility.py`,
`tests/test_test_command.py`, `tests/test_git_command_normalization.py`.
