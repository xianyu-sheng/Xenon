#!/usr/bin/env python3
"""启动性能基准测试。

演示缓存和并行优化的效果。
"""

import time
from pathlib import Path

# 清除缓存以测试冷启动
cache_path = Path.home() / ".xenon" / "cache" / "provider_models.json"
if cache_path.exists():
    print("清除现有缓存...")
    cache_path.unlink()

print("\n=== 测试 1: 冷启动（无缓存，并行请求）===")
start = time.time()
from xenon.repl.provider_registry import get_configured_providers
providers = get_configured_providers(refresh_models=True, use_cache=True)
cold_time = time.time() - start

print(f"✓ 获取到 {len(providers)} 个 provider")
for p in providers:
    print(f"  - {p.name}: {len(p.models)} 个模型")
print(f"⏱ 耗时: {cold_time:.2f} 秒")

print("\n=== 测试 2: 热启动（使用缓存）===")
start = time.time()
providers = get_configured_providers(refresh_models=True, use_cache=True)
warm_time = time.time() - start

print(f"✓ 获取到 {len(providers)} 个 provider")
print(f"⏱ 耗时: {warm_time:.3f} 秒")

print("\n=== 性能提升总结 ===")
speedup = cold_time / warm_time if warm_time > 0 else 0
print(f"冷启动: {cold_time:.2f}s")
print(f"热启动: {warm_time:.3f}s")
print(f"提速: {speedup:.1f}x")
print(f"节省时间: {cold_time - warm_time:.2f}s")

if warm_time < 0.1:
    print("✓ 热启动 <100ms，达到优化目标")
else:
    print(f"⚠ 热启动 {warm_time*1000:.0f}ms，可能有改进空间")

print(f"\n缓存位置: {cache_path}")
