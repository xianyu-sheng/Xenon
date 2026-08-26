"""统一工具门控系统 — xenon/engine/tool_gate.py

设计目标：融合三个独立系统为统一的工具门控层
- execution_policy: 决定"允许什么级别的操作"（战略层）
- evidence_gate: 记录"实际执行了什么操作"（审计层）
- tool_executor: 验证"参数是否合法"（战术层）

职责边界：
1. 工具黑名单（tool_overrides.disabled）
2. 参数校验级别（param_validation: strict/moderate/lenient）
3. 证据链模式（evidence_mode: enforce/observe/disabled）
4. 执行指标记录（拒绝次数、自动读取触发等）

集成方式：
- ToolExecutor 在 __init__ 时自动创建 ToolGate 实例（tool_gate=None 时）
- validate_tool_params() 函数接收 tool_gate 参数并查询校验级别
- 引擎无需修改，门控系统自动生效

会话隔离：
每个 ToolGate 实例持有独立的 metrics，多会话互不干扰。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


# ── 配置类型 ──────────────────────────────────────────────


class ParamValidationLevel(str, Enum):
    """参数校验级别枚举。"""
    STRICT = "strict"      # ≥2 条命中即拦截
    MODERATE = "moderate"  # ≥3 条命中才拦截（默认）
    LENIENT = "lenient"    # 不拦截，仅记录


class EvidenceMode(str, Enum):
    """证据链模式枚举。"""
    ENFORCE = "enforce"    # 强制要求证据，缺失则拒绝
    OBSERVE = "observe"    # 记录但不拒绝（默认）
    DISABLED = "disabled"  # 完全禁用证据链


@dataclass
class ToolGateConfig:
    """单个工具的门控配置（用于 tool_overrides）。"""
    disabled: bool = False
    param_validation: ParamValidationLevel | None = None
    evidence_mode: EvidenceMode | None = None


# ── 指标记录 ──────────────────────────────────────────────


@dataclass
class ToolGateMetrics:
    """工具门控指标（会话级，每个 ToolGate 实例独立）。"""
    denied_by_disabled: int = 0     # 黑名单拒绝次数
    denied_by_evidence: int = 0     # 证据链拒绝次数
    denied_by_params: int = 0       # 参数校验拒绝次数
    auto_reads_triggered: int = 0   # 自动读取触发次数

    def record_denial(self, reason: str) -> None:
        """记录拒绝事件到对应指标。"""
        reason_lower = reason.lower()
        if "disabled" in reason_lower or "黑名单" in reason_lower or "禁用" in reason_lower:
            self.denied_by_disabled += 1
        elif "evidence" in reason_lower or "证据" in reason_lower:
            self.denied_by_evidence += 1
        elif "param" in reason_lower or "参数" in reason_lower:
            self.denied_by_params += 1

    def record_auto_read(self) -> None:
        """记录自动读取触发。"""
        self.auto_reads_triggered += 1


# ── 主门控类 ──────────────────────────────────────────────


class ToolGate:
    """统一工具门控系统。

    集成三大审核维度：
    1. 黑名单控制（tool_overrides.disabled）
    2. 参数校验级别（三级：strict/moderate/lenient）
    3. 证据链模式（enforce/observe/disabled）

    使用方式：
        gate = ToolGate.from_config(get_config())

        # Stage 0.5: 黑名单检查
        passed, reason = gate.check_before(tool_name, params)
        if not passed:
            return fail(reason)

        # Stage 2: 参数校验
        level = gate.get_param_validation_level(tool_name)
        ok, reason, action = validate_tool_params(params, tool_name, gate)
    """

    def __init__(
        self,
        param_validation: ParamValidationLevel = ParamValidationLevel.MODERATE,
        evidence_mode: EvidenceMode = EvidenceMode.OBSERVE,
        tool_overrides: dict[str, ToolGateConfig] | None = None,
    ):
        """初始化工具门控。

        Args:
            param_validation: 全局参数校验级别
            evidence_mode: 全局证据链模式
            tool_overrides: 工具级覆盖配置
        """
        self.param_validation = param_validation
        self.evidence_mode = evidence_mode
        self.tool_overrides = tool_overrides or {}
        self.metrics = ToolGateMetrics()

    @classmethod
    def from_config(cls, config: Any) -> ToolGate:
        """从 SystemConfig 构造 ToolGate。

        配置优先级：
        1. validation.strict=True 时，全局 param_validation 设为 STRICT
        2. tool_gate.param_validation 覆盖全局级别
        3. tool_gate.tool_overrides 覆盖单个工具配置

        Args:
            config: SystemConfig 实例

        Returns:
            配置好的 ToolGate 实例
        """
        # 默认值
        param_validation = ParamValidationLevel.MODERATE
        evidence_mode = EvidenceMode.OBSERVE
        tool_overrides: dict[str, ToolGateConfig] = {}

        # 优先检查 validation.strict（向后兼容）
        if hasattr(config, "validation") and hasattr(config.validation, "strict") and config.validation.strict:
            param_validation = ParamValidationLevel.STRICT

        # 尝试读取 tool_gate 配置段
        tool_gate_section = getattr(config, "tool_gate", None)
        if tool_gate_section is not None:
            # 解析全局 param_validation（覆盖 validation.strict）
            pv = getattr(tool_gate_section, "param_validation", None)
            if pv:
                try:
                    param_validation = ParamValidationLevel(pv)
                except (ValueError, TypeError):
                    logger.warning(
                        "tool_gate.param_validation 值无效: %r，使用当前值", pv
                    )

            # 解析全局 evidence_mode
            em = getattr(tool_gate_section, "evidence_mode", None)
            if em:
                try:
                    evidence_mode = EvidenceMode(em)
                except (ValueError, TypeError):
                    logger.warning(
                        "tool_gate.evidence_mode 值无效: %r，使用默认值 observe", em
                    )

            # 解析 tool_overrides
            overrides_raw = getattr(tool_gate_section, "tool_overrides", None)
            if isinstance(overrides_raw, dict):
                for tool_name, override_data in overrides_raw.items():
                    if not isinstance(override_data, dict):
                        continue

                    tool_config = ToolGateConfig()

                    # disabled 黑名单
                    disabled = override_data.get("disabled")
                    if isinstance(disabled, bool):
                        tool_config.disabled = disabled

                    # param_validation 覆盖
                    pv_override = override_data.get("param_validation")
                    if pv_override:
                        try:
                            tool_config.param_validation = ParamValidationLevel(pv_override)
                        except (ValueError, TypeError):
                            logger.warning(
                                "tool_overrides.%s.param_validation 值无效: %r",
                                tool_name, pv_override
                            )

                    # evidence_mode 覆盖
                    em_override = override_data.get("evidence_mode")
                    if em_override:
                        try:
                            tool_config.evidence_mode = EvidenceMode(em_override)
                        except (ValueError, TypeError):
                            logger.warning(
                                "tool_overrides.%s.evidence_mode 值无效: %r",
                                tool_name, em_override
                            )

                    tool_overrides[tool_name] = tool_config

        return cls(
            param_validation=param_validation,
            evidence_mode=evidence_mode,
            tool_overrides=tool_overrides,
        )

    def check_before(
        self,
        tool_name: str,
        params: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        """Stage 0.5: 执行前检查（黑名单）。

        Args:
            tool_name: 工具名称
            params: 工具参数（保留用于未来扩展工具级规则，当前仅用于黑名单检查）

        Returns:
            (passed, reason) - passed=False 时应拒绝执行，reason 是人类可读原因
        """
        override = self.tool_overrides.get(tool_name)
        if override and override.disabled:
            reason = f"工具 '{tool_name}' 已被配置禁用（tool_gate.tool_overrides.{tool_name}.disabled=true）"
            self.metrics.record_denial(reason)
            return False, reason

        return True, ""

    def get_param_validation_level(self, tool_name: str) -> ParamValidationLevel:
        """获取指定工具的参数校验级别。

        优先级：tool_overrides[tool_name] > 全局配置

        Args:
            tool_name: 工具名称

        Returns:
            该工具应使用的参数校验级别
        """
        override = self.tool_overrides.get(tool_name)
        if override and override.param_validation is not None:
            return override.param_validation
        return self.param_validation

    def get_evidence_mode(self, tool_name: str) -> EvidenceMode:
        """获取指定工具的证据链模式。

        优先级：tool_overrides[tool_name] > 全局配置

        Args:
            tool_name: 工具名称

        Returns:
            该工具应使用的证据链模式
        """
        override = self.tool_overrides.get(tool_name)
        if override and override.evidence_mode is not None:
            return override.evidence_mode
        return self.evidence_mode

    def is_tool_disabled(self, tool_name: str) -> bool:
        """检查工具是否被禁用（黑名单）。

        Args:
            tool_name: 工具名称

        Returns:
            True 表示工具已被禁用
        """
        override = self.tool_overrides.get(tool_name)
        return override is not None and override.disabled
