"""测试符号链接逃逸漏洞修复 (P0-2)

CVE-2026-XXXX: 符号链接逃逸导致沙箱绕过
修复日期: 2026-08-23

攻击场景:
1. 攻击者在工作区创建符号链接指向沙箱外
2. LLM 被诱导写入该符号链接
3. _validate_path 验证通过（链接本身在沙箱内）
4. 批量操作失败回滚时，使用原始 Path 写入 → 写入沙箱外

修复:
_validate_path 返回 resolved 路径，所有操作使用返回值。
"""

import tempfile
from pathlib import Path
import pytest
from xenon.nodes.network_security import SecurityError
from xenon.nodes.tool_node import ToolNode
from xenon.engine.context import AgentContext


def test_symlink_escape_prevented(real_path_validation):
    """验证符号链接逃逸已被修复"""
    # 保存原始函数
    original_extra_roots = ToolNode._extra_allowed_roots

    try:
        # 禁用 /tmp 等额外根目录
        ToolNode._extra_allowed_roots = staticmethod(lambda: ())

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()

            outside = Path(tmpdir) / "outside"
            outside.mkdir()

            target_file = outside / "secret.txt"
            target_file.write_text("SECRET_DATA")

            # 攻击者创建符号链接
            symlink = workspace / "malicious.txt"
            symlink.symlink_to(target_file)

            # 创建工具节点
            context = AgentContext()
            node = ToolNode(
                "writer",
                action_type="write_file",
                file_path=str(symlink),
                content="EVIL_PAYLOAD",
                cwd=str(workspace),
            )

            # 尝试写入符号链接：拦截以 SecurityError 上抛，由 ToolExecutor
            # 归类为终端错误，而不是伪装成一次「执行完成但失败」的结果。
            with pytest.raises(SecurityError, match="路径越界"):
                node.execute(context)

            # 验证沙箱外文件未被修改
            assert target_file.read_text() == "SECRET_DATA"
    finally:
        # 恢复原始函数
        ToolNode._extra_allowed_roots = original_extra_roots


def test_symlink_escape_in_batch_rollback(real_path_validation):
    """验证批量操作回滚时的符号链接逃逸"""
    original_extra_roots = ToolNode._extra_allowed_roots

    try:
        ToolNode._extra_allowed_roots = staticmethod(lambda: ())

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()

            outside = Path(tmpdir) / "outside"
            outside.mkdir()

            target_file = outside / "secret.txt"
            target_file.write_text("SECRET_DATA")

            # 创建符号链接
            symlink = workspace / "malicious.txt"
            symlink.symlink_to(target_file)

            # 创建正常文件
            normal_file = workspace / "normal.txt"
            normal_file.write_text("NORMAL")

            context = AgentContext()

            # 批量写入：一个正常文件 + 一个符号链接
            node = ToolNode(
                "batch_writer",
                action_type="batch_write",
                files=[
                    {"file_path": str(normal_file), "content": "NEW_NORMAL"},
                    {"file_path": str(symlink), "content": "EVIL"},
                ],
                cwd=str(workspace),
            )

            result = node.execute(context)

            # 批量操作应该失败（包含不安全路径）
            assert result["success"] is False

            # 验证沙箱外文件未被修改
            assert target_file.read_text() == "SECRET_DATA"

            # 验证正常文件未被修改（事务回滚）
            assert normal_file.read_text() == "NORMAL"
    finally:
        ToolNode._extra_allowed_roots = original_extra_roots


def test_symlink_inside_workspace_allowed(real_path_validation):
    """验证工作区内的符号链接仍然可用"""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()

        # 创建工作区内的文件
        target = workspace / "target.txt"
        target.write_text("TARGET")

        # 创建工作区内的符号链接
        link = workspace / "link.txt"
        link.symlink_to(target)

        context = AgentContext()
        node = ToolNode(
            "writer",
            action_type="write_file",
            file_path=str(link),
            content="UPDATED",
            cwd=str(workspace),
        )

        result = node.execute(context)

        # 工作区内的符号链接应该允许
        assert result["success"] is True

        # 验证通过符号链接写入成功
        assert target.read_text() == "UPDATED"


def test_relative_path_escape_prevented(real_path_validation):
    """验证 ../ 路径逃逸被阻止"""
    original_extra_roots = ToolNode._extra_allowed_roots

    try:
        ToolNode._extra_allowed_roots = staticmethod(lambda: ())

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()

            outside = Path(tmpdir) / "outside.txt"
            outside.write_text("SECRET")

            context = AgentContext()
            node = ToolNode(
                "writer",
                action_type="write_file",
                file_path="../outside.txt",
                content="EVIL",
                cwd=str(workspace),
            )

            with pytest.raises(SecurityError, match="路径越界"):
                node.execute(context)

            # 验证文件未被修改
            assert outside.read_text() == "SECRET"
    finally:
        ToolNode._extra_allowed_roots = original_extra_roots


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
