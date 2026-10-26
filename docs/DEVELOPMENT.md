# Development Guide (开发者指南)

This document provides a comprehensive guide for developers contributing to Xenon, including environment setup, running tests across various scopes, platform nuances, the brand asset pipeline, and the evaluation harness.

---

## 1. Environment Setup (环境准备)

Xenon requires Python 3.10 or newer; CI runs the test matrix on 3.10, 3.11 and 3.12 (3.12 is the primary development version).

### Python Development Environment
```bash
# Clone and enter the repository
git clone https://github.com/xianyu-sheng/Xenon.git
cd Xenon

# Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install Xenon in editable mode with development dependencies
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

### Static Checks
Verify that the installed code compiles and passes linter checks:
```bash
python -m compileall -q xenon tests
ruff check xenon tests evals
```

---

## 2. Test Execution Recipes (测试执行指南)

Xenon's test suite uses pytest markers to differentiate between fast unit tests, end-to-end flows, and network-dependent live tests.

### A. Full Offline Test Suite (推荐日常运行)
Runs all unit and integration tests that do not require external network connections or live LLM credentials:
```bash
python -m pytest tests \
  -m "not live and not e2e" \
  -q --tb=short \
  --cov=xenon \
  --cov-fail-under=70 \
  --timeout=120
```

### A2. Kernel Contract Checks (单循环内核)
The kernel (`xenon/kernel/`) is a semantic layer: it must not import the legacy
engine, branch on concrete tool names, or perform direct side effects.  These
checks are fast and should run before touching kernel code:
```bash
python scripts/check_kernel_purity.py
python -m pytest tests/kernel -q
python -m pytest -m kernel_contract --collect-only -q
```
`kernel_contract` tests are the north-star contract: arbitrary tool names, a
lying model that must not deliver, and deterministic replay.  They xfail until
the runtime lands in #196.

### B. Deterministic CLI End-to-End Suite
Runs deterministic subprocess CLI integration tests:
```bash
python -m pytest tests/e2e -m e2e -q --tb=short --timeout=60
```

### C. Targeted Single Test or Module
To test a single module or test case during development:
```bash
# Run a specific test file
python -m pytest tests/test_public_api.py -v

# Run a specific test function by name
python -m pytest tests/test_b1_command_quoted.py -k "test_quoted" -v
```

### D. Live Tests (`-m live`)
Live tests communicate with actual LLM providers and external network services.
> **Note:** Live tests are excluded from default CI runs. They require valid API keys (e.g., `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`). Avoid running concurrent live tests to prevent rate limit throttling.
```bash
# Run only if valid credentials are set in environment
export OPENAI_API_KEY="your-key"
python -m pytest tests -m "live" --timeout=180
```

---

## 3. Platform Differences & Known Baselines (跨平台差异)

CI validates on Ubuntu, Windows, and macOS. When developing locally, keep in mind these platform-specific considerations:

- **Windows:**
  - **Symbolic Links:** Symlink tests require Windows Developer Mode enabled or elevated administrative permissions (`SeCreateSymbolicLinkPrivilege`).
  - **Terminal / PTY:** Pseudo-terminal emulation on Windows uses conpty/winpty instead of Unix `pty`. Tests exercising raw Unix PTY flags are skipped or adjusted on Windows.
  - **Character Encoding:** Windows consoles may default to legacy code pages (e.g., CP936/GBK, CP1252). Xenon explicitly handles UTF-8 I/O normalization, but localized console output should be checked for encoding drift.
- **macOS:**
  - Standard BSD utility flags may differ slightly from GNU coreutils on Linux.
  - System temp directories reside under `/var/folders/...` rather than `/tmp`.
- **Linux:**
  - Reference environment matching GitHub Actions `ubuntu-latest`.

> Tracking issue for platform hardening (known Windows baseline failures, wider smoke subsets): [#119](https://github.com/xianyu-sheng/Xenon/issues/119).

---

## 4. Brand Asset Pipeline (品牌资产生成管线)

The visual assets (animated GIFs, SVGs, and PNG logos) are deterministically rendered by a TypeScript canvas script located in `scripts/brand`.

### Building / Re-rendering Brand Assets
When modifying the logo geometry, startup animation frame data, or color palette:
```bash
cd scripts/brand
npm install
npm run render
```

### Output Artifacts
The render pipeline writes:
- `xenon/utils/_brand_frames.py` — terminal animation frame table (played by `xenon/utils/logo.py`).
- `docs/logo.svg` — Scalable vector logo.
- `docs/logo.png` — High-resolution static image.
- `docs/logo-animated.gif` — Animated runner sequence (embedded in the README).
- `docs/hero-terminal.svg` / `docs/hero-terminal.en.svg` — README hero terminals (zh/en).

`docs/demo.gif` is a hand-recorded terminal demo and is **not** produced by this pipeline.

> **Rule:** Only re-render and commit brand assets when changing the animation storyboard or source branding logic in `scripts/brand/src/`.

---

## 5. Evaluation Harness (`evals/`)

The `evals/` directory contains benchmarking and task evaluation suites for evaluating Xenon's agent reasoning and tool usage.

- **Offline mock evaluations** — deterministic fixtures, no API cost:

  ```bash
  python evals/runner.py --mode mock
  ```

  The report is written to `evals/reports/mock_report.md`. Real-model runs (`--mode real`) need provider credentials and are not part of CI.
- **Benchmark Catalogs:**
  See `docs/XENON_BENCHMARK_CATALOG.md` and `docs/XENON_EVAL_PROTOCOL.md` for methodology and protocol definitions.

---

## 6. Contribution Workflow & Good First Issues (任务认领与提交)

1. **Find an Unassigned Issue:**
   Always check that an issue has no assignee before starting. Good starting points include:
   - **#127:** `quality: expand Ruff ruleset — phase 1 (isort)` (good first issue)
   - **#120:** `quality: introduce incremental static type checking (mypy)`
   - **#126:** `evals: deterministic offline replay subset in CI`
2. **Comment to Claim:**
   Leave a comment on the issue stating you are working on it so duplicate efforts are avoided.
3. **Mandatory CI Evidence:**
   In your Pull Request description, include the command output demonstrating that linting and offline tests pass, following the template in `.github/PULL_REQUEST_TEMPLATE.md`.
