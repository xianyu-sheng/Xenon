"""共享测试夹具。"""

import pytest


def pytest_addoption(parser):
    """注册 --run-live 开关。

    真实 LLM 端到端测试（pytestmark = pytest.mark.live）会真的发起网络请求，
    在无凭证/受限网络环境下会长时间阻塞甚至超时。默认跳过，只有显式传
    --run-live 时才执行。
    """
    parser.addoption(
        "--run-live",
        action="store_true",
        default=False,
        help="运行标记为 live 的真实 LLM 端到端测试（默认跳过）。",
    )


def pytest_collection_modifyitems(config, items):
    """未显式 --run-live 时，跳过所有 live 标记用例。"""
    if config.getoption("--run-live"):
        return
    skip_live = pytest.mark.skip(reason="live 测试默认跳过，需 --run-live 显式开启")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


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
