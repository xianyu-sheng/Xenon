"""
Provider Registry — 预设厂商信息库。

所有主流大模型厂商的 base_url 已预设。配置 API Key 后，模型列表会优先
从厂商接口实时拉取；内置列表只作为离线兜底。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

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
    models: list[str]       # 离线兜底模型列表（短名）
    api_key: str = ""       # 用户填入的 key
    model_list_path: str = "models"  # 支持 OpenAI 兼容 /models 时填入


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
        model_list_path="https://api.anthropic.com/v1/models",
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


def _model_list_url(provider: ProviderInfo, *, after_id: str | None = None) -> str:
    """构造厂商模型列表接口 URL。"""
    path = provider.model_list_path or "models"
    if path.startswith(("http://", "https://")):
        url = path
    else:
        url = f"{provider.base_url.rstrip('/')}/{path.lstrip('/')}"

    if after_id:
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}{urlencode({'after_id': after_id})}"
    return url


def _model_list_headers(provider: ProviderInfo, api_key: str) -> dict[str, str]:
    """返回模型列表接口所需认证头。"""
    headers = {"Accept": "application/json"}
    if provider.key == "anthropic":
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
    else:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _created_score(item: Any) -> float:
    """返回模型创建时间分数，用于把厂商最新模型排在前面。"""
    if not isinstance(item, dict):
        return 0.0

    created = item.get("created")
    if isinstance(created, int | float):
        return float(created)

    created_at = item.get("created_at")
    if isinstance(created_at, str):
        try:
            normalized = created_at.replace("Z", "+00:00")
            return datetime.fromisoformat(normalized).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def _extract_model_items(payload: Any) -> list[Any]:
    """兼容 OpenAI/Anthropic/OpenAI-compatible 的模型列表响应。"""
    if isinstance(payload, dict):
        for key in ("data", "models"):
            items = payload.get(key)
            if isinstance(items, list):
                return items
    if isinstance(payload, list):
        return payload
    return []


def _model_id_from_item(item: Any) -> str | None:
    if isinstance(item, str):
        return item
    if not isinstance(item, dict):
        return None
    for key in ("id", "name", "model"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _parse_model_payload(payload: Any) -> list[str]:
    """从接口响应中解析并去重模型名。"""
    model_rows: list[tuple[str, float, int]] = []
    seen: set[str] = set()
    for index, item in enumerate(_extract_model_items(payload)):
        model = _model_id_from_item(item)
        if model and model not in seen:
            model_rows.append((model, _created_score(item), index))
            seen.add(model)

    if any(created for _, created, _ in model_rows):
        model_rows.sort(key=lambda row: (-row[1], row[2], row[0]))

    return [model for model, _, _ in model_rows]


def fetch_provider_models(provider: ProviderInfo, api_key: str) -> list[str]:
    """从厂商模型列表接口实时获取模型短名；失败时返回空列表。"""
    if not api_key:
        return []

    models: list[str] = []
    seen: set[str] = set()
    after_id: str | None = None
    try:
        while True:
            response = httpx.get(
                _model_list_url(provider, after_id=after_id),
                headers=_model_list_headers(provider, api_key),
                timeout=MODEL_LIST_TIMEOUT,
            )
            response.raise_for_status()
            payload = response.json()
            for model in _parse_model_payload(payload):
                if model not in seen:
                    models.append(model)
                    seen.add(model)

            if not (isinstance(payload, dict) and payload.get("has_more") and payload.get("last_id")):
                break
            after_id = str(payload["last_id"])
    except Exception as e:
        logger.debug("获取 %s 实时模型列表失败: %s", provider.key, e)
        return []

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
    """获取已配置 API Key 的厂商列表。

    refresh_models=True 时只使用厂商实时接口返回的模型，避免把内置兜底列表
    误展示为最新模型；refresh_models=False 时才返回内置示例列表。
    """
    creds = load_credentials()
    configured = []
    for key, info in PROVIDERS.items():
        if key in creds and creds[key]:
            if refresh_models:
                models = fetch_provider_models(info, creds[key])
            else:
                models = info.models
            info_copy = ProviderInfo(
                name=info.name, key=info.key, base_url=info.base_url,
                env_key=info.env_key, models=models, api_key=creds[key],
                model_list_path=info.model_list_path,
            )
            configured.append(info_copy)
    return configured
