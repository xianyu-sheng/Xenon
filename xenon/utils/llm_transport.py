"""HTTP transport + endpoint resolution (extracted from llm_client)."""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any

from pathlib import Path
from urllib.parse import urlparse

import httpx
import yaml

logger = logging.getLogger(__name__)

_CLIENT_POOL: dict[str, httpx.Client] = {}
_CLIENT_LOCK = threading.Lock()


def _client_pool_key(endpoint: "ModelEndpoint") -> str:
    return f"{endpoint.provider}|{endpoint.base_url}"


def _get_pooled_client(
    endpoint: "ModelEndpoint", timeout: float = 120.0
) -> httpx.Client:
    """获取（或创建）per-provider 复用的长生命 httpx.Client。"""
    key = _client_pool_key(endpoint)
    with _CLIENT_LOCK:
        client = _CLIENT_POOL.get(key)
        if client is None or client.is_closed:
            client = _create_http_client(timeout=timeout)
            _CLIENT_POOL[key] = client
        return client


def close_clients() -> None:
    """显式关闭所有池化 Client（进程退出或测试清理时调用）。"""
    with _CLIENT_LOCK:
        for client in _CLIENT_POOL.values():
            try:
                client.close()
            except Exception:  # noqa: BLE001 — 关闭时忽略个别异常
                pass
        _CLIENT_POOL.clear()


# ── 安全代理处理 ────────────────────────────────────────────


def _build_proxy_config() -> httpx.Proxy | None:
    """
    从环境变量构建 httpx 兼容的代理配置。

    httpx 不支持 socks:// 代理，而部分用户环境可能设置了
    ALL_PROXY=socks://...（如 Clash 的混合端口），直接传给 httpx 会抛
    ValueError: Unknown scheme for proxy URL。

    此函数优先使用 HTTPS_PROXY/HTTP_PROXY，忽略不支持的 socks://。
    """
    for env_name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        val = os.getenv(env_name)
        if val and (val.startswith("http://") or val.startswith("https://")):
            return httpx.Proxy(url=val)

    # ALL_PROXY 只在使用 http/https 协议时才接受
    for env_name in ("ALL_PROXY", "all_proxy"):
        val = os.getenv(env_name)
        if val and (val.startswith("http://") or val.startswith("https://")):
            return httpx.Proxy(url=val)

    return None


def _create_http_client(
    timeout: float = 120.0,
    proxy: httpx.Proxy | None | object = _build_proxy_config,  # sentinel
    **kwargs: Any,
) -> httpx.Client:
    """
    创建带安全代理配置的 httpx.Client。

    自动从环境变量读取代理设置，过滤掉 httpx 不支持的 socks:// 协议。
    可通过 proxy=None 强制不走代理。
    额外关键字参数透传给 httpx.Client（如 follow_redirects）。
    """
    if proxy is _build_proxy_config:
        proxy = _build_proxy_config()
    return httpx.Client(timeout=timeout, proxy=proxy, **kwargs)


# ── 全局凭证路径 ──────────────────────────────────────────
_CREDENTIALS_PATH = Path.home() / ".xenon" / "credentials.yaml"


@dataclass
class ModelEndpoint:
    """单个模型的调用元信息。"""

    provider: str  # "openai" | "anthropic" | "deepseek"
    model_name: str  # 厂商侧模型名，如 "claude-3-5-sonnet-20241022"
    base_url: str  # API 基础地址
    api_key: str = field(repr=False, default="")
    max_tokens: int = 4096


