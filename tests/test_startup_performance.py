"""测试启动性能优化 - 缓存、并行请求、过期逻辑、API Key 变化。"""

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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


def mock_network_delay(*args, **kwargs):
    """模拟网络延迟（每个请求 0.5 秒）。"""
    time.sleep(0.5)
    return ["model1", "model2", "model3"]


@pytest.fixture
def temp_cache_dir(tmp_path):
    """创建临时缓存目录。"""
    cache_dir = tmp_path / ".xenon" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    with patch("xenon.repl.provider_registry._get_cache_path") as mock_path:
        mock_path.return_value = cache_dir / "provider_models.json"
        yield cache_dir


@pytest.fixture
def mock_credentials():
    """模拟配置的 credentials。"""
    with patch("xenon.repl.provider_registry.load_credentials") as mock_creds, \
         patch("xenon.repl.provider_registry._load_custom_providers") as mock_custom:
        # 模拟配置了 3 个 provider
        mock_creds.return_value = {
            "openai": "test-openai-key",
            "anthropic": "test-anthropic-key",
            "deepseek": "test-deepseek-key",
        }
        # 没有自定义 provider
        mock_custom.return_value = {}
        yield mock_creds


@patch("xenon.repl.provider_registry._fetch_provider_models_from_network")
def test_parallel_fetching_faster_than_serial(
    mock_fetch, temp_cache_dir, mock_credentials
):
    """测试并行获取比串行获取快。"""
    mock_fetch.side_effect = mock_network_delay

    # 清除缓存，强制网络请求
    start_time = time.time()
    providers = get_configured_providers(refresh_models=True, use_cache=False)
    elapsed = time.time() - start_time

    # 3 个 provider，每个 0.5 秒
    # 串行：3 * 0.5 = 1.5 秒
    # 并行：max(0.5, 0.5, 0.5) ≈ 0.5-0.6 秒
    assert elapsed < 1.0, f"并行请求耗时 {elapsed:.2f}s，应该 <1.0s"
    assert len(providers) == 3

    # 验证确实发起了 3 次网络请求
    assert mock_fetch.call_count == 3


@patch("xenon.repl.provider_registry._fetch_provider_models_from_network")
def test_cached_startup_is_fast(mock_fetch, temp_cache_dir, mock_credentials):
    """测试缓存启动非常快。"""
    mock_fetch.side_effect = mock_network_delay

    # 第一次启动：冷启动，会请求网络
    start_time = time.time()
    providers1 = get_configured_providers(refresh_models=True, use_cache=True)
    cold_start_time = time.time() - start_time

    assert len(providers1) == 3
    assert mock_fetch.call_count == 3

    # 第二次启动：热启动，使用缓存
    mock_fetch.reset_mock()
    start_time = time.time()
    providers2 = get_configured_providers(refresh_models=True, use_cache=True)
    warm_start_time = time.time() - start_time

    assert len(providers2) == 3
    assert mock_fetch.call_count == 0  # 没有网络请求

    # 热启动应该非常快（<100ms）
    assert warm_start_time < 0.1, f"缓存启动耗时 {warm_start_time:.3f}s，应该 <0.1s"

    # 热启动应该比冷启动快至少 5 倍
    speedup = cold_start_time / warm_start_time
    assert speedup > 5, f"提速比 {speedup:.1f}x，应该 >5x"


@patch("xenon.repl.provider_registry._fetch_provider_models_from_network")
def test_optimal_concurrency_level(mock_fetch, temp_cache_dir, mock_credentials):
    """测试并发度优化（使用 CPU 核心数 * 2）。"""
    import os

    mock_fetch.side_effect = mock_network_delay

    # 记录并发调用的时间点
    call_times = []

    def track_calls(*args, **kwargs):
        call_times.append(time.time())
        time.sleep(0.5)
        return ["model1", "model2"]

    mock_fetch.side_effect = track_calls

    start_time = time.time()
    providers = get_configured_providers(refresh_models=True, use_cache=False)
    elapsed = time.time() - start_time

    # 验证所有调用基本同时发生（并行）
    if len(call_times) > 1:
        time_spread = max(call_times) - min(call_times)
        # 并行调用应该在很短时间内都启动（<0.1秒）
        assert time_spread < 0.1, f"调用时间跨度 {time_spread:.3f}s，不够并行"

    # 总耗时应该接近单个请求时间（并行效果）
    assert elapsed < 1.0, f"并行请求耗时 {elapsed:.2f}s，应该 <1.0s"


