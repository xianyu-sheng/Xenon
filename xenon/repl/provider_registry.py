"""
Provider Registry — 预设厂商信息库。

所有主流大模型厂商的 base_url 已预设。配置 API Key 后，模型列表会优先
从厂商接口实时拉取；内置列表只作为离线兜底。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
import yaml

from xenon.utils.atomic_write import atomic_write_text
from xenon.utils.llm_client import _create_http_client

CREDENTIALS_PATH = Path.home() / ".xenon" / "credentials.yaml"
MODEL_LIST_TIMEOUT = 8.0

logger = logging.getLogger(__name__)
MODEL_FETCH_ERRORS: dict[str, str] = {}


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
    MODEL_FETCH_ERRORS.pop(provider.key, None)
    if not api_key:
        MODEL_FETCH_ERRORS[provider.key] = "API Key 为空"
        return []

    models: list[str] = []
    seen: set[str] = set()
    after_id: str | None = None
    try:
        while True:
            with _create_http_client(timeout=MODEL_LIST_TIMEOUT) as client:
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
                for model in _parse_model_payload(payload):
                    if model not in seen:
                        models.append(model)
                        seen.add(model)

                if not (isinstance(payload, dict) and payload.get("has_more") and payload.get("last_id")):
                    break
                after_id = str(payload["last_id"])
    except Exception as e:
        MODEL_FETCH_ERRORS[provider.key] = f"{e.__class__.__name__}: {e}"
        logger.debug("获取 %s 实时模型列表失败: %s", provider.key, MODEL_FETCH_ERRORS[provider.key])
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
    content = yaml.dump(creds, allow_unicode=True, default_flow_style=False)
    atomic_write_text(CREDENTIALS_PATH, content, mode=0o600)  # A9 原子写 + A10 chmod 0600
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

    v0.3.0+ 修复（C-2）：API Key 解析顺序为
    1) ~/.xenon/credentials.yaml（最高优先级）
    2) 环境变量 info.env_key
    3) anthropic 厂商额外 fallback ANTHROPIC_AUTH_TOKEN（Claude Code / Anthropic SDK 标准）
    """
    creds = load_credentials()
    configured = []
    for key, info in PROVIDERS.items():
        api_key = _resolve_api_key(key, info, creds)
        if not api_key:
            continue
        if refresh_models:
            models = fetch_provider_models(info, api_key)
            if models:
                # v0.3.0+ 修复（B-3）：拉取的列表按内置 info.models 顺序重排
                # 通用机制：内置"已知能力排序"（如 deepseek-v4-pro > v4-flash）
                # 优先于外部 API 返回顺序——外部 API 顺序由服务器决定
                # 不可控。保持拉取列表里**内置未列出**的模型原顺序追加。
                models = _sort_models_by_priority(models, info.models)
        else:
            models = info.models
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
        if not api_key:
            continue
        # v0.5.2: 修补空 key（纯中文名称register时key为空 → model_id变成 /model）
        if not key or not key.strip():
            key = "custom"
        info_copy = ProviderInfo(
            name=cfg.get("name", key),
            key=key,
            base_url=cfg.get("base_url", ""),
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
    if creds.get(provider_key):
        return creds[provider_key]
    # 2) 标准环境变量
    val = os.getenv(info.env_key)
    if val:
        return val
    # 3) anthropic 厂商额外 fallback：Claude Code / SDK 用 ANTHROPIC_AUTH_TOKEN
    if provider_key == "anthropic":
        val = os.getenv("ANTHROPIC_AUTH_TOKEN")
        if val:
            return val
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
    creds = load_credentials()
    creds[_CUSTOM_PROVIDERS_KEY] = all_custom
    save_credentials(creds)
    return True


def _load_custom_providers() -> dict:
    """从 credentials.yaml 加载自定义模型商。"""
    if not CREDENTIALS_PATH.exists():
        return {}
    with open(CREDENTIALS_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get(_CUSTOM_PROVIDERS_KEY, {}) or {}


def _save_custom_provider(info: ProviderInfo) -> None:
    """持久化自定义模型商。"""
    all_custom = _load_custom_providers()
    all_custom[info.key] = {
        "name": info.name, "base_url": info.base_url,
        "api_key": info.api_key, "models": info.models,
    }
    creds = load_credentials()
    creds[_CUSTOM_PROVIDERS_KEY] = all_custom
    save_credentials(creds)


# Public alias
list_custom_providers = _load_custom_providers


# ── v0.5.3: MCP 服务器持久化 ─────────────────────────────────
_MCP_SERVERS_KEY = "_mcp_servers"


def load_mcp_servers() -> list[dict[str, object]]:
    """从 credentials.yaml 加载已持久化的 MCP 服务器配置。

    Returns:
        [{"name": "12306", "command": "npx", "args": ["-y", "12306-mcp"]},
         {"name": "web", "url": "http://localhost:3000/sse"}, ...]
    """
    if not CREDENTIALS_PATH.exists():
        return []
    with open(CREDENTIALS_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    servers = data.get(_MCP_SERVERS_KEY, [])
    if not isinstance(servers, list):
        return []
    return servers


def save_mcp_server(name: str, *, command: str = "", args: list[str] | None = None, url: str = "") -> None:
    """持久化一个 MCP 服务器配置（新增或更新同名配置）。"""
    servers = load_mcp_servers()
    # 移除同名旧配置
    servers = [s for s in servers if s.get("name") != name]
    entry: dict[str, object] = {"name": name}
    if url:
        entry["url"] = url
    else:
        entry["command"] = command
        entry["args"] = args or []
    servers.append(entry)

    creds = load_credentials()
    creds[_MCP_SERVERS_KEY] = servers
    save_credentials(creds)


def remove_mcp_server(name: str) -> bool:
    """从持久化配置中移除一个 MCP 服务器。返回是否成功移除。"""
    servers = load_mcp_servers()
    new_servers = [s for s in servers if s.get("name") != name]
    if len(new_servers) == len(servers):
        return False
    creds = load_credentials()
    creds[_MCP_SERVERS_KEY] = new_servers
    save_credentials(creds)
    return True
