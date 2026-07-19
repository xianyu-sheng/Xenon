"""
v0.4.0 Step 11: Benchmark Fetcher — 自动获取模型公开评测分数以确定队列层级。

使用 HuggingFace Open LLM Leaderboard 数据集获取基准测试分数。
结果缓存到 ~/.xenon/benchmark_cache.json（TTL 7 天）。
失败时静默回退到 _infer_capability 的层级推断。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CACHE_DIR = Path.home() / ".xenon"
CACHE_PATH = CACHE_DIR / "benchmark_cache.json"
CACHE_TTL_S = 86400 * 7  # 7 days

# HuggingFace Open LLM Leaderboard v3 API
HF_LEADERBOARD_URL = "https://huggingface.co/api/leaderboards/open-llm-leaderboard/results"

# 模型别名到排行榜名称的映射
_MODEL_NAME_OVERRIDES: dict[str, str] = {
    "deepseek-v4-pro": "deepseek-ai/DeepSeek-V4-Pro",
    "deepseek-v4-flash": "deepseek-ai/DeepSeek-V4-Flash",
    "deepseek-chat": "deepseek-ai/DeepSeek-V3-0324",
    "qwen3-235b-a22b": "Qwen/Qwen3-235B-A22B",
    "qwq-32b": "Qwen/QwQ-32B",
    "llama-4-maverick": "meta-llama/Llama-4-Maverick-17B-128E",
    "llama-4-scout": "meta-llama/Llama-4-Scout-17B-16E",
    "mixtral-8x22b": "mistralai/Mixtral-8x22B-Instruct-v0.1",
    "gemma-3-27b": "google/gemma-3-27b-it",
    "phi-4": "microsoft/phi-4",
}

# 基准分数到 tier 的映射
_TIER_BY_SCORE: list[tuple[float, int]] = [
    (0.80, 5),   # ≥ 80% → tier 5 (flagship)
    (0.65, 4),   # ≥ 65% → tier 4
    (0.50, 3),   # ≥ 50% → tier 3
    (0.35, 2),   # ≥ 35% → tier 2
    (0.0,  1),   # below → tier 1
]


class BenchmarkFetcher:
    """从公共数据源获取并缓存模型基准测试结果。"""

    def __init__(self, cache_path: str | None = None):
        self._cache_path = Path(cache_path) if cache_path else CACHE_PATH
        self._cache: dict[str, dict[str, Any]] = {}
        self._load_cache()

    def fetch(self, model_id: str) -> dict[str, Any] | None:
        """获取模型的基准测试数据。先查缓存，miss 则远程拉取。"""
        alias = model_id.split("/")[-1] if "/" in model_id else model_id
        cached = self._cache.get(alias)
        if cached and self._is_fresh(cached):
            return cached
        return self._fetch_remote(alias)

    def estimate_tier(self, model_id: str, fallback_tier: int = 3) -> int:
        """使用基准测试分数估计模型的层级。

        如果获取失败或缓存中没有数据，返回 fallback_tier。
        """
        data = self.fetch(model_id)
        if not data:
            return fallback_tier

        avg = self._average_score(data)
        if avg is not None:
            for threshold, tier in _TIER_BY_SCORE:
                if avg >= threshold:
                    return tier
        return fallback_tier

    @staticmethod
    def _average_score(data: dict[str, Any]) -> float | None:
        """从基准测试结果计算平均分。"""
        scores = []
        for key in ("average", "mmlu", "bbh", "gsm8k", "humaneval", "truthfulqa",
                    "mmlu_pro", "gpqa", "musr", "ifeval"):
            val = data.get(key)
            if val is not None:
                try:
                    scores.append(float(val))
                except (ValueError, TypeError):
                    pass
        return sum(scores) / len(scores) if scores else None

    def _fetch_remote(self, alias: str) -> dict[str, Any] | None:
        """从 HuggingFace Open LLM Leaderboard 获取数据。"""
        hf_name = _MODEL_NAME_OVERRIDES.get(alias, alias)
        try:
            import urllib.request
            import urllib.error

            url = f"{HF_LEADERBOARD_URL}?search={hf_name}"
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=10.0) as resp:  # type: ignore[attr-defined]
                data = json.loads(resp.read().decode())

            if isinstance(data, list) and data:
                result = data[0]
                self._cache[alias] = {
                    **result,
                    "_cached_at": time.time(),
                }
                self._save_cache()
                return result
        except Exception as e:
            logger.debug(f"获取 {alias} 基准测试数据失败: {e}")
        return None

    def _is_fresh(self, cached: dict) -> bool:
        ts = cached.get("_cached_at", 0)
        return (time.time() - ts) < CACHE_TTL_S

    def _load_cache(self) -> None:
        try:
            if self._cache_path.exists():
                self._cache = json.loads(self._cache_path.read_text())
        except Exception:
            self._cache = {}

    def _save_cache(self) -> None:
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(
                json.dumps(self._cache, ensure_ascii=False, indent=2)
            )
        except Exception as e:
            logger.debug(f"保存基准测试缓存失败: {e}")


# 模块级单例，延迟初始化
_fetcher: BenchmarkFetcher | None = None


def get_benchmark_fetcher() -> BenchmarkFetcher:
    """获取 BenchmarkFetcher 单例。"""
    global _fetcher
    if _fetcher is None:
        _fetcher = BenchmarkFetcher()
    return _fetcher
