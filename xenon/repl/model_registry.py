"""
Model Registry — 运行时模型管理。

支持：
- /set_model 添加/修改模型配置
- /models 查看当前模型列表
- /mode 切换思考范式
- 运行时动态修改模型优先级
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

import xenon.engine.builtin_engines  # noqa: F401  # 触发内置范式注册
from xenon.engine.registry import ENGINE_REGISTRY

from xenon.utils.atomic_write import atomic_write_text


@dataclass
class ModelConfig:
    """单个模型的运行时配置。"""

    model_id: str           # "provider/model_name"
    alias: str = ""         # 简短别名，如 "claude", "gpt4"
    api_key: str = ""       # 可选，覆盖全局凭证
    base_url: str = ""      # 可选，自定义端点
    max_tokens: int = 4096  # 请求给上游的生成预算；Xenon 不再二次钳制
    temperature: float = 0.7
    reasoning_effort: str = ""  # OpenAI-compatible reasoning level (low/medium/high/max)
    context_window: int = 128000  # 上下文窗口（R4）
    weight: float = 1.0            # v0.4.0: 模型池权重


@dataclass
class ThinkingMode:
    """思考范式配置。"""

    name: str                    # "react", "plan-execute", "reflection"
    description: str = ""
    workflow_template: str = ""  # 对应的 YAML 模板路径


# ── 预置思考范式 ──────────────────────────────────────────
# 由 ENGINE_REGISTRY 派生，避免范式清单出现第二份来源。此前这里是一份手工维护
# 的字典，与 repl.py 的 dispatch 链、setup_wizard 的说明文案和 evals 的白名单
# 各自独立，新增一种范式要四处同改，漏一处就静默不生效。
#
# 名称与描述的唯一来源是 xenon/engine/builtin_engines.py 的 register_engine()。
BUILTIN_MODES: dict[str, ThinkingMode] = {
    name: ThinkingMode(name=name, description=spec.description)
    for name, spec in ENGINE_REGISTRY.items()
}


class ModelRegistry:
    """
    运行时模型注册表。

    管理当前可用的模型、角色分配和思考范式。
    """

    def __init__(self) -> None:
        # 可用模型池 {alias: ModelConfig}
        self.models: dict[str, ModelConfig] = {}
        # 角色分配 {role_name: [alias, ...]}，如 {"planner": ["claude", "gpt4"]}
        self.role_priority: dict[str, list[str]] = {}
        # 当前思考范式
        self.current_mode: str = "direct"
        self.modes: dict[str, ThinkingMode] = dict(BUILTIN_MODES)

    # ── 模型管理 ──────────────────────────────────────────

    def add_model(self, model_id: str, alias: str, **kwargs: Any) -> ModelConfig:
        """
        添加或更新一个模型。

        Args:
            model_id: "provider/model_name" 格式
            alias: 简短别名
            **kwargs: api_key, base_url, max_tokens, temperature,
                reasoning_effort, context_window, weight
        """
        normalized_model = model_id.lower().rsplit("/", 1)[-1]
        ark_context_windows = {
            "doubao-seed-2-1-pro-260628": 262_144,
            "deepseek-v4-pro-260425": 1_048_576,
            "deepseek-v4-flash-260425": 1_048_576,
            "glm-5-2-260617": 1_048_576,
        }
        if "context_window" not in kwargs and model_id.lower().startswith("ark/"):
            from xenon.repl.provider_registry import get_model_metadata

            metadata = get_model_metadata(model_id)
            context_window = (
                int(metadata.get("context_window", 0) or 0)
                or ark_context_windows.get(normalized_model)
            )
            if context_window:
                kwargs["context_window"] = context_window
        if (
            "context_window" not in kwargs
            and normalized_model in {"deepseek-v4-pro", "deepseek-v4-flash"}
        ):
            # DeepSeek 官方 V4 模型规格（核对日期 2026-07-21）。
            kwargs["context_window"] = 1_000_000
        if normalized_model == "deepseek-v4-pro" and not kwargs.get("reasoning_effort"):
            # The official DeepSeek integration guide recommends max thinking
            # for V4 Pro coding tasks. Users can still override this per model.
            kwargs["reasoning_effort"] = "max"
        config = ModelConfig(model_id=model_id, alias=alias, **kwargs)
        self.models[alias] = config
        return config

    def context_window_for(self, aliases: list[str]) -> int:
        """返回给定别名模型的上下文窗口最小值（瓶颈模型）；无有效值则 0。

        R4：供 ContextManager 注入 max_tokens，使 needs_compact 按实际模型窗口
        触发，而非 128000 硬编码（8k 模型永不触发 / 1M 模型过早压缩）。
        """
        windows = [
            mc.context_window
            for a in aliases
            if (mc := self.models.get(a)) and getattr(mc, "context_window", 0) > 0
        ]
        return min(windows) if windows else 0

    def remove_model(self, alias: str) -> bool:
        """移除一个模型。"""
        if alias in self.models:
            del self.models[alias]
            # 同时从角色分配中清理
            for role in self.role_priority:
                self.role_priority[role] = [a for a in self.role_priority[role] if a != alias]
            return True
        return False

    def get_model(self, alias: str) -> ModelConfig | None:
        return self.models.get(alias)

    def get_model_by_id(self, model_id: str) -> ModelConfig | None:
        """Look up runtime configuration by canonical provider/model id."""
        return next((m for m in self.models.values() if m.model_id == model_id), None)

    def list_models(self) -> list[ModelConfig]:
        return list(self.models.values())

    # ── 角色分配 ──────────────────────────────────────────

    def assign_role(self, role: str, aliases: list[str]) -> None:
        """
        为角色设置模型优先级。

        Args:
            role: 角色名（如 "planner", "coder", "reviewer"）
            aliases: 模型别名列表，按优先级排列
        """
        # 验证所有别名都存在
        for alias in aliases:
            if alias not in self.models:
                raise ValueError(f"模型别名 '{alias}' 未注册。可用: {list(self.models.keys())}")
        self.role_priority[role] = aliases

    def get_role_priority(self, role: str) -> list[str]:
        """
        获取角色的模型优先级（返回 model_id 列表）。

        如果角色未分配，返回所有模型的默认顺序。
        """
        if role in self.role_priority:
            return [self.models[a].model_id for a in self.role_priority[role]]
        # 默认返回所有模型
        return [m.model_id for m in self.models.values()]

    # ── 思考范式 ──────────────────────────────────────────

    def set_mode(self, mode_name: str) -> ThinkingMode:
        """切换思考范式。"""
        if mode_name not in self.modes:
            available = ", ".join(self.modes.keys())
            raise ValueError(f"未知范式: {mode_name}。可用: {available}")
        self.current_mode = mode_name
        return self.modes[mode_name]

    def get_current_mode(self) -> ThinkingMode:
        return self.modes[self.current_mode]

    # ── 持久化 ────────────────────────────────────────────

    def export_config(self) -> dict[str, Any]:
        """导出当前配置为可序列化的字典。"""
        return {
            "models": {
                alias: {
                    "model_id": m.model_id,
                    "api_key": m.api_key,
                    "base_url": m.base_url,
                    "max_tokens": m.max_tokens,
                    "temperature": m.temperature,
                    "reasoning_effort": m.reasoning_effort,
                    "context_window": m.context_window,
                    "weight": m.weight,
                }
                for alias, m in self.models.items()
            },
            "roles": self.role_priority,
            "mode": self.current_mode,
        }

    def save_to_file(self, path: str | Path) -> None:
        """保存配置到 YAML 文件。"""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        content = yaml.dump(self.export_config(), allow_unicode=True, default_flow_style=False)
        atomic_write_text(path, content, mode=0o600)  # A9 原子写 + A10 chmod 0600

    def load_from_file(self, path: str | Path) -> None:
        """从 YAML 文件加载配置。"""
        path = Path(path)
        if not path.exists():
            return

        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        # 加载模型
        for alias, mcfg in data.get("models", {}).items():
            self.add_model(
                model_id=mcfg["model_id"],
                alias=alias,
                api_key=mcfg.get("api_key", ""),
                base_url=mcfg.get("base_url", ""),
                max_tokens=mcfg.get("max_tokens", 4096),
                temperature=mcfg.get("temperature", 0.7),
                reasoning_effort=mcfg.get("reasoning_effort", ""),
                context_window=mcfg.get("context_window", 128000),
                weight=mcfg.get("weight", 1.0),  # P0: 修复有损往返(export 写了 weight,load 原未读回)
            )

        # 加载角色
        for role, aliases in data.get("roles", {}).items():
            try:
                self.assign_role(role, aliases)
            except ValueError:
                pass

        # 加载范式
        if "mode" in data:
            try:
                self.set_mode(data["mode"])
            except ValueError:
                pass

    def load_from_credentials(self, credentials_path: str | Path | None = None) -> None:
        """从 credentials.yaml 的 providers 段加载模型。

        v0.8.5: 统一配置源 - 支持从 credentials.yaml 中声明的 providers 自动加载模型。
        只添加 models.yaml 中不存在的模型，避免覆盖用户手动配置。

        Args:
            credentials_path: credentials.yaml 路径，默认为 ~/.xenon/credentials.yaml
        """
        if credentials_path is None:
            credentials_path = Path.home() / ".xenon" / "credentials.yaml"
        else:
            credentials_path = Path(credentials_path)

        if not credentials_path.exists():
            return

        try:
            with open(credentials_path, encoding="utf-8") as f:
                creds = yaml.safe_load(f) or {}
        except Exception:
            return

        providers = creds.get("providers", {})
        if not providers:
            return

        # 获取已存在的 model_id 列表（来自 models.yaml）
        existing_model_ids = {m.model_id for m in self.models.values()}

        for provider_key, provider_config in providers.items():
            if not isinstance(provider_config, dict):
                continue

            api_key = provider_config.get("api_key", "")
            base_url = provider_config.get("base_url", "")
            models_list = provider_config.get("models", [])

            # v0.8.5: 识别真实 provider（根据 api_mode/api_style）
            # heyroute-claude -> anthropic
            # heyroute-deepseek -> openai
            # heyroute-gpt -> openai
            api_mode = provider_config.get("api_mode", "")
            api_style = provider_config.get("api_style", "")

            # 确定真实的 provider 前缀
            if api_mode == "anthropic" or api_style == "anthropic":
                real_provider = "anthropic"
            elif "gpt" in provider_key.lower() or "openai" in provider_key.lower():
                real_provider = "openai"
            elif "deepseek" in provider_key.lower():
                real_provider = "openai"  # DeepSeek 兼容 OpenAI API
            else:
                # 默认使用 provider_key 作为前缀
                real_provider = provider_key

            if not models_list:
                continue

            for model_name in models_list:
                # 构造 model_id 使用真实 provider
                model_id = f"{real_provider}/{model_name}"

                # 跳过已存在的模型（models.yaml 优先）
                if model_id in existing_model_ids:
                    continue

                # 生成别名
                alias = model_name.replace(".", "-").replace("/", "-")

                # 注册模型（weight=1.0 作为默认值，低于 models.yaml 中手动配置的优先级）
                try:
                    self.add_model(
                        model_id=model_id,
                        alias=alias,
                        api_key=api_key,
                        base_url=base_url,
                        weight=1.0,  # 默认权重，低于手动配置
                    )
                except Exception:
                    # 忽略注册失败的模型
                    continue
