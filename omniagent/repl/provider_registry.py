"""
Provider Registry — 预设厂商信息库。

所有主流大模型厂商的 base_url、模型列表、定价信息均已预设，
用户只需填入 API Key 即可使用。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml

CREDENTIALS_PATH = Path.home() / ".omniagent" / "credentials.yaml"
MODEL_LIST_TIMEOUT = 8.0

logger = logging.getLogger(__name__)


@dataclass
class ProviderInfo:
    """厂商预设信息。"""
    name: str               # 显示名
    key: str                # 内部标识
    base_url: str           # API 地址
    env_key: str            # 环境变量名
    models: list[str]       # 该厂商下的模型列表（短名）
    api_key: str = ""       # 用户填入的 key
    model_list_path: str = ""  # 支持 OpenAI 兼容 /models 时填入


# ── 预设厂商 ──────────────────────────────────────────────

PROVIDERS: dict[str, ProviderInfo] = {
    "openai": ProviderInfo(
        name="OpenAI",
        key="openai",
        base_url="https://api.openai.com/v1",
        env_key="OPENAI_API_KEY",
        models=["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-3.5-turbo", "o1-preview", "o1-mini"],
    ),
    "anthropic": ProviderInfo(
        name="Anthropic",
        key="anthropic",
        base_url="https://api.anthropic.com",
        env_key="ANTHROPIC_API_KEY",
        models=["claude-sonnet-4-20250514", "claude-3-5-sonnet-20241022", "claude-3-5-haiku-20241022", "claude-3-opus-20240229"],
    ),
    "deepseek": ProviderInfo(
        name="DeepSeek",
        key="deepseek",
        base_url="https://api.deepseek.com/v1",
        env_key="DEEPSEEK_API_KEY",
        models=["deepseek-v4-pro", "deepseek-v4-flash", "deepseek-chat", "deepseek-coder", "deepseek-reasoner"],
        model_list_path="https://api.deepseek.com/models",
    ),
    "google": ProviderInfo(
        name="Google Gemini",
        key="google",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        env_key="GOOGLE_API_KEY",
        models=["gemini-2.0-flash", "gemini-2.0-flash-lite", "gemini-1.5-pro", "gemini-1.5-flash"],
    ),
    "zhipu": ProviderInfo(
        name="智谱 GLM",
        key="zhipu",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        env_key="ZHIPU_API_KEY",
        models=["glm-4-plus", "glm-4-flash", "glm-4-long", "glm-4-air"],
    ),
    "qwen": ProviderInfo(
        name="阿里通义千问",
        key="qwen",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        env_key="QWEN_API_KEY",
        models=["qwen-max", "qwen-plus", "qwen-turbo", "qwen-long"],
    ),
    "moonshot": ProviderInfo(
        name="月之暗面 Kimi",
        key="moonshot",
        base_url="https://api.moonshot.cn/v1",
        env_key="MOONSHOT_API_KEY",
        models=["moonshot-v1-128k", "moonshot-v1-32k", "moonshot-v1-8k"],
    ),
    "baichuan": ProviderInfo(
        name="百川智能",
        key="baichuan",
        base_url="https://api.baichuan-ai.com/v1",
        env_key="BAICHUAN_API_KEY",
        models=["Baichuan4", "Baichuan3-Turbo", "Baichuan2-Turbo"],
    ),
    "minimax": ProviderInfo(
        name="MiniMax",
        key="minimax",
        base_url="https://api.minimax.chat/v1",
        env_key="MINIMAX_API_KEY",
        models=["abab6.5s-chat", "abab6.5-chat", "abab5.5-chat"],
    ),
    "ollama": ProviderInfo(
        name="Ollama (本地)",
        key="ollama",
        base_url="http://localhost:11434/v1",
        env_key="OLLAMA_API_KEY",
        models=["llama3", "llama3.1", "codellama", "deepseek-coder-v2", "qwen2.5", "mistral"],
    ),
    "xiaomi": ProviderInfo(
        name="小米 MiMo",
        key="xiaomi",
        base_url="https://token-plan-cn.xiaomimimo.com/v1",
        env_key="XIAOMI_API_KEY",
        models=["mimo-v2.5-pro", "mimo-v2.5", "mimo-v2-pro"],
    ),
}


def get_provider(key: str) -> ProviderInfo | None:
    """获取厂商信息。"""
    return PROVIDERS.get(key)


def list_providers() -> list[ProviderInfo]:
    """列出所有预设厂商。"""
    return list(PROVIDERS.values())


def get_all_model_ids() -> list[str]:
    """获取所有可用的 model_id（provider/model 格式）。"""
    result = []
    for p in PROVIDERS.values():
        for m in p.models:
            result.append(f"{p.key}/{m}")
    return result


def find_model_id(short_name: str) -> str | None:
    """
    根据短名查找完整的 model_id。
    例: "deepseek-coder" -> "deepseek/deepseek-coder"
    """
    for p in PROVIDERS.values():
        if short_name in p.models:
            return f"{p.key}/{short_name}"
    return None


def fetch_provider_models(provider: ProviderInfo, api_key: str) -> list[str]:
    """从厂商模型列表接口实时获取模型短名；失败时返回空列表。"""
    if not provider.model_list_path or not api_key:
        return []

    url = provider.model_list_path
    if not url.startswith(("http://", "https://")):
        url = f"{provider.base_url.rstrip('/')}/{url.lstrip('/')}"

    try:
        response = httpx.get(
            url,
            headers={"Accept": "application/json", "Authorization": f"Bearer {api_key}"},
            timeout=MODEL_LIST_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as e:
        logger.debug("获取 %s 模型列表失败，使用内置列表: %s", provider.key, e)
        return []

    models: list[str] = []
    seen: set[str] = set()
    for item in payload.get("data", []):
        model = item.get("id") if isinstance(item, dict) else item
        if isinstance(model, str) and model and model not in seen:
            models.append(model)
            seen.add(model)
    return models


# ── 凭证管理 ──────────────────────────────────────────────

def load_credentials() -> dict[str, str]:
    """从文件加载凭证。"""
    creds: dict[str, str] = {}
    if CREDENTIALS_PATH.exists():
        with open(CREDENTIALS_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
            creds = {k.lower(): v for k, v in data.items()}
    return creds


def save_credentials(creds: dict[str, str]) -> Path:
    """保存凭证到文件。"""
    CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CREDENTIALS_PATH, "w", encoding="utf-8") as f:
        yaml.dump(creds, f, allow_unicode=True, default_flow_style=False)
    return CREDENTIALS_PATH


def set_provider_key(provider_key: str, api_key: str) -> None:
    """设置某个厂商的 API Key 并保存。"""
    creds = load_credentials()
    creds[provider_key] = api_key
    save_credentials(creds)


def remove_provider_key(provider_key: str) -> None:
    """移除某个厂商的 API Key。"""
    creds = load_credentials()
    creds.pop(provider_key, None)
    save_credentials(creds)


def get_configured_providers(*, refresh_models: bool = True) -> list[ProviderInfo]:
    """获取已配置 API Key 的厂商列表。"""
    creds = load_credentials()
    configured = []
    for key, info in PROVIDERS.items():
        if key in creds and creds[key]:
            models = info.models
            if refresh_models:
                live_models = fetch_provider_models(info, creds[key])
                if live_models:
                    models = live_models
            info_copy = ProviderInfo(
                name=info.name, key=info.key, base_url=info.base_url,
                env_key=info.env_key, models=models, api_key=creds[key],
                model_list_path=info.model_list_path,
            )
            configured.append(info_copy)
    return configured
