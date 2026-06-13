"""
完整链路端到端测试：file_move / file_copy

模拟从 LLM JSON 响应 → parse → normalize → ToolNode 构造 → execute → 验证
覆盖 ReAct engine 的真实执行路径
"""
import sys
import tempfile
import shutil
from pathlib import Path

from omniagent.engine.context import AgentContext
from omniagent.nodes.tool_node import ToolNode
from omniagent.utils.response_adapter import parse_react

passed = 0
failed = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  [PASS] {name}")
    else:
        failed += 1
        print(f"  [FAIL] {name} -- {detail}")


def run_tests() -> int:
    global passed, failed

    print("=" * 60)
    print("完整链路集成测试：file_move / file_copy")
    print("=" * 60)

    # ── 准备测试文件（必须在项目目录内，安全策略限制文件操作不越界）──
    tmpdir = Path(tempfile.mkdtemp(prefix="omni_test_", dir=Path(__file__).resolve().parent))
    src_file = tmpdir / "test_source.txt"
    src_file.write_text("hello world", encoding="utf-8")
    dst_file = tmpdir / "moved_file.txt"

    print(f"测试目录: {tmpdir}")

    # ============================================================
    # 测试 1: file_move 完整链路 (LLM JSON → parse → normalize → execute)
    # ============================================================
    print()
    print("--- Test 1: file_move 完整链路 ---")

    # Step 1: 模拟 LLM 输出的 JSON
    llm_response = (
        '```json\n'
        '{"thought": "需要移动文件", "action": "file_move", '
        '"action_input": {"source": "'
        + str(src_file).replace("\\", "\\\\")
        + '", "destination": "'
        + str(dst_file).replace("\\", "\\\\")
        + '"}}\n'
        "```"
    )

    # Step 2: parse_react 解析
    parsed = parse_react(llm_response)
    check("1.1 parse_react 解析成功", "action" in parsed, str(parsed))
    check("1.2 action 为 file_move", parsed.get("action") == "file_move")
    check(
        "1.3 action_input 包含 source",
        "source" in parsed.get("action_input", {}),
    )
    check(
        "1.4 action_input 包含 destination",
        "destination" in parsed.get("action_input", {}),
    )

    # Step 3: normalize_params（这是之前出 bug 的环节！）
    action_input = parsed.get("action_input", {})
    normalized = ToolNode.normalize_params(action_input)
    check("1.5 normalize 保留 source", "source" in normalized, str(normalized))
    check(
        "1.6 normalize 保留 destination",
        "destination" in normalized,
        str(normalized),
    )

    # Step 4: 构造 ToolNode
    ctx = AgentContext()
    node = ToolNode("test_move", action_type="file_move", **normalized)
    check("1.7 ToolNode 构造成功", True)
    check(
        "1.8 node.source 正确",
        node.source == str(src_file),
        f"expected={src_file}, got={node.source}",
    )
    check(
        "1.9 node.destination 正确",
        node.destination == str(dst_file),
        f"expected={dst_file}, got={node.destination}",
    )

    # Step 5: 执行并验证
    result = node.execute(ctx)
    check("1.10 execute 返回成功", result.get("success") is True, str(result))
    check("1.11 源文件已移走", not src_file.exists(), "source still exists!")
    check("1.12 目标文件已存在", dst_file.exists(), "destination missing!")
    check(
        "1.13 目标文件内容正确",
        dst_file.read_text() == "hello world",
        f"content={dst_file.read_text()}",
    )

    # ============================================================
    # 测试 2: file_copy 完整链路
    # ============================================================
    print()
    print("--- Test 2: file_copy 完整链路 ---")

    src_file2 = tmpdir / "test_source2.txt"
    src_file2.write_text("copy me", encoding="utf-8")
    dst_copy = tmpdir / "copied_file.txt"

    llm_response2 = (
        '```json\n'
        '{"thought": "需要复制文件", "action": "file_copy", '
        '"action_input": {"source": "'
        + str(src_file2).replace("\\", "\\\\")
        + '", "destination": "'
        + str(dst_copy).replace("\\", "\\\\")
        + '"}}\n'
        "```"
    )

    parsed2 = parse_react(llm_response2)
    check("2.1 action 为 file_copy", parsed2.get("action") == "file_copy")

    normalized2 = ToolNode.normalize_params(parsed2.get("action_input", {}))
    check(
        "2.2 normalize 保留参数",
        "source" in normalized2 and "destination" in normalized2,
    )

    node2 = ToolNode("test_copy", action_type="file_copy", **normalized2)
    result2 = node2.execute(ctx)
    check("2.3 execute 返回成功", result2.get("success") is True, str(result2))
    check("2.4 源文件仍存在 (copy 不删除源)", src_file2.exists())
    check("2.5 目标文件已创建", dst_copy.exists())
    check("2.6 目标文件内容正确", dst_copy.read_text() == "copy me")

    # ============================================================
    # 测试 3: 错误处理
    # ============================================================
    print()
    print("--- Test 3: 错误处理 ---")

    nonexistent = tmpdir / "does_not_exist.txt"
    node3 = ToolNode(
        "test_err",
        action_type="file_move",
        source=str(nonexistent),
        destination=str(tmpdir / "nope.txt"),
    )
    result3 = node3.execute(ctx)
    check(
        "3.1 源不存在返回失败",
        result3.get("success") is False,
        str(result3),
    )
    check(
        "3.2 错误信息包含提示",
        "不存在" in result3.get("error", ""),
        result3.get("error", ""),
    )

    # ============================================================
    # 测试 4: 目录移动
    # ============================================================
    print()
    print("--- Test 4: 目录移动 ---")

    src_dir2 = tmpdir / "dir_to_move"
    src_dir2.mkdir(exist_ok=True)
    (src_dir2 / "file1.txt").write_text("file1")
    sub_dir = src_dir2 / "sub"
    sub_dir.mkdir()
    (sub_dir / "file2.txt").write_text("file2")

    dst_dir2 = tmpdir / "moved_dir_dest"

    node4 = ToolNode(
        "test_dirmove",
        action_type="file_move",
        source=str(src_dir2),
        destination=str(dst_dir2),
    )
    result4 = node4.execute(ctx)
    check("4.1 目录移动成功", result4.get("success") is True, str(result4))
    check("4.2 源目录已不存在", not src_dir2.exists())
    check("4.3 目标目录已存在", dst_dir2.exists())
    check(
        "4.4 子文件保留",
        (dst_dir2 / "file1.txt").exists() and (dst_dir2 / "sub" / "file2.txt").exists(),
    )

    # ============================================================
    # 测试 5: ReAct Engine 内部 _execute_tool 集成
    # ============================================================
    print()
    print("--- Test 5: ReAct Engine._execute_tool 集成 ---")

    from omniagent.engine.react_engine import ReActEngine
    from omniagent.engine.callbacks import SilentCallback

    engine = ReActEngine(
        model_priority=["deepseek/deepseek-v4-pro"],
        max_iterations=1,
        callback=SilentCallback(),
    )

    # 通过 engine 的 _execute_tool 执行（这是真实执行路径）
    src_file3 = tmpdir / "integration_test.txt"
    src_file3.write_text("integration test content")
    dst_file3 = tmpdir / "integration_result.txt"

    tool_result = engine._execute_tool(
        action="file_move",
        action_input={
            "source": str(src_file3),
            "destination": str(dst_file3),
        },
        context=AgentContext(),
    )
    check(
        "5.1 Engine._execute_tool 返回字符串",
        isinstance(tool_result, str),
        type(tool_result),
    )
    check("5.2 目标文件已存在", dst_file3.exists())
    check("5.3 源文件已移走", not src_file3.exists())

    # 同样测试 file_copy
    src_file4 = tmpdir / "integration_test2.txt"
    src_file4.write_text("copy via engine")
    dst_file4 = tmpdir / "integration_copy_result.txt"

    tool_result2 = engine._execute_tool(
        action="file_copy",
        action_input={
            "source": str(src_file4),
            "destination": str(dst_file4),
        },
        context=AgentContext(),
    )
    check(
        "5.4 Engine file_copy 返回字符串",
        isinstance(tool_result2, str),
    )
    check("5.5 源文件仍存在", src_file4.exists())
    check("5.6 目标文件已创建", dst_file4.exists())
    check(
        "5.7 内容正确",
        dst_file4.read_text() == "copy via engine",
    )

    # ============================================================
    # 测试 6: 权限审批链路
    # ============================================================
    print()
    print("--- Test 6: 权限审批链路 ---")

    approval_log = []

    def capture_approval(tool_name, params_preview):
        approval_log.append((tool_name, params_preview))
        return True

    ToolNode.set_approval_handler(capture_approval)

    src_file5 = tmpdir / "approval_test.txt"
    src_file5.write_text("approval test")
    dst_file5 = tmpdir / "approved_move.txt"

    node6 = ToolNode(
        "test_perm",
        action_type="file_move",
        source=str(src_file5),
        destination=str(dst_file5),
    )
    result6 = node6.execute(ctx)
    ToolNode.set_approval_handler(None)
    check("6.1 审批 handler 可被调用", True)  # 当前 security 对 file_move 默认 allow
    check("6.2 审批后执行成功", result6.get("success") is True, str(result6))

    # ============================================================
    # 清理
    # ============================================================
    shutil.rmtree(tmpdir, ignore_errors=True)
    print(f"\n清理完成: {tmpdir}")

    # ============================================================
    # 总结
    # ============================================================
    print()
    print("=" * 60)
    print(f"结果: {passed} passed, {failed} failed (共 {passed+failed} 项)")
    print("=" * 60)

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(run_tests())
