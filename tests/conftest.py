"""共享测试夹具。"""

import pytest


@pytest.fixture(autouse=True)
def _reset_provider_probe_cache():
    """每个用例前后清空 provider 探测缓存。

    懒加载契约（v0.9.0）给 get_configured_providers 加了模块级探测缓存
    （_providers_probed / _probed_providers），用于避免同一会话内重复付网络
    代价——启动路径不再探测，/model 等按需入口探测一次后复用。

    但模块级状态在测试进程里是跨用例共享的：前一个用例探测过之后，后一个
    用例调 get_configured_providers 会直接命中缓存、根本不走 mock，于是
    call_count 断言拿到 0、或者拿到上一个用例伪造的 provider 列表。

    这里做全局 autouse 重置。放在 conftest 而不是各测试文件里，是因为凡是
    间接触达该函数的用例都需要隔离（provider_registry / repl / c2 env
    fallback / startup_experience 等多个文件），逐个文件加夹具必然漏。
    """
    from xenon.repl.provider_registry import invalidate_provider_probe

    invalidate_provider_probe()
    yield
    invalidate_provider_probe()