# ── 厂商默认配置 ──────────────────────────────────────────
_PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "env_key": "OPENAI_API_KEY",
    },
    "anthropic": {
        "base_url": "https://api.anthropic.com",
        "env_key": "ANTHROPIC_API_KEY",
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "env_key": "DEEPSEEK_API_KEY",
    },
    "ark": {
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "env_key": "ARK_API_KEY",
    },
    "google": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "env_key": "GOOGLE_API_KEY",
    },
    "zhipu": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "env_key": "ZHIPU_API_KEY",
    },
    "qwen": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "env_key": "QWEN_API_KEY",
    },
    "moonshot": {
        "base_url": "https://api.moonshot.cn/v1",
        "env_key": "MOONSHOT_API_KEY",
    },
    "baichuan": {
        "base_url": "https://api.baichuan-ai.com/v1",
        "env_key": "BAICHUAN_API_KEY",
    },
    "minimax": {
        "base_url": "https://api.minimax.chat/v1",
        "env_key": "MINIMAX_API_KEY",
    },
    "ollama": {
        "base_url": "http://localhost:11434/v1",
        "env_key": "OLLAMA_API_KEY",
    },
    "xiaomi": {
        "base_url": "https://token-plan-cn.xiaomimimo.com/v1",
        "env_key": "XIAOMI_API_KEY",
    },
}


def _credentials_path():
    """惰性读路径：测试 patch llm_client._CREDENTIALS_PATH 时同步生效。"""

    from xenon.utils.llm_client import _CREDENTIALS_PATH

    return _CREDENTIALS_PATH


