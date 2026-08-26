"""测试模型缓存功能。"""

import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from xenon.repl.provider_registry import (
    MODEL_CACHE_TTL,
    ProviderInfo,
    _get_cache_path,
    _hash_api_key,
    _hash_base_url,
    _is_cache_valid,
    _load_model_cache,
    _save_model_cache,
    _update_provider_cache,
    clear_model_cache,
    fetch_provider_models,
    get_configured_providers,
)


@pytest.fixture
def temp_cache_dir(tmp_path):
    """创建临时缓存目录。"""
    cache_dir = tmp_path / ".xenon" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    with patch("xenon.repl.provider_registry._get_cache_path") as mock_path:
        mock_path.return_value = cache_dir / "provider_models.json"
        yield cache_dir


def test_hash_api_key():
    """测试 API Key 哈希生成。"""
    key1 = "test-key-123"
    key2 = "test-key-456"
    hash1 = _hash_api_key(key1)
    hash2 = _hash_api_key(key2)

    assert len(hash1) == 16
    assert len(hash2) == 16
    assert hash1 != hash2
    assert _hash_api_key(key1) == hash1  # 一致性


def test_hash_base_url():
    """测试 base_url 哈希生成。"""
    url1 = "https://api.example.com/v1"
    url2 = "https://api.example.com/v2"
    hash1 = _hash_base_url(url1)
    hash2 = _hash_base_url(url2)

    assert len(hash1) == 16
    assert len(hash2) == 16
    assert hash1 != hash2
    assert _hash_base_url(url1) == hash1  # 一致性


def test_load_empty_cache(temp_cache_dir):
    """测试加载空缓存。"""
    cache = _load_model_cache()
    assert cache == {}


def test_save_and_load_cache(temp_cache_dir):
    """测试保存和加载缓存。"""
    test_cache = {
        "openai": {
            "models": ["gpt-4", "gpt-3.5"],
            "fetched_at": time.time(),
            "api_key_hash": "abc123",
            "base_url_hash": "def456",
        }
    }

    _save_model_cache(test_cache)
    loaded = _load_model_cache()

    assert loaded == test_cache
    assert loaded["openai"]["models"] == ["gpt-4", "gpt-3.5"]


def test_cache_expiration_cleanup(temp_cache_dir):
    """测试缓存过期清理（2倍TTL）。"""
    old_time = time.time() - (MODEL_CACHE_TTL * 2 + 100)
    recent_time = time.time() - 100

    test_cache = {
        "old_provider": {
            "models": ["old-model"],
            "fetched_at": old_time,
            "api_key_hash": "old",
            "base_url_hash": "old",
        },
        "recent_provider": {
            "models": ["new-model"],
            "fetched_at": recent_time,
            "api_key_hash": "new",
            "base_url_hash": "new",
        },
    }

    _save_model_cache(test_cache)
    loaded = _load_model_cache()

    # 过期的条目应该被清理
    assert "old_provider" not in loaded
    assert "recent_provider" in loaded


def test_is_cache_valid():
    """测试缓存有效性检查。"""
    current_time = time.time()
    api_key = "test-key"
    base_url = "https://api.example.com"

    # 有效的缓存
    valid_entry = {
        "models": ["model1", "model2"],
        "fetched_at": current_time - 100,
        "api_key_hash": _hash_api_key(api_key),
        "base_url_hash": _hash_base_url(base_url),
    }
    assert _is_cache_valid(valid_entry, api_key, base_url, current_time)

    # 过期的缓存
    expired_entry = {
        "models": ["model1"],
        "fetched_at": current_time - MODEL_CACHE_TTL - 100,
        "api_key_hash": _hash_api_key(api_key),
        "base_url_hash": _hash_base_url(base_url),
    }
    assert not _is_cache_valid(expired_entry, api_key, base_url, current_time)

    # API Key 变化
    wrong_key_entry = {
        "models": ["model1"],
        "fetched_at": current_time - 100,
        "api_key_hash": _hash_api_key("wrong-key"),
        "base_url_hash": _hash_base_url(base_url),
    }
    assert not _is_cache_valid(wrong_key_entry, api_key, base_url, current_time)

    # base_url 变化
    wrong_url_entry = {
        "models": ["model1"],
        "fetched_at": current_time - 100,
        "api_key_hash": _hash_api_key(api_key),
        "base_url_hash": _hash_base_url("https://wrong.url"),
    }
    assert not _is_cache_valid(wrong_url_entry, api_key, base_url, current_time)

    # 空模型列表
    empty_models_entry = {
        "models": [],
        "fetched_at": current_time - 100,
        "api_key_hash": _hash_api_key(api_key),
        "base_url_hash": _hash_base_url(base_url),
    }
    assert not _is_cache_valid(empty_models_entry, api_key, base_url, current_time)


