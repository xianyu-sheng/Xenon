# Xenon — Quick Start Guide

A local-first AI coding assistant with 8 reasoning paradigms, 20+ built-in tools,
and DeepSeek cache cost optimization.

## Installation

```bash
# Current v0.6.3 source (includes the new TUI):
pip install -U "git+https://github.com/xianyu-sheng/Xenon.git"
```

The `v0.6.3` tag and release package are still on the release checklist. Until
they are published, installing from Git avoids accidentally using an older TUI.

Requirements: Python 3.10+, Linux / macOS / Windows (PowerShell).

## Setup (30 seconds)

```bash
xenon
```

On first launch, follow the `/setup` wizard to configure your DeepSeek API key.
You can also set it via environment variable:

```bash
export DEEPSEEK_API_KEY=sk-your-key-here
xenon
```

The tool automatically discovers available models from your API endpoint.

## Basic Usage

```
❯ /mode react          Switch to ReAct reasoning mode (or Shift+Tab to cycle)
❯ Create a Python CLI tool that converts Markdown to HTML

❯ /config              View current model configuration
❯ /cost                Show cumulative token usage and estimated cost
❯ /help                List all available commands
```

## Terminal UI

Xenon v0.6.3 keeps the existing engine architecture and replaces the high-frequency
conversation layout:

```text
───────────────────────────────────────────────
  ❯ Type a message
───────────────────────────────────────────────

  ● deepseek  ·  deepseek/deepseek-v4-pro  ·  react  ·  context 5.0%  ·  cache 89%  ·  ¥0.03  ·  Ctrl+O details  ·  Shift+Tab mode
```

- The two input rules span the terminal width. The lower rule belongs to the
  input area; API/model state is a separate toolbar fixed to the screen bottom.
- Assistant answers and optimized prompts no longer use large panels. Answers
  keep normal brightness; metadata, HTTP logs, and helper text are dimmed.
- Tool exploration is collapsed to one summary line by default. `Ctrl+O`
  expands or collapses the previous execution trace.
- Narrow terminals retain high-priority API/context fields and hide lower-priority
  status items instead of wrapping the toolbar.

See [TUI.md](TUI.md) for the complete layout contract, shortcuts, and fallback behavior.

### 8 Reasoning Paradigms

| Mode | What it does | Best for |
|------|-------------|----------|
| `direct` | One-shot Q&A (no tools) | Quick questions |
| `react` | Think → Act → Observe loop | File ops, coding, debugging |
| `plan-execute` | Plan first, then execute in DAG order | Multi-step refactors |
| `reflection` | Execute then self-review | Quality-critical tasks |
| `novel` | Long-form writing with chapter planning | Documentation, reports |
| `plan-react` | Plan + ReAct per step | Complex multi-file tasks |
| `plan-reflection` | Plan + self-review per step | Production-grade refactors |
| `react-reflection` | ReAct + final review | Code with quality gates |

### 20+ Built-in Tools

`list_files` / `read_file` / `write_file` / `edit_file` / `command` / `git` /
`web_fetch` / `github_fetch` / `clone_repo` / `search_files` / `code_index` /
`ast_analyze` / `refactor` / `lsp_goto_def` / `lsp_find_refs` / `weather` / `datetime` /
`spawn_agent` / `batch_write` / `batch_edit` / `diff_preview`

Plus MCP integration — browse and install community tool servers:

```
❯ /mcp browse
❯ /mcp install @smithery-ai/github
```

## Vision Bridge (multimodal)

Paste an image (Ctrl+Alt+V) and Xenon uses a lightweight vision model to describe
it, then passes the description to your main reasoning model:

```
❯ /vision on
❯ [Ctrl+Alt+V to paste screenshot] → "The error is on line 42..."
```

## DeepSeek Configuration

### Get an API Key

1. Visit [platform.deepseek.com](https://platform.deepseek.com)
2. Register and create an API key
3. Set it via `/setup` in Xenon or `export DEEPSEEK_API_KEY=sk-...`

### Supported DeepSeek Models

| Model | Best for | Cache support |
|-------|----------|--------------|
| `deepseek-v4-pro` | Coding, complex agents, advanced reasoning | Yes |
| `deepseek-v4-flash` | Fast, high-concurrency and cost-efficient tasks | Yes |

Both V4 models support thinking and non-thinking modes, tool calls, a 1M-token
context window, and up to 384K output. The legacy `deepseek-chat` and
`deepseek-reasoner` aliases are not presented by Xenon's offline fallback because
DeepSeek will retire them on 2026-07-24 23:59 Beijing time. When online, Xenon
still discovers the model list from your endpoint rather than assuming it.

Xenon registers `deepseek-v4-pro` with `reasoning_effort: max` by default. The
value is persisted per model and is passed through ordinary, streaming, and
native tool-calling requests. Override it when you need a different latency /
reasoning trade-off:

```text
❯ /set_model ds-pro deepseek/deepseek-v4-pro reasoning_effort=high
```

Accepted values are `low`, `medium`, `high`, and `max`. A forced tool choice
(`required`, `none`, or a named function) disables thinking for that individual
request because DeepSeek does not allow forced tool choice and thinking mode in
the same request.

> **Cache savings**: Xenon's CacheTracker automatically shows how much
> you save via DeepSeek's context caching. Hit `/cost` to see real-time numbers.

### Using with Other Providers

Xenon auto-detects API format from the model prefix:

```bash
# OpenAI-compatible endpoints
export OPENAI_API_KEY=sk-...
xenon --model openai/gpt-4o

# Anthropic Claude
export ANTHROPIC_API_KEY=sk-ant-...
xenon --model anthropic/claude-3-5-sonnet-20241022

# Volcengine ARK (China)
export ARK_API_KEY=your-ark-key
xenon --model ark/deepseek-v4-pro
```

Or add models permanently in `/setup`:
```
❯ /setup
  → Select provider → Enter API key → Done
```

## Architecture

The v0.6.3 work hardens the existing components and refreshes the TUI; it does
not replace Xenon's top-level architecture. The three pillars remain:

1. **Cache-aware cost loop** — DeepSeek cache usage, estimated CNY cost, and
   prompt-prefix alignment are observable without an extra model call.
2. **8-engine auto-router** — From simple Q&A to plan-execute-reflect chains,
   each engine is a standalone module with pluggable callbacks.
3. **7-stage tool pipeline** — Parameter normalization, hallucination checks,
   permissions, circuit breaking, execution, and structured results.

Provider registry, ModelPool failover, and ContextManager remain supporting
components inside those boundaries.

Read the full breakdown in [ARCHITECTURE.md](ARCHITECTURE.md).

## Tips

- **Shift+Tab** to cycle reasoning modes
- **Ctrl+O** to expand/collapse the previous execution details
- **Ctrl+C** once to interrupt current engine, twice to exit
- **`XENON_NO_PT=1 xenon`** to run without prompt-toolkit (plain readline mode)
- **`/resume`** to list and restore previous sessions (auto-saved on exit)

## Troubleshooting

```bash
# Check connectivity
❯ /pool                  Show model pool health

# Reset everything
❯ /config reset
❯ /setup                Re-configure from scratch

# Verbose logging
❯ /thinking on          Show full reasoning traces
```

## Contributing

```bash
git clone https://github.com/xianyu-sheng/Xenon.git
cd Xenon
pip install -e ".[dev]"
pytest tests xenon/tests -m "not live and not e2e" -q
```

PRs welcome! Please include tests for new features.
