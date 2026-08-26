"""
Provider Registry — 预设厂商信息库。

所有主流大模型厂商的 base_url 已预设。配置 API Key 后，模型列表会优先
从厂商接口实时拉取；内置列表只作为离线兜底。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx
import yaml

from xenon.utils.atomic_write import atomic_write_text
from xenon.utils.llm_client import _create_http_client
from xenon.repl.system_config import get_config

def _get_credentials_path() -> Path:
    """获取凭证文件路径（支持配置文件和环境变量）。

    评测/沙箱可把持久化配置指向隔离文件；普通用户行为保持不变。每次调用都重新
    解析，因为配置来源本身是活的：环境变量可以在进程运行中改，config.yaml 也
    可以被编辑。
    """
    return Path(get_config().paths.credentials).expanduser()


# 向后兼容别名：导入时求值一次的快照。新代码请调用 ``_get_credentials_path()``。
CREDENTIALS_PATH = _get_credentials_path()
MODEL_LIST_TIMEOUT = 8.0
MODEL_CACHE_TTL = 3600  # 1 hour cache TTL

logger = logging.getLogger(__name__)
MODEL_FETCH_ERRORS: dict[str, str] = {}
_cache_lock = threading.Lock()  # Thread-safe cache operations

_DEEPSEEK_RETIRED_MODEL_NAMES = frozenset({
    "deepseek-chat",
    "deepseek-reasoner",
    "deepseek-coder",
})

ARK_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
ARK_FALLBACK_MODELS = [
    "doubao-seed-2-1-pro-260628",
    "glm-5-2-260617",
    "deepseek-v4-pro-260425",
    "deepseek-v4-flash-260425",
]
MODEL_METADATA: dict[tuple[str, str], dict[str, Any]] = {}


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
    model_error: str = ""    # 实时模型列表获取失败原因


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
        models=["deepseek-v4-pro", "deepseek-v4-flash"],
        model_list_path="https://api.deepseek.com/models",
    ),
    "ark": ProviderInfo(
        name="火山方舟 Ark",
        key="ark",
        base_url=ARK_BASE_URL,
        env_key="ARK_API_KEY",
        models=list(ARK_FALLBACK_MODELS),
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
    例: "deepseek-v4-pro" -> "deepseek/deepseek-v4-pro"
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


def _is_ark_chat_model(item: Any) -> bool:
    """Reject Ark image/video/embedding models from the chat model pool."""
    if not isinstance(item, dict):
        return True
    task_types = item.get("task_type")
    if isinstance(task_types, list) and task_types:
        return "TextGeneration" in task_types
    modalities = item.get("modalities")
    if isinstance(modalities, dict):
        outputs = modalities.get("output_modalities")
        if isinstance(outputs, list) and outputs:
            return "text" in outputs
    # Older compatible endpoints may omit capability metadata. Preserve those
    # entries rather than turning a schema evolution into an empty model list.
    return True


def _remember_model_metadata(provider: ProviderInfo, items: list[Any]) -> None:
    """Keep non-secret model capability metadata discovered in this process."""
    for item in items:
        model = _model_id_from_item(item)
        if not model or not isinstance(item, dict):
            continue
        token_limits = item.get("token_limits")
        features = item.get("features")
        MODEL_METADATA[(provider.key, model)] = {
            "context_window": (
                int(token_limits.get("context_window", 0) or 0)
                if isinstance(token_limits, dict)
                else 0
            ),
            "max_output_tokens": (
                int(token_limits.get("max_output_token_length", 0) or 0)
                if isinstance(token_limits, dict)
                else 0
            ),
            "features": dict(features) if isinstance(features, dict) else {},
        }


def get_model_metadata(model_id: str) -> dict[str, Any]:
    """Return capability metadata captured by the latest model discovery."""
    if "/" not in model_id:
        return {}
    provider, model = model_id.split("/", 1)
    return dict(MODEL_METADATA.get((provider.lower(), model), {}))


# ── 模型缓存管理 ──────────────────────────────────────────────

def _get_cache_path() -> Path:
    """获取模型缓存文件路径。"""
    cache_dir = Path.home() / ".xenon" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / "provider_models.json"


def _hash_api_key(api_key: str) -> str:
    """生成 API Key 的 SHA256 哈希值用于检测变化。"""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


def _hash_base_url(base_url: str) -> str:
    """生成 base_url 的 SHA256 哈希值用于检测变化。"""
    return hashlib.sha256(base_url.encode("utf-8")).hexdigest()[:16]


def _load_model_cache() -> dict[str, Any]:
    """从磁盘加载模型缓存，并清理过期条目。"""
    cache_path = _get_cache_path()
    if not cache_path.exists():
        return {}
    try:
        with open(cache_path, encoding="utf-8") as f:
            cache = json.load(f)
            if not isinstance(cache, dict):
                return {}

            # 清理过期 2 倍 TTL 的条目，防止缓存文件无限膨胀
            current_time = time.time()
            cleaned_cache = {}
            for key, entry in cache.items():
                if isinstance(entry, dict):
                    fetched_at = entry.get("fetched_at", 0)
                    if current_time - fetched_at <= MODEL_CACHE_TTL * 2:
                        cleaned_cache[key] = entry

            # 如果清理后有变化，异步写回（不阻塞当前加载）
            if len(cleaned_cache) != len(cache):
                logger.debug("清理了 %d 个过期缓存条目", len(cache) - len(cleaned_cache))

            return cleaned_cache
    except Exception as e:
        logger.debug("加载模型缓存失败: %s", e)
        return {}


def _save_model_cache(cache: dict[str, Any]) -> None:
    """保存模型缓存到磁盘（线程安全）。"""
    cache_path = _get_cache_path()
    try:
        content = json.dumps(cache, ensure_ascii=False, indent=2)
        atomic_write_text(cache_path, content)
    except Exception as e:
        logger.debug("保存模型缓存失败: %s", e)


def _update_provider_cache(provider_key: str, cache_entry: dict[str, Any]) -> None:
    """更新单个 provider 的缓存条目（线程安全）。"""
    with _cache_lock:
        cache = _load_model_cache()
        cache[provider_key] = cache_entry
        _save_model_cache(cache)


def _is_cache_valid(
    cache_entry: dict[str, Any],
    api_key: str,
    base_url: str,
    current_time: float,
) -> bool:
    """检查缓存条目是否有效（未过期且 API Key/base_url 未变）。"""
    if not isinstance(cache_entry, dict):
        return False

    # 检查必要字段
    if "models" not in cache_entry or "fetched_at" not in cache_entry:
        return False

    # 检查过期时间
    fetched_at = cache_entry.get("fetched_at", 0)
    if current_time - fetched_at > MODEL_CACHE_TTL:
        return False

    # 检查 API Key 是否变化
    cached_hash = cache_entry.get("api_key_hash", "")
    current_hash = _hash_api_key(api_key)
    if cached_hash != current_hash:
        return False

    # 检查 base_url 是否变化
    cached_base_url_hash = cache_entry.get("base_url_hash", "")
    current_base_url_hash = _hash_base_url(base_url)
    if cached_base_url_hash != current_base_url_hash:
        return False

    # 检查模型列表是否为空或无效
    models = cache_entry.get("models", [])
    if not isinstance(models, list) or not models:
        return False

    return True


def clear_model_cache(provider_key: str | None = None) -> None:
    """清除模型缓存。

    Args:
        provider_key: 指定厂商 key，为 None 时清除所有缓存
    """
    if provider_key is None:
        # 清除所有缓存
        cache_path = _get_cache_path()
        if cache_path.exists():
            try:
                cache_path.unlink()
                logger.info("已清除所有模型缓存")
            except Exception as e:
                logger.debug("清除模型缓存失败: %s", e)
    else:
        # 清除指定厂商的缓存
        cache = _load_model_cache()
        if provider_key in cache:
            del cache[provider_key]
            _save_model_cache(cache)
            logger.info("已清除 %s 的模型缓存", provider_key)


def fetch_provider_models(provider: ProviderInfo, api_key: str, *, use_cache: bool = True) -> list[str]:
    """从厂商模型列表接口实时获取模型短名；失败时返回空列表。

    Args:
        provider: 厂商信息
        api_key: API Key
        use_cache: 是否使用缓存（默认 True）

    Returns:
        模型列表（短名）
    """
    MODEL_FETCH_ERRORS.pop(provider.key, None)
    if not api_key:
        MODEL_FETCH_ERRORS[provider.key] = "API Key 为空"
        return []

    # 尝试从缓存加载
    if use_cache:
        cache = _load_model_cache()
        cache_entry = cache.get(provider.key, {})
        current_time = time.time()

        if _is_cache_valid(cache_entry, api_key, provider.base_url, current_time):
            logger.debug("使用缓存的 %s 模型列表", provider.key)
            return cache_entry["models"]

    # 缓存未命中或无效，从网络获取
    models = _fetch_provider_models_from_network(provider, api_key)

    # 更新缓存（线程安全）
    if models and use_cache:
        cache_entry = {
            "models": models,
            "fetched_at": time.time(),
            "api_key_hash": _hash_api_key(api_key),
            "base_url_hash": _hash_base_url(provider.base_url),
        }
        _update_provider_cache(provider.key, cache_entry)

    return models


def _fetch_provider_models_from_network(provider: ProviderInfo, api_key: str) -> list[str]:
    """从网络获取厂商模型列表（不使用缓存）。"""
    MODEL_FETCH_ERRORS.pop(provider.key, None)
    models: list[str] = []
    seen: set[str] = set()
    after_id: str | None = None
    for key in [key for key in MODEL_METADATA if key[0] == provider.key]:
        MODEL_METADATA.pop(key, None)
    try:
        # Keep one connection alive across paginated model directories. Ark's
        # catalog is large enough that recreating TLS/proxy state per page can
        # turn startup discovery into a minute-long operation.
        with _create_http_client(timeout=MODEL_LIST_TIMEOUT) as client:
            while True:
                response = client.get(
                    _model_list_url(provider, after_id=after_id),
                    headers=_model_list_headers(provider, api_key),
                )
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as e:
                    body = e.response.text.strip().replace("\n", " ")
                    detail = body[:160] if body else e.response.reason_phrase
                    MODEL_FETCH_ERRORS[provider.key] = f"HTTP {e.response.status_code}: {detail}"
                    logger.debug("获取 %s 实时模型列表失败: %s", provider.key, MODEL_FETCH_ERRORS[provider.key])
                    return []
                payload = response.json()
                items = _extract_model_items(payload)
                if provider.key == "ark":
                    items = [item for item in items if _is_ark_chat_model(item)]
                _remember_model_metadata(provider, items)
                for model in _parse_model_payload(items):
                    if model not in seen:
                        models.append(model)
                        seen.add(model)

                if not (
                    isinstance(payload, dict)
                    and payload.get("has_more")
                    and payload.get("last_id")
                ):
                    break
                after_id = str(payload["last_id"])
    except Exception as e:
        MODEL_FETCH_ERRORS[provider.key] = f"{e.__class__.__name__}: {e}"
        logger.debug("获取 %s 实时模型列表失败: %s", provider.key, MODEL_FETCH_ERRORS[provider.key])
        return []

    return models


# ── 凭证管理 ──────────────────────────────────────────────

def load_credentials(path: Path | None = None) -> dict[str, Any]:
    """从文件加载凭证，并兼容旧版 custom Ark 配置。

    旧版 Xenon 只能把方舟注册为 ``_custom_providers``。若其中恰好只有一个
    官方 Ark 数据面地址，运行时将它映射为一等 ``ark`` provider；这里只返回
    兼容视图，不会静默改写用户文件。
    """
    credentials_path = path or _get_credentials_path()
    creds = _read_credentials(credentials_path)
    if not creds.get("ark"):
        legacy_key = _legacy_ark_api_key(creds)
        if legacy_key:
            creds["ark"] = legacy_key
    return creds


def _read_credentials(path: Path | None = None) -> dict[str, Any]:
    """Read the exact on-disk mapping without compatibility projections."""
    credentials_path = path or _get_credentials_path()
    if not credentials_path.exists():
        return {}
    with open(credentials_path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        logger.warning("凭证文件不是 YAML 映射，忽略其内容: %s", credentials_path)
        return {}
    cleaned: dict[str, Any] = {}
    for key, value in data.items():
        normalized_key = str(key).strip().lower()
        if not normalized_key:
            continue
        if isinstance(value, str):
            value = value.strip()
            # 空字符串不是凭证：丢弃它，让环境变量 fallback 正常生效。
            if not value:
                continue
        cleaned[normalized_key] = value
    return cleaned


def _legacy_ark_api_key(data: dict[str, Any]) -> str:
    """Return one unambiguous API key from a legacy custom Ark provider."""
    custom = data.get(_CUSTOM_PROVIDERS_KEY, {})
    if not isinstance(custom, dict):
        return ""
    candidates: list[str] = []
    for config in custom.values():
        if not isinstance(config, dict):
            continue
        try:
            hostname = (urlparse(str(config.get("base_url", ""))).hostname or "").lower()
        except ValueError:
            continue
        api_key = config.get("api_key")
        if hostname == "ark.cn-beijing.volces.com" and isinstance(api_key, str) and api_key.strip():
            candidates.append(api_key.strip())
    unique = list(dict.fromkeys(candidates))
    return unique[0] if len(unique) == 1 else ""


def save_credentials(creds: dict[str, Any], path: Path | None = None) -> Path:
    """保存凭证到文件。"""
    credentials_path = path or _get_credentials_path()
    credentials_path.parent.mkdir(parents=True, exist_ok=True)
    content = yaml.dump(creds, allow_unicode=True, default_flow_style=False)
    atomic_write_text(credentials_path, content, mode=0o600)  # A9 原子写 + A10 chmod 0600
    return credentials_path


def set_provider_key(provider_key: str, api_key: str) -> None:
    """设置某个厂商的 API Key 并保存。"""
    creds = _read_credentials()
    creds[provider_key] = api_key
    save_credentials(creds)


def remove_provider_key(provider_key: str) -> None:
    """移除某个厂商的 API Key。"""
    creds = _read_credentials()
    creds.pop(provider_key, None)
    if provider_key == "ark":
        custom = creds.get(_CUSTOM_PROVIDERS_KEY)
        if isinstance(custom, dict):
            creds[_CUSTOM_PROVIDERS_KEY] = {
                key: config
                for key, config in custom.items()
                if not _is_official_ark_config(config)
            }
    save_credentials(creds)


def _is_official_ark_config(config: Any) -> bool:
    if not isinstance(config, dict):
        return False
    try:
        hostname = (urlparse(str(config.get("base_url", ""))).hostname or "").lower()
    except ValueError:
        return False
    return hostname == "ark.cn-beijing.volces.com"


def get_configured_providers(*, refresh_models: bool = True, use_cache: bool = True) -> list[ProviderInfo]:
    """获取已配置 API Key 的厂商列表。

    Args:
        refresh_models: 是否刷新模型列表（从网络或缓存获取）
        use_cache: 是否使用缓存（仅在 refresh_models=True 时生效）

    refresh_models=True 时只使用厂商实时接口返回的模型，避免把内置兜底列表
    误展示为最新模型；refresh_models=False 时才返回内置示例列表。

    v0.3.0+ 修复（C-2）：API Key 解析顺序为
    1) ~/.xenon/credentials.yaml（最高优先级）
    2) 环境变量 info.env_key
    3) anthropic 厂商额外 fallback ANTHROPIC_AUTH_TOKEN（Claude Code / Anthropic SDK 标准）
    """
    creds = load_credentials()
    configured = []

    # 收集需要获取模型列表的厂商
    providers_to_fetch: list[tuple[str, ProviderInfo, str]] = []

    for key, info in PROVIDERS.items():
        api_key = _resolve_api_key(key, info, creds)
        if not api_key:
            continue

        if refresh_models:
            providers_to_fetch.append((key, info, api_key))
        else:
            # 不刷新时直接使用内置列表
            info_copy = ProviderInfo(
                name=info.name, key=info.key, base_url=info.base_url,
                env_key=info.env_key, models=info.models, api_key=api_key,
                model_list_path=info.model_list_path,
                model_error="",
            )
            configured.append(info_copy)

    # 并行获取模型列表
    if refresh_models and providers_to_fetch:
        # 使用线程池并行获取，并发度为 min(providers数量, CPU核心数*2)
        max_workers = min(len(providers_to_fetch), (os.cpu_count() or 4) * 2)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # 提交所有任务
            future_to_provider = {
                executor.submit(fetch_provider_models, info, api_key, use_cache=use_cache): (key, info, api_key)
                for key, info, api_key in providers_to_fetch
            }

            # 收集结果
            for future in as_completed(future_to_provider):
                key, info, api_key = future_to_provider[future]
                try:
                    models = future.result()
                    if models:
                        if key == "deepseek":
                            models = [
                                model for model in models
                                if model not in _DEEPSEEK_RETIRED_MODEL_NAMES
                            ]
                        # v0.3.0+ 修复（B-3）：拉取的列表按内置 info.models 顺序重排
                        models = _sort_models_by_priority(models, info.models)
                except Exception as e:
                    logger.debug("获取 %s 模型列表时发生异常: %s", key, e)
                    models = []

                info_copy = ProviderInfo(
                    name=info.name, key=info.key, base_url=info.base_url,
                    env_key=info.env_key, models=models, api_key=api_key,
                    model_list_path=info.model_list_path,
                    model_error=MODEL_FETCH_ERRORS.get(key, ""),
                )
                configured.append(info_copy)

    # v0.4.0: 合并自定义模型商
    for key, cfg in _load_custom_providers().items():
        api_key = cfg.get("api_key", "")
        api_key = api_key.strip() if isinstance(api_key, str) else ""
        if not api_key:
            continue
        # v0.5.2: 修补空 key（纯中文名称register时key为空 → model_id变成 /model）
        if not key or not key.strip():
            key = "custom"
        info_copy = ProviderInfo(
            name=cfg.get("name", key),
            key=key,
            base_url=str(cfg.get("base_url", "")).strip(),
            env_key="",
            models=cfg.get("models", []),
            api_key=api_key,
            model_list_path="models",
            model_error="",
        )
        configured.append(info_copy)

    return configured


def _resolve_api_key(
    provider_key: str, info: ProviderInfo, creds: dict[str, str]
) -> str:
    """解析厂商 API Key。

    v0.3.0+ 修复（C-2）：之前只从 ~/.xenon/credentials.yaml 读 env_key 字段
    形同虚设——Claude Code / Anthropic SDK 内设 ANTHROPIC_AUTH_TOKEN 的用户完全
    无法使用 xenon。现在支持 yaml → env_key → anthropic 特殊 fallback。
    """
    # 1) yaml 配置优先（用户明确指定的最优先）
    yaml_value = creds.get(provider_key)
    if isinstance(yaml_value, str) and yaml_value.strip():
        return yaml_value.strip()
    # 2) 标准环境变量
    val = os.getenv(info.env_key)
    if isinstance(val, str) and val.strip():
        return val.strip()
    # 3) anthropic 厂商额外 fallback：Claude Code / SDK 用 ANTHROPIC_AUTH_TOKEN
    if provider_key == "anthropic":
        val = os.getenv("ANTHROPIC_AUTH_TOKEN")
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _sort_models_by_priority(
    fetched: list[str], priority: list[str]
) -> list[str]:
    """按内置 priority 列表顺序重排 fetched 列表。

    v0.3.0+ 修复（B-3）：deepseek API 返回的模型列表里
    `deepseek-v4-flash` 在 `deepseek-v4-pro` 之前，但内置 info.models
    里 v4-pro 在前——这导致 REPL 自动加载 `p.models[0]` 时选了 v4-flash
    而非配置的 v4-pro。
    通用机制：内置 priority 决定默认模型选择顺序，拉取列表中未在
    priority 的项保持原顺序追加在末尾。
    """
    p_idx = {m: i for i, m in enumerate(priority)}
    in_priority = [m for m in fetched if m in p_idx]
    not_in_priority = [m for m in fetched if m not in p_idx]
    in_priority.sort(key=lambda m: p_idx[m])
    return in_priority + not_in_priority

# ── 动态模型商注册 (v0.4.0) ──────────────────────────────

_CUSTOM_PROVIDERS_KEY = "_custom_providers"


def register_custom_provider(name: str, base_url: str, api_key: str):
    """动态注册自定义模型商。返回 ProviderInfo。

    v0.4.0: 用户无需等代码更新即可接入任意 OpenAI 兼容 API。
    自定义模型商存入 credentials.yaml 的 _custom_providers 段。
    """
    import re as _re
    key = _re.sub(r"[^a-z0-9]", "", name.lower())[:20]
    # v0.5.2: 纯中文/Unicode 名称会导致 key 为空 → model_id 变 /model 格式
    if not key:
        key = "custom"

    info = ProviderInfo(
        name=name, key=key, base_url=base_url.rstrip("/"),
        env_key="", models=[], api_key=api_key,
        model_list_path="models",
    )
    models = fetch_provider_models(info, api_key)
    if models:
        info.models = models
    else:
        info.models = ["(auto-fetch failed, check base_url and API key)"]

    _save_custom_provider(info)
    return info


def remove_custom_provider(key: str) -> bool:
    """删除自定义模型商。"""
    all_custom = _load_custom_providers()
    if key not in all_custom:
        return False
    del all_custom[key]
    creds = _read_credentials()
    creds[_CUSTOM_PROVIDERS_KEY] = all_custom
    save_credentials(creds)
    return True


def _load_custom_providers() -> dict:
    """从 credentials.yaml 加载自定义模型商。"""
    credentials_path = _get_credentials_path()
    if not credentials_path.exists():
        return {}
    with open(credentials_path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get(_CUSTOM_PROVIDERS_KEY, {}) or {}


def _save_custom_provider(info: ProviderInfo) -> None:
    """持久化自定义模型商。"""
    all_custom = _load_custom_providers()
    all_custom[info.key] = {
        "name": info.name, "base_url": info.base_url,
        "api_key": info.api_key, "models": info.models,
    }
    creds = _read_credentials()
    creds[_CUSTOM_PROVIDERS_KEY] = all_custom
    save_credentials(creds)


# Public alias
list_custom_providers = _load_custom_providers


# ── v0.5.3: MCP 服务器持久化 ─────────────────────────────────
_MCP_SERVERS_KEY = "_mcp_servers"


def load_mcp_servers(path: Path | None = None) -> list[dict[str, object]]:
    """从 credentials.yaml 加载已持久化的 MCP 服务器配置。

    Returns:
        [{"name": "12306", "command": "npx", "args": ["-y", "12306-mcp"]},
         {"name": "web", "url": "http://localhost:3000/sse"}, ...]
    """
    credentials_path = path or _get_credentials_path()
    if not credentials_path.exists():
        return []
    with open(credentials_path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    servers = data.get(_MCP_SERVERS_KEY, [])
    if not isinstance(servers, list):
        return []
    return servers


def save_mcp_server(
    name: str,
    *,
    command: str = "",
    args: list[str] | None = None,
    url: str = "",
    env: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    path: Path | None = None,
) -> None:
    """持久化一个 MCP 服务器配置（新增或更新同名配置）。"""
    from xenon.memory.locking import InterProcessFileLock

    credentials_path = path or _get_credentials_path()
    with InterProcessFileLock(credentials_path.with_suffix(".lock")):
        servers = load_mcp_servers(credentials_path)
        servers = [s for s in servers if s.get("name") != name]
        entry: dict[str, object] = {"name": name}
        if url:
            entry["url"] = url
            if headers:
                entry["headers"] = headers
        else:
            entry["command"] = command
            entry["args"] = args or []
            if env:
                entry["env"] = env
        servers.append(entry)

        creds = _read_credentials(credentials_path)
        creds[_MCP_SERVERS_KEY] = servers
        save_credentials(creds, credentials_path)


def remove_mcp_server(name: str, *, path: Path | None = None) -> bool:
    """从持久化配置中移除一个 MCP 服务器。返回是否成功移除。"""
    from xenon.memory.locking import InterProcessFileLock

    credentials_path = path or _get_credentials_path()
    with InterProcessFileLock(credentials_path.with_suffix(".lock")):
        servers = load_mcp_servers(credentials_path)
        new_servers = [s for s in servers if s.get("name") != name]
        if len(new_servers) == len(servers):
            return False
        creds = _read_credentials(credentials_path)
        creds[_MCP_SERVERS_KEY] = new_servers
        save_credentials(creds, credentials_path)
        return True
