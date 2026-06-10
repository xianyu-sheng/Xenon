# OmniAgent: Local Multi-Model Agent Runtime / Coding Agent CLI

[![CI](https://github.com/xianyu-sheng/omniagent/actions/workflows/ci.yml/badge.svg)](https://github.com/xianyu-sheng/omniagent/actions/workflows/ci.yml)

OmniAgent is a local-first coding agent CLI for developers who want a transparent, configurable agent runtime: multi-model routing, ReAct / Plan-Execute / Reflection workflows, MCP tools, memory, and project context in one Python package.

![OmniAgent terminal demo](docs/assets/terminal-demo.svg)

## Why It Matters

| Capability | What it gives you |
| --- | --- |
| Multi-model runtime | Route work across DeepSeek, OpenAI, Claude, Gemini, Qwen, Ollama, and other providers. |
| Agent workflows | Switch between direct chat, ReAct tool use, Plan-Execute, Reflection, and combined modes. |
| Local tool execution | Read/write files, search code, run commands, inspect git, fetch web/GitHub content, and call MCP tools. |
| Project memory | Inject project structure, rules, conversation context, and cross-session memory into the agent loop. |

## Quick Start

```bash
git clone https://github.com/xianyu-sheng/omniagent.git
cd omniagent
pip install -e ".[dev]"
omniagent
```

Inside the CLI:

```text
You: /setup
You: /set_model
You: /mode react
You: !python -m pytest tests -q
You: /new_terminal
You: 帮我检查 tests 失败原因并给出修复方案
```

API keys are stored locally in `~/.omniagent/credentials.yaml`. You can also pass models directly:

```bash
omniagent chat -m deepseek/deepseek-v4-pro openai/gpt-4o
```

## Architecture

```mermaid
flowchart LR
    User["用户输入"] --> Planner["Planner / ReAct / Reflection"]
    Planner --> Router["Model Router"]
    Router --> Tools["Tool Registry / MCP"]
    Tools --> Memory["Memory / Project Context"]
    Memory --> Executor["Executor"]
    Executor --> Reviewer["Reviewer"]
    Reviewer --> User
```

- Planner / ReAct / Reflection chooses how the agent reasons: direct answer, tool loop, plan execution, or review.
- Model Router resolves provider/model priority and fallback behavior.
- Tool Registry / MCP exposes local tools plus external MCP servers.
- Memory / Project Context injects conversation history, rules, file tree, and saved memories.
- Executor and Reviewer turn plans into actions, validate outputs, and surface results back to the CLI.

## Common Commands

| Command | Purpose |
| --- | --- |
| `/setup` | Configure provider API keys, default models, and modes. |
| `/set_model` | Register or interactively select a model. Configured providers load model options from their live API; built-in examples are used only when refresh is disabled. |
| `/mode` | Switch between direct, react, plan-execute, reflection, and combined modes. |
| `/project` | Inspect detected project type, file tree, and project rules. |
| `/edit <file> <instruction>` | Ask the LLM to edit a file and review the diff before applying. |
| `/mcp` | Add/list/remove MCP servers and inspect external tools. |
| `/memory` | Manage cross-session memory. |
| `/compact` | Summarize long context to control token usage. |
| `!<command>` | Run a shell command directly from the OmniAgent input line with the existing safety checks. |
| `/shell <command>` | Slash-command form of direct shell execution. |
| `/new_terminal [cwd]` | Open an observable child terminal. On Windows Terminal it uses a split pane; otherwise it opens a new shell window. |
| `/terminal_status [lines]` | Read the latest child-terminal transcript output. |
| `/terminal_quote [lines]` | Quote recent child-terminal output into the current OmniAgent context for follow-up questions. |
| `/open <file[:line]>` | Open a local file in the configured editor, VS Code, or the OS default app. |

The REPL input uses `prompt_toolkit` when available, so Left/Right/Home/End can move inside the current command and edit text in the middle. `Shift+Enter` inserts a new line; `Enter` sends.

## Eval

OmniAgent includes a small 20-task agent eval suite in `evals/tasks.yaml`.

Mock Eval is deterministic and used by CI:

```bash
python evals/runner.py --mode mock --output evals/reports/mock_report.md
```

Real Eval uses your configured model and API key. It is manual by design and is not run in CI:

```bash
python evals/runner.py --mode real --model deepseek/deepseek-v4-pro --output evals/reports/real_report.md
python evals/runner.py --mode real --model openai/gpt-4o --output evals/reports/real_report.md
```

The report records task count, success rate, average token estimate, tool calls, tool failures, and failure summaries. See `evals/reports/sample_report.md` for the real-eval report shape, then replace it with a freshly generated local run before using it as portfolio evidence.

## Testing

```bash
python -m pytest tests -q
python evals/runner.py --mode mock --output evals/reports/mock_report.md
```

The test suite covers REPL commands, tools, memory, project context, callbacks, prompt optimization, code indexing, and the eval runner. The root-level `test_all_modules.py` is a manual integration script and can make real API/tool calls, so it is not part of default CI.

## Security

Supported today:

- API keys are stored locally in `~/.omniagent/credentials.yaml`.
- File edits can be reviewed as diffs before confirmation.
- Dangerous shell and git commands are blocked or require explicit confirmation paths.
- Sensitive paths and common credential filenames are guarded in tool operations.

Planned:

- Fine-grained workspace sandbox policy.
- Per-tool allowlist/denylist configuration.
- Richer audit logs for long agent runs.

## Project Rules

Create `.omniagent/rules.md` in your project root to guide the agent:

```markdown
# Project Rules
- Use Python 3.12.
- Prefer pytest for tests.
- Show diffs before editing tracked source files.
- Keep API keys and credentials out of the repository.
```

## License

MIT License

## Credits

- [Rich](https://github.com/Textualize/rich) for terminal UI
- [httpx](https://github.com/encode/httpx) for HTTP calls
- [PyYAML](https://github.com/yaml/pyyaml) for YAML parsing
