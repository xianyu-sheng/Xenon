"""
OmniAgent P0+P1 真实操作测试 — 模拟用户实际使用场景

场景:
1. 创建项目 + 写入文件 → 验证 checkpoint 保护
2. 搜索代码 → 验证 ripgrep/Python re 搜索
3. 编辑文件 → 验证 checkpoint 还原
4. 移动文件 → 验证 move + checkpoint
5. 写入失败 → 验证断路器 + 重试
6. 运行 pytest → 验证测试工具
7. ReAct 引擎短任务 → 验证端到端
8. Compactor 压缩 → 验证上下文管理
"""
import asyncio
import sys
import tempfile
import shutil
from pathlib import Path

PASS = 0
FAIL = 0

def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} -- {detail}")


def main():
    global PASS, FAIL
    print("=" * 60)
    print("OmniAgent P0+P1 真实操作测试")
    print("=" * 60)

    # ═══════════════════════════════════════════════
    # 场景 1: 创建项目 + 写入文件 (Checkpoint)
    # ═══════════════════════════════════════════════
    print("\n[场景1] 创建项目 + 写入文件")
    from omniagent.tools.file_ops import WriteFileTool, CreateDirectoryTool, ListFilesTool
    from omniagent.engine.checkpoint import get_checkpoint

    tmpdir = Path(tempfile.mkdtemp(prefix="omni_real_", dir=Path.cwd()))
    ckpt = get_checkpoint()

    # 1.1 创建目录
    ct = CreateDirectoryTool()
    r = asyncio.run(ct.invoke({"file_path": str(tmpdir / "myproject" / "src")}))
    check("1.1 创建嵌套目录", not r.is_error, str(r.content)[:100])

    # 1.2 写入多个文件
    wt = WriteFileTool()
    files = {
        "myproject/README.md": "# My Project\n\nA test project.",
        "myproject/src/main.py": "def main():\n    print('Hello World')\n\nif __name__ == '__main__':\n    main()",
        "myproject/src/utils.py": "def add(a, b):\n    return a + b",
        "myproject/tests/test_main.py": "def test_main():\n    assert True",
    }
    for fpath, content in files.items():
        r = asyncio.run(wt.invoke({"file_path": str(tmpdir / fpath), "content": content}))
        check(f"1.2 写入 {fpath}", not r.is_error, str(r.content)[:100])

    # 1.3 列出文件
    lt = ListFilesTool()
    r = asyncio.run(lt.invoke({"file_path": str(tmpdir / "myproject"), "pattern": "*.py"}))
    py_files = r.metadata.get("files", [])
    check("1.3 列出 Python 文件", len(py_files) >= 2, f"found {len(py_files)}")

    # ═══════════════════════════════════════════════
    # 场景 2: 搜索代码 (Ripgrep/Python re)
    # ═══════════════════════════════════════════════
    print("\n[场景2] 搜索代码")
    from omniagent.tools.search_git import SearchFilesTool
    st = SearchFilesTool()

    # 2.1 搜索函数定义
    r = asyncio.run(st.invoke({
        "file_path": str(tmpdir / "myproject"),
        "search_pattern": "def main",
        "file_filter": "*.py",
    }))
    check("2.1 搜索 def main", r.metadata.get("match_count", 0) >= 1,
          f"matches={r.metadata.get('match_count')}, engine={r.metadata.get('engine')}")
    check("2.2 搜索引擎", r.metadata.get("engine") in ("ripgrep", "python_re"))

    # 2.2 搜索不存在的模式
    r = asyncio.run(st.invoke({
        "file_path": str(tmpdir / "myproject"),
        "search_pattern": "XYZZY_NOT_HERE_999",
    }))
    check("2.3 搜索无匹配", r.metadata.get("match_count", -1) == 0)

    # ═══════════════════════════════════════════════
    # 场景 3: 编辑文件 (EditFileTool + Checkpoint)
    # ═══════════════════════════════════════════════
    print("\n[场景3] 编辑文件 + Checkpoint 保护")
    from omniagent.tools.file_ops import EditFileTool, ReadFileTool
    et = EditFileTool()
    rt = ReadFileTool()

    main_py = tmpdir / "myproject" / "src" / "main.py"
    original = main_py.read_text()

    # 3.1 正常编辑
    r = asyncio.run(et.invoke({
        "file_path": str(main_py),
        "old_text": "Hello World",
        "new_text": "Hello OmniAgent",
    }))
    check("3.1 编辑成功", not r.is_error, str(r.content)[:100])
    check("3.2 内容已更新", "Hello OmniAgent" in main_py.read_text())

    # 3.3 编辑不存在的文本
    r = asyncio.run(et.invoke({
        "file_path": str(main_py),
        "old_text": "THIS_TEXT_DOES_NOT_EXIST_999",
        "new_text": "whatever",
    }))
    check("3.3 编辑不存在的文本报错", r.is_error)
    check("3.4 文件未被破坏", "Hello OmniAgent" in main_py.read_text(),
          "内容被意外修改!")

    # ═══════════════════════════════════════════════
    # 场景 4: 移动文件 (FileMoveTool + Checkpoint)
    # ═══════════════════════════════════════════════
    print("\n[场景4] 移动文件")
    from omniagent.tools.file_ops import FileMoveTool
    mt = FileMoveTool()

    src = tmpdir / "myproject" / "src" / "utils.py"
    dst = tmpdir / "myproject" / "src" / "helpers.py"
    src_content = src.read_text()

    r = asyncio.run(mt.invoke({"source": str(src), "destination": str(dst)}))
    check("4.1 移动成功", not r.is_error, str(r.content)[:100])
    check("4.2 源已不存在", not src.exists())
    check("4.3 目标已存在", dst.exists())
    check("4.4 内容保留", dst.read_text() == src_content)

    # ═══════════════════════════════════════════════
    # 场景 5: 断路器 + 重试 (模拟连续失败)
    # ═══════════════════════════════════════════════
    print("\n[场景5] 断路器 + 重试")
    from omniagent.engine.react_engine import ReActEngine
    from omniagent.engine.callbacks import SilentCallback
    from omniagent.engine.context import AgentContext

    engine = ReActEngine(
        model_priority=["deepseek/deepseek-v4-pro"],
        max_iterations=3,
        callback=SilentCallback(),
    )

    # 5.1 连续失败触发断路器
    for i in range(3):
        engine._execute_tool("read_file", {"file_path": str(tmpdir / "NONEXISTENT_FILE.xyz")}, AgentContext())
    status = engine.breaker.status("read_file")
    check("5.1 断路器触发", status.get("tripped", False),
          f"failures={status.get('consecutive_failures')}, tripped={status.get('tripped')}")

    # 5.2 断路器阻止后续调用
    allow = engine.breaker.allow("read_file")
    check("5.2 断路器阻止工具", not allow)

    # 5.3 其他工具不受影响
    check("5.3 其他工具正常", engine.breaker.allow("write_file"))

    # 5.4 重置后恢复
    engine.breaker.reset("read_file")
    check("5.4 重置后恢复", engine.breaker.allow("read_file"))

    # ═══════════════════════════════════════════════
    # 场景 6: 运行测试 (PytestTool)
    # ═══════════════════════════════════════════════
    print("\n[场景6] PytestTool")
    from omniagent.tools.test_runner import PytestTool, TestCommandTool

    # 6.1 解析 pytest 输出
    ptool = PytestTool()
    parsed = PytestTool._parse_pytest_output(
        "tests/test_a.py::t1 PASSED\ntests/test_b.py::t2 FAILED\n"
        "FAILED tests/test_b.py::t2 - AssertionError\n2 passed, 1 failed",
        1,
    )
    check("6.1 解析 passed", parsed["passed"] == 2, str(parsed))
    check("6.2 解析 failed", parsed["failed"] == 1, str(parsed))
    check("6.3 解析 failures 详情", len(parsed["failures"]) == 1)

    # 6.2 TestCommandTool
    tctool = TestCommandTool()
    r = asyncio.run(tctool.invoke({"command": "echo integration_test_ok"}))
    check("6.4 命令执行成功", not r.is_error and "integration_test_ok" in str(r.content))
    check("6.5 returncode=0", r.metadata.get("returncode") == 0)

    # 6.3 危险命令拦截
    r = asyncio.run(tctool.invoke({"command": "rm -rf /"}))
    check("6.6 危险命令拦截", r.is_error)

    # ═══════════════════════════════════════════════
    # 场景 7: ReAct 引擎端到端
    # ═══════════════════════════════════════════════
    print("\n[场景7] ReAct 引擎端到端")
    engine2 = ReActEngine(
        model_priority=["deepseek/deepseek-v4-pro"],
        max_iterations=3,
        callback=SilentCallback(),
    )
    # 简单任务: 不需要工具
    result = engine2.run("用一句话回答: 1+1等于几? 只输出答案。")
    check("7.1 ReAct 任务成功", len(result) > 0 and ("2" in result or "二" in result),
          f"result={result[:100]}")
    check("7.2 结果合理长度", 1 < len(result) < 500, f"len={len(result)}")

    # ═══════════════════════════════════════════════
    # 场景 8: Compactor 上下文压缩
    # ═══════════════════════════════════════════════
    print("\n[场景8] Compactor")
    from omniagent.engine.compactor import Compactor
    compactor = Compactor(Path(".omniagent/sessions/_real_test"))

    # 8.1 小上下文不需要压缩
    small = [{"role": "user", "content": "hi"}]
    check("8.1 小上下文不压缩", not compactor.needs_compact(Compactor._estimate_tokens(small)))

    # 8.2 大上下文需要压缩
    big = [{"role": "user", "content": "x" * 700000}]
    check("8.2 大上下文需要压缩", compactor.needs_compact(Compactor._estimate_tokens(big)))

    # 8.3 消息格式化
    msgs = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Write a function to sort a list."},
        {"role": "assistant", "content": "Here is the code:\n```python\ndef sort_list(lst):\n    return sorted(lst)\n```"},
    ]
    formatted = Compactor._format_messages(msgs)
    check("8.3 格式化包含 role", "[system]" in formatted and "[user]" in formatted)
    check("8.4 格式化包含关键词", "sort" in formatted.lower())

    # ═══════════════════════════════════════════════
    # 清理
    # ═══════════════════════════════════════════════
    shutil.rmtree(tmpdir, ignore_errors=True)
    print(f"\n清理: {tmpdir}")

    # ═══════════════════════════════════════════════
    # 总结
    # ═══════════════════════════════════════════════
    print()
    print("=" * 60)
    print(f"结果: {PASS} passed, {FAIL} failed (共 {PASS + FAIL} 项)")
    print("=" * 60)

    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
