"""单元测试：ToolGate 孤立行为。

测试范围：
1. from_config 解析各种配置组合
2. check_before 黑名单检查
3. get_param_validation_level 工具级覆盖
4. get_evidence_mode 工具级覆盖
5. metrics 记录
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from xenon.engine.tool_gate import (
    EvidenceMode,
    ParamValidationLevel,
    ToolGate,
    ToolGateConfig,
    ToolGateMetrics,
)


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
    tool_gate: MockToolGateSectionConfig | None = None

    def __post_init__(self):
        """如果 tool_gate 未显式设置，使用默认配置对象。"""
        if self.tool_gate is None:
            self.tool_gate = MockToolGateSectionConfig()


class TestToolGateFromConfig:
    """测试 from_config 配置解析。"""

    def test_from_config_defaults(self):
        """默认配置：moderate + observe + 无覆盖。"""
        config = MockSystemConfig()
        gate = ToolGate.from_config(config)

        assert gate.param_validation == ParamValidationLevel.MODERATE
        assert gate.evidence_mode == EvidenceMode.OBSERVE
        assert gate.tool_overrides == {}

    def test_from_config_strict_validation(self):
        """validation.strict=True 应设置全局 STRICT（当 tool_gate.param_validation 未显式设置时）。"""
        # 不设置 tool_gate 段，让 __post_init__ 创建默认的
        config = MockSystemConfig(
            validation=MockValidationConfig(strict=True),
        )
        # 确保 tool_gate.param_validation 不会覆盖 validation.strict
        # 通过设置为 None 让其使用 validation.strict
        config.tool_gate.param_validation = ""  # 空字符串让 from_config 跳过解析

        gate = ToolGate.from_config(config)

        assert gate.param_validation == ParamValidationLevel.STRICT

    def test_from_config_tool_gate_overrides_strict(self):
        """tool_gate.param_validation 应覆盖 validation.strict。"""
        config = MockSystemConfig(
            validation=MockValidationConfig(strict=True),
            tool_gate=MockToolGateSectionConfig(param_validation="lenient"),
        )
        gate = ToolGate.from_config(config)

        # tool_gate 段的配置优先级更高
        assert gate.param_validation == ParamValidationLevel.LENIENT

    def test_from_config_strict_with_tool_overrides(self):
        """C2 修复验证：strict=True 时不应忽略 tool_overrides。"""
        config = MockSystemConfig(
            validation=MockValidationConfig(strict=True),
            tool_gate=MockToolGateSectionConfig(
                param_validation="",  # 空字符串让 validation.strict 生效
                tool_overrides={
                    "command": {"disabled": True},
                    "read_file": {"param_validation": "lenient"},
                }
            ),
        )
        gate = ToolGate.from_config(config)

        # 全局应为 STRICT（继承 validation.strict）
        assert gate.param_validation == ParamValidationLevel.STRICT

        # 黑名单应生效
        assert gate.is_tool_disabled("command")
        assert not gate.is_tool_disabled("read_file")

        # 工具级覆盖应生效
        assert gate.get_param_validation_level("read_file") == ParamValidationLevel.LENIENT
        assert gate.get_param_validation_level("write_file") == ParamValidationLevel.STRICT

    def test_from_config_evidence_mode(self):
        """解析 evidence_mode 配置。"""
        config = MockSystemConfig(
            tool_gate=MockToolGateSectionConfig(evidence_mode="enforce"),
        )
        gate = ToolGate.from_config(config)

        assert gate.evidence_mode == EvidenceMode.ENFORCE

    def test_from_config_invalid_values(self):
        """无效配置值应回退到默认值并记录警告。"""
        config = MockSystemConfig(
            tool_gate=MockToolGateSectionConfig(
                param_validation="invalid_level",
                evidence_mode="invalid_mode",
            ),
        )
        gate = ToolGate.from_config(config)

        # 应回退到默认值
        assert gate.param_validation == ParamValidationLevel.MODERATE
        assert gate.evidence_mode == EvidenceMode.OBSERVE


class TestToolGateCheckBefore:
    """测试 check_before 黑名单检查。"""

    def test_check_before_allowed_tool(self):
        """未被禁用的工具应通过检查。"""
        gate = ToolGate()
        passed, reason = gate.check_before("read_file", {})

        assert passed is True
        assert reason == ""
        assert gate.metrics.denied_by_disabled == 0

    def test_check_before_disabled_tool(self):
        """被禁用的工具应拒绝执行并记录指标。"""
        gate = ToolGate(
            tool_overrides={
                "command": ToolGateConfig(disabled=True),
            }
        )
        passed, reason = gate.check_before("command", {"cmd": "ls"})

        assert passed is False
        assert "disabled" in reason.lower() or "禁用" in reason
        assert gate.metrics.denied_by_disabled == 1

    def test_check_before_multiple_denials(self):
        """多次拒绝应累积指标。"""
        gate = ToolGate(
            tool_overrides={
                "command": ToolGateConfig(disabled=True),
            }
        )

        for _ in range(3):
            passed, _ = gate.check_before("command", {})
            assert passed is False

        assert gate.metrics.denied_by_disabled == 3


class TestToolGateParamValidation:
    """测试 get_param_validation_level 工具级覆盖。"""

    def test_param_validation_global_default(self):
        """无覆盖时返回全局配置。"""
        gate = ToolGate(param_validation=ParamValidationLevel.STRICT)

        assert gate.get_param_validation_level("read_file") == ParamValidationLevel.STRICT
        assert gate.get_param_validation_level("write_file") == ParamValidationLevel.STRICT

    def test_param_validation_tool_override(self):
        """工具级覆盖应优先于全局配置。"""
        gate = ToolGate(
            param_validation=ParamValidationLevel.STRICT,
            tool_overrides={
                "read_file": ToolGateConfig(param_validation=ParamValidationLevel.LENIENT),
            }
        )

        assert gate.get_param_validation_level("read_file") == ParamValidationLevel.LENIENT
        assert gate.get_param_validation_level("write_file") == ParamValidationLevel.STRICT


class TestToolGateEvidenceMode:
    """测试 get_evidence_mode 工具级覆盖。"""

    def test_evidence_mode_global_default(self):
        """无覆盖时返回全局配置。"""
        gate = ToolGate(evidence_mode=EvidenceMode.ENFORCE)

        assert gate.get_evidence_mode("write_file") == EvidenceMode.ENFORCE
        assert gate.get_evidence_mode("edit_file") == EvidenceMode.ENFORCE

    def test_evidence_mode_tool_override(self):
        """工具级覆盖应优先于全局配置。"""
        gate = ToolGate(
            evidence_mode=EvidenceMode.ENFORCE,
            tool_overrides={
                "read_file": ToolGateConfig(evidence_mode=EvidenceMode.DISABLED),
            }
        )

        assert gate.get_evidence_mode("read_file") == EvidenceMode.DISABLED
        assert gate.get_evidence_mode("write_file") == EvidenceMode.ENFORCE


class TestToolGateMetrics:
    """测试 ToolGateMetrics 指标记录。"""

    def test_metrics_record_denial_disabled(self):
        """record_denial 应识别 disabled 原因。"""
        metrics = ToolGateMetrics()
        metrics.record_denial("工具 'command' 已被配置禁用")

        assert metrics.denied_by_disabled == 1
        assert metrics.denied_by_evidence == 0
        assert metrics.denied_by_params == 0

    def test_metrics_record_denial_evidence(self):
        """record_denial 应识别 evidence 原因。"""
        metrics = ToolGateMetrics()
        metrics.record_denial("缺少读取证据")

        assert metrics.denied_by_disabled == 0
        assert metrics.denied_by_evidence == 1
        assert metrics.denied_by_params == 0

    def test_metrics_record_denial_params(self):
        """record_denial 应识别 params 原因。"""
        metrics = ToolGateMetrics()
        metrics.record_denial("参数 'file_path' 疑似 LLM 幻觉")

        assert metrics.denied_by_disabled == 0
        assert metrics.denied_by_evidence == 0
        assert metrics.denied_by_params == 1

    def test_metrics_record_auto_read(self):
        """record_auto_read 应累积计数。"""
        metrics = ToolGateMetrics()
        metrics.record_auto_read()
        metrics.record_auto_read()

        assert metrics.auto_reads_triggered == 2

    def test_metrics_isolation(self):
        """不同 ToolGate 实例的 metrics 应独立。"""
        gate1 = ToolGate(
            tool_overrides={"command": ToolGateConfig(disabled=True)}
        )
        gate2 = ToolGate(
            tool_overrides={"command": ToolGateConfig(disabled=True)}
        )

        gate1.check_before("command", {})
        assert gate1.metrics.denied_by_disabled == 1
        assert gate2.metrics.denied_by_disabled == 0

        gate2.check_before("command", {})
        gate2.check_before("command", {})
        assert gate1.metrics.denied_by_disabled == 1
        assert gate2.metrics.denied_by_disabled == 2


class TestToolGateIsToolDisabled:
    """测试 is_tool_disabled 便捷方法。"""

    def test_is_tool_disabled_true(self):
        """被禁用的工具应返回 True。"""
        gate = ToolGate(
            tool_overrides={"command": ToolGateConfig(disabled=True)}
        )

        assert gate.is_tool_disabled("command") is True

    def test_is_tool_disabled_false(self):
        """未被禁用的工具应返回 False。"""
        gate = ToolGate(
            tool_overrides={"command": ToolGateConfig(disabled=False)}
        )

        assert gate.is_tool_disabled("command") is False
        assert gate.is_tool_disabled("read_file") is False