def test_update_provider_cache_thread_safe(temp_cache_dir):
    """测试线程安全的缓存更新。"""
    # 先创建初始缓存
    initial_cache = {
        "provider1": {
            "models": ["model1"],
            "fetched_at": time.time(),
            "api_key_hash": "hash1",
            "base_url_hash": "url1",
        }
    }
    _save_model_cache(initial_cache)

    # 更新单个 provider
    new_entry = {
        "models": ["model2", "model3"],
        "fetched_at": time.time(),
        "api_key_hash": "hash2",
        "base_url_hash": "url2",
    }
    _update_provider_cache("provider2", new_entry)

    # 验证两个 provider 都存在
    loaded = _load_model_cache()
    assert "provider1" in loaded
    assert "provider2" in loaded
    assert loaded["provider2"]["models"] == ["model2", "model3"]


def test_clear_model_cache(temp_cache_dir):
    """测试清除缓存。"""
    test_cache = {
        "provider1": {"models": ["model1"], "fetched_at": time.time()},
        "provider2": {"models": ["model2"], "fetched_at": time.time()},
    }
    _save_model_cache(test_cache)

    # 清除特定 provider
    clear_model_cache("provider1")
    loaded = _load_model_cache()
    assert "provider1" not in loaded
    assert "provider2" in loaded

    # 清除所有
    clear_model_cache(None)
    loaded = _load_model_cache()
    assert loaded == {}


@patch("xenon.repl.provider_registry._fetch_provider_models_from_network")
def test_fetch_provider_models_uses_cache(mock_fetch, temp_cache_dir):
    """测试 fetch_provider_models 使用缓存。"""
    provider = ProviderInfo(
        name="Test",
        key="test",
        base_url="https://api.test.com",
        env_key="TEST_API_KEY",
        models=["fallback-model"],
    )
    api_key = "test-key-123"

    # 第一次调用，缓存未命中，应该调用网络
    mock_fetch.return_value = ["model1", "model2"]
    models1 = fetch_provider_models(provider, api_key, use_cache=True)
    assert models1 == ["model1", "model2"]
    assert mock_fetch.call_count == 1

    # 第二次调用，缓存命中，不应该调用网络
    mock_fetch.reset_mock()
    models2 = fetch_provider_models(provider, api_key, use_cache=True)
    assert models2 == ["model1", "model2"]
    assert mock_fetch.call_count == 0  # 未调用网络


@patch("xenon.repl.provider_registry._fetch_provider_models_from_network")
def test_fetch_provider_models_cache_invalidation(mock_fetch, temp_cache_dir):
    """测试缓存失效场景。"""
    provider = ProviderInfo(
        name="Test",
        key="test",
        base_url="https://api.test.com",
        env_key="TEST_API_KEY",
        models=["fallback-model"],
    )

    # 第一次使用 api_key1
    mock_fetch.return_value = ["model1"]
    models1 = fetch_provider_models(provider, "api-key-1", use_cache=True)
    assert models1 == ["model1"]

    # API Key 变化，缓存应该失效
    mock_fetch.reset_mock()
    mock_fetch.return_value = ["model2"]
    models2 = fetch_provider_models(provider, "api-key-2", use_cache=True)
    assert models2 == ["model2"]
    assert mock_fetch.call_count == 1  # 应该重新请求网络


@patch("xenon.repl.provider_registry._fetch_provider_models_from_network")
def test_fetch_provider_models_base_url_change(mock_fetch, temp_cache_dir):
    """测试 base_url 变化导致缓存失效。"""
    api_key = "test-key"

    # 第一次使用 url1
    provider1 = ProviderInfo(
        name="Test",
        key="test",
        base_url="https://api.test1.com",
        env_key="TEST_API_KEY",
        models=["fallback"],
    )
    mock_fetch.return_value = ["model1"]
    models1 = fetch_provider_models(provider1, api_key, use_cache=True)
    assert models1 == ["model1"]

    # base_url 变化，缓存应该失效
    provider2 = ProviderInfo(
        name="Test",
        key="test",
        base_url="https://api.test2.com",
        env_key="TEST_API_KEY",
        models=["fallback"],
    )
    mock_fetch.reset_mock()
    mock_fetch.return_value = ["model2"]
    models2 = fetch_provider_models(provider2, api_key, use_cache=True)
    assert models2 == ["model2"]
    assert mock_fetch.call_count == 1  # 应该重新请求网络
