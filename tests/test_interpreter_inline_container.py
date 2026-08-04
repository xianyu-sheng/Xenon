"""SWE-bench 隔离容器解释器内联验证测试。

验证：docker exec 隔离容器内 `python -c` 验证命令放行（SWE-bench 标准工作流），
宿主机上保持拦截（行为不变），其他危险模式（rm -rf / 等）容器内同样拦截。
"""

from __future__ import annotations

import pytest

from xenon.nodes.tool_node import SecurityError, ToolNode


def _make_tool(**kwargs) -> ToolNode:
    """构造 ToolNode（默认 command 动作）。"""
    return ToolNode(
        "test-node",
        action_type="command",
        action="",
        **kwargs,
    )


class TestInterpreterInlineInContainer:
    def test_host_blocks_python_c(self) -> None:
        """宿主机（无 docker 前缀）上 python -c 仍然拦截。"""
        node = _make_tool()
        node.bind_command_runtime(())  # 空前缀 = 宿主机
        assert node.allow_interpreter_inline is False
        with pytest.raises(SecurityError):
            node._validate_command(
                'python -c "import os; print(1)"'
            )

    def test_container_allows_python_c(self) -> None:
        """docker exec 容器内 python -c 放行（SWE-bench 验证循环）。"""
        node = _make_tool()
        node.bind_command_runtime(("docker", "exec", "sweb.xenon.test", "bash"))
        assert node.allow_interpreter_inline is True
        # 不应抛异常
        node._validate_command(
            'python -c "from parser import parse_left; print(parse_left(1))"'
        )

    def test_container_blocks_rm_rf(self) -> None:
        """容器内 rm -rf / 仍拦截（防止破坏评测环境）。"""
        node = _make_tool()
        node.bind_command_runtime(("docker", "exec", "sweb.xenon.test", "bash"))
        with pytest.raises(SecurityError):
            node._validate_command("rm -rf /")

    def test_container_blocks_shutdown(self) -> None:
        """容器内 shutdown 仍拦截。"""
        node = _make_tool()
        node.bind_command_runtime(("docker", "exec", "sweb.xenon.test", "bash"))
        with pytest.raises(SecurityError):
            node._validate_command("shutdown now")

    def test_container_blocks_base64_decode_exec(self) -> None:
        """容器内 base64 解码执行仍拦截。"""
        node = _make_tool()
        node.bind_command_runtime(("docker", "exec", "sweb.xenon.test", "bash"))
        with pytest.raises(SecurityError):
            node._validate_command(
                'echo "cHl0aG9uLm9zLnN5c3RlbSgncm0gLXJmIC8nKQ==" | base64 -d | python'
            )

    def test_security_disabled_skips_all(self) -> None:
        """security_enabled=False 时全跳过（含容器内）。"""
        node = _make_tool(security_enabled=False)
        node.bind_command_runtime(("docker", "exec", "sweb.xenon.test", "bash"))
        node._validate_command("rm -rf /")  # 不抛
        assert True

    def test_direct_prefix_not_docker_keeps_blocked(self) -> None:
        """非 docker exec 前缀（如自定义 wrapper）不触发放行。"""
        node = _make_tool()
        node.bind_command_runtime(("sudo", "sh"))
        assert node.allow_interpreter_inline is False
        with pytest.raises(SecurityError):
            node._validate_command('python -c "print(1)"')