def _load_credentials() -> dict[str, str]:
    """从 ~/.xenon/credentials.yaml 或环境变量加载 API Key。

    v0.3.0+ 修复（C-2 延伸）：anthropic 厂商额外 fallback ANTHROPIC_AUTH_TOKEN
    （Claude Code / Anthropic SDK 标准环境变量）。原来只认 ANTHROPIC_API_KEY，
    导致 Claude Code 内跑 xenon 走代理（如火山方舟）时即便 ANTHROPIC_AUTH_TOKEN
    已设也会报"未找到 API Key"。
    """
    creds: dict[str, str] = {}
    if _credentials_path().exists():
        with open(_credentials_path(), encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
            if isinstance(data, dict):
                for key, value in data.items():
                    normalized_key = str(key).strip().lower()
                    if not normalized_key:
                        continue
                    if isinstance(value, str):
                        value = value.strip()
                        if not value:
                            continue
                    # Keep the legacy custom-provider mapping available to
                    # build_endpoint while rejecting blank scalar secrets.
                    creds[normalized_key] = value
            else:
                logger.warning(
                    "凭证文件不是 YAML 映射，忽略其内容: %s", _credentials_path()
                )
                data = {}
            if not creds.get("ark"):
                legacy_key = _legacy_ark_api_key(data)
                if legacy_key:
                    creds["ark"] = legacy_key

    # v0.4.0 修复: 环境变量作为补充（yaml 优先，env 仅在 yaml 未配置时生效）
    # 此前无条件覆盖导致 ~/.bashrc 旧 key 覆盖 yaml 新 key，对齐 provider_registry 行为
    for provider, cfg in _PROVIDER_DEFAULTS.items():
        env_val = os.getenv(cfg["env_key"])
        if isinstance(env_val, str) and env_val.strip() and not creds.get(provider):
            creds[provider] = env_val.strip()

    # v0.3.0+ 修复（C-2）：anthropic 额外 fallback ANTHROPIC_AUTH_TOKEN
    if not creds.get("anthropic"):
        auth_token = os.getenv("ANTHROPIC_AUTH_TOKEN")
        if isinstance(auth_token, str) and auth_token.strip():
            creds["anthropic"] = auth_token.strip()
    return creds


def _legacy_ark_api_key(data: dict[str, Any]) -> str:
    """Read one unambiguous Ark key from the pre-0.7 custom-provider shape."""
    custom = data.get("_custom_providers", {})
    if not isinstance(custom, dict):
        return ""
    candidates: list[str] = []
    for config in custom.values():
        if not isinstance(config, dict):
            continue
        try:
            hostname = (
                urlparse(str(config.get("base_url", ""))).hostname or ""
            ).lower()
        except ValueError:
            continue
        api_key = config.get("api_key")
        if (
            hostname == "ark.cn-beijing.volces.com"
            and isinstance(api_key, str)
            and api_key.strip()
        ):
            candidates.append(api_key.strip())
    unique = list(dict.fromkeys(candidates))
    return unique[0] if len(unique) == 1 else ""


def parse_model_id(model_id: str) -> tuple[str, str]:
    """
    解析 'provider/model_name' 格式的 model_id。
    例: "anthropic/claude-3-5-sonnet" -> ("anthropic", "claude-3-5-sonnet")
    """
    if "/" not in model_id:
        raise ValueError(
            f"model_id 必须为 'provider/model_name' 格式，收到: {model_id}"
        )
    provider, name = model_id.split("/", 1)
    return provider.lower(), name


def _load_custom_provider_config(provider_key: str) -> dict | None:
    """v0.4.0: 从 credentials.yaml 加载自定义模型商配置。

    v0.5.3: 兼容旧版本产生的空 key（纯中文名称注册时 key 被清空）。
    查找顺序：exact key → "custom"（修补后的默认 key）→ 空字符串（旧版本遗留）。

    自定义模型商历史上有两个段名：``/setup`` 写 ``_custom_providers``，而
    ``providers`` 段由外部集成（integration_cli）写入。ModelRegistry 只读
    ``providers`` 并据此注册模型，本函数原先只读 ``_custom_providers``——于是
    ``providers`` 段声明的模型商能注册出模型、却无法解析出 endpoint，选中即报
    「不支持的 provider」。两段现在都读，``_custom_providers`` 优先，使配置源
    在注册层与调用层之间保持一致。
    """
    try:
        import yaml as _yaml

        path = Path.home() / ".xenon" / "credentials.yaml"
        if not path.exists():
            return None
        with open(path, encoding="utf-8") as f:
            data = _yaml.safe_load(f) or {}
        custom_providers = data.get("_custom_providers")
        custom_providers = (
            custom_providers if isinstance(custom_providers, dict) else {}
        )
        declared = data.get("providers")
        declared = declared if isinstance(declared, dict) else {}
        # v0.5.3: 兼容空 key 和修补后的 "custom" key
        cfg = (
            custom_providers.get(provider_key)
            or declared.get(provider_key)
            or custom_providers.get("custom")
            or custom_providers.get("")
        )
        if not isinstance(cfg, dict) or not cfg.get("base_url"):
            # 没有 base_url 的条目无法构成 endpoint，交由调用方报「不支持的
            # provider」，而不是返回半个配置导致更晚、更难懂的失败。
            return None
        # 如果通过空 key 找到，自动修复为 "custom"（下次保存时生效）
        if cfg is not None and not custom_providers.get(provider_key):
            custom_providers["custom"] = cfg
        return cfg
    except Exception:
        return None


def build_endpoint(
    model_id: str,
    credentials: dict[str, str] | None = None,
    base_url: str | None = None,
) -> ModelEndpoint:
    """根据 model_id 构建完整的调用端点信息。

    v0.4.0: 支持动态注册的自定义模型商。
    """
    provider, model_name = parse_model_id(model_id)
    creds = credentials or _load_credentials()

    # v0.4.0: 先查内置 + 动态注册的 defaults
    defaults = _PROVIDER_DEFAULTS.get(provider)
    custom_config = None
    if defaults is None:
        # 尝试从自定义模型商加载
        custom_config = _load_custom_provider_config(provider)
        if custom_config:
            defaults = {"base_url": custom_config["base_url"], "env_key": ""}
        else:
            raise ValueError(
                f"不支持的 provider: {provider}，内置: {list(_PROVIDER_DEFAULTS.keys())}。"
                f"可使用 /setup 注册自定义模型商。"
            )

    api_key = creds.get(provider, "")
    api_key = api_key.strip() if isinstance(api_key, str) else ""
    # v0.5.3: 自定义模型商的 API Key 优先从 custom_config 取
    if not api_key and custom_config:
        api_key = custom_config.get("api_key", "")
        api_key = api_key.strip() if isinstance(api_key, str) else ""
    if not api_key:
        raise ValueError(
            f"未找到 {provider} 的 API Key。"
            f"请在 {_CREDENTIALS_PATH} 或环境变量 {defaults.get('env_key', '')} 中配置。"
        )
    return ModelEndpoint(
        provider=provider,
        model_name=model_name,
        base_url=(
            base_url
            or os.getenv(f"{provider.upper()}_BASE_URL")
            or defaults["base_url"]
        ),
        api_key=api_key,
    )


# ── 统一调用接口 ──────────────────────────────────────────


