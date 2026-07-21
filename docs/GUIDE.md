# Xenon — Quick Start Guide

A local-first AI coding assistant with 8 reasoning paradigms, 20+ built-in tools,
and DeepSeek cache cost optimization.

## Installation

```bash
pip install xenon
# or bleeding edge:
pip install git+https://github.com/xianyu-sheng/Xenon.git
```

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

Plus MCP integration — browse and install from 7000+ community tool servers:

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
| `deepseek-chat` (V3) | General tasks | Yes |
| `deepseek-reasoner` (R1) | Complex reasoning | Yes |
| `deepseek-v4-pro` | Long context, advanced reasoning | Yes |

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

Xenon is built on three pillars:

1. **Multi-model abstraction** — Provider registry + model pool with automatic
   failover. One API key outage won't kill your session.
2. **8-engine reasoning** — From simple Q&A to plan-execute-reflect chains,
   each engine is a standalone module with pluggable callbacks.
3. **Reliability pipeline** — Circuit breaker, budget manager, hollow answer
   detector, context compressor. Every tool call passes 7 validation stages.

Read the full breakdown in [ARCHITECTURE.md](ARCHITECTURE.md).

## Tips

- **Shift+Tab** to cycle reasoning modes
- **Ctrl+O** to expand/collapse the thinking panel
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
pytest tests/ -q
```

PRs welcome! Please include tests for new features.
