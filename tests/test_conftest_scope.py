"""回归防护：仓库级 autouse fixture 必须对两棵测试树同时生效。

历史问题：4 个 autouse fixture 只放在 ``tests/conftest.py``，导致
``xenon/tests/`` 下的 8 个文件一个都拿不到。其中
``_isolate_chat_completion_mock`` 是 ``chat_completion`` mock 泄漏的唯一兜底，
而 ``xenon/tests/`` 恰好放着直接打 LLM 客户端的 cache 用例。

本文件断言 fixture 在 ``tests/`` 生效；``xenon/tests/`` 侧的同名断言见
``xenon/tests/test_conftest_scope.py``。两者都过才说明提到根目录成功。
"""

REQUIRED_AUTOUSE_FIXTURES = {
    "_disable_security_for_tests",
    "_isolate_cache_telemetry",
    "_auto_confirm_destructive",
    "_isolate_chat_completion_mock",
}


def test_root_autouse_fixtures_apply_under_tests_tree(request):
    """``tests/`` 下必须拿到全部 4 个 autouse fixture。"""
    applied = set(request.fixturenames)
    missing = REQUIRED_AUTOUSE_FIXTURES - applied
    assert not missing, (
        f"tests/ 缺少仓库级 autouse fixture: {sorted(missing)}。"
        "检查仓库根目录 conftest.py 是否存在。"
    )


def test_chat_completion_is_the_real_function(request):
    """``_isolate_chat_completion_mock`` 生效时，进入用例前不应残留 mock。"""
    import xenon.engine.base as engine_base
    import xenon.utils.llm_client as llm_client

    for mod, attr in (
        (engine_base, "chat_completion"),
        (llm_client, "chat_completion"),
        (llm_client, "chat_completion_stream"),
    ):
        fn = getattr(mod, attr)
        assert callable(fn), f"{mod.__name__}.{attr} 不可调用"
        name = getattr(fn, "__name__", "")
        assert "fake" not in name.lower() and "mock" not in name.lower(), (
            f"{mod.__name__}.{attr} 残留 mock: {name}（fixture 未兜住泄漏）"
        )
