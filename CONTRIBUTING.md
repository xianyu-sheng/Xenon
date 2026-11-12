# 贡献指南

感谢你关注 Xenon。Xenon 是一个面向终端和编程任务的开源 AI Agent 工作区，欢迎提交问题、文档改进和代码贡献。

## 反馈渠道

- GitHub Issues：缺陷、功能设计与跨平台讨论（中文 / English 均可）。
- GitHub Discussions：功能建议、体验反馈与路线图见置顶的[路线图与反馈收集](https://github.com/xianyu-sheng/Xenon/discussions/143)。
- 想认领任务？置顶的[贡献者入口 #144](https://github.com/xianyu-sheng/Xenon/issues/144) 列了新人友好任务与认领流程。
- GitHub Pull Requests：正式代码合并入口。
- 安全问题请勿开公开 issue，按 [SECURITY.md](SECURITY.md) 的流程私下报告。

## 提交 Issue

请尽量提供：

- Xenon 版本（例如 `xenon --version` 的输出）。
- Python 版本和操作系统。
- 使用的模型或 Provider（不要粘贴 API Key）。
- 最小复现步骤、实际结果和期望结果。
- 相关命令、错误信息和经过脱敏的日志。

## 提交 Pull Request

1. 从 GitHub 的 `main` 分支创建分支；**先看 issue 是否已被 assign**——已被认领的任务请勿重复开工。
2. 保持改动聚焦，并为行为变化补充测试或文档。
3. 本地验证（与 CI 的 lint / 测试范围一致，见 `.github/workflows/ci.yml`）：

   ```bash
   ruff check xenon tests evals
   pytest -q -m "not live"
   ```

   详细的本地测试命令、跨平台差异与构建管线见 [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)。

4. **PR 描述必须包含「CI 证据」段落**（评审的必读项，模板见 `.github/PULL_REQUEST_TEMPLATE.md`）：

   - 附上你**实际执行的完整命令**与**完整结果行**，例如：
     - 针对性测试：`python -m pytest tests/test_xxx.py -q` → `N passed`
     - 全量离线测试：`python -m pytest tests -m "not live" -q` → 输出尾行
     - 静态检查：`python -m ruff check xenon tests` → `All checks passed!`
   - **新增/修改的测试必须在没有你的改动时失败**（证明它真的锁住了行为），并在 PR 里说明验证方式；
   - 与 `main` 分支同命令的结果对比：**不得出现新增失败**。Windows 本地的 symlink / PTY 等已知基线失败可以不计，但请在 PR 中注明；
   - 附上 CI 运行链接。fork 首次贡献者的 workflow 需要维护者点一次批准——批准后请确保跑绿；
   - 只写「本地跑过没问题」而不附命令与输出的 PR，评审会要求补齐证据后再审。

5. 不要提交凭证、个人配置、构建产物或供应商真实响应。
6. 文档中声明功能所属版本时，只写**已发布**的版本号：源码中的 `__version__` 始终等于最近一次发布的版本，不代表 `main` 上刚合并的功能已经发布。未发布的功能请写「下一版本」，不要写成当前版本号——这是曾经让两位贡献者白做工的坑。

## 依赖升级策略 (Dependency Upgrade Policy)

Xenon 使用 Dependabot 每周自动检查依赖更新（涵盖 `pip` 运行时依赖、`scripts/brand` npm 依赖以及 GitHub Actions），并通过分组合并减少 PR 噪声。

### 合并前最小验证清单 (Pre-merge Checklist)
Dependabot PR 严禁绕过 CI。合并前必须由维护者完成以下最小验证：
1. **CI 必须全绿**：静态检查 (`ruff check`)、离线全量测试 (`pytest -m "not live"`) 必须全部跑绿通过；
2. **不锁死精确版本**：保持运行时依赖下界原则（如 `package>=x.y.z`），不引入锁定精确版本的约束；
3. **核心依赖回归验证**：若更新涉及核心库（如 `httpx`、`tiktoken`、`rich`、`jedi` 等），需在审查时确认无废弃 API 或破坏性行为变化；
4. **运行时代码纯净性**：不为单纯通过 Dependabot PR 而侵入性改动核心运行时代码。

---

# Contributing (English)

Thank you for your interest in Xenon, an open-source AI agent workspace for
terminal and programming tasks. Issues, documentation improvements and code
contributions are all welcome. The section above is the authoritative (Chinese)
version; this is a concise English mirror of the essentials.

## Feedback channels

- **GitHub Issues** — bugs, feature design, cross-platform discussion (Chinese
  or English).
- **GitHub Discussions** — feature ideas, feedback and the roadmap live in the
  pinned [Roadmap & Feedback](https://github.com/xianyu-sheng/Xenon/discussions/143) thread.
- **Looking for a task?** The pinned [contributor entry point #144](https://github.com/xianyu-sheng/Xenon/issues/144)
  lists starter tasks and how to claim one.
- **GitHub Pull Requests** — the merge path for code.
- Security issues must **not** be filed publicly; follow the private reporting
  process in [SECURITY.md](SECURITY.md).

## Filing issues

Please include: the Xenon version (`xenon --version`), Python version and OS,
the model/provider in use (never paste API keys), minimal reproduction steps,
expected vs. actual results, and sanitized logs or error messages.

## Submitting a pull request

1. Branch from `main` and **check the issue is not already assigned** — do not
   start work on a claimed task.
2. Keep the change focused; add tests or docs for behavior changes.
3. Verify locally (same scope as CI, see `.github/workflows/ci.yml`):

   ```bash
   ruff check xenon tests evals
   pytest -q -m "not live"
   ```

   For detailed local test recipes, cross-platform notes, and brand asset pipelines, see [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

4. **The PR description must include a "CI evidence" section** (required for
   review; template in `.github/PULL_REQUEST_TEMPLATE.md`):
   - The exact commands you ran and their full result lines (targeted tests,
     the offline suite, and ruff).
   - New or modified tests **must fail without your change** — state how you
     verified that.
   - A comparison against `main` with the same commands: **no new failures**.
     Known baseline failures on your platform (e.g. symlink/PTY tests on
     Windows) may be excluded if noted in the PR.
   - A link to the CI run. First-time fork contributors need a maintainer to
     approve the workflow run once — make sure it is green afterwards.
   - PRs that only say "works locally" without commands and output will be
     asked for evidence before review.
5. Never commit credentials, personal configuration, build artifacts, or real
  vendor responses.
6. When docs claim a feature belongs to a version, only cite **released**
   versions: `__version__` always equals the latest release and does not mean
   features merged into `main` have shipped. Write "next release" for
   unreleased features instead of a concrete version number.

## Dependency Upgrade Policy

Xenon uses Dependabot to automatically check for dependency updates weekly across `pip` runtime dependencies, `scripts/brand` npm dependencies, and GitHub Actions workflows, grouping minor and patch updates to minimize PR noise.

### Pre-merge Checklist
Dependabot PRs must never bypass CI. Maintainers must verify the following before merging:
1. **All CI checks must pass**: Static analysis (`ruff check`) and offline tests (`pytest -m "not live"`) must be green.
2. **Preserve lower-bound policy**: Do not lock exact pinned versions; preserve lower-bound version specifications (e.g. `package>=x.y.z`).
3. **Core dependency regression review**: For critical dependencies (such as `httpx`, `tiktoken`, `rich`, `jedi`), verify there are no breaking changes or deprecated API usages.
4. **Runtime code purity**: Do not alter runtime behavior or hack core logic solely to satisfy an automated bump.