class TestCacheReadWriteCorrectness:
    """测试缓存读写正确性。"""

    @patch("xenon.repl.provider_registry._fetch_provider_models_from_network")
    def test_cache_read_write_roundtrip(self, mock_fetch, temp_cache_dir):
        """测试缓存读写往返正确性。"""
        provider = ProviderInfo(
            name="Test",
            key="test",
            base_url="https://api.test.com",
            env_key="TEST_API_KEY",
            models=["fallback"],
        )
        api_key = "test-key-123"

        # 模拟网络返回
        mock_fetch.return_value = ["model-a", "model-b", "model-c"]

        # 第一次调用，缓存未命中
        models1 = fetch_provider_models(provider, api_key, use_cache=True)
        assert models1 == ["model-a", "model-b", "model-c"]
        assert mock_fetch.call_count == 1

        # 第二次调用，应该从缓存读取
        mock_fetch.reset_mock()
        models2 = fetch_provider_models(provider, api_key, use_cache=True)
        assert models2 == ["model-a", "model-b", "model-c"]
        assert mock_fetch.call_count == 0

        # 验证缓存内容正确
        cache = _load_model_cache()
        assert "test" in cache
        assert cache["test"]["models"] == ["model-a", "model-b", "model-c"]
        assert cache["test"]["api_key_hash"] == _hash_api_key(api_key)
        assert cache["test"]["base_url_hash"] == _hash_base_url(provider.base_url)

    @patch("xenon.repl.provider_registry._fetch_provider_models_from_network")
    def test_cache_file_format(self, mock_fetch, temp_cache_dir):
        """测试缓存文件格式正确。"""
        provider = ProviderInfo(
            name="Test",
            key="test",
            base_url="https://api.test.com",
            env_key="TEST_API_KEY",
            models=["fallback"],
        )
        api_key = "test-key"

        mock_fetch.return_value = ["model1", "model2"]
        fetch_provider_models(provider, api_key, use_cache=True)

        # 验证缓存内容正确
        cache_data = _load_model_cache()
        assert "test" in cache_data
        assert cache_data["test"]["models"] == ["model1", "model2"]
        assert "fetched_at" in cache_data["test"]
        assert "api_key_hash" in cache_data["test"]
        assert "base_url_hash" in cache_data["test"]


class TestParallelRequestCorrectness:
    """测试并行请求正确性。"""

    @patch("xenon.repl.provider_registry._fetch_provider_models_from_network")
    def test_parallel_cache_writes_thread_safe(self, mock_fetch, temp_cache_dir):
        """测试并行写入缓存时的线程安全性。"""

        def mock_network_fetch(provider, api_key):
            time.sleep(0.01)
            return [f"{provider.key}-model"]

        mock_fetch.side_effect = mock_network_fetch

        # 创建多个不同的 provider
        providers = [
            ProviderInfo(
                name=f"Provider{i}",
                key=f"provider{i}",
                base_url=f"https://api{i}.test.com",
                env_key=f"PROVIDER{i}_KEY",
                models=["fallback"],
            )
            for i in range(10)
        ]

        # 并行获取（会并行写入缓存）
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [
                executor.submit(fetch_provider_models, p, f"key{i}", use_cache=True)
                for i, p in enumerate(providers)
            ]
            results = [f.result() for f in as_completed(futures)]

        # 验证所有结果都被正确保存
        cache = _load_model_cache()
        assert len(cache) == 10
        for i in range(10):
            assert f"provider{i}" in cache
            assert cache[f"provider{i}"]["models"] == [f"provider{i}-model"]


