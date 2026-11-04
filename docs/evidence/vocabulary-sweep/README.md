# 多引擎措辞清理（#294）

用户提问：「现在还有 react 吗？我们不是单循环吗？把表述、命令上的残留都扫一遍。」

引擎早删了，**说引擎的话还在**。本轮按证据清完，并留下一个守卫防止它回来。

## 清点（改动前）

| 范围 | 「引擎」 | 「范式」 |
|---|---|---|
| `xenon/`（不含 `engine/` 工具层） | 面向用户字符串 34 处 | 19 处 |
| `xenon/engine/`（工具执行层 docstring） | 20 处 | — |
| `docs/` + README | 9 个文件命中 | — |

## 术语定案

| 旧词 | 新词 | 理由 |
|---|---|---|
| 思考范式 / 推理范式 / 范式 | **模式**（mode） | 用户选的是预设，不是推理体系 |
| 引擎类型 / 默认 ReAct 引擎 | **阶段配置**（profile） | 只有一个循环，模式只决定跑哪些阶段 |
| 思考引擎发生切换 | 运行模式发生切换 | 缓存归因标签 |
| 引擎终止原因 | 回合终止原因 | 语义正确 |

## 改了什么

- **CLI**：`--mode` / `--auto-route` 帮助、`--mode` 列表列头。
- **命令**：`/mode`（描述、选择器标题、输出、错误）、`/setup`、`/sub-agent`、
  `/runtime`、`/sessions`、`/help` 提示行、`/permissions` 相关表头。
- **面板**：状态栏 `范式:` → `模式:`；setup 向导、会话表、成本表。
- **路由**：智能路由的提示与日志（`推荐模式`、`保持当前模式`、`模式切换失败`）。
- **内部 docstring**：`xenon/engine/*` 的 20 处「引擎」→「循环/工具执行层」。
- **README / README.en**：对比表「引擎」→「循环」；`/mode` 说明改「模式」。
- **ARCHITECTURE.md**：修掉 4 处指向**已删除文件**的引用
  （`engine/registry.py`、`builtin_engines.py`、`react_engine.py`），
  总览图里的「7 范式执行器」改成「LoopRuntime + 阶段配置」。
- **COMPARISON.md**：「多范式引擎（核心差异化）」→「一个循环 + 多阶段配置」，
  7 → 6，并写明 `react` 只是 `direct` 的别名。
- **design/mechanism-fit.md**：标记为历史归档（其「尚未回迁」判断已过时）。

## 顺带修掉的真 bug

`/sub-agent` 的帮助列了 **7 种「引擎类型」**，其中 3 个**从来没被接受**
（`direct`、`plan-reflection`、`react-reflection`）——清单会撒谎。
现在帮助文本从 `CHILD_PROFILES` **生成**（唯一来源），列出的是真正可用的 5 个；
`--profile` 为正式参数，`--engine` 保留兼容。

## 守卫（防止回归）

`scripts/check_engine_vocabulary.py`：扫描发布包里的**字符串字面量**，
出现「范式 / 多引擎 / 思考引擎 / 推理引擎 / 引擎类型」即失败；
允许清单只放两类：真实产品名（Brave 搜索引擎）与明确解释退役的句子。

`tests/test_engine_vocabulary.py` 把它接进 CI，并自带一条「守卫必须真的会报错」
的负向测试（否则它只是装饰）。

## 真实 TTY

| 检查 | 结果 |
|---|---|
| `/mode` 输出用「模式」 | ✅ |
| `/mode` 输出仍出现「范式」 | ❌ 已无 |
| `/sub-agent` 输出用「profile」 | ✅ |
| `/sub-agent` 仍说「引擎类型」 | ❌ 已无 |

## 有意保留

- `xenon/engine/` **目录名**（工具执行层，75 处引用）：改名是独立重构，
  目前由 `check_engine_imports.py` 守卫其骨架边界，docstring 已改口径。
- `xenon/repl/paradigm_selector.py` 的**模块名**：语义已改为「模式选择器」，
  改名留给后续重构（内部 API，无用户可见影响）。
- 已归档历史文档中的「多引擎」（记录的是事实史）。
