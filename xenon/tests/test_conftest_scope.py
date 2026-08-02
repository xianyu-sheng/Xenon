"""回归防护：``xenon/tests/`` 也必须拿到仓库级 autouse fixture。

这是本次修复的核心断言 —— 修复前此文件会失败（4 个 fixture 全部缺失）。
配套断言见 ``tests/test_conftest_scope.py``。
"""

REQUIRED_AUTOUSE_FIXTURES = {
    "_disable_security_for_tests",
    "_isolate_cache_telemetry",
    "_auto_confirm_destructive",
    "_isolate_chat_completion_mock",
}


def test_root_autouse_fixtures_apply_under_xenon_tests_tree(request):
    """``xenon/tests/`` 下必须拿到全部 4 个 autouse fixture。

    修复前：pytest 的 conftest 按目录树生效，``tests/conftest.py`` 管不到
    ``xenon/tests/``，此断言会报 4 个全缺。
    """
    applied = set(request.fixturenames)
    missing = REQUIRED_AUTOUSE_FIXTURES - applied
    assert not missing, (
        f"xenon/tests/ 缺少仓库级 autouse fixture: {sorted(missing)}。"
        "这些 fixture 必须放在仓库根目录 conftest.py 才能覆盖两棵测试树。"
    )


def test_cache_telemetry_is_redirected_away_from_home():
    """``_isolate_cache_telemetry`` 生效时，缓存目录不得指向用户家目录。

    ``xenon/tests/`` 里的 cache 用例会写遥测；缺少该 fixture 时会污染
    真实的 ``~`` 目录。
    """
    import os
    from pathlib import Path

    cache_dir = os.environ.get("XENON_CACHE_DIR")
    assert cache_dir, "XENON_CACHE_DIR 未设置，_isolate_cache_telemetry 未生效"
    assert Path.home() not in Path(cache_dir).parents, (
        f"缓存目录仍在家目录下: {cache_dir}"
    )