class TestCacheExpirationLogic:
    """测试缓存过期逻辑。"""

    @patch("xenon.repl.provider_registry._fetch_provider_models_from_network")
    def test_expired_cache_refetches(self, mock_fetch, temp_cache_dir):
        """测试过期缓存会重新获取。"""
        provider = ProviderInfo(
            name="Test",
            key="test",
            base_url="https://api.test.com",
            env_key="TEST_API_KEY",
            models=["fallback"],
        )
        api_key = "test-key"

        # 创建一个已过期的缓存
        expired_time = time.time() - MODEL_CACHE_TTL - 100
        expired_cache = {
            "test": {
                "models": ["old-model"],
                "fetched_at": expired_time,
                "api_key_hash": _hash_api_key(api_key),
                "base_url_hash": _hash_base_url(provider.base_url),
            }
        }
        _save_model_cache(expired_cache)

        # 应该检测到过期，重新获取
        mock_fetch.return_value = ["new-model"]
        models = fetch_provider_models(provider, api_key, use_cache=True)
        assert models == ["new-model"]
        assert mock_fetch.call_count == 1

    def test_cache_validity_checks(self, temp_cache_dir):
        """测试缓存有效性检查逻辑。"""
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

        # 空模型列表
        empty_models_entry = {
            "models": [],
            "fetched_at": current_time - 100,
            "api_key_hash": _hash_api_key(api_key),
            "base_url_hash": _hash_base_url(base_url),
        }
        assert not _is_cache_valid(empty_models_entry, api_key, base_url, current_time)


class TestAPIKeyChangeInvalidation:
    """测试 API Key 变化时缓存失效。"""

    @patch("xenon.repl.provider_registry._fetch_provider_models_from_network")
    def test_api_key_change_invalidates_cache(self, mock_fetch, temp_cache_dir):
        """测试 API Key 变化时缓存失效。"""
        provider = ProviderInfo(
            name="Test",
            key="test",
            base_url="https://api.test.com",
            env_key="TEST_API_KEY",
            models=["fallback"],
        )

        # 第一次使用 key1
        mock_fetch.return_value = ["model1"]
        models1 = fetch_provider_models(provider, "key1", use_cache=True)
        assert models1 == ["model1"]
        assert mock_fetch.call_count == 1

        # 切换到 key2，缓存应该失效
        mock_fetch.reset_mock()
        mock_fetch.return_value = ["model2"]
        models2 = fetch_provider_models(provider, "key2", use_cache=True)
        assert models2 == ["model2"]
        assert mock_fetch.call_count == 1

        # 再次使用 key2，应该命中缓存
        mock_fetch.reset_mock()
        models3 = fetch_provider_models(provider, "key2", use_cache=True)
        assert models3 == ["model2"]
        assert mock_fetch.call_count == 0

    @patch("xenon.repl.provider_registry._fetch_provider_models_from_network")
    def test_base_url_change_invalidates_cache(self, mock_fetch, temp_cache_dir):
        """测试 base_url 变化时缓存失效。"""
        api_key = "test-key"

        # 第一次使用 url1
        provider1 = ProviderInfo(
            name="Test",
            key="test",
            base_url="https://api1.test.com",
            env_key="TEST_API_KEY",
            models=["fallback"],
        )
        mock_fetch.return_value = ["model1"]
        models1 = fetch_provider_models(provider1, api_key, use_cache=True)
        assert models1 == ["model1"]

        # 切换到 url2，缓存应该失效
        provider2 = ProviderInfo(
            name="Test",
            key="test",
            base_url="https://api2.test.com",
            env_key="TEST_API_KEY",
            models=["fallback"],
        )
        mock_fetch.reset_mock()
        mock_fetch.return_value = ["model2"]
        models2 = fetch_provider_models(provider2, api_key, use_cache=True)
        assert models2 == ["model2"]
        assert mock_fetch.call_count == 1
