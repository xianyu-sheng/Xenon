"""
OmniAgent 端到端测试 — 真实引擎调用验证
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any

# ── UTF-8 编码（Windows 兼容）──
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 确保项目根目录在 sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ═══════════════════════════════════════════════════════════════
# 测试基础设施
# ═══════════════════════════════════════════════════════════════

PASS = 0
FAIL = 0
SKIP = 0


def check(name: str, condition: bool, detail: str = ""):
    """断言一个条件，记录通过/失败。"""
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} — {detail}" if detail else f"  [FAIL] {name}")


def skip(name: str, reason: str = ""):
    """跳过测试。"""
    global SKIP
    SKIP += 1
    print(f"  [SKIP] {name} — {reason}")


def get_model_ids() -> list[str]:
    """获取已配置的模型 ID 列表。"""
    from omniagent.repl.provider_registry import get_configured_providers, load_credentials

    creds = load_credentials()
    if not creds:
        return []

    providers = get_configured_providers()
    model_ids = []
    for p in providers:
        if p.models:
            model_id = f"{p.key}/{p.models[0]}"
            model_ids.append(model_id)
    return model_ids


def assert_has_sections(text: str, sections: list[str]) -> bool:
    """检查文本是否包含指定的章节标题。"""
    text_lower = text.lower()
    return all(s.lower() in text_lower for s in sections)


def assert_min_length(text: str, min_chars: int) -> bool:
    """检查文本长度是否足够。"""
    return len(text.strip()) >= min_chars


def assert_not_hollow(text: str) -> bool:
    """检查文本不是空洞的工作描述（元语言）。"""
    hollow_starters = [
        "我将", "我会", "接下来", "首先", "然后", "现在开始",
        "让我", "下面我", "基于收集", "已经收集", "继续完成",
    ]
    stripped = text.strip()
    for starter in hollow_starters:
        if stripped.startswith(starter):
            return False
    return True


def assert_has_file_references(text: str, min_count: int = 1) -> bool:
    """检查文本中是否包含实际文件引用。"""
    import re
    file_patterns = [
        r'\b[\w/\\-]+\.(?:py|js|ts|json|yaml|yml|md|txt|bat|ps1|html|css)\b',
        r'[A-Z]:[\\/][^\s,]+',
    ]
    count = 0
    for pattern in file_patterns:
        count += len(re.findall(pattern, text))
    return count >= min_count


def section(text: str):
    """打印测试章节标题。"""
    print(f"\n{'=' * 60}")
    print(f"  {text}")
    print(f"{'=' * 60}")


# ═══════════════════════════════════════════════════════════════
# 测试场景 1: ReAct 引擎 — 简单分析任务
# ═══════════════════════════════════════════════════════════════

def test_react_simple_analysis():
    """ReAct 引擎：分析一个已知存在的项目目录。"""
    section("场景 1: ReAct 引擎 — 分析本地项目")

    model_ids = get_model_ids()
    if not model_ids:
        skip("场景 1 (ReAct 分析)", "未配置模型，跳过")
        return

    # 使用项目自身的目录作为测试目标（保证存在）
    test_dir = str(PROJECT_ROOT / "omniagent")

    from omniagent.engine.react_engine import ReActEngine
    from omniagent.engine.callbacks import EngineCallback

    engine = ReActEngine(
        model_priority=model_ids,
        max_iterations=8,
        callback=EngineCallback(),
    )

    user_input = f"请你简单分析 {test_dir} 目录下的代码结构，列出主要模块和它们的职责。用中文回答。"

    print(f"  输入: {user_input[:100]}...")
    print(f"  模型: {model_ids[0]}")
    print(f"  [运行中...]")

    try:
        start = time.time()
        result = engine.run(user_input)
        elapsed = time.time() - start

        print(f"  耗时: {elapsed:.1f}s")
        print(f"  输出长度: {len(result)} 字符")
        print(f"  输出预览: {result[:300]}...")

        # ── 质量验证 ──
        check("1.1 输出不为空", len(result.strip()) > 0)
        check("1.2 输出不小于 200 字符", assert_min_length(result, 200),
              f"实际长度: {len(result)}")
        check("1.3 输出不是空洞的元语言", assert_not_hollow(result),
              f"输出以 '{result[:50]}' 开头")
        check("1.4 包含实际文件引用", assert_has_file_references(result, min_count=2),
              f"未找到文件引用")

        # 模块名检查（至少有 engine, repl, tools 中的 2 个）
        module_count = sum(
            1 for m in ["engine", "repl", "tools", "nodes", "utils", "core", "mcp"]
            if m.lower() in result.lower()
        )
        check("1.5 提到了关键模块", module_count >= 2,
              f"只找到 {module_count} 个模块引用")

    except Exception as e:
        check("1.x 引擎执行", False, f"异常: {e}")


# ═══════════════════════════════════════════════════════════════
# 测试场景 2: Plan-Execute 引擎 — 结构化分析
# ═══════════════════════════════════════════════════════════════

def test_planexecute_analysis():
    """Plan-Execute 引擎：带规划的结构化分析。"""
    section("场景 2: Plan-Execute 引擎 — 结构化分析")

    model_ids = get_model_ids()
    if not model_ids:
        skip("场景 2 (Plan-Execute)", "未配置模型，跳过")
        return

    # 使用项目根目录（保证有 README.md + omniagent/）
    test_dir = str(PROJECT_ROOT)

    from omniagent.engine.plan_execute_engine import PlanExecuteEngine
    from omniagent.engine.callbacks import EngineCallback

    engine = PlanExecuteEngine(
        model_priority=model_ids,
        max_steps=10,
        callback=EngineCallback(),
    )

    user_input = f"分析 {test_dir} 这个 Python 项目的结构。读取 README.md 和 omniagent/__init__.py，然后说明项目是什么、有哪些子模块。"

    print(f"  输入: {user_input[:120]}...")
    print(f"  模型: {model_ids[0]}")
    print(f"  [运行中...]")

    try:
        start = time.time()
        result = engine.run(user_input)
        elapsed = time.time() - start

        print(f"  耗时: {elapsed:.1f}s")
        print(f"  输出长度: {len(result)} 字符")
        print(f"  输出预览: {result[:300]}...")

        check("2.1 输出不为空", len(result.strip()) > 0)
        check("2.2 输出不小于 150 字符", assert_min_length(result, 150),
              f"实际长度: {len(result)}")
        check("2.3 不是空洞输出", assert_not_hollow(result))
        # Plan-Execute 应该生成了步骤
        check("2.4 输出包含分析内容",
              len(result) > 100 and not result.startswith("未能生成有效的执行计划"),
              f"可能规划失败: {result[:100]}")

    except Exception as e:
        check("2.x 引擎执行", False, f"异常: {e}")


# ═══════════════════════════════════════════════════════════════
# 测试场景 3: ReAct 引擎 — 工具执行（代码搜索）
# ═══════════════════════════════════════════════════════════════

def test_react_tool_execution():
    """ReAct 引擎：验证工具实际执行（非模拟）。"""
    section("场景 3: ReAct 引擎 — 工具执行验证")

    model_ids = get_model_ids()
    if not model_ids:
        skip("场景 3 (工具执行)", "未配置模型，跳过")
        return

    from omniagent.engine.react_engine import ReActEngine
    from omniagent.engine.callbacks import EngineCallback
    from omniagent.engine.context import AgentContext

    engine = ReActEngine(
        model_priority=model_ids,
        max_iterations=6,
        callback=EngineCallback(),
    )

    # 这个任务强制 LLM 使用 list_files 工具
    context = AgentContext()
    user_input = f"列出 {PROJECT_ROOT / 'omniagent' / 'engine'} 目录下的所有 .py 文件，然后告诉我有哪些引擎相关的文件。"

    print(f"  输入: {user_input[:100]}...")
    print(f"  模型: {model_ids[0]}")
    print(f"  [运行中...]")

    try:
        start = time.time()
        result = engine.run(user_input, context=context)
        elapsed = time.time() - start

        print(f"  耗时: {elapsed:.1f}s")
        print(f"  输出长度: {len(result)} 字符")

        check("3.1 输出不为空", len(result.strip()) > 0)
        check("3.2 不是空洞输出", assert_not_hollow(result))

        # 检查是否提到了引擎文件
        engine_files = ["react_engine", "plan_execute_engine", "combined_engines", "reflection_engine"]
        found = [f for f in engine_files if f in result.lower()]
        check("3.3 提到了引擎文件",
              len(found) >= 1,
              f"找到的引擎: {found}")

    except Exception as e:
        check("3.x 工具执行", False, f"异常: {e}")


# ═══════════════════════════════════════════════════════════════
# 测试场景 4: 多范式一致性 — 同一问题在不同模式下都可完成
# ═══════════════════════════════════════════════════════════════

def test_cross_mode_consistency():
    """验证同一问题在 react 和 plan-execute 模式下都能给出有意义的回答。"""
    section("场景 4: 跨范式一致性")

    model_ids = get_model_ids()
    if not model_ids:
        skip("场景 4 (跨范式)", "未配置模型，跳过")
        return

    from omniagent.engine.react_engine import ReActEngine
    from omniagent.engine.plan_execute_engine import PlanExecuteEngine
    from omniagent.engine.callbacks import EngineCallback

    user_input = "解释一下 Python 的上下文管理器（context manager）是什么，并给出一个代码示例。用中文。"

    results: dict[str, str] = {}

    for mode_name, engine_cls, kwargs in [
        ("react", ReActEngine, {"max_iterations": 5}),
        ("plan-execute", PlanExecuteEngine, {"max_steps": 5}),
    ]:
        print(f"\n  --- {mode_name} 模式 ---")
        print(f"  [运行中...]")

        try:
            engine = engine_cls(
                model_priority=model_ids,
                callback=EngineCallback(),
                **kwargs,
            )
            start = time.time()
            result = engine.run(user_input)
            elapsed = time.time() - start

            results[mode_name] = result
            print(f"  耗时: {elapsed:.1f}s, 长度: {len(result)} 字符")

            check(f"4.{mode_name} 输出不为空", len(result.strip()) > 0)
            check(f"4.{mode_name} 输出不小于 100 字符",
                  assert_min_length(result, 100),
                  f"实际: {len(result)}")

        except Exception as e:
            check(f"4.{mode_name} 执行", False, f"异常: {e}")

    # 交叉验证：两个模式都完成了任务
    if len(results) == 2:
        both_ok = all(len(r) > 50 for r in results.values())
        check("4.5 两种范式都完成了任务", both_ok,
              f"react={len(results.get('react',''))}, plan-execute={len(results.get('plan-execute',''))}")


# ═══════════════════════════════════════════════════════════════
# 测试场景 5: 错误恢复 — 不存在的路径
# ═══════════════════════════════════════════════════════════════

def test_error_recovery():
    """验证引擎在遇到不存在的路径时能优雅处理而非崩溃。"""
    section("场景 5: 错误恢复 — 不存在的路径")

    model_ids = get_model_ids()
    if not model_ids:
        skip("场景 5 (错误恢复)", "未配置模型，跳过")
        return

    from omniagent.engine.react_engine import ReActEngine
    from omniagent.engine.callbacks import EngineCallback

    engine = ReActEngine(
        model_priority=model_ids,
        max_iterations=5,
        callback=EngineCallback(),
    )

    user_input = "读取文件 Z:\\nonexistent\\fake_project\\main.py 的内容并分析它。"

    print(f"  输入: {user_input}")
    print(f"  [运行中...]")

    try:
        start = time.time()
        result = engine.run(user_input)
        elapsed = time.time() - start

        print(f"  耗时: {elapsed:.1f}s")
        print(f"  输出: {result[:300]}...")

        # 核心验证：不应该崩溃，应该返回有意义的错误信息
        check("5.1 没有崩溃（返回了结果）", len(result.strip()) > 0)
        check("5.2 结果包含错误提示",
              any(kw in result.lower() for kw in ["不存在", "not found", "不存在", "找不到", "无此"]),
              f"输出: {result[:100]}")

    except Exception as e:
        check("5.x 错误恢复", False, f"引擎崩溃: {e}")


# ═══════════════════════════════════════════════════════════════
# 测试场景 6: 目录侦察服务
# ═══════════════════════════════════════════════════════════════

def test_directory_scout():
    """验证 DirectoryScout 能正确侦察目录结构。"""
    section("场景 6: DirectoryScout 服务")

    from omniagent.engine.directory_scout import DirectoryScout
    from omniagent.engine.context import AgentContext

    scout = DirectoryScout()

    # 测试 6.1: 从输入中提取目录
    path = scout.extract_directory("分析 D:\\test\\myproject 的代码")
    check("6.1 提取 Windows 路径", path == "D:\\test\\myproject",
          f"得到: {path}")

    path = scout.extract_directory("分析 /home/user/project 的结构")
    check("6.2 提取 Unix 路径", path == "/home/user/project",
          f"得到: {path}")

    path = scout.extract_directory("你好，帮我看看代码")
    check("6.3 无路径时返回 None", path is None,
          f"得到: {path}")

    # 测试 6.4: 侦察真实目录
    test_dir = str(PROJECT_ROOT / "omniagent" / "engine")
    result = scout.scout(f"分析 {test_dir}", AgentContext())

    if result.has_data:
        check("6.4 侦察到文件列表", len(result.root_files) > 0)
        check("6.5 侦察到 Python 文件", len(result.py_files) > 0 or result.root_files != "")
    else:
        check("6.4 侦察服务可用", False, f"错误: {result.error}")


# ═══════════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════════

def main():
    """运行所有端到端测试。"""
    global PASS, FAIL, SKIP

    print("=" * 60)
    print("  OmniAgent 端到端测试套件")
    print("  (通过真实引擎调用验证完整流程)")
    print("=" * 60)

    model_ids = get_model_ids()
    print(f"\n已配置模型: {model_ids if model_ids else '(无)'}")
    if model_ids:
        print(f"将使用: {model_ids[0]}")
    print()

    # ── 无需模型的快速测试 ──
    test_directory_scout()

    # ── 需要模型的测试 ──
    if not model_ids:
        print("\n" + "=" * 60)
        print("  ⚠️ 未配置模型，跳过需要 LLM 的测试")
        print("  请先运行 omniagent 并配置 API Key (/setup)")
        print("=" * 60)
    else:
        test_error_recovery()
        test_react_tool_execution()
        test_react_simple_analysis()
        test_planexecute_analysis()
        test_cross_mode_consistency()

    # ── 汇总 ──
    total = PASS + FAIL + SKIP
    print(f"\n{'=' * 60}")
    print(f"  测试完成: {total} 项")
    print(f"  通过: {PASS} | 失败: {FAIL} | 跳过: {SKIP}")
    if FAIL > 0:
        print(f"  ⚠️ 有 {FAIL} 项测试失败，请检查上述输出")
    else:
        print(f"  所有测试通过!" if SKIP == 0 else f"  所有可运行测试通过! ({SKIP} 项因无模型跳过)")
    print(f"{'=' * 60}")

    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
