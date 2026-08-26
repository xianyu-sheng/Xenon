"""集成测试：ToolGate 与 ToolExecutor 的完整集成。

测试范围：
1. ToolExecutor 自动创建 ToolGate
2. 黑名单工具被真实拦截
3. 参数校验三级与 ToolGate 集成
4. evidence_mode 集成（仅验证配置传递，实际行为由 evidence_gate 负责）
5. metrics 在真实执行流程中的记录
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


from xenon.engine.context import AgentContext
from xenon.engine.tool_gate import (
    EvidenceMode,
    ParamValidationLevel,
    ToolGate,
    ToolGateConfig,
)
from xenon.engine.tool_tracker import ToolExecutionTracker
from xenon.nodes.tool_executor import ToolExecutor, ToolExecutionState


@dataclass
class MockValidationConfig:
    """模拟 ValidationConfig。"""

    strict: bool = False


@dataclass
class MockToolGateSectionConfig:
    """模拟 ToolGateSectionConfig。"""

    param_validation: str = "moderate"
    evidence_mode: str = "observe"
    tool_overrides: dict[str, Any] = field(default_factory=dict)


@dataclass
class MockSystemConfig:
    """模拟 SystemConfig。"""

    validation: MockValidationConfig = field(default_factory=MockValidationConfig)
    tool_gate: MockToolGateSectionConfig = field(
        default_factory=MockToolGateSectionConfig
    )


class TestToolExecutorAutoCreatesToolGate:
    """测试 ToolExecutor 自动创建 ToolGate。"""

    def test_executor_auto_creates_tool_gate(self, monkeypatch):
        """ToolExecutor.__init__ 在 tool_gate=None 时应自动创建。"""
        # Mock get_config 返回自定义配置
        mock_config = MockSystemConfig(
            tool_gate=MockToolGateSectionConfig(
                param_validation="strict",
                tool_overrides={"command": {"disabled": True}},
            )
        )
        monkeypatch.setattr(
            "xenon.nodes.tool_executor.get_config",
            lambda: mock_config,
        )

        # 不传 tool_gate 参数
        executor = ToolExecutor()

        # 应自动创建 ToolGate 实例
        assert executor.tool_gate is not None
        assert isinstance(executor.tool_gate, ToolGate)

        # 配置应生效
        assert executor.tool_gate.param_validation == ParamValidationLevel.STRICT
        assert executor.tool_gate.is_tool_disabled("command") is True

    def test_executor_respects_explicit_tool_gate(self):
        """显式传入 tool_gate 应优先使用。"""
        custom_gate = ToolGate(
            param_validation=ParamValidationLevel.LENIENT,
            tool_overrides={"read_file": ToolGateConfig(disabled=True)},
        )

        executor = ToolExecutor(tool_gate=custom_gate)

        assert executor.tool_gate is custom_gate
        assert executor.tool_gate.param_validation == ParamValidationLevel.LENIENT
        assert executor.tool_gate.is_tool_disabled("read_file") is True


class TestToolExecutorBlacklistIntegration:
    """测试黑名单工具被真实拦截。"""

    def test_disabled_tool_blocked_by_executor(self):
        """黑名单工具应在 Stage 0.5 被拦截，不执行实际逻辑。"""
        gate = ToolGate(tool_overrides={"command": ToolGateConfig(disabled=True)})
        executor = ToolExecutor(tool_gate=gate)

        context = AgentContext()
        tracker = ToolExecutionTracker()

        result = executor.execute(
            tool_name="command",
            params={"cmd": "echo hello"},
            context=context,
            tracker=tracker,
        )

        # 应被拒绝
        assert result.success is False
        assert result.state == ToolExecutionState.FAILED
        assert result.error is not None
        assert "禁用" in result.error or "disabled" in result.error.lower()

        # metrics 应记录
        assert gate.metrics.denied_by_disabled == 1

    def test_disabled_tool_multiple_attempts(self):
        """多次尝试执行黑名单工具应累积 metrics。"""
        gate = ToolGate(tool_overrides={"write_file": ToolGateConfig(disabled=True)})
        executor = ToolExecutor(tool_gate=gate)

        context = AgentContext()
        tracker = ToolExecutionTracker()

        for _ in range(3):
            result = executor.execute(
                tool_name="write_file",
                params={"file_path": "/tmp/test.txt", "content": "test"},
                context=context,
                tracker=tracker,
            )
            assert result.success is False

        assert gate.metrics.denied_by_disabled == 3

    def test_non_disabled_tool_passes_gate(self):
        """未被禁用的工具应通过黑名单检查。"""
        gate = ToolGate(tool_overrides={"command": ToolGateConfig(disabled=True)})
        executor = ToolExecutor(tool_gate=gate)

        context = AgentContext()
        tracker = ToolExecutionTracker()

        # read_file 未被禁用，应通过黑名单检查
        # （后续可能因其他原因失败，但不应是 gate_denied）
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
            f.write("test content")
            temp_path = f.name

        try:
            result = executor.execute(
                tool_name="read_file",
                params={"file_path": temp_path},
                context=context,
                tracker=tracker,
            )

            # 不应被 gate 拒绝（error 中不应包含"禁用"相关字样）
            if result.error:
                assert "禁用" not in result.error
                assert "disabled" not in result.error.lower()
            # 实际应成功读取
            assert result.success is True
        finally:
            Path(temp_path).unlink(missing_ok=True)


class TestToolExecutorParamValidationIntegration:
    """测试参数校验与 ToolGate 的集成。"""

    def test_strict_validation_blocks_suspicious_params(self):
        """STRICT 级别应在 ≥2 条命中时拦截。"""
        gate = ToolGate(param_validation=ParamValidationLevel.STRICT)
        executor = ToolExecutor(tool_gate=gate)

        context = AgentContext()
        tracker = ToolExecutionTracker()

        # 构造疑似幻觉参数：包含 <placeholder> + 过长
        suspicious_path = "/path/to/<file_placeholder>" + "x" * 300

        result = executor.execute(
            tool_name="read_file",
            params={"file_path": suspicious_path},
            context=context,
            tracker=tracker,
        )

        # 应被参数校验拦截
        assert result.success is False
        assert result.error is not None
        assert "幻觉" in result.error or "可疑" in result.error

    def test_moderate_validation_allows_two_hits(self):
        """MODERATE 级别应允许 2 条命中（仅警告）。"""
        gate = ToolGate(param_validation=ParamValidationLevel.MODERATE)
        executor = ToolExecutor(tool_gate=gate)

        context = AgentContext()
        tracker = ToolExecutionTracker()

        # 2 条命中：<placeholder> + 过长（但不够 3 条）
        suspicious_path = "/path/to/<file_placeholder>" + "x" * 300

        result = executor.execute(
            tool_name="read_file",
            params={"file_path": suspicious_path},
            context=context,
            tracker=tracker,
        )

        # 应通过参数校验（但可能因文件不存在失败）
        # 检查错误不是因为参数幻觉
        if not result.success and result.error:
            assert "幻觉" not in result.error

    def test_lenient_validation_never_blocks(self):
        """LENIENT 级别应永不拦截。"""
        gate = ToolGate(param_validation=ParamValidationLevel.LENIENT)
        executor = ToolExecutor(tool_gate=gate)

        context = AgentContext()
        tracker = ToolExecutionTracker()

        # 构造明显幻觉参数：多条命中
        suspicious_path = "/path/to/<placeholder>" + "x" * 500 + "{{variable}}"

        result = executor.execute(
            tool_name="read_file",
            params={"file_path": suspicious_path},
            context=context,
            tracker=tracker,
        )

        # 不应被参数校验拦截（检查错误不是因为参数幻觉）
        if not result.success and result.error:
            assert "幻觉" not in result.error

    def test_tool_level_param_validation_override(self):
        """工具级校验配置应覆盖全局配置。"""
        gate = ToolGate(
            param_validation=ParamValidationLevel.STRICT,
            tool_overrides={
                "read_file": ToolGateConfig(
                    param_validation=ParamValidationLevel.LENIENT
                ),
            },
        )
        executor = ToolExecutor(tool_gate=gate)

        context = AgentContext()
        tracker = ToolExecutionTracker()

        # read_file 使用 LENIENT，write_file 使用 STRICT
        suspicious_path = "/path/to/<placeholder>" + "x" * 300

        # read_file 应通过（LENIENT）
        result1 = executor.execute(
            tool_name="read_file",
            params={"file_path": suspicious_path},
            context=context,
            tracker=tracker,
        )
        # 不应被参数校验拦截
        if not result1.success and result1.error:
            assert "幻觉" not in result1.error

        # write_file 应被拦截（STRICT）
        result2 = executor.execute(
            tool_name="write_file",
            params={"file_path": suspicious_path, "content": "test"},
            context=context,
            tracker=tracker,
        )
        assert result2.success is False
        assert result2.error is not None
        assert "幻觉" in result2.error or "可疑" in result2.error


class TestToolExecutorEvidenceModeIntegration:
    """测试 evidence_mode 配置传递（实际行为由 evidence_gate 负责）。"""

    def test_evidence_mode_configuration_passed(self):
        """ToolGate 的 evidence_mode 配置应正确传递。"""
        gate = ToolGate(
            evidence_mode=EvidenceMode.ENFORCE,
            tool_overrides={
                "read_file": ToolGateConfig(evidence_mode=EvidenceMode.DISABLED),
            },
        )

        # 验证配置正确
        assert gate.get_evidence_mode("write_file") == EvidenceMode.ENFORCE
        assert gate.get_evidence_mode("read_file") == EvidenceMode.DISABLED


class TestToolExecutorMetricsIntegration:
    """测试 metrics 在真实执行流程中的记录。"""

    def test_metrics_record_blacklist_denial(self):
        """黑名单拒绝应记录到 metrics。"""
        gate = ToolGate(tool_overrides={"command": ToolGateConfig(disabled=True)})
        executor = ToolExecutor(tool_gate=gate)

        context = AgentContext()
        tracker = ToolExecutionTracker()

        executor.execute(
            tool_name="command",
            params={"cmd": "ls"},
            context=context,
            tracker=tracker,
        )

        assert gate.metrics.denied_by_disabled >= 1

    def test_metrics_not_shared_across_executors(self):
        """不同 ToolExecutor 实例的 metrics 应独立。"""
        gate1 = ToolGate(tool_overrides={"command": ToolGateConfig(disabled=True)})
        gate2 = ToolGate(tool_overrides={"command": ToolGateConfig(disabled=True)})

        executor1 = ToolExecutor(tool_gate=gate1)
        executor2 = ToolExecutor(tool_gate=gate2)

        context = AgentContext()
        tracker = ToolExecutionTracker()

        # executor1 执行 2 次
        for _ in range(2):
            executor1.execute(
                tool_name="command",
                params={"cmd": "ls"},
                context=context,
                tracker=tracker,
            )

        # executor2 执行 1 次
        executor2.execute(
            tool_name="command",
            params={"cmd": "ls"},
            context=context,
            tracker=tracker,
        )

        # metrics 应独立
        assert gate1.metrics.denied_by_disabled == 2
        assert gate2.metrics.denied_by_disabled == 1


class TestToolExecutorBackwardCompatibility:
    """测试向后兼容性。"""

    def test_validation_strict_still_works(self, monkeypatch):
        """validation.strict=True 应继续生效。"""
        mock_config = MockSystemConfig(
            validation=MockValidationConfig(strict=True),
        )
        # 确保 tool_gate.param_validation 不覆盖
        mock_config.tool_gate.param_validation = ""

        monkeypatch.setattr(
            "xenon.nodes.tool_executor.get_config",
            lambda: mock_config,
        )

        executor = ToolExecutor()

        # 应自动创建 STRICT 级别的 ToolGate
        assert executor.tool_gate.param_validation == ParamValidationLevel.STRICT

    def test_validation_strict_with_overrides(self, monkeypatch):
        """validation.strict=True + tool_overrides 应同时生效（C2 修复）。"""
        mock_config = MockSystemConfig(
            validation=MockValidationConfig(strict=True),
            tool_gate=MockToolGateSectionConfig(
                param_validation="",  # 空字符串让 validation.strict 生效
                tool_overrides={"command": {"disabled": True}},
            ),
        )
        monkeypatch.setattr(
            "xenon.nodes.tool_executor.get_config",
            lambda: mock_config,
        )

        executor = ToolExecutor()
        context = AgentContext()
        tracker = ToolExecutionTracker()

        # 黑名单应生效
        result = executor.execute(
            tool_name="command",
            params={"cmd": "ls"},
            context=context,
            tracker=tracker,
        )

        assert result.success is False
        assert result.error is not None
        assert "禁用" in result.error or "disabled" in result.error.lower()

        # 全局校验级别应为 STRICT
        assert executor.tool_gate.param_validation == ParamValidationLevel.STRICT
